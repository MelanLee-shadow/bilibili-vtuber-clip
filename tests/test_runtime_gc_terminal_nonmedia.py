"""Terminal runs give up bulk JSON caches and repository copies, never records.

Synthetic trees only; sizes straddle the 1 MiB decision-record cap.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import time

import pytest

from src.autoslice import runtime_gc as gc
from tests.test_runtime_gc import observed_unlinks  # noqa: F401  (Linux inotify stand-in on macOS)

BIG = gc.DECISION_RECORD_MAX_BYTES + 1


def put(path: Path, size: int = 8, content: bytes | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content if content is not None else b"x" * size)
    return path


def age_tree(root: Path, days: float = 10) -> None:
    stamp = time.time() - days * gc.DAY
    for directory, dirs, files in os.walk(root, topdown=False):
        for name in files + dirs:
            os.utime(Path(directory) / name, (stamp, stamp), follow_symlinks=False)
        os.utime(directory, (stamp, stamp))


def plan(base: Path, **kwargs) -> dict:
    return gc.collect(base, home=base / "absent-home", live=gc.Activity(), free_bytes=30 * gc.GIB, **kwargs)


def targets(value: dict) -> dict[Path, str]:
    return {Path(row["path"]): row["lifecycle"] for row in value["targets"]}


def declare(run: Path, status: str = "abandoned", schema: str = gc.RUN_LIFECYCLE_SCHEMA) -> Path:
    return put(run / gc.RUN_LIFECYCLE, content=json.dumps({"schema_version": schema, "status": status}).encode())


def run_tree(base: Path) -> dict:
    run = base / "private-runs" / "discovery-v01-20261009"
    tree = {
        "chat": put(run / "work" / "current.chat-authority.json", BIG),
        "flags": put(run / "work" / "semantictalk_1_2.review-flags.json", 2 * BIG),
        "jsonl": put(run / "work" / "packets.jsonl", BIG),
        "copy_py": put(run / "code" / "src" / "autoslice" / "runtime_gc.py"),
        "copy_script": put(run / "code" / "scripts" / "runtime_gc.py"),
        "copy_pyc": put(run / "code" / "src" / "autoslice" / "__pycache__" / "runtime_gc.cpython-311.pyc"),
        "loose_pycache": put(run / "selection" / "__pycache__" / "selector.cpython-311.pyc"),
        # Kept: decision kinds at any size, small JSON, text records, run-local
        # scripts and non-code repository assets.
        "result": put(run / "RESULT.json", BIG),
        "record": put(run / "work" / "semantictalk_1_2.record.json", BIG),
        "checkpoint": put(run / "work" / "semantictalk_1_2.final-review-carryover.checkpoint.json", BIG),
        "manifest": put(run / "pkg" / "clip.upload_manifest.json", BIG),
        "small": put(run / "work" / "verdict-cache.json", gc.DECISION_RECORD_MAX_BYTES),
        "spec": put(run / "SPEC.json"),
        "srt": put(run / "work" / "clip.srt", BIG),
        "orchestrator": put(run / "scripts" / "oci3_orchestrate.py"),
        "font": put(run / "code" / "assets" / "font.ttf", BIG),
        "commit": put(run / "code" / "DEPLOYED_COMMIT", content=b"a" * 40 + b"\n"),
    }
    tree["run"] = run
    return tree


RECLAIMED = {"chat": "terminal-cache", "flags": "terminal-cache", "jsonl": "terminal-cache",
             "copy_py": "terminal-code", "copy_script": "terminal-code", "copy_pyc": "terminal-code",
             "loose_pycache": "terminal-code"}


def test_abandoned_run_reclaims_bulk_caches_and_repo_copies_only(tmp_path):
    tree = run_tree(tmp_path)
    declare(tree["run"])
    age_tree(tmp_path)
    value = plan(tmp_path)
    assert targets(value) == {tree[key]: lifecycle for key, lifecycle in RECLAIMED.items()}
    code = [row for row in value["targets"] if row["kind"] == "code"]
    assert code and all(row["commit_hints"] == ["a" * 40] for row in code)
    assert value["summary_by_lifecycle"]["terminal-cache"]["logical_bytes"] == 4 * BIG
    assert value["code_source_allocated_bytes"].keys() == {"recorded_commit"}


@pytest.mark.parametrize("marker", [
    None,                                   # idle run: media clock only
    ("done", gc.RUN_LIFECYCLE_SCHEMA),      # not a terminal status
    ("abandoned", "run-lifecycle.v0"),      # unknown record schema
])
def test_non_terminal_or_free_form_runs_keep_all_non_media(tmp_path, marker):
    tree = run_tree(tmp_path)
    put(tree["run"] / "RESULT.json", content=b'{"status": "FAILED_PRESERVED_NO_RETRY"}')
    if marker:
        declare(tree["run"], *marker)
    age_tree(tmp_path)
    assert not plan(tmp_path)["targets"]


def test_recent_write_explicit_preserve_and_open_file_guard_non_media(tmp_path):
    tree = run_tree(tmp_path)
    declare(tree["run"])
    age_tree(tmp_path)
    put(tree["run"] / "work" / "note.txt")  # a write this hour protects the whole run
    value = plan(tmp_path)
    assert not value["targets"] and value["retained_nonmedia_logical_bytes"]["recent-write"] > 0
    age_tree(tmp_path)
    value = plan(tmp_path, preserve={str(tree["chat"])})
    assert tree["chat"] not in targets(value)
    busy = gc.collect(tmp_path, home=tmp_path / "absent", live=gc.Activity(paths={tree["flags"]}),
                      free_bytes=30 * gc.GIB)
    assert not busy["targets"]


def test_rejected_out_candidate_gives_up_caches_but_keeps_records(tmp_path):
    root = tmp_path / "out" / "2026-10-01" / "c1"
    cache = put(root / "c1.chat-authority.json", BIG)
    record = put(root / "c1.record.json", BIG)
    put(tmp_path / "state" / "2026-10-01.json",
        content=json.dumps({"picks": [{"candidate_id": "c1", "status": "candidate_rejected"}]}).encode())
    age_tree(tmp_path)
    assert targets(plan(tmp_path)) == {cache: "terminal-cache"}
    assert record.exists()


def test_active_current_row_outranks_the_runs_abandoned_record(tmp_path):
    root = tmp_path / "private-runs" / "c1-run"
    put(root / "SPEC.json", content=b'{"candidate_id": "c1", "date": "2026-10-01"}')
    put(root / "c1.chat-authority.json", BIG)
    declare(root)
    put(tmp_path / "state" / "2026-10-01.json",
        content=json.dumps({"picks": [{"candidate_id": "c1", "status": "processing"}]}).encode())
    age_tree(tmp_path)
    assert not plan(tmp_path)["targets"]


def test_reports_and_candidates_never_give_up_non_media(tmp_path):
    for category in ("reports", "candidates"):
        run = tmp_path / category / "evidence-run"
        put(run / "x.chat-authority.json", BIG)
        declare(run)
    age_tree(tmp_path)
    assert not plan(tmp_path)["targets"]


def test_published_candidate_subtree_only_and_recall_inputs_stay(tmp_path):
    from tests.test_runtime_gc_publication import CID, RUN, incident_tree
    base = tmp_path / "autoslice"
    tree = incident_tree(base)
    out = base / RUN / "production-normal" / "output"
    own_cache = put(out / CID / f"{CID}.review-flags.json", BIG)
    live = put(out / CID / f"{CID}.chat-authority.json", BIG)  # SOURCE parent live authority
    sibling = put(out / "semantictalk_1553130_1796100" / "semantictalk_1553130_1796100.review-flags.json", BIG)
    run_cache = put(base / RUN / "selection-0-1800" / "SELECTION-DIAGNOSTICS.cache.json", BIG)
    age_tree(base)
    found = targets(gc.collect(base, home=base / "absent", live=gc.Activity(), free_bytes=30 * gc.GIB,
                               registry=tree["registry"]))
    assert found.get(own_cache) == "terminal-cache"
    # The recall closure keeps the live authority; another clip and run-level
    # files of a shared run are not this candidate's to give up.
    assert live not in found and sibling not in found and run_cache not in found


def test_cli_dry_run_with_stub_process_table_reports_new_classes(tmp_path):
    tree = run_tree(tmp_path / "base")
    declare(tree["run"])
    age_tree(tmp_path / "base")
    proc = tmp_path / "proc"
    put(proc / "locks", content=b"")
    output = tmp_path / "plan.json"
    assert gc.main(["--base", str(tmp_path / "base"), "--home", str(tmp_path / "absent"),
                    "--json", str(output), "--proc", str(proc)]) == 0
    written = json.loads(output.read_text())
    assert {row["lifecycle"] for row in written["targets"]} == {"terminal-cache", "terminal-code"}


def test_apply_records_code_source_with_git_blob_ids_before_unlinking(tmp_path, monkeypatch, observed_unlinks):
    tree = run_tree(tmp_path)
    declare(tree["run"])
    age_tree(tmp_path)
    approved = plan(tmp_path)
    monkeypatch.setattr(gc, "activity", lambda *_: gc.Activity())
    result = gc.apply_plan(approved)
    assert result["status"] == "APPLIED"
    assert all(not tree[key].exists() for key in RECLAIMED)
    assert all(tree[key].exists() for key in ("result", "record", "srt", "orchestrator", "font", "small"))
    records = list((tmp_path / "state" / "runtime-gc-code-sources").glob("*.json"))
    assert len(records) == 1
    document = json.loads(records[0].read_text())
    assert document["run"] == str(tree["run"]) and document["recorded_commits"] == ["a" * 40]
    sample = tmp_path / "blob-sample"
    sample.write_bytes(b"x" * 8)
    expected = subprocess.run(["git", "hash-object", str(sample)], capture_output=True, text=True, check=True).stdout.strip()
    assert document["files"]["code/src/autoslice/runtime_gc.py"] == {"git_blob_sha1": expected, "bytes": 8}


def test_unwritable_code_source_record_refuses_the_whole_batch(tmp_path, monkeypatch, observed_unlinks):
    tree = run_tree(tmp_path)
    declare(tree["run"])
    age_tree(tmp_path)
    approved = plan(tmp_path)
    put(tmp_path / "state" / "runtime-gc-code-sources")  # a file where the record directory belongs
    monkeypatch.setattr(gc, "activity", lambda *_: gc.Activity())
    result = gc.apply_plan(approved)
    assert result["status"] == "REFUSED" and result["removed"] == []
    assert all(tree[key].exists() for key in RECLAIMED)


def test_publication_does_not_conclude_a_later_repair_run_of_the_same_candidate(tmp_path):
    from tests.test_runtime_gc_publication import CID, incident_tree
    base = tmp_path / "autoslice"
    tree = incident_tree(base)
    repair = base / "private-runs" / "b-unified-b3180c21-20261021" / CID / "normal-v1" / "output" / CID
    caches = {put(repair / f"{CID}.chat-authority.json", BIG), put(repair / f"{CID}.review-flags.json", BIG)}
    copy = put(repair / "replacement_recuts" / f"{CID}.recut.mp4")
    age_tree(base / "private-runs" / "b-unified-b3180c21-20261021", days=3)
    found = targets(gc.collect(base, home=base / "absent", live=gc.Activity(), free_bytes=30 * gc.GIB,
                               registry=tree["registry"]))
    # Written after the publication elsewhere: idle clock for media, records kept.
    assert copy not in found and not caches & set(found)
    age_tree(base / "private-runs" / "b-unified-b3180c21-20261021", days=8)
    found = targets(gc.collect(base, home=base / "absent", live=gc.Activity(), free_bytes=30 * gc.GIB,
                               registry=tree["registry"]))
    assert found.get(copy) == "idle" and not caches & set(found)


@pytest.mark.parametrize("declared", [False, True])
def test_daily_verdict_alone_does_not_release_an_operator_runs_non_media(tmp_path, declared):
    run = tmp_path / "private-runs" / "c1-rework"
    put(run / "SPEC.json", content=b'{"candidate_id": "c1", "date": "2026-10-01"}')
    cache = put(run / "c1.chat-authority.json", BIG)
    put(tmp_path / "state" / "2026-10-01.json",
        content=json.dumps({"picks": [{"candidate_id": "c1", "status": "candidate_rejected"}]}).encode())
    if declared:
        declare(run, "rejected")
    age_tree(tmp_path)
    assert (cache in targets(plan(tmp_path))) is declared
