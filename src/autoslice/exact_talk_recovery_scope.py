"""Fail-closed song-work isolation for exact talk recovery runs."""

from __future__ import annotations

from collections.abc import Collection

from src.autoslice.candidate_selection import _exact_talk_contract_ids
from src.autoslice.runner_proxy import RunnerProxy
from src.autoslice.selected_source_fact_recovery import RECOVERY_RECEIPT_FIELD
from src.autoslice.selected_final_review_recovery import (
    RECOVERY_RECEIPT_FIELD as FINAL_REVIEW_RECOVERY_RECEIPT_FIELD,
)


_runner = RunnerProxy()


def _marker_bound_topic_candidate_ids(state: dict) -> set[str] | None:
    from src.autoslice.published_topic_collision import (
        RECOVERY_BLOCKED,
        RECOVERY_CONVERGED,
        PublishedTopicCollisionError,
        _recovery_ledger_entries,
        inspect_published_topic_resolution_recovery,
    )

    try:
        candidate_ids = tuple(_recovery_ledger_entries(state))
    except PublishedTopicCollisionError:
        return None
    protected: set[str] = set()
    for candidate_id in candidate_ids:
        try:
            disposition = inspect_published_topic_resolution_recovery(
                state, candidate_id
            )
        except Exception:  # noqa: BLE001 - malformed lineage stays protected
            disposition = RECOVERY_BLOCKED
        if disposition != RECOVERY_CONVERGED:
            protected.add(candidate_id)
    return protected


def _active_v5_topic_scope(
    state: dict, candidate_ids: Collection[str] | None
) -> bool:
    values = tuple(candidate_ids or ())
    block = state.get("operator_processing_scope")
    return bool(
        len(values) == 1
        and isinstance(block, dict)
        and block.get("schema_version") == "operator-processing-scope-grant.v5"
        and block.get("intent") == "RECOVER_NAMED_RESOLVED_TOPIC_DEDUP_HOLD"
        and block.get("candidate_ids") == list(values)
        and block.get("upload_allowed") is False
    )


def _source_fact_bound_candidate_ids(state: dict) -> set[str]:
    """Protect every durable v6 lineage from unscoped Talk maintenance.

    The superseded archive remains a witness if the current row's receipt was
    removed or corrupted.  Presence is enough here: malformed authority must
    become *more* protected, never equivalent to an unbound generic failure.
    """

    protected: set[str] = set()
    for collection in (
        "pending_talk",
        "talk_backlog",
        "picks",
        "talk_below_confidence_threshold",
        "talk_superseded_attempts",
    ):
        rows = state.get(collection)
        if not isinstance(rows, list):
            continue
        for row in rows:
            if (
                not isinstance(row, dict)
                or row.get(RECOVERY_RECEIPT_FIELD) is None
            ):
                continue
            candidate_id = str(
                row.get("candidate_id") or row.get("cid") or ""
            ).strip()
            if candidate_id:
                protected.add(candidate_id)
    return protected


def _active_v6_source_fact_scope(
    state: dict, candidate_ids: Collection[str] | None
) -> bool:
    values = tuple(candidate_ids or ())
    block = state.get("operator_processing_scope")
    return bool(
        len(values) == 1
        and isinstance(block, dict)
        and block.get("schema_version") == "operator-processing-scope-grant.v6"
        and block.get("intent")
        == "RECOVER_NAMED_SELECTED_SOURCE_FACT_REJECTION"
        and block.get("candidate_ids") == list(values)
        and block.get("upload_allowed") is False
    )


def _final_review_bound_candidate_ids(state: dict) -> set[str]:
    """Protect every durable v7 lineage from broad Talk maintenance."""

    protected: set[str] = set()
    for collection in (
        "pending_talk",
        "talk_backlog",
        "picks",
        "talk_below_confidence_threshold",
        "talk_superseded_attempts",
    ):
        rows = state.get(collection)
        if not isinstance(rows, list):
            continue
        for row in rows:
            if (
                not isinstance(row, dict)
                or row.get(FINAL_REVIEW_RECOVERY_RECEIPT_FIELD) is None
            ):
                continue
            candidate_id = str(
                row.get("candidate_id") or row.get("cid") or ""
            ).strip()
            if candidate_id:
                protected.add(candidate_id)
    return protected


def _active_v7_final_review_scope(
    state: dict, candidate_ids: Collection[str] | None
) -> bool:
    values = tuple(candidate_ids or ())
    block = state.get("operator_processing_scope")
    return bool(
        len(values) == 1
        and isinstance(block, dict)
        and block.get("schema_version") == "operator-processing-scope-grant.v7"
        and block.get("intent")
        == "RECOVER_NAMED_SELECTED_FINAL_REVIEW_REJECTION"
        and block.get("candidate_ids") == list(values)
        and block.get("upload_allowed") is False
    )


