"""reselect --from-counter (the only reselect mode), supersede marking, and the CLI's init and upload refusal."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fakes import srt

from src.autoslice.manual_lane import cli
from src.autoslice.manual_lane.reselect import (
    load_cues,
    reselect_from_counter,
    supersede_candidate,
)
from src.autoslice.manual_lane.state import ClipState

ORIG = "auto_210119_649_846"


def _orig_task(tmp_path: Path, adopted: dict | None):
    task = tmp_path / "task"
    tmp_path.mkdir(parents=True, exist_ok=True)
    bcut = tmp_path / "bcut.srt"
    bcut.write_text(srt(40, step=10.0), encoding="utf-8")   # cue k spans (k-1)*10 .. k*10 seconds
    clip = {"cid": ORIG, "recording_date": "2026-10-24", "source_name": "src.flv", "start_ms": 50_000,
            "end_ms": 120_000, "start_cue": 6, "end_cue": 12, "hook": "钩子", "mac_bcut_srt": str(bcut),
            "remote_dir": f"/r/2026-10-24/{ORIG}", "proposal": {"start_cue": 6, "end_cue": 12}}
    orig = ClipState.create(task / "state" / f"{ORIG}.json", clip=clip, session={"speaker_mode": "uniform_host"})
    if adopted is not None:
        path = task / "clips" / ORIG / "boundary" / "decision.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"adopted": adopted}), encoding="utf-8")
    return task, orig


def test_from_counter_opens_a_new_candidate_from_cpas_last_cut_and_leaves_the_original_alone(tmp_path):
    task, orig = _orig_task(tmp_path, {"start_cue": 8, "end_cue": 20, "start_ms": 70000, "end_ms": 200000})
    before = (task / "state" / f"{ORIG}.json").read_text(encoding="utf-8")
    result = reselect_from_counter(task_dir=task, orig=orig, cues=load_cues(Path(orig.data["clip"]["mac_bcut_srt"])),
                                   authority="U103", quote="按长窗口重选")
    assert result.ok, result.detail
    new = ClipState.load(task / "state" / f"{result.new_cid}.json")
    assert (new.data["clip"]["start_cue"], new.data["clip"]["end_cue"]) == (8, 20)
    assert new.data["clip"]["reselected_from"] == ORIG
    assert new.data["clip"]["remote_dir"].endswith(f"/{result.new_cid}")
    assert (task / "state" / f"{ORIG}.json").read_text(encoding="utf-8") == before


def test_from_counter_needs_a_boundary_decision_and_a_different_cut(tmp_path):
    task, orig = _orig_task(tmp_path, None)
    cues = load_cues(Path(orig.data["clip"]["mac_bcut_srt"]))
    assert reselect_from_counter(task_dir=task, orig=orig, cues=cues, authority="U103",
                                 quote="q").reason == "RESELECT_NO_COUNTER"
    task, orig = _orig_task(tmp_path / "same", {"start_cue": 6, "end_cue": 12, "start_ms": 0, "end_ms": 0})
    cues = load_cues(Path(orig.data["clip"]["mac_bcut_srt"]))
    assert reselect_from_counter(task_dir=task, orig=orig, cues=cues, authority="U103",
                                 quote="q").reason == "RESELECT_UNCHANGED"


def test_reselect_refuses_an_empty_authority(tmp_path):
    task, orig = _orig_task(tmp_path, {"start_cue": 8, "end_cue": 20, "start_ms": 0, "end_ms": 0})
    with pytest.raises(ValueError):
        reselect_from_counter(task_dir=task, orig=orig, cues=[], authority=" ", quote="q")


def test_supersede_marks_status_and_keeps_the_file(tmp_path):
    task, orig = _orig_task(tmp_path, None)
    successor = ClipState.create(task / "state" / "auto_210119_1_2.json",
                                 clip={"cid": "auto_210119_1_2"}, session={})
    assert successor is not None
    result = supersede_candidate(task_dir=task, cid=ORIG, by="auto_210119_1_2", authority="U103",
                                 quote="按长窗口重选", reason="新窗口")
    assert result.ok
    raw = ClipState.load(task / "state" / f"{ORIG}.json").data["clip"]
    assert raw["status"] == "SUPERSEDED" and raw["superseded_by"] == "auto_210119_1_2"
    assert raw["superseded"]["reason"] == "新窗口"
    assert (task / "state" / f"{ORIG}.json").exists()


def test_cli_exposes_only_the_kept_subcommands():
    parser = cli.build_parser()
    names = set(parser._subparsers._group_actions[0].choices)
    assert names == {"init", "auto", "discover", "compile", "run", "status", "listen-queue", "listen-import", "import-subtitles", "reselect",
                     "supersede", "trim", "score", "revise", "import-cover", "resolve-dissent", "redo", "record-title-receipt"}


def test_cli_init_creates_the_state_from_a_selection_entry(tmp_path):
    sel = tmp_path / "selection.json"
    sel.write_text(json.dumps({"input_pool": [{"cid": "c1", "segment_path": "/x/22966160_20261024-19-31-06.json",
                                                "start_ms": 1000, "end_ms": 61000, "hook": "h",
                                                "selection_scorecard": {"start_cue": 1, "end_cue": 5}}]}),
                   encoding="utf-8")
    session = tmp_path / "session.json"
    session.write_text(json.dumps({"recording_date": "2026-10-24", "participants": [["李豆沙"]],
                                   "own_uploader_mids": [1]}), encoding="utf-8")
    bcut = tmp_path / "b.srt"
    bcut.write_text(srt(5), encoding="utf-8")
    task = tmp_path / "t"
    rc = cli.main(["init", "--task-dir", str(task), "--cid", "c1", "--selection", str(sel),
                   "--session", str(session), "--mac-bcut-srt", str(bcut)])
    assert rc == 0
    state = ClipState.load(task / "state" / "c1.json")
    assert state.data["clip"]["proposal"] == {"start_cue": 1, "end_cue": 5}
    assert state.data["steps"] == {} and state.data["upload"]["enabled"] is False


def test_upload_without_a_quote_is_refused_before_any_remote_call(tmp_path, capsys):
    rc = cli.main(["run", "--task-dir", str(tmp_path / "t"), "--cid", "c1", "--upload"])
    assert rc == 2 and "--quote" in capsys.readouterr().out


# ----- a superseded candidate is skipped where the lane looks at its other candidates ------------------------------------
def _siblings(tmp_path: Path) -> Path:
    """A task with the live candidate `live`, a replaced one `old` (SUPERSEDED by `live`) and the asking one `me`."""
    task = tmp_path / "task"
    for cid in ("me", "live", "old"):
        clip = {"cid": cid, "recording_date": "2026-10-24", "source_name": "src.flv", "start_ms": 0, "end_ms": 1000,
                "hook": "h", "remote_dir": f"/r/{cid}"}
        state = ClipState.create(task / "state" / f"{cid}.json", clip=clip, session={})
        if cid == "old":
            state.data["clip"].update(status="SUPERSEDED", superseded_by="live")
            state.save()
    return task


def test_listen_queue_skips_a_superseded_candidate(tmp_path, monkeypatch):
    task = _siblings(tmp_path)
    asked = []
    monkeypatch.setattr(cli, "clip_queue", lambda **kw: asked.append(kw["cid"]) or {"cid": kw["cid"], "items": [1]})
    monkeypatch.setattr(cli, "write_queue", lambda out, plans, remote_for: {"clips": [p["cid"] for p in plans]})
    assert cli.main(["listen-queue", "--task-dir", str(task)]) == 0
    assert sorted(asked) == ["live", "me"]


def test_batch_titles_and_recent_covers_skip_a_superseded_candidate(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from src.autoslice.manual_lane import title_history
    from src.autoslice.manual_lane.step6_cover import _lane_covers

    task = _siblings(tmp_path)
    monkeypatch.setattr(title_history, "load_publication_registry", lambda: {"entries": []})
    for cid in ("live", "old"):
        (task / "clips" / cid / "title").mkdir(parents=True)
        (task / "clips" / cid / "title/title.txt").write_text(f"【李豆沙】{cid}的标题\n", encoding="utf-8")
        (task / "clips" / cid / "title/candidates.json").write_text(json.dumps({"cover_route": {
            "method": "skill.candidates", "at": "2026-10-24T10:00:00Z", "recording_date": "2026-10-24",
            "chosen": {"route": "cutout"}}}), encoding="utf-8")
    history = title_history.recent_titles(task / "state/me.json", task / "clips/me", "me")
    assert history.batch == ("【李豆沙】live的标题",)
    covers = _lane_covers(SimpleNamespace(clip_dir=task / "clips/me"))
    assert [c["cid"] for c in covers] == ["live"]
