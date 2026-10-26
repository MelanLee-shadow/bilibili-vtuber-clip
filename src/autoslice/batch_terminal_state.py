"""Terminal state projection for ordinary and exact recovery batches."""

from __future__ import annotations

import time
from collections.abc import Collection
from typing import Mapping

from src.autoslice.publication_reconciliation import (
    project_publication_closure,
    publication_row_is_verified,
)


# Batch-terminal states retain their original meaning. A paused provider is
# handled separately and only for one already completed, nonqueued candidate.
REVIEWABLE_BATCH_STATUSES = frozenset(
    {
        "review_ready", "review_ready_with_failures", "review_ready_retry_wait",
        "publication_in_progress", "ready_unpublished", "ready_unpublished_with_failures",
    }
)


def candidate_package_review_allowed(state: Mapping[str, object], candidate_id: str) -> bool:
    """Admission only; never resume a provider, change state or authorize upload."""
    status = str(state.get("status") or "")
    if status in REVIEWABLE_BATCH_STATUSES:
        return True
    if status != "paused_cpa_down" or not candidate_id:
        return False
    picks = state.get("picks")
    if not isinstance(picks, list) or any(not isinstance(row, Mapping) for row in picks):
        return False
    matches = [row for row in picks if row.get("candidate_id") == candidate_id]
    if len(matches) != 1:
        return False
    pick = matches[0]
    if (
        pick.get("status") != "review_ready"
        or type(pick.get("rc")) is not int
        or pick["rc"] != 0
        or publication_row_is_verified(pick)
    ):
        return False
    for collection in ("pending_talk", "pending_song", "songs"):
        rows = state.get(collection, [])
        if not isinstance(rows, list) or any(not isinstance(row, Mapping) for row in rows):
            return False
        if any(candidate_id in (row.get("candidate_id"), row.get("cid")) for row in rows):
            return False
    return True


SONG_TERMINAL_DISPOSITION_SCHEMA_VERSION = "song-terminal-disposition.v1"
# A reference/clock proof failure says nothing decisive about the performance.
# Only the separately typed performer/content observations authorize rejection.
SONG_PROOF_UNRESOLVED_CODES = frozenset({
    'SONG_AUDIO_LRC_IDENTITY_AMBIGUOUS', 'SONG_AUDIO_LRC_ALIGNMENT_INVALID',
    'SONG_LRC_REFERENCE_UNRESOLVED', 'SONG_AUDIO_LRC_PROOF_UNRESOLVED',
})


def song_infra_transient_is_active(
    *,
    record: Mapping[str, object],
    reason_codes: Collection[object],
    infra_transient_reason_codes: Collection[object],
    retry_cap: int | None = None,
    fallback_to_reason_codes: bool = False,
) -> bool:
    """Keep actual provider failures separate from unresolved song proof.

    A reference/clock failure does not establish an outage through an old
    preliminary provider hint. Explicit service failures remain waitable;
    the terminal owner separately applies the cap to typed performer vetoes.
    """

    infra_codes = {str(code) for code in infra_transient_reason_codes}
    reasons = {str(code) for code in reason_codes or ()}
    explicit = str(record.get("transient_failure_code") or "")
    if not explicit:
        # Pre-``PUBLIC_CASE_ID`` records have no typed field.  Only the requeue caller
        # opts into the reason-code fallback; terminal projection deliberately
        # honours the typed field alone, so an untyped record with a confirmed
        # non-host performance still terminalizes exactly as it did before.
        return fallback_to_reason_codes and bool(reasons & infra_codes) and not (
            reasons & SONG_PROOF_UNRESOLVED_CODES
        )
    if explicit not in infra_codes:
        return False
    return True


def project_terminal_song_disposition(
    record: dict,
    *,
    terminal_performer_rejection_codes: Collection[object] = (),
    infra_transient_reason_codes: Collection[object] = (),
    song_infra_retry_cap: int | None = None,
) -> bool:
    """Project a completed negative song proof into a durable terminal receipt.

    A reference/clock proof failure remains blocked; it is not evidence of an
    unsuitable performance. Confirmed performer negatives retain their typed
    terminal meaning. Existing terminal records are not reopened here.
    """

    if record.get("delivered") or record.get("verified_delivery_pending_commit") is True:
        return False
    if record.get("status") == "candidate_rejected":
        return True
    if record.get("status") not in {"blocked", "failed"}:
        return False

    reasons = {str(code) for code in record.get("reason_codes") or []}
    performer_codes = {
        str(code) for code in terminal_performer_rejection_codes
    }
    performer_rejections = reasons & performer_codes
    if not performer_rejections:
        return False

    # A nonzero selector exit cannot authorize a semantic rejection.  Missing
    # rc is accepted only for historical performer-rejection rows whose old
    # schema predated the field.
    rc = record.get("rc")
    if rc is not None and rc != 0:
        return False

    active_transient = song_infra_transient_is_active(
        record=record,
        reason_codes=reasons,
        infra_transient_reason_codes=infra_transient_reason_codes,
        retry_cap=song_infra_retry_cap,
    )
    retries = record.get('transient_retry_count')
    exhausted_performer_veto = (bool(performer_rejections) and song_infra_retry_cap is not None
        and type(retries) is int and retries >= song_infra_retry_cap)
    if active_transient and not exhausted_performer_veto:
        return False

    decisive_reasons = sorted(performer_rejections)
    record["status"] = "candidate_rejected"
    record["decision"] = "REJECT"
    record["song_terminal_disposition"] = {
        "schema_version": SONG_TERMINAL_DISPOSITION_SCHEMA_VERSION,
        "status": "TERMINAL",
        "disposition": "DETERMINISTIC_CONTENT_REJECTION",
        "reason_codes": decisive_reasons,
        "retryable": False,
        "revival_authority": "EXPLICIT_OPERATOR_REVIVAL_REQUIRED",
    }
    for key in (
        "transient_failure_code",
        "retry_after_seconds",
        "next_retry_at_epoch",
        "next_retry_at",
    ):
        record.pop(key, None)
    return True


