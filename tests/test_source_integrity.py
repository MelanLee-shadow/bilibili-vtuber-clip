import json
import os

import pytest

from ops.recording import bililive_recorder_adapter as adapter
from src.autoslice import source_integrity
from src.autoslice.source_integrity import (
    MediaSegmentObservation,
    audit_finalized_recording_inventory,
    build_source_range_ledger,
    plan_bilibili_replay_compensation,
    plan_bilibili_replay_download_commands,
)


def _typed_connection_stub(tmp_path, monkeypatch, *, successor_open_time=None):
    date_dir = tmp_path / "2026-08-12"
    date_dir.mkdir()
    stub = date_dir / "123456_20260812-20-29-51.flv"
    successor = date_dir / "123456_20260812-20-29-54.flv"
    successor_mp4 = successor.with_suffix(".mp4")
    stub.write_bytes(b"stub")
    stub.with_suffix(".xml").write_text(
        '<?xml version="1.0"?><i><BililiveRecorder version="2.18.0"/>'
        '<BililiveRecorderRecordInfo roomid="123456" name="主播" title="测试" '
        'start_time="2026-08-12T20:29:51+08:00"/>'
        '<d p="0.009,1,25,16777215,0,0,123,0" user="观众" '
        'raw=\'[[0,1,25,16777215,1786537791],"你好",[123,"观众",0,0]]\'>'
        "你好</d></i>",
        encoding="utf-8",
    )
    successor.write_bytes(b"successor-source")
    successor_mp4.write_bytes(b"successor-mp4")
    stub_relative = f"{date_dir.name}/{stub.name}"
    successor_relative = f"{date_dir.name}/{successor.name}"
    session_id = "session-a"
    webhook_files = {
        stub_relative: {
            "status": "CLOSED",
            "session_id": session_id,
            "opening_event_id": "open-a",
            "closing_event_id": "close-a",
            "file_open_time": "2026-08-12T20:29:52.70775+08:00",
            "file_close_time": "2026-08-12T20:29:53.1716935+08:00",
            "file_size": stub.stat().st_size,
            "duration": 3.03,
        },
        successor_relative: {
            "status": "CLOSED",
            "session_id": session_id,
            "opening_event_id": "open-b",
            "closing_event_id": "close-b",
            "file_open_time": successor_open_time or "2026-08-12T20:29:54.3497935+08:00",
            "file_close_time": "2026-08-12T20:59:58.5961912+08:00",
            "file_size": successor.stat().st_size,
            "duration": 1804.164,
        },
    }
    finalized = {
        successor_relative: {
            "source_size": successor.stat().st_size,
            "source_mtime_ns": successor.stat().st_mtime_ns,
            "target": str(successor_mp4),
            "target_sha256": adapter.sha256_file(successor_mp4),
            "finalized_at": "2026-08-12T15:45:21+00:00",
        }
    }
    monkeypatch.setattr(
        adapter,
        "probe_connection_stub_video",
        lambda *_args, **_kwargs: {
            "duration_seconds": 3.03,
            "size_bytes": stub.stat().st_size,
            "video_codec": "h264",
            "width": 0,
            "height": 0,
            "decoded_video_frames": 0,
            "video_packets": 0,
        },
    )
    monkeypatch.setattr(
        adapter,
        "probe_media",
        lambda *_args, **_kwargs: {
            "duration_seconds": 1804.164,
            "size_bytes": successor_mp4.stat().st_size,
            "video_codec": "h264",
            "audio_codec": "aac",
            "width": 1920,
            "height": 1080,
        },
    )
    row = adapter.build_connection_stub_disposition(
        stub,
        record_root=tmp_path,
        webhook_files=webhook_files,
        finalized=finalized,
    )
    assert row is not None
    state = {
        "schema_version": "bililive-recorder-adapter-state.v1",
        "webhook_files": webhook_files,
        "finalized": finalized,
        "source_dispositions": {stub_relative: row},
    }
    state_path = tmp_path / "adapter-state.json"
    state_path.write_text(json.dumps(state), encoding="utf-8")
    return date_dir, stub, successor_mp4, state_path


def test_inventory_revalidates_three_second_connection_stub_handoff(tmp_path, monkeypatch):
    date_dir, stub, _successor_mp4, state_path = _typed_connection_stub(
        tmp_path,
        monkeypatch,
        successor_open_time="2026-08-12T20:29:55.5000000+08:00",
    )

    result = audit_finalized_recording_inventory(
        date_dir,
        room_id="123456",
        adapter_state_path=state_path,
    )

    assert result["status"] == "PASS"
    assert result["issues"] == [
        {
            "severity": "WARN",
            "code": "RECORDER_CONNECTION_STUB_NO_DECODABLE_VIDEO",
            "message": "typed connection-stub disposition revalidated",
            "segment_stem": stub.stem,
            "path": str(stub),
            "source_disposition_schema": "recording-connection-stub.v1",
            "source_disposition_status": "IGNORED_CONNECTION_STUB",
        }
    ]


def _typed_timestamp_rebind(tmp_path, monkeypatch):
    date_dir, stub, successor_mp4, state_path = _typed_connection_stub(tmp_path, monkeypatch)
    state = json.loads(state_path.read_text(encoding="utf-8"))
    relative = f"{date_dir.name}/{stub.name}"
    row = state["source_dispositions"][relative]
    for binding in (row["session"]["successor_source"], row["session"]["successor_mp4"]):
        binding["mtime_ns"] -= 1
        binding["ctime_ns"] -= 1
    unsigned = {key: value for key, value in row.items() if key != "canonical_integrity"}
    row["canonical_integrity"]["canonical_json_sha256"] = adapter._canonical_json_sha256(unsigned)
    receipt_mount = {
        "mount_id": 77,
        "major_minor": "0:67",
        "root": "/",
        "mount_point": "/adapter/Videos",
        "filesystem_type": "fuse.cloudfs",
        "mount_source": "CloudFS",
    }
    monkeypatch.setattr(adapter, "_mount_identity_for_path", lambda _path: receipt_mount)
    receipts: list[dict] = []
    adapter.validate_connection_stub_disposition(
        stub,
        row,
        record_root=tmp_path,
        webhook_files=state["webhook_files"],
        finalized=state["finalized"],
        identity_rebinds=receipts,
        identity_rebind_attestations={
            "source": adapter._attest_regular_file(stub),
            "xml": adapter._attest_regular_file(stub.with_suffix(".xml")),
            "successor_mp4": adapter._attest_regular_file(successor_mp4),
        },
    )
    state["source_disposition_identity_rebinds"] = {relative: receipts}
    state_path.write_text(json.dumps(state), encoding="utf-8")
    return date_dir, stub, successor_mp4, state_path, receipt_mount


