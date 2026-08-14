"""Runner orchestration for typed historical Talk-only recovery scopes."""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy

from src.autoslice.delivery_recovery import (
    RecoveryReviewRerunError,
    _recovery_queue_item,
)
from src.autoslice.exact_talk_recovery_scope import maintain_delivery_recovery_scope
from src.autoslice.operator_processing_scope import (
    FINAL_REVIEW_RECOVERY_GRANT_SCHEMA,
    FINAL_REVIEW_RECOVERY_INTENT,
    HELD_CURRENT_RERENDER_INTENT,
    SOURCE_FACT_RECOVERY_GRANT_SCHEMA,
    SOURCE_FACT_RECOVERY_INTENT,
    SONG_STATE_COLLECTIONS,
    STATE_KEY,
    TOPIC_HOLD_RECOVERY_GRANT_SCHEMA,
    TOPIC_HOLD_RECOVERY_INTENT,
    operator_talk_scope,
)
from src.autoslice.published_topic_recovery_lineage import (
    RECOVERY_REBOUND_FIELDS,
    RECOVERY_REBOUND_PRODUCTION_PREPARE,
    RECOVERY_REBOUND_SEMANTIC_SCORECARD_REFRESH,
    RECOVERY_REBOUND_SESSION_ANNOTATION,
    top_level_changed_fields,
)
from src.autoslice.runner_proxy import RunnerProxy
from src.autoslice import semantic_evidence_scorecard_refresh as semantic_chat_refresh


_runner = RunnerProxy()

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


def _topic_scope_candidate_ids(
    state: Mapping[str, object], candidate_ids: tuple[str, ...] | None
) -> tuple[str, ...]:
    block = state.get(STATE_KEY)
    values = tuple(candidate_ids or ())
    if not (
        len(values) == 1
        and isinstance(block, Mapping)
        and block.get("schema_version") == TOPIC_HOLD_RECOVERY_GRANT_SCHEMA
        and block.get("intent") == TOPIC_HOLD_RECOVERY_INTENT
        and block.get("candidate_ids") == list(values)
        and block.get("upload_allowed") is False
    ):
        return ()
    return values


def _source_fact_scope_candidate_ids(
    state: Mapping[str, object], candidate_ids: tuple[str, ...] | None
) -> tuple[str, ...]:
    """Recognize only the frozen structural envelope of one v6 grant."""

    block = state.get(STATE_KEY)
    values = tuple(candidate_ids or ())
    if not (
        len(values) == 1
        and isinstance(block, Mapping)
        and block.get("schema_version") == SOURCE_FACT_RECOVERY_GRANT_SCHEMA
        and block.get("intent") == SOURCE_FACT_RECOVERY_INTENT
        and block.get("candidate_ids") == list(values)
        and block.get("upload_allowed") is False
        and isinstance(block.get("grant_id"), str)
        and str(block.get("grant_id")).strip() == block.get("grant_id")
        and block.get("grant_id")
    ):
        return ()
    return values


def _final_review_scope_candidate_ids(
    state: Mapping[str, object], candidate_ids: tuple[str, ...] | None
) -> tuple[str, ...]:
    """Recognize only the frozen structural envelope of one v7 grant."""

    block = state.get(STATE_KEY)
    values = tuple(candidate_ids or ())
    if not (
        len(values) == 1
        and isinstance(block, Mapping)
        and block.get("schema_version") == FINAL_REVIEW_RECOVERY_GRANT_SCHEMA
        and block.get("intent") == FINAL_REVIEW_RECOVERY_INTENT
        and block.get("candidate_ids") == list(values)
        and block.get("upload_allowed") is False
        and isinstance(block.get("grant_id"), str)
        and str(block.get("grant_id")).strip() == block.get("grant_id")
        and block.get("grant_id")
    ):
        return ()
    return values


def _terminal_final_review_handoff_candidate_ids(
    state: Mapping[str, object], candidate_ids: tuple[str, ...] | None
) -> tuple[str, ...]:
    """Recognize only a canonical terminal v5-to-v7 handoff."""

    values = _final_review_scope_candidate_ids(state, candidate_ids)
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
    except Exception:  # noqa: BLE001 - authority probes fail closed
        return ()
    return values if probe == dict(state) and terminal else ()


def _target_talk_rows(
    state: Mapping[str, object], candidate_id: str
) -> list[tuple[str, dict]]:
    return [
        (collection, row)
        for collection in _ACTIVE_TALK_COLLECTIONS
        for row in (
            state.get(collection)
            if isinstance(state.get(collection), list)
            else []
        )
        if isinstance(row, dict)
        and str(row.get("candidate_id") or row.get("cid") or "").strip()
        == candidate_id
    ]


def _source_fact_grant_id(state: Mapping[str, object]) -> str:
    block = state.get(STATE_KEY)
    return str(block.get("grant_id") or "") if isinstance(block, Mapping) else ""


def _final_review_grant_id(state: Mapping[str, object]) -> str:
    block = state.get(STATE_KEY)
    return str(block.get("grant_id") or "") if isinstance(block, Mapping) else ""


def _restore_source_fact_transition_block(
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
        "intent": SOURCE_FACT_RECOVERY_INTENT,
        "upload_allowed": False,
        "reason_code": reason_code,
    }


def _matching_source_fact_runtime_block(
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
        and block.get("intent") == SOURCE_FACT_RECOVERY_INTENT
        and block.get("upload_allowed") is False
    )


def _restore_final_review_transition_block(
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
        "intent": FINAL_REVIEW_RECOVERY_INTENT,
        "upload_allowed": False,
        "reason_code": reason_code,
    }


def _matching_final_review_runtime_block(
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
        and block.get("intent") == FINAL_REVIEW_RECOVERY_INTENT
        and block.get("upload_allowed") is False
    )


def _topic_transition_preimage(
    state: Mapping[str, object], candidate_ids: tuple[str, ...] | None
) -> dict | None:
    return (
        deepcopy(dict(state))
        if _topic_scope_candidate_ids(state, candidate_ids)
        else None
    )


def _final_review_transition_preimage(
    state: Mapping[str, object], candidate_ids: tuple[str, ...] | None
) -> dict | None:
    return (
        deepcopy(dict(state))
        if _final_review_scope_candidate_ids(state, candidate_ids)
        else None
    )


