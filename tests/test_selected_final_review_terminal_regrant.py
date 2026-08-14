from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import scripts.free_session_autoslice as runner
from src.autoslice import historical_failed_talk_scope
from src.autoslice import operator_processing_scope
from src.autoslice import produce_dispatch
from src.autoslice import published_topic_final_review_handoff as topic_handoff
from src.autoslice import selected_final_review_terminal_regrant as regrant
from src.autoslice import (
    selected_final_review_terminal_regrant_runtime as regrant_runtime,
)
from src.autoslice import talk_lane
from src.autoslice.published_topic_recovery_lineage import (
    RECOVERY_REBOUND_SESSION_ANNOTATION,
)
from src.autoslice.selected_final_review_recovery import (
    QUEUE_TO_PICK_TRANSITION as V7_QUEUE_TO_PICK,
    RECOVERY_RECEIPT_FIELD as V7_RECEIPT_FIELD,
    advance_selected_final_review_recovery_receipt,
    build_selected_final_review_recovery_receipt,
)
from src.autoslice.selected_final_review_terminal_regrant import (
    BLOCKED,
    CONVERGED,
    OUTSTANDING,
    QUEUE_TO_PICK_TRANSITION,
    READY_TO_REQUEUE,
    RECOVERY_RECEIPT_FIELD,
    advance_selected_final_review_terminal_regrant_receipt,
    build_selected_final_review_terminal_regrant_receipt,
    inspect_selected_final_review_terminal_regrant,
    is_selected_final_review_terminal_rejection,
    valid_terminal_regrant_grant,
    validate_initial_terminal_regrant_transition,
    validate_selected_final_review_terminal_regrant_receipt,
    validate_terminal_regrant_descendant_state,
)


CID = "auto_213135_806_1068"
DATE = "2026-08-08"
OLD_V7_GRANT = "recover-806-v7"
V8_GRANT = "recover-806-v8"
V7_INITIAL = "sha256:" + "0" * 64
RECORDED = "sha256:" + "1" * 64
CURRENT = "sha256:" + "2" * 64


def _terminal_rejection(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "candidate_id": CID,
        "status": "candidate_rejected",
        "rejected_status": "failed",
        "rc": 1,
        "selected_repair": True,
        "failure_kind": "subtitle_authority",
        "failure_stage": "chat_authority_final_artifact",
        "failure_recoverable": False,
        "rejection_reason": "subtitle_authority_unresolved_backfilled",
        "failure_recovery_fingerprint": RECORDED,
        "selected_final_review_recovery": {},
    }
    row.update(overrides)
    return row


def test_exact_terminal_final_artifact_rejection_predicate() -> None:
    assert is_selected_final_review_terminal_rejection(_terminal_rejection())
    for field, value in (
        ("status", "failed"),
        ("rejected_status", "review_ready"),
        ("rc", True),
        ("rc", 0),
        ("selected_repair", False),
        ("failure_kind", "story_contract"),
        ("failure_stage", "final_review_findings"),
        ("failure_recoverable", True),
        ("rejection_reason", "story_contract_unresolved_backfilled"),
    ):
        assert not is_selected_final_review_terminal_rejection(
            _terminal_rejection(**{field: value})
        )


def _parent_terminal(**overrides: object) -> dict[str, object]:
    initial_rejection = {
        **_terminal_rejection(),
        "failure_stage": "final_review_findings",
        "failure_recovery_fingerprint": V7_INITIAL,
    }
    initial_rejection.update(copy.deepcopy(overrides))
    initial_rejection.pop(V7_RECEIPT_FIELD)
    queue = {
        "cid": CID,
        "segment_path": "/recordings/806.mp4",
        "start_ms": 806_000,
        "end_ms": 1_068_000,
        "selected_repair": True,
    }
    initial = build_selected_final_review_recovery_receipt(
        old_row=initial_rejection,
        queued_row=queue,
        candidate_id=CID,
        grant_id=OLD_V7_GRANT,
        current_fingerprint=RECORDED,
    )
    terminal_body = _terminal_rejection()
    terminal_body.update(copy.deepcopy(overrides))
    terminal_body.pop(V7_RECEIPT_FIELD)
    consumed = advance_selected_final_review_recovery_receipt(
        initial,
        from_row={**queue, V7_RECEIPT_FIELD: initial},
        to_row=terminal_body,
        candidate_id=CID,
        grant_id=OLD_V7_GRANT,
        transition_kind=V7_QUEUE_TO_PICK,
    )
    return {**terminal_body, V7_RECEIPT_FIELD: consumed}