def _typed_reused_portable_rebind(tmp_path, monkeypatch):
    date_dir, stub, successor_mp4, state_path = _typed_connection_stub(tmp_path, monkeypatch)
    state = json.loads(state_path.read_text(encoding="utf-8"))
    relative = f"{date_dir.name}/{stub.name}"
    row = state["source_dispositions"][relative]
    base_mount = {
        "mount_id": 77,
        "major_minor": "0:67",
        "root": "/",
        "mount_point": "/adapter/Videos",
        "filesystem_type": "fuse.cloudfs",
        "mount_source": "CloudFS",
    }
    current_mount = {**base_mount, "mount_id": 88, "mount_point": "/new-adapter/Videos"}
    real_fingerprint = adapter._regular_file_fingerprint
    real_attestation = adapter._attest_regular_file

    def offset_fingerprint(path, offset):
        fingerprint = real_fingerprint(path)
        fingerprint["inode"] += offset
        return fingerprint

    def offset_attestation(path, offset):
        attestation = real_attestation(path)
        attestation["inode"] += offset
        return attestation

    monkeypatch.setattr(
        adapter,
        "_regular_file_fingerprint",
        lambda path: offset_fingerprint(path, 20),
    )
    monkeypatch.setattr(
        adapter,
        "_attest_regular_file",
        lambda path: offset_attestation(path, 20),
    )
    monkeypatch.setattr(adapter, "_mount_identity_for_path", lambda _path: base_mount)
    receipts = []
    adapter.validate_connection_stub_disposition(
        stub,
        row,
        record_root=tmp_path,
        webhook_files=state["webhook_files"],
        finalized=state["finalized"],
        identity_rebinds=receipts,
        identity_rebind_attestations={
            "source": adapter._attest_regular_file(stub),
            "xml": adapter._attest_regular_file(stub.with_suffix(".xml")),
            "successor_mp4": adapter._attest_regular_file(successor_mp4),
        },
    )
    monkeypatch.setattr(adapter, "_regular_file_fingerprint", real_fingerprint)
    monkeypatch.setattr(adapter, "_attest_regular_file", real_attestation)
    monkeypatch.setattr(adapter, "_mount_identity_for_path", lambda _path: current_mount)
    adapter.validate_connection_stub_disposition(
        stub,
        row,
        record_root=tmp_path,
        webhook_files=state["webhook_files"],
        finalized=state["finalized"],
        identity_rebinds=receipts,
        identity_rebind_attestations={
            "source": adapter._attest_regular_file(stub),
            "xml": adapter._attest_regular_file(stub.with_suffix(".xml")),
            "successor_mp4": adapter._attest_regular_file(successor_mp4),
        },
    )
    assert receipts[-1]["policy"] == adapter.SOURCE_DISPOSITION_REUSED_PORTABLE_REBIND_POLICY
    state["source_disposition_identity_rebinds"] = {relative: receipts}
    state_path.write_text(json.dumps(state), encoding="utf-8")
    monkeypatch.setattr(source_integrity, "_mount_identity_for_path", lambda _path: current_mount)
    return date_dir, stub, state_path, receipts


def _typed_reused_portable_timestamp_rebind(tmp_path, monkeypatch):
    date_dir, stub, successor_mp4, state_path = _typed_connection_stub(tmp_path, monkeypatch)
    state = json.loads(state_path.read_text(encoding="utf-8"))
    relative = f"{date_dir.name}/{stub.name}"
    row = state["source_dispositions"][relative]
    base_mount = {
        "mount_id": 77,
        "major_minor": "0:67",
        "root": "/",
        "mount_point": "/adapter/Videos",
        "filesystem_type": "fuse.cloudfs",
        "mount_source": "CloudFS",
    }
    current_mount = {**base_mount, "mount_id": 88, "mount_point": "/new-adapter/Videos"}
    real_fingerprint = adapter._regular_file_fingerprint
    real_attestation = adapter._attest_regular_file

    def offset_fingerprint(path, offset):
        fingerprint = real_fingerprint(path)
        fingerprint["inode"] += offset
        return fingerprint

    def offset_attestation(path, offset):
        attestation = real_attestation(path)
        attestation["inode"] += offset
        return attestation

    monkeypatch.setattr(
        adapter,
        "_regular_file_fingerprint",
        lambda path: offset_fingerprint(path, 20),
    )
    monkeypatch.setattr(
        adapter,
        "_attest_regular_file",
        lambda path: offset_attestation(path, 20),
    )
    monkeypatch.setattr(adapter, "_mount_identity_for_path", lambda _path: base_mount)
    receipts = []
    adapter.validate_connection_stub_disposition(
        stub,
        row,
        record_root=tmp_path,
        webhook_files=state["webhook_files"],
        finalized=state["finalized"],
        identity_rebinds=receipts,
        identity_rebind_attestations={
            "source": adapter._attest_regular_file(stub),
            "xml": adapter._attest_regular_file(stub.with_suffix(".xml")),
            "successor_mp4": adapter._attest_regular_file(successor_mp4),
        },
    )
    monkeypatch.setattr(adapter, "_regular_file_fingerprint", real_fingerprint)
    monkeypatch.setattr(adapter, "_attest_regular_file", real_attestation)
    successor = stub.with_name(row["session"]["successor_relative_path"].rsplit("/", 1)[1])
    for path in (successor, successor_mp4):
        before = path.stat()
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns + 1_000_000_000))
    monkeypatch.setattr(adapter, "_mount_identity_for_path", lambda _path: current_mount)
    adapter.validate_connection_stub_disposition(
        stub,
        row,
        record_root=tmp_path,
        webhook_files=state["webhook_files"],
        finalized=state["finalized"],
        identity_rebinds=receipts,
        identity_rebind_attestations={
            "source": adapter._attest_regular_file(stub),
            "xml": adapter._attest_regular_file(stub.with_suffix(".xml")),
            "successor_mp4": adapter._attest_regular_file(successor_mp4),
        },
    )
    assert (
        receipts[-1]["policy"]
        == adapter.SOURCE_DISPOSITION_REUSED_PORTABLE_TIMESTAMP_REBIND_POLICY
    )
    state["source_disposition_identity_rebinds"] = {relative: receipts}
    state_path.write_text(json.dumps(state), encoding="utf-8")
    monkeypatch.setattr(source_integrity, "_mount_identity_for_path", lambda _path: current_mount)
    return date_dir, stub, state_path, receipts


