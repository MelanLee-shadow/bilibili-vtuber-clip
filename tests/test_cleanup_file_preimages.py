"""Cleanup identity regressions use only disposable pytest files, never live media."""

from __future__ import annotations

import hashlib
import importlib
import json
import os
import stat
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
apply = importlib.import_module("cleanup_apply_plan")


def snapshot(path):
    s = path.stat()
    return {
        "schema_version": "cleanup-file-preimage.v1",
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "dev": s.st_dev,
        "ino": s.st_ino,
        "bytes": s.st_size,
        "mtime_ns": s.st_mtime_ns,
        "ctime_ns": s.st_ctime_ns,
        "mode": stat.S_IMODE(s.st_mode),
        "uid": s.st_uid,
        "gid": s.st_gid,
        "nlink": s.st_nlink,
    }


@pytest.fixture
def fixture(tmp_path, monkeypatch):
    base = tmp_path.resolve() / "runtime"
    target = base / "out" / "2026-08-22" / "auto_100000_1_2" / "working.wav"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"original")
    row = {
        "path": str(target),
        "bytes": 8,
        "day": "2026-08-22",
        "cls": "working.wav",
        "owner": "auto_100000_1_2",
        "owner_status": "published",
        "preimage": snapshot(target),
    }
    monkeypatch.setattr(apply, "quiet_window", lambda base: [])
    return base, target, row, tmp_path / "plan.json"


def invoke(f, monkeypatch, rows=None, dry=False):
    base, target, row, plan = f
    plan.write_text(json.dumps(rows if rows is not None else [row]))
    monkeypatch.setattr(
        sys,
        "argv",
        ["cleanup_apply_plan.py", str(plan), "--base", str(base)] + (["--dry-run"] if dry else []),
    )
    return apply.main()


def test_equal_size_rewrite_is_not_deleted(fixture, monkeypatch):
    _, target, row, _ = fixture
    target.write_bytes(b"modified")
    os.utime(target, ns=(row["preimage"]["mtime_ns"], row["preimage"]["mtime_ns"]))
    assert invoke(fixture, monkeypatch) == 2
    assert target.read_bytes() == b"modified"


def test_missing_preimage_cannot_delete(fixture, monkeypatch):
    _, target, row, _ = fixture
    row.pop("preimage")
    assert invoke(fixture, monkeypatch) == 2
    assert target.exists()


def test_same_bytes_new_inode_cannot_delete(fixture, monkeypatch):
    _, target, row, _ = fixture
    other = target.with_suffix(".new")
    other.write_bytes(target.read_bytes())
    os.utime(other, ns=(row["preimage"]["mtime_ns"], row["preimage"]["mtime_ns"]))
    os.replace(other, target)
    assert invoke(fixture, monkeypatch) == 2
    assert target.exists()


def test_symlink_is_not_followed_or_removed(fixture, monkeypatch):
    _, target, _, _ = fixture
    other = target.with_suffix(".saved")
    target.rename(other)
    target.symlink_to(other)
    assert invoke(fixture, monkeypatch) == 2
    assert target.is_symlink() and other.read_bytes() == b"original"


def test_valid_identity_can_remove_only_temporary_fixture(fixture, monkeypatch):
    _, target, _, _ = fixture
    assert invoke(fixture, monkeypatch) == 0
    assert not target.exists()


def test_dry_run_keeps_exact_bytes(fixture, monkeypatch):
    _, target, row, _ = fixture
    assert invoke(fixture, monkeypatch, dry=True) == 0
    assert snapshot(target) == row["preimage"]


@pytest.mark.parametrize("change", ["hash", "bytes", "nlink", "schema", "mode", "extra", "bool"])
def test_invalid_preimage_is_rejected(fixture, monkeypatch, change):
    _, target, row, _ = fixture
    replacements = {
        "hash": ("sha256", "0" * 64),
        "bytes": ("bytes", 9),
        "nlink": ("nlink", 2),
        "schema": ("schema_version", "unknown"),
        "mode": ("mode", 0),
        "extra": ("unrecognized", 1),
        "bool": ("ino", True),
    }
    key, value = replacements[change]
    row["preimage"][key] = value
    assert invoke(fixture, monkeypatch) == 2
    assert target.exists()


def test_whole_batch_drift_is_detected_before_first_delete(fixture, monkeypatch):
    _, target, row, _ = fixture
    second = target.with_name("second.wav")
    second.write_bytes(b"original")
    second_row = {**row, "path": str(second), "preimage": snapshot(second)}
    second.write_bytes(b"modified")
    assert invoke(fixture, monkeypatch, rows=[row, second_row]) == 2
    assert target.exists() and second.exists()


def test_runtime_recheck_blocks_change_after_batch_check(fixture, monkeypatch):
    _, target, _, _ = fixture
    original = apply.remove_if_matches

    def race(*args, **kwargs):
        target.write_bytes(b"modified")
        return original(*args, **kwargs)

    monkeypatch.setattr(apply, "remove_if_matches", race)
    assert invoke(fixture, monkeypatch) == 2
    assert target.read_bytes() == b"modified"