def _seal_final_review_queue_rebound(
    date: str,
    state: dict,
    *,
    candidate_ids: tuple[str, ...] | None,
    preimage: Mapping[str, object],
    phase: str,
    reason_code: str,
) -> bool:
    """Validate every v7 queue enrichment before the runner may persist it."""

    from src.autoslice.selected_final_review_recovery import (
        RECOVERY_RECEIPT_FIELD,
        validate_selected_final_review_recovery_receipt,
    )

    values = _final_review_scope_candidate_ids(preimage, candidate_ids)
    candidate_id = values[0] if len(values) == 1 else ""
    before_rows = _target_talk_rows(preimage, candidate_id)
    after_rows = _target_talk_rows(state, candidate_id)
    transition_ok = False
    if len(before_rows) == len(after_rows) == 1:
        before_collection, before_row = before_rows[0]
        after_collection, after_row = after_rows[0]
        receipt = before_row.get(RECOVERY_RECEIPT_FIELD)
        declared_current = (
            receipt.get("current_failure_recovery_fingerprint")
            if isinstance(receipt, Mapping)
            else None
        )
        same_collection_required = phase != RECOVERY_REBOUND_PRODUCTION_PREPARE
        collections_valid = bool(
            before_collection in _QUEUED_TALK_COLLECTIONS
            and after_collection in _QUEUED_TALK_COLLECTIONS
            and (not same_collection_required or before_collection == after_collection)
        )
        try:
            transition_ok = bool(
                isinstance(declared_current, str)
                and collections_valid
                and after_row.get(RECOVERY_RECEIPT_FIELD) == receipt
                and set(top_level_changed_fields(before_row, after_row)).issubset(
                    RECOVERY_REBOUND_FIELDS[phase]
                )
                and validate_selected_final_review_recovery_receipt(
                    receipt,
                    queued_row=before_row,
                    candidate_id=candidate_id,
                    grant_id=_final_review_grant_id(preimage),
                    current_fingerprint=declared_current,
                    allow_queue_rebound=True,
                )
                and validate_selected_final_review_recovery_receipt(
                    receipt,
                    queued_row=after_row,
                    candidate_id=candidate_id,
                    grant_id=_final_review_grant_id(preimage),
                    current_fingerprint=declared_current,
                    allow_queue_rebound=True,
                )
            )
        except Exception:  # noqa: BLE001 - rollback owns malformed rebound state
            transition_ok = False
    if transition_ok:
        return True
    _restore_final_review_transition_block(
        state,
        preimage,
        date=date,
        candidate_ids=values,
        reason_code=reason_code,
    )
    return False


def _initial_final_review_handoff_outcome(
    state: Mapping[str, object],
    *,
    date: str,
    candidate_ids: tuple[str, ...] | None,
) -> str | None:
    """Classify a pure, recovery-ready pre-receipt v7 rejection."""

    values = _final_review_scope_candidate_ids(state, candidate_ids)
    if len(values) != 1:
        return None
    from src.autoslice.published_topic_final_review_handoff import (
        HANDOFF_ABSENT,
        HANDOFF_READY,
        inspect_initial_final_review_handoff,
    )
    from src.autoslice.selected_final_review_recovery import (
        READY_TO_REQUEUE,
        inspect_selected_final_review_recovery,
    )

    probe = deepcopy(dict(state))
    try:
        inspection = inspect_selected_final_review_recovery(
            probe,
            candidate_id=values[0],
            grant_id=_final_review_grant_id(probe),
        )
        outcome = inspect_initial_final_review_handoff(
            probe,
            candidate_id=values[0],
            recording_date=date,
        )
    except Exception:  # noqa: BLE001 - caller's rollback owns invalid authority
        return None
    return (
        outcome
        if (
            probe == state
            and inspection.outcome == READY_TO_REQUEUE
            and outcome in {HANDOFF_ABSENT, HANDOFF_READY}
        )
        else None
    )


def _no_marker_initial_annotation_is_valid(
    state: Mapping[str, object],
    *,
    date: str,
    candidate_ids: tuple[str, ...],
    preimage: Mapping[str, object],
) -> bool:
    """Validate one pre-receipt pick rebound without inventing a v5 marker."""

    from src.autoslice.published_topic_final_review_handoff import HANDOFF_ABSENT
    from src.autoslice.selected_final_review_recovery import (
        RECOVERY_RECEIPT_FIELD,
        is_selected_final_review_rejection,
    )

    candidate_id = candidate_ids[0]
    before_rows = _target_talk_rows(preimage, candidate_id)
    after_rows = _target_talk_rows(state, candidate_id)
    if len(before_rows) != 1 or len(after_rows) != 1:
        return False
    before_collection, before_row = before_rows[0]
    after_collection, after_row = after_rows[0]
    if not (
        before_collection == after_collection == "picks"
        and RECOVERY_RECEIPT_FIELD not in before_row
        and RECOVERY_RECEIPT_FIELD not in after_row
        and is_selected_final_review_rejection(before_row)
        and is_selected_final_review_rejection(after_row)
        and set(top_level_changed_fields(before_row, after_row)).issubset(
            RECOVERY_REBOUND_FIELDS[RECOVERY_REBOUND_SESSION_ANNOTATION]
        )
        and set(top_level_changed_fields(preimage, state)).issubset(
            {*_SESSION_ANNOTATION_STATE_FIELDS, before_collection}
        )
        and state.get("upload_allowed") is False
        and state.get(STATE_KEY) == preimage.get(STATE_KEY)
        and state.get("talk_superseded_attempts")
        == preimage.get("talk_superseded_attempts")
        and all(
            state.get(collection) == preimage.get(collection)
            for collection in SONG_STATE_COLLECTIONS
        )
    ):
        return False
    before_non_target = [
        row
        for row in preimage.get("picks", [])
        if not (
            isinstance(row, Mapping)
            and str(row.get("candidate_id") or row.get("cid") or "").strip()
            == candidate_id
        )
    ]
    after_non_target = [
        row
        for row in state.get("picks", [])
        if not (
            isinstance(row, Mapping)
            and str(row.get("candidate_id") or row.get("cid") or "").strip()
            == candidate_id
        )
    ]
    return bool(
        before_non_target == after_non_target
        and _initial_final_review_handoff_outcome(
            state,
            date=date,
            candidate_ids=candidate_ids,
        )
        == HANDOFF_ABSENT
    )