def test_recording_inventory_accepts_reused_portable_fuse_rebind(tmp_path, monkeypatch):
    date_dir, stub, state_path, receipts = _typed_reused_portable_rebind(tmp_path, monkeypatch)

    audit = audit_finalized_recording_inventory(
        date_dir,
        room_id="123456",
        adapter_state_path=state_path,
    )

    assert len(receipts) == 2
    assert audit["status"] == "PASS"
    assert audit["issues"] == [
        {
            "severity": "WARN",
            "code": "RECORDER_CONNECTION_STUB_NO_DECODABLE_VIDEO",
            "message": "typed connection-stub disposition revalidated",
            "segment_stem": stub.stem,
            "path": str(stub),
            "source_disposition_schema": "recording-connection-stub.v1",
            "source_disposition_status": "IGNORED_CONNECTION_STUB",
        }
    ]


def test_recording_inventory_accepts_reused_portable_timestamp_rebind(tmp_path, monkeypatch):
    date_dir, stub, state_path, receipts = _typed_reused_portable_timestamp_rebind(
        tmp_path, monkeypatch
    )

    audit = audit_finalized_recording_inventory(
        date_dir,
        room_id="123456",
        adapter_state_path=state_path,
    )

    assert len(receipts) == 2
    assert audit["status"] == "PASS"


def test_recording_inventory_rejects_reused_portable_timestamp_rebind_tamper(
    tmp_path, monkeypatch
):
    date_dir, stub, state_path, receipts = _typed_reused_portable_timestamp_rebind(
        tmp_path, monkeypatch
    )
    receipts[-1]["changed_fields"]["successor_source"] = ["inode", "mtime_ns"]
    receipts[-1]["canonical_integrity"] = {
        "algorithm": "sha256",
        "canonical_json_sha256": adapter._canonical_json_sha256(
            {key: value for key, value in receipts[-1].items() if key != "canonical_integrity"}
        ),
    }
    state = json.loads(state_path.read_text(encoding="utf-8"))
    relative = next(iter(state["source_disposition_identity_rebinds"]))
    state["source_disposition_identity_rebinds"][relative] = receipts
    state_path.write_text(json.dumps(state), encoding="utf-8")
    row = state["source_dispositions"][relative]
    successor = date_dir / row["session"]["successor_relative_path"].rsplit("/", 1)[1]
    valid, detail = source_integrity._connection_stub_identity_matches(
        row=row,
        bindings={
            "source": row["source"],
            "xml": row["xml"],
            "successor_source": row["session"]["successor_source"],
            "successor_mp4": row["session"]["successor_mp4"],
        },
        paths={
            "source": stub,
            "xml": stub.with_suffix(".xml"),
            "successor_source": successor,
            "successor_mp4": successor.with_suffix(".mp4"),
        },
        relative_paths={
            "source": relative,
            "xml": f"{date_dir.name}/{stub.with_suffix('.xml').name}",
            "successor_source": row["session"]["successor_relative_path"],
            "successor_mp4": row["session"]["successor_relative_path"].replace(".flv", ".mp4"),
        },
        identity_rebinds=receipts,
    )
    assert not valid
    assert detail == "source disposition reused-portable timestamp rebind binding drifted"

    audit = audit_finalized_recording_inventory(
        date_dir,
        room_id="123456",
        adapter_state_path=state_path,
    )
    assert audit["status"] == "BLOCKED"


def test_recording_inventory_rejects_reused_portable_rebind_chain_drift(tmp_path, monkeypatch):
    date_dir, _stub, state_path, receipts = _typed_reused_portable_rebind(tmp_path, monkeypatch)
    receipts[-1]["current_mount"]["mount_id"] = receipts[-2]["current_mount"]["mount_id"]
    receipts[-1]["canonical_integrity"] = {
        "algorithm": "sha256",
        "canonical_json_sha256": adapter._canonical_json_sha256(
            {key: value for key, value in receipts[-1].items() if key != "canonical_integrity"}
        ),
    }
    state = json.loads(state_path.read_text(encoding="utf-8"))
    relative = next(iter(state["source_disposition_identity_rebinds"]))
    state["source_disposition_identity_rebinds"][relative] = receipts
    state_path.write_text(json.dumps(state), encoding="utf-8")
    row = state["source_dispositions"][relative]
    source = date_dir / relative.rsplit("/", 1)[1]
    successor = date_dir / row["session"]["successor_relative_path"].rsplit("/", 1)[1]
    valid, detail = source_integrity._connection_stub_identity_matches(
        row=row,
        bindings={
            "source": row["source"],
            "xml": row["xml"],
            "successor_source": row["session"]["successor_source"],
            "successor_mp4": row["session"]["successor_mp4"],
        },
        paths={
            "source": source,
            "xml": source.with_suffix(".xml"),
            "successor_source": successor,
            "successor_mp4": successor.with_suffix(".mp4"),
        },
        relative_paths={
            "source": relative,
            "xml": f"{date_dir.name}/{source.with_suffix('.xml').name}",
            "successor_source": row["session"]["successor_relative_path"],
            "successor_mp4": row["session"]["successor_relative_path"].replace(".flv", ".mp4"),
        },
        identity_rebinds=receipts,
    )
    assert not valid
    assert detail == "source disposition reused-portable rebind binding drifted"

    audit = audit_finalized_recording_inventory(
        date_dir,
        room_id="123456",
        adapter_state_path=state_path,
    )

    assert audit["status"] == "BLOCKED"


