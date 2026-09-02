"""Durable runner commit boundary for the one v6 source-fact recovery."""

from __future__ import annotations

from collections.abc import Callable, Mapping

from src.autoslice import (
    historical_failed_talk_scope,
    operator_processing_scope as operator_scope,
)
from src.autoslice.selected_source_fact_recovery import (
    RECOVERY_RECEIPT_FIELD,
    validate_selected_source_fact_recovery_receipt,
)


class SelectedSourceFactRecoveryPersistenceError(RuntimeError):
    """The v6 rejection-to-queue transition was not durably committed."""


_ACTIVE_TALK_COLLECTIONS = (
    "pending_talk",
    "talk_backlog",
    "picks",
    "talk_below_confidence_threshold",
)


def _candidate_id(row: object) -> str:
    if not isinstance(row, Mapping):
        return ""
    return str(row.get("candidate_id") or row.get("cid") or "").strip()


def capture_active_scope(
    state: Mapping[str, object],
    *,
    date: str,
    candidate_ids: tuple[str, ...] | None,
) -> tuple[str, str] | None:
    """Capture exactly one already-admitted v6 target before maintenance."""

    values = tuple(candidate_ids or ())
    block = state.get(operator_scope.STATE_KEY)
    if not (
        len(values) == 1
        and isinstance(block, Mapping)
        and block.get("schema_version") == operator_scope.SOURCE_FACT_RECOVERY_GRANT_SCHEMA
        and block.get("intent") == operator_scope.SOURCE_FACT_RECOVERY_INTENT
        and block.get("candidate_ids") == list(values)
        and block.get("upload_allowed") is False
        and isinstance(block.get("grant_id"), str)
        and block["grant_id"].strip() == block["grant_id"]
        and block["grant_id"]
    ):
        return None
    admission = operator_scope.operator_scope_admission(state, date=date)
    if not (
        admission.admitted
        and admission.candidate_ids == values
        and admission.grant_id == block["grant_id"]
        and values[0] in admission.outstanding_candidate_ids
    ):
        return None
    return values[0], block["grant_id"]


def _validate_head(
    state: Mapping[str, object],
    *,
    date: str,
    candidate_id: str,
    grant_id: str,
) -> None:
    matching: list[tuple[str, Mapping[str, object]]] = []
    for collection in _ACTIVE_TALK_COLLECTIONS:
        rows = state.get(collection)
        if not isinstance(rows, list):
            continue
        matching.extend(
            (collection, row)
            for row in rows
            if isinstance(row, Mapping) and _candidate_id(row) == candidate_id
        )
    if len(matching) != 1 or matching[0][0] != "pending_talk":
        raise SelectedSourceFactRecoveryPersistenceError(
            "SELECTED_SOURCE_FACT_RECOVERY_PERSISTENCE_QUEUE_HEAD_INVALID"
        )
    for collection in operator_scope.SONG_STATE_COLLECTIONS:
        rows = state.get(collection)
        if isinstance(rows, list) and any(_candidate_id(row) == candidate_id for row in rows):
            raise SelectedSourceFactRecoveryPersistenceError(
                "SELECTED_SOURCE_FACT_RECOVERY_PERSISTENCE_MIXED_SONG_STATE"
            )
    queue_row = matching[0][1]
    receipt = queue_row.get(RECOVERY_RECEIPT_FIELD)
    current_fingerprint = (
        receipt.get("current_failure_recovery_fingerprint")
        if isinstance(receipt, Mapping)
        else None
    )
    if not (
        isinstance(current_fingerprint, str)
        and validate_selected_source_fact_recovery_receipt(
            receipt,
            queued_row=queue_row,
            candidate_id=candidate_id,
            grant_id=grant_id,
            current_fingerprint=current_fingerprint,
        )
    ):
        raise SelectedSourceFactRecoveryPersistenceError(
            "SELECTED_SOURCE_FACT_RECOVERY_PERSISTENCE_RECEIPT_INVALID"
        )
    admission = operator_scope.operator_scope_admission(state, date=date)
    if not (
        admission.admitted
        and admission.grant_id == grant_id
        and admission.candidate_ids == (candidate_id,)
        and admission.outstanding_candidate_ids == (candidate_id,)
    ):
        raise SelectedSourceFactRecoveryPersistenceError(
            "SELECTED_SOURCE_FACT_RECOVERY_PERSISTENCE_SCOPE_DRIFT"
        )