def project_terminal_batch_state(
    state: dict,
    *,
    delivered_talk_statuses: Collection[object],
    talk_failure_statuses: Collection[object],
    cover_pending_status: object,
    exact_closure: Mapping[str, object],
    retry_epoch: int | None,
    terminal_song_performer_rejection_codes: Collection[object] = (),
    song_infra_transient_reason_codes: Collection[object] = (),
    song_infra_retry_cap: int | None = None,
    mutate_songs: bool = True,
) -> dict[str, object]:
    picks = [row for row in state.get("picks", []) if isinstance(row, dict)]
    songs = [row for row in state.get("songs", []) if isinstance(row, dict)]
    if mutate_songs:
        for row in songs:
            project_terminal_song_disposition(
                row,
                terminal_performer_rejection_codes=(
                    terminal_song_performer_rejection_codes
                ),
                infra_transient_reason_codes=song_infra_transient_reason_codes,
                song_infra_retry_cap=song_infra_retry_cap,
            )
    delivered_talk = [
        row
        for row in picks
        if row.get("status") in delivered_talk_statuses
        or publication_row_is_verified(row)
    ]
    delivered_songs = [
        row
        for row in songs
        if row.get("delivered") or publication_row_is_verified(row)
    ]
    repaired = [row for row in delivered_talk if row.get("boundary_repairs")]
    blocked_songs = [row for row in songs if row.get("status") == "blocked"]
    rejected_songs = [
        row for row in songs if row.get("status") == "candidate_rejected"
    ]
    failures = [
        row
        for row in picks + songs
        if (
            row.get("status") in talk_failure_statuses
            or row.get("status") in {"candidate_rejected", cover_pending_status}
        )
        # 狍哥案修复：有界重评分车道的 status="failed" 行计入
        # retry_wait（复用既有 next_retry_at_epoch/scheduled_talk_retry_epoch
        # 机制），不算 failure、不冒充 review_ready_with_failures。真终态失败
        # 走 status=candidate_rejected，仍照常计入。
        and not (
            row.get("status") == "failed"
            and row.get("failure_kind") == "selection_rescore"
        )
    ]

    exact_status = str(exact_closure.get("status") or "")
    if exact_status == "NOT_APPLICABLE":
        state.pop("exact_talk_contract_closure", None)
    else:
        state["exact_talk_contract_closure"] = dict(exact_closure)
    if retry_epoch is None:
        state.pop("next_retry_at_epoch", None)
        state.pop("next_retry_at", None)
    else:
        state["next_retry_at_epoch"] = retry_epoch
        state["next_retry_at"] = time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime(retry_epoch)
        )

    if exact_status == "INCOMPLETE":
        status = "recovery_incomplete"
    elif exact_status == "COMPLETE":
        status = "review_ready"
    elif retry_epoch is not None:
        status = (
            "review_ready_retry_wait"
            if delivered_talk or delivered_songs
            else "retry_wait"
        )
    elif delivered_talk or delivered_songs:
        status = "review_ready_with_failures" if failures else "review_ready"
    else:
        status = "no_delivery"
    publication_closure = project_publication_closure(state)
    if publication_closure["status"] == "NOT_APPLICABLE":
        state.pop("publication_closure", None)
    else:
        state["publication_closure"] = publication_closure
        status = str(publication_closure["status"])
    state["status"] = status
    return {
        "picks": picks,
        "songs": songs,
        "delivered_talk": delivered_talk,
        "delivered_songs": delivered_songs,
        "repaired": repaired,
        "blocked_songs": blocked_songs,
        "rejected_songs": rejected_songs,
        "failures": failures,
        "exact_closure": dict(exact_closure),
        "retry_epoch": retry_epoch,
        "publication_closure": publication_closure,
    }
