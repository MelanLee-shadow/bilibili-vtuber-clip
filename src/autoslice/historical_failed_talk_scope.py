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
    HELD_CURRENT_RERENDER_INTENT,
    STATE_KEY,
    TOPIC_HOLD_RECOVERY_GRANT_SCHEMA,
    TOPIC_HOLD_RECOVERY_INTENT,
    operator_talk_scope,
)
from src.autoslice.runner_proxy import RunnerProxy
from src.autoslice import semantic_evidence_scorecard_refresh as semantic_chat_refresh


_runner = RunnerProxy()

_RUNTIME_BLOCK_KEY = "operator_processing_scope_runtime_block"
_RUNTIME_BLOCK_SCHEMA = "operator-processing-scope-runtime-block.v1"


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


def _topic_transition_preimage(
    state: Mapping[str, object], candidate_ids: tuple[str, ...] | None
) -> dict | None:
    return (
        deepcopy(dict(state))
        if _topic_scope_candidate_ids(state, candidate_ids)
        else None
    )


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
    changed = _runner.annotate_state_sessions(
        date,
        state,
        include_song_rows=include_song_rows,
        talk_candidate_ids=candidate_ids,
    )
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
    result = semantic_chat_refresh.refresh_operator_scoped_chat_scorecards(date, state)
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
        )
    except (OSError, TypeError, ValueError, RecoveryReviewRerunError):
        return 0
    state["picks"] = [row for row in picks if row is not record]
    pending.append(item)
    return 1


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

    return operator_talk_scope(state, date=date)


def maintain(
    date: str,
    state: dict,
    *,
    automatic_maintenance: bool,
    candidate_ids: tuple[str, ...] | None,
) -> tuple[int, int, int, int, bool]:
    held_current_requeued = 0
    topic_cover_requeued = 0
    topic_retry_preimage: dict | None = None
    topic_retry_expected_review_state: dict | None = None
    block = state.get(STATE_KEY)
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
    except Exception:  # noqa: BLE001 - v5 transition must not persist half a retry
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
        failed_talks + topic_cover_requeued,
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
        and block.get("intent") == TOPIC_HOLD_RECOVERY_INTENT
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
    reprioritize(state, candidate_ids)
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
    _runner.prioritize(
        state,
        frozen_talk_candidate_ids=candidate_ids,
        allow_song_work=candidate_ids is None,
        allow_published_topic_review=not bool(
            _topic_scope_candidate_ids(state, candidate_ids)
        ),
    )


def prepare_production_context(
    date: str,
    state: dict,
    candidate_ids: tuple[str, ...] | None,
) -> tuple[bool, dict | None]:
    """Attach deterministic routing/name inputs and seal the resulting queue row."""

    preimage = _topic_transition_preimage(state, candidate_ids)
    items = _runner.scoped_pending_talk_items(state, candidate_ids)
    routing_claim = _runner.prepare_speaker_routing(date, items, state=state)
    song_names = _runner.collect_song_name_candidates(date, state)
    if song_names:
        for item in _runner.scoped_pending_talk_items(state, candidate_ids):
            if isinstance(item, dict):
                item["song_name_candidates"] = song_names
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
    """Capture the exact v5 input row immediately before producer dispatch."""

    return _topic_transition_preimage(state, candidate_ids)


def seal_production_transition(
    date: str,
    state: dict,
    candidate_ids: tuple[str, ...] | None,
    preimage: Mapping[str, object] | None,
) -> bool:
    """Seal exact prepared queue -> result/queue before runner persistence."""

    if preimage is None:
        return True
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
    if _topic_scope_candidate_ids(state, candidate_ids):
        # v5 binds every current row by full SHA.  Cover maintenance persists
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