def commit_captured_transition(
    state: dict,
    *,
    date: str,
    scope: tuple[str, str] | None,
    persist: Callable[[], None],
    readback: Callable[[], Mapping[str, object]],
    log: Callable[[str], None],
) -> tuple[str, str] | None:
    """Persist and reread a captured v6 head; no v3/generic path is claimed."""

    if scope is None:
        return None
    candidate_id, grant_id = scope
    _validate_head(state, date=date, candidate_id=candidate_id, grant_id=grant_id)
    persist()
    _validate_head(state, date=date, candidate_id=candidate_id, grant_id=grant_id)
    try:
        persisted = readback()
    except Exception as exc:  # noqa: BLE001 - corrupted/missing readback is fatal
        raise SelectedSourceFactRecoveryPersistenceError(
            "SELECTED_SOURCE_FACT_RECOVERY_PERSISTENCE_READBACK_INVALID"
        ) from exc
    if not isinstance(persisted, Mapping):
        raise SelectedSourceFactRecoveryPersistenceError(
            "SELECTED_SOURCE_FACT_RECOVERY_PERSISTENCE_READBACK_INVALID"
        )
    _validate_head(persisted, date=date, candidate_id=candidate_id, grant_id=grant_id)
    log(
        f"{date}: durably resumed selected-source-fact recovery {candidate_id} "
        f"under grant {grant_id}"
    )
    return scope


def maintain_and_persist(
    date: str,
    state: dict,
    *,
    automatic_maintenance: bool,
    candidate_ids: tuple[str, ...] | None,
    persist: Callable[[], None],
    readback: Callable[[], Mapping[str, object]],
    log: Callable[[str], None],
    song_pipeline_fingerprint: Callable[[], str],
) -> tuple[int, int, int, int, bool]:
    """Run maintenance and make an admitted v6 requeue a verified commit.

    This deliberately retains the runner's ordinary result tuple and its
    pre-existing persistence behavior.  Only a captured v6 grant replaces the
    count-based acknowledgement with a validated writeback/readback boundary.
    """

    scope = capture_active_scope(state, date=date, candidate_ids=candidate_ids)
    result = historical_failed_talk_scope.maintain(
        date,
        state,
        automatic_maintenance=automatic_maintenance,
        candidate_ids=candidate_ids,
    )
    scope = commit_captured_transition(
        state,
        date=date,
        scope=scope,
        persist=persist,
        readback=readback,
        log=log,
    )
    (
        recovered_song_deliveries,
        requeued_stale_talks,
        requeued_talks,
        requeued_songs,
        song_fingerprint_baseline_changed,
    ) = result
    if recovered_song_deliveries:
        persist()
        log(
            f"{date}: recovered {recovered_song_deliveries} verified song delivery "
            "package(s) without selector/ASR/LRC rerun"
        )
    if not scope and (
        any((requeued_stale_talks, requeued_talks, requeued_songs))
        or song_fingerprint_baseline_changed
    ):
        persist()
    if requeued_stale_talks or requeued_talks or requeued_songs:
        log(
            f"{date}: requeued {requeued_stale_talks} stale CURRENT talk package(s), "
            f"{requeued_talks} recoverable talk failure(s), and "
            f"{requeued_songs} recoverable song BLOCK(s) for song pipeline "
            f"{song_pipeline_fingerprint()[:19]}…"
        )
    return result


def validate_head_for_test(
    state: Mapping[str, object], *, date: str, candidate_id: str, grant_id: str
) -> None:
    """Narrow test seam; production uses :func:`commit_captured_transition`."""

    _validate_head(state, date=date, candidate_id=candidate_id, grant_id=grant_id)