def _seal_initial_final_review_session_annotation(
    date: str,
    state: dict,
    *,
    candidate_ids: tuple[str, ...] | None,
    preimage: Mapping[str, object],
    handoff_outcome: str,
) -> bool:
    """Validate the allowed pick rebound before creating the first v7 receipt."""

    values = _final_review_scope_candidate_ids(preimage, candidate_ids)
    transition_ok = False
    if len(values) == 1 and _initial_final_review_handoff_outcome(
        preimage, date=date, candidate_ids=values
    ) == handoff_outcome:
        from src.autoslice.published_topic_final_review_handoff import (
            HANDOFF_ABSENT,
            HANDOFF_READY,
        )
        try:
            if handoff_outcome == HANDOFF_READY:
                from src.autoslice.published_topic_collision import (
                    seal_published_topic_resolution_row_rebounds,
                )

                transition_ok = bool(
                    seal_published_topic_resolution_row_rebounds(
                        state,
                        pre_state=preimage,
                        phase=RECOVERY_REBOUND_SESSION_ANNOTATION,
                        candidate_ids=values,
                    )
                    and _initial_final_review_handoff_outcome(
                        state,
                        date=date,
                        candidate_ids=values,
                    )
                    == HANDOFF_READY
                )
            elif handoff_outcome == HANDOFF_ABSENT:
                transition_ok = _no_marker_initial_annotation_is_valid(
                    state,
                    date=date,
                    candidate_ids=values,
                    preimage=preimage,
                )
        except Exception:  # noqa: BLE001 - restore the whole annotation preimage
            transition_ok = False
    if transition_ok:
        return True
    _restore_final_review_transition_block(
        state,
        preimage,
        date=date,
        candidate_ids=values,
        reason_code="SELECTED_FINAL_REVIEW_RECOVERY_SESSION_ANNOTATION_BLOCKED",
    )
    return False


def _restore_topic_transition_block(
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
        "intent": TOPIC_HOLD_RECOVERY_INTENT,
        "upload_allowed": False,
        "reason_code": reason_code,
    }


def annotate_sessions(
    date: str,
    state: dict,
    *,
    include_song_rows: bool,
    candidate_ids: tuple[str, ...] | None,
) -> int:
    """Run session annotation and seal every changed v5 ledger row before persist."""

    preimage = _topic_transition_preimage(state, candidate_ids)
    final_review_preimage = _final_review_transition_preimage(state, candidate_ids)
    initial_final_review_handoff = (
        _initial_final_review_handoff_outcome(
            final_review_preimage,
            date=date,
            candidate_ids=candidate_ids,
        )
        if final_review_preimage is not None
        else None
    )
    try:
        changed = _runner.annotate_state_sessions(
            date,
            state,
            include_song_rows=include_song_rows,
            talk_candidate_ids=candidate_ids,
        )
    except Exception:
        if final_review_preimage is None:
            raise
        values = _final_review_scope_candidate_ids(
            final_review_preimage, candidate_ids
        )
        _restore_final_review_transition_block(
            state,
            final_review_preimage,
            date=date,
            candidate_ids=values,
            reason_code=(
                "SELECTED_FINAL_REVIEW_RECOVERY_SESSION_ANNOTATION_BLOCKED"
            ),
        )
        return -1
    if final_review_preimage is not None:
        final_review_valid = (
            _seal_initial_final_review_session_annotation(
                date,
                state,
                candidate_ids=candidate_ids,
                preimage=final_review_preimage,
                handoff_outcome=initial_final_review_handoff,
            )
            if initial_final_review_handoff is not None
            else _seal_final_review_queue_rebound(
                date,
                state,
                candidate_ids=candidate_ids,
                preimage=final_review_preimage,
                phase=RECOVERY_REBOUND_SESSION_ANNOTATION,
                reason_code=(
                    "SELECTED_FINAL_REVIEW_RECOVERY_SESSION_ANNOTATION_BLOCKED"
                ),
            )
        )
        if not final_review_valid:
            return -1
    if preimage is None:
        return int(changed)
    from src.autoslice.published_topic_collision import (
        seal_published_topic_resolution_row_rebounds,
    )

    if seal_published_topic_resolution_row_rebounds(
        state,
        pre_state=preimage,
        phase="SESSION_ANNOTATION",
        candidate_ids=None,
    ):
        return int(changed)
    values = _topic_scope_candidate_ids(preimage, candidate_ids)
    _restore_topic_transition_block(
        state,
        preimage,
        date=date,
        candidate_ids=values,
        reason_code="TOPIC_DEDUP_SESSION_ANNOTATION_TRANSITION_BLOCKED",
    )
    return -1


def refresh_scorecards(
    date: str,
    state: dict,
    candidate_ids: tuple[str, ...] | None,
) -> int:
    """Refresh semantic cards behind a strict v5 queue-row rebound."""

    preimage = _topic_transition_preimage(state, candidate_ids)
    final_review_preimage = _final_review_transition_preimage(state, candidate_ids)
    result = semantic_chat_refresh.refresh_operator_scoped_chat_scorecards(date, state)
    if final_review_preimage is not None and not _seal_final_review_queue_rebound(
        date,
        state,
        candidate_ids=candidate_ids,
        preimage=final_review_preimage,
        phase=RECOVERY_REBOUND_SEMANTIC_SCORECARD_REFRESH,
        reason_code="SELECTED_FINAL_REVIEW_RECOVERY_SCORECARD_REFRESH_BLOCKED",
    ):
        return -1
    if preimage is None:
        return result
    from src.autoslice.published_topic_collision import (
        seal_published_topic_resolution_row_rebounds,
    )

    values = _topic_scope_candidate_ids(preimage, candidate_ids)
    if seal_published_topic_resolution_row_rebounds(
        state,
        pre_state=preimage,
        phase="SEMANTIC_SCORECARD_REFRESH",
        candidate_ids=values,
    ):
        return result
    _restore_topic_transition_block(
        state,
        preimage,
        date=date,
        candidate_ids=values,
        reason_code="TOPIC_DEDUP_SCORECARD_REFRESH_TRANSITION_BLOCKED",
    )
    return -1


