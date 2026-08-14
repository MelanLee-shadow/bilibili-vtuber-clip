"""Historical v2 failed-pick scope is a Talk-only lane capability."""

from __future__ import annotations

import copy
from pathlib import Path

import pytest

import scripts.free_session_autoslice as runner
from src.autoslice import candidate_selection
from src.autoslice import semantic_evidence_scorecard_refresh as semantic_chat_refresh
from src.autoslice.exact_talk_recovery_scope import maintain_delivery_recovery_scope
from src.autoslice.final_review_provider_budget_retry import (
    LEDGER_FIELD as FINAL_REVIEW_PROVIDER_BUDGET_LEDGER_FIELD,
    LEDGER_SCHEMA as FINAL_REVIEW_PROVIDER_BUDGET_LEDGER_SCHEMA,
)
from src.autoslice.operator_processing_scope import (
    FINAL_REVIEW_RECOVERY_GRANT_SCHEMA,
    FINAL_REVIEW_RECOVERY_INTENT,
    GRANT_SCHEMA,
    SOURCE_FACT_RECOVERY_GRANT_SCHEMA,
    SOURCE_FACT_RECOVERY_INTENT,
    SPEAKER_HOLD_RECOVERY_GRANT_SCHEMA,
    SPEAKER_HOLD_RECOVERY_INTENT,
    TOPIC_HOLD_RECOVERY_GRANT_SCHEMA,
    TOPIC_HOLD_RECOVERY_INTENT,
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


def _v5_state() -> dict:
    state = _state()
    state["operator_processing_scope"] = {
        "schema_version": TOPIC_HOLD_RECOVERY_GRANT_SCHEMA,
        "grant_id": "release-one-resolved-topic-hold",
        "recording_date": DATE,
        "reason": "刷新后的人工去重结论已绑定，只释放点名停泊件且不授权任何上传。",
        "candidate_ids": [TARGET],
        "user_authorization": {
            "quote": "806，1576内容没问题可以发。",
            "timestamp": "2026-08-13T18:00:00Z",
        },
        "expires_at": "2099-08-14T06:00:00Z",
        "intent": TOPIC_HOLD_RECOVERY_INTENT,
        "upload_allowed": False,
    }
    state["picks"] = [_failed(OTHER)]
    state["published_topic_dedup_review"] = {
        "schema_version": "published-topic-dedup-review-state.v1",
        "holds": [
            {
                "candidate_id": TARGET,
                "candidate": {
                    "cid": TARGET,
                    "segment_path": "/recordings/target.mp4",
                    "start_ms": 10_000,
                    "end_ms": 20_000,
                    "hook": "target",
                },
                "queue_origin": "pending_talk",
                "suppression_authorized": False,
                "upload_authorized": False,
            }
        ],
    }
    return state


def _v5_selected_authority_rejection_state() -> dict:
    state = _v5_state()
    rejected = {
        "candidate_id": TARGET,
        "status": "candidate_rejected",
        "rejected_status": "failed",
        "selected_repair": True,
        "rc": 1,
        "failure_kind": "subtitle_authority",
        "failure_stage": "chat_authority_final_artifact",
        "failure_recoverable": False,
        "failure_recovery_fingerprint": "sha256:" + "1" * 64,
        "rejection_reason": "subtitle_authority_unresolved_backfilled",
        "segment": f"{TARGET}.mp4",
        "start_ms": 10_000,
        "end_ms": 20_000,
        "hook": TARGET,
        "selection_scorecard": {"schema_version": "test-scorecard.v1"},
    }
    state["picks"].append(rejected)
    state["published_topic_dedup_review"] = {
        "schema_version": "published-topic-dedup-review-state.v1",
        "holds": [
            {
                "candidate_id": TARGET,
                "disposition": "HUMAN_TOPIC_DEDUP_REVIEW_AUTHORITY_STALE",
                "reason_code": "PUBLISHED_TOPIC_REVIEW_AUTHORITY_STALE",
                "score_mutated": False,
                "suppression_authorized": False,
                "upload_authorized": False,
                "queue_origin": None,
                "candidate": copy.deepcopy(rejected),
                "evidence": {
                    "error": (
                        "PublishedTopicCollisionError: current scorecard refresh "
                        "receipt is missing"
                    )
                },
            }
        ],
    }
    state["published_topic_resolution_recovery"] = {
        "schema_version": "published-topic-resolution-recovery-ledger.v1",
        "entries": {TARGET: {"candidate_id": TARGET}},
        "ledger_sha256": "test-seal",
    }
    return state


def _v6_grant() -> dict:
    return {
        "schema_version": SOURCE_FACT_RECOVERY_GRANT_SCHEMA,
        "grant_id": "retry-one-selected-source-fact-rejection",
        "recording_date": DATE,
        "reason": "只重试点名的出处事实失败候选，不处理或发布其他候选。",
        "candidate_ids": [TARGET],
        "user_authorization": {
            "quote": "继续，我连上网了",
            "timestamp": "2026-08-13T22:00:00Z",
        },
        "expires_at": "2099-08-14T06:00:00Z",
        "intent": SOURCE_FACT_RECOVERY_INTENT,
        "upload_allowed": False,
    }


def _v6_rejection() -> dict:
    return {
        "candidate_id": TARGET,
        "status": "candidate_rejected",
        "rejected_status": "failed",
        "selected_repair": True,
        "rc": 1,
        "failure_kind": "story_contract",
        "failure_stage": "source_fact_repair",
        "failure_recoverable": False,
        "failure_recovery_fingerprint": "sha256:" + "1" * 64,
        "rejection_reason": "story_contract_unresolved_backfilled",
        "segment": f"{TARGET}.mp4",
        "start_ms": 10_000,
        "end_ms": 20_000,
        "hook": TARGET,
    }


def _v6_queued_state() -> dict:
    from src.autoslice.selected_source_fact_recovery import (
        RECOVERY_RECEIPT_FIELD,
        build_selected_source_fact_recovery_receipt,
    )

    queue_row = {
        "cid": TARGET,
        "segment_path": f"/recordings/{TARGET}.mp4",
        "start_ms": 10_000,
        "end_ms": 20_000,
        "hook": TARGET,
        "selected_repair": True,
        "talk_repair_retry_count": 1,
    }
    receipt = build_selected_source_fact_recovery_receipt(
        old_row=_v6_rejection(),
        queued_row=queue_row,
        candidate_id=TARGET,
        grant_id=_v6_grant()["grant_id"],
        current_fingerprint="sha256:" + "2" * 64,
    )
    queue_row[RECOVERY_RECEIPT_FIELD] = receipt
    return {
        "status": "no_delivery",
        "upload_allowed": False,
        "operator_processing_scope": _v6_grant(),
        "picks": [_failed(OTHER)],
        "pending_talk": [queue_row],
        "talk_backlog": [],
        "talk_below_confidence_threshold": [],
        "pending_song": [copy.deepcopy(SONG)],
        "song_backlog": [],
        "song_selection_backlog": [],
        "songs": [],
        "song_superseded_attempts": [],
    }


def _v7_grant() -> dict:
    grant = _v6_grant()
    grant.update(
        {
            "schema_version": FINAL_REVIEW_RECOVERY_GRANT_SCHEMA,
            "grant_id": "retry-one-selected-final-review-rejection",
            "reason": "只重试点名的 final-review 失败及 provider-budget continuation。",
            "intent": FINAL_REVIEW_RECOVERY_INTENT,
        }
    )
    return grant


def _v7_rejection() -> dict:
    row = _v6_rejection()
    row.update(
        {
            "failure_kind": "subtitle_authority",
            "failure_stage": "final_review_findings",
            "rejection_reason": "subtitle_authority_unresolved_backfilled",
            "talk_repair_retry_count": 5,
        }
    )
    return row


def _v7_queued_state() -> dict:
    from src.autoslice.selected_final_review_recovery import (
        RECOVERY_RECEIPT_FIELD,
        build_selected_final_review_recovery_receipt,
    )

    queue_row = {
        "cid": TARGET,
        "segment_path": f"/recordings/{TARGET}.mp4",
        "start_ms": 10_000,
        "end_ms": 20_000,
        "hook": TARGET,
        "selected_repair": True,
        "talk_repair_retry_count": 6,
    }
    receipt = build_selected_final_review_recovery_receipt(
        old_row=_v7_rejection(),
        queued_row=queue_row,
        candidate_id=TARGET,
        grant_id=_v7_grant()["grant_id"],
        current_fingerprint="sha256:" + "2" * 64,
    )
    queue_row[RECOVERY_RECEIPT_FIELD] = receipt
    state = _v6_queued_state()
    state["operator_processing_scope"] = _v7_grant()
    state["pending_talk"] = [queue_row]
    state["talk_superseded_attempts"] = []
    return state


def _v7_fresh_rejection_state() -> dict:
    state = _state()
    state["operator_processing_scope"] = _v7_grant()
    state["picks"] = [_v7_rejection(), _failed(OTHER)]
    state["pending_talk"] = []
    state["talk_below_confidence_threshold"] = []
    state["talk_superseded_attempts"] = []
    return state


def test_v7_no_marker_handoff_absent_preserves_ordinary_maintenance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.autoslice import historical_failed_talk_scope
    from src.autoslice import published_topic_final_review_handoff as handoff

    state = _v7_fresh_rejection_state()
    monkeypatch.setattr(
        runner,
        "talk_failure_recovery_fingerprint",
        lambda kind, candidate_id: "sha256:" + "2" * 64
        if (kind, candidate_id) == ("subtitle_authority", TARGET)
        else None,
    )
    assert handoff.inspect_initial_final_review_handoff(
        state,
        candidate_id=TARGET,
        recording_date=DATE,
    ) == handoff.HANDOFF_ABSENT

    def generic(_date, value, **_kwargs):
        value["ordinary_v7_maintenance_ran"] = True
        return 1, 2, 3, 4, True

    monkeypatch.setattr(
        historical_failed_talk_scope,
        "maintain_delivery_recovery_scope",
        generic,
    )
    monkeypatch.setattr(
        handoff,
        "seal_published_topic_final_review_handoff",
        lambda *_a, **_k: pytest.fail("ABSENT must not enter terminal handoff"),
    )

    assert historical_failed_talk_scope.maintain(
        DATE,
        state,
        automatic_maintenance=True,
        candidate_ids=(TARGET,),
    ) == (1, 2, 3, 4, True)
    assert state["ordinary_v7_maintenance_ran"] is True
    assert "published_topic_resolution_recovery" not in state
    assert "operator_processing_scope_runtime_block" not in state


@pytest.mark.parametrize(
    "failure_mode",
    [
        "inspect_blocked",
        "inspect_mutates",
        "generic_error",
        "seal_false",
        "seal_error",
        "top_level_upload",
        "scope_drift",
    ],
)
def test_v7_terminal_handoff_failure_restores_full_preimage_and_typed_block(
    monkeypatch: pytest.MonkeyPatch,
    failure_mode: str,
) -> None:
    from src.autoslice import historical_failed_talk_scope
    from src.autoslice import published_topic_final_review_handoff as handoff

    state = _v7_fresh_rejection_state()
    state["published_topic_resolution_recovery"] = {"old_v5_marker": "sealed"}
    preimage = copy.deepcopy(state)
    calls: list[str] = []

    def inspect(value, **_kwargs):
        calls.append("inspect")
        if failure_mode == "inspect_mutates":
            value["probe_mutation"] = True
        return (
            handoff.HANDOFF_BLOCKED
            if failure_mode == "inspect_blocked"
            else handoff.HANDOFF_READY
        )

    def generic(_date, value, **_kwargs):
        calls.append("generic")
        value["generic_partial_mutation"] = True
        if failure_mode == "top_level_upload":
            value["upload_allowed"] = True
        elif failure_mode == "scope_drift":
            value["operator_processing_scope"] = _grant()
        if failure_mode == "generic_error":
            raise RuntimeError("generic requeue failed after mutation")
        return 0, 0, 1, 0, False

    def seal(value, _candidate_id, **kwargs):
        calls.append("seal")
        assert kwargs["pre_state"] == preimage
        value["terminal_partial_mutation"] = True
        if failure_mode == "seal_error":
            raise RuntimeError("terminal seal failed after mutation")
        return failure_mode not in {"seal_false", "top_level_upload", "scope_drift"}

    monkeypatch.setattr(handoff, "inspect_initial_final_review_handoff", inspect)
    monkeypatch.setattr(handoff, "seal_published_topic_final_review_handoff", seal)
    monkeypatch.setattr(
        historical_failed_talk_scope,
        "maintain_delivery_recovery_scope",
        generic,
    )

    result = historical_failed_talk_scope.maintain(
        DATE,
        state,
        automatic_maintenance=True,
        candidate_ids=(TARGET,),
    )

    assert result == (0, 0, 0, 0, False)
    assert {
        key: value
        for key, value in state.items()
        if key != "operator_processing_scope_runtime_block"
    } == preimage
    assert state["operator_processing_scope_runtime_block"] == {
        "schema_version": "operator-processing-scope-runtime-block.v1",
        "recording_date": DATE,
        "candidate_ids": [TARGET],
        "intent": FINAL_REVIEW_RECOVERY_INTENT,
        "upload_allowed": False,
        "reason_code": "SELECTED_FINAL_REVIEW_TOPIC_LINEAGE_HANDOFF_BLOCKED",
    }
    assert calls == {
        "inspect_blocked": ["inspect"],
        "inspect_mutates": ["inspect"],
        "generic_error": ["inspect", "generic"],
        "seal_false": ["inspect", "generic", "seal"],
        "seal_error": ["inspect", "generic", "seal"],
        "top_level_upload": ["inspect", "generic", "seal"],
        "scope_drift": ["inspect", "generic", "seal"],
    }[failure_mode]


def test_v7_terminal_handoff_seals_after_generic_requeue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.autoslice import historical_failed_talk_scope
    from src.autoslice import published_topic_final_review_handoff as handoff

    state = _v7_fresh_rejection_state()
    state["published_topic_resolution_recovery"] = {"old_v5_marker": "sealed"}
    preimage = copy.deepcopy(state)
    calls: list[str] = []

    monkeypatch.setattr(
        handoff,
        "inspect_initial_final_review_handoff",
        lambda *_a, **_k: calls.append("inspect") or handoff.HANDOFF_READY,
    )

    def generic(_date, value, **_kwargs):
        calls.append("generic")
        value["picks"] = [
            row for row in value["picks"] if row.get("candidate_id") != TARGET
        ]
        value["pending_talk"].append({"cid": TARGET, "initial_v7_receipt": True})
        return 0, 0, 1, 0, False

    def seal(value, candidate_id, **kwargs):
        calls.append("seal")
        assert candidate_id == TARGET
        assert kwargs["pre_state"] == preimage
        assert value["pending_talk"][-1]["cid"] == TARGET
        value["published_topic_resolution_recovery"] = {"terminal": "sealed"}
        return True

    monkeypatch.setattr(
        historical_failed_talk_scope,
        "maintain_delivery_recovery_scope",
        generic,
    )
    monkeypatch.setattr(handoff, "seal_published_topic_final_review_handoff", seal)

    assert historical_failed_talk_scope.maintain(
        DATE,
        state,
        automatic_maintenance=True,
        candidate_ids=(TARGET,),
    ) == (0, 0, 1, 0, False)
    assert calls == ["inspect", "generic", "seal"]
    assert state["published_topic_resolution_recovery"] == {"terminal": "sealed"}
    assert "operator_processing_scope_runtime_block" not in state


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


def test_broad_maintenance_cannot_requeue_a_marker_bound_topic_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _state()
    state["published_topic_resolution_recovery"] = {
        "schema_version": "published-topic-resolution-recovery-ledger.v1",
        "entries": {},
        "ledger_sha256": "sha256:test-seal",
    }
    calls: list[set[str] | None] = []
    from src.autoslice import published_topic_collision as topic_collision

    monkeypatch.setattr(
        topic_collision,
        "_recovery_ledger_entries",
        lambda _state: (TARGET,),
    )
    monkeypatch.setattr(
        topic_collision,
        "inspect_published_topic_resolution_recovery",
        lambda _state, candidate_id: (
            "RELEASED_RETRY_PENDING" if candidate_id == TARGET else "CONVERGED"
        ),
    )
    monkeypatch.setattr(
        runner,
        "requeue_recoverable_talks",
        lambda _date, _state, *, candidate_ids=None: calls.append(candidate_ids) or 0,
    )
    monkeypatch.setattr(
        runner,
        "requeue_stale_current_recovery_talks",
        lambda *_a, **_k: 0,
    )
    monkeypatch.setattr(runner, "recover_bound_song_deliveries", lambda *_a: 0)
    monkeypatch.setattr(runner, "requeue_recoverable_songs", lambda *_a: 0)

    result = maintain_delivery_recovery_scope(
        DATE,
        state,
        automatic_maintenance=True,
        talk_candidate_ids=None,
    )

    assert result == (0, 0, 0, 0, False)
    assert calls == [{OTHER}]


@pytest.mark.parametrize("receipt_location", ["current", "superseded"])
def test_broad_maintenance_cannot_requeue_a_v6_bound_failure(
    monkeypatch: pytest.MonkeyPatch,
    receipt_location: str,
) -> None:
    state = _state()
    state.pop("operator_processing_scope")
    receipt = {"schema_version": "selected-source-fact-recovery-receipt.v1"}
    if receipt_location == "current":
        state["picks"][0]["selected_source_fact_recovery"] = receipt
    else:
        # Even deletion/corruption of the current receipt cannot turn a
        # previously sealed one-shot lineage into ordinary broad maintenance.
        state["talk_superseded_attempts"] = [
            {
                "candidate_id": TARGET,
                "selected_source_fact_recovery": receipt,
            }
        ]
    calls: list[set[str] | None] = []
    monkeypatch.setattr(
        runner,
        "requeue_recoverable_talks",
        lambda _date, _state, *, candidate_ids=None: calls.append(candidate_ids)
        or 0,
    )
    monkeypatch.setattr(
        runner,
        "requeue_stale_current_recovery_talks",
        lambda *_a, **_k: 0,
    )
    monkeypatch.setattr(runner, "recover_bound_song_deliveries", lambda *_a: 0)
    monkeypatch.setattr(runner, "requeue_recoverable_songs", lambda *_a: 0)

    result = maintain_delivery_recovery_scope(
        DATE,
        state,
        automatic_maintenance=True,
        talk_candidate_ids=None,
    )

    assert result == (0, 0, 0, 0, False)
    assert calls == [{OTHER}]


@pytest.mark.parametrize("receipt_location", ["current", "superseded"])
def test_broad_maintenance_cannot_requeue_a_v7_bound_failure(
    monkeypatch: pytest.MonkeyPatch,
    receipt_location: str,
) -> None:
    state = _state()
    state.pop("operator_processing_scope")
    receipt = {"schema_version": "selected-final-review-recovery-receipt.v1"}
    if receipt_location == "current":
        state["picks"][0]["selected_final_review_recovery"] = receipt
    else:
        state["talk_superseded_attempts"] = [
            {
                "candidate_id": TARGET,
                "selected_final_review_recovery": receipt,
            }
        ]
    calls: list[set[str] | None] = []
    monkeypatch.setattr(
        runner,
        "requeue_recoverable_talks",
        lambda _date, _state, *, candidate_ids=None: calls.append(candidate_ids)
        or 0,
    )
    monkeypatch.setattr(
        runner, "requeue_stale_current_recovery_talks", lambda *_a, **_k: 0
    )
    monkeypatch.setattr(runner, "recover_bound_song_deliveries", lambda *_a: 0)
    monkeypatch.setattr(runner, "requeue_recoverable_songs", lambda *_a: 0)

    result = maintain_delivery_recovery_scope(
        DATE, state, automatic_maintenance=True, talk_candidate_ids=None
    )
    assert result == (0, 0, 0, 0, False)
    assert calls == [{OTHER}]


def test_null_superseded_v6_receipt_does_not_freeze_ordinary_recovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _state()
    state.pop("operator_processing_scope")
    state["talk_superseded_attempts"] = [
        {
            "candidate_id": TARGET,
            "selected_source_fact_recovery": None,
        }
    ]
    calls: list[str] = []
    monkeypatch.setattr(
        runner,
        "requeue_recoverable_deliveries",
        lambda _date, _state: calls.append("broad") or (0, 0, 0),
    )
    monkeypatch.setattr(
        runner,
        "requeue_stale_current_recovery_talks",
        lambda *_a, **_k: 0,
    )
    monkeypatch.setattr(runner, "recover_bound_song_deliveries", lambda *_a: 0)
    monkeypatch.setattr(runner, "requeue_recoverable_songs", lambda *_a: 0)

    maintain_delivery_recovery_scope(
        DATE,
        state,
        automatic_maintenance=True,
        talk_candidate_ids=None,
    )

    assert calls == ["broad"]


@pytest.mark.parametrize(
    "malformed_ledger",
    [
        "not-an-object",
        {
            "schema_version": "published-topic-resolution-recovery-ledger.v1",
            "entries": [],
        },
        {
            "schema_version": "wrong-ledger-schema",
            "entries": {},
            "ledger_sha256": "sha256:" + "0" * 64,
        },
        {
            "schema_version": "published-topic-resolution-recovery-ledger.v1",
            "entries": {},
            "ledger_sha256": "sha256:" + "0" * 64,
        },
        {
            "schema_version": "published-topic-resolution-recovery-ledger.v1",
            "entries": {"unsafe candidate id": {}},
            "ledger_sha256": "sha256:" + "0" * 64,
        },
    ],
)
def test_broad_maintenance_blocks_all_talk_requeue_for_malformed_topic_lineage(
    monkeypatch: pytest.MonkeyPatch,
    malformed_ledger: object,
) -> None:
    state = _state()
    state["published_topic_resolution_recovery"] = malformed_ledger
    calls: list[set[str] | None] = []
    monkeypatch.setattr(
        runner,
        "requeue_recoverable_talks",
        lambda _date, _state, *, candidate_ids=None: calls.append(candidate_ids) or 0,
    )
    monkeypatch.setattr(
        runner,
        "requeue_stale_current_recovery_talks",
        lambda *_a, **_k: 0,
    )
    monkeypatch.setattr(runner, "recover_bound_song_deliveries", lambda *_a: 0)
    monkeypatch.setattr(runner, "requeue_recoverable_songs", lambda *_a: 0)

    maintain_delivery_recovery_scope(
        DATE,
        state,
        automatic_maintenance=True,
        talk_candidate_ids=None,
    )

    assert calls == [set()]


def test_exact_contract_cannot_run_stale_talk_requeue_with_malformed_topic_lineage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _state()
    state["run_mode"] = "RECOVERY_REVIEW"
    state["talk_selection_contract"] = {
        "schema_version": "talk-selection-contract.v1",
        "mode": "EXACT_CANDIDATE_SET_NO_BACKFILL",
        "candidate_ids": [TARGET],
        "source_state_sha256": "sha256:" + "a" * 64,
        "authority": "exact review-only recovery",
    }
    state["published_topic_resolution_recovery"] = {
        "schema_version": "wrong-ledger-schema",
        "entries": {},
        "ledger_sha256": "sha256:" + "0" * 64,
    }
    monkeypatch.setattr(
        runner,
        "requeue_stale_current_recovery_talks",
        lambda *_a, **_k: pytest.fail("malformed ledger must block stale Talk requeue"),
    )
    calls: list[set[str] | None] = []
    monkeypatch.setattr(
        runner,
        "requeue_recoverable_talks",
        lambda _date, _state, *, candidate_ids=None: calls.append(candidate_ids) or 0,
    )

    result = maintain_delivery_recovery_scope(
        DATE,
        state,
        automatic_maintenance=True,
        talk_candidate_ids=None,
    )

    assert result == (0, 0, 0, 0, False)
    assert calls == [set()]


def test_v5_shape_cannot_bypass_malformed_topic_lineage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _v5_state()
    state["published_topic_resolution_recovery"] = {
        "schema_version": "wrong-ledger-schema",
        "entries": {},
        "ledger_sha256": "sha256:" + "0" * 64,
    }
    calls: list[set[str] | None] = []
    monkeypatch.setattr(
        runner,
        "requeue_recoverable_talks",
        lambda _date, _state, *, candidate_ids=None: calls.append(candidate_ids) or 0,
    )

    result = maintain_delivery_recovery_scope(
        DATE,
        state,
        automatic_maintenance=True,
        talk_candidate_ids=(TARGET,),
    )

    assert result == (0, 0, 0, 0, False)
    assert calls == [set()]


def test_v5_first_release_writes_marker_and_second_queued_tick_skips_release(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.autoslice import historical_failed_talk_scope
    from src.autoslice import published_topic_collision as topic_collision

    state = _v5_state()
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
    order: list[str] = []

    def inspect(value: dict, candidate_id: str) -> str:
        order.append("inspect")
        assert candidate_id == TARGET
        if value.get("published_topic_resolution_recovery") is not None:
            return "RELEASED_QUEUED"
        return "READY_TO_RELEASE"

    def release(value: dict, candidate_id: str) -> bool:
        order.append("release")
        assert candidate_id == TARGET
        hold = value["published_topic_dedup_review"]["holds"].pop()
        value["pending_talk"].append(copy.deepcopy(hold["candidate"]))
        value["published_topic_resolution_recovery"] = {
            "schema_version": "published-topic-resolution-recovery-marker.v1",
            "candidate_id": TARGET,
        }
        return True

    def generic(_date, value, *, automatic_maintenance, talk_candidate_ids):
        order.append("generic")
        assert automatic_maintenance is True
        assert tuple(talk_candidate_ids) == (TARGET,)
        assert [row["cid"] for row in value["pending_talk"]] == [TARGET]
        return 0, 0, 0, 0, False

    monkeypatch.setattr(
        topic_collision,
        "inspect_published_topic_resolution_recovery",
        inspect,
        raising=False,
    )
    monkeypatch.setattr(
        topic_collision,
        "release_resolved_published_topic_hold",
        release,
    )
    monkeypatch.setattr(
        historical_failed_talk_scope,
        "maintain_delivery_recovery_scope",
        generic,
    )

    first = historical_failed_talk_scope.maintain(
        DATE,
        state,
        automatic_maintenance=True,
        candidate_ids=(TARGET,),
    )

    assert first == (0, 0, 0, 0, False)
    assert order == ["inspect", "release", "inspect", "generic"]
    assert state["published_topic_resolution_recovery"] == {
        "schema_version": "published-topic-resolution-recovery-marker.v1",
        "candidate_id": TARGET,
    }
    assert state["published_topic_dedup_review"]["holds"] == []
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

    order.clear()
    second = historical_failed_talk_scope.maintain(
        DATE,
        state,
        automatic_maintenance=True,
        candidate_ids=(TARGET,),
    )

    assert second == (0, 0, 0, 0, False)
    assert order == ["inspect", "generic"]
    assert "operator_processing_scope_runtime_block" not in state


def test_v5_retry_pending_still_enters_generic_maintenance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A typed recoverable pick is unfinished work, never a terminal release."""

    from src.autoslice import historical_failed_talk_scope
    from src.autoslice import published_topic_collision as topic_collision

    state = _v5_state()
    state["published_topic_dedup_review"]["holds"] = []
    state["pending_talk"] = []
    state["picks"].append(
        {
            "candidate_id": TARGET,
            "status": "failed",
            "failure_recoverable": True,
        }
    )
    state["published_topic_resolution_recovery"] = {
        "schema_version": "published-topic-resolution-recovery-ledger.v1",
        "entries": {TARGET: {"candidate_id": TARGET}},
        "ledger_sha256": "test-seal",
    }
    order: list[str] = []
    monkeypatch.setattr(
        topic_collision,
        "inspect_published_topic_resolution_recovery",
        lambda *_a, **_k: "RELEASED_RETRY_PENDING",
    )

    def generic(_date, value, *, automatic_maintenance, talk_candidate_ids):
        order.append("generic")
        assert automatic_maintenance is True
        assert tuple(talk_candidate_ids) == (TARGET,)
        assert value["picks"][-1]["failure_recoverable"] is True
        failed = value["picks"].pop()
        value["pending_talk"].append(
            {
                "cid": TARGET,
                "segment_path": "/recordings/target.mp4",
                "start_ms": 10_000,
                "end_ms": 20_000,
                "hook": "target",
                "selection_scorecard": {"schema_version": "test.v1"},
                "recovery_source_record_sha256": "sha256:" + "a" * 64,
                "test_failed_row": failed,
            }
        )
        return 0, 0, 1, 0, False

    monkeypatch.setattr(
        historical_failed_talk_scope,
        "maintain_delivery_recovery_scope",
        generic,
    )

    def advance(value, candidate_id, **kwargs):
        order.append("advance")
        assert candidate_id == TARGET
        assert kwargs["pre_state"]["picks"][-1]["failure_recoverable"] is True
        assert kwargs["from_collection"] == "picks"
        assert kwargs["from_row"]["candidate_id"] == TARGET
        assert kwargs["to_collection"] == "pending_talk"
        assert kwargs["to_row"] == value["pending_talk"][-1]
        return True

    monkeypatch.setattr(
        topic_collision,
        "advance_published_topic_resolution_recovery",
        advance,
    )

    result = historical_failed_talk_scope.maintain(
        DATE,
        state,
        automatic_maintenance=True,
        candidate_ids=(TARGET,),
    )

    assert result == (0, 0, 1, 0, False)
    assert order == ["generic", "advance"]
    assert "operator_processing_scope_runtime_block" not in state


def test_v5_changed_boundary_fingerprint_requeues_false_failure_and_advances_lineage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A repaired boundary contract retries under v5 without falsifying recoverability."""

    from src.autoslice import historical_failed_talk_scope
    from src.autoslice import published_topic_collision as topic_collision

    state = _v5_state()
    state["published_topic_dedup_review"]["holds"] = []
    state["pending_talk"] = []
    state["picks"].append(
        {
            "candidate_id": TARGET,
            "status": "failed",
            "failure_kind": "content_boundary",
            "failure_recoverable": False,
            "failure_recovery_fingerprint": "sha256:" + "1" * 64,
        }
    )
    state["published_topic_resolution_recovery"] = {
        "schema_version": "published-topic-resolution-recovery-ledger.v1",
        "entries": {TARGET: {"candidate_id": TARGET}},
        "ledger_sha256": "test-seal",
    }
    order: list[str] = []
    monkeypatch.setattr(
        topic_collision,
        "inspect_published_topic_resolution_recovery",
        lambda *_a, **_k: "RELEASED_RETRY_PENDING",
    )

    def generic(_date, value, *, automatic_maintenance, talk_candidate_ids):
        order.append("generic")
        assert automatic_maintenance is True
        assert tuple(talk_candidate_ids) == (TARGET,)
        failed = value["picks"].pop()
        assert failed["failure_recoverable"] is False
        value["pending_talk"].append(
            {
                "cid": TARGET,
                "segment_path": "/recordings/target.mp4",
                "start_ms": 10_000,
                "end_ms": 20_000,
                "hook": "target",
                "recovery_source_record_sha256": "sha256:" + "a" * 64,
            }
        )
        return 0, 0, 1, 0, False

    monkeypatch.setattr(
        historical_failed_talk_scope,
        "maintain_delivery_recovery_scope",
        generic,
    )

    def advance(value, candidate_id, **kwargs):
        order.append("advance")
        assert candidate_id == TARGET
        assert kwargs["from_row"]["failure_recoverable"] is False
        assert kwargs["from_row"]["failure_kind"] == "content_boundary"
        assert kwargs["to_row"] == value["pending_talk"][-1]
        value["published_topic_resolution_recovery"] = {"head": "sealed-retry"}
        return True

    monkeypatch.setattr(
        topic_collision,
        "advance_published_topic_resolution_recovery",
        advance,
    )

    result = historical_failed_talk_scope.maintain(
        DATE,
        state,
        automatic_maintenance=True,
        candidate_ids=(TARGET,),
    )

    assert result == (0, 0, 1, 0, False)
    assert order == ["generic", "advance"]
    assert state["published_topic_resolution_recovery"] == {
        "head": "sealed-retry"
    }
    assert "operator_processing_scope_runtime_block" not in state


def test_v5_unsealed_generic_retry_transition_rolls_back_and_blocks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.autoslice import historical_failed_talk_scope
    from src.autoslice import published_topic_collision as topic_collision

    state = _v5_state()
    state["published_topic_dedup_review"]["holds"] = []
    state["pending_talk"] = []
    state["picks"].append(
        {
            "candidate_id": TARGET,
            "status": "failed",
            "failure_recoverable": True,
        }
    )
    state["published_topic_resolution_recovery"] = {
        "schema_version": "published-topic-resolution-recovery-ledger.v1",
        "entries": {TARGET: {"candidate_id": TARGET}},
        "ledger_sha256": "test-seal",
    }
    preimage = copy.deepcopy(state)
    monkeypatch.setattr(
        topic_collision,
        "inspect_published_topic_resolution_recovery",
        lambda *_a, **_k: "RELEASED_RETRY_PENDING",
    )

    def generic(_date, value, **_kwargs):
        value["picks"].pop()
        value["pending_talk"].append({"cid": TARGET, "unsealed": True})
        return 0, 0, 1, 0, False

    monkeypatch.setattr(
        historical_failed_talk_scope,
        "maintain_delivery_recovery_scope",
        generic,
    )
    monkeypatch.setattr(
        topic_collision,
        "advance_published_topic_resolution_recovery",
        lambda *_a, **_k: False,
    )

    result = historical_failed_talk_scope.maintain(
        DATE,
        state,
        automatic_maintenance=True,
        candidate_ids=(TARGET,),
    )

    assert result == (0, 0, 0, 0, False)
    assert {
        key: value
        for key, value in state.items()
        if key != "operator_processing_scope_runtime_block"
    } == preimage
    assert state["operator_processing_scope_runtime_block"]["reason_code"] == (
        "TOPIC_DEDUP_RETRY_TRANSITION_BLOCKED"
    )


@pytest.mark.parametrize(
    "failure_phase",
    [
        "generic_error",
        "no_move",
        "review_mutation",
        "advance_false",
        "advance_error",
    ],
)
def test_v5_redundant_stale_hold_removal_rolls_back_with_failed_requeue_or_lineage(
    monkeypatch: pytest.MonkeyPatch,
    failure_phase: str,
) -> None:
    from src.autoslice import historical_failed_talk_scope
    from src.autoslice import published_topic_collision as topic_collision

    state = _v5_selected_authority_rejection_state()
    preimage = copy.deepcopy(state)
    order: list[str] = []
    monkeypatch.setattr(
        topic_collision,
        "inspect_published_topic_resolution_recovery",
        lambda *_a, **_k: "RELEASED_RETRY_PENDING",
    )

    def generic(_date, value, **_kwargs):
        order.append("generic")
        assert value["published_topic_dedup_review"]["holds"] == []
        if failure_phase == "generic_error":
            value["picks"].pop()
            raise RuntimeError("generic recovery failed after mutation")
        if failure_phase == "no_move":
            return 0, 0, 0, 0, False
        failed = next(
            row
            for row in value["picks"]
            if row.get("candidate_id") == TARGET
        )
        value["picks"].remove(failed)
        value["pending_talk"].append(
            {
                "cid": TARGET,
                "segment": failed["segment"],
                "start_ms": failed["start_ms"],
                "end_ms": failed["end_ms"],
                "hook": failed["hook"],
                "selection_scorecard": copy.deepcopy(
                    failed["selection_scorecard"]
                ),
                "recovery_source_record_sha256": "sha256:" + "a" * 64,
            }
        )
        if failure_phase == "review_mutation":
            value["published_topic_dedup_review"]["unsealed"] = True
        return 0, 0, 1, 0, False

    monkeypatch.setattr(
        historical_failed_talk_scope,
        "maintain_delivery_recovery_scope",
        generic,
    )

    def advance(*_args, **_kwargs):
        order.append("advance")
        if failure_phase == "advance_error":
            raise RuntimeError("lineage seal failed")
        return False

    monkeypatch.setattr(
        topic_collision,
        "advance_published_topic_resolution_recovery",
        advance,
    )

    result = historical_failed_talk_scope.maintain(
        DATE,
        state,
        automatic_maintenance=True,
        candidate_ids=(TARGET,),
    )

    assert result == (0, 0, 0, 0, False)
    assert order == (
        ["generic"]
        if failure_phase in {"generic_error", "no_move", "review_mutation"}
        else ["generic", "advance"]
    )
    assert {
        key: value
        for key, value in state.items()
        if key != "operator_processing_scope_runtime_block"
    } == preimage
    assert state["published_topic_dedup_review"]["holds"] == preimage[
        "published_topic_dedup_review"
    ]["holds"]
    assert state["operator_processing_scope_runtime_block"]["reason_code"] == (
        "TOPIC_DEDUP_RETRY_TRANSITION_BLOCKED"
    )


def test_v5_session_annotation_rebound_failure_rolls_back_before_persist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.autoslice import historical_failed_talk_scope
    from src.autoslice import published_topic_collision as topic_collision

    state = _v5_state()
    state["published_topic_resolution_recovery"] = {"sealed": "preimage"}
    state["pending_talk"] = [
        {
            "cid": TARGET,
            "segment_path": "/recordings/target.mp4",
            "start_ms": 10_000,
            "end_ms": 20_000,
            "hook": "target",
        }
    ]
    preimage = copy.deepcopy(state)

    def annotate(_date, value, **_kwargs):
        value["pending_talk"][0]["session_id"] = "unsealed-session"
        return True

    monkeypatch.setattr(runner, "annotate_state_sessions", annotate)
    monkeypatch.setattr(
        topic_collision,
        "seal_published_topic_resolution_row_rebounds",
        lambda *_a, **_k: False,
        raising=False,
    )

    assert historical_failed_talk_scope.annotate_sessions(
        DATE,
        state,
        include_song_rows=False,
        candidate_ids=(TARGET,),
    ) == -1
    assert {
        key: value
        for key, value in state.items()
        if key != "operator_processing_scope_runtime_block"
    } == preimage
    assert state["operator_processing_scope_runtime_block"]["reason_code"] == (
        "TOPIC_DEDUP_SESSION_ANNOTATION_TRANSITION_BLOCKED"
    )


def test_v5_unsealed_producer_pick_is_rolled_back_to_exact_queue_input(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.autoslice import historical_failed_talk_scope
    from src.autoslice import published_topic_collision as topic_collision

    state = _v5_state()
    state["published_topic_dedup_review"]["holds"] = []
    state["published_topic_resolution_recovery"] = {"sealed": "preimage"}
    queue_row = {
        "cid": TARGET,
        "segment_path": "/recordings/target.mp4",
        "start_ms": 10_000,
        "end_ms": 20_000,
        "hook": "target",
    }
    state["pending_talk"] = [queue_row]
    preimage = historical_failed_talk_scope.production_preimage(state, (TARGET,))
    assert preimage is not None
    state["pending_talk"] = []
    state["picks"].append(
        {
            "candidate_id": TARGET,
            "status": "failed",
            "reason_codes": ["PRODUCE_UNEXPECTED_EXCEPTION"],
        }
    )
    monkeypatch.setattr(
        topic_collision,
        "seal_published_topic_resolution_production_transition",
        lambda *_a, **_k: False,
        raising=False,
    )

    assert not historical_failed_talk_scope.seal_production_transition(
        DATE, state, (TARGET,), preimage
    )
    assert state["pending_talk"] == [queue_row]
    assert all(row.get("candidate_id") != TARGET for row in state["picks"])
    assert state["operator_processing_scope_runtime_block"]["reason_code"] == (
        "TOPIC_DEDUP_PRODUCTION_TRANSITION_BLOCKED"
    )


def test_v6_seals_exact_final_pick_after_runner_bundle_backfill() -> None:
    from src.autoslice import historical_failed_talk_scope
    from src.autoslice.selected_source_fact_recovery import (
        QUEUE_TO_PICK_TRANSITION,
        RECOVERY_RECEIPT_FIELD,
        validate_consumed_source_fact_recovery_receipt,
    )

    state = _v6_queued_state()
    queue_row = state["pending_talk"][0]
    original_receipt = copy.deepcopy(queue_row[RECOVERY_RECEIPT_FIELD])
    preimage = historical_failed_talk_scope.production_preimage(state, (TARGET,))
    assert preimage is not None

    final_pick = {
        "candidate_id": TARGET,
        "status": "review_ready",
        "selected_repair": True,
        "title": "producer title",
        RECOVERY_RECEIPT_FIELD: copy.deepcopy(original_receipt),
    }
    # These fields are written by the runner after the producer returns.  The
    # receipt must bind this final persisted row, not the earlier result.
    final_pick["bundle_lifecycle"] = "CURRENT"
    final_pick["bundle_compliance"] = "COMPLIANT"
    state["pending_talk"] = []
    state["picks"].append(final_pick)

    assert historical_failed_talk_scope.seal_production_transition(
        DATE, state, (TARGET,), preimage
    )
    sealed = final_pick[RECOVERY_RECEIPT_FIELD]
    assert sealed != original_receipt
    assert sealed["transitions"][-1]["kind"] == QUEUE_TO_PICK_TRANSITION
    assert sealed["current_row"] == {
        key: value
        for key, value in final_pick.items()
        if key != RECOVERY_RECEIPT_FIELD
    }
    assert validate_consumed_source_fact_recovery_receipt(
        sealed,
        candidate_id=TARGET,
        grant_id=_v6_grant()["grant_id"],
        consumed_row=final_pick,
    )


def test_v7_seals_exact_final_pick_after_runner_bundle_backfill() -> None:
    from src.autoslice import historical_failed_talk_scope
    from src.autoslice.selected_final_review_recovery import (
        QUEUE_TO_PICK_TRANSITION,
        RECOVERY_RECEIPT_FIELD,
        validate_consumed_final_review_recovery_receipt,
    )

    state = _v7_queued_state()
    queue_row = state["pending_talk"][0]
    original_receipt = copy.deepcopy(queue_row[RECOVERY_RECEIPT_FIELD])
    preimage = historical_failed_talk_scope.production_preimage(state, (TARGET,))
    assert preimage is not None
    final_pick = {
        "candidate_id": TARGET,
        "status": "review_ready",
        "selected_repair": True,
        "bundle_lifecycle": "CURRENT",
        "bundle_compliance": "COMPLIANT",
        RECOVERY_RECEIPT_FIELD: copy.deepcopy(original_receipt),
    }
    state["pending_talk"] = []
    state["picks"].append(final_pick)

    assert historical_failed_talk_scope.seal_production_transition(
        DATE, state, (TARGET,), preimage
    )
    sealed = final_pick[RECOVERY_RECEIPT_FIELD]
    assert sealed["transitions"][-1]["kind"] == QUEUE_TO_PICK_TRANSITION
    assert sealed["current_row"]["bundle_lifecycle"] == "CURRENT"
    assert validate_consumed_final_review_recovery_receipt(
        sealed,
        candidate_id=TARGET,
        grant_id=_v7_grant()["grant_id"],
        consumed_row=final_pick,
    )


def test_v7_queue_tamper_rolls_back_and_sets_typed_runtime_block() -> None:
    from src.autoslice import historical_failed_talk_scope
    from src.autoslice.selected_final_review_recovery import RECOVERY_RECEIPT_FIELD

    state = _v7_queued_state()
    preimage = historical_failed_talk_scope.production_preimage(state, (TARGET,))
    assert preimage is not None
    state["pending_talk"][0][RECOVERY_RECEIPT_FIELD]["current_row"][
        "hook"
    ] = "tampered"
    assert not historical_failed_talk_scope.seal_production_transition(
        DATE, state, (TARGET,), preimage
    )
    assert {
        key: value
        for key, value in state.items()
        if key != "operator_processing_scope_runtime_block"
    } == preimage
    runtime_block = state["operator_processing_scope_runtime_block"]
    assert runtime_block["intent"] == FINAL_REVIEW_RECOVERY_INTENT
    assert runtime_block["reason_code"] == (
        "SELECTED_FINAL_REVIEW_RECOVERY_PRODUCTION_TRANSITION_BLOCKED"
    )


@pytest.mark.parametrize(
    ("phase", "reason_code"),
    [
        ("annotation", "SELECTED_FINAL_REVIEW_RECOVERY_SESSION_ANNOTATION_BLOCKED"),
        ("scorecard", "SELECTED_FINAL_REVIEW_RECOVERY_SCORECARD_REFRESH_BLOCKED"),
        ("prioritize", "SELECTED_FINAL_REVIEW_RECOVERY_PRIORITIZE_BLOCKED"),
        ("prepare", "SELECTED_FINAL_REVIEW_RECOVERY_PRODUCTION_PREPARE_BLOCKED"),
    ],
)
def test_v7_intermediate_queue_tamper_rolls_back_before_persist(
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
    reason_code: str,
) -> None:
    from src.autoslice import historical_failed_talk_scope

    state = _v7_queued_state()
    preimage = copy.deepcopy(state)

    def tamper(*_args, **_kwargs):
        state["pending_talk"][0]["hook"] = "tampered"
        return 1

    if phase == "annotation":
        monkeypatch.setattr(runner, "annotate_state_sessions", tamper)
        result = historical_failed_talk_scope.annotate_sessions(
            DATE, state, include_song_rows=False, candidate_ids=(TARGET,)
        )
        assert result == -1
    elif phase == "scorecard":
        monkeypatch.setattr(
            semantic_chat_refresh,
            "refresh_operator_scoped_chat_scorecards",
            tamper,
        )
        assert historical_failed_talk_scope.refresh_scorecards(
            DATE, state, (TARGET,)
        ) == -1
    elif phase == "prioritize":
        monkeypatch.setattr(historical_failed_talk_scope, "reprioritize", tamper)
        assert historical_failed_talk_scope.prioritize_and_capture(
            DATE, state, (TARGET,)
        ) is None
    else:
        monkeypatch.setattr(runner, "prepare_speaker_routing", tamper)
        monkeypatch.setattr(runner, "collect_song_name_candidates", lambda *_a: [])
        assert historical_failed_talk_scope.prepare_production_context(
            DATE, state, (TARGET,)
        ) == (False, None)

    assert {
        key: value
        for key, value in state.items()
        if key != "operator_processing_scope_runtime_block"
    } == preimage
    assert state["operator_processing_scope_runtime_block"] == {
        "schema_version": "operator-processing-scope-runtime-block.v1",
        "recording_date": DATE,
        "candidate_ids": [TARGET],
        "intent": FINAL_REVIEW_RECOVERY_INTENT,
        "upload_allowed": False,
        "reason_code": reason_code,
    }


def test_v7_initial_handoff_annotation_seal_failure_restores_full_preimage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.autoslice import historical_failed_talk_scope
    from src.autoslice import published_topic_collision as topic_collision
    from src.autoslice import published_topic_final_review_handoff as handoff

    state = _v7_fresh_rejection_state()
    state["picks"][0]["session_relation_authority"] = None
    preimage = copy.deepcopy(state)
    monkeypatch.setattr(
        runner,
        "talk_failure_recovery_fingerprint",
        lambda kind, candidate_id: "sha256:" + "2" * 64
        if (kind, candidate_id) == ("subtitle_authority", TARGET)
        else None,
    )

    def annotate(*_args, **_kwargs):
        state["picks"][0].pop("session_relation_authority")
        return 1

    monkeypatch.setattr(runner, "annotate_state_sessions", annotate)
    monkeypatch.setattr(
        handoff,
        "inspect_initial_final_review_handoff",
        lambda *_a, **_k: handoff.HANDOFF_READY,
    )
    monkeypatch.setattr(
        topic_collision,
        "seal_published_topic_resolution_row_rebounds",
        lambda *_a, **_k: False,
    )

    assert historical_failed_talk_scope.annotate_sessions(
        DATE,
        state,
        include_song_rows=False,
        candidate_ids=(TARGET,),
    ) == -1
    assert {
        key: value
        for key, value in state.items()
        if key != "operator_processing_scope_runtime_block"
    } == preimage
    assert state["operator_processing_scope_runtime_block"] == {
        "schema_version": "operator-processing-scope-runtime-block.v1",
        "recording_date": DATE,
        "candidate_ids": [TARGET],
        "intent": FINAL_REVIEW_RECOVERY_INTENT,
        "upload_allowed": False,
        "reason_code": "SELECTED_FINAL_REVIEW_RECOVERY_SESSION_ANNOTATION_BLOCKED",
    }


def test_v7_no_marker_initial_session_annotation_stays_absent_and_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.autoslice import historical_failed_talk_scope
    from src.autoslice import published_topic_final_review_handoff as handoff
    from src.autoslice.selected_final_review_recovery import (
        READY_TO_REQUEUE,
        inspect_selected_final_review_recovery,
    )

    state = _v7_fresh_rejection_state()
    state["picks"][0]["session_relation_authority"] = None
    other_before = copy.deepcopy(state["picks"][1])
    songs_before = {
        key: copy.deepcopy(state[key])
        for key in (
            "pending_song",
            "song_backlog",
            "song_selection_backlog",
            "songs",
            "song_superseded_attempts",
        )
    }

    monkeypatch.setattr(
        runner,
        "talk_failure_recovery_fingerprint",
        lambda kind, candidate_id: "sha256:" + "2" * 64
        if (kind, candidate_id) == ("subtitle_authority", TARGET)
        else None,
    )

    def annotate(_date, value, **kwargs):
        assert kwargs == {
            "include_song_rows": False,
            "talk_candidate_ids": (TARGET,),
        }
        value["picks"][0].pop("session_relation_authority")
        value["segment_sessions"] = {"target": "session-target"}
        value["recording_sessions"] = ["session-target"]
        return 1

    monkeypatch.setattr(runner, "annotate_state_sessions", annotate)
    assert handoff.inspect_initial_final_review_handoff(
        state,
        candidate_id=TARGET,
        recording_date=DATE,
    ) == handoff.HANDOFF_ABSENT

    assert historical_failed_talk_scope.annotate_sessions(
        DATE,
        state,
        include_song_rows=False,
        candidate_ids=(TARGET,),
    ) == 1

    assert "session_relation_authority" not in state["picks"][0]
    assert state["picks"][1] == other_before
    assert {key: state[key] for key in songs_before} == songs_before
    assert "published_topic_resolution_recovery" not in state
    assert "operator_processing_scope_runtime_block" not in state
    assert handoff.inspect_initial_final_review_handoff(
        state,
        candidate_id=TARGET,
        recording_date=DATE,
    ) == handoff.HANDOFF_ABSENT
    assert inspect_selected_final_review_recovery(
        state,
        candidate_id=TARGET,
        grant_id=_v7_grant()["grant_id"],
    ).outcome == READY_TO_REQUEUE


@pytest.mark.parametrize("drift_target", [False, True])
def test_v7_no_marker_initial_annotation_row_drift_rolls_back(
    monkeypatch: pytest.MonkeyPatch,
    drift_target: bool,
) -> None:
    from src.autoslice import historical_failed_talk_scope

    state = _v7_fresh_rejection_state()
    state["picks"][0]["session_relation_authority"] = None
    preimage = copy.deepcopy(state)
    monkeypatch.setattr(
        runner,
        "talk_failure_recovery_fingerprint",
        lambda kind, candidate_id: "sha256:" + "2" * 64
        if (kind, candidate_id) == ("subtitle_authority", TARGET)
        else None,
    )

    def annotate(*_args, **_kwargs):
        state["picks"][0].pop("session_relation_authority")
        state["picks"][0 if drift_target else 1]["hook"] = (
            "unauthorized-row-drift"
        )
        return 1

    monkeypatch.setattr(runner, "annotate_state_sessions", annotate)

    assert historical_failed_talk_scope.annotate_sessions(
        DATE,
        state,
        include_song_rows=False,
        candidate_ids=(TARGET,),
    ) == -1
    assert {
        key: value
        for key, value in state.items()
        if key != "operator_processing_scope_runtime_block"
    } == preimage
    assert state["operator_processing_scope_runtime_block"] == {
        "schema_version": "operator-processing-scope-runtime-block.v1",
        "recording_date": DATE,
        "candidate_ids": [TARGET],
        "intent": FINAL_REVIEW_RECOVERY_INTENT,
        "upload_allowed": False,
        "reason_code": "SELECTED_FINAL_REVIEW_RECOVERY_SESSION_ANNOTATION_BLOCKED",
    }


def test_v7_no_marker_initial_annotation_exception_rolls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.autoslice import historical_failed_talk_scope

    state = _v7_fresh_rejection_state()
    state["picks"][0]["session_relation_authority"] = None
    preimage = copy.deepcopy(state)
    monkeypatch.setattr(
        runner,
        "talk_failure_recovery_fingerprint",
        lambda kind, candidate_id: "sha256:" + "2" * 64
        if (kind, candidate_id) == ("subtitle_authority", TARGET)
        else None,
    )

    def annotate(*_args, **_kwargs):
        state["picks"][0].pop("session_relation_authority")
        raise RuntimeError("session discovery failed after mutation")

    monkeypatch.setattr(runner, "annotate_state_sessions", annotate)

    assert historical_failed_talk_scope.annotate_sessions(
        DATE,
        state,
        include_song_rows=False,
        candidate_ids=(TARGET,),
    ) == -1
    assert {
        key: value
        for key, value in state.items()
        if key != "operator_processing_scope_runtime_block"
    } == preimage
    assert state["operator_processing_scope_runtime_block"]["reason_code"] == (
        "SELECTED_FINAL_REVIEW_RECOVERY_SESSION_ANNOTATION_BLOCKED"
    )


def test_v7_intermediate_declared_queue_rebounds_remain_valid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.autoslice import historical_failed_talk_scope
    from src.autoslice.selected_final_review_recovery import (
        RECOVERY_RECEIPT_FIELD,
        validate_selected_final_review_recovery_receipt,
    )

    state = _v7_queued_state()
    row = state["pending_talk"][0]
    receipt = copy.deepcopy(row[RECOVERY_RECEIPT_FIELD])

    def annotate(*_args, **_kwargs):
        row["session_id"] = "session-1576"
        return 1

    monkeypatch.setattr(runner, "annotate_state_sessions", annotate)
    assert historical_failed_talk_scope.annotate_sessions(
        DATE, state, include_song_rows=False, candidate_ids=(TARGET,)
    ) == 1

    def refresh(*_args, **_kwargs):
        row["selection_scorecard"] = {"schema_version": "test.v1"}
        return 1

    monkeypatch.setattr(
        semantic_chat_refresh, "refresh_operator_scoped_chat_scorecards", refresh
    )
    assert historical_failed_talk_scope.refresh_scorecards(
        DATE, state, (TARGET,)
    ) == 1

    def prioritize(*_args, **_kwargs):
        row["cover_diversity_slot"] = 2

    monkeypatch.setattr(historical_failed_talk_scope, "reprioritize", prioritize)
    assert historical_failed_talk_scope.prioritize_and_capture(
        DATE, state, (TARGET,)
    ) is not None

    def prepare(*_args, **_kwargs):
        row["speaker_routing_candidate"] = "豆沙"
        return {"status": "prepared"}

    monkeypatch.setattr(runner, "prepare_speaker_routing", prepare)
    monkeypatch.setattr(runner, "collect_song_name_candidates", lambda *_a: [])
    assert historical_failed_talk_scope.prepare_production_context(
        DATE, state, (TARGET,)
    )[0]
    assert row[RECOVERY_RECEIPT_FIELD] == receipt
    assert validate_selected_final_review_recovery_receipt(
        receipt,
        queued_row=row,
        candidate_id=TARGET,
        grant_id=_v7_grant()["grant_id"],
        current_fingerprint="sha256:" + "2" * 64,
        allow_queue_rebound=True,
    )


def test_missing_v7_grant_freezes_receipt_queue_before_broad_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.autoslice import historical_failed_talk_scope

    state = _v7_queued_state()
    state.pop("operator_processing_scope")
    scope = historical_failed_talk_scope.freeze(state, date=DATE)
    assert scope == ()
    monkeypatch.setattr(
        runner,
        "cover_repair_needed",
        lambda *_a: pytest.fail("empty v7 scope must not inspect broad covers"),
    )
    monkeypatch.setattr(
        runner,
        "discover_segments",
        lambda *_a: pytest.fail("empty v7 scope must not discover broad work"),
    )
    assert historical_failed_talk_scope.work_flags(
        DATE, state, automatic_maintenance=True, candidate_ids=scope
    ) == (False, False, False)
    historical_failed_talk_scope.discover(DATE, state, scope)
    assert runner.scoped_pending_talk_items(state, scope) == []


def test_v6_seals_synthetic_recoverable_producer_failure() -> None:
    from src.autoslice import historical_failed_talk_scope
    from src.autoslice.selected_source_fact_recovery import (
        RECOVERY_RECEIPT_FIELD,
        validate_consumed_source_fact_recovery_receipt,
    )

    state = _v6_queued_state()
    receipt = copy.deepcopy(
        state["pending_talk"][0][RECOVERY_RECEIPT_FIELD]
    )
    preimage = historical_failed_talk_scope.production_preimage(state, (TARGET,))
    assert preimage is not None
    failed = {
        "candidate_id": TARGET,
        "status": "failed",
        "selected_repair": True,
        "failure_kind": "provider_transient",
        "failure_stage": "produce_dispatch",
        "failure_recoverable": True,
        "reason_codes": ["PRODUCE_UNEXPECTED_EXCEPTION"],
        RECOVERY_RECEIPT_FIELD: receipt,
    }
    state["pending_talk"] = []
    state["picks"].append(failed)

    assert historical_failed_talk_scope.seal_production_transition(
        DATE, state, (TARGET,), preimage
    )
    assert validate_consumed_source_fact_recovery_receipt(
        failed[RECOVERY_RECEIPT_FIELD],
        candidate_id=TARGET,
        grant_id=_v6_grant()["grant_id"],
        consumed_row=failed,
    )


def test_v6_seals_runner_backfilled_source_fact_rejection() -> None:
    from src.autoslice import historical_failed_talk_scope
    from src.autoslice.selected_source_fact_recovery import RECOVERY_RECEIPT_FIELD

    state = _v6_queued_state()
    receipt = copy.deepcopy(
        state["pending_talk"][0][RECOVERY_RECEIPT_FIELD]
    )
    preimage = historical_failed_talk_scope.production_preimage(state, (TARGET,))
    assert preimage is not None
    result = {
        "candidate_id": TARGET,
        "status": "failed",
        "selected_repair": True,
        RECOVERY_RECEIPT_FIELD: receipt,
    }
    # Model apply_talk_backfill_rejection_policy mutating the producer result
    # before the historical scope's final commit seam.
    result.update(
        {
            "status": "candidate_rejected",
            "rejected_status": "failed",
            "rc": 1,
            "failure_kind": "story_contract",
            "failure_stage": "source_fact_repair",
            "failure_recoverable": False,
            "rejection_reason": "story_contract_unresolved_backfilled",
        }
    )
    state["pending_talk"] = []
    state["picks"].append(result)

    assert historical_failed_talk_scope.seal_production_transition(
        DATE, state, (TARGET,), preimage
    )
    assert result[RECOVERY_RECEIPT_FIELD]["current_row"]["status"] == (
        "candidate_rejected"
    )
    assert result[RECOVERY_RECEIPT_FIELD]["current_row"]["rejection_reason"] == (
        "story_contract_unresolved_backfilled"
    )


def test_v6_allows_only_declared_queue_rebound_and_rolls_back_receipt_tamper() -> None:
    from src.autoslice import historical_failed_talk_scope
    from src.autoslice.selected_source_fact_recovery import RECOVERY_RECEIPT_FIELD

    state = _v6_queued_state()
    preimage = historical_failed_talk_scope.production_preimage(state, (TARGET,))
    assert preimage is not None
    state["pending_talk"][0]["title_attempts"] = 1
    assert historical_failed_talk_scope.seal_production_transition(
        DATE, state, (TARGET,), preimage
    )

    preimage = historical_failed_talk_scope.production_preimage(state, (TARGET,))
    assert preimage is not None
    state["pending_talk"][0][RECOVERY_RECEIPT_FIELD]["current_row"][
        "hook"
    ] = "tampered"
    assert not historical_failed_talk_scope.seal_production_transition(
        DATE, state, (TARGET,), preimage
    )
    assert {
        key: value
        for key, value in state.items()
        if key != "operator_processing_scope_runtime_block"
    } == preimage
    runtime_block = state["operator_processing_scope_runtime_block"]
    assert runtime_block["intent"] == SOURCE_FACT_RECOVERY_INTENT
    assert runtime_block["upload_allowed"] is False
    assert runtime_block["reason_code"] == (
        "SELECTED_SOURCE_FACT_RECOVERY_PRODUCTION_TRANSITION_BLOCKED"
    )


def test_v5_cover_pending_uses_full_producer_requeue_and_skips_in_place_repair(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.autoslice import historical_failed_talk_scope

    state = _v5_state()
    state["published_topic_dedup_review"]["holds"] = []
    state["picks"].append(
        {
            "candidate_id": TARGET,
            "status": "media_ready_cover_pending",
            "segment": "target.mp4",
            "start_ms": 10_000,
            "end_ms": 20_000,
            "hook": "target",
            "talk_repair_retry_count": 0,
        }
    )
    queued = {
        "cid": TARGET,
        "segment_path": "/recordings/target.mp4",
        "start_ms": 10_000,
        "end_ms": 20_000,
        "hook": "target",
        "selected_repair": True,
        "recovery_source_record_sha256": "sha256:" + "a" * 64,
    }
    provider_budget_history = [
        {
            "candidate_id": TARGET,
            FINAL_REVIEW_PROVIDER_BUDGET_LEDGER_FIELD: {
                "schema_version": FINAL_REVIEW_PROVIDER_BUDGET_LEDGER_SCHEMA,
                "entries": [
                    {
                        "candidate_id": TARGET,
                        "retry_fingerprint": "sha256:" + "b" * 64,
                    }
                ],
            },
        }
    ]
    state["talk_superseded_attempts"] = provider_budget_history
    captured: dict[str, object] = {}

    def queue_item(*_args, **kwargs):
        captured.update(kwargs)
        return copy.deepcopy(queued)

    monkeypatch.setattr(
        historical_failed_talk_scope,
        "_recovery_queue_item",
        queue_item,
    )

    assert historical_failed_talk_scope._requeue_topic_cover_pending(
        DATE, state, TARGET
    ) == 1
    assert captured["provider_budget_history"] is provider_budget_history
    assert state["pending_talk"] == [queued]
    assert all(row.get("candidate_id") != TARGET for row in state["picks"])
    monkeypatch.setattr(
        runner,
        "repair_covers",
        lambda *_a, **_k: pytest.fail("v5 must not enter multi-persist cover repair"),
    )
    historical_failed_talk_scope.repair_covers(
        DATE,
        state,
        automatic_maintenance=True,
        candidate_ids=(TARGET,),
    )


def test_v7_cover_pending_requeues_transactionally_through_full_producer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.autoslice import historical_failed_talk_scope
    from src.autoslice.selected_final_review_recovery import (
        PICK_TO_QUEUE_TRANSITION,
        QUEUE_TO_PICK_TRANSITION,
        RECOVERY_RECEIPT_FIELD,
        advance_selected_final_review_recovery_receipt,
        validate_selected_final_review_recovery_receipt,
    )

    state = _v7_queued_state()
    queue_row = state["pending_talk"].pop()
    cover_pick = {
        "candidate_id": TARGET,
        "status": "media_ready_cover_pending",
        "selected_repair": True,
        "segment": f"{TARGET}.mp4",
        "start_ms": 10_000,
        "end_ms": 20_000,
        "hook": TARGET,
        # 1576 enters v7 from the historical count-5 rejection and its first
        # fingerprint-driven retry is count 6.  Cover continuation must not be
        # stranded behind the generic lifetime cap (currently 3).
        "talk_repair_retry_count": 6,
    }
    receipt = advance_selected_final_review_recovery_receipt(
        queue_row[RECOVERY_RECEIPT_FIELD],
        from_row=queue_row,
        to_row=cover_pick,
        candidate_id=TARGET,
        grant_id=_v7_grant()["grant_id"],
        transition_kind=QUEUE_TO_PICK_TRANSITION,
    )
    cover_pick[RECOVERY_RECEIPT_FIELD] = receipt
    state["picks"].append(cover_pick)
    queued = {
        "cid": TARGET,
        "segment_path": f"/recordings/{TARGET}.mp4",
        "start_ms": 10_000,
        "end_ms": 20_000,
        "hook": TARGET,
        "selected_repair": True,
        "talk_repair_retry_count": 7,
        "retry_reason": "selected_final_review_cover_pending_full_producer_retry",
    }

    monkeypatch.setattr(
        historical_failed_talk_scope,
        "_recovery_queue_item",
        lambda *_a, **_k: copy.deepcopy(queued),
    )
    assert historical_failed_talk_scope._requeue_final_review_cover_pending(
        DATE, state, candidate_ids=(TARGET,)
    ) == 1
    rebound = state["pending_talk"][0]
    next_receipt = rebound[RECOVERY_RECEIPT_FIELD]
    assert next_receipt["transitions"][-1]["kind"] == PICK_TO_QUEUE_TRANSITION
    assert validate_selected_final_review_recovery_receipt(
        next_receipt,
        queued_row=rebound,
        candidate_id=TARGET,
        grant_id=_v7_grant()["grant_id"],
        current_fingerprint="sha256:" + "2" * 64,
    )
    monkeypatch.setattr(
        runner,
        "repair_covers",
        lambda *_a, **_k: pytest.fail("v7 must not use multi-persist cover repair"),
    )
    historical_failed_talk_scope.repair_covers(
        DATE,
        state,
        automatic_maintenance=True,
        candidate_ids=(TARGET,),
    )


def test_v6_cover_pending_requeues_transactionally_and_skips_in_place_repair(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.autoslice import historical_failed_talk_scope
    from src.autoslice.selected_source_fact_recovery import (
        PICK_TO_QUEUE_TRANSITION,
        QUEUE_TO_PICK_TRANSITION,
        RECOVERY_RECEIPT_FIELD,
        advance_selected_source_fact_recovery_receipt,
        validate_selected_source_fact_recovery_receipt,
    )

    state = _v6_queued_state()
    queue_row = state["pending_talk"].pop()
    receipt = queue_row[RECOVERY_RECEIPT_FIELD]
    cover_pick = {
        "candidate_id": TARGET,
        "status": "media_ready_cover_pending",
        "selected_repair": True,
        "segment": f"{TARGET}.mp4",
        "start_ms": 10_000,
        "end_ms": 20_000,
        "hook": TARGET,
        "talk_repair_retry_count": 1,
    }
    receipt = advance_selected_source_fact_recovery_receipt(
        receipt,
        from_row=queue_row,
        to_row=cover_pick,
        candidate_id=TARGET,
        grant_id=_v6_grant()["grant_id"],
        transition_kind=QUEUE_TO_PICK_TRANSITION,
    )
    cover_pick[RECOVERY_RECEIPT_FIELD] = receipt
    state["picks"].append(cover_pick)
    queued = {
        "cid": TARGET,
        "segment_path": f"/recordings/{TARGET}.mp4",
        "start_ms": 10_000,
        "end_ms": 20_000,
        "hook": TARGET,
        "selected_repair": True,
        "talk_repair_retry_count": 2,
        "retry_reason": "selected_source_fact_cover_pending_full_producer_retry",
        "recovery_source_record_sha256": "sha256:" + "a" * 64,
    }
    provider_budget_history = [
        {
            "candidate_id": TARGET,
            FINAL_REVIEW_PROVIDER_BUDGET_LEDGER_FIELD: {
                "schema_version": FINAL_REVIEW_PROVIDER_BUDGET_LEDGER_SCHEMA,
                "entries": [
                    {
                        "candidate_id": TARGET,
                        "retry_fingerprint": "sha256:" + "b" * 64,
                    }
                ],
            },
        }
    ]
    state["talk_superseded_attempts"] = provider_budget_history
    captured: dict[str, object] = {}

    def queue_item(*_args, **kwargs):
        captured.update(kwargs)
        return copy.deepcopy(queued)

    monkeypatch.setattr(
        historical_failed_talk_scope,
        "_recovery_queue_item",
        queue_item,
    )
    monkeypatch.setattr(
        historical_failed_talk_scope,
        "maintain_delivery_recovery_scope",
        lambda *_a, **_k: (0, 0, 0, 0, False),
    )

    assert historical_failed_talk_scope.freeze(state, date=DATE) == (TARGET,)
    song_conflict = copy.deepcopy(state)
    song_conflict["pending_song"] = [{"candidate_id": TARGET}]
    assert historical_failed_talk_scope.freeze(song_conflict, date=DATE) == ()
    assert historical_failed_talk_scope.maintain(
        DATE,
        state,
        automatic_maintenance=True,
        candidate_ids=(TARGET,),
    ) == (0, 0, 1, 0, False)
    assert captured["provider_budget_history"] is provider_budget_history
    assert all(row.get("candidate_id") != TARGET for row in state["picks"])
    assert state["pending_talk"][0]["cid"] == TARGET
    next_receipt = state["pending_talk"][0][RECOVERY_RECEIPT_FIELD]
    assert next_receipt["transitions"][-1]["kind"] == PICK_TO_QUEUE_TRANSITION
    assert validate_selected_source_fact_recovery_receipt(
        next_receipt,
        queued_row=state["pending_talk"][0],
        candidate_id=TARGET,
        grant_id=_v6_grant()["grant_id"],
        current_fingerprint="sha256:" + "2" * 64,
    )

    monkeypatch.setattr(
        runner,
        "repair_covers",
        lambda *_a, **_k: pytest.fail("v6 must not enter multi-persist cover repair"),
    )
    historical_failed_talk_scope.repair_covers(
        DATE,
        state,
        automatic_maintenance=True,
        candidate_ids=(TARGET,),
    )


@pytest.mark.parametrize("failure_mode", ["false", "exception", "post_blocked"])
def test_v5_failed_release_records_typed_block_rolls_back_and_runs_no_other_work(
    monkeypatch: pytest.MonkeyPatch, failure_mode: str
) -> None:
    from src.autoslice import historical_failed_talk_scope
    from src.autoslice import published_topic_collision as topic_collision

    state = _v5_state()
    preimage = copy.deepcopy(state)

    inspection_count = 0

    def inspect(*_args, **_kwargs) -> str:
        nonlocal inspection_count
        inspection_count += 1
        if failure_mode == "post_blocked" and inspection_count > 1:
            return "BLOCKED"
        return "READY_TO_RELEASE"

    monkeypatch.setattr(
        topic_collision,
        "inspect_published_topic_resolution_recovery",
        inspect,
        raising=False,
    )
    def fail_after_partial_mutation(value: dict, _candidate_id: str) -> bool:
        value["pending_talk"].append({"cid": "must_be_rolled_back"})
        if failure_mode == "exception":
            raise RuntimeError("repository authority became unavailable")
        return failure_mode == "post_blocked"

    monkeypatch.setattr(
        topic_collision,
        "release_resolved_published_topic_hold",
        fail_after_partial_mutation,
    )
    monkeypatch.setattr(
        historical_failed_talk_scope,
        "maintain_delivery_recovery_scope",
        lambda *_a, **_k: pytest.fail("failed v5 release must not enter generic maintenance"),
    )

    result = historical_failed_talk_scope.maintain(
        DATE,
        state,
        automatic_maintenance=True,
        candidate_ids=(TARGET,),
    )

    assert result == (0, 0, 0, 0, False)
    assert {
        key: value
        for key, value in state.items()
        if key != "operator_processing_scope_runtime_block"
    } == preimage
    assert state["operator_processing_scope_runtime_block"] == {
        "schema_version": "operator-processing-scope-runtime-block.v1",
        "recording_date": DATE,
        "candidate_ids": [TARGET],
        "intent": TOPIC_HOLD_RECOVERY_INTENT,
        "upload_allowed": False,
        "reason_code": "TOPIC_DEDUP_HOLD_RELEASE_FAILED",
    }
    monkeypatch.setattr(
        semantic_chat_refresh,
        "runner_date_work_flags",
        lambda *_a, **_k: pytest.fail("blocked v5 must not probe or run follow-on work"),
    )
    assert historical_failed_talk_scope.work_flags(
        DATE,
        state,
        automatic_maintenance=True,
        candidate_ids=(TARGET,),
    ) == (False, False, False)


@pytest.mark.parametrize("drift", ["marker", "queued_row", "resolution"])
def test_v5_drifted_queued_release_blocks_before_any_release_or_generic_work(
    monkeypatch: pytest.MonkeyPatch, drift: str
) -> None:
    from src.autoslice import historical_failed_talk_scope
    from src.autoslice import published_topic_collision as topic_collision

    state = _v5_state()
    state["published_topic_dedup_review"]["holds"] = []
    state["pending_talk"] = [{"cid": TARGET, "drift": drift}]
    state["published_topic_resolution_recovery"] = {
        "schema_version": "published-topic-resolution-recovery-marker.v1",
        "drift": drift,
    }
    preimage = copy.deepcopy(state)
    monkeypatch.setattr(
        topic_collision,
        "inspect_published_topic_resolution_recovery",
        lambda *_a, **_k: "BLOCKED",
        raising=False,
    )
    monkeypatch.setattr(
        topic_collision,
        "release_resolved_published_topic_hold",
        lambda *_a, **_k: pytest.fail("blocked queued v5 must not call one-shot release"),
    )
    monkeypatch.setattr(
        historical_failed_talk_scope,
        "maintain_delivery_recovery_scope",
        lambda *_a, **_k: pytest.fail("blocked queued v5 must not enter generic maintenance"),
    )

    result = historical_failed_talk_scope.maintain(
        DATE,
        state,
        automatic_maintenance=True,
        candidate_ids=(TARGET,),
    )

    assert result == (0, 0, 0, 0, False)
    assert {
        key: value
        for key, value in state.items()
        if key != "operator_processing_scope_runtime_block"
    } == preimage
    assert state["operator_processing_scope_runtime_block"]["reason_code"] == (
        "TOPIC_DEDUP_RELEASE_STATE_BLOCKED"
    )


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
            "allow_published_topic_review": True,
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


def test_process_date_v5_seals_synthetic_pick_before_any_state_persist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The real runner commit point cannot persist an unbound crash result."""

    from src.autoslice import historical_failed_talk_scope

    queue_row = {
        "cid": TARGET,
        "segment_path": "/recordings/target.mp4",
        "start_ms": 10_000,
        "end_ms": 20_000,
        "hook": "target",
        "selection_scorecard": {"schema_version": "test.v1"},
    }
    state = _v5_state()
    state["published_topic_dedup_review"]["holds"] = []
    state["published_topic_resolution_recovery"] = {"head": "queue"}
    state["pending_talk"] = [queue_row]
    persisted: list[dict] = []
    calls: list[str] = []

    monkeypatch.setattr(historical_failed_talk_scope, "freeze", lambda *_a, **_k: (TARGET,))
    monkeypatch.setattr(
        historical_failed_talk_scope, "annotate_sessions", lambda *_a, **_k: 0
    )
    monkeypatch.setattr(
        historical_failed_talk_scope,
        "maintain",
        lambda *_a, **_k: (0, 0, 0, 0, False),
    )
    monkeypatch.setattr(
        historical_failed_talk_scope,
        "work_flags",
        lambda *_a, **_k: (False, True, False),
    )
    monkeypatch.setattr(historical_failed_talk_scope, "discover", lambda *_a, **_k: None)
    monkeypatch.setattr(
        historical_failed_talk_scope,
        "refresh_scorecards",
        lambda *_a, **_k: 0,
    )
    monkeypatch.setattr(
        historical_failed_talk_scope,
        "prioritize_and_capture",
        lambda *_a, **_k: [copy.deepcopy(queue_row)],
    )
    monkeypatch.setattr(
        historical_failed_talk_scope,
        "prepare_production_context",
        lambda *_a, **_k: (True, None),
    )

    def capture_preimage(value, _ids):
        calls.append("preimage")
        return copy.deepcopy(value)

    def seal(_date, value, _ids, preimage):
        calls.append("seal")
        assert preimage["pending_talk"] == [queue_row]
        assert value["pending_talk"] == []
        target = next(row for row in value["picks"] if row.get("candidate_id") == TARGET)
        assert target["reason_codes"] == ["PRODUCE_UNEXPECTED_EXCEPTION"]
        assert target["failure_recoverable"] is True
        value["published_topic_resolution_recovery"] = {"head": "sealed-pick"}
        return True

    monkeypatch.setattr(historical_failed_talk_scope, "production_preimage", capture_preimage)
    monkeypatch.setattr(historical_failed_talk_scope, "seal_production_transition", seal)
    monkeypatch.setattr(historical_failed_talk_scope, "repair_covers", lambda *_a, **_k: None)
    monkeypatch.setattr(runner, "read_state", lambda _date: state)
    monkeypatch.setattr(runner, "runtime_health_error", lambda: None)
    monkeypatch.setattr(runner, "recover_finalized_legacy_hls", lambda *_a, **_k: [])
    monkeypatch.setattr(
        runner,
        "audit_finalized_recording_inventory",
        lambda *_a, **_k: {"can_select": True, "issues": []},
    )
    monkeypatch.setattr(runner, "AUTOMATIC_MAINTENANCE_NOT_BEFORE", DATE)
    monkeypatch.setattr(runner, "cpa_healthy", lambda: True)
    monkeypatch.setattr(runner, "session_sealed", lambda *_a, **_k: True)
    monkeypatch.setattr(runner, "split_produce_blocked_talk_items", lambda rows: (rows, []))
    def crash_in_real_batch(_date, _item):
        raise TimeoutError("synthetic producer crash")

    monkeypatch.setattr(runner, "produce_talk", crash_in_real_batch)
    monkeypatch.setattr(runner, "talk_pipeline_fingerprint", lambda _cid: "sha256:test")
    monkeypatch.setattr(runner, "live_hold_recheck", lambda: False)
    monkeypatch.setattr(runner, "apply_talk_backfill_rejection_policy", lambda *_a, **_k: False)

    def write_state(_date, value):
        target_picks = [
            row
            for row in value.get("picks", [])
            if isinstance(row, dict) and row.get("candidate_id") == TARGET
        ]
        if target_picks:
            assert value["published_topic_resolution_recovery"] == {
                "head": "sealed-pick"
            }
        persisted.append(copy.deepcopy(value))

    monkeypatch.setattr(runner, "write_state", write_state)
    monkeypatch.setattr(runner, "write_reports", lambda *_a, **_k: None)
    monkeypatch.setattr(runner, "queue_collab_evidence_capture", lambda *_a, **_k: None)
    monkeypatch.setattr(runner, "log", lambda *_a, **_k: None)
    monkeypatch.setattr(
        runner,
        "_project_terminal_batch_state",
        lambda value, **_k: {
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

    runner.process_date(DATE)

    assert calls == ["preimage", "seal"]
    assert persisted
    assert state["published_topic_resolution_recovery"] == {"head": "sealed-pick"}


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
