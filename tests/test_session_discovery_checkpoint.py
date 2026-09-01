from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

import scripts.session_autoslice as runner
from src.autoslice import (
    historical_failed_talk_scope,
    semantic_evidence_scorecard_refresh,
    session_discovery,
)
from src.autoslice.visual_song_discovery import (
    VisualSongCandidate,
    VisualSongDiscoveryResult,
)


def _candidate(candidate_id: str, preview: str) -> SimpleNamespace:
    return SimpleNamespace(
        anchor=SimpleNamespace(
            candidate_id=f"semantic-{candidate_id}",
            anchor_start_ms=100,
            anchor_end_ms=900,
        ),
        boundary=SimpleNamespace(
            resolved_start_ms=100,
            resolved_end_ms=900,
        ),
        content_type_hint="talk",
        text_preview=preview,
    )


def _ready_visual(title: str) -> VisualSongDiscoveryResult:
    return VisualSongDiscoveryResult(
        status="READY",
        candidates=(
            VisualSongCandidate(
                title,
                100,
                900,
                {"list_index": 1},
                0.9,
            ),
        ),
        cache_path="cache.json",
        content_fingerprint="a" * 64,
        config_sha256="b" * 64,
    )


def _install_fakes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    crash_on_second: bool = True,
) -> tuple[Path, Path, list[Path]]:
    first = tmp_path / "22966160_20260831-00-00-01.mp4"
    second = tmp_path / "22966160_20260831-00-00-02.mp4"
    first.write_bytes(b"first")
    second.write_bytes(b"second")
    segments = [first, second]
    srts = {
        first: tmp_path / "first.srt",
        second: tmp_path / "second.srt",
    }
    for path in srts.values():
        path.write_text(
            "1\n00:00:00,100 --> 00:00:00,900\ntext\n",
            encoding="utf-8",
        )

    monkeypatch.setattr(runner, "MIN_SEGMENT_BYTES", 1)
    monkeypatch.setattr(runner, "list_segments", lambda _date: segments)
    monkeypatch.setattr(
        runner,
        "bcut_transcribe",
        lambda segment, _date: srts[segment],
    )
    monkeypatch.setattr(runner, "find_danmaku_xml", lambda _segment: None)
    monkeypatch.setattr(runner, "find_chat_jsonl", lambda _segment: None)
    monkeypatch.setattr(
        runner,
        "resolve_structured_chat_binding",
        lambda *_args, **_kwargs: {
            "structured_chat_required": False,
            "chat_binding_status": "NOT_REGISTERED",
        },
    )
    monkeypatch.setattr(runner, "danmaku_hints", lambda _xml: None)
    monkeypatch.setattr(runner, "ffprobe_ms", lambda _segment: 1_000)
    monkeypatch.setattr(
        runner,
        "recording_session_id",
        lambda segment, _date: f"session-{segment.stem}",
    )
    monkeypatch.setattr(
        runner,
        "session_relation_for_segment",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(runner, "log", lambda _message: None)
    monkeypatch.setattr(
        runner,
        "recall_candidates",
        lambda srt, *_args: (
            [_candidate("first", "first candidate")]
            if srt == srts[first]
            else [_candidate("second", "second candidate")],
            "fake",
            {},
        ),
    )

    def discover_visual_songs(segment, *_args, **_kwargs):
        if segment == second and crash_on_second:
            raise RuntimeError("simulated second segment crash")
        return _ready_visual("第一首歌" if segment == first else "第二首歌")

    monkeypatch.setattr(runner, "discover_visual_songs", discover_visual_songs)
    monkeypatch.setattr(runner, "_remember_song_quarantine_interval", lambda *_args: None)
    monkeypatch.setattr(
        session_discovery,
        "resolve_segment_scene_context",
        lambda segment, **_kwargs: {"scene": segment.stem},
    )
    monkeypatch.setattr(runner, "BASE", tmp_path / "base")
    return first, second, segments


def test_discovery_checkpoints_deep_copy_before_next_segment_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first, second, _segments = _install_fakes(tmp_path, monkeypatch)
    state: dict = {"status": "processing"}
    durable_snapshots: list[dict] = []

    def persist_state() -> None:
        durable_snapshots.append(deepcopy(state))
        state.clear()
        state.update(deepcopy(durable_snapshots[-1]))

    with pytest.raises(RuntimeError, match="simulated second segment crash"):
        session_discovery.discover_segments(
            "2026-08-31",
            state,
            persist_state=persist_state,
        )

    assert len(durable_snapshots) == 1
    durable = durable_snapshots[0]
    assert durable["segments_done"] == [first.stem]
    first_tag = "".join(character for character in first.stem if character.isdigit())[-6:]
    second_tag = "".join(character for character in second.stem if character.isdigit())[-6:]
    assert [row["cid"] for row in durable["pending_talk"]] == [
        f"auto_{first_tag}_0_0"
    ]
    assert durable["visual_song_inventory"].keys() == {first.stem}
    assert durable["visual_song_inventory"][first.stem]["candidates"][0]["song_title"] == "第一首歌"
    assert second.stem not in durable["segment_durations_ms"]
    assert all(row["cid"] != f"auto_{second_tag}_0_0" for row in durable["pending_talk"])


def test_discovery_checkpoint_failure_stops_before_next_segment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _first, second, segments = _install_fakes(
        tmp_path,
        monkeypatch,
        crash_on_second=False,
    )
    state: dict = {}
    seen: list[dict] = []

    def persist_state() -> None:
        seen.append(deepcopy(state))
        raise OSError("checkpoint write failed")

    with pytest.raises(OSError, match="checkpoint write failed"):
        session_discovery.discover_segments(
            "2026-08-31",
            state,
            persist_state=persist_state,
        )

    assert len(seen) == 1
    assert seen[0]["segments_done"] == [segments[0].stem]
    assert second.stem not in state.get("segments_done", [])
    second_tag = "".join(character for character in second.stem if character.isdigit())[-6:]
    assert all(row["cid"] != f"auto_{second_tag}_0_0" for row in state["pending_talk"])


def test_historical_discovery_forwards_optional_persist_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def callback() -> None:
        return None

    captured: dict[str, object] = {}

    def fake_discover(date, state, **kwargs):
        captured.update(date=date, state=state, **kwargs)

    monkeypatch.setattr(historical_failed_talk_scope._runner, "discover_segments", fake_discover)
    state: dict = {}

    historical_failed_talk_scope.discover(
        "2026-08-31",
        state,
        None,
        persist_state=callback,
    )

    assert captured == {
        "date": "2026-08-31",
        "state": state,
        "persist_state": callback,
    }


def test_visual_retry_due_detection_treats_legacy_as_one_attempt_and_exhausts():
    stem = "22966160_20260831-00-00-01"

    legacy = {
        "segments_done": [stem],
        "visual_song_inventory": {stem: {"status": "FAILED", "candidates": []}},
    }
    assert session_discovery.visual_song_retry_due(legacy)

    current = {
        "segments_done": [stem],
        "visual_song_inventory": {
            stem: {
                "status": "FAILED",
                "visual_retry": {
                    "attempts": 1,
                    "max_attempts": 2,
                    "status": "DUE",
                    "due": True,
                    "exhausted": False,
                },
            }
        },
    }
    assert session_discovery.visual_song_retry_due(current)
    current["visual_song_inventory"][stem]["visual_retry"] = {
        "attempts": 2,
        "max_attempts": 2,
        "status": "EXHAUSTED",
        "due": False,
        "exhausted": True,
    }
    assert not session_discovery.visual_song_retry_due(current)
    current["segments_done"] = []
    assert not session_discovery.visual_song_retry_due(current)


def test_done_failed_visual_retry_is_visual_only_and_deduplicates_pending_rows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    segment = tmp_path / "22966160_20260831-00-00-01.mp4"
    segment.write_bytes(b"media")
    other = tmp_path / "22966160_20260831-00-00-02.mp4"
    other.write_bytes(b"other")
    monkeypatch.setattr(runner, "list_segments", lambda _date: [segment])
    monkeypatch.setattr(runner, "ffprobe_ms", lambda _segment: 1_000)
    monkeypatch.setattr(runner, "find_danmaku_xml", lambda _segment: None)
    monkeypatch.setattr(runner, "danmaku_count_in", lambda *_args: 0)
    monkeypatch.setattr(runner, "recording_session_id", lambda *_args: "session-1")
    monkeypatch.setattr(runner, "_remember_song_quarantine_interval", lambda *_args: None)
    monkeypatch.setattr(runner, "BASE", tmp_path / "base")
    monkeypatch.setattr(runner, "bcut_transcribe", lambda *_args: pytest.fail("BCUT rerun"))
    monkeypatch.setattr(runner, "recall_candidates", lambda *_args: pytest.fail("semantic recall rerun"))

    existing = {
        "cid": "song_existing",
        "segment_path": str(segment),
        "anchor_start_ms": 100,
        "anchor_end_ms": 900,
        "lane": "semantic_recall",
        "title_hint": "旧标题",
        "session_id": "session-1",
    }
    other_row = {
        "cid": "song_other",
        "segment_path": str(other),
        "anchor_start_ms": 100,
        "anchor_end_ms": 900,
        "lane": "semantic_recall",
    }
    state = {
        "segments_done": [segment.stem],
        "visual_song_inventory": {
            segment.stem: {"status": "FAILED", "candidates": []}
        },
        "segment_durations_ms": {segment.stem: 1_000},
        "segment_sessions": {segment.stem: "session-1"},
        "visual_song_seen_entries": [],
        "pending_song": [existing, other_row],
    }
    calls: list[Path] = []

    def discover_visual(segment_path, *_args, **_kwargs):
        calls.append(segment_path)
        return _ready_visual("晴る")

    monkeypatch.setattr(runner, "discover_visual_songs", discover_visual)
    durable: list[dict] = []

    def persist_state() -> None:
        durable.append(deepcopy(state))
        state.clear()
        state.update(deepcopy(durable[-1]))

    session_discovery.discover_segments(
        "2026-08-31", state, persist_state=persist_state
    )

    assert calls == [segment]
    assert len(durable) == 1
    assert [row["cid"] for row in state["pending_song"]] == [
        "song_existing",
        "song_other",
    ]
    merged = state["pending_song"][0]
    assert merged["title_hint"] == "晴る"
    assert merged["visual_song_matched_to_asr"] is True
    assert state["visual_song_inventory"][segment.stem]["status"] == "READY"
    retry = state["visual_song_inventory"][segment.stem]["visual_retry"]
    assert retry["attempts"] == 2 and retry["status"] == "SUCCEEDED"
    assert state["visual_song_seen_entries"] == ["list:1:晴る"]

    session_discovery.discover_segments("2026-08-31", state)
    assert calls == [segment]


@pytest.mark.parametrize(
    "collection", ["song_backlog", "song_selection_backlog", "songs"]
)
def test_visual_retry_enriches_song_collection_in_place_and_appends_unmatched_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    collection: str,
) -> None:
    segment = tmp_path / "22966160_20260831-00-00-01.mp4"
    segment.write_bytes(b"media")
    monkeypatch.setattr(runner, "list_segments", lambda _date: [segment])
    monkeypatch.setattr(runner, "ffprobe_ms", lambda _segment: 1_000)
    monkeypatch.setattr(runner, "find_danmaku_xml", lambda _segment: None)
    monkeypatch.setattr(runner, "danmaku_count_in", lambda *_args: 0)
    monkeypatch.setattr(runner, "recording_session_id", lambda *_args: "session-1")
    monkeypatch.setattr(runner, "_remember_song_quarantine_interval", lambda *_args: None)
    monkeypatch.setattr(runner, "BASE", tmp_path / "base")
    monkeypatch.setattr(runner, "bcut_transcribe", lambda *_args: pytest.fail("BCUT rerun"))
    monkeypatch.setattr(
        runner, "recall_candidates", lambda *_args: pytest.fail("semantic recall rerun")
    )
    semantic_row = {
        "cid": "song_semantic",
        "segment_path": str(segment),
        "anchor_start_ms": 100,
        "anchor_end_ms": 900,
        "lane": "semantic_recall",
        "title_hint": "旧标题",
        "session_id": "session-1",
    }
    visual_result = VisualSongDiscoveryResult(
        status="READY",
        candidates=(
            VisualSongCandidate("晴る", 100, 900, {"list_index": 1}, 0.9),
            VisualSongCandidate("未匹配", 920, 1_000, {"list_index": 2}, 0.8),
        ),
        cache_path="cache.json",
        content_fingerprint="a" * 64,
        config_sha256="b" * 64,
    )
    state = {
        "segments_done": [segment.stem],
        "visual_song_inventory": {segment.stem: {"status": "FAILED"}},
        "segment_durations_ms": {segment.stem: 1_000},
        "segment_sessions": {segment.stem: "session-1"},
        "visual_song_seen_entries": [],
        "pending_song": [],
        collection: [semantic_row],
    }
    calls: list[Path] = []

    def discover_visual(segment_path, *_args, **_kwargs):
        calls.append(segment_path)
        return visual_result

    monkeypatch.setattr(runner, "discover_visual_songs", discover_visual)

    session_discovery.discover_segments("2026-08-31", state)

    assert calls == [segment]
    enriched = state[collection][0]
    assert enriched["cid"] == "song_semantic"
    assert enriched["title_hint"] == "晴る"
    assert enriched["visual_song_matched_to_asr"] is True
    assert len(state["pending_song"]) == 1
    assert state["pending_song"][0]["title_hint"] == "未匹配"
    assert state["pending_song"][0]["lane"] == "visual_song_inventory"

    # The successful retry is terminal and must not append the unmatched row
    # again on a later discovery tick.
    session_discovery.discover_segments("2026-08-31", state)
    assert calls == [segment]
    assert len(state["pending_song"]) == 1