def _requeue_topic_cover_pending(date: str, state: dict, candidate_id: str) -> int:
    """Move one sealed cover-pending pick back through the full Talk producer."""

    picks = state.get("picks")
    pending = state.get("pending_talk")
    if not isinstance(picks, list) or not isinstance(pending, list):
        return 0
    matches = [
        row
        for row in picks
        if isinstance(row, dict)
        and str(row.get("candidate_id") or row.get("cid") or "") == candidate_id
    ]
    if (
        len(matches) != 1
        or matches[0].get("status") != "media_ready_cover_pending"
        or any(
            isinstance(row, Mapping)
            and str(row.get("candidate_id") or row.get("cid") or "")
            == candidate_id
            for row in pending
        )
        or int(matches[0].get("talk_repair_retry_count") or 0)
        >= _runner.TALK_REPAIR_LIFETIME_RETRY_CAP
    ):
        return 0
    record = matches[0]
    try:
        item = _recovery_queue_item(
            date,
            record,
            candidate_id=candidate_id,
            retry_reason="topic_dedup_cover_pending_full_producer_retry",
            selected_repair=True,
            given_end_ms=record.get("given_end_ms"),
            given_end_authority=record.get("given_end_authority"),
            recovery_publication_authority=record.get(
                "recovery_publication_authority"
            ),
            provider_budget_history=(
                state.get("talk_superseded_attempts") or ()
            ),
        )
    except (OSError, TypeError, ValueError, RecoveryReviewRerunError):
        return 0
    state["picks"] = [row for row in picks if row is not record]
    pending.append(item)
    return 1


def _requeue_receipt_bound_cover_pending(
    date: str,
    state: dict,
    *,
    candidate_ids: tuple[str, ...] | None,
    final_review: bool,
) -> int:
    """Transactionally move one receipt-bound v6/v7 cover hold to Talk."""

    values = (
        _final_review_scope_candidate_ids(state, candidate_ids)
        if final_review
        else _source_fact_scope_candidate_ids(state, candidate_ids)
    )
    if len(values) != 1:
        return 0
    candidate_id = values[0]
    rows = _target_talk_rows(state, candidate_id)
    cover_rows = [
        (collection, row)
        for collection, row in rows
        if row.get("status") == "media_ready_cover_pending"
    ]
    if not cover_rows:
        return 0
    preimage = deepcopy(dict(state))
    reason_prefix = (
        "SELECTED_FINAL_REVIEW_RECOVERY"
        if final_review
        else "SELECTED_SOURCE_FACT_RECOVERY"
    )
    restore_block = (
        _restore_final_review_transition_block
        if final_review
        else _restore_source_fact_transition_block
    )

    def blocked() -> int:
        restore_block(
            state,
            preimage,
            date=date,
            candidate_ids=values,
            reason_code=f"{reason_prefix}_COVER_REQUEUE_BLOCKED",
        )
        return -1

    pending = state.get("pending_talk")
    picks = state.get("picks")
    if not (
        len(rows) == 1
        and len(cover_rows) == 1
        and cover_rows[0][0] == "picks"
        and isinstance(pending, list)
        and isinstance(picks, list)
    ):
        return blocked()
    record = cover_rows[0][1]
    if (
        not final_review
        and int(record.get("talk_repair_retry_count") or 0)
        >= _runner.TALK_REPAIR_LIFETIME_RETRY_CAP
    ):
        return blocked()
    if final_review:
        from src.autoslice.selected_final_review_recovery import (
            PICK_TO_QUEUE_TRANSITION,
            RECOVERY_RECEIPT_FIELD,
            advance_selected_final_review_recovery_receipt as advance_receipt,
            validate_consumed_final_review_recovery_receipt as validate_consumed,
            validate_selected_final_review_recovery_receipt as validate_queue,
        )
    else:
        from src.autoslice.selected_source_fact_recovery import (
            PICK_TO_QUEUE_TRANSITION,
            RECOVERY_RECEIPT_FIELD,
            advance_selected_source_fact_recovery_receipt as advance_receipt,
            validate_consumed_source_fact_recovery_receipt as validate_consumed,
            validate_selected_source_fact_recovery_receipt as validate_queue,
        )

    grant_id = (
        _final_review_grant_id(preimage)
        if final_review
        else _source_fact_grant_id(preimage)
    )
    receipt = record.get(RECOVERY_RECEIPT_FIELD)
    if not validate_consumed(
        receipt,
        candidate_id=candidate_id,
        grant_id=grant_id,
        consumed_row=record,
    ):
        return blocked()
    try:
        item = _recovery_queue_item(
            date,
            record,
            candidate_id=candidate_id,
            retry_reason=(
                "selected_final_review_cover_pending_full_producer_retry"
                if final_review
                else "selected_source_fact_cover_pending_full_producer_retry"
            ),
            selected_repair=True,
            given_end_ms=record.get("given_end_ms"),
            given_end_authority=record.get("given_end_authority"),
            recovery_publication_authority=record.get(
                "recovery_publication_authority"
            ),
            provider_budget_history=(
                state.get("talk_superseded_attempts") or ()
            ),
        )
        carried_receipt = item.pop(RECOVERY_RECEIPT_FIELD, None)
        if carried_receipt is not None and carried_receipt != receipt:
            raise ValueError("cover requeue changed the source-fact receipt")
        next_receipt = advance_receipt(
            receipt,
            from_row=record,
            to_row=item,
            candidate_id=candidate_id,
            grant_id=grant_id,
            transition_kind=PICK_TO_QUEUE_TRANSITION,
        )
        item[RECOVERY_RECEIPT_FIELD] = next_receipt
        current_fingerprint = next_receipt.get(
            "current_failure_recovery_fingerprint"
        )
        if not (
            isinstance(current_fingerprint, str)
            and validate_queue(
                next_receipt,
                queued_row=item,
                candidate_id=candidate_id,
                grant_id=grant_id,
                current_fingerprint=current_fingerprint,
            )
        ):
            raise ValueError("cover requeue receipt did not bind the queue row")
    except Exception:  # noqa: BLE001 - this transition owns a full rollback
        return blocked()
    state["picks"] = [row for row in picks if row is not record]
    pending.append(item)
    return 1


def _requeue_source_fact_cover_pending(
    date: str,
    state: dict,
    *,
    candidate_ids: tuple[str, ...] | None,
) -> int:
    return _requeue_receipt_bound_cover_pending(
        date,
        state,
        candidate_ids=candidate_ids,
        final_review=False,
    )


def _requeue_final_review_cover_pending(
    date: str,
    state: dict,
    *,
    candidate_ids: tuple[str, ...] | None,
) -> int:
    return _requeue_receipt_bound_cover_pending(
        date,
        state,
        candidate_ids=candidate_ids,
        final_review=True,
    )