def _grant(parent: dict[str, object], marker: dict[str, object], **overrides: object):
    parent_receipt = parent[V7_RECEIPT_FIELD]
    grant: dict[str, object] = {
        "schema_version": "operator-processing-scope-grant.v8",
        "grant_id": V8_GRANT,
        "recording_date": DATE,
        "reason": "one fresh no-upload retry after the final-artifact verifier fix",
        "candidate_ids": [CID],
        "user_authorization": {
            "quote": "继续完成806这一次新的终稿重试。",
            "timestamp": "2026-08-14T15:00:00Z",
        },
        "expires_at": "2026-08-14T20:00:00Z",
        "intent": "REGRANT_NAMED_SELECTED_FINAL_REVIEW_TERMINAL_REJECTION",
        "upload_allowed": False,
        "attempt_limit": 1,
        "predecessor": {
            "prior_operator_scope_grant_id": OLD_V7_GRANT,
            "parent_recovery_receipt_sha256": regrant.canonical_sha256(parent_receipt),
            "terminal_row_sha256": regrant.canonical_sha256(parent),
            "terminal_marker_sha256": regrant.canonical_sha256(marker),
            "recorded_failure_recovery_fingerprint": RECORDED,
            "current_failure_recovery_fingerprint": CURRENT,
        },
    }
    grant.update(overrides)
    return grant


def _fresh_grant(parent: dict[str, object], marker: dict[str, object]) -> dict[str, object]:
    now = datetime.now(timezone.utc)
    return _grant(
        parent,
        marker,
        user_authorization={
            "quote": "继续执行这一次新的806终稿验证。",
            "timestamp": (now - timedelta(minutes=1)).isoformat(),
        },
        expires_at=(now + timedelta(hours=5)).isoformat(),
    )


def _queue(parent: dict[str, object], **overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "cid": CID,
        "segment_path": "/recordings/806.mp4",
        "start_ms": 806_000,
        "end_ms": 1_068_000,
        "selected_repair": True,
        V7_RECEIPT_FIELD: copy.deepcopy(parent[V7_RECEIPT_FIELD]),
    }
    row.update(overrides)
    return row


def _state(row: dict[str, object], collection: str = "picks") -> dict[str, object]:
    state: dict[str, object] = {
        "upload_allowed": False,
        "pending_talk": [],
        "talk_backlog": [],
        "picks": [],
        "talk_below_confidence_threshold": [],
        "talk_superseded_attempts": [],
        "pending_song": [],
        "song_backlog": [],
        "song_selection_backlog": [],
        "songs": [],
        "song_superseded_attempts": [],
    }
    state[collection] = [row]
    return state


def _queued_state(
    parent: dict[str, object],
    marker: dict[str, object],
    grant: dict[str, object],
) -> dict[str, object]:
    queue = _queue(
        parent,
        retry_reason="selected_final_review_terminal_regrant",
        talk_repair_retry_count=8,
    )
    receipt = build_selected_final_review_terminal_regrant_receipt(
        state=_state(parent),
        old_row=parent,
        queued_row=queue,
        candidate_id=CID,
        grant=grant,
    )
    state = _state({**queue, RECOVERY_RECEIPT_FIELD: receipt}, "pending_talk")
    state["operator_processing_scope"] = grant
    state["published_topic_resolution_recovery"] = {"frozen": marker}
    state["talk_superseded_attempts"] = [
        {
            "candidate_id": CID,
            V7_RECEIPT_FIELD: copy.deepcopy(parent[V7_RECEIPT_FIELD]),
            RECOVERY_RECEIPT_FIELD: copy.deepcopy(receipt),
        }
    ]
    return state


def test_v8_grant_is_single_attempt_short_lived_and_exact() -> None:
    parent = _parent_terminal()
    marker = {"marker": "terminal-v5-to-v7"}
    grant = _grant(parent, marker)
    assert valid_terminal_regrant_grant(grant, candidate_id=CID, recording_date=DATE)
    for drift in (
        {"attempt_limit": 2},
        {"attempt_limit": True},
        {"upload_allowed": True},
        {"candidate_ids": [CID, "auto_other"]},
        {"expires_at": "2026-08-14T22:00:01Z"},
    ):
        assert not valid_terminal_regrant_grant(
            {**grant, **drift}, candidate_id=CID, recording_date=DATE
        )


