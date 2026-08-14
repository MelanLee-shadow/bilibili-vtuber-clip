"""Frozen Talk ticks must never migrate, mutate, or dispatch unnamed queues."""

from __future__ import annotations

import copy
import json

import pytest

import scripts.free_session_autoslice as runner
from src.autoslice import candidate_selection, historical_failed_talk_scope
from src.autoslice.talk_quota_policy import TalkQuotaPolicy


DATE = "2026-08-08"
TARGET_806 = "auto_213135_806_1068"
TARGET_1576 = "auto_210131_1576_1802"
NON_TARGET_550 = "auto_230125_550_701"
NON_TARGET_714 = "auto_230125_714_806"


def _candidate(candidate_id: str, *, selected: bool = False, marker: str = "") -> dict:
    row = {
        "cid": candidate_id,
        "segment_path": f"/recordings/{candidate_id}.mp4",
        "start_ms": 10_000,
        "end_ms": 20_000,
        "confidence": 0.99,
        "hook": candidate_id,
        "session_id": "live-20260808T210000+0800",
        "opaque_marker": {"value": marker, "order": [3, 1, 2]},
    }
    if selected:
        row["selected_repair"] = True
    return row


def _grant(candidate_ids: list[str]) -> dict:
    return {
        "schema_version": "operator-processing-scope-grant.v1",
        "grant_id": "frozen-talk-isolation-canary",
        "recording_date": DATE,
        "reason": "只处理点名 Talk，并保持其余 Talk 队列逐字节不变。",
        "candidate_ids": candidate_ids,
        "user_authorization": {
            "quote": "806，1576内容没问题可以发。",
            "timestamp": "2026-08-13T18:00:00Z",
        },
        "expires_at": "2099-08-14T06:00:00Z",
    }