def _remove_reconcilable_redundant_stale_hold(
    state: dict, candidate_id: str
) -> dict | None:
    """Remove only the exact stale hold copied from a sealed rejection head."""

    from src.autoslice.published_topic_collision import REVIEW_STATE_FIELD
    from src.autoslice.published_topic_selected_rejection import (
        reconcilable_redundant_stale_hold_index,
    )

    index = reconcilable_redundant_stale_hold_index(state, candidate_id)
    if index is None:
        return None
    current = state.get(REVIEW_STATE_FIELD)
    holds = current.get("holds") if isinstance(current, Mapping) else None
    if not isinstance(holds, list) or not (0 <= index < len(holds)):
        return None
    next_review = deepcopy(dict(current))
    next_holds = deepcopy(holds)
    del next_holds[index]
    next_review["holds"] = next_holds
    state[REVIEW_STATE_FIELD] = next_review
    return deepcopy(next_review)


def freeze(state: Mapping[str, object], *, date: str) -> tuple[str, ...] | None:
    """Return the immutable Talk-only allowlist for this tick, if any."""

    # Canonical admission already recognizes a receipt-bound cover-pending pick
    # as OUTSTANDING.  Never reconstruct scope locally after an admission BLOCK:
    # the block may be caused by a duplicate/Song conflict that this module must
    # not bypass merely because the pick receipt itself remains valid.
    return operator_talk_scope(state, date=date)


_FINAL_REVIEW_HANDOFF_BLOCK = "SELECTED_FINAL_REVIEW_TOPIC_LINEAGE_HANDOFF_BLOCKED"


def _block_final_review_handoff(
    date: str,
    state: dict,
    *,
    preimage: Mapping[str, object],
    candidate_ids: tuple[str, ...],
) -> None:
    _restore_final_review_transition_block(
        state,
        preimage,
        date=date,
        candidate_ids=candidate_ids,
        reason_code=_FINAL_REVIEW_HANDOFF_BLOCK,
    )


def _prepare_final_review_handoff(
    date: str,
    state: dict,
    *,
    automatic_maintenance: bool,
    candidate_ids: tuple[str, ...] | None,
) -> tuple[dict | None, tuple[str, ...], bool]:
    """Capture the transaction preimage only for a fresh, valid v7 handoff."""

    values = _final_review_scope_candidate_ids(state, candidate_ids)
    if not automatic_maintenance or len(values) != 1:
        return None, values, True
    from src.autoslice.published_topic_final_review_handoff import (
        HANDOFF_ABSENT,
        HANDOFF_READY,
        inspect_initial_final_review_handoff,
    )

    preimage = deepcopy(dict(state))
    try:
        disposition = inspect_initial_final_review_handoff(
            state,
            candidate_id=values[0],
            recording_date=date,
        )
    except Exception:  # noqa: BLE001 - this pure authority probe fails closed
        disposition = None
    if dict(state) != preimage or disposition not in {HANDOFF_ABSENT, HANDOFF_READY}:
        _block_final_review_handoff(
            date,
            state,
            preimage=preimage,
            candidate_ids=values,
        )
        return None, values, False
    return (preimage if disposition == HANDOFF_READY else None), values, True


def _seal_final_review_handoff(
    date: str,
    state: dict,
    *,
    preimage: Mapping[str, object] | None,
    candidate_ids: tuple[str, ...],
) -> bool:
    """Commit the terminal marker or restore the full transaction preimage."""

    if preimage is None:
        return True
    from src.autoslice.published_topic_final_review_handoff import (
        seal_published_topic_final_review_handoff,
    )

    try:
        sealed = seal_published_topic_final_review_handoff(
            state,
            candidate_ids[0],
            pre_state=preimage,
            recording_date=date,
        )
    except Exception:  # noqa: BLE001 - full preimage is the transaction owner
        sealed = False
    if sealed:
        return True
    _block_final_review_handoff(
        date,
        state,
        preimage=preimage,
        candidate_ids=candidate_ids,
    )
    return False


