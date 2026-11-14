"""Concurrent cover routes on a REAL on-disk remote (step 6, 70-cover): no route may share a writable file with another.

The first real-model run lost routes because every job wrote the same `s6-cover.sh`.  Here the lane's own cover_run.run_all
drives four jobs at once through the real adapters.LocalRemote (scripts written to disk and run by bash), against a stand-in
for cover_candidates.py that meets the other jobs at a file barrier (so the jobs are really in flight together), writes its
own result file with its own content, and, for one route, exits 0 without any result.  The assertions read the disk: each
job's own script and result exist with their own content, and the route that left nothing is a recorded tool failure with
its own whole output in its own file."""

from __future__ import annotations

import json
from pathlib import Path

from fakes import FakeCpa, FakeRemote, make_ctx

from src.autoslice.manual_lane import cover_run
from src.autoslice.manual_lane.adapters import LocalRemote

STAND_IN = '''
import json, pathlib, sys, time
job = json.loads(pathlib.Path(sys.argv[1]).read_text())
routes = job["routes"]
barrier = pathlib.Path(job["barrier"])
barrier.mkdir(parents=True, exist_ok=True)
(barrier / "+".join(routes)).write_text("here")
deadline = time.time() + 30
while len(list(barrier.iterdir())) < job["jobs"] and time.time() < deadline:
    time.sleep(0.05)
if len(list(barrier.iterdir())) < job["jobs"]:
    print("OTHER JOBS NEVER ARRIVED", file=sys.stderr)
    sys.exit(7)
print("OUT-OF-" + "+".join(routes) + " " + "x" * 5000)           # longer than the 400-character note
print("ERR-OF-" + "+".join(routes), file=sys.stderr)
if routes == ["screenshot"]:
    sys.exit(0)                                                 # exits 0 and leaves no result file
rows = [{"id": r, "route": r, "status": "RENDERED", "made_by": "+".join(routes)} for r in routes]
for r in routes:
    for ext in ("jpg", "png"):
        pathlib.Path(job["cand"], f"{r}.{ext}").write_bytes(("PICTURE-" + r).encode())
pathlib.Path(job["cand"], job["result"]).write_text(json.dumps({"rows": rows, "made_by": "+".join(routes)}))
'''


def test_concurrent_routes_each_keep_their_own_script_result_and_output_on_a_real_disk(tmp_path, monkeypatch):
    remote_dir = tmp_path / "media" / "clip"
    scripts = tmp_path / "media" / "scripts"
    cand = remote_dir / "cover/cand"
    cand.mkdir(parents=True)
    (remote_dir / "cover/cover_candidates.py").write_text(STAND_IN, encoding="utf-8")
    monkeypatch.setattr(cover_run, "root_script", lambda cmd, **_k: cmd + "\n")                  # no sudo on the test machine
    monkeypatch.setattr(cover_run, "remote_python", lambda args, **_k: f"python3 -B {args}")
    ctx = make_ctx(tmp_path / "mac", remote=FakeRemote(), cpa=FakeCpa([]))
    ctx.adapters.remote = LocalRemote(scripts_dir=str(scripts))
    ctx.remote_dir = str(remote_dir)
    groups = [["screenshot"], ["emote"], ["cutout", "ai_sticker"], ["ai_scene"]]
    job = {"barrier": str(remote_dir / "barrier"), "jobs": len(groups), "cand": str(cand)}

    runs = {tuple(r.routes): r for r in cover_run.run_all(ctx, job, groups)}

    # every job's own script is on disk, under its own name, and carries its own job file
    names = sorted(p.name for p in scripts.iterdir())
    assert names == ["s6-cover-job-ai_scene.sh", "s6-cover-job-cutout_ai_sticker.sh", "s6-cover-job-emote.sh",
                     "s6-cover-job-screenshot.sh"]
    for group in groups:
        key = "+".join(group)
        script = (scripts / f"s6-cover-job-{key.replace('+', '_')}.sh").read_text(encoding="utf-8")
        assert f"job-{key}.json" in script and all(f"job-{'+'.join(other)}.json" not in script for other in groups if other != group)

    # every job that made something has its own result, with its own content; the picture files are its own
    for group in groups[1:]:
        key = "+".join(group)
        on_disk = json.loads((cand / f"result-{key}.json").read_text(encoding="utf-8"))
        assert on_disk["made_by"] == key
        run = runs[tuple(group)]
        assert run.result["made_by"] == key and [r["id"] for r in run.rows] == group and not run.tool_failure
        assert json.loads((ctx.clip_dir / f"cover/cand/result-{key}.json").read_text(encoding="utf-8"))["made_by"] == key
        for route in group:
            assert (ctx.clip_dir / f"cover/cand/{route}.jpg").read_bytes() == f"PICTURE-{route}".encode()

    # the route that exited 0 without a result is a tool failure with its own whole output, never "infeasible"
    lost = runs[("screenshot",)]
    assert lost.result is None and lost.tool_failure == "TOOL_NO_RESULT_RC0"
    assert "OUT-OF-screenshot" in Path(lost.stdout_path).read_text(encoding="utf-8")
    assert Path(lost.stdout_path).read_text(encoding="utf-8").count("x") == 5000           # whole, not the 400-character tail
    assert Path(lost.stderr_path).read_text(encoding="utf-8").strip() == "ERR-OF-screenshot"
    assert cover_run.infeasible_routes(list(runs.values()), ["screenshot"]) == []
    failures = cover_run.tool_failures(list(runs.values()))
    assert [(f["routes"], f["reason"], f["stdout"], f["stderr"]) for f in failures] == [
        (["screenshot"], "TOOL_NO_RESULT_RC0", lost.stdout_path, lost.stderr_path)]
    assert len({run.stdout_path for run in runs.values()}) == len(groups)                  # one output file per job
    for run in runs.values():
        assert ("OUT-OF-" + "+".join(run.routes)) in Path(run.stdout_path).read_text(encoding="utf-8")
