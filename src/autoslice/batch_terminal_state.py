"""Terminal state projection for ordinary and exact recovery batches."""

from __future__ import annotations

import time
from collections.abc import Collection
from typing import Mapping


SONG_TERMINAL_DISPOSITION_SCHEMA_VERSION = "song-terminal-disposition.v1"
SONG_DETERMINISTIC_PROOF_REJECTION_CODES = frozenset(
    {
        "SONG_AUDIO_LRC_IDENTITY_AMBIGUOUS",
        "SONG_AUDIO_LRC_ALIGNMENT_INVALID",
    }
)


def project_terminal_song_disposition(
    record: dict,
    *,
    terminal_performer_rejection_codes: Collection[object] = (),
    infra_transient_reason_codes: Collection[object] = (),
) -> bool:
    """Project a completed negative song proof into a durable terminal receipt.

    A pipeline fingerprint change is not new acoustic evidence.  Completed
    audio/LRC negatives and confirmed non-host performances therefore remain
    rejected until an operator explicitly revives the candidate.  An explicit
    typed provider transient always wins over stale/partial negative reasons.
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
    proof_rejections = reasons & SONG_DETERMINISTIC_PROOF_REJECTION_CODES
    performer_rejections = reasons & performer_codes
    if not proof_rejections and not performer_rejections:
        return False

    # A nonzero selector exit cannot authorize a semantic rejection.  Missing
    # rc is accepted only for historical performer-rejection rows whose old
    # schema predated the field.
    rc = record.get("rc")
    if rc is not None and rc != 0:
        return False
    if proof_rejections and rc != 0:
        return False

    explicit_transient = str(record.get("transient_failure_code") or "")
    if explicit_transient and explicit_transient in {
        str(code) for code in infra_transient_reason_codes
    }:
        return False

    decisive_reasons = sorted(proof_rejections | performer_rejections)
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
) -> dict[str, object]:
    picks = [row for row in state.get("picks", []) if isinstance(row, dict)]
    songs = [row for row in state.get("songs", []) if isinstance(row, dict)]
    for row in songs:
        project_terminal_song_disposition(
            row,
            terminal_performer_rejection_codes=(
                terminal_song_performer_rejection_codes
            ),
            infra_transient_reason_codes=song_infra_transient_reason_codes,
        )
    delivered_talk = [
        row for row in picks if row.get("status") in delivered_talk_statuses
    ]
    delivered_songs = [row for row in songs if row.get("delivered")]
    repaired = [row for row in delivered_talk if row.get("boundary_repairs")]
    blocked_songs = [row for row in songs if row.get("status") == "blocked"]
    rejected_songs = [
        row for row in songs if row.get("status") == "candidate_rejected"
    ]
    failures = [
        row
        for row in picks + songs
        if row.get("status") in talk_failure_statuses
        or row.get("status") in {"candidate_rejected", cover_pending_status}
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
    }
