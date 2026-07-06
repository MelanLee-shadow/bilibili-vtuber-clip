"""Unit tests for the unattended runner's pure helpers (first-real-run lessons)."""
import json

from scripts.free_session_autoslice import (
    MAX_SONGS_PER_DATE,
    MAX_TALK_PICKS,
    last_json_block,
    prioritize,
    safe_name,
)


def test_last_json_block_parses_nested_produce_summary():
    """The produce log summary contains nested objects — a non-greedy regex
    failed on it (first run's summary showed 标题=(见log) everywhere)."""
    tail = 'noise\n{"a": 1}\nmore\n{"candidate_id": "x", "timing_qa": {"dropped": 0, "retimed": 3}, "title": "真标题"}\n'
    obj = last_json_block(tail)
    assert obj["title"] == "真标题"
    assert obj["timing_qa"]["retimed"] == 3


def test_last_json_block_empty_on_garbage():
    assert last_json_block("no json here }{ broken") == {}


def test_safe_name_sanitizes_and_falls_back():
    assert safe_name("百合是工作？/她当场*不买书", "cid") == "百合是工作？她当场不买书"
    assert safe_name("", "cid") == "cid"
    assert len(safe_name("很长" * 40, "cid")) <= 18


def test_prioritize_caps_talk_and_songs_with_reasons():
    state = {
        "picks": [], "songs": [],
        "pending_talk": [
            {"segment_path": f"/rec/seg{i % 2}.mp4", "start_ms": i * 1000, "end_ms": i * 1000 + 30_000,
             "hook": f"hook{i}", "confidence": 0.9, "cid": f"auto_{i}"}
            for i in range(MAX_TALK_PICKS + 3)
        ],
        "pending_song": [
            {"segment_path": "/rec/seg0.mp4", "anchor_start_ms": i * 10_000, "anchor_end_ms": i * 10_000 + 60_000,
             "danmaku": i * 10, "cid": f"song_{i}", "hook": "", "preview": ""}
            for i in range(MAX_SONGS_PER_DATE + 2)
        ],
    }
    prioritize(state)
    assert len(state["pending_talk"]) == MAX_TALK_PICKS
    assert len(state["not_selected"]) == 3
    assert len(state["pending_song"]) == MAX_SONGS_PER_DATE
    # 弹幕最高的两个被保留（降序）
    assert [s["danmaku"] for s in state["pending_song"]] == [30, 20]
    assert len(state["song_backlog"]) == 2
    assert "上限" in state["song_backlog"][0]