def test_failed_visual_retry_checkpoints_exhaustion_and_does_not_repeat(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    segment = tmp_path / "22966160_20260831-00-00-01.mp4"
    segment.write_bytes(b"media")
    monkeypatch.setattr(runner, "list_segments", lambda _date: [segment])
    monkeypatch.setattr(runner, "ffprobe_ms", lambda _segment: 1_000)
    monkeypatch.setattr(runner, "BASE", tmp_path / "base")
    monkeypatch.setattr(runner, "discover_visual_songs", lambda *_args, **_kwargs: pytest.fail("one retry only"))
    state = {
        "segments_done": [segment.stem],
        "visual_song_inventory": {segment.stem: {"status": "FAILED"}},
    }
    checkpoints: list[dict] = []

    def persist_state() -> None:
        checkpoints.append(deepcopy(state))
        state.clear()
        state.update(deepcopy(checkpoints[-1]))

    # The provider raises; the visual lane must still checkpoint an exhausted
    # retry instead of leaving the date alive for every later tick.
    monkeypatch.setattr(
        runner,
        "discover_visual_songs",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("provider secret")),
    )
    session_discovery.discover_segments("2026-08-31", state, persist_state=persist_state)
    assert len(checkpoints) == 1
    retry = state["visual_song_inventory"][segment.stem]["visual_retry"]
    assert retry["attempts"] == 2 and retry["status"] == "EXHAUSTED"
    assert "provider secret" not in state["visual_song_inventory"][segment.stem]["error"]
    assert not session_discovery.visual_song_retry_due(state)

    session_discovery.discover_segments("2026-08-31", state)
    assert len(checkpoints) == 1


def test_visual_retry_wakes_only_unscope_work_flags(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    segment = tmp_path / "22966160_20260831-00-00-01.mp4"
    segment.write_bytes(b"media")
    monkeypatch.setattr(
        semantic_evidence_scorecard_refresh,
        "_runner",
        SimpleNamespace(
            list_segments=lambda _date: [segment],
            backlog_has_eligible_session_work=lambda _state: False,
            cover_repair_needed=lambda *_args: False,
        ),
    )
    monkeypatch.setattr(
        semantic_evidence_scorecard_refresh,
        "operator_scoped_chat_refresh_needed",
        lambda *_args: False,
    )
    state = {
        "segments_done": [segment.stem],
        "visual_song_inventory": {segment.stem: {"status": "FAILED"}},
    }

    assert semantic_evidence_scorecard_refresh.runner_date_work_flags(
        "2026-08-31", state, automatic_maintenance=False
    ) == (False, True, False)
    # A frozen Talk scope must not wake Song/visual discovery.
    assert semantic_evidence_scorecard_refresh.runner_date_work_flags(
        "2026-08-31",
        state,
        automatic_maintenance=False,
        talk_candidate_ids=("talk-only",),
    ) == (False, False, False)