def test_outside_file_is_not_deleted_even_with_correct_hash(fixture, monkeypatch):
    base, target, row, _ = fixture
    outside = base.parent / "working.wav"
    outside.write_bytes(b"original")
    row.update(path=str(outside), preimage=snapshot(outside))
    assert invoke(fixture, monkeypatch) == 2
    assert outside.exists() and target.exists()


def test_hardlinked_file_is_not_deleted(fixture, monkeypatch):
    _, target, row, _ = fixture
    alias = target.with_name("linked.wav")
    os.link(target, alias)
    row["preimage"] = snapshot(target)
    assert invoke(fixture, monkeypatch) == 2
    assert target.exists() and alias.exists()


def test_parent_symlink_is_not_traversed(fixture, monkeypatch):
    _, target, row, _ = fixture
    parent = target.parent
    real = parent.with_name(parent.name + "-real")
    parent.rename(real)
    parent.symlink_to(real, target_is_directory=True)
    row["preimage"] = snapshot(target)
    assert invoke(fixture, monkeypatch) == 2
    assert target.exists()


def test_duplicate_rows_do_not_partially_delete(fixture, monkeypatch):
    _, target, row, _ = fixture
    assert invoke(fixture, monkeypatch, rows=[row, row]) == 2
    assert target.exists()


@pytest.mark.parametrize(
    "name", ["clip.recut.mp4", "clip.burned-final-sapphire72.mp4", "unknown.bin"]
)
def test_protected_classes_cannot_be_smuggled_in_plan(fixture, monkeypatch, name):
    _, target, row, _ = fixture
    other = target.with_name(name)
    other.write_bytes(b"original")
    row.update(path=str(other), preimage=snapshot(other))
    assert invoke(fixture, monkeypatch) == 2
    assert other.exists()


def test_producer_emits_consumer_compatible_preimage(fixture, monkeypatch):
    planner = importlib.import_module("cleanup_preflight_scan")
    base, target, row, plan = fixture
    monkeypatch.setattr(planner, "quiet_window", lambda base: [])
    monkeypatch.setattr(planner, "authority_references", lambda base: (set(), set()))
    monkeypatch.setattr(
        planner, "candidate_states", lambda base: ({row["owner"]: "published"}, set())
    )
    monkeypatch.setattr(planner, "in_flight_candidates", lambda base: set())
    monkeypatch.setattr(
        sys, "argv", ["cleanup_preflight_scan.py", "--base", str(base), "--json", str(plan)]
    )
    assert planner.main() == 0
    generated = json.loads(plan.read_text())
    assert len(generated) == 1 and generated[0]["preimage"] == snapshot(target)
    monkeypatch.setattr(
        sys, "argv", ["cleanup_apply_plan.py", str(plan), "--base", str(base), "--dry-run"]
    )
    assert apply.main() == 0 and target.exists()


def test_preimage_reader_detects_mutation_during_hash(fixture, monkeypatch):
    helper = importlib.import_module("_cleanup_file_identity")
    base, target, _, _ = fixture
    original = helper.os.read
    changed = False

    def read(fd, size):
        nonlocal changed
        data = original(fd, size)
        if data and not changed:
            changed = True
            target.write_bytes(b"modified")
        return data

    monkeypatch.setattr(helper.os, "read", read)
    with pytest.raises(ValueError, match="changed"):
        helper.capture_preimage(str(base), str(target))


def test_quiet_window_remains_required(fixture, monkeypatch):
    _, target, _, _ = fixture
    monkeypatch.setattr(apply, "quiet_window", lambda base: ["runner active"])
    assert invoke(fixture, monkeypatch) == 2 and target.exists()


def test_planner_omits_shared_inodes(fixture, monkeypatch):
    planner = importlib.import_module("cleanup_preflight_scan")
    base, target, row, plan = fixture
    os.link(target, target.with_name("shared.wav"))
    monkeypatch.setattr(planner, "quiet_window", lambda base: [])
    monkeypatch.setattr(planner, "authority_references", lambda base: (set(), set()))
    monkeypatch.setattr(
        planner, "candidate_states", lambda base: ({row["owner"]: "published"}, set())
    )
    monkeypatch.setattr(planner, "in_flight_candidates", lambda base: set())
    monkeypatch.setattr(
        sys, "argv", ["cleanup_preflight_scan.py", "--base", str(base), "--json", str(plan)]
    )
    assert planner.main() == 0 and json.loads(plan.read_text()) == []
    assert target.exists()


def test_wrong_declared_size_is_not_coerced(fixture, monkeypatch):
    _, target, row, _ = fixture
    row["bytes"] = "8"
    assert invoke(fixture, monkeypatch) == 2 and target.exists()


def test_reporting_marks_logical_not_measured_bytes(fixture, monkeypatch):
    base, target, row, plan = fixture
    report = plan.with_name("report.json")
    plan.write_text(json.dumps([row]))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "cleanup_apply_plan.py",
            str(plan),
            "--base",
            str(base),
            "--dry-run",
            "--manifest",
            str(report),
        ],
    )
    assert apply.main() == 0 and target.exists()
    written = json.loads(report.read_text())
    assert written["freed_bytes_basis"] == "SUM_LOGICAL_BYTES_NOT_MEASURED"
    assert written["deleted_files"] == written["freed_bytes"] == 0
    assert written["would_delete_files"] == 1 and written["would_delete_logical_bytes"] == 8
