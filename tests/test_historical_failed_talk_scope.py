"""Historical v2 failed-pick scope is a Talk-only lane capability."""

from __future__ import annotations

import copy
from pathlib import Path

import pytest

import scripts.free_session_autoslice as runner
from src.autoslice import candidate_selection
from src.autoslice import semantic_evidence_scorecard_refresh as semantic_chat_refresh
from src.autoslice.operator_processing_scope import (
    GRANT_SCHEMA,
    SPEAKER_HOLD_RECOVERY_GRANT_SCHEMA,
    SPEAKER_HOLD_RECOVERY_INTENT,
    operator_scope_admission,
    operator_talk_scope,
)


DATE = "2026-08-09"
TARGET = "auto_target"
OTHER = "auto_other"
SONG = {"candidate_id": "song_pending", "status": "pending"}


def _grant() -> dict:
    return {
        "schema_version": "operator-processing-scope-grant.v2",
        "grant_id": "review-only-talk-target",
        "recording_date": DATE,
        "reason": "只恢复点名 Talk 供人工复核，不处理或发布同日 Song。",
        "candidate_ids": [TARGET],
        "user_authorization": {
            "quote": "talk这三个看起来都可以；任何时候优先修复流水线",
            "timestamp": "2026-08-13T06:00:00Z",
        },
        "expires_at": "2099-08-14T06:00:00Z",
        "intent": "RECOVER_NAMED_FAILED_PICKS",
    }


def _failed(candidate_id: str) -> dict:
    return {
        "candidate_id": candidate_id,
        "status": "failed",
        "failure_recoverable": True,
        "segment": f"{candidate_id}.mp4",
        "start_ms": 10_000,
        "end_ms": 20_000,
        "hook": candidate_id,
    }


def _state() -> dict:
    return {
        "status": "no_delivery",
        "upload_allowed": False,
        "operator_processing_scope": _grant(),
        "picks": [_failed(TARGET), _failed(OTHER)],
        "pending_talk": [],
        "talk_backlog": [
            {
                "cid": "auto_reserve",
                "segment_path": "/recordings/reserve.mp4",
                "start_ms": 30_000,
                "end_ms": 40_000,
                "hook": "reserve",
            }
        ],
        "pending_song": [copy.deepcopy(SONG)],
        "song_backlog": [{"candidate_id": "song_backlog"}],
        "song_selection_backlog": [{"candidate_id": "song_selection"}],
        "songs": [{"candidate_id": "song_old", "status": "blocked"}],
        "song_superseded_attempts": [{"candidate_id": "song_superseded"}],
        "segments_done": [],
        "segments_dead": {},
    }


def test_v2_scope_freezes_only_an_admitted_historical_failed_talk() -> None:
    state = _state()
    assert operator_talk_scope(state, date=DATE) == (TARGET,)
    state["talk_selection_contract"] = {"schema_version": "conflicting-exact-contract"}
    assert operator_talk_scope(state, date=DATE) == ()
    state.pop("talk_selection_contract")
    state["operator_processing_scope"]["intent"] = "RECOVER_SONGS_TOO"
    assert operator_talk_scope(state, date=DATE) is None


def test_v1_queued_talk_scope_is_talk_only_without_mislabeling_as_failed() -> None:
    state = _state()
    state["picks"] = [_failed(OTHER)]
    state["pending_talk"] = [{"cid": TARGET}]
    grant = state["operator_processing_scope"]
    grant["schema_version"] = GRANT_SCHEMA
    grant.pop("intent")

    assert operator_talk_scope(state, date=DATE) == (TARGET,)


def test_operator_scope_cannot_reinterpret_a_song_as_talk_work() -> None:
    state = _state()
    grant = state["operator_processing_scope"]
    grant["schema_version"] = GRANT_SCHEMA
    grant.pop("intent")
    grant["candidate_ids"] = ["song_pending"]

    assert operator_talk_scope(state, date=DATE) is None