def maintain(
    date: str,
    state: dict,
    *,
    automatic_maintenance: bool,
    candidate_ids: tuple[str, ...] | None,
) -> tuple[int, int, int, int, bool]:
    if _matching_source_fact_runtime_block(
        state, date=date, candidate_ids=candidate_ids
    ) or _matching_final_review_runtime_block(
        state, date=date, candidate_ids=candidate_ids
    ):
        return 0, 0, 0, 0, False
    (
        final_review_handoff_preimage,
        final_review_values,
        final_review_handoff_ready,
    ) = _prepare_final_review_handoff(
        date,
        state,
        automatic_maintenance=automatic_maintenance,
        candidate_ids=candidate_ids,
    )
    if not final_review_handoff_ready:
        return 0, 0, 0, 0, False
    held_current_requeued = 0
    topic_cover_requeued = 0
    source_fact_cover_requeued = 0
    final_review_cover_requeued = 0
    topic_retry_preimage: dict | None = None
    topic_retry_expected_review_state: dict | None = None
    block = state.get(STATE_KEY)
    if automatic_maintenance and _source_fact_scope_candidate_ids(
        state, candidate_ids
    ):
        source_fact_cover_requeued = _requeue_source_fact_cover_pending(
            date,
            state,
            candidate_ids=candidate_ids,
        )
        if source_fact_cover_requeued < 0:
            return 0, 0, 0, 0, False
    if automatic_maintenance and _final_review_scope_candidate_ids(
        state, candidate_ids
    ):
        try:
            final_review_cover_requeued = _requeue_final_review_cover_pending(
                date, state, candidate_ids=candidate_ids
            )
        except Exception:  # noqa: BLE001 - handoff owns every post-probe failure
            if final_review_handoff_preimage is None:
                raise
            final_review_cover_requeued = -1
        if final_review_cover_requeued < 0:
            if final_review_handoff_preimage is not None:
                _block_final_review_handoff(
                    date,
                    state,
                    preimage=final_review_handoff_preimage,
                    candidate_ids=final_review_values,
                )
            return 0, 0, 0, 0, False
    if (
        automatic_maintenance
        and len(candidate_ids or ()) == 1
        and isinstance(block, Mapping)
        and block.get("schema_version") == TOPIC_HOLD_RECOVERY_GRANT_SCHEMA
        and block.get("intent") == TOPIC_HOLD_RECOVERY_INTENT
        and block.get("candidate_ids") == list(candidate_ids or ())
        and block.get("upload_allowed") is False
    ):
        from src.autoslice.published_topic_collision import (
            RECOVERY_BLOCKED,
            RECOVERY_READY_TO_RELEASE,
            RECOVERY_RELEASED_QUEUED,
            RECOVERY_RELEASED_RETRY_PENDING,
            advance_published_topic_resolution_recovery,
            inspect_published_topic_resolution_recovery,
            release_resolved_published_topic_hold,
        )

        preimage = deepcopy(dict(state))
        candidate_id = tuple(candidate_ids or ())[0]
        try:
            disposition = inspect_published_topic_resolution_recovery(
                state, candidate_id
            )
        except Exception:  # noqa: BLE001 - runtime authority must fail closed
            disposition = RECOVERY_BLOCKED
        if dict(state) != preimage:
            # Inspection is a pure authority probe.  A mutating implementation
            # cannot be trusted to authorize the one allowed state transition.
            disposition = RECOVERY_BLOCKED
        if disposition == RECOVERY_READY_TO_RELEASE:
            try:
                released = release_resolved_published_topic_hold(state, candidate_id)
                released_state = deepcopy(dict(state))
                post_release = inspect_published_topic_resolution_recovery(
                    state, candidate_id
                )
                if dict(state) != released_state:
                    post_release = RECOVERY_BLOCKED
            except Exception:  # noqa: BLE001 - release must be transactional here
                released = False
                post_release = RECOVERY_BLOCKED
            release_ready = released and post_release == RECOVERY_RELEASED_QUEUED
        else:
            release_ready = disposition in {
                RECOVERY_RELEASED_QUEUED,
                RECOVERY_RELEASED_RETRY_PENDING,
            }
        if not release_ready:
            state.clear()
            state.update(preimage)
            state["operator_processing_scope_runtime_block"] = {
                "schema_version": "operator-processing-scope-runtime-block.v1",
                "recording_date": date,
                "candidate_ids": list(candidate_ids),
                "intent": TOPIC_HOLD_RECOVERY_INTENT,
                "upload_allowed": False,
                "reason_code": (
                    "TOPIC_DEDUP_HOLD_RELEASE_FAILED"
                    if disposition == RECOVERY_READY_TO_RELEASE
                    else "TOPIC_DEDUP_RELEASE_STATE_BLOCKED"
                ),
            }
            return 0, 0, 0, 0, False
        state.pop("operator_processing_scope_runtime_block", None)
        if disposition == RECOVERY_RELEASED_RETRY_PENDING:
            topic_retry_preimage = deepcopy(dict(state))
            try:
                topic_retry_expected_review_state = (
                    _remove_reconcilable_redundant_stale_hold(
                        state, candidate_id
                    )
                )
                if topic_retry_expected_review_state is not None:
                    post_reconcile_preimage = deepcopy(dict(state))
                    post_reconcile = inspect_published_topic_resolution_recovery(
                        state, candidate_id
                    )
                    if (
                        dict(state) != post_reconcile_preimage
                        or post_reconcile != RECOVERY_RELEASED_RETRY_PENDING
                    ):
                        raise ValueError(
                            "redundant stale hold reconciliation did not preserve "
                            "retry authority"
                        )
            except Exception:  # noqa: BLE001 - reconciliation is transactional
                _restore_topic_transition_block(
                    state,
                    topic_retry_preimage,
                    date=date,
                    candidate_ids=tuple(candidate_ids or ()),
                    reason_code="TOPIC_DEDUP_STALE_HOLD_RECONCILIATION_BLOCKED",
                )
                return 0, 0, 0, 0, False
            topic_cover_requeued = _requeue_topic_cover_pending(
                date, state, candidate_id
            )
    if (
        automatic_maintenance
        and candidate_ids
        and isinstance(block, Mapping)
        and block.get("intent") == HELD_CURRENT_RERENDER_INTENT
    ):
        from src.autoslice.held_current_talk_rerender import (
            HeldCurrentTalkRerenderError,
            requeue_named_held_current_talk_for_review,
        )

        try:
            held_current_requeued = requeue_named_held_current_talk_for_review(
                date,
                state,
                candidate_ids=candidate_ids,
                grant_id=str(block.get("grant_id") or ""),
            )
        except HeldCurrentTalkRerenderError as exc:
            state["operator_processing_scope_runtime_block"] = {
                "schema_version": "operator-processing-scope-runtime-block.v1",
                "recording_date": date,
                "candidate_ids": list(candidate_ids),
                "intent": HELD_CURRENT_RERENDER_INTENT,
                "upload_allowed": False,
                "reason_code": str(exc),
            }
    try:
        result = maintain_delivery_recovery_scope(
            date,
            state,
            automatic_maintenance=automatic_maintenance,
            talk_candidate_ids=candidate_ids,
        )
    except Exception:  # noqa: BLE001 - typed transition owns full rollback
        if final_review_handoff_preimage is not None:
            _block_final_review_handoff(
                date,
                state,
                preimage=final_review_handoff_preimage,
                candidate_ids=final_review_values,
            )
            return 0, 0, 0, 0, False
        if topic_retry_preimage is None:
            raise
        _restore_topic_transition_block(
            state,
            topic_retry_preimage,
            date=date,
            candidate_ids=tuple(candidate_ids or ()),
            reason_code="TOPIC_DEDUP_RETRY_TRANSITION_BLOCKED",
        )
        return 0, 0, 0, 0, False
    if not _seal_final_review_handoff(
        date,
        state,
        preimage=final_review_handoff_preimage,
        candidate_ids=final_review_values,
    ):
        return 0, 0, 0, 0, False
    if topic_retry_preimage is not None:
        candidate_id = tuple(candidate_ids or ())[0]

        def target_rows(value: Mapping[str, object]) -> list[tuple[str, Mapping[str, object]]]:
            return [
                (collection, row)
                for collection in (
                    "pending_talk",
                    "talk_backlog",
                    "picks",
                    "talk_below_confidence_threshold",
                )
                for row in (
                    value.get(collection)
                    if isinstance(value.get(collection), list)
                    else []
                )
                if isinstance(row, Mapping)
                and str(row.get("cid") or row.get("candidate_id") or "")
                == candidate_id
            ]

        before_rows = target_rows(topic_retry_preimage)
        after_rows = target_rows(state)
        transition_ok = False
        review_state_unchanged = (
            topic_retry_expected_review_state is None
            or state.get("published_topic_dedup_review")
            == topic_retry_expected_review_state
        )
        if before_rows == after_rows and topic_retry_expected_review_state is None:
            # Cooldown/fingerprint/budget policy legitimately kept the exact
            # failed pick in place; it remains OUTSTANDING for a later tick.
            transition_ok = True
        elif (
            review_state_unchanged
            and len(before_rows) == 1
            and before_rows[0][0] == "picks"
            and len(after_rows) == 1
            and after_rows[0][0] in {"pending_talk", "talk_backlog"}
        ):
            try:
                transition_ok = advance_published_topic_resolution_recovery(
                    state,
                    candidate_id,
                    pre_state=topic_retry_preimage,
                    from_collection=before_rows[0][0],
                    from_row=before_rows[0][1],
                    to_collection=after_rows[0][0],
                    to_row=after_rows[0][1],
                )
            except Exception:  # noqa: BLE001 - rollback owns every failed seal
                transition_ok = False
        if not transition_ok:
            state.clear()
            state.update(topic_retry_preimage)
            state["operator_processing_scope_runtime_block"] = {
                "schema_version": "operator-processing-scope-runtime-block.v1",
                "recording_date": date,
                "candidate_ids": list(candidate_ids or ()),
                "intent": TOPIC_HOLD_RECOVERY_INTENT,
                "upload_allowed": False,
                "reason_code": "TOPIC_DEDUP_RETRY_TRANSITION_BLOCKED",
            }
            return 0, 0, 0, 0, False
    recovered_songs, stale_talks, failed_talks, blocked_songs, baseline_changed = result
    return (
        recovered_songs,
        stale_talks + held_current_requeued,
        failed_talks
        + topic_cover_requeued
        + source_fact_cover_requeued
        + final_review_cover_requeued,
        blocked_songs,
        baseline_changed,
    )


