"""Disposable reference-scan controls, never live cleanup or provider access."""
from __future__ import annotations

import builtins
import importlib
import json
import os
import sys
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))
planner = importlib.import_module("cleanup_preflight_scan")


def document(tmp_path: Path, payload: bytes = b"{}") -> tuple[Path, Path]:
    base = tmp_path / "runtime"
    path = base / "reports" / "authority.json"
    path.parent.mkdir(parents=True)
    path.write_bytes(payload)
    return base, path


def test_unreadable_authority_is_not_treated_as_no_reference(tmp_path, monkeypatch):
    base, path = document(tmp_path)
    original_builtin, original_os = builtins.open, os.open

    def checked_builtin(name, *args, **kwargs):
        if os.fspath(name) == str(path):
            raise PermissionError("synthetic read failure")
        return original_builtin(name, *args, **kwargs)

    def checked_os(name, *args, **kwargs):
        if os.fspath(name) == str(path):
            raise PermissionError("synthetic read failure")
        return original_os(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", checked_builtin)
    monkeypatch.setattr(os, "open", checked_os)
    with pytest.raises((OSError, ValueError)):
        planner.authority_references(str(base))


def test_oversize_authority_cannot_silently_disappear(tmp_path):
    base, path = document(tmp_path)
    with path.open("r+b") as stream:
        stream.truncate(20 * 2**20 + 1)  # Sparse disposable file, not real evidence.
    with pytest.raises((OSError, ValueError)):
        planner.authority_references(str(base))


def test_invalid_utf8_cannot_silently_drop_bytes(tmp_path):
    base, _ = document(tmp_path, b'{"path": "\xff"}')
    with pytest.raises((OSError, ValueError)):
        planner.authority_references(str(base))


def test_custom_runtime_root_is_used_by_reference_matcher(tmp_path):
    base, path = document(tmp_path)
    target = base / "out" / "2026-08-22" / "auto_100000_1_2" / "working.wav"
    path.write_text(json.dumps({"input": str(target)}))
    refs, dirs = planner.authority_references(str(base))
    assert str(target) in refs and dirs == set()


@pytest.mark.parametrize("kind", ["root", "directory", "document"])
def test_symlink_in_authority_tree_is_not_followed(tmp_path, kind):
    base, path = document(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    target = outside / "linked.json"
    target.write_text("{}")
    if kind == "root":
        path.unlink()
        path.parent.rmdir()
        path.parent.symlink_to(outside, target_is_directory=True)
    elif kind == "directory":
        (path.parent / "linked").symlink_to(outside, target_is_directory=True)
    else:
        path.unlink()
        path.symlink_to(target)
    with pytest.raises((OSError, ValueError)):
        planner.authority_references(str(base))
    assert target.read_text() == "{}"


def test_unreadable_walk_is_not_a_complete_empty_scan(tmp_path, monkeypatch):
    base, path = document(tmp_path)
    real_scandir = os.scandir

    def fail_walk(name):
        if os.fspath(name) == str(path.parent):
            raise PermissionError(13, "synthetic denied directory", str(name))
        return real_scandir(name)

    monkeypatch.setattr(os, "scandir", fail_walk)
    with pytest.raises((OSError, ValueError)):
        planner.authority_references(str(base))


def test_nonregular_authority_cannot_block_reader(tmp_path):
    base, path = document(tmp_path)
    path.unlink()
    os.mkfifo(path)
    with pytest.raises((OSError, ValueError)):
        planner.authority_references(str(base))
    assert path.exists()


@pytest.mark.parametrize("mutation", ["same_inode", "replace_inode"])
def test_change_between_stat_and_open_or_during_read_is_rejected(tmp_path, monkeypatch, mutation):
    base, path = document(tmp_path, b"old-data")
    done = False
    if mutation == "same_inode":
        real_read = os.read

        def read(fd, size):
            nonlocal done
            raw = real_read(fd, size)
            if raw and not done:
                done = True
                path.write_bytes(b"new-data")
            return raw

        monkeypatch.setattr(os, "read", read)
    else:
        real_open = os.open

        def open_file(name, *args, **kwargs):
            nonlocal done
            if os.fspath(name) == str(path) and not done:
                done = True
                other = path.with_name("replacement")
                other.write_bytes(b"old-data")
                os.replace(other, path)
            return real_open(name, *args, **kwargs)

        monkeypatch.setattr(os, "open", open_file)
    with pytest.raises((OSError, ValueError)):
        planner.authority_references(str(base))
    assert done


def test_earlier_authority_drift_is_not_hidden_by_later_file(tmp_path, monkeypatch):
    base, first = document(tmp_path)
    second = first.with_name("other.json")
    second.write_text("{}")
    real_read = planner._authority_text
    seen = []

    def read(path, info):
        text = real_read(path, info)
        if seen:
            Path(seen[0]).write_text("changed after scan")
        seen.append(path)
        return text

    monkeypatch.setattr(planner, "_authority_text", read)
    with pytest.raises((OSError, ValueError)):
        planner.authority_references(str(base))
    assert len(seen) == 2


def test_new_namespace_entry_during_scan_is_detected(tmp_path, monkeypatch):
    base, path = document(tmp_path)
    real_read = planner._authority_text

    def read(name, info):
        text = real_read(name, info)
        path.with_name("new-authority.json").write_text("{}")
        return text

    monkeypatch.setattr(planner, "_authority_text", read)
    with pytest.raises((OSError, ValueError)):
        planner.authority_references(str(base))


def test_empty_optional_authority_roots_are_not_invented(tmp_path):
    base = tmp_path / "runtime"
    base.mkdir()
    assert planner.authority_references(str(base)) == (set(), set())
    assert list(base.iterdir()) == []


def test_cited_file_and_specific_directory_protection_are_preserved(tmp_path):
    base, path = document(tmp_path)
    day = base / "out" / "2026-08-22"
    specific = day / "auto_100000_1_2" / "source-context"
    target = specific / "working.wav"
    unrelated = str(base) + "-other/out/2026-08-22/auto_100000_1_2/working.wav"
    path.write_text(json.dumps([str(target), str(specific), str(day), unrelated]))
    before = path.read_bytes()
    refs, dirs = planner.authority_references(str(base) + "/")
    assert refs == {str(target), str(specific), str(day)}
    assert dirs == {str(specific)}
    assert path.read_bytes() == before


def test_valid_document_at_exact_limit_is_not_rejected(tmp_path, monkeypatch):
    base, _ = document(tmp_path, b"12345678")
    monkeypatch.setattr(planner, "MAX_AUTHORITY_BYTES", 8)
    assert planner.authority_references(str(base)) == (set(), set())


@pytest.mark.parametrize("existing_plan", [True, False])
def test_incomplete_scan_neither_emits_nor_overwrites_plan(tmp_path, monkeypatch, capsys, existing_plan):
    base, _ = document(tmp_path, b"\xff do-not-echo-private-content")
    plan = tmp_path / "cleanup-plan.json"
    if existing_plan:
        plan.write_bytes(b"old-plan-not-renewed")
    monkeypatch.setattr(planner, "quiet_window", lambda _: [])

    def forbidden(*_args, **_kwargs):
        raise AssertionError("Incomplete scan reached candidate/media planning")

    monkeypatch.setattr(planner, "candidate_states", forbidden)
    monkeypatch.setattr(sys, "argv", ["cleanup_preflight_scan.py", "--base", str(base), "--json", str(plan)])
    assert planner.main() == 2
    printed = capsys.readouterr().out
    assert "GATE 2 authority: BLOCKED" in printed
    assert "do-not-echo-private-content" not in printed
    if existing_plan:
        assert plan.read_bytes() == b"old-plan-not-renewed"
    else:
        assert not plan.exists()


def test_native_cli_failure_has_no_plan_and_no_deletion(tmp_path):
    base, path = document(tmp_path, b"\xff bad encoding")
    (base / "DISABLED").touch()
    plan = tmp_path / "plan.json"
    # Only the synthetic runtime skips host-wide quiet-window classification;
    # the real reference scanner and CLI failure path execute unchanged.
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts/cleanup_preflight_scan.py"),
         "--base", str(base), "--json", str(plan), "--ignore-quiet-window"],
        capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 2, result.stdout + result.stderr
    assert "GATE 2 authority: BLOCKED" in result.stdout
    assert not plan.exists() and path.read_bytes() == b"\xff bad encoding"


def test_real_reference_scanner_keeps_an_actually_cited_target(tmp_path, monkeypatch):
    base, path = document(tmp_path)
    cid = "auto_100000_1_2"
    target = base / "out" / "2026-08-22" / cid / "working.wav"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"retained fixture")
    path.write_text(json.dumps({"source": str(target)}))
    (base / "state").mkdir()
    (base / "state/2026-08-22.json").write_text(json.dumps({
        "picks": [{"candidate_id": cid, "status": "published"}],
    }))
    monkeypatch.setattr(planner, "quiet_window", lambda _: [])
    plan = tmp_path / "plan.json"
    monkeypatch.setattr(sys, "argv", ["cleanup_preflight_scan.py", "--base", str(base), "--json", str(plan)])
    assert planner.main() == 0
    assert json.loads(plan.read_text()) == []
    assert target.read_bytes() == b"retained fixture"
