"""Synthetic format/reference controls; no original media or cleanup operation."""

from __future__ import annotations

import importlib
import json
from pathlib import Path
import struct
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))
planner = importlib.import_module("cleanup_preflight_scan")


def metadata(payload: bytes = b"\xff\x00synthetic") -> bytes:
    finder = bytes(32) + payload
    end = 50 + len(finder)
    return (
        struct.pack(">II16sH", 0x51607, 0x20000, b"Mac OS X        ", 2)
        + struct.pack(">III", 9, 50, len(finder))
        + struct.pack(">III", 2, end, 0)
        + finder
    )


def pair(tmp_path, payload=None):
    base = tmp_path / "runtime"
    directory = base / "reports"
    directory.mkdir(parents=True)
    data, side = directory / "authority.json", directory / "._authority.json"
    target = base / "out/2026-01-01/auto_100000_1_2/working.wav"
    data.write_text(json.dumps({"source": str(target)}, ensure_ascii=False))
    side.write_bytes(metadata() if payload is None else payload)
    return base, data, side, target


def test_validated_metadata_keeps_actual_companion_reference(tmp_path):
    base, data, side, target = pair(tmp_path)
    originals = data.read_bytes(), side.read_bytes()
    refs, _ = planner.authority_references(str(base))
    assert str(target) in refs
    assert originals == (data.read_bytes(), side.read_bytes())


def test_binary_metadata_does_not_hide_embedded_utf8_path_references(tmp_path):
    base, data, side, target = pair(tmp_path)
    other = base / "out/2026-01-01/auto_100000_1_2/中文.wav"
    side.write_bytes(metadata(b"\xff\x00" + str(other).encode() + b"\x00\xff"))
    refs, _ = planner.authority_references(str(base))
    assert {str(target), str(other)} <= refs


def test_plain_dot_underscore_document_is_still_text_not_skipped(tmp_path):
    base, data, side, target = pair(tmp_path)
    data.unlink()
    side.write_text(str(target))
    assert str(target) in planner.authority_references(str(base))[0]


def test_bad_utf8_with_metadata_filename_is_not_enough(tmp_path):
    base, _, _, _ = pair(tmp_path, b"\xffpretend-sidecar")
    with pytest.raises((OSError, ValueError)):
        planner.authority_references(str(base))


@pytest.mark.parametrize(
    "fault",
    [
        "version",
        "short_header",
        "short_table",
        "count",
        "duplicate",
        "data_fork",
        "unknown_entry",
        "in_header",
        "beyond_end",
        "resource_payload",
        "short_finder",
        "trailing",
        "orphan",
        "bad_name",
    ],
)
def test_unsupported_or_malformed_appledouble_still_blocks(tmp_path, fault):
    raw = bytearray(metadata())
    if fault == "version":
        struct.pack_into(">I", raw, 4, 0x10000)
    elif fault == "short_header":
        raw = raw[:20]
    elif fault == "short_table":
        raw = raw[:45]
    elif fault == "count":
        struct.pack_into(">H", raw, 24, 65535)
    elif fault == "duplicate":
        struct.pack_into(">I", raw, 38, 9)
    elif fault == "data_fork":
        struct.pack_into(">I", raw, 38, 1)
    elif fault == "unknown_entry":
        struct.pack_into(">I", raw, 38, 42)
    elif fault == "in_header":
        struct.pack_into(">I", raw, 30, 0)
    elif fault == "beyond_end":
        struct.pack_into(">I", raw, 34, 99999)
    elif fault == "resource_payload":
        struct.pack_into(">III", raw, 38, 2, len(raw) - 1, 1)
    elif fault == "short_finder":
        struct.pack_into(">I", raw, 34, 31)
    elif fault == "trailing":
        raw += b"\xfftrailing"
    base, data, side, _ = pair(tmp_path, raw)
    if fault == "orphan":
        data.unlink()
    if fault == "bad_name":
        side.rename(side.with_name("binary-as-authority.json"))
    with pytest.raises((OSError, ValueError)):
        planner.authority_references(str(base))


@pytest.mark.parametrize("fault", ["symlink", "directory", "invalid_utf8", "oversize"])
def test_companion_is_in_the_same_complete_regular_text_closure(tmp_path, fault):
    base, data, _, _ = pair(tmp_path)
    if fault == "symlink":
        data.unlink()
        outside = tmp_path / "outside.json"
        outside.write_text("{}")
        data.symlink_to(outside)
    elif fault == "directory":
        data.unlink()
        data.mkdir()
    elif fault == "invalid_utf8":
        data.write_bytes(b"\xff")
    else:
        with data.open("r+b") as f:
            f.truncate(planner.MAX_AUTHORITY_BYTES + 1)
    with pytest.raises((OSError, ValueError)):
        planner.authority_references(str(base))


@pytest.mark.parametrize("which", ["sidecar", "companion"])
def test_metadata_pair_identity_drift_is_not_hidden(tmp_path, monkeypatch, which):
    base, data, side, _ = pair(tmp_path)
    original = planner._authority_text
    victim = side if which == "sidecar" else data
    mutated = []

    def changing(path, info):
        value = original(path, info)
        if Path(path) == victim:
            victim.write_bytes(victim.read_bytes())
            mutated.append(True)
        return value

    monkeypatch.setattr(planner, "_authority_text", changing)
    with pytest.raises((OSError, ValueError)):
        planner.authority_references(str(base))
    assert mutated


def test_invalid_metadata_preserves_previous_plan_and_stops_before_media(tmp_path, monkeypatch):
    base, data, side, _ = pair(tmp_path)
    side.write_bytes(metadata()[:-1])
    plan = tmp_path / "plan.json"
    plan.write_bytes(b"old-plan")
    monkeypatch.setattr(planner, "quiet_window", lambda _: [])

    def forbidden(*args):
        raise AssertionError("Invalid metadata reached media scan")

    monkeypatch.setattr(planner, "candidate_states", forbidden)
    monkeypatch.setattr(
        sys, "argv", ["cleanup_preflight_scan.py", "--base", str(base), "--json", str(plan)]
    )
    assert planner.main() == 2
    assert plan.read_bytes() == b"old-plan"


def test_valid_pair_does_not_make_referenced_media_deletable(tmp_path, monkeypatch):
    base, _, _, target = pair(tmp_path)
    target.parent.mkdir(parents=True)
    target.write_bytes(b"synthetic-media-retained")
    (base / "state").mkdir()
    (base / "state/2026-01-01.json").write_text(
        json.dumps({"picks": [{"candidate_id": "auto_100000_1_2", "status": "published"}]})
    )
    monkeypatch.setattr(planner, "quiet_window", lambda _: [])
    plan = tmp_path / "plan.json"
    monkeypatch.setattr(
        sys, "argv", ["cleanup_preflight_scan.py", "--base", str(base), "--json", str(plan)]
    )
    assert planner.main() == 0
    assert json.loads(plan.read_text()) == []
    assert target.read_bytes() == b"synthetic-media-retained"