def _canonical(row: dict) -> bytes:
    return json.dumps(
        row,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")


@pytest.fixture()
def frozen_selection_guards(monkeypatch: pytest.MonkeyPatch) -> None:
    policy = TalkQuotaPolicy(
        kind="talk",
        scope_key="talk:live-20260808T210000+0800",
        cap=5,
        extra_slot_min_score=None,
        recording_date=DATE,
    )
    monkeypatch.setattr(candidate_selection, "_talk_quota_policy", lambda _item: policy)

    def assert_only_targets(state: dict) -> None:
        visible = [
            row.get("cid") or row.get("candidate_id")
            for key in ("pending_talk", "talk_backlog")
            for row in state.get(key, [])
            if isinstance(row, dict)
        ]
        assert set(visible) <= {TARGET_806, TARGET_1576}

    monkeypatch.setattr(
        candidate_selection._runner,
        "exclude_session_edge_bgm_candidates",
        assert_only_targets,
    )
    monkeypatch.setattr(
        candidate_selection._runner,
        "quarantine_overlapping_talk_candidates",
        assert_only_targets,
    )
    def assert_scoped_hold(state: dict, *, candidate_ids) -> None:
        visible = {
            row.get("cid") or row.get("candidate_id")
            for key in ("pending_talk", "talk_backlog")
            for row in state.get(key, [])
            if isinstance(row, dict)
        }
        assert {NON_TARGET_550, NON_TARGET_714} <= visible
        assert set(candidate_ids) <= {TARGET_806, TARGET_1576}

    monkeypatch.setattr(
        candidate_selection,
        "hold_published_topic_collision_reviews",
        assert_scoped_hold,
    )
    monkeypatch.setattr(
        candidate_selection._runner,
        "refill_songs",
        lambda _state: pytest.fail("frozen Talk scope entered Song refill"),
    )


def test_frozen_prioritize_preserves_non_target_collection_order_and_bytes(
    frozen_selection_guards: None,
) -> None:
    pending_non_targets = [
        _candidate(NON_TARGET_550, marker="pending-first"),
        _candidate("auto_pending_second", marker="pending-second"),
    ]
    backlog_non_targets = [
        _candidate(NON_TARGET_714, marker="backlog-first"),
        _candidate("auto_backlog_second", marker="backlog-second"),
    ]
    song_collections = {
        "songs": [{"candidate_id": "song_done", "opaque": [2, 1]}],
        "pending_song": [{"cid": "song_pending", "opaque": {"z": 1}}],
        "song_backlog": [{"cid": "song_backlog", "opaque": ["b", "a"]}],
        "song_selection_backlog": [
            {"cid": "song_selection", "opaque": {"keep": True}}
        ],
    }
    state = {
        "picks": [],
        **copy.deepcopy(song_collections),
        "pending_talk": [
            _candidate(TARGET_806, selected=True),
            *copy.deepcopy(pending_non_targets),
        ],
        "talk_backlog": [
            copy.deepcopy(backlog_non_targets[0]),
            _candidate(TARGET_1576, selected=True),
            copy.deepcopy(backlog_non_targets[1]),
        ],
        "operator_processing_scope": _grant([TARGET_806, TARGET_1576]),
    }
    pending_bytes = [_canonical(row) for row in pending_non_targets]
    backlog_bytes = [_canonical(row) for row in backlog_non_targets]
    frozen_song_bytes = copy.deepcopy(song_collections)

    candidate_selection.prioritize(
        state,
        frozen_talk_candidate_ids=(TARGET_806, TARGET_1576),
        allow_song_work=False,
    )

    restored_pending = [
        row
        for row in state["pending_talk"]
        if row["cid"] not in {TARGET_806, TARGET_1576}
    ]
    restored_backlog = [
        row
        for row in state["talk_backlog"]
        if row["cid"] not in {TARGET_806, TARGET_1576}
    ]
    assert [row["cid"] for row in restored_pending] == [
        NON_TARGET_550,
        "auto_pending_second",
    ]
    assert [row["cid"] for row in restored_backlog] == [
        NON_TARGET_714,
        "auto_backlog_second",
    ]
    assert [_canonical(row) for row in restored_pending] == pending_bytes
    assert [_canonical(row) for row in restored_backlog] == backlog_bytes
    assert {
        key: state[key]
        for key in frozen_song_bytes
    } == frozen_song_bytes


def test_process_date_routes_metadata_and_dispatch_only_to_frozen_target(
    frozen_selection_guards: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = _candidate(TARGET_806, selected=True, marker="target")
    pending_other = _candidate(NON_TARGET_550, marker="must-remain-pending")
    backlog_other = _candidate(NON_TARGET_714, marker="must-remain-backlog")
    state = {
        "status": "processing",
        "run_mode": "PRODUCTION",
        "upload_allowed": False,
        "picks": [],
        "songs": [],
        "pending_talk": [target, copy.deepcopy(pending_other)],
        "talk_backlog": [copy.deepcopy(backlog_other)],
        "operator_processing_scope": _grant([TARGET_806]),
    }
    pending_bytes = _canonical(pending_other)
    backlog_bytes = _canonical(backlog_other)
    routed: list[list[str]] = []
    dispatched: list[list[str]] = []
    reprioritized: list[tuple[str, ...] | None] = []

    monkeypatch.setattr(runner, "read_state", lambda _date: state)
    monkeypatch.setattr(runner, "write_state", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(runner, "write_reports", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(runner, "runtime_health_error", lambda: None)
    monkeypatch.setattr(runner, "recover_finalized_legacy_hls", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(
        runner,
        "audit_finalized_recording_inventory",
        lambda *_args, **_kwargs: {"can_select": True, "issues": []},
    )
    monkeypatch.setattr(runner, "annotate_state_sessions", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(
        historical_failed_talk_scope,
        "maintain",
        lambda *_args, **_kwargs: (0, 0, 0, 0, False),
    )
    monkeypatch.setattr(
        historical_failed_talk_scope,
        "work_flags",
        lambda *_args, **_kwargs: (True, True, False),
    )
    monkeypatch.setattr(runner, "cpa_healthy", lambda: True)
    monkeypatch.setattr(runner, "session_sealed", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(
        runner.semantic_chat_refresh,
        "refresh_operator_scoped_chat_scorecards",
        lambda *_args, **_kwargs: 0,
    )
    original_reprioritize = historical_failed_talk_scope.reprioritize

    def reprioritize(value: dict, candidate_ids: tuple[str, ...] | None) -> None:
        reprioritized.append(candidate_ids)
        original_reprioritize(value, candidate_ids)

    monkeypatch.setattr(
        historical_failed_talk_scope,
        "reprioritize",
        reprioritize,
    )

    def prepare(_date: str, items: list[dict], **_kwargs) -> None:
        ids = [item["cid"] for item in items]
        assert ids == [TARGET_806]
        routed.append(ids)
        for item in items:
            item["speaker_route_seen"] = True

    def split(items: list[dict]) -> tuple[list[dict], list[dict]]:
        assert [item["cid"] for item in items] == [TARGET_806]
        return items, []

    def produce(_date: str, items: list[dict], produce_fn) -> list[dict]:
        assert produce_fn is runner.produce_talk
        ids = [item["cid"] for item in items]
        assert ids == [TARGET_806]
        dispatched.append(ids)
        return [{"candidate_id": TARGET_806, "status": "candidate_rejected"}]

    monkeypatch.setattr(runner, "prepare_speaker_routing", prepare)
    monkeypatch.setattr(runner, "collect_song_name_candidates", lambda *_args: ["测试歌名"])
    monkeypatch.setattr(runner, "split_produce_blocked_talk_items", split)
    monkeypatch.setattr(runner, "produce_batch", produce)
    monkeypatch.setattr(runner, "apply_talk_backfill_rejection_policy", lambda *_a, **_k: True)
    monkeypatch.setattr(runner, "cover_repair_needed", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(
        runner,
        "_project_terminal_batch_state",
        lambda value, **_kwargs: {
            "picks": value["picks"],
            "songs": value["songs"],
            "delivered_talk": [],
            "repaired": [],
            "delivered_songs": [],
            "blocked_songs": [],
            "rejected_songs": [],
            "failures": value["picks"],
            "retry_epoch": None,
        },
    )
    monkeypatch.setattr(runner, "queue_collab_evidence_capture", lambda *_a, **_k: None)
    monkeypatch.setattr(runner, "log", lambda *_args, **_kwargs: None)

    runner.process_date(DATE)

    assert routed == [[TARGET_806]]
    assert dispatched == [[TARGET_806]]
    assert reprioritized == [(TARGET_806,)]
    assert [row["cid"] for row in state["pending_talk"]] == [NON_TARGET_550]
    assert [row["cid"] for row in state["talk_backlog"]] == [NON_TARGET_714]
    assert _canonical(state["pending_talk"][0]) == pending_bytes
    assert _canonical(state["talk_backlog"][0]) == backlog_bytes
    assert "speaker_route_seen" not in state["pending_talk"][0]
    assert "song_name_candidates" not in state["pending_talk"][0]
