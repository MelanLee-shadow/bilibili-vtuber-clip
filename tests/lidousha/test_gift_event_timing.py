"""Regression coverage for recorder-native gift clocks and masked senders."""

import hashlib
import json

import pytest

from src.autoslice.chat_evidence import load_chat_jsonl
from src.autoslice.chat_event_timing import send_time_ms
from src.autoslice.clip_context import select_context_chat


@pytest.mark.parametrize("command", ["SEND_GIFT", "COMBO_SEND"])
@pytest.mark.parametrize("timestamp", [1786769318, 1786769318000])
def test_gift_payload_timestamp_is_not_lost_without_recorder_send_time(
    tmp_path, command, timestamp
):
    # C13's real payload has only data.timestamp; uname is deliberately masked.
    payload = {
        "cmd": command,
        "data": {"timestamp": timestamp, "giftName": "千纸鹤", "uname": "我***", "uid": 0},
    }
    raw = (json.dumps(payload, ensure_ascii=False) + "\n").encode()
    items = load_chat_jsonl(
        tmp_path / "source.jsonl",
        recording_start_ms=1786769318000 - 111040,
        source_bytes=raw,
    )

    assert len(items) == 1
    item = items[0]
    assert (item.kind, item.text, item.sender, item.offset_ms) == (
        "gift", "千纸鹤", "我***", 111040
    )
    assert item.source_sha256 == hashlib.sha256(raw).hexdigest()
    assert item.source_event_id == ""  # Do not invent a source identity.
    assert select_context_chat(items) == items


@pytest.mark.parametrize("command", ["SEND_GIFT", "COMBO_SEND"])
def test_gift_event_clock_precedes_late_recorder_ingestion(command):
    payload = {
        "send_time": 1786769350,
        "data": {"timestamp": 1786769318, "send_time": 1786769340},
    }
    assert send_time_ms(payload, command) == 1786769318000


@pytest.mark.parametrize(
    "invalid_timestamp",
    [None, True, "1786769318", "unknown", {}, [], float("nan"), float("inf"), -float("inf")],
)
def test_invalid_gift_clock_retains_existing_send_time_fallback(invalid_timestamp):
    payload = {"send_time": 1786769350, "data": {"timestamp": invalid_timestamp}}
    assert send_time_ms(payload, "SEND_GIFT") == 1786769350000


def test_gift_without_any_valid_clock_is_not_fabricated(tmp_path):
    raw = json.dumps({
        "cmd": "SEND_GIFT", "data": {"giftName": "千纸鹤", "uname": "我***"}
    }).encode()
    assert load_chat_jsonl(tmp_path / "source.jsonl", source_bytes=raw) == []


def test_unknown_event_does_not_acquire_a_gift_timestamp():
    assert send_time_ms({"data": {"timestamp": 1786769318}}, "OTHER_EVENT") is None
