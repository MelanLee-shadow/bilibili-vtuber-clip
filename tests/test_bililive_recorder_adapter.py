from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import subprocess

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
    path = (
        Path(__file__).resolve().parents[1]
        / "ops/recording/bililive_recorder.config.v3.json"
    )
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


def test_inactive_room_must_stay_inactive_before_finalization(
    tmp_path: Path, monkeypatch
) -> None:
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
        explicit_relative_paths=[
            "2026-07-23/123456_20260723-11-00-00.flv"
        ],
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
            "RelativePath": (
                "Videos/123456/2026-07-23/"
                "123456_20260723-13-57-26.flv"
            ),
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