def work_flags(
    date: str,
    state: dict,
    *,
    automatic_maintenance: bool,
    candidate_ids: tuple[str, ...] | None,
) -> tuple[bool, bool, bool]:
    block = state.get("operator_processing_scope_runtime_block")
    if (
        isinstance(block, Mapping)
        and block.get("schema_version") == "operator-processing-scope-runtime-block.v1"
        and block.get("intent")
        in {
            TOPIC_HOLD_RECOVERY_INTENT,
            SOURCE_FACT_RECOVERY_INTENT,
            FINAL_REVIEW_RECOVERY_INTENT,
        }
        and block.get("recording_date") == date
        and block.get("candidate_ids") == list(candidate_ids or ())
        and block.get("upload_allowed") is False
    ):
        return False, False, False
    return semantic_chat_refresh.runner_date_work_flags(
        date,
        state,
        automatic_maintenance=automatic_maintenance,
        talk_candidate_ids=candidate_ids,
    )


def discover(date: str, state: dict, candidate_ids: tuple[str, ...] | None) -> None:
    if candidate_ids is None:
        _runner.discover_segments(date, state)


def prioritize_and_capture(
    date: str,
    state: dict,
    candidate_ids: tuple[str, ...] | None,
) -> list[dict] | None:
    allowed = set(candidate_ids) if candidate_ids is not None else None
    capture = [
        dict(item)
        for item in state.get("pending_talk", [])
        if isinstance(item, dict)
        and (
            allowed is None
            or str(item.get("cid") or item.get("candidate_id") or "") in allowed
        )
    ]
    preimage = _topic_transition_preimage(state, candidate_ids)
    final_review_preimage = _final_review_transition_preimage(state, candidate_ids)
    reprioritize(state, candidate_ids)
    if final_review_preimage is not None and not _seal_final_review_queue_rebound(
        date,
        state,
        candidate_ids=candidate_ids,
        preimage=final_review_preimage,
        phase=RECOVERY_REBOUND_PRODUCTION_PREPARE,
        reason_code="SELECTED_FINAL_REVIEW_RECOVERY_PRIORITIZE_BLOCKED",
    ):
        return None
    if preimage is not None:
        from src.autoslice.published_topic_collision import (
            seal_published_topic_resolution_row_rebounds,
        )

        values = _topic_scope_candidate_ids(preimage, candidate_ids)
        if not seal_published_topic_resolution_row_rebounds(
            state,
            pre_state=preimage,
            phase="PRODUCTION_PREPARE",
            candidate_ids=values,
        ):
            _restore_topic_transition_block(
                state,
                preimage,
                date=date,
                candidate_ids=values,
                reason_code="TOPIC_DEDUP_PRIORITIZE_TRANSITION_BLOCKED",
            )
            return None
    return capture


def reprioritize(state: dict, candidate_ids: tuple[str, ...] | None) -> None:
    topic_review_already_terminal = bool(
        _terminal_final_review_handoff_candidate_ids(state, candidate_ids)
    )
    _runner.prioritize(
        state,
        frozen_talk_candidate_ids=candidate_ids,
        allow_song_work=candidate_ids is None,
        allow_published_topic_review=not bool(
            _topic_scope_candidate_ids(state, candidate_ids)
        )
        and not topic_review_already_terminal,
    )


def prepare_production_context(
    date: str,
    state: dict,
    candidate_ids: tuple[str, ...] | None,
) -> tuple[bool, dict | None]:
    """Attach deterministic routing/name inputs and seal the resulting queue row."""

    preimage = _topic_transition_preimage(state, candidate_ids)
    final_review_preimage = _final_review_transition_preimage(state, candidate_ids)
    items = _runner.scoped_pending_talk_items(state, candidate_ids)
    routing_claim = _runner.prepare_speaker_routing(date, items, state=state)
    song_names = _runner.collect_song_name_candidates(date, state)
    if song_names:
        for item in _runner.scoped_pending_talk_items(state, candidate_ids):
            if isinstance(item, dict):
                item["song_name_candidates"] = song_names
    if final_review_preimage is not None and not _seal_final_review_queue_rebound(
        date,
        state,
        candidate_ids=candidate_ids,
        preimage=final_review_preimage,
        phase=RECOVERY_REBOUND_PRODUCTION_PREPARE,
        reason_code="SELECTED_FINAL_REVIEW_RECOVERY_PRODUCTION_PREPARE_BLOCKED",
    ):
        return False, None
    if preimage is None:
        return True, routing_claim
    from src.autoslice.published_topic_collision import (
        seal_published_topic_resolution_row_rebounds,
    )

    values = _topic_scope_candidate_ids(preimage, candidate_ids)
    if seal_published_topic_resolution_row_rebounds(
        state,
        pre_state=preimage,
        phase="PRODUCTION_PREPARE",
        candidate_ids=values,
    ):
        return True, routing_claim
    _restore_topic_transition_block(
        state,
        preimage,
        date=date,
        candidate_ids=values,
        reason_code="TOPIC_DEDUP_PRODUCTION_PREPARE_TRANSITION_BLOCKED",
    )
    return False, None


