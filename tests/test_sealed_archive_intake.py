from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
from xml.sax.saxutils import quoteattr

import pytest

from ops.recording import bililive_recorder_adapter as adapter
from ops.recording.sealed_archive_intake import (
    SealedArchiveIntakeError,
    intake_sealed_archive,
)
from src.autoslice.source_integrity import audit_finalized_recording_inventory


ROOM_ID = "22966160"
DATE = "2026-10-03"
FILENAME = "22966160_20261003-22-09-30.flv"
START_TIME = "2026-10-03T22:09:30.5741337+08:00"


def _xml() -> str:
    danmaku = [[0, 1, 25, 16777215, 1784786247], "普通弹幕", [123, "观众", 0, 0]]
    superchat = {"id": 9, "message": "醒目留言", "uid": 456, "price": 30}
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        "<i>"
        '<BililiveRecorder version="2.18.0"/>'
        f'<BililiveRecorderRecordInfo roomid="{ROOM_ID}" name="主播" title="测试" '
        f'start_time="{START_TIME}"/>'
        f'<d raw={quoteattr(json.dumps(danmaku, ensure_ascii=False))}>普通弹幕</d>'
        f'<sc raw={quoteattr(json.dumps(superchat, ensure_ascii=False))}>醒目留言</sc>'
        "</i>\n"
    )


def _state(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema_version": "bililive-recorder-adapter-state.v1",
                "managed_since_epoch": 0,
                "finalized": {},
                "source_dispositions": {},
                "source_disposition_identity_rebinds": {},
                "source_disposition_identity_rebind_tasks": {},
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )


