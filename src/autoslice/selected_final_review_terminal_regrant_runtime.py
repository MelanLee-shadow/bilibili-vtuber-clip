"""Runner transaction seams for the one-shot v8 terminal regrant."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from copy import deepcopy

from src.autoslice.operator_processing_scope import (
    FINAL_REVIEW_TERMINAL_REGRANT_INTENT,
    SONG_STATE_COLLECTIONS,
    STATE_KEY,
)
from src.autoslice.published_topic_recovery_lineage import (
    RECOVERY_REBOUND_FIELDS,
    RECOVERY_REBOUND_PRODUCTION_PREPARE,
    RECOVERY_REBOUND_SESSION_ANNOTATION,
    top_level_changed_fields,
)
from src.autoslice.selected_final_review_terminal_regrant import (
    BLOCKED,
    CONVERGED,
    OUTSTANDING,
    QUEUE_TO_PICK_TRANSITION,
    READY_TO_REQUEUE,
    RECOVERY_RECEIPT_FIELD,
    active_selected_final_review_terminal_regrant_scope,
    advance_selected_final_review_terminal_regrant_receipt,
    inspect_selected_final_review_terminal_regrant,
    valid_terminal_regrant_grant,
    validate_initial_terminal_regrant_transition,
    validate_selected_final_review_terminal_regrant_receipt,
)


_RUNTIME_BLOCK_KEY = "operator_processing_scope_runtime_block"
_RUNTIME_BLOCK_SCHEMA = "operator-processing-scope-runtime-block.v1"
_ACTIVE_TALK_COLLECTIONS = (
    "pending_talk",
    "talk_backlog",
    "picks",
    "talk_below_confidence_threshold",
)
_QUEUED_TALK_COLLECTIONS = frozenset({"pending_talk", "talk_backlog"})
_SESSION_ANNOTATION_STATE_FIELDS = frozenset(
    {
        "recording_sessions",
        "segment_relation_authorities",
        "segment_scene_contexts",
        "segment_sessions",
        "session_relation_authority",
    }
)
_ZERO_RESULT = (0, 0, 0, 0, False)


def _candidate_id(row: object) -> str:
    if not isinstance(row, Mapping):
        return ""
    return str(row.get("candidate_id") or row.get("cid") or "").strip()


def scope_candidate_ids(
    state: Mapping[str, object], candidate_ids: tuple[str, ...] | None
) -> tuple[str, ...]:
    """Recognize only the strict structural envelope of one v8 grant."""

    values = tuple(candidate_ids or ())
    block = state.get(STATE_KEY)
    if len(values) != 1 or not isinstance(block, Mapping):
        return ()
    return (
        values
        if valid_terminal_regrant_grant(
            block,
            candidate_id=values[0],
            recording_date=str(block.get("recording_date") or ""),
        )
        else ()
    )


def _grant(state: Mapping[str, object]) -> Mapping[str, object] | None:
    block = state.get(STATE_KEY)
    return block if isinstance(block, Mapping) else None


def _target_rows(
    state: Mapping[str, object], candidate_id: str
) -> list[tuple[str, Mapping[str, object]]]:
    return [
        (collection, row)
        for collection in _ACTIVE_TALK_COLLECTIONS
        for row in (state.get(collection) if isinstance(state.get(collection), list) else [])
        if isinstance(row, Mapping) and _candidate_id(row) == candidate_id
    ]


def transition_preimage(
    state: Mapping[str, object], candidate_ids: tuple[str, ...] | None
) -> dict | None:
    return deepcopy(dict(state)) if scope_candidate_ids(state, candidate_ids) else None


def restore_transition_block(
    state: dict,
    preimage: Mapping[str, object],
    *,
    date: str,
    candidate_ids: tuple[str, ...],
    reason_code: str,
) -> None:
    state.clear()
    state.update(deepcopy(dict(preimage)))
    state[_RUNTIME_BLOCK_KEY] = {
        "schema_version": _RUNTIME_BLOCK_SCHEMA,
        "recording_date": date,
        "candidate_ids": list(candidate_ids),
        "intent": FINAL_REVIEW_TERMINAL_REGRANT_INTENT,
        "upload_allowed": False,
        "reason_code": reason_code,
    }


def matching_runtime_block(
    state: Mapping[str, object],
    *,
    date: str,
    candidate_ids: tuple[str, ...] | None,
) -> bool:
    block = state.get(_RUNTIME_BLOCK_KEY)
    return bool(
        isinstance(block, Mapping)
        and block.get("schema_version") == _RUNTIME_BLOCK_SCHEMA
        and block.get("recording_date") == date
        and block.get("candidate_ids") == list(candidate_ids or ())
        and block.get("intent") == FINAL_REVIEW_TERMINAL_REGRANT_INTENT
        and block.get("upload_allowed") is False
    )


def terminal_handoff_candidate_ids(
    state: Mapping[str, object], candidate_ids: tuple[str, ...] | None
) -> tuple[str, ...]:
    """Recognize a marker-preserving v8 descendant without mutating state."""

    values = scope_candidate_ids(state, candidate_ids)
    if len(values) != 1:
        return ()
    from src.autoslice.published_topic_collision import _recovery_ledger_entries
    from src.autoslice.published_topic_final_review_handoff import (
        validate_terminal_handoff_state,
    )

    probe = deepcopy(dict(state))
    try:
        marker = _recovery_ledger_entries(probe).get(values[0])
        terminal = isinstance(marker, Mapping) and validate_terminal_handoff_state(
            probe, values[0], marker
        )
    except Exception:  # noqa: BLE001 - authority probe fails closed
        return ()
    return values if probe == dict(state) and terminal else ()


def _non_target_state_unchanged(
    preimage: Mapping[str, object],
    state: Mapping[str, object],
    *,
    candidate_id: str,
) -> bool:
    if not (
        preimage.get(STATE_KEY) == state.get(STATE_KEY)
        and preimage.get("upload_allowed") is False
        and state.get("upload_allowed") is False
        and all(
            preimage.get(collection) == state.get(collection)
            for collection in SONG_STATE_COLLECTIONS
        )
    ):
        return False
    for collection in _ACTIVE_TALK_COLLECTIONS:
        before = [
            row
            for row in (preimage.get(collection) or [])
            if not (isinstance(row, Mapping) and _candidate_id(row) == candidate_id)
        ]
        after = [
            row
            for row in (state.get(collection) or [])
            if not (isinstance(row, Mapping) and _candidate_id(row) == candidate_id)
        ]
        if before != after:
            return False
    return True


def seal_queue_rebound(
    date: str,
    state: dict,
    *,
    candidate_ids: tuple[str, ...] | None,
    preimage: Mapping[str, object],
    phase: str,
    reason_code: str,
) -> bool:
    """Validate one receipt-headed v8 queue enrichment before persistence."""

    values = scope_candidate_ids(preimage, candidate_ids)
    candidate_id = values[0] if len(values) == 1 else ""
    before_rows = _target_rows(preimage, candidate_id)
    after_rows = _target_rows(state, candidate_id)
    grant = _grant(preimage)
    transition_ok = False
    if len(before_rows) == len(after_rows) == 1 and isinstance(grant, Mapping):
        before_collection, before_row = before_rows[0]
        after_collection, after_row = after_rows[0]
        receipt = before_row.get(RECOVERY_RECEIPT_FIELD)
        same_collection_required = phase != RECOVERY_REBOUND_PRODUCTION_PREPARE
        try:
            transition_ok = bool(
                isinstance(receipt, Mapping)
                and before_collection in _QUEUED_TALK_COLLECTIONS
                and after_collection in _QUEUED_TALK_COLLECTIONS
                and (not same_collection_required or before_collection == after_collection)
                and after_row.get(RECOVERY_RECEIPT_FIELD) == receipt
                and set(top_level_changed_fields(before_row, after_row)).issubset(
                    RECOVERY_REBOUND_FIELDS[phase]
                )
                and preimage.get("talk_superseded_attempts")
                == state.get("talk_superseded_attempts")
                and _non_target_state_unchanged(preimage, state, candidate_id=candidate_id)
                and validate_selected_final_review_terminal_regrant_receipt(
                    receipt,
                    current_row=before_row,
                    candidate_id=candidate_id,
                    grant=grant,
                    allow_queue_rebound=True,
                )
                and validate_selected_final_review_terminal_regrant_receipt(
                    receipt,
                    current_row=after_row,
                    candidate_id=candidate_id,
                    grant=grant,
                    allow_queue_rebound=True,
                )
                and terminal_handoff_candidate_ids(state, values) == values
            )
        except Exception:  # noqa: BLE001 - rollback owns malformed rebound state
            transition_ok = False
    if transition_ok:
        return True
    restore_transition_block(
        state,
        preimage,
        date=date,
        candidate_ids=values,
        reason_code=reason_code,
    )
    return False


def seal_session_annotation(
    date: str,
    state: dict,
    *,
    candidate_ids: tuple[str, ...] | None,
    preimage: Mapping[str, object],
) -> bool:
    """Allow initial annotation only when the terminal pick remains exact."""

    values = scope_candidate_ids(preimage, candidate_ids)
    candidate_id = values[0] if len(values) == 1 else ""
    grant = _grant(preimage)
    before_rows = _target_rows(preimage, candidate_id)
    after_rows = _target_rows(state, candidate_id)
    if len(before_rows) == 1 and isinstance(before_rows[0][1].get(RECOVERY_RECEIPT_FIELD), Mapping):
        return seal_queue_rebound(
            date,
            state,
            candidate_ids=candidate_ids,
            preimage=preimage,
            phase=RECOVERY_REBOUND_SESSION_ANNOTATION,
            reason_code=("SELECTED_FINAL_REVIEW_TERMINAL_REGRANT_SESSION_ANNOTATION_BLOCKED"),
        )
    transition_ok = False
    try:
        transition_ok = bool(
            len(before_rows) == len(after_rows) == 1
            and before_rows[0][0] == after_rows[0][0] == "picks"
            and before_rows[0][1] == after_rows[0][1]
            and isinstance(grant, Mapping)
            and RECOVERY_RECEIPT_FIELD not in before_rows[0][1]
            and set(top_level_changed_fields(preimage, state)).issubset(
                _SESSION_ANNOTATION_STATE_FIELDS
            )
            and preimage.get("talk_superseded_attempts") == state.get("talk_superseded_attempts")
            and _non_target_state_unchanged(preimage, state, candidate_id=candidate_id)
            and terminal_handoff_candidate_ids(state, values) == values
            and inspect_selected_final_review_terminal_regrant(
                preimage, candidate_id=candidate_id, grant=grant
            ).outcome
            == READY_TO_REQUEUE
            and inspect_selected_final_review_terminal_regrant(
                state, candidate_id=candidate_id, grant=grant
            ).outcome
            == READY_TO_REQUEUE
        )
    except Exception:  # noqa: BLE001 - rollback owns malformed authority
        transition_ok = False
    if transition_ok:
        return True
    restore_transition_block(
        state,
        preimage,
        date=date,
        candidate_ids=values,
        reason_code="SELECTED_FINAL_REVIEW_TERMINAL_REGRANT_SESSION_ANNOTATION_BLOCKED",
    )
    return False


def maintain_if_active(
    date: str,
    state: dict,
    automatic_maintenance: bool,
    candidate_ids: tuple[str, ...] | None,
    maintain_delivery: Callable[..., tuple[int, int, int, int, bool]],
) -> tuple[int, int, int, int, bool] | None:
    """Own the entire v8 pick-to-queue transaction outside the legacy maintain."""

    values = scope_candidate_ids(state, candidate_ids)
    if len(values) != 1:
        return None
    if matching_runtime_block(state, date=date, candidate_ids=candidate_ids):
        return _ZERO_RESULT
    if not automatic_maintenance:
        return _ZERO_RESULT
    grant = _grant(state)
    preimage = deepcopy(dict(state))
    try:
        inspection = (
            inspect_selected_final_review_terminal_regrant(
                state, candidate_id=values[0], grant=grant
            )
            if isinstance(grant, Mapping)
            else None
        )
    except Exception:  # noqa: BLE001 - typed authority probe fails closed
        inspection = None
    if dict(state) != preimage or inspection is None or inspection.outcome == BLOCKED:
        restore_transition_block(
            state,
            preimage,
            date=date,
            candidate_ids=values,
            reason_code="SELECTED_FINAL_REVIEW_TERMINAL_REGRANT_REQUEUE_BLOCKED",
        )
        return _ZERO_RESULT
    if inspection.outcome in {OUTSTANDING, CONVERGED}:
        return _ZERO_RESULT
    if inspection.outcome != READY_TO_REQUEUE:
        restore_transition_block(
            state,
            preimage,
            date=date,
            candidate_ids=values,
            reason_code="SELECTED_FINAL_REVIEW_TERMINAL_REGRANT_REQUEUE_BLOCKED",
        )
        return _ZERO_RESULT
    try:
        result = maintain_delivery(
            date,
            state,
            automatic_maintenance=automatic_maintenance,
            talk_candidate_ids=candidate_ids,
        )
        sealed = bool(
            isinstance(grant, Mapping)
            and validate_initial_terminal_regrant_transition(
                preimage,
                state,
                candidate_id=values[0],
                grant=grant,
            )
        )
    except Exception:  # noqa: BLE001 - full preimage owns this transition
        sealed = False
        result = _ZERO_RESULT
    if sealed:
        return result
    restore_transition_block(
        state,
        preimage,
        date=date,
        candidate_ids=values,
        reason_code="SELECTED_FINAL_REVIEW_TERMINAL_REGRANT_REQUEUE_BLOCKED",
    )
    return _ZERO_RESULT


def seal_production_transition(
    date: str,
    state: dict,
    *,
    candidate_ids: tuple[str, ...] | None,
    preimage: Mapping[str, object],
) -> bool:
    """Consume one v8 queue at the sole queue-to-pick persistence seam."""

    values = scope_candidate_ids(preimage, candidate_ids)
    candidate_id = values[0] if len(values) == 1 else ""
    grant = _grant(preimage)
    before_rows = _target_rows(preimage, candidate_id)
    after_rows = _target_rows(state, candidate_id)
    transition_ok = False
    if (
        len(before_rows) == len(after_rows) == 1
        and before_rows[0][0] in _QUEUED_TALK_COLLECTIONS
        and isinstance(grant, Mapping)
    ):
        before_row = before_rows[0][1]
        receipt = before_row.get(RECOVERY_RECEIPT_FIELD)
        after_collection, after_row = after_rows[0]
        try:
            before_valid = bool(
                isinstance(receipt, Mapping)
                and validate_selected_final_review_terminal_regrant_receipt(
                    receipt,
                    current_row=before_row,
                    candidate_id=candidate_id,
                    grant=grant,
                    allow_queue_rebound=True,
                )
                and _non_target_state_unchanged(preimage, state, candidate_id=candidate_id)
                and preimage.get("talk_superseded_attempts")
                == state.get("talk_superseded_attempts")
            )
            if (
                before_valid
                and after_collection in _QUEUED_TALK_COLLECTIONS
                and after_row.get(RECOVERY_RECEIPT_FIELD) == receipt
            ):
                transition_ok = bool(
                    validate_selected_final_review_terminal_regrant_receipt(
                        receipt,
                        current_row=after_row,
                        candidate_id=candidate_id,
                        grant=grant,
                        allow_queue_rebound=True,
                    )
                    and inspect_selected_final_review_terminal_regrant(
                        state, candidate_id=candidate_id, grant=grant
                    ).outcome
                    == OUTSTANDING
                )
            elif (
                before_valid
                and after_collection == "picks"
                and after_row.get(RECOVERY_RECEIPT_FIELD) == receipt
            ):
                final_row = deepcopy(after_row)
                final_row.pop(RECOVERY_RECEIPT_FIELD, None)
                next_receipt = advance_selected_final_review_terminal_regrant_receipt(
                    receipt,
                    from_row=before_row,
                    to_row=final_row,
                    candidate_id=candidate_id,
                    grant=grant,
                    transition_kind=QUEUE_TO_PICK_TRANSITION,
                    allow_queue_rebound=True,
                )
                after_row[RECOVERY_RECEIPT_FIELD] = next_receipt
                transition_ok = bool(
                    validate_selected_final_review_terminal_regrant_receipt(
                        next_receipt,
                        current_row=after_row,
                        candidate_id=candidate_id,
                        grant=grant,
                    )
                    and inspect_selected_final_review_terminal_regrant(
                        state, candidate_id=candidate_id, grant=grant
                    ).outcome
                    == CONVERGED
                )
        except Exception:  # noqa: BLE001 - rollback owns this commit point
            transition_ok = False
    if transition_ok:
        return True
    restore_transition_block(
        state,
        preimage,
        date=date,
        candidate_ids=values,
        reason_code="SELECTED_FINAL_REVIEW_TERMINAL_REGRANT_PRODUCTION_BLOCKED",
    )
    return False


def active_scope(
    date: str,
    state: Mapping[str, object],
    candidate_ids: tuple[str, ...] | None,
) -> bool:
    """Expose the canonical active-scope check for cover suppression tests."""

    return bool(active_selected_final_review_terminal_regrant_scope(date, state, candidate_ids))