def test_recording_inventory_blocks_finalized_playlist_without_mp4(tmp_path):
    date_dir = tmp_path / "2026-07-22"
    date_dir.mkdir()
    stem = "123456_20260722-20-05-11"
    (date_dir / f"{stem}.m4s").write_bytes(b"raw-media")
    (date_dir / f"{stem}.m3u8").write_text(
        "#EXTM3U\n#EXT-X-VERSION:7\n#EXT-X-ENDLIST\n",
        encoding="utf-8",
    )

    audit = audit_finalized_recording_inventory(date_dir, room_id="123456")

    assert audit["status"] == "BLOCKED"
    assert audit["can_select"] is False
    assert audit["consumer_segments"] == []
    assert [issue["code"] for issue in audit["issues"]] == ["FINALIZED_PLAYLIST_WITHOUT_MP4"]


def test_recording_inventory_accepts_raw_sidecars_with_consumable_mp4(tmp_path):
    date_dir = tmp_path / "2026-07-22"
    date_dir.mkdir()
    stem = "123456_20260722-19-35-15"
    for suffix in (".m4s", ".mp4"):
        (date_dir / f"{stem}{suffix}").write_bytes(b"media")
    (date_dir / f"{stem}.m3u8").write_text(
        "#EXTM3U\n#EXT-X-ENDLIST\n",
        encoding="utf-8",
    )

    audit = audit_finalized_recording_inventory(date_dir, room_id="123456")

    assert audit["status"] == "PASS"
    assert audit["can_select"] is True
    assert audit["issues"] == []
    assert audit["consumer_segments"] == [str(date_dir / f"{stem}.mp4")]


def test_recording_inventory_blocks_closed_bililive_recorder_flv_without_mp4(
    tmp_path,
):
    date_dir = tmp_path / "2026-07-23"
    date_dir.mkdir()
    stem = "123456_20260723-19-35-15"
    (date_dir / f"{stem}.flv").write_bytes(b"closed-official-recorder-source")
    (date_dir / f"{stem}.xml").write_text(
        '<i><BililiveRecorder version="2.18.0"/></i>',
        encoding="utf-8",
    )

    audit = audit_finalized_recording_inventory(date_dir, room_id="123456")

    assert audit["status"] == "BLOCKED"
    assert audit["can_select"] is False
    assert [issue["code"] for issue in audit["issues"]] == ["CLOSED_FLV_WITHOUT_MP4"]


def test_recording_inventory_accepts_flv_with_atomic_adapter_mp4(tmp_path):
    date_dir = tmp_path / "2026-07-23"
    date_dir.mkdir()
    stem = "123456_20260723-19-35-15"
    (date_dir / f"{stem}.flv").write_bytes(b"source")
    (date_dir / f"{stem}.mp4").write_bytes(b"consumer")

    audit = audit_finalized_recording_inventory(date_dir, room_id="123456")

    assert audit["status"] == "PASS"
    assert audit["consumer_segments"] == [str(date_dir / f"{stem}.mp4")]


def test_recording_inventory_accepts_typed_connection_stub_as_warn(
    tmp_path,
    monkeypatch,
):
    date_dir, stub, successor_mp4, state_path = _typed_connection_stub(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "src.autoslice.source_integrity._sha256_file",
        lambda _path: (_ for _ in ()).throw(
            AssertionError("inventory must not reread the historical MP4")
        ),
    )

    audit = audit_finalized_recording_inventory(
        date_dir,
        room_id="123456",
        adapter_state_path=state_path,
    )

    assert audit["status"] == "PASS"
    assert audit["can_select"] is True
    assert audit["consumer_segments"] == [str(successor_mp4)]
    assert audit["issues"] == [
        {
            "code": "RECORDER_CONNECTION_STUB_NO_DECODABLE_VIDEO",
            "severity": "WARN",
            "message": "typed connection-stub disposition revalidated",
            "segment_stem": stub.stem,
            "path": str(stub),
            "source_disposition_schema": "recording-connection-stub.v1",
            "source_disposition_status": "IGNORED_CONNECTION_STUB",
        }
    ]