def test_v8_receipt_freezes_parent_and_allows_only_queue_to_pick(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent = _parent_terminal()
    marker = {"marker": "terminal-v5-to-v7"}
    grant = _grant(parent, marker)
    state = _state(parent)
    monkeypatch.setattr(regrant, "_marker_for", lambda *_args: marker)
    monkeypatch.setattr(regrant, "_parent_authority_valid", lambda *_args, **_kw: True)
    queue = _queue(parent)
    receipt = build_selected_final_review_terminal_regrant_receipt(
        state=state,
        old_row=parent,
        queued_row=queue,
        candidate_id=CID,
        grant=grant,
    )
    queued = {**queue, RECOVERY_RECEIPT_FIELD: receipt}
    assert validate_selected_final_review_terminal_regrant_receipt(
        receipt,
        current_row=queued,
        candidate_id=CID,
        grant=grant,
    )
    pick_body = {
        **queue,
        "candidate_id": CID,
        "status": "candidate_rejected",
        "failure_kind": "subtitle_authority",
        "failure_stage": "chat_authority_final_artifact",
        "failure_recoverable": False,
    }
    consumed = advance_selected_final_review_terminal_regrant_receipt(
        receipt,
        from_row=queued,
        to_row=pick_body,
        candidate_id=CID,
        grant=grant,
        transition_kind=QUEUE_TO_PICK_TRANSITION,
    )
    pick = {**pick_body, RECOVERY_RECEIPT_FIELD: consumed}
    assert validate_selected_final_review_terminal_regrant_receipt(
        consumed,
        current_row=pick,
        candidate_id=CID,
        grant=grant,
    )
    assert consumed["parent_final_review_recovery_receipt"] == parent[V7_RECEIPT_FIELD]
    assert pick[V7_RECEIPT_FIELD] == parent[V7_RECEIPT_FIELD]
    with pytest.raises(ValueError):
        advance_selected_final_review_terminal_regrant_receipt(
            consumed,
            from_row=pick,
            to_row=queue,
            candidate_id=CID,
            grant=grant,
            transition_kind="V8_PICK_TO_QUEUE",
        )
    tampered = copy.deepcopy(consumed)
    tampered["parent_final_review_recovery_receipt"]["receipt_sha256"] = "sha256:" + "f" * 64
    assert not validate_selected_final_review_terminal_regrant_receipt(
        tampered,
        current_row={**pick_body, RECOVERY_RECEIPT_FIELD: tampered},
        candidate_id=CID,
        grant=grant,
    )


def test_v8_initial_changed_same_and_consumed_are_one_shot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent = _parent_terminal()
    marker = {"marker": "terminal-v5-to-v7"}
    grant = _grant(parent, marker)
    state = _state(parent)
    monkeypatch.setattr(regrant, "_parent_authority_valid", lambda *_a, **_kw: True)
    assert (
        inspect_selected_final_review_terminal_regrant(state, candidate_id=CID, grant=grant).outcome
        == READY_TO_REQUEUE
    )
    same = copy.deepcopy(grant)
    same["predecessor"]["current_failure_recovery_fingerprint"] = RECORDED
    assert (
        inspect_selected_final_review_terminal_regrant(state, candidate_id=CID, grant=same).outcome
        == CONVERGED
    )

    monkeypatch.setattr(regrant, "_marker_for", lambda *_args: marker)
    monkeypatch.setattr(
        runner,
        "talk_failure_recovery_fingerprint",
        lambda kind, cid: (
            CURRENT
            if (kind, cid) == ("subtitle_authority", CID)
            else pytest.fail("unrelated fingerprint requested")
        ),
    )
    monkeypatch.setattr(
        "src.autoslice.published_topic_final_review_handoff.validate_terminal_handoff_state",
        lambda *_args: True,
    )
    queue = _queue(parent)
    receipt = build_selected_final_review_terminal_regrant_receipt(
        state=state,
        old_row=parent,
        queued_row=queue,
        candidate_id=CID,
        grant=grant,
    )
    queued = {**queue, RECOVERY_RECEIPT_FIELD: receipt}
    queued_state = _state(queued, "pending_talk")
    assert (
        inspect_selected_final_review_terminal_regrant(
            queued_state, candidate_id=CID, grant=grant
        ).outcome
        == OUTSTANDING
    )
    pick_body = {**queue, "candidate_id": CID, "status": "review_ready"}
    consumed = advance_selected_final_review_terminal_regrant_receipt(
        receipt,
        from_row=queued,
        to_row=pick_body,
        candidate_id=CID,
        grant=grant,
        transition_kind=QUEUE_TO_PICK_TRANSITION,
    )
    pick_state = _state({**pick_body, RECOVERY_RECEIPT_FIELD: consumed}, "picks")
    assert (
        inspect_selected_final_review_terminal_regrant(
            pick_state, candidate_id=CID, grant=grant
        ).outcome
        == CONVERGED
    )
    pick_state["pending_song"] = [{"candidate_id": CID}]
    assert (
        inspect_selected_final_review_terminal_regrant(
            pick_state, candidate_id=CID, grant=grant
        ).outcome
        == BLOCKED
    )


def _reseal_receipt(receipt: dict[str, object], grant: dict[str, object]) -> dict[str, object]:
    result = copy.deepcopy(receipt)
    result["operator_scope_grant_id"] = grant["grant_id"]
    result["operator_scope_grant"] = copy.deepcopy(grant)
    result["operator_scope_grant_sha256"] = regrant.canonical_sha256(grant)
    result["recorded_failure_recovery_fingerprint"] = grant["predecessor"][
        "recorded_failure_recovery_fingerprint"
    ]
    result["current_failure_recovery_fingerprint"] = grant["predecessor"][
        "current_failure_recovery_fingerprint"
    ]
    result["terminal_marker_sha256"] = grant["predecessor"]["terminal_marker_sha256"]
    result["receipt_sha256"] = regrant.canonical_sha256(
        {key: value for key, value in result.items() if key != "receipt_sha256"}
    )
    return result


def test_resealed_receipt_cannot_rebind_its_parent_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent = _parent_terminal()
    marker = {"marker": "terminal-v5-to-v7"}
    grant = _grant(parent, marker)
    monkeypatch.setattr(regrant, "_marker_for", lambda *_args: marker)
    monkeypatch.setattr(regrant, "_parent_authority_valid", lambda *_a, **_k: True)
    queue = _queue(parent)
    receipt = build_selected_final_review_terminal_regrant_receipt(
        state=_state(parent),
        old_row=parent,
        queued_row=queue,
        candidate_id=CID,
        grant=grant,
    )
    for field, value in (
        ("prior_operator_scope_grant_id", "unrelated-v7"),
        ("parent_recovery_receipt_sha256", "sha256:" + "a" * 64),
        ("terminal_row_sha256", "sha256:" + "b" * 64),
        ("recorded_failure_recovery_fingerprint", "sha256:" + "c" * 64),
    ):
        drifted_grant = copy.deepcopy(grant)
        drifted_grant["predecessor"][field] = value
        drifted = _reseal_receipt(receipt, drifted_grant)
        assert not validate_selected_final_review_terminal_regrant_receipt(
            drifted,
            current_row={**queue, RECOVERY_RECEIPT_FIELD: drifted},
            candidate_id=CID,
            grant=drifted_grant,
        )
    reused = copy.deepcopy(grant)
    reused["grant_id"] = OLD_V7_GRANT
    reused_receipt = _reseal_receipt(receipt, reused)
    assert not validate_selected_final_review_terminal_regrant_receipt(
        reused_receipt,
        current_row={**queue, RECOVERY_RECEIPT_FIELD: reused_receipt},
        candidate_id=CID,
        grant=reused,
    )


def test_v8_queue_rejects_provider_budget_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent = _parent_terminal()
    marker = {"marker": "terminal-v5-to-v7"}
    grant = _grant(parent, marker)
    monkeypatch.setattr(regrant, "_marker_for", lambda *_args: marker)
    monkeypatch.setattr(regrant, "_parent_authority_valid", lambda *_a, **_k: True)
    queue = _queue(
        parent,
        final_review_provider_budget_retry_ledger={"consumed": True},
    )
    with pytest.raises(ValueError):
        build_selected_final_review_terminal_regrant_receipt(
            state=_state(parent),
            old_row=parent,
            queued_row=queue,
            candidate_id=CID,
            grant=grant,
        )


@pytest.mark.parametrize("status", ["title_failed", "unreadable_cue_review_required"])
def test_every_real_terminal_producer_status_consumes_v8(
    monkeypatch: pytest.MonkeyPatch, status: str
) -> None:
    parent = _parent_terminal()
    marker = {"marker": "terminal-v5-to-v7"}
    grant = _grant(parent, marker)
    monkeypatch.setattr(regrant, "_marker_for", lambda *_args: marker)
    monkeypatch.setattr(regrant, "_parent_authority_valid", lambda *_a, **_k: True)
    queue = _queue(parent)
    receipt = build_selected_final_review_terminal_regrant_receipt(
        state=_state(parent),
        old_row=parent,
        queued_row=queue,
        candidate_id=CID,
        grant=grant,
    )
    queued = {**queue, RECOVERY_RECEIPT_FIELD: receipt}
    pick_body = {**queue, "candidate_id": CID, "status": status}
    consumed = advance_selected_final_review_terminal_regrant_receipt(
        receipt,
        from_row=queued,
        to_row=pick_body,
        candidate_id=CID,
        grant=grant,
        transition_kind=QUEUE_TO_PICK_TRANSITION,
    )
    assert validate_selected_final_review_terminal_regrant_receipt(
        consumed,
        current_row={**pick_body, RECOVERY_RECEIPT_FIELD: consumed},
        candidate_id=CID,
        grant=grant,
    )


def test_descendant_binds_receipt_head_to_collection_and_handles_bad_collections(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent = _parent_terminal()
    marker = {"marker": "terminal-v5-to-v7"}
    grant = _grant(parent, marker)
    monkeypatch.setattr(regrant, "_marker_for", lambda *_args: marker)
    monkeypatch.setattr(regrant, "_parent_authority_valid", lambda *_a, **_k: True)
    monkeypatch.setattr(regrant, "_marker_regrant_relation_valid", lambda *_a: True)
    queue = _queue(parent)
    receipt = build_selected_final_review_terminal_regrant_receipt(
        state=_state(parent),
        old_row=parent,
        queued_row=queue,
        candidate_id=CID,
        grant=grant,
    )
    wrong_collection = _state({**queue, RECOVERY_RECEIPT_FIELD: receipt}, "picks")
    assert not validate_terminal_regrant_descendant_state(
        wrong_collection,
        CID,
        marker,
        parent_validator=lambda *_a: True,
    )
    malformed = _state({**queue, RECOVERY_RECEIPT_FIELD: receipt}, "pending_talk")
    malformed["talk_backlog"] = 7
    assert not validate_terminal_regrant_descendant_state(
        malformed,
        CID,
        marker,
        parent_validator=lambda *_a: True,
    )


def test_foreign_v8_history_does_not_block_initial_regrant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent = _parent_terminal()
    grant = _grant(parent, {"marker": "terminal-v5-to-v7"})
    state = _state(parent)
    state["talk_superseded_attempts"] = [
        {
            "candidate_id": "auto_foreign",
            RECOVERY_RECEIPT_FIELD: {"malformed": True},
        }
    ]
    monkeypatch.setattr(regrant, "_parent_authority_valid", lambda *_a, **_k: True)
    assert (
        inspect_selected_final_review_terminal_regrant(state, candidate_id=CID, grant=grant).outcome
        == READY_TO_REQUEUE
    )


def test_talk_backlog_is_a_valid_initial_queue_destination(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent = _parent_terminal()
    marker = {"marker": "terminal-v5-to-v7"}
    grant = _grant(parent, marker)
    monkeypatch.setattr(regrant, "_marker_for", lambda *_args: marker)
    monkeypatch.setattr(regrant, "_parent_authority_valid", lambda *_a, **_k: True)
    queue = _queue(parent)
    receipt = build_selected_final_review_terminal_regrant_receipt(
        state=_state(parent),
        old_row=parent,
        queued_row=queue,
        candidate_id=CID,
        grant=grant,
    )
    pre = _state(parent)
    pre["operator_processing_scope"] = grant
    post = _state({**queue, RECOVERY_RECEIPT_FIELD: receipt}, "talk_backlog")
    post["operator_processing_scope"] = grant
    post["talk_superseded_attempts"] = [
        {
            "candidate_id": CID,
            V7_RECEIPT_FIELD: copy.deepcopy(parent[V7_RECEIPT_FIELD]),
            RECOVERY_RECEIPT_FIELD: copy.deepcopy(receipt),
        }
    ]
    monkeypatch.setattr(
        regrant,
        "inspect_selected_final_review_terminal_regrant",
        lambda state, **_kw: regrant.SelectedFinalReviewTerminalRegrantInspection(
            OUTSTANDING if state is post else READY_TO_REQUEUE,
            "test",
        ),
    )
    assert validate_initial_terminal_regrant_transition(
        pre,
        post,
        candidate_id=CID,
        grant=grant,
    )


def test_lost_current_v8_receipt_cannot_fall_back_to_v7_marker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent = _parent_terminal()
    marker = {"marker": "terminal-v5-to-v7"}
    state = _state(parent)
    state["talk_superseded_attempts"] = [
        {"candidate_id": CID, RECOVERY_RECEIPT_FIELD: {"malformed": True}}
    ]
    monkeypatch.setattr(topic_handoff, "_validate_terminal_handoff_state_v7", lambda *_a: True)
    monkeypatch.setattr(
        regrant, "validate_terminal_regrant_descendant_state", lambda *_a, **_k: False
    )
    assert not topic_handoff.validate_terminal_handoff_state(state, CID, marker)


def test_v8_operator_scope_blocks_a_foreign_active_epoch() -> None:
    parent = _parent_terminal()
    grant = _grant(parent, {"marker": "terminal-v5-to-v7"})
    state = _state(parent)
    state["operator_processing_scope"] = grant
    state["pending_talk"] = [
        {
            "candidate_id": "auto_foreign",
            RECOVERY_RECEIPT_FIELD: {"candidate_id": "auto_foreign"},
        }
    ]
    assert operator_processing_scope.operator_talk_scope(state, date=DATE) == ()


def test_v8_expiry_wins_before_marker_or_fingerprint_io(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent = _parent_terminal()
    grant = _grant(parent, {"marker": "terminal-v5-to-v7"})
    state = _state(parent)
    state["operator_processing_scope"] = grant
    monkeypatch.setattr(
        regrant,
        "_parent_authority_valid",
        lambda *_a, **_k: pytest.fail("expired v8 must not inspect parent authority"),
    )
    monkeypatch.setattr(
        regrant,
        "_marker_for",
        lambda *_a, **_k: pytest.fail("expired v8 must not inspect marker bytes"),
    )

    admission = operator_processing_scope.operator_scope_admission(
        state,
        date=DATE,
        now=datetime(2026, 8, 14, 20, 0, 1, tzinfo=timezone.utc),
    )

    assert not admission.admitted
    assert admission.reason_code == "EXPIRED"
    assert (
        operator_processing_scope.operator_talk_scope(
            state,
            date=DATE,
            now=datetime(2026, 8, 14, 20, 0, 1, tzinfo=timezone.utc),
        )
        == ()
    )


def test_v8_real_delivery_requeue_bypasses_legacy_cap_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rec_root = tmp_path / "recordings"
    date_root = rec_root / DATE
    date_root.mkdir(parents=True)
    segment = date_root / "22966160_20260808-21-31-35.mp4"
    segment.write_bytes(b"media")
    parent = _parent_terminal(
        segment=segment.name,
        start_ms=806_000,
        end_ms=868_000,
        hook="terminal 806",
        talk_repair_retry_count=99,
        talk_transient_retry_count=9,
        pipeline_fingerprint="sha256:" + "a" * 64,
    )
    marker = {"marker": "terminal-v5-to-v7"}
    grant = _fresh_grant(parent, marker)
    state = _state(parent)
    state["operator_processing_scope"] = grant

    monkeypatch.setattr(runner, "REC_ROOT", rec_root)
    monkeypatch.setattr(runner, "talk_pipeline_fingerprint", lambda _cid: "sha256:" + "b" * 64)
    monkeypatch.setattr(
        runner,
        "talk_failure_recovery_fingerprint",
        lambda kind, cid: (
            CURRENT
            if (kind, cid) == ("subtitle_authority", CID)
            else pytest.fail("v8 retry inspected an unrelated recovery fingerprint")
        ),
    )
    monkeypatch.setattr(runner, "ffprobe_ms", lambda _path: 900_000)
    monkeypatch.setattr(runner, "find_danmaku_xml", lambda _path: None)
    monkeypatch.setattr(
        runner,
        "resolve_structured_chat_binding",
        lambda _segment, **_kwargs: {
            "structured_chat_required": False,
            "chat_binding_status": "NOT_REGISTERED",
        },
    )
    monkeypatch.setattr(runner, "TALK_REPAIR_LIFETIME_RETRY_CAP", 3)
    monkeypatch.setattr(regrant, "_marker_for", lambda *_args: marker)
    monkeypatch.setattr(regrant, "_parent_authority_valid", lambda *_a, **_k: True)
    monkeypatch.setattr(topic_handoff, "validate_terminal_handoff_state", lambda *_args: True)

    assert runner.requeue_recoverable_talks(DATE, state, candidate_ids=(CID,)) == 1
    assert state["picks"] == []
    queued = state["pending_talk"][0]
    assert queued["cid"] == CID
    assert queued["retry_reason"] == "selected_final_review_terminal_regrant"
    assert queued["talk_repair_retry_count"] == 100
    assert queued[V7_RECEIPT_FIELD] == parent[V7_RECEIPT_FIELD]
    assert validate_selected_final_review_terminal_regrant_receipt(
        queued[RECOVERY_RECEIPT_FIELD],
        current_row=queued,
        candidate_id=CID,
        grant=grant,
    )
    archived = state["talk_superseded_attempts"][-1]
    assert archived[V7_RECEIPT_FIELD] == parent[V7_RECEIPT_FIELD]
    assert archived[RECOVERY_RECEIPT_FIELD] == queued[RECOVERY_RECEIPT_FIELD]
    assert archived[V7_RECEIPT_FIELD] is not queued[V7_RECEIPT_FIELD]
    assert archived[RECOVERY_RECEIPT_FIELD] is not queued[RECOVERY_RECEIPT_FIELD]


def test_v8_historical_requeue_and_production_seal_consume_one_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent = _parent_terminal(talk_repair_retry_count=7)
    marker = {"marker": "terminal-v5-to-v7"}
    grant = _fresh_grant(parent, marker)
    other = {"candidate_id": "auto_other", "status": "failed"}
    state = _state(parent)
    state["picks"].append(copy.deepcopy(other))
    state["pending_song"] = [{"candidate_id": "song_other"}]
    state["operator_processing_scope"] = grant
    state["published_topic_resolution_recovery"] = {"frozen": marker}
    frozen_marker = copy.deepcopy(state["published_topic_resolution_recovery"])
    frozen_other = copy.deepcopy(other)
    frozen_songs = copy.deepcopy(state["pending_song"])

    monkeypatch.setattr(regrant, "_marker_for", lambda *_args: marker)
    monkeypatch.setattr(regrant, "_parent_authority_valid", lambda *_a, **_k: True)
    monkeypatch.setattr(
        runner,
        "talk_failure_recovery_fingerprint",
        lambda kind, cid: (
            CURRENT
            if (kind, cid) == ("subtitle_authority", CID)
            else pytest.fail("v8 transaction inspected unrelated recovery authority")
        ),
    )
    monkeypatch.setattr(topic_handoff, "validate_terminal_handoff_state", lambda *_args: True)

    def requeue_once(_date: str, value: dict, **kwargs: object):
        assert kwargs == {
            "automatic_maintenance": True,
            "talk_candidate_ids": (CID,),
        }
        queued_body = _queue(
            parent,
            retry_reason="selected_final_review_terminal_regrant",
            talk_repair_retry_count=8,
        )
        receipt = build_selected_final_review_terminal_regrant_receipt(
            state=value,
            old_row=parent,
            queued_row=queued_body,
            candidate_id=CID,
            grant=grant,
        )
        queued = {**queued_body, RECOVERY_RECEIPT_FIELD: receipt}
        value["picks"] = [row for row in value["picks"] if row.get("candidate_id") != CID]
        value["pending_talk"] = [queued]
        value["talk_superseded_attempts"].append(
            {
                "candidate_id": CID,
                V7_RECEIPT_FIELD: copy.deepcopy(parent[V7_RECEIPT_FIELD]),
                RECOVERY_RECEIPT_FIELD: copy.deepcopy(receipt),
            }
        )
        return 0, 0, 1, 0, False

    monkeypatch.setattr(
        historical_failed_talk_scope,
        "maintain_delivery_recovery_scope",
        requeue_once,
    )
    assert historical_failed_talk_scope.maintain(
        DATE,
        state,
        automatic_maintenance=True,
        candidate_ids=(CID,),
    ) == (0, 0, 1, 0, False)
    assert state["published_topic_resolution_recovery"] == frozen_marker
    assert state["picks"] == [frozen_other]
    assert state["pending_song"] == frozen_songs
    queued = state["pending_talk"][0]
    assert queued[V7_RECEIPT_FIELD] == parent[V7_RECEIPT_FIELD]
    assert (
        inspect_selected_final_review_terminal_regrant(state, candidate_id=CID, grant=grant).outcome
        == OUTSTANDING
    )

    preimage = historical_failed_talk_scope.production_preimage(state, (CID,))
    assert preimage is not None
    final_pick = copy.deepcopy(queued)
    final_pick.update(
        {
            "candidate_id": CID,
            "status": "review_ready",
            "bundle_lifecycle": "CURRENT",
            "bundle_compliance": "COMPLIANT",
        }
    )
    state["pending_talk"] = []
    state["picks"].append(final_pick)
    assert historical_failed_talk_scope.seal_production_transition(DATE, state, (CID,), preimage)
    sealed = final_pick[RECOVERY_RECEIPT_FIELD]
    assert sealed["transitions"][-1]["kind"] == QUEUE_TO_PICK_TRANSITION
    assert final_pick[V7_RECEIPT_FIELD] == parent[V7_RECEIPT_FIELD]
    assert state["published_topic_resolution_recovery"] == frozen_marker
    assert state["picks"][0] == frozen_other
    assert state["pending_song"] == frozen_songs
    assert (
        inspect_selected_final_review_terminal_regrant(state, candidate_id=CID, grant=grant).outcome
        == CONVERGED
    )

    monkeypatch.setattr(
        historical_failed_talk_scope,
        "maintain_delivery_recovery_scope",
        lambda *_a, **_k: pytest.fail("consumed v8 must never dispatch twice"),
    )
    assert historical_failed_talk_scope.maintain(
        DATE,
        state,
        automatic_maintenance=True,
        candidate_ids=(CID,),
    ) == (0, 0, 0, 0, False)


def test_normal_and_synthetic_producer_results_deepcopy_both_v7_and_v8(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent = _parent_terminal()
    marker = {"marker": "terminal-v5-to-v7"}
    grant = _fresh_grant(parent, marker)
    queue = _queue(parent)
    monkeypatch.setattr(regrant, "_marker_for", lambda *_args: marker)
    monkeypatch.setattr(regrant, "_parent_authority_valid", lambda *_a, **_k: True)
    receipt = build_selected_final_review_terminal_regrant_receipt(
        state=_state(parent),
        old_row=parent,
        queued_row=queue,
        candidate_id=CID,
        grant=grant,
    )
    item = {**queue, RECOVERY_RECEIPT_FIELD: receipt}

    normal = talk_lane._carry_talk_recovery_result(item, {"candidate_id": CID, "status": "failed"})
    for field in (V7_RECEIPT_FIELD, RECOVERY_RECEIPT_FIELD):
        assert normal[field] == item[field]
        assert normal[field] is not item[field]

    def crash(_date: str, _item: dict, **_kwargs: object) -> dict:
        raise RuntimeError("synthetic producer crash")

    synthetic = produce_dispatch.produce_batch_windowed(
        DATE,
        [item],
        crash,
        produce_talk_fn=crash,
        produce_song_fn=lambda _date, _item: {},
        base=tmp_path,
        max_parallel=1,
        log=lambda _message: None,
        talk_pipeline_fingerprint=lambda _candidate_id: "sha256:" + "b" * 64,
        pipeline_fingerprint=lambda: "sha256:" + "c" * 64,
        song_pipeline_fingerprint=lambda: "sha256:" + "d" * 64,
        song_window_pre_ms=1_000,
        song_window_post_ms=1_000,
    )[0]
    for field in (V7_RECEIPT_FIELD, RECOVERY_RECEIPT_FIELD):
        assert synthetic[field] == item[field]
        assert synthetic[field] is not item[field]


def test_v8_queue_rebound_is_whitelisted_and_tamper_rolls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent = _parent_terminal()
    marker = {"marker": "terminal-v5-to-v7"}
    grant = _fresh_grant(parent, marker)
    monkeypatch.setattr(regrant, "_marker_for", lambda *_args: marker)
    monkeypatch.setattr(regrant, "_parent_authority_valid", lambda *_a, **_k: True)
    monkeypatch.setattr(
        regrant_runtime,
        "terminal_handoff_candidate_ids",
        lambda _state, candidate_ids: tuple(candidate_ids or ()),
    )
    state = _queued_state(parent, marker, grant)
    receipt = copy.deepcopy(state["pending_talk"][0][RECOVERY_RECEIPT_FIELD])
    preimage = copy.deepcopy(state)
    state["pending_talk"][0]["session_id"] = "session-806"
    assert regrant_runtime.seal_queue_rebound(
        DATE,
        state,
        candidate_ids=(CID,),
        preimage=preimage,
        phase=RECOVERY_REBOUND_SESSION_ANNOTATION,
        reason_code="TEST_V8_REBOUND_BLOCKED",
    )
    assert state["pending_talk"][0][RECOVERY_RECEIPT_FIELD] == receipt

    allowed_preimage = copy.deepcopy(state)
    state["pending_talk"][0]["hook"] = "unauthorized hook drift"
    assert not regrant_runtime.seal_queue_rebound(
        DATE,
        state,
        candidate_ids=(CID,),
        preimage=allowed_preimage,
        phase=RECOVERY_REBOUND_SESSION_ANNOTATION,
        reason_code="TEST_V8_REBOUND_BLOCKED",
    )
    assert {
        key: value
        for key, value in state.items()
        if key != "operator_processing_scope_runtime_block"
    } == allowed_preimage
    assert state["operator_processing_scope_runtime_block"]["reason_code"] == (
        "TEST_V8_REBOUND_BLOCKED"
    )


def test_v8_production_tamper_restores_full_preimage_and_blocks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent = _parent_terminal()
    marker = {"marker": "terminal-v5-to-v7"}
    grant = _fresh_grant(parent, marker)
    monkeypatch.setattr(regrant, "_marker_for", lambda *_args: marker)
    monkeypatch.setattr(regrant, "_parent_authority_valid", lambda *_a, **_k: True)
    monkeypatch.setattr(
        runner,
        "talk_failure_recovery_fingerprint",
        lambda _kind, _cid: CURRENT,
    )
    monkeypatch.setattr(topic_handoff, "validate_terminal_handoff_state", lambda *_args: True)
    state = _queued_state(parent, marker, grant)
    preimage = historical_failed_talk_scope.production_preimage(state, (CID,))
    assert preimage is not None
    queue = state["pending_talk"].pop()
    final_pick = copy.deepcopy(queue)
    final_pick.update({"candidate_id": CID, "status": "review_ready"})
    final_pick[V7_RECEIPT_FIELD] = {"tampered": True}
    state["picks"].append(final_pick)

    assert not historical_failed_talk_scope.seal_production_transition(
        DATE, state, (CID,), preimage
    )
    assert {
        key: value
        for key, value in state.items()
        if key != "operator_processing_scope_runtime_block"
    } == preimage
    assert state["operator_processing_scope_runtime_block"] == {
        "schema_version": "operator-processing-scope-runtime-block.v1",
        "recording_date": DATE,
        "candidate_ids": [CID],
        "intent": regrant.GRANT_INTENT,
        "upload_allowed": False,
        "reason_code": "SELECTED_FINAL_REVIEW_TERMINAL_REGRANT_PRODUCTION_BLOCKED",
    }
