from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import time

import pytest

from ops.recording import bililive_recorder_adapter as adapter


def _xml(*, start_time: str = "2026-07-23T13:57:26.3170443+08:00") -> str:
    danmaku_info = [
        [0, 1, 25, 16777215, 1784786247],
        "普通弹幕",
        [123, "弹幕用户", 0, 0],
    ]
    superchat = {
        "id": 9,
        "message": "醒目留言",
        "uid": 456,
        "price": 30,
        "time": 60,
        "ts": 1784786248,
        "user_info": {"uname": "SC用户"},
    }
    gift = {
        "giftName": "辣条",
        "num": 2,
        "uid": 789,
        "uname": "礼物用户",
        "send_time": 1784786249,
    }
    guard = {
        "guard_level": 3,
        "num": 1,
        "uid": 101,
        "username": "舰长用户",
        "start_time": 1784786250,
    }
    from xml.sax.saxutils import quoteattr

    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        "<i>"
        '<BililiveRecorder version="2.18.0"/>'
        f'<BililiveRecorderRecordInfo roomid="123456" name="主播" '
        f'title="测试" start_time="{start_time}"/>'
        f'<d p="1.000,1,25,16777215,0,0,123,0" user="弹幕用户" '
        f"raw={quoteattr(json.dumps(danmaku_info, ensure_ascii=False))}>普通弹幕</d>"
        f'<sc ts="2.000" user="SC用户" uid="456" price="30" time="60" '
        f"raw={quoteattr(json.dumps(superchat, ensure_ascii=False))}>醒目留言</sc>"
        f'<gift ts="3.000" user="礼物用户" uid="789" giftname="辣条" giftcount="2" '
        f"raw={quoteattr(json.dumps(gift, ensure_ascii=False))}/>"
        f'<guard ts="4.000" user="舰长用户" uid="101" level="3" count="1" '
        f"raw={quoteattr(json.dumps(guard, ensure_ascii=False))}/>"
        "</i>\n"
    )


def _connection_stub_xml(
    *,
    start_time: str = "2026-08-12T20:29:51.0000000+08:00",
    event_count: int = 1,
) -> str:
    from xml.sax.saxutils import quoteattr

    events = []
    for index in range(event_count):
        raw = [[0, 1, 25, 16777215, 1786537791 + index], "你好", [123, "观众", 0, 0]]
        events.append(
            '<d p="0.009,1,25,16777215,0,0,123,0" user="观众" '
            f"raw={quoteattr(json.dumps(raw, ensure_ascii=False))}>你好</d>"
        )
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        "<i>"
        '<BililiveRecorder version="2.18.0"/>'
        '<BililiveRecorderRecordInfo roomid="123456" name="主播" title="测试" '
        f'start_time="{start_time}"/>' + "".join(events) + "</i>\n"
    )