def test_recording_inventory_accepts_persisted_fuse_identity_rebind(
    tmp_path,
    monkeypatch,
):
    date_dir, stub, successor_mp4, state_path = _typed_connection_stub(tmp_path, monkeypatch)
    state = json.loads(state_path.read_text(encoding="utf-8"))
    relative = f"{date_dir.name}/{stub.name}"
    row = state["source_dispositions"][relative]
    bindings = (
        row["source"],
        row["xml"],
        row["session"]["successor_source"],
        row["session"]["successor_mp4"],
    )
    for binding in bindings:
        binding["device"] += 1
        binding["inode"] += 1
    unsigned = {key: value for key, value in row.items() if key != "canonical_integrity"}
    row["canonical_integrity"]["canonical_json_sha256"] = adapter._canonical_json_sha256(unsigned)
    fuse_mount = {
        "mount_id": 77,
        "major_minor": "0:67",
        "root": "/",
        "mount_point": str(tmp_path),
        "filesystem_type": "fuse.cloudfs",
        "mount_source": "CloudFS",
    }
    monkeypatch.setattr(adapter, "_mount_identity_for_path", lambda _path: fuse_mount)
    receipts: list[dict] = []
    successor = date_dir / row["session"]["successor_relative_path"].split("/", 1)[1]
    adapter.validate_connection_stub_disposition(
        stub,
        row,
        record_root=tmp_path,
        webhook_files=state["webhook_files"],
        finalized=state["finalized"],
        identity_rebinds=receipts,
        identity_rebind_attestations={
            "source": adapter._attest_regular_file(stub),
            "xml": adapter._attest_regular_file(stub.with_suffix(".xml")),
            "successor_mp4": adapter._attest_regular_file(successor.with_suffix(".mp4")),
        },
    )
    state["source_disposition_identity_rebinds"] = {relative: receipts}
    state_path.write_text(json.dumps(state), encoding="utf-8")
    inventory_mount = {
        **fuse_mount,
        "mount_id": 991,
        "mount_point": "/runner/Videos",
        "root": "/live-streaming/22966160",
    }
    monkeypatch.setattr(
        source_integrity,
        "_mount_identity_for_path",
        lambda _path: inventory_mount,
    )

    audit = audit_finalized_recording_inventory(
        date_dir,
        room_id="123456",
        adapter_state_path=state_path,
    )

    assert audit["status"] == "PASS"
    assert audit["consumer_segments"] == [str(successor_mp4)]
    assert audit["issues"][0]["severity"] == "WARN"


def test_recording_inventory_accepts_portable_fuse_timestamp_rebind(
    tmp_path,
    monkeypatch,
):
    date_dir, stub, successor_mp4, state_path, receipt_mount = _typed_timestamp_rebind(
        tmp_path, monkeypatch
    )
    state = json.loads(state_path.read_text(encoding="utf-8"))
    relative = f"{date_dir.name}/{stub.name}"
    row = state["source_dispositions"][relative]
    successor = successor_mp4.with_suffix(".flv")
    receipts = state["source_disposition_identity_rebinds"][relative]
    inventory_mount = {
        **receipt_mount,
        "mount_id": 991,
        "root": "/live-streaming/22966160",
        "mount_point": "/runner/Videos",
    }
    monkeypatch.setattr(source_integrity, "_mount_identity_for_path", lambda _path: inventory_mount)

    audit = audit_finalized_recording_inventory(
        date_dir,
        room_id="123456",
        adapter_state_path=state_path,
    )

    assert audit["status"] == "PASS"
    assert audit["consumer_segments"] == [str(successor_mp4)]
    receipt = receipts[0]
    assert receipt["schema_version"] == "recording-source-fuse-timestamp-rebind.v1"
    assert receipt["current_bindings"]["successor_source"]["sha256"] is None
    assert receipt["legacy_promotion"]["successor_source"] == {
        "verification_basis": "LEGACY_NO_PRIOR_SHA256_WEBHOOK_FINALIZED_LEDGER_STABLE_STAT",
        "historical_sha256_available": False,
        "historical_sha256": None,
        "path": receipt["previous_bindings"]["successor_source"]["path"],
        "size_bytes": successor.stat().st_size,
        "mode": receipt["previous_bindings"]["successor_source"]["mode"],
        "webhook_file_size": state["webhook_files"][
            row["session"]["successor_relative_path"]
        ]["file_size"],
        "finalized_ledger_source_size": state["finalized"][
            row["session"]["successor_relative_path"]
        ]["source_size"],
        "successor_mp4_historical_sha256": state["finalized"][
            row["session"]["successor_relative_path"]
        ]["target_sha256"],
    }


@pytest.mark.parametrize("tamper", ["changed_fields", "legacy_hash_claim"])
def test_recording_inventory_blocks_timestamp_rebind_receipt_tamper(
    tmp_path,
    monkeypatch,
    tamper,
):
    date_dir, stub, successor_mp4, state_path, receipt_mount = _typed_timestamp_rebind(
        tmp_path, monkeypatch
    )
    state = json.loads(state_path.read_text(encoding="utf-8"))
    relative = f"{date_dir.name}/{stub.name}"
    receipt = state["source_disposition_identity_rebinds"][relative][0]
    if tamper == "changed_fields":
        receipt["changed_fields"]["successor_source"] = ["mtime_ns"]
    else:
        receipt["current_bindings"]["successor_source"]["sha256"] = adapter.sha256_file(
            successor_mp4.with_suffix(".flv")
        )
        receipt["legacy_promotion"]["successor_source"]["historical_sha256_available"] = True
        receipt["legacy_promotion"]["successor_source"]["historical_sha256"] = receipt[
            "current_bindings"
        ]["successor_source"]["sha256"]
    receipt["canonical_integrity"] = {
        "algorithm": "sha256",
        "canonical_json_sha256": adapter._canonical_json_sha256(
            {key: value for key, value in receipt.items() if key != "canonical_integrity"}
        ),
    }
    state_path.write_text(json.dumps(state), encoding="utf-8")
    monkeypatch.setattr(
        source_integrity,
        "_mount_identity_for_path",
        lambda _path: {**receipt_mount, "mount_id": 991, "mount_point": "/runner/Videos"},
    )

    audit = audit_finalized_recording_inventory(
        date_dir,
        room_id="123456",
        adapter_state_path=state_path,
    )

    assert audit["status"] == "BLOCKED"
    assert "binding drifted" in audit["issues"][0]["source_disposition_error"]


def test_recording_inventory_blocks_timestamp_drift_without_next_receipt(
    tmp_path,
    monkeypatch,
):
    date_dir, _stub, successor_mp4, state_path, receipt_mount = _typed_timestamp_rebind(
        tmp_path, monkeypatch
    )
    for path in (successor_mp4.with_suffix(".flv"), successor_mp4):
        current = path.stat()
        os.utime(path, ns=(current.st_atime_ns, current.st_mtime_ns + 1_000_000_000))
    monkeypatch.setattr(
        source_integrity,
        "_mount_identity_for_path",
        lambda _path: {**receipt_mount, "mount_id": 991, "mount_point": "/runner/Videos"},
    )

    audit = audit_finalized_recording_inventory(
        date_dir,
        room_id="123456",
        adapter_state_path=state_path,
    )

    assert audit["status"] == "BLOCKED"
    assert "effective fingerprint drifted" in audit["issues"][0]["source_disposition_error"]