def suppress_exact_talk_recovery_song_work(
    state: dict, *, phase: str
) -> bool:
    """Clear active song queues that an exact talk contract cannot authorize."""

    if not _exact_talk_contract_ids(state):
        return False
    cleared: dict[str, dict[str, object]] = {}
    for key in ("pending_song", "song_backlog", "song_selection_backlog"):
        rows = state.get(key)
        if not isinstance(rows, list) or not rows:
            continue
        candidate_ids: list[str] = []
        legacy_row_count = 0
        for row in rows:
            if not isinstance(row, dict):
                legacy_row_count += 1
                continue
            candidate_id = str(
                row.get("candidate_id")
                or row.get("cid")
                or row.get("song_candidate_id")
                or ""
            ).strip()
            if candidate_id:
                candidate_ids.append(candidate_id)
        cleared[key] = {
            "row_count": len(rows),
            "candidate_ids": sorted(set(candidate_ids)),
            "legacy_row_count": legacy_row_count,
        }
        state[key] = []
    if not cleared:
        return False
    state.setdefault(
        "exact_talk_recovery_song_scope_suppressions", []
    ).append(
        {
            "schema_version": "exact-talk-recovery-song-scope-suppression.v1",
            "reason": "EXACT_TALK_RECOVERY_FORBIDS_SONG_WORK",
            "phase": phase,
            "cleared": cleared,
        }
    )
    return True


def maintain_delivery_recovery_scope(
    date: str,
    state: dict,
    *,
    automatic_maintenance: bool,
    talk_candidate_ids: Collection[str] | None = None,
) -> tuple[int, int, int, int, bool]:
    """Run talk/song maintenance without crossing an exact talk-only scope."""

    exact_talk_recovery = bool(_exact_talk_contract_ids(state))
    suppress_exact_talk_recovery_song_work(
        state, phase="before_delivery_recovery"
    )
    song_baseline = state.get("song_pipeline_fingerprint_baseline")
    if not automatic_maintenance:
        return 0, 0, 0, 0, False
    talk_only_recovery = talk_candidate_ids is not None
    marker_bound_ids = _marker_bound_topic_candidate_ids(state)
    source_fact_bound_ids = _source_fact_bound_candidate_ids(state)
    final_review_bound_ids = _final_review_bound_candidate_ids(state)
    ledger_invalid = marker_bound_ids is None
    if ledger_invalid:
        # A malformed durable lineage is not equivalent to no lineage.  Keep
        # broad maintenance from mutating any Talk row until the ledger is
        # repaired under its typed authority.
        marker_bound_ids = {
            str(row.get("candidate_id") or row.get("cid") or "")
            for row in (state.get("picks") or [])
            if isinstance(row, dict)
            and str(row.get("candidate_id") or row.get("cid") or "")
        }
    if exact_talk_recovery or talk_only_recovery:
        recovered_songs = 0
        stale_talks = (
            _runner.requeue_stale_current_recovery_talks(date, state)
            if exact_talk_recovery and not ledger_invalid
            else 0
        )
        allowed_talk_ids = (
            set(talk_candidate_ids or ())
            if talk_only_recovery
            else set(_exact_talk_contract_ids(state))
        )
        if ledger_invalid:
            allowed_talk_ids.clear()
        else:
            if not _active_v5_topic_scope(state, talk_candidate_ids):
                allowed_talk_ids -= marker_bound_ids
            if not _active_v6_source_fact_scope(state, talk_candidate_ids):
                allowed_talk_ids -= source_fact_bound_ids
            if not _active_v7_final_review_scope(state, talk_candidate_ids):
                allowed_talk_ids -= final_review_bound_ids
        failed_talks = (
            _runner.requeue_recoverable_talks(
                date,
                state,
                candidate_ids=allowed_talk_ids,
            )
            if (
                talk_only_recovery
                or marker_bound_ids
                or source_fact_bound_ids
                or final_review_bound_ids
            )
            else _runner.requeue_recoverable_talks(date, state)
        )
        blocked_songs = 0
    elif marker_bound_ids or source_fact_bound_ids or final_review_bound_ids:
        # Durable v5/v6 lineages may only advance under their strict single-CID
        # scopes.  Broad maintenance may still handle unrelated Talk/Song rows,
        # but cannot discard or advance either receipt shape.
        protected_ids = (
            marker_bound_ids | source_fact_bound_ids | final_review_bound_ids
        )
        allowed_talk_ids = {
            str(row.get("candidate_id") or row.get("cid") or "")
            for row in (state.get("picks") or [])
            if isinstance(row, dict)
            and str(row.get("candidate_id") or row.get("cid") or "")
            not in protected_ids
        }
        recovered_songs = _runner.recover_bound_song_deliveries(date, state)
        stale_talks = _runner.requeue_stale_current_recovery_talks(date, state)
        failed_talks = _runner.requeue_recoverable_talks(
            date, state, candidate_ids=allowed_talk_ids
        )
        blocked_songs = _runner.requeue_recoverable_songs(date, state)
    else:
        recovered_songs = _runner.recover_bound_song_deliveries(date, state)
        (
            stale_talks,
            failed_talks,
            blocked_songs,
        ) = _runner.requeue_recoverable_deliveries(date, state)
    song_baseline_changed = (
        state.get("song_pipeline_fingerprint_baseline") != song_baseline
    )
    return (
        recovered_songs,
        stale_talks,
        failed_talks,
        blocked_songs,
        song_baseline_changed,
    )