def _connection_stub_fixture(tmp_path: Path, monkeypatch):
    date_dir = tmp_path / "2026-08-12"
    date_dir.mkdir(parents=True)
    stub = date_dir / "123456_20260812-20-29-51.flv"
    successor = date_dir / "123456_20260812-20-29-54.flv"
    successor_mp4 = successor.with_suffix(".mp4")
    stub.write_bytes(b"first-connection-stub")
    stub.with_suffix(".xml").write_text(_connection_stub_xml(), encoding="utf-8")
    successor.write_bytes(b"valid-successor-source")
    successor_mp4.write_bytes(b"valid-successor-mp4")
    session_id = "a9af9685-8991-4923-af3e-ef6067d1b9bb"
    stub_relative = f"{date_dir.name}/{stub.name}"
    successor_relative = f"{date_dir.name}/{successor.name}"
    webhook_files = {
        stub_relative: {
            "status": "CLOSED",
            "session_id": session_id,
            "opening_event_id": "stub-opening",
            "closing_event_id": "stub-closed",
            "file_open_time": "2026-08-12T20:29:52.70775+08:00",
            "file_close_time": "2026-08-12T20:29:53.1716935+08:00",
            "file_size": stub.stat().st_size,
            "duration": 3.03,
        },
        successor_relative: {
            "status": "CLOSED",
            "session_id": session_id,
            "opening_event_id": "successor-opening",
            "closing_event_id": "successor-closed",
            "file_open_time": "2026-08-12T20:29:54.3497935+08:00",
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
    return stub, successor, webhook_files, finalized


def _resign_disposition_after_simulated_remount(row: dict) -> None:
    """Make the persisted row look like it came from the prior FUSE epoch."""

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


def _fuse_mount_identity(_path: Path) -> dict[str, object]:
    return {
        "mount_id": 77,
        "major_minor": "0:67",
        "root": "/",
        "mount_point": "/adapter/Videos",
        "filesystem_type": "fuse.cloudfs",
        "mount_source": "CloudFS",
    }


def _identity_rebind_attestations(stub: Path, successor: Path) -> dict[str, dict]:
    paths = {
        "source": stub,
        "xml": stub.with_suffix(".xml"),
        "successor_mp4": successor.with_suffix(".mp4"),
    }
    return {role: adapter._attest_regular_file(path) for role, path in paths.items()}


def _args(tmp_path: Path) -> argparse.Namespace:
    return argparse.Namespace(
        room=123456,
        endpoint="http://127.0.0.1:1/graphql",
        record_root=tmp_path / "recordings",
        status_path=tmp_path / "runtime/status.json",
        state_path=tmp_path / "runtime/state.json",
        webhook_journal=tmp_path / "runtime/webhook-events.jsonl",
        env_file=tmp_path / "missing.env",
        recorder_config=tmp_path / "missing-config.json",
        cookie_health_refresh=6 * 60 * 60,
        cookie_health_timeout=1.0,
        managed_since_epoch=0.0,
        api_timeout=1.0,
        inactive_grace_seconds=0,
        status_continuity_max_gap_seconds=150,
        max_finalize=8,
        ffmpeg="ffmpeg",
        ffprobe="ffprobe",
    )


def test_committed_config_prioritizes_1080p_avc_and_forces_ipv4() -> None:
    path = Path(__file__).resolve().parents[1] / "ops/recording/bililive_recorder.config.v3.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    global_config = payload["global"]

    assert global_config["RecordingQuality"]["Value"].split(",")[0] == "avc10000"
    assert global_config["RecordingQuality"]["Value"].split(",") == [
        "avc10000",
        "avc400",
        "avc250",
    ]
    assert global_config["NetworkTransportAllowedAddressFamily"] == {
        "HasValue": True,
        "Value": 1,
    }
    assert payload["rooms"] == [
        {
            "RoomId": {"HasValue": True, "Value": 22966160},
            "AutoRecord": {"HasValue": True, "Value": True},
        }
    ]


def test_xml_to_jsonl_restores_bilibili_command_envelopes(tmp_path: Path) -> None:
    xml_path = tmp_path / "123456_20260723-13-57-26.xml"
    xml_path.write_text(_xml(), encoding="utf-8")

    payload, info, event_count = adapter.xml_to_jsonl(xml_path)
    rows = [json.loads(line) for line in payload.decode().splitlines()]

    assert event_count == 4
    assert info["start_time"].startswith("2026-07-23T13:57:26")
    assert [row["cmd"] for row in rows] == [
        "DANMU_MSG",
        "SUPER_CHAT_MESSAGE",
        "SEND_GIFT",
        "GUARD_BUY",
    ]
    assert rows[0]["info"][1] == "普通弹幕"
    assert rows[1]["data"]["message"] == "醒目留言"
    assert rows[2]["data"]["giftName"] == "辣条"


def test_xml_without_raw_evidence_is_rejected(tmp_path: Path) -> None:
    xml_path = tmp_path / "123456_20260723-13-57-26.xml"
    xml_path.write_text(
        '<?xml version="1.0"?><i><BililiveRecorder version="2.18.0"/>'
        '<BililiveRecorderRecordInfo roomid="123456" name="主播" '
        'title="测试" start_time="2026-07-23T13:57:26+08:00"/>'
        '<d p="1,1,25,1,0,0,1,0">没有 raw</d></i>',
        encoding="utf-8",
    )

    with pytest.raises(adapter.AdapterError, match="lacks required raw evidence"):
        adapter.xml_to_jsonl(xml_path)


def test_webhook_journal_dedupes_and_preserves_closed_state_when_out_of_order(
    tmp_path: Path,
) -> None:
    journal = tmp_path / "webhook-events.jsonl"
    relative = "Videos/123456/2026-07-23/123456_20260723-13-57-26.flv"
    closed = {
        "EventType": "FileClosed",
        "EventTimestamp": "2026-07-23T14:27:26.1234567+08:00",
        "EventId": "11111111-1111-4111-8111-111111111111",
        "EventData": {
            "RoomId": 123456,
            "SessionId": "session-a",
            "RelativePath": relative,
            "FileSize": 123456,
            "Duration": 1800.0,
            "FileOpenTime": "2026-07-23T13:57:26.1234567+08:00",
            "FileCloseTime": "2026-07-23T14:27:26.1234567+08:00",
        },
    }
    opening = {
        "EventType": "FileOpening",
        "EventTimestamp": "2026-07-23T13:57:26+08:00",
        "EventId": "22222222-2222-4222-8222-222222222222",
        "EventData": {
            "RoomId": 123456,
            "SessionId": "session-a",
            "RelativePath": relative,
            "FileOpenTime": "2026-07-23T13:57:26+08:00",
        },
    }
    adapter.append_webhook_event(journal, closed, room_id=123456)
    adapter.append_webhook_event(journal, closed, room_id=123456)
    adapter.append_webhook_event(journal, opening, room_id=123456)
    state = {}

    assert adapter.reconcile_webhook_journal(journal, state, room_id=123456)
    assert not adapter.reconcile_webhook_journal(journal, state, room_id=123456)
    row = state["webhook_files"]["2026-07-23/123456_20260723-13-57-26.flv"]
    assert row["status"] == "CLOSED"
    assert row["file_size"] == 123456
    assert len(state["webhook_event_ids"]) == 2


@pytest.mark.parametrize("event_count", [0, 1])
def test_connection_stub_disposition_binds_first_opening_and_finalized_successor(
    tmp_path: Path,
    monkeypatch,
    event_count: int,
) -> None:
    stub, successor, webhook_files, finalized = _connection_stub_fixture(tmp_path, monkeypatch)
    stub.with_suffix(".xml").write_text(
        _connection_stub_xml(event_count=event_count), encoding="utf-8"
    )

    row = adapter.build_connection_stub_disposition(
        stub,
        record_root=tmp_path,
        webhook_files=webhook_files,
        finalized=finalized,
    )

    assert row is not None
    assert row["schema_version"] == "recording-connection-stub.v1"
    assert row["status"] == "IGNORED_CONNECTION_STUB"
    assert row["reason_code"] == "RECORDER_CONNECTION_STUB_NO_DECODABLE_VIDEO"
    assert row["source"]["sha256"] == adapter.sha256_file(stub)
    assert row["xml"]["event_count"] == event_count
    assert row["decode"]["decoded_video_frames"] == 0
    assert row["decode"]["video_packets"] == 0
    assert row["session"]["prior_same_session_openings"] == 0
    assert row["session"]["successor_relative_path"].endswith(successor.name)
    assert row["source_action"] == {
        "finalized": False,
        "delete_source": "never",
        "move_source": "never",
    }
    adapter.validate_connection_stub_disposition(
        stub,
        row,
        record_root=tmp_path,
        webhook_files=webhook_files,
        finalized=finalized,
    )


def test_connection_stub_rejects_more_than_one_orphan_xml_event(
    tmp_path: Path,
    monkeypatch,
) -> None:
    stub, successor, webhook_files, finalized = _connection_stub_fixture(tmp_path, monkeypatch)
    stub.with_suffix(".xml").write_text(_connection_stub_xml(event_count=2), encoding="utf-8")
    original_attest = adapter._attest_regular_file

    def refuse_expensive_successor_attestation(path: Path):
        if path == successor.with_suffix(".mp4"):
            pytest.fail("ineligible stub must reject before hashing successor MP4")
        return original_attest(path)

    monkeypatch.setattr(adapter, "_attest_regular_file", refuse_expensive_successor_attestation)

    assert (
        adapter.build_connection_stub_disposition(
            stub,
            record_root=tmp_path,
            webhook_files=webhook_files,
            finalized=finalized,
        )
        is None
    )


def test_connection_stub_requires_no_prior_same_session_opening(
    tmp_path: Path,
    monkeypatch,
) -> None:
    stub, successor, webhook_files, finalized = _connection_stub_fixture(tmp_path, monkeypatch)
    webhook_files["2026-08-12/123456_20260812-20-29-50.flv"] = {
        **webhook_files[next(iter(webhook_files))],
        "opening_event_id": "earlier-opening",
        "closing_event_id": "earlier-closed",
        "file_open_time": "2026-08-12T20:29:50+08:00",
        "file_close_time": "2026-08-12T20:29:51+08:00",
    }

    assert (
        adapter.build_connection_stub_disposition(
            stub,
            record_root=tmp_path,
            webhook_files=webhook_files,
            finalized=finalized,
        )
        is None
    )


def test_connection_stub_requires_successor_within_two_seconds(
    tmp_path: Path,
    monkeypatch,
) -> None:
    stub, successor, webhook_files, finalized = _connection_stub_fixture(tmp_path, monkeypatch)
    webhook_files[f"2026-08-12/{successor.name}"]["file_open_time"] = (
        "2026-08-12T20:29:55.5000000+08:00"
    )

    assert (
        adapter.build_connection_stub_disposition(
            stub,
            record_root=tmp_path,
            webhook_files=webhook_files,
            finalized=finalized,
        )
        is None
    )


def test_connection_stub_requires_immediate_next_opening_to_share_session(
    tmp_path: Path,
    monkeypatch,
) -> None:
    stub, successor, webhook_files, finalized = _connection_stub_fixture(tmp_path, monkeypatch)
    webhook_files["2026-08-12/123456_20260812-20-29-53.flv"] = {
        "status": "CLOSED",
        "session_id": "different-session",
        "opening_event_id": "other-opening",
        "closing_event_id": "other-closed",
        "file_open_time": "2026-08-12T20:29:54+08:00",
        "file_close_time": "2026-08-12T20:29:54.1000000+08:00",
        "file_size": 1,
        "duration": 0.1,
    }

    assert (
        adapter.build_connection_stub_disposition(
            stub,
            record_root=tmp_path,
            webhook_files=webhook_files,
            finalized=finalized,
        )
        is None
    )


def test_connection_stub_requires_zero_event_official_xml(
    tmp_path: Path,
    monkeypatch,
) -> None:
    stub, _successor, webhook_files, finalized = _connection_stub_fixture(tmp_path, monkeypatch)
    stub.with_suffix(".xml").write_text(_xml(), encoding="utf-8")

    assert (
        adapter.build_connection_stub_disposition(
            stub,
            record_root=tmp_path,
            webhook_files=webhook_files,
            finalized=finalized,
        )
        is None
    )


def test_decodable_short_video_stays_in_normal_finalization_lane(
    tmp_path: Path,
    monkeypatch,
) -> None:
    stub, _successor, webhook_files, finalized = _connection_stub_fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(
        adapter,
        "probe_connection_stub_video",
        lambda *_args, **_kwargs: {
            "duration_seconds": 3.03,
            "size_bytes": stub.stat().st_size,
            "video_codec": "h264",
            "width": 1920,
            "height": 1080,
            "decoded_video_frames": 75,
            "video_packets": 75,
        },
    )

    assert (
        adapter.build_connection_stub_disposition(
            stub,
            record_root=tmp_path,
            webhook_files=webhook_files,
            finalized=finalized,
        )
        is None
    )


def test_connection_stub_requires_real_successor_mp4_to_match_ledger(
    tmp_path: Path,
    monkeypatch,
) -> None:
    stub, successor, webhook_files, finalized = _connection_stub_fixture(tmp_path, monkeypatch)
    finalized[f"2026-08-12/{successor.name}"]["target_sha256"] = "0" * 64

    assert (
        adapter.build_connection_stub_disposition(
            stub,
            record_root=tmp_path,
            webhook_files=webhook_files,
            finalized=finalized,
        )
        is None
    )


def test_connection_stub_successor_hash_read_error_stays_in_normal_fail_closed_lane(
    tmp_path: Path,
    monkeypatch,
) -> None:
    stub, _successor, webhook_files, finalized = _connection_stub_fixture(tmp_path, monkeypatch)
    real_attest = adapter._attest_regular_file

    def unreadable_successor(path: Path):
        if path.suffix == ".mp4":
            raise adapter.AdapterError("simulated read failure")
        return real_attest(path)

    monkeypatch.setattr(adapter, "_attest_regular_file", unreadable_successor)

    assert (
        adapter.build_connection_stub_disposition(
            stub,
            record_root=tmp_path,
            webhook_files=webhook_files,
            finalized=finalized,
        )
        is None
    )


@pytest.mark.parametrize(
    "broken_gate",
    [
        "source_not_closed",
        "source_opening_id_missing",
        "source_closing_id_missing",
        "source_size_mismatch",
        "source_duration_ten_seconds",
        "source_wall_ten_seconds",
        "successor_not_closed",
        "successor_session_mismatch",
        "successor_closing_id_missing",
        "successor_ledger_size_mismatch",
    ],
)
def test_connection_stub_requires_every_event_session_time_and_stat_gate(
    tmp_path: Path,
    monkeypatch,
    broken_gate: str,
) -> None:
    stub, successor, webhook_files, finalized = _connection_stub_fixture(tmp_path, monkeypatch)
    stub_relative = f"2026-08-12/{stub.name}"
    successor_relative = f"2026-08-12/{successor.name}"
    source_event = webhook_files[stub_relative]
    successor_event = webhook_files[successor_relative]
    if broken_gate == "source_not_closed":
        source_event["status"] = "OPEN"
    elif broken_gate == "source_opening_id_missing":
        source_event["opening_event_id"] = None
    elif broken_gate == "source_closing_id_missing":
        source_event["closing_event_id"] = None
    elif broken_gate == "source_size_mismatch":
        source_event["file_size"] += 1
    elif broken_gate == "source_duration_ten_seconds":
        source_event["duration"] = 10.0
    elif broken_gate == "source_wall_ten_seconds":
        source_event["file_close_time"] = "2026-08-12T20:30:02.70775+08:00"
    elif broken_gate == "successor_not_closed":
        successor_event["status"] = "OPEN"
    elif broken_gate == "successor_session_mismatch":
        successor_event["session_id"] = "different-session"
    elif broken_gate == "successor_closing_id_missing":
        successor_event["closing_event_id"] = None
    elif broken_gate == "successor_ledger_size_mismatch":
        finalized[successor_relative]["source_size"] += 1

    assert (
        adapter.build_connection_stub_disposition(
            stub,
            record_root=tmp_path,
            webhook_files=webhook_files,
            finalized=finalized,
        )
        is None
    )


def test_connection_stub_preserves_historical_ledger_mtime_after_cloudfs_settles(
    tmp_path: Path,
    monkeypatch,
) -> None:
    stub, successor, webhook_files, finalized = _connection_stub_fixture(tmp_path, monkeypatch)
    successor_relative = f"2026-08-12/{successor.name}"
    finalized[successor_relative]["source_mtime_ns"] -= 1

    row = adapter.build_connection_stub_disposition(
        stub,
        record_root=tmp_path,
        webhook_files=webhook_files,
        finalized=finalized,
    )

    assert row is not None
    assert (
        row["session"]["successor_finalized_ledger"]["source_mtime_ns"]
        != row["session"]["successor_source"]["mtime_ns"]
    )
    adapter.validate_connection_stub_disposition(
        stub,
        row,
        record_root=tmp_path,
        webhook_files=webhook_files,
        finalized=finalized,
    )


@pytest.mark.parametrize("unexpected_final_lane", ["mp4", "ledger"])
def test_connection_stub_disposition_drift_if_stub_enters_final_lane(
    tmp_path: Path,
    monkeypatch,
    unexpected_final_lane: str,
) -> None:
    stub, _successor, webhook_files, finalized = _connection_stub_fixture(tmp_path, monkeypatch)
    row = adapter.build_connection_stub_disposition(
        stub,
        record_root=tmp_path,
        webhook_files=webhook_files,
        finalized=finalized,
    )
    assert row is not None
    relative = f"2026-08-12/{stub.name}"
    if unexpected_final_lane == "mp4":
        stub.with_suffix(".mp4").write_bytes(b"unexpected-finalization")
    else:
        finalized[relative] = {"unexpected": "finalization"}

    with pytest.raises(adapter.AdapterError, match="evidence drifted"):
        adapter.validate_connection_stub_disposition(
            stub,
            row,
            record_root=tmp_path,
            webhook_files=webhook_files,
            finalized=finalized,
        )


def test_connection_stub_disposition_drift_fails_closed(
    tmp_path: Path,
    monkeypatch,
) -> None:
    stub, _successor, webhook_files, finalized = _connection_stub_fixture(tmp_path, monkeypatch)
    row = adapter.build_connection_stub_disposition(
        stub,
        record_root=tmp_path,
        webhook_files=webhook_files,
        finalized=finalized,
    )
    assert row is not None
    stub.write_bytes(b"drifted-stub")

    with pytest.raises(adapter.AdapterError, match="fingerprint drifted"):
        adapter.validate_connection_stub_disposition(
            stub,
            row,
            record_root=tmp_path,
            webhook_files=webhook_files,
            finalized=finalized,
        )


def test_connection_stub_fuse_remount_rebinds_once_then_stays_metadata_only(
    tmp_path: Path,
    monkeypatch,
) -> None:
    stub, successor, webhook_files, finalized = _connection_stub_fixture(tmp_path, monkeypatch)
    row = adapter.build_connection_stub_disposition(
        stub,
        record_root=tmp_path,
        webhook_files=webhook_files,
        finalized=finalized,
    )
    assert row is not None
    _resign_disposition_after_simulated_remount(row)
    monkeypatch.setattr(adapter, "_mount_identity_for_path", _fuse_mount_identity)
    rebinds: list[dict] = []

    adapter.validate_connection_stub_disposition(
        stub,
        row,
        record_root=tmp_path,
        webhook_files=webhook_files,
        finalized=finalized,
        identity_rebinds=rebinds,
        identity_rebind_attestations=_identity_rebind_attestations(stub, successor),
    )

    assert len(rebinds) == 1
    assert rebinds[0]["schema_version"] == "recording-source-fuse-identity-rebind.v1"
    assert rebinds[0]["policy"] == "FUSE_REMOUNT_DEVICE_INODE_REBIND"
    assert rebinds[0]["current_mount"]["filesystem_type"] == "fuse.cloudfs"
    assert (
        rebinds[0]["previous_bindings"]["source"]["device"]
        != (rebinds[0]["current_bindings"]["source"]["device"])
    )

    monkeypatch.setattr(
        adapter,
        "_attest_regular_file",
        lambda _path: pytest.fail("a settled rebind must remain metadata-only"),
    )
    adapter.validate_connection_stub_disposition(
        stub,
        row,
        record_root=tmp_path,
        webhook_files=webhook_files,
        finalized=finalized,
        identity_rebinds=rebinds,
    )
    assert len(rebinds) == 1


def test_connection_stub_local_inode_drift_still_fails_closed(
    tmp_path: Path,
    monkeypatch,
) -> None:
    stub, _successor, webhook_files, finalized = _connection_stub_fixture(tmp_path, monkeypatch)
    row = adapter.build_connection_stub_disposition(
        stub,
        record_root=tmp_path,
        webhook_files=webhook_files,
        finalized=finalized,
    )
    assert row is not None
    _resign_disposition_after_simulated_remount(row)
    monkeypatch.setattr(adapter, "_mount_identity_for_path", lambda _path: None)

    with pytest.raises(adapter.AdapterError, match="outside one shared FUSE mount"):
        adapter.validate_connection_stub_disposition(
            stub,
            row,
            record_root=tmp_path,
            webhook_files=webhook_files,
            finalized=finalized,
            identity_rebinds=[],
        )


def test_connection_stub_fuse_rebind_refuses_non_identity_stat_drift(
    tmp_path: Path,
    monkeypatch,
) -> None:
    stub, _successor, webhook_files, finalized = _connection_stub_fixture(tmp_path, monkeypatch)
    row = adapter.build_connection_stub_disposition(
        stub,
        record_root=tmp_path,
        webhook_files=webhook_files,
        finalized=finalized,
    )
    assert row is not None
    _resign_disposition_after_simulated_remount(row)
    row["source"]["mtime_ns"] -= 1
    unsigned = {key: value for key, value in row.items() if key != "canonical_integrity"}
    row["canonical_integrity"]["canonical_json_sha256"] = adapter._canonical_json_sha256(unsigned)
    monkeypatch.setattr(adapter, "_mount_identity_for_path", _fuse_mount_identity)

    with pytest.raises(adapter.AdapterError, match="non-identity fingerprint drift"):
        adapter.validate_connection_stub_disposition(
            stub,
            row,
            record_root=tmp_path,
            webhook_files=webhook_files,
            finalized=finalized,
            identity_rebinds=[],
        )


@pytest.mark.parametrize("binding_name", ["source", "xml", "successor_mp4"])
def test_connection_stub_fuse_rebind_rehashes_every_historically_bound_file(
    tmp_path: Path,
    monkeypatch,
    binding_name: str,
) -> None:
    stub, successor, webhook_files, finalized = _connection_stub_fixture(tmp_path, monkeypatch)
    row = adapter.build_connection_stub_disposition(
        stub,
        record_root=tmp_path,
        webhook_files=webhook_files,
        finalized=finalized,
    )
    assert row is not None
    _resign_disposition_after_simulated_remount(row)
    corrupted_path = {
        "source": stub,
        "xml": stub.with_suffix(".xml"),
        "successor_mp4": successor.with_suffix(".mp4"),
    }[binding_name]
    real_attest = adapter._attest_regular_file

    def attest_with_hash_drift(path: Path) -> dict:
        attestation = real_attest(path)
        if path == corrupted_path:
            attestation["sha256"] = "0" * 64
        return attestation

    monkeypatch.setattr(
        adapter,
        "_attest_regular_file",
        attest_with_hash_drift,
    )
    monkeypatch.setattr(adapter, "_mount_identity_for_path", _fuse_mount_identity)

    with pytest.raises(adapter.AdapterError, match="full-byte hash drift"):
        adapter.validate_connection_stub_disposition(
            stub,
            row,
            record_root=tmp_path,
            webhook_files=webhook_files,
            finalized=finalized,
            identity_rebinds=[],
            identity_rebind_attestations=_identity_rebind_attestations(stub, successor),
        )


def test_run_once_auto_records_and_revalidates_connection_stub_disposition(
    tmp_path: Path,
    monkeypatch,
) -> None:
    args = _args(tmp_path)
    stub, successor, webhook_files, finalized = _connection_stub_fixture(
        args.record_root, monkeypatch
    )
    args.state_path.parent.mkdir(parents=True, exist_ok=True)
    args.state_path.write_text(
        json.dumps(
            {
                "schema_version": adapter.STATE_SCHEMA_VERSION,
                "managed_since_epoch": 0.0,
                "finalized": finalized,
                "webhook_files": webhook_files,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        adapter,
        "query_room_status",
        lambda *_args, **_kwargs: {
            "streaming": False,
            "recording": False,
            "danmakuConnected": False,
            "ioStats": {},
            "recordingStats": {},
        },
    )
    monkeypatch.setattr(
        adapter,
        "finalize_recording",
        lambda *_args, **_kwargs: pytest.fail("typed stub must not finalize"),
    )

    assert adapter.run_once(args) == 0
    first_state = json.loads(args.state_path.read_text(encoding="utf-8"))
    relative = f"2026-08-12/{stub.name}"
    row = first_state["source_dispositions"][relative]
    assert row["status"] == "IGNORED_CONNECTION_STUB"
    assert relative not in first_state["finalized"]
    assert not stub.with_suffix(".mp4").exists()

    def unexpected_historical_media_read(*_args, **_kwargs):
        pytest.fail("recurring disposition validation must be metadata-only")

    monkeypatch.setattr(adapter, "sha256_file", unexpected_historical_media_read)
    monkeypatch.setattr(adapter, "probe_media", unexpected_historical_media_read)
    monkeypatch.setattr(adapter, "probe_connection_stub_video", unexpected_historical_media_read)
    assert adapter.run_once(args) == 0
    second_state = json.loads(args.state_path.read_text(encoding="utf-8"))
    assert second_state["source_dispositions"][relative] == row


def test_run_once_persists_legacy_fuse_rebind_without_manual_state_edit(
    tmp_path: Path,
    monkeypatch,
) -> None:
    args = _args(tmp_path)
    stub, successor, webhook_files, finalized = _connection_stub_fixture(
        args.record_root, monkeypatch
    )
    row = adapter.build_connection_stub_disposition(
        stub,
        record_root=args.record_root,
        webhook_files=webhook_files,
        finalized=finalized,
    )
    assert row is not None
    _resign_disposition_after_simulated_remount(row)
    relative = f"2026-08-12/{stub.name}"
    args.state_path.parent.mkdir(parents=True, exist_ok=True)
    args.state_path.write_text(
        json.dumps(
            {
                "schema_version": adapter.STATE_SCHEMA_VERSION,
                "managed_since_epoch": 0.0,
                "finalized": finalized,
                "webhook_files": webhook_files,
                "source_dispositions": {relative: row},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(adapter, "_mount_identity_for_path", _fuse_mount_identity)
    monkeypatch.setattr(
        adapter,
        "_advance_identity_rebind_task",
        lambda **_kwargs: _identity_rebind_attestations(stub, successor),
    )
    monkeypatch.setattr(
        adapter,
        "query_room_status",
        lambda *_args, **_kwargs: {
            "streaming": False,
            "recording": False,
            "danmakuConnected": False,
            "ioStats": {},
            "recordingStats": {},
        },
    )

    assert adapter.run_once(args) == 0

    migrated = json.loads(args.state_path.read_text(encoding="utf-8"))
    receipts = migrated["source_disposition_identity_rebinds"][relative]
    assert len(receipts) == 1
    assert migrated["source_dispositions"][relative] == row


def test_active_revalidation_persists_waiting_task_without_hashing(
    tmp_path: Path,
    monkeypatch,
) -> None:
    stub, _successor, webhook_files, finalized = _connection_stub_fixture(tmp_path, monkeypatch)
    row = adapter.build_connection_stub_disposition(
        stub,
        record_root=tmp_path,
        webhook_files=webhook_files,
        finalized=finalized,
    )
    assert row is not None
    _resign_disposition_after_simulated_remount(row)
    relative = f"2026-08-12/{stub.name}"
    state = {
        "source_dispositions": {relative: row},
        "webhook_files": webhook_files,
        "finalized": finalized,
    }
    monkeypatch.setattr(adapter, "_mount_identity_for_path", _fuse_mount_identity)
    monkeypatch.setattr(
        adapter,
        "_attest_regular_file",
        lambda _path: pytest.fail("active revalidation must not hash source bytes"),
    )

    with pytest.raises(adapter.AdapterError, match="waiting for recorder idle"):
        adapter.revalidate_source_dispositions(
            state,
            record_root=tmp_path,
            room_id=123456,
            allow_identity_rebind_start=False,
            identity_rebind_spool=tmp_path / "local-spool",
        )

    task = state["source_disposition_identity_rebind_tasks"][relative]
    assert task["status"] == "WAITING_FOR_IDLE"
    assert task["attempt"] == 0
    assert task["pid"] is None


def test_idle_revalidation_hashes_in_child_then_atomically_seals_receipt(
    tmp_path: Path,
    monkeypatch,
) -> None:
    stub, _successor, webhook_files, finalized = _connection_stub_fixture(tmp_path, monkeypatch)
    row = adapter.build_connection_stub_disposition(
        stub,
        record_root=tmp_path,
        webhook_files=webhook_files,
        finalized=finalized,
    )
    assert row is not None
    _resign_disposition_after_simulated_remount(row)
    relative = f"2026-08-12/{stub.name}"
    state = {
        "source_dispositions": {relative: row},
        "webhook_files": webhook_files,
        "finalized": finalized,
    }
    spool = tmp_path / "local-spool"

    def mount_identity(path: Path) -> dict[str, object] | None:
        return None if str(path).startswith(str(spool)) else _fuse_mount_identity(path)

    monkeypatch.setattr(adapter, "_mount_identity_for_path", mount_identity)
    with pytest.raises(adapter.AdapterError, match="hash is pending"):
        adapter.revalidate_source_dispositions(
            state,
            record_root=tmp_path,
            room_id=123456,
            allow_identity_rebind_start=True,
            identity_rebind_spool=spool,
        )

    task = state["source_disposition_identity_rebind_tasks"][relative]
    result_path = adapter._identity_rebind_run_dir(spool, relative) / "result.json"
    deadline = time.monotonic() + 5
    while not result_path.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert result_path.is_file()
    child = adapter._IDENTITY_REBIND_CHILDREN[int(task["pid"])]
    while child.poll() is None and time.monotonic() < deadline:
        time.sleep(0.01)
    assert child.returncode == 0

    assert adapter.revalidate_source_dispositions(
        state,
        record_root=tmp_path,
        room_id=123456,
        allow_identity_rebind_start=True,
        identity_rebind_spool=spool,
    ) == {relative}
    assert relative not in state["source_disposition_identity_rebind_tasks"]
    receipts = state["source_disposition_identity_rebinds"][relative]
    assert len(receipts) == 1
    assert receipts[0]["current_bindings"]["successor_source"]["sha256"] is None
    assert (
        receipts[0]["legacy_promotion"]["successor_source"]["historical_sha256_available"] is False
    )


def test_run_once_two_ticks_persist_pending_then_publish_sealed_rebind(
    tmp_path: Path,
    monkeypatch,
) -> None:
    args = _args(tmp_path)
    stub, _successor, webhook_files, finalized = _connection_stub_fixture(
        args.record_root, monkeypatch
    )
    row = adapter.build_connection_stub_disposition(
        stub,
        record_root=args.record_root,
        webhook_files=webhook_files,
        finalized=finalized,
    )
    assert row is not None
    _resign_disposition_after_simulated_remount(row)
    relative = f"2026-08-12/{stub.name}"
    args.state_path.parent.mkdir(parents=True, exist_ok=True)
    args.state_path.write_text(
        json.dumps(
            {
                "schema_version": adapter.STATE_SCHEMA_VERSION,
                "managed_since_epoch": 0.0,
                "finalized": finalized,
                "webhook_files": webhook_files,
                "source_dispositions": {relative: row},
            }
        ),
        encoding="utf-8",
    )
    spool = args.state_path.parent / ".source-disposition-identity-rebind"

    def mount_identity(path: Path) -> dict[str, object] | None:
        return None if str(path).startswith(str(spool)) else _fuse_mount_identity(path)

    monkeypatch.setattr(adapter, "_mount_identity_for_path", mount_identity)
    monkeypatch.setattr(
        adapter,
        "query_room_status",
        lambda *_args, **_kwargs: {
            "streaming": False,
            "recording": False,
            "danmakuConnected": False,
            "ioStats": {},
            "recordingStats": {},
        },
    )

    assert adapter.run_once(args) == 2
    pending_state = json.loads(args.state_path.read_text(encoding="utf-8"))
    task = pending_state["source_disposition_identity_rebind_tasks"][relative]
    assert task["status"] == "PENDING_HASH"
    pending_status = json.loads(args.status_path.read_text(encoding="utf-8"))
    assert "identity rebind hash is pending" in pending_status["error"]
    child = adapter._IDENTITY_REBIND_CHILDREN[int(task["pid"])]
    deadline = time.monotonic() + 5
    while child.poll() is None and time.monotonic() < deadline:
        time.sleep(0.01)
    assert child.returncode == 0

    assert adapter.run_once(args) == 0
    completed_state = json.loads(args.state_path.read_text(encoding="utf-8"))
    assert relative not in completed_state["source_disposition_identity_rebind_tasks"]
    receipts = completed_state["source_disposition_identity_rebinds"][relative]
    assert len(receipts) == 1
    assert receipts[0]["canonical_integrity"]["canonical_json_sha256"]
    completed_status = json.loads(args.status_path.read_text(encoding="utf-8"))
    assert completed_status["error"] is None


def test_identity_rebind_local_json_refuses_symlink(tmp_path: Path) -> None:
    target = tmp_path / "target.json"
    target.write_text("{}", encoding="utf-8")
    link = tmp_path / "result.json"
    link.symlink_to(target)

    with pytest.raises(adapter.AdapterError, match="cannot open"):
        adapter._local_json(link)


def test_identity_rebind_missing_result_remains_pending(tmp_path: Path) -> None:
    relative = "2026-08-12/123456_20260812-20-29-51.flv"
    run_dir = adapter._identity_rebind_run_dir(tmp_path, relative)
    run_dir.mkdir(parents=True)

    assert (
        adapter._load_identity_rebind_hash_result(
            relative=relative,
            task={"token": "not-finished"},
            spool_root=tmp_path,
        )
        is None
    )


def test_identity_rebind_linux_spool_refuses_unknown_mount(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(adapter.sys, "platform", "linux")
    monkeypatch.setattr(adapter, "_mount_identity_for_path", lambda _path: None)

    with pytest.raises(adapter.AdapterError, match="filesystem identity is unknown"):
        adapter._ensure_identity_rebind_spool(tmp_path / "spool")


def test_run_once_disposition_same_size_byte_drift_is_an_error(
    tmp_path: Path,
    monkeypatch,
) -> None:
    args = _args(tmp_path)
    stub, _successor, webhook_files, finalized = _connection_stub_fixture(
        args.record_root, monkeypatch
    )
    args.state_path.parent.mkdir(parents=True, exist_ok=True)
    args.state_path.write_text(
        json.dumps(
            {
                "schema_version": adapter.STATE_SCHEMA_VERSION,
                "managed_since_epoch": 0.0,
                "finalized": finalized,
                "webhook_files": webhook_files,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        adapter,
        "query_room_status",
        lambda *_args, **_kwargs: {
            "streaming": False,
            "recording": False,
            "danmakuConnected": False,
            "ioStats": {},
            "recordingStats": {},
        },
    )

    assert adapter.run_once(args) == 0
    original = stub.read_bytes()
    original_stat = stub.stat()
    stub.write_bytes(b"x" * len(original))
    os.utime(stub, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))

    assert adapter.run_once(args) == 2
    status = json.loads(args.status_path.read_text(encoding="utf-8"))
    assert "source disposition drift" in status["error"]
    assert not stub.with_suffix(".mp4").exists()


def test_existing_disposition_is_revalidated_even_while_room_is_active(
    tmp_path: Path,
    monkeypatch,
) -> None:
    args = _args(tmp_path)
    stub, _successor, webhook_files, finalized = _connection_stub_fixture(
        args.record_root, monkeypatch
    )
    row = adapter.build_connection_stub_disposition(
        stub,
        record_root=args.record_root,
        webhook_files=webhook_files,
        finalized=finalized,
    )
    assert row is not None
    relative = f"2026-08-12/{stub.name}"
    args.state_path.parent.mkdir(parents=True, exist_ok=True)
    args.state_path.write_text(
        json.dumps(
            {
                "schema_version": adapter.STATE_SCHEMA_VERSION,
                "managed_since_epoch": 0.0,
                "finalized": finalized,
                "webhook_files": webhook_files,
                "source_dispositions": {relative: row},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        adapter,
        "query_room_status",
        lambda *_args, **_kwargs: {
            "streaming": True,
            "recording": True,
            "danmakuConnected": True,
            "ioStats": {},
            "recordingStats": {},
        },
    )
    observed = []
    monkeypatch.setattr(
        adapter,
        "validate_connection_stub_disposition",
        lambda source, current, **_kwargs: observed.append((source, current)) or current,
    )

    assert adapter.run_once(args) == 0
    assert observed == [(stub, row)]


def test_run_once_never_finalizes_while_streaming(tmp_path: Path, monkeypatch) -> None:
    args = _args(tmp_path)
    args.record_root.mkdir(parents=True)
    monkeypatch.setattr(
        adapter,
        "query_room_status",
        lambda *_args, **_kwargs: {
            "streaming": True,
            "recording": True,
            "danmakuConnected": True,
            "ioStats": {"networkMbps": 2.0},
            "recordingStats": {"currentFileSize": 1234},
        },
    )
    monkeypatch.setattr(
        adapter,
        "finalize_recording",
        lambda *_args, **_kwargs: pytest.fail("active recording must not finalize"),
    )

    assert adapter.run_once(args) == 0
    status = json.loads(args.status_path.read_text(encoding="utf-8"))
    assert status["live_status"] == 1
    assert status["recording"] is True
    assert status["rec_total"] == 1234
    assert status["finalizing"] is False


def test_inactive_room_must_stay_inactive_before_finalization(tmp_path: Path, monkeypatch) -> None:
    args = _args(tmp_path)
    args.inactive_grace_seconds = 180
    args.record_root.mkdir(parents=True)
    monkeypatch.setattr(adapter.time, "time", lambda: 1000.0)
    monkeypatch.setattr(
        adapter,
        "query_room_status",
        lambda *_args, **_kwargs: {
            "streaming": False,
            "recording": False,
            "danmakuConnected": False,
            "ioStats": {},
            "recordingStats": {},
        },
    )
    monkeypatch.setattr(
        adapter,
        "finalize_recording",
        lambda *_args, **_kwargs: pytest.fail("inactive grace must hold finalization"),
    )

    assert adapter.run_once(args) == 0
    status = json.loads(args.status_path.read_text(encoding="utf-8"))
    state = json.loads(args.state_path.read_text(encoding="utf-8"))
    assert status["live_status"] == 0
    assert status["running_status"] == "finalizing"
    assert status["finalizing"] is True
    assert status["inactive_grace_remaining_seconds"] == 180
    assert state["inactive_since_epoch"] == 1000.0


def test_discovery_does_not_read_old_xml_without_webhook_ownership(
    tmp_path: Path,
) -> None:
    date_dir = tmp_path / "2026-07-23"
    date_dir.mkdir()
    legacy = date_dir / "123456_20260723-10-00-00.flv"
    official = date_dir / "123456_20260723-11-00-00.flv"
    legacy.write_bytes(b"legacy")
    official.write_bytes(b"official")
    official.with_suffix(".xml").write_text(_xml(), encoding="utf-8")

    found = adapter.discover_managed_flvs(
        tmp_path,
        room_id=123456,
        managed_since_epoch=max(legacy.stat().st_mtime, official.stat().st_mtime) + 60,
    )

    assert found == []


def test_discovery_accepts_old_file_from_validated_webhook_ledger(
    tmp_path: Path,
) -> None:
    date_dir = tmp_path / "2026-07-23"
    date_dir.mkdir()
    official = date_dir / "123456_20260723-11-00-00.flv"
    official.write_bytes(b"official")

    found = adapter.discover_managed_flvs(
        tmp_path,
        room_id=123456,
        managed_since_epoch=official.stat().st_mtime + 60,
        explicit_relative_paths=["2026-07-23/123456_20260723-11-00-00.flv"],
    )

    assert found == [official]


def test_idle_status_does_not_probe_historical_flv_bytes(
    tmp_path: Path,
    monkeypatch,
) -> None:
    date_dir = tmp_path / "2026-07-23"
    date_dir.mkdir()
    (date_dir / "123456_20260723-11-00-00.flv").write_bytes(b"x" * 300_000)
    monkeypatch.setattr(
        adapter,
        "probe_stream_shape",
        lambda *_args, **_kwargs: pytest.fail(
            "idle status must not read historical media through FUSE"
        ),
    )

    status = adapter.build_status(
        room_id=123456,
        room={
            "streaming": False,
            "recording": False,
            "ioStats": {},
            "recordingStats": {},
        },
        now_epoch=1.0,
        record_root=tmp_path,
    )

    assert status["latest_source"] is None


def test_closed_source_finalization_error_keeps_downstream_fail_closed(
    tmp_path: Path, monkeypatch
) -> None:
    args = _args(tmp_path)
    date_dir = args.record_root / "2026-07-23"
    date_dir.mkdir(parents=True)
    (date_dir / "123456_20260723-13-57-26.flv").write_bytes(b"closed-without-xml")
    monkeypatch.setattr(
        adapter,
        "query_room_status",
        lambda *_args, **_kwargs: {
            "streaming": False,
            "recording": False,
            "danmakuConnected": False,
            "ioStats": {},
            "recordingStats": {},
        },
    )

    assert adapter.run_once(args) == 1
    status = json.loads(args.status_path.read_text(encoding="utf-8"))
    assert status["service_reachable"] is True
    assert status["live_status"] == 0
    assert status["error"] == "1 closed recording(s) failed finalization"
    assert status["finalize_errors"]
    assert not (date_dir / "123456_20260723-13-57-26.mp4").exists()


def test_run_once_finalizes_only_with_matching_fileclosed_evidence(
    tmp_path: Path, monkeypatch
) -> None:
    args = _args(tmp_path)
    date_dir = args.record_root / "2026-07-23"
    date_dir.mkdir(parents=True)
    source = date_dir / "123456_20260723-13-57-26.flv"
    source.write_bytes(b"closed-source")
    source.with_suffix(".xml").write_text(_xml(), encoding="utf-8")
    event = {
        "EventType": "FileClosed",
        "EventTimestamp": "2026-07-23T14:27:26+08:00",
        "EventId": "33333333-3333-4333-8333-333333333333",
        "EventData": {
            "RoomId": 123456,
            "SessionId": "session-b",
            "RelativePath": ("Videos/123456/2026-07-23/123456_20260723-13-57-26.flv"),
            "FileSize": source.stat().st_size,
            "Duration": 1800.0,
            "FileOpenTime": "2026-07-23T13:57:26+08:00",
            "FileCloseTime": "2026-07-23T14:27:26+08:00",
        },
    }
    adapter.append_webhook_event(args.webhook_journal, event, room_id=123456)
    monkeypatch.setattr(
        adapter,
        "query_room_status",
        lambda *_args, **_kwargs: {
            "streaming": False,
            "recording": False,
            "danmakuConnected": False,
            "ioStats": {},
            "recordingStats": {},
        },
    )

    def fake_finalize(path: Path, **_kwargs):
        path.with_suffix(".mp4").write_bytes(b"validated-mp4")
        return {
            "source": str(path),
            "target": str(path.with_suffix(".mp4")),
            "status": "finalized",
            "event_count": 4,
            "media": {"duration_seconds": 1.0},
            "target_sha256": adapter.sha256_file(path.with_suffix(".mp4")),
            "publish_method": "test",
        }

    monkeypatch.setattr(adapter, "finalize_recording", fake_finalize)

    assert adapter.run_once(args) == 0
    state = json.loads(args.state_path.read_text(encoding="utf-8"))
    relative = "2026-07-23/123456_20260723-13-57-26.flv"
    assert state["webhook_files"][relative]["status"] == "CLOSED"
    assert state["finalized"][relative]["target_sha256"]


@pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg/ffprobe required",
)
def test_finalize_real_flv_is_atomic_idempotent_and_preserves_source(tmp_path: Path) -> None:
    source = tmp_path / "123456_20260723-13-57-26.flv"
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
    source.with_suffix(".xml").write_text(_xml(), encoding="utf-8")

    first = adapter.finalize_recording(source)
    target = source.with_suffix(".mp4")
    first_bytes = target.read_bytes()

    assert first["status"] == "finalized"
    assert source.is_file()
    assert target.is_file()
    assert not target.with_name(target.name + ".tmp").exists()
    assert source.with_suffix(".jsonl").is_file()
    meta = json.loads(source.with_suffix(".meta.json").read_text(encoding="utf-8"))
    assert meta["description"]["RecordStartTime"].startswith("2026-07-23T13:57:26")

    source_stat = source.stat()
    second = adapter.finalize_recording(
        source,
        existing_ledger={
            "source_size": source_stat.st_size,
            "source_mtime_ns": source_stat.st_mtime_ns,
            "target_sha256": first["target_sha256"],
        },
    )

    assert second["status"] == "already_finalized"
    assert target.read_bytes() == first_bytes