def test_recording_inventory_blocks_timestamp_receipt_mount_drift(
    tmp_path,
    monkeypatch,
):
    date_dir, _stub, _successor_mp4, state_path, receipt_mount = _typed_timestamp_rebind(
        tmp_path, monkeypatch
    )
    monkeypatch.setattr(
        source_integrity,
        "_mount_identity_for_path",
        lambda _path: {
            **receipt_mount,
            "mount_id": 991,
            "major_minor": "0:68",
            "mount_point": "/runner/Videos",
        },
    )

    audit = audit_finalized_recording_inventory(
        date_dir,
        room_id="123456",
        adapter_state_path=state_path,
    )

    assert audit["status"] == "BLOCKED"
    assert "mount identity drifted" in audit["issues"][0]["source_disposition_error"]


def test_recording_inventory_blocks_rebind_on_different_fuse_device(
    tmp_path,
    monkeypatch,
):
    date_dir, stub, _successor_mp4, state_path = _typed_connection_stub(tmp_path, monkeypatch)
    state = json.loads(state_path.read_text(encoding="utf-8"))
    relative = f"{date_dir.name}/{stub.name}"
    row = state["source_dispositions"][relative]
    for binding in (
        row["source"],
        row["xml"],
        row["session"]["successor_source"],
        row["session"]["successor_mp4"],
    ):
        binding["device"] += 1
        binding["inode"] += 1
    unsigned = {key: value for key, value in row.items() if key != "canonical_integrity"}
    row["canonical_integrity"]["canonical_json_sha256"] = adapter._canonical_json_sha256(unsigned)
    receipt_mount = {
        "mount_id": 77,
        "major_minor": "0:67",
        "root": "/",
        "mount_point": str(tmp_path),
        "filesystem_type": "fuse.cloudfs",
        "mount_source": "CloudFS",
    }
    monkeypatch.setattr(adapter, "_mount_identity_for_path", lambda _path: receipt_mount)
    successor = date_dir / row["session"]["successor_relative_path"].split("/", 1)[1]
    receipts: list[dict] = []
    adapter.validate_connection_stub_disposition(
        stub,
        row,
        record_root=tmp_path,
        webhook_files=state["webhook_files"],
        finalized=state["finalized"],
        identity_rebinds=receipts,
        identity_rebind_attestations={
            "source": adapter._attest_regular_file(stub),
            "xml": adapter._attest_regular_file(stub.with_suffix(".xml")),
            "successor_mp4": adapter._attest_regular_file(successor.with_suffix(".mp4")),
        },
    )
    state["source_disposition_identity_rebinds"] = {relative: receipts}
    state_path.write_text(json.dumps(state), encoding="utf-8")
    monkeypatch.setattr(
        source_integrity,
        "_mount_identity_for_path",
        lambda _path: {**receipt_mount, "major_minor": "0:68", "mount_id": 88},
    )

    audit = audit_finalized_recording_inventory(
        date_dir,
        room_id="123456",
        adapter_state_path=state_path,
    )

    assert audit["status"] == "BLOCKED"
    assert "mount identity drifted" in audit["issues"][0]["source_disposition_error"]


def test_recording_inventory_accepts_historical_finalized_source_mtime(
    tmp_path,
    monkeypatch,
):
    date_dir, stub, _successor_mp4, state_path = _typed_connection_stub(tmp_path, monkeypatch)
    state = json.loads(state_path.read_text(encoding="utf-8"))
    relative = f"{date_dir.name}/{stub.name}"
    row = state["source_dispositions"][relative]
    successor_relative = row["session"]["successor_relative_path"]
    historical_mtime = state["finalized"][successor_relative]["source_mtime_ns"] - 1
    state["finalized"][successor_relative]["source_mtime_ns"] = historical_mtime
    row["session"]["successor_finalized_ledger"]["source_mtime_ns"] = historical_mtime
    unsigned = {key: value for key, value in row.items() if key != "canonical_integrity"}
    row["canonical_integrity"]["canonical_json_sha256"] = adapter._canonical_json_sha256(unsigned)
    state_path.write_text(json.dumps(state), encoding="utf-8")

    audit = audit_finalized_recording_inventory(
        date_dir,
        room_id="123456",
        adapter_state_path=state_path,
    )

    assert audit["status"] == "PASS"
    assert audit["can_select"] is True


def test_recording_inventory_blocks_same_size_successor_rewrite_with_restored_mtime(
    tmp_path,
    monkeypatch,
):
    date_dir, _stub, successor_mp4, state_path = _typed_connection_stub(tmp_path, monkeypatch)
    original = successor_mp4.read_bytes()
    original_stat = successor_mp4.stat()
    successor_mp4.write_bytes(b"x" * len(original))
    os.utime(
        successor_mp4,
        ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns),
    )

    audit = audit_finalized_recording_inventory(
        date_dir,
        room_id="123456",
        adapter_state_path=state_path,
    )

    assert audit["status"] == "BLOCKED"
    assert audit["issues"][0]["code"] == "CLOSED_FLV_WITHOUT_MP4"
    assert "drifted" in audit["issues"][0]["source_disposition_error"]


def test_recording_inventory_blocks_when_typed_stub_bytes_drift(
    tmp_path,
    monkeypatch,
):
    date_dir, stub, _successor_mp4, state_path = _typed_connection_stub(tmp_path, monkeypatch)
    stub.write_bytes(b"drifted")

    audit = audit_finalized_recording_inventory(
        date_dir,
        room_id="123456",
        adapter_state_path=state_path,
    )

    assert audit["status"] == "BLOCKED"
    assert audit["can_select"] is False
    assert audit["issues"][0]["code"] == "CLOSED_FLV_WITHOUT_MP4"
    assert "drifted" in audit["issues"][0]["source_disposition_error"]