def production_preimage(
    state: Mapping[str, object], candidate_ids: tuple[str, ...] | None
) -> dict | None:
    """Capture the exact v5-v7 input immediately before producer dispatch."""

    return (
        deepcopy(dict(state))
        if (
            _source_fact_scope_candidate_ids(state, candidate_ids)
            or _final_review_scope_candidate_ids(state, candidate_ids)
        )
        else _topic_transition_preimage(state, candidate_ids)
    )


def seal_production_transition(
    date: str,
    state: dict,
    candidate_ids: tuple[str, ...] | None,
    preimage: Mapping[str, object] | None,
) -> bool:
    """Seal exact prepared queue -> result/queue before runner persistence."""

    if preimage is None:
        return True
    source_fact_values = _source_fact_scope_candidate_ids(preimage, candidate_ids)
    final_review_values = _final_review_scope_candidate_ids(preimage, candidate_ids)
    receipt_values = source_fact_values or final_review_values
    if len(receipt_values) == 1:
        final_review = bool(final_review_values)
        if final_review:
            from src.autoslice.selected_final_review_recovery import (
                QUEUE_TO_PICK_TRANSITION,
                RECOVERY_RECEIPT_FIELD,
                advance_selected_final_review_recovery_receipt as advance_receipt,
                validate_consumed_final_review_recovery_receipt as validate_consumed,
                validate_selected_final_review_recovery_receipt as validate_queue,
            )
        else:
            from src.autoslice.selected_source_fact_recovery import (
                QUEUE_TO_PICK_TRANSITION,
                RECOVERY_RECEIPT_FIELD,
                advance_selected_source_fact_recovery_receipt as advance_receipt,
                validate_consumed_source_fact_recovery_receipt as validate_consumed,
                validate_selected_source_fact_recovery_receipt as validate_queue,
            )

        candidate_id = receipt_values[0]
        grant_id = (
            _final_review_grant_id(preimage)
            if final_review
            else _source_fact_grant_id(preimage)
        )
        before_rows = _target_talk_rows(preimage, candidate_id)
        after_rows = _target_talk_rows(state, candidate_id)
        transition_ok = False
        if (
            len(before_rows) == 1
            and before_rows[0][0] in _QUEUED_TALK_COLLECTIONS
            and len(after_rows) == 1
        ):
            before_row = before_rows[0][1]
            receipt = before_row.get(RECOVERY_RECEIPT_FIELD)
            declared_current = (
                receipt.get("current_failure_recovery_fingerprint")
                if isinstance(receipt, Mapping)
                else None
            )
            before_valid = bool(
                isinstance(declared_current, str)
                and validate_queue(
                    receipt,
                    queued_row=before_row,
                    candidate_id=candidate_id,
                    grant_id=grant_id,
                    current_fingerprint=declared_current,
                    allow_queue_rebound=True,
                )
            )
            after_collection, after_row = after_rows[0]
            if (
                before_valid
                and after_collection in _QUEUED_TALK_COLLECTIONS
            ):
                transition_ok = bool(
                    after_row.get(RECOVERY_RECEIPT_FIELD) == receipt
                    and validate_queue(
                        receipt,
                        queued_row=after_row,
                        candidate_id=candidate_id,
                        grant_id=grant_id,
                        current_fingerprint=str(declared_current),
                        allow_queue_rebound=True,
                    )
                )
            elif (
                before_valid
                and after_collection == "picks"
                and after_row.get(RECOVERY_RECEIPT_FIELD) == receipt
            ):
                try:
                    final_row = deepcopy(after_row)
                    final_row.pop(RECOVERY_RECEIPT_FIELD, None)
                    next_receipt = advance_receipt(
                        receipt,
                        from_row=before_row,
                        to_row=final_row,
                        candidate_id=candidate_id,
                        grant_id=grant_id,
                        transition_kind=QUEUE_TO_PICK_TRANSITION,
                        allow_queue_rebound=True,
                    )
                    after_row[RECOVERY_RECEIPT_FIELD] = next_receipt
                    transition_ok = (
                        validate_consumed(
                            next_receipt,
                            candidate_id=candidate_id,
                            grant_id=grant_id,
                            consumed_row=after_row,
                        )
                    )
                except Exception:  # noqa: BLE001 - rollback owns this commit point
                    transition_ok = False
        if transition_ok:
            return True
        restore_block = (
            _restore_final_review_transition_block
            if final_review
            else _restore_source_fact_transition_block
        )
        restore_block(
            state,
            preimage,
            date=date,
            candidate_ids=receipt_values,
            reason_code=(
                "SELECTED_FINAL_REVIEW_RECOVERY_PRODUCTION_TRANSITION_BLOCKED"
                if final_review
                else "SELECTED_SOURCE_FACT_RECOVERY_PRODUCTION_TRANSITION_BLOCKED"
            ),
        )
        return False
    from src.autoslice.published_topic_collision import (
        seal_published_topic_resolution_production_transition,
    )

    values = _topic_scope_candidate_ids(preimage, candidate_ids)
    if len(values) == 1 and seal_published_topic_resolution_production_transition(
        state,
        values[0],
        pre_state=preimage,
    ):
        return True
    _restore_topic_transition_block(
        state,
        preimage,
        date=date,
        candidate_ids=values,
        reason_code="TOPIC_DEDUP_PRODUCTION_TRANSITION_BLOCKED",
    )
    return False


def repair_covers(
    date: str,
    state: dict,
    *,
    automatic_maintenance: bool,
    candidate_ids: tuple[str, ...] | None,
) -> None:
    if not automatic_maintenance:
        return
    if _topic_scope_candidate_ids(state, candidate_ids) or (
        _source_fact_scope_candidate_ids(state, candidate_ids)
    ) or (
        _final_review_scope_candidate_ids(state, candidate_ids)
    ):
        # v5-v7 bind every current row by full SHA.  Cover maintenance persists
        # several in-place partial states internally, so the released candidate
        # must instead requeue through the sealed full-producer path next tick.
        return
    if candidate_ids is None:
        _runner.repair_covers(date, state)
        return
    allowed = set(candidate_ids)
    scoped = {
        str(record.get("candidate_id") or record.get("cid") or "")
        for record in state.get("picks", [])
        if isinstance(record, dict)
        and str(record.get("candidate_id") or record.get("cid") or "") in allowed
        and _runner.cover_repair_needed(date, record)
    }
    if scoped:
        _runner.repair_covers(date, state, candidate_ids=scoped)