def test_frozen_talk_scope_ignores_pure_song_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _state()
    state["pending_talk"] = []
    state["talk_backlog"] = []
    monkeypatch.setattr(
        semantic_chat_refresh._runner,
        "list_segments",
        lambda *_a, **_k: pytest.fail("Talk-only work flags touched discovery"),
    )
    monkeypatch.setattr(
        semantic_chat_refresh._runner,
        "cover_repair_needed",
        lambda *_a, **_k: False,
    )
    assert semantic_chat_refresh.runner_date_work_flags(
        DATE,
        state,
        automatic_maintenance=True,
        talk_candidate_ids=(TARGET,),
    ) == (False, False, False)


def test_frozen_allowlist_survives_mid_tick_convergence_without_backfill(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _state()
    state["picks"] = []
    state["pending_talk"] = [
        {
            "cid": TARGET,
            "segment_path": "/recordings/target.mp4",
            "start_ms": 10_000,
            "end_ms": 20_000,
            "hook": "target",
            "selected_repair": True,
        }
    ]
    monkeypatch.setattr(
        candidate_selection._runner,
        "refill_songs",
        lambda *_a, **_k: pytest.fail("Song refill crossed frozen scope"),
    )
    candidate_selection.prioritize(
        state,
        frozen_talk_candidate_ids=(TARGET,),
        allow_song_work=False,
    )
    assert [row["cid"] for row in state["pending_talk"]] == [TARGET]
    state["pending_talk"] = []  # named item finished/rejected within this tick
    candidate_selection.prioritize(
        state,
        frozen_talk_candidate_ids=(TARGET,),
        allow_song_work=False,
    )
    assert state["pending_talk"] == []
    assert [row["cid"] for row in state["talk_backlog"]] == ["auto_reserve"]
    assert state["pending_song"] == [SONG]


def test_process_date_preserves_song_queues_and_never_backfills_after_rejection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _state()
    song_preimage = copy.deepcopy(
        {
            key: state[key]
            for key in (
                "pending_song",
                "song_backlog",
                "song_selection_backlog",
                "songs",
                "song_superseded_attempts",
            )
        }
    )
    calls: list[tuple[str, object]] = []

    def requeue_talks(date: str, value: dict, *, candidate_ids=None) -> int:
        assert date == DATE
        assert set(candidate_ids or ()) == {TARGET}
        target = next(row for row in value["picks"] if row["candidate_id"] == TARGET)
        value["picks"].remove(target)
        value["pending_talk"].append(
            {
                "cid": TARGET,
                "segment_path": "/recordings/target.mp4",
                "start_ms": 10_000,
                "end_ms": 20_000,
                "hook": "target",
                "confidence": 0.95,
                "selected_repair": True,
            }
        )
        calls.append(("requeue", tuple(candidate_ids or ())))
        return 1

    def prioritize(value: dict, **kwargs) -> None:
        assert kwargs == {
            "frozen_talk_candidate_ids": (TARGET,),
            "allow_song_work": False,
        }
        pool = list(value.get("pending_talk", [])) + list(value.get("talk_backlog", []))
        value["pending_talk"] = [
            row for row in pool if (row.get("cid") or row.get("candidate_id")) == TARGET
        ]
        value["talk_backlog"] = [
            row for row in pool if (row.get("cid") or row.get("candidate_id")) != TARGET
        ]
        calls.append(("prioritize", [row.get("cid") for row in value["pending_talk"]]))

    def produce_batch(_date: str, items: list[dict], produce_fn) -> list[dict]:
        assert produce_fn is runner.produce_talk
        assert [row["cid"] for row in items] == [TARGET]
        calls.append(("produce", TARGET))
        return [{"candidate_id": TARGET, "status": "candidate_rejected"}]

    def terminal(value: dict, **_kwargs) -> dict:
        value["status"] = "no_delivery"
        return {
            "picks": list(value["picks"]),
            "songs": list(value["songs"]),
            "delivered_talk": [],
            "repaired": [],
            "delivered_songs": [],
            "blocked_songs": [],
            "rejected_songs": [],
            "failures": list(value["picks"]),
            "retry_epoch": None,
        }

    monkeypatch.setattr(runner, "read_state", lambda _date: state)
    monkeypatch.setattr(runner, "runtime_health_error", lambda: None)
    monkeypatch.setattr(runner, "recover_finalized_legacy_hls", lambda *_a, **_k: [])
    monkeypatch.setattr(
        runner,
        "audit_finalized_recording_inventory",
        lambda *_a, **_k: {"can_select": True, "issues": []},
    )
    monkeypatch.setattr(runner, "annotate_state_sessions", lambda *_a, **_k: False)
    monkeypatch.setattr(runner, "AUTOMATIC_MAINTENANCE_NOT_BEFORE", DATE)
    monkeypatch.setattr(runner, "requeue_recoverable_talks", requeue_talks)
    monkeypatch.setattr(
        runner,
        "recover_bound_song_deliveries",
        lambda *_a, **_k: pytest.fail("Song recovery crossed the Talk-only scope"),
    )
    monkeypatch.setattr(
        runner,
        "requeue_recoverable_deliveries",
        lambda *_a, **_k: pytest.fail("combined Talk/Song recovery crossed the scope"),
    )
    monkeypatch.setattr(runner, "song_pipeline_fingerprint", lambda: "sha256:test")
    monkeypatch.setattr(runner, "write_state", lambda *_a, **_k: None)
    monkeypatch.setattr(runner, "write_reports", lambda *_a, **_k: None)
    monkeypatch.setattr(runner, "cpa_healthy", lambda: True)
    monkeypatch.setattr(
        runner,
        "discover_segments",
        lambda *_a, **_k: pytest.fail("historical v2 must not rediscover segments"),
    )
    monkeypatch.setattr(runner, "session_sealed", lambda *_a, **_k: True)
    monkeypatch.setattr(
        runner.semantic_chat_refresh,
        "refresh_operator_scoped_chat_scorecards",
        lambda *_a, **_k: 0,
    )
    monkeypatch.setattr(runner, "prioritize", prioritize)
    monkeypatch.setattr(runner, "prepare_speaker_routing", lambda *_a, **_k: None)
    monkeypatch.setattr(runner, "collect_song_name_candidates", lambda *_a, **_k: [])
    monkeypatch.setattr(runner, "split_produce_blocked_talk_items", lambda rows: (rows, []))
    monkeypatch.setattr(runner, "produce_batch", produce_batch)
    monkeypatch.setattr(runner, "apply_talk_backfill_rejection_policy", lambda *_a, **_k: True)
    monkeypatch.setattr(
        runner,
        "refill_songs",
        lambda *_a, **_k: pytest.fail("Song refill crossed the Talk-only scope"),
    )
    monkeypatch.setattr(runner, "cover_repair_needed", lambda *_a, **_k: False)
    monkeypatch.setattr(
        runner,
        "repair_covers",
        lambda *_a, **_k: pytest.fail("unscoped cover repair crossed the scope"),
    )
    monkeypatch.setattr(runner, "_project_terminal_batch_state", terminal)
    monkeypatch.setattr(runner, "queue_collab_evidence_capture", lambda *_a, **_k: None)
    monkeypatch.setattr(runner, "log", lambda *_a, **_k: None)

    runner.process_date(DATE)

    assert calls == [
        ("requeue", (TARGET,)),
        ("prioritize", [TARGET]),
        ("produce", TARGET),
        ("prioritize", []),
    ]
    assert state["talk_backlog"][0]["cid"] == "auto_reserve"
    assert OTHER in {row.get("candidate_id") for row in state["picks"]}
    assert {
        key: state[key]
        for key in (
            "pending_song",
            "song_backlog",
            "song_selection_backlog",
            "songs",
            "song_superseded_attempts",
        )
    } == song_preimage
    assert state["upload_allowed"] is False


def test_v4_real_annotation_and_terminal_projection_preserve_all_song_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Integration canary: only 221 moves; real annotation/terminal cannot touch Song."""

    target = "auto_221234_1349_1418"
    other = "auto_other_speaker_hold"
    old_fingerprint = "sha256:" + "1" * 64
    new_fingerprint = "sha256:" + "2" * 64
    segment = tmp_path / "recordings" / DATE / "22966160_20260809-22-12-34.mp4"
    segment.parent.mkdir(parents=True)
    segment.write_bytes(b"media")
    segment.with_suffix(".meta.json").write_text(
        '{"LiveStartTime":"2026-08-09T22:12:34+08:00"}', encoding="utf-8"
    )

    def hold(candidate_id: str) -> dict:
        status = "speaker_evidence_insufficient"
        return {
            "candidate_id": candidate_id,
            "status": status,
            "failure_kind": "speaker_evidence",
            "failure_stage": "speaker_finalization",
            "failure_recoverable": False,
            "failure_recovery_fingerprint": old_fingerprint,
            "segment": segment.name,
            "start_ms": 10_000,
            "end_ms": 20_000,
            "hook": candidate_id,
            "speaker_manual_review": {
                "schema_version": "speaker-manual-review-hold.v1",
                "status": "PENDING_HUMAN_REVIEW",
                "held_status": status,
                "upload_authorized": False,
            },
        }

    grant = {
        "schema_version": SPEAKER_HOLD_RECOVERY_GRANT_SCHEMA,
        "grant_id": "2026-08-09-recover-221-speaker-hold",
        "recording_date": DATE,
        "reason": "Ivan 已确认 221 说话人为李豆沙，只恢复这一条供无上传重产。",
        "candidate_ids": [target],
        "user_authorization": {
            "quote": "89221说话人这个是李豆沙，没问题。",
            "timestamp": "2026-08-13T18:00:00Z",
        },
        "expires_at": "2099-08-14T06:00:00Z",
        "intent": SPEAKER_HOLD_RECOVERY_INTENT,
        "upload_allowed": False,
    }
    terminalizable_song = {
        "candidate_id": "song_terminalizable",
        "status": "blocked",
        "rc": 0,
        "reason_codes": ["SONG_AUDIO_LRC_IDENTITY_AMBIGUOUS"],
        "segment_path": str(segment),
    }
    state = {
        "status": "no_delivery",
        "run_mode": "PRODUCTION",
        "source_authority": "RECORDER",
        "upload_allowed": False,
        "operator_processing_scope": grant,
        "picks": [hold(target), hold(other)],
        "pending_talk": [],
        "talk_backlog": [
            {
                "cid": "auto_unscoped_reserve",
                "segment_path": str(segment),
                "start_ms": 30_000,
                "end_ms": 40_000,
                "hook": "unscoped reserve",
            }
        ],
        "pending_song": [{"candidate_id": "song_pending", "segment_path": str(segment)}],
        "song_backlog": [{"candidate_id": "song_backlog", "segment_path": str(segment)}],
        "song_selection_backlog": [
            {"candidate_id": "song_selection", "segment_path": str(segment)}
        ],
        "songs": [terminalizable_song],
        "song_superseded_attempts": [
            {"candidate_id": "song_old", "segment_path": str(segment)}
        ],
        "segments_done": [],
        "segments_dead": {},
    }
    song_keys = (
        "pending_song",
        "song_backlog",
        "song_selection_backlog",
        "songs",
        "song_superseded_attempts",
    )
    song_preimage = copy.deepcopy({key: state[key] for key in song_keys})
    other_talk_preimage = copy.deepcopy(state["picks"][1])
    backlog_preimage = copy.deepcopy(state["talk_backlog"])
    produced: list[str] = []

    monkeypatch.setattr(runner, "read_state", lambda _date: state)
    monkeypatch.setattr(runner, "REC_ROOT", tmp_path / "recordings")
    monkeypatch.setattr(runner, "runtime_health_error", lambda: None)
    monkeypatch.setattr(runner, "recover_finalized_legacy_hls", lambda *_a, **_k: [])
    monkeypatch.setattr(
        runner,
        "audit_finalized_recording_inventory",
        lambda *_a, **_k: {"can_select": True, "issues": []},
    )
    monkeypatch.setattr(runner, "list_segments", lambda _date: [segment])
    monkeypatch.setattr(runner, "session_relation_for_segment", lambda *_a, **_k: None)
    monkeypatch.setattr(runner, "AUTOMATIC_MAINTENANCE_NOT_BEFORE", DATE)
    monkeypatch.setattr(
        runner,
        "recover_bound_song_deliveries",
        lambda *_a, **_k: pytest.fail("v4 must not recover Song deliveries"),
    )
    monkeypatch.setattr(
        runner,
        "requeue_recoverable_deliveries",
        lambda *_a, **_k: pytest.fail("v4 must not enter combined Talk/Song recovery"),
    )
    monkeypatch.setattr(
        runner,
        "requeue_recoverable_songs",
        lambda *_a, **_k: pytest.fail("v4 must not requeue Song candidates"),
    )
    monkeypatch.setattr(
        runner,
        "discover_segments",
        lambda *_a, **_k: pytest.fail("v4 must not discover Talk or Song candidates"),
    )
    monkeypatch.setattr(
        runner,
        "discover_visual_songs",
        lambda *_a, **_k: pytest.fail("v4 must not run visual Song discovery"),
    )
    monkeypatch.setattr(runner, "talk_pipeline_fingerprint", lambda _cid: new_fingerprint)
    monkeypatch.setattr(
        runner,
        "talk_failure_recovery_fingerprint",
        lambda kind, cid: new_fingerprint
        if (kind, cid) == ("speaker_evidence", target)
        else old_fingerprint,
    )
    monkeypatch.setattr(runner, "ffprobe_ms", lambda _path: 600_000)
    monkeypatch.setattr(runner, "find_danmaku_xml", lambda _path: None)
    monkeypatch.setattr(runner, "find_chat_jsonl", lambda _path: None)
    monkeypatch.setattr(
        runner,
        "resolve_structured_chat_binding",
        lambda *_a, **_k: {
            "structured_chat_required": False,
            "chat_binding_status": "NOT_REQUIRED",
        },
    )
    monkeypatch.setattr(runner, "write_state", lambda *_a, **_k: None)
    monkeypatch.setattr(runner, "write_reports", lambda *_a, **_k: None)
    monkeypatch.setattr(runner, "cpa_healthy", lambda: True)
    monkeypatch.setattr(runner, "session_sealed", lambda *_a, **_k: True)
    monkeypatch.setattr(
        runner.semantic_chat_refresh,
        "refresh_operator_scoped_chat_scorecards",
        lambda *_a, **_k: 0,
    )

    monkeypatch.setattr(runner, "prepare_speaker_routing", lambda *_a, **_k: None)
    monkeypatch.setattr(runner, "collect_song_name_candidates", lambda *_a, **_k: [])
    monkeypatch.setattr(runner, "split_produce_blocked_talk_items", lambda rows: (rows, []))

    def produce(_date: str, rows: list[dict], produce_fn) -> list[dict]:
        assert produce_fn is runner.produce_talk
        assert [row["cid"] for row in rows] == [target]
        produced.append(target)
        return [{"candidate_id": target, "status": "review_ready", "rc": 0}]

    monkeypatch.setattr(runner, "produce_batch", produce)
    monkeypatch.setattr(
        runner,
        "produce_song",
        lambda *_a, **_k: pytest.fail("v4 must not produce Song candidates"),
    )
    monkeypatch.setattr(
        runner,
        "refill_songs",
        lambda *_a, **_k: pytest.fail("v4 must not refill Song candidates"),
    )
    monkeypatch.setattr(runner, "cover_repair_needed", lambda *_a, **_k: False)
    monkeypatch.setattr(runner, "queue_collab_evidence_capture", lambda *_a, **_k: None)
    monkeypatch.setattr(runner, "log", lambda *_a, **_k: None)

    runner.process_date(DATE)

    assert produced == [target]
    assert {key: state[key] for key in song_keys} == song_preimage
    assert state["songs"][0]["status"] == "blocked"
    assert state["picks"][0] == other_talk_preimage
    assert state["talk_backlog"] == backlog_preimage
    assert state["upload_allowed"] is False
    assert operator_scope_admission(state, date=DATE).reason_code == "CONVERGED"
    assert operator_talk_scope(state, date=DATE) is None