def test_recording_inventory_blocks_resigned_cross_session_successor(
    tmp_path,
    monkeypatch,
):
    date_dir, stub, _successor_mp4, state_path = _typed_connection_stub(tmp_path, monkeypatch)
    state = json.loads(state_path.read_text(encoding="utf-8"))
    relative = f"{date_dir.name}/{stub.name}"
    row = state["source_dispositions"][relative]
    successor_relative = row["session"]["successor_relative_path"]
    state["webhook_files"][successor_relative]["session_id"] = "session-b"
    row["session"]["successor_webhook"]["session_id"] = "session-b"
    unsigned = {key: value for key, value in row.items() if key != "canonical_integrity"}
    row["canonical_integrity"]["canonical_json_sha256"] = adapter._canonical_json_sha256(unsigned)
    state_path.write_text(json.dumps(state), encoding="utf-8")

    audit = audit_finalized_recording_inventory(
        date_dir,
        room_id="123456",
        adapter_state_path=state_path,
    )

    assert audit["status"] == "BLOCKED"
    assert "same-session" in audit["issues"][0]["source_disposition_error"]


def test_danmaku_outruns_tiny_media_requires_replay_compensation():
    ledger = build_source_range_ledger(
        room_id="123456",
        session_date="2026-06-29",
        segments=[
            MediaSegmentObservation(
                path="/app/Videos/123456/2026-06-29/tiny.m4s",
                start_ms=0,
                end_ms=4_040,
                duration_ms=4_040,
                size_bytes=3_369,
                probed_ok=True,
            )
        ],
        expected_start_ms=0,
        expected_end_ms=7_200_000,
        danmaku_latest_ms=7_100_000,
        active_media_size_growth_bytes=0,
    )

    assert ledger.compensation_required is True
    assert ledger.replay_probe_required is True
    assert ledger.can_use_local_source is False
    assert ledger.missing_ranges[0].start_ms == 4_040
    codes = {issue.code for issue in ledger.issues}
    assert "MEDIA_SEGMENT_TOO_SHORT" in codes
    assert "DANMAKU_OUTRUNS_MEDIA" in codes
    assert "MEDIA_STALLED_WHILE_DANMAKU_ADVANCES" in codes


def test_contiguous_valid_segments_do_not_need_replay_probe():
    ledger = build_source_range_ledger(
        room_id="123456",
        session_date="2026-06-29",
        segments=[
            MediaSegmentObservation(
                path="a.m4s", start_ms=0, end_ms=60_000, duration_ms=60_000, size_bytes=5_000_000
            ),
            MediaSegmentObservation(
                path="b.m4s",
                start_ms=60_000,
                end_ms=120_000,
                duration_ms=60_000,
                size_bytes=5_000_000,
            ),
        ],
        expected_start_ms=0,
        expected_end_ms=120_000,
        danmaku_latest_ms=119_000,
        active_media_size_growth_bytes=1024,
    )

    assert ledger.compensation_required is False
    assert ledger.replay_probe_required is False
    assert ledger.can_use_local_source is True
    assert ledger.missing_ranges == ()
    assert ledger.issues == ()


def test_unprobeable_redundant_sidecar_warns_without_replay_probe():
    ledger = build_source_range_ledger(
        room_id="123456",
        session_date="2026-07-01",
        segments=[
            MediaSegmentObservation(
                path="raw-sidecar.m4s",
                start_ms=0,
                end_ms=0,
                duration_ms=0,
                size_bytes=3_369,
                probed_ok=False,
            ),
            MediaSegmentObservation(
                path="decoded-recording.flv",
                start_ms=0,
                end_ms=120_000,
                duration_ms=120_000,
                size_bytes=5_000_000,
                probed_ok=True,
            ),
        ],
        expected_start_ms=0,
        expected_end_ms=120_000,
    )

    assert ledger.compensation_required is False
    assert ledger.replay_probe_required is False
    assert ledger.can_use_local_source is True
    assert ledger.missing_ranges == ()
    probe_issue = next(issue for issue in ledger.issues if issue.code == "MEDIA_PROBE_FAILED")
    assert probe_issue.severity == "WARN"
    assert probe_issue.to_manifest()["severity"] == "WARN"


def test_unprobeable_segment_not_covered_by_sibling_still_blocks():
    ledger = build_source_range_ledger(
        room_id="123456",
        session_date="2026-07-01",
        segments=[
            MediaSegmentObservation(
                path="possibly-unique.m4s",
                start_ms=0,
                end_ms=120_000,
                duration_ms=120_000,
                size_bytes=5_000_000,
                probed_ok=False,
            ),
            MediaSegmentObservation(
                path="short-sibling.flv",
                start_ms=0,
                end_ms=60_000,
                duration_ms=60_000,
                size_bytes=5_000_000,
                probed_ok=True,
            ),
        ],
        expected_start_ms=0,
        expected_end_ms=120_000,
    )

    assert ledger.compensation_required is True
    assert ledger.replay_probe_required is True
    assert ledger.can_use_local_source is False
    probe_issue = next(issue for issue in ledger.issues if issue.code == "MEDIA_PROBE_FAILED")
    assert probe_issue.severity == "BLOCK"


def test_tiny_restart_stub_warns_when_verified_coverage_is_complete():
    ledger = build_source_range_ledger(
        room_id="123456",
        session_date="2026-07-01",
        segments=[
            MediaSegmentObservation(
                path="a.flv", start_ms=0, end_ms=60_000, duration_ms=60_000, size_bytes=5_000_000
            ),
            MediaSegmentObservation(
                path="restart-stub.flv",
                start_ms=60_000,
                end_ms=64_000,
                duration_ms=4_000,
                size_bytes=3_369,
            ),
            MediaSegmentObservation(
                path="b.flv",
                start_ms=64_000,
                end_ms=120_000,
                duration_ms=56_000,
                size_bytes=5_000_000,
            ),
        ],
        expected_start_ms=0,
        expected_end_ms=120_000,
        max_gap_ms=2_000,
    )

    assert ledger.compensation_required is False
    assert ledger.replay_probe_required is False
    assert ledger.can_use_local_source is True
    assert ledger.missing_ranges == ()
    issue_by_code = {issue.code: issue for issue in ledger.issues}
    assert issue_by_code["MEDIA_SEGMENT_TOO_SHORT"].severity == "WARN"
    assert issue_by_code["MEDIA_SEGMENT_TOO_SMALL"].severity == "WARN"