@pytest.fixture
def fixture(tmp_path: Path) -> dict[str, Path | str]:
    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        pytest.skip("ffmpeg/ffprobe required for the synthetic AV fixture")
    source_dir = tmp_path / "115open" / "李豆沙录播" / ROOM_ID / DATE
    source_dir.mkdir(parents=True)
    source = source_dir / FILENAME
    xml = source.with_suffix(".xml")
    subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-y",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=320x180:r=25",
            "-f",
            "lavfi",
            "-i",
            "anullsrc=r=48000:cl=stereo",
            "-t",
            "1",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-f",
            "flv",
            str(source),
        ],
        check=True,
    )
    xml.write_text(_xml(), encoding="utf-8")
    source_sha = adapter.sha256_file(source)
    xml_sha = adapter.sha256_file(xml)
    source_size = source.stat().st_size
    xml_size = xml.stat().st_size
    relative = f"{DATE}/{FILENAME}"
    open_exclusions = {
        field: {"source_relative": relative, "open_relatives": []}
        for field in (
            "open_exclusion_before_copy",
            "open_exclusion_before_stability",
            "open_exclusion_after_copy",
            "open_exclusion_after_media",
            "open_exclusion_after_stability",
        )
    }
    row = {
        "status": "fuse_copy_complete",
        "attempts": 1,
        "source": str(source),
        "xml": str(xml),
        "target_flv": str(source),
        "target_xml": str(xml),
        "source_size_bytes": source_size,
        "source_sha256": source_sha,
        "xml_sha256": xml_sha,
        "flv_copy": {
            "status": "copied",
            "target_size_bytes": source_size,
            "target_sha256": source_sha,
        },
        "xml_copy": {
            "status": "copied",
            "target_size_bytes": xml_size,
            "target_sha256": xml_sha,
        },
        "media_verification": {
            "ffprobe_rc": 0,
            "full_av_decode_rc": 0,
            "valid_video_geometry": True,
            "video_stream_count": 1,
            "ffprobe_stderr_present": False,
            "decode_stderr_present": False,
        },
        "xml_parse": {"root_tag": "i", "event_counts": {"d": 1, "gift": 0, "guard": 0, "sc": 1}},
        "stable_pair": {
            phase: {
                "flv": {"size_bytes": source_size},
                "xml": {"size_bytes": xml_size},
            }
            for phase in ("before", "after")
        },
        **open_exclusions,
    }
    receipt = tmp_path / "PREVIOUS-22-09-CLOSURE.json"
    receipt.write_text(
        json.dumps(
            {
                "state_path": "/home/维护者/.local/share/vtuber-slice/archive-state.json",
                "state_source": "unique existing native archive worker",
                "rows": {relative: row},
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    state = tmp_path / "adapter-state.json"
    _state(state)
    return {
        "source": source,
        "xml": xml,
        "receipt": receipt,
        "state": state,
        "output": tmp_path / "oci3-local" / DATE,
        "relative": relative,
    }


def _call(fixture: dict[str, Path | str], **overrides):
    values = {
        "receipt_path": fixture["receipt"],
        "expected_receipt_sha256": adapter.sha256_file(fixture["receipt"]),
        "room_id": ROOM_ID,
        "date": DATE,
        "source_flv": fixture["source"],
        "source_xml": fixture["xml"],
        "output_dir": fixture["output"],
        "state_path": fixture["state"],
    }
    values.update(overrides)
    return intake_sealed_archive(**values)


def test_sealed_intake_finalizes_to_local_date_root_and_is_idempotent(fixture) -> None:
    source = fixture["source"]
    xml = fixture["xml"]
    source_before = source.read_bytes()
    xml_before = xml.read_bytes()
    first = _call(fixture)
    target = fixture["output"] / FILENAME.replace(".flv", ".mp4")
    local_xml = fixture["output"] / FILENAME.replace(".flv", ".xml")
    assert first["status"] == "finalized"
    assert first["state_updated"] is True
    assert target.is_file()
    assert local_xml.is_file()
    assert local_xml.read_bytes() == xml_before
    assert first["local_xml_path"] == str(local_xml)
    assert first["local_xml_sha256"] == adapter.sha256_file(xml)
    assert (fixture["output"] / FILENAME.replace(".flv", ".jsonl")).is_file()
    assert (fixture["output"] / FILENAME.replace(".flv", ".meta.json")).is_file()
    assert source.read_bytes() == source_before
    assert xml.read_bytes() == xml_before
    state_before = fixture["state"].read_bytes()
    target_before = target.read_bytes()
    state = json.loads(state_before)
    row = state["finalized"][fixture["relative"]]
    assert row["source_path"] == str(source.absolute())
    assert row["source_sha256"] == adapter.sha256_file(source)
    assert row["xml_sha256"] == adapter.sha256_file(xml)
    assert row["local_xml_path"] == str(local_xml)
    assert row["local_xml_sha256"] == adapter.sha256_file(xml)
    assert row["source_authority"] == "N100_115_ARCHIVE_STATE_NATURAL_SEAL"
    inventory = audit_finalized_recording_inventory(fixture["output"], room_id=ROOM_ID)
    assert inventory["status"] == "PASS"
    assert inventory["can_select"] is True

    second = _call(fixture)
    assert second["status"] == "already_finalized"
    assert second["state_updated"] is False
    assert fixture["state"].read_bytes() == state_before
    assert target.read_bytes() == target_before
    assert local_xml.read_bytes() == xml_before


def test_sealed_intake_rejects_unsealed_row(fixture) -> None:
    payload = json.loads(fixture["receipt"].read_text(encoding="utf-8"))
    payload["rows"][fixture["relative"]]["status"] = "copying"
    fixture["receipt"].write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    with pytest.raises(SealedArchiveIntakeError, match="fuse_copy_complete"):
        _call(fixture)
    assert json.loads(fixture["state"].read_text())["finalized"] == {}


def test_sealed_intake_rejects_hash_and_identity_drift(fixture) -> None:
    fixture["xml"].write_bytes(fixture["xml"].read_bytes() + b"drift")
    with pytest.raises(SealedArchiveIntakeError, match="XML size|XML SHA-256"):
        _call(fixture)
    fixture["xml"].write_text(_xml(), encoding="utf-8")
    fixture["source"].write_bytes(fixture["source"].read_bytes() + b"drift")
    with pytest.raises(SealedArchiveIntakeError, match="source size|SHA-256"):
        _call(fixture)
    with pytest.raises(SealedArchiveIntakeError, match="source filename"):
        _call(fixture, room_id="22966161")
    assert json.loads(fixture["state"].read_text())["finalized"] == {}


def test_sealed_intake_rejects_symlink_xml_sidecar(fixture) -> None:
    fixture["output"].mkdir(parents=True)
    local_xml = fixture["output"] / FILENAME.replace(".flv", ".xml")
    local_xml.symlink_to(fixture["xml"])
    with pytest.raises(SealedArchiveIntakeError, match="symlink"):
        _call(fixture)
    assert local_xml.is_symlink()
    assert json.loads(fixture["state"].read_text())["finalized"] == {}


def test_sealed_intake_rejects_different_xml_sidecar(fixture) -> None:
    fixture["output"].mkdir(parents=True)
    local_xml = fixture["output"] / FILENAME.replace(".flv", ".xml")
    local_xml.write_bytes(b"different XML")
    with pytest.raises(SealedArchiveIntakeError, match="XML sidecar hash or size"):
        _call(fixture)
    assert local_xml.read_bytes() == b"different XML"
    assert json.loads(fixture["state"].read_text())["finalized"] == {}


def test_sealed_intake_rejects_concurrent_adapter_owner(fixture) -> None:
    lock_fd = adapter._truncated_acquire_state_lock(fixture["state"])
    try:
        with pytest.raises(SealedArchiveIntakeError, match="lock"):
            _call(fixture)
    finally:
        adapter._truncated_release_state_lock(lock_fd)
    assert json.loads(fixture["state"].read_text())["finalized"] == {}


def test_sealed_intake_rejects_existing_target_without_ledger(fixture) -> None:
    fixture["output"].mkdir(parents=True)
    target = fixture["output"] / FILENAME.replace(".flv", ".mp4")
    target.write_bytes(b"occupied-target")
    with pytest.raises(SealedArchiveIntakeError, match="without matching finalized ledger"):
        _call(fixture)
    assert not (fixture["output"] / FILENAME.replace(".flv", ".xml")).exists()
    assert json.loads(fixture["state"].read_text())["finalized"] == {}


def _coverage_ledger(path: Path, *, webhook_files=None, finalized=None, dispositions=None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema_version": "bililive-recorder-adapter-state.v1",
                "managed_since_epoch": 0,
                "finalized": finalized or {},
                "webhook_files": webhook_files or {},
                "source_dispositions": dispositions or {},
                "source_disposition_identity_rebinds": {},
                "source_disposition_identity_rebind_tasks": {},
            }
        ),
        encoding="utf-8",
    )
    return path


def _native(open_time: str, close_time: str, status: str = "CLOSED") -> dict:
    return {
        "status": status,
        "session_id": "oci3-session",
        "opening_event_id": "open",
        "closing_event_id": "close" if status == "CLOSED" else None,
        "file_open_time": open_time,
        "file_close_time": close_time if status == "CLOSED" else None,
        "file_size": 1,
        "duration": 1.0,
    }


def test_sealed_intake_refuses_overlap_with_oci3_self_recording(fixture, tmp_path) -> None:
    # OCI3 recorded 22:09:29-22:39:31 itself; the N100 piece 22:09:30(+1 s) overlaps.
    coverage = _coverage_ledger(
        tmp_path / "oci3-115open/adapter-state.json",
        webhook_files={
            f"{DATE}/22966160_20261003-22-09-29.flv": _native(
                "2026-10-03T22:09:29.1+08:00", "2026-10-03T22:39:31.2+08:00"
            )
        },
    )
    with pytest.raises(SealedArchiveIntakeError, match="OCI3_SELF_RECORDING_PRIORITY"):
        _call(fixture, coverage_state_paths=[coverage])
    assert not fixture["output"].exists()
    assert json.loads(fixture["state"].read_text(encoding="utf-8"))["finalized"] == {}
    live = _coverage_ledger(
        tmp_path / "live/adapter-state.json",
        webhook_files={
            f"{DATE}/22966160_20261003-21-59-00.flv": _native(
                "2026-10-03T21:59:00+08:00", "", status="OPEN"
            )
        },
    )
    with pytest.raises(SealedArchiveIntakeError, match="OCI3_SELF_RECORDING_PRIORITY"):
        _call(fixture, coverage_state_paths=[live])


def test_sealed_intake_fills_a_gap_next_to_oci3_self_recording(fixture, tmp_path) -> None:
    coverage = _coverage_ledger(
        tmp_path / "oci3-115open/adapter-state.json",
        webhook_files={
            f"{DATE}/22966160_20261003-22-09-32.flv": _native(
                "2026-10-03T22:09:32+08:00", "2026-10-03T22:39:35+08:00"
            ),
            # A typed-ignored connection stub is not self-recording coverage.
            f"{DATE}/22966160_20261003-22-09-30.flv": _native(
                "2026-10-03T22:09:30.1+08:00", "2026-10-03T22:09:30.4+08:00"
            ),
        },
        dispositions={
            f"{DATE}/22966160_20261003-22-09-30.flv": {"status": "IGNORED_CONNECTION_STUB"}
        },
    )
    result = _call(fixture, coverage_state_paths=[coverage])
    assert result["status"] == "finalized"


def test_sealed_intake_refuses_a_second_import_of_the_same_piece(fixture, tmp_path) -> None:
    source_sha = adapter.sha256_file(fixture["source"])
    elsewhere = _coverage_ledger(
        tmp_path / "legacy/adapter-state.json",
        finalized={
            fixture["relative"]: {
                "source_sha256": source_sha,
                "source_authority": "N100_115_ARCHIVE_STATE_NATURAL_SEAL",
            }
        },
    )
    with pytest.raises(SealedArchiveIntakeError, match="DUPLICATE_IMPORT"):
        _call(fixture, coverage_state_paths=[elsewhere])
    assert not fixture["output"].exists()

    # Same bytes already finalized under another relative key in the target ledger.
    state = json.loads(fixture["state"].read_text(encoding="utf-8"))
    state["finalized"][f"{DATE}/22966160_20261003-22-09-31.flv"] = {"source_sha256": source_sha}
    fixture["state"].write_text(json.dumps(state), encoding="utf-8")
    with pytest.raises(SealedArchiveIntakeError, match="DUPLICATE_IMPORT"):
        _call(fixture)


def test_sealed_intake_cli_defaults_to_existing_oci3_ledgers() -> None:
    from ops.recording import sealed_archive_intake as intake

    assert [str(path) for path in intake.DEFAULT_COVERAGE_STATE_PATHS] == [
        "/opt/bilive/recording/adapter-state.json",
        "/opt/bilive/recording/oci3-115open/adapter-state.json",
        "/opt/bilive/recording/oci3-local-first/adapter-state.json",
    ]
    args = intake._build_parser().parse_args(
        [
            "--receipt", "r", "--expected-receipt-sha256", "0" * 64, "--room-id", ROOM_ID,
            "--date", DATE, "--source-flv", "a.flv", "--source-xml", "a.xml",
            "--output-dir", "o", "--state-path", "s",
            "--coverage-state", "x.json", "--coverage-state", "y.json",
        ]
    )
    assert args.coverage_state == [Path("x.json"), Path("y.json")]