def test_gap_between_segments_records_missing_source_range():
    ledger = build_source_range_ledger(
        room_id="123456",
        session_date="2026-06-29",
        segments=[
            MediaSegmentObservation(
                path="a.m4s", start_ms=0, end_ms=60_000, duration_ms=60_000, size_bytes=5_000_000
            ),
            MediaSegmentObservation(
                path="b.m4s",
                start_ms=90_000,
                end_ms=150_000,
                duration_ms=60_000,
                size_bytes=5_000_000,
            ),
        ],
        expected_start_ms=0,
        expected_end_ms=150_000,
        max_gap_ms=2_000,
    )

    assert ledger.compensation_required is True
    assert [(r.start_ms, r.end_ms) for r in ledger.missing_ranges] == [(60_000, 90_000)]
    assert "MEDIA_COVERAGE_GAP" in {issue.code for issue in ledger.issues}


def test_replay_compensation_plan_fails_closed_without_auth_or_availability():
    ledger = build_source_range_ledger(
        room_id="123456",
        session_date="2026-06-29",
        segments=[],
        expected_start_ms=0,
        expected_end_ms=60_000,
    )

    no_auth = plan_bilibili_replay_compensation(ledger, auth_available=False, replay_available=None)
    assert no_auth.status == "BLOCKED"
    assert "BILIBILI_REPLAY_AUTH_REQUIRED" in no_auth.reason_codes
    assert no_auth.download_ranges == ()

    unavailable = plan_bilibili_replay_compensation(
        ledger, auth_available=True, replay_available=False
    )
    assert unavailable.status == "RETRY_INFRA"
    assert "BILIBILI_REPLAY_UNAVAILABLE" in unavailable.reason_codes
    assert unavailable.download_ranges == ()


def test_replay_compensation_plan_is_download_ready_only_when_auth_and_replay_available():
    ledger = build_source_range_ledger(
        room_id="123456",
        session_date="2026-06-29",
        segments=[],
        expected_start_ms=0,
        expected_end_ms=60_000,
    )

    plan = plan_bilibili_replay_compensation(ledger, auth_available=True, replay_available=True)

    assert plan.status == "READY_TO_DOWNLOAD"
    assert plan.reason_codes == ()
    assert [(r.start_ms, r.end_ms) for r in plan.download_ranges] == [(0, 60_000)]
    manifest = plan.to_manifest()
    assert "cookie" not in str(manifest).lower()
    assert manifest["room_id"] == "123456"


def test_replay_download_command_plan_fails_closed_without_url_tool_or_auth(tmp_path):
    ledger = build_source_range_ledger(
        room_id="123456",
        session_date="2026-06-29",
        segments=[],
        expected_start_ms=60_000,
        expected_end_ms=90_000,
    )
    compensation = plan_bilibili_replay_compensation(
        ledger, auth_available=True, replay_available=True
    )

    missing_url = plan_bilibili_replay_download_commands(
        compensation,
        replay_url=None,
        output_dir=tmp_path,
        cookie_file=tmp_path / "cookie.json",
        tool_path="/usr/bin/yt-dlp",
    )
    assert missing_url.status == "BLOCKED"
    assert "BILIBILI_REPLAY_URL_REQUIRED" in missing_url.reason_codes
    assert missing_url.commands == ()

    missing_auth = plan_bilibili_replay_download_commands(
        compensation,
        replay_url="https://www.bilibili.com/video/BVplaceholder",
        output_dir=tmp_path,
        cookie_file=None,
        tool_path="/usr/bin/yt-dlp",
    )
    assert missing_auth.status == "BLOCKED"
    assert "BILIBILI_REPLAY_AUTH_REQUIRED" in missing_auth.reason_codes
    assert missing_auth.commands == ()

    missing_tool = plan_bilibili_replay_download_commands(
        compensation,
        replay_url="https://www.bilibili.com/video/BVplaceholder",
        output_dir=tmp_path,
        cookie_file=tmp_path / "cookie.json",
        tool_path=None,
    )
    assert missing_tool.status == "RETRY_INFRA"
    assert "BILIBILI_DOWNLOAD_TOOL_MISSING" in missing_tool.reason_codes
    assert missing_tool.commands == ()


def test_replay_download_command_plan_builds_redacted_yt_dlp_sections(tmp_path):
    ledger = build_source_range_ledger(
        room_id="123456",
        session_date="2026-06-29",
        segments=[],
        expected_start_ms=60_000,
        expected_end_ms=90_000,
    )
    compensation = plan_bilibili_replay_compensation(
        ledger, auth_available=True, replay_available=True
    )
    cookie_file = tmp_path / "cookie.json"

    command_plan = plan_bilibili_replay_download_commands(
        compensation,
        replay_url="https://www.bilibili.com/video/BVplaceholder",
        output_dir=tmp_path / "downloads",
        cookie_file=cookie_file,
        tool_path="/usr/local/bin/yt-dlp",
    )

    assert command_plan.status == "READY"
    assert command_plan.reason_codes == ()
    assert len(command_plan.commands) == 1
    argv = command_plan.commands[0]
    assert argv[:2] == ("/usr/local/bin/yt-dlp", "https://www.bilibili.com/video/BVplaceholder")
    assert "--download-sections" in argv
    assert "*00:01:00.000-00:01:30.000" in argv
    assert "--cookies" in argv
    assert str(cookie_file) in argv
    manifest = command_plan.to_manifest()
    assert str(cookie_file) not in str(manifest)
    assert "[REDACTED_COOKIE_FILE]" in str(manifest)
