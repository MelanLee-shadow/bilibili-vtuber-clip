"""Terminal state projection for ordinary and exact recovery batches."""

from __future__ import annotations

import time
from collections.abc import Collection
from typing import Mapping

from src.autoslice.publication_reconciliation import (
    project_publication_closure,
    publication_row_is_verified,
)


SONG_TERMINAL_DISPOSITION_SCHEMA_VERSION = "song-terminal-disposition.v1"
SONG_DETERMINISTIC_PROOF_REJECTION_CODES = frozenset(
    {
        "SONG_AUDIO_LRC_IDENTITY_AMBIGUOUS",
        "SONG_AUDIO_LRC_ALIGNMENT_INVALID",
    }
)


def song_infra_transient_is_active(
    *,
    record: Mapping[str, object],
    reason_codes: Collection[object],
    infra_transient_reason_codes: Collection[object],
    retry_cap: int | None = None,
    fallback_to_reason_codes: bool = False,
) -> bool:
    """Whether this song attempt still counts as a waitable provider transient.

    An explicitly typed transient emitted by ``song_lane`` normally outranks
    stale/partial negative reasons (2026-08-08 ``4af4a88``).  That veto was
    unbounded, and combined with the song lane's JINGTING provenance false
    positives it produced an immortal candidate: ``refill_songs`` gives
    ``selected_repair`` items first claim on ``song_delivery_budget`` (one per
    live session), so 2026-08-08's ``song_200130_1012`` sat at
    ``transient_retry_count=4`` holding the only slot while the other eight
    candidates of that session never got a single attempt.

    Past ``retry_cap`` the veto lapses, but **only** against a coexisting
    deterministic content rejection.  A record carrying nothing but a genuine
    infrastructure code keeps waiting however high the counter climbs — a
    provider outage never becomes a content failure by sitting in the queue
    (``test_rate_limited_song_retry_cap_is_terminal_for_same_pipeline``).
    """

    infra_codes = {str(code) for code in infra_transient_reason_codes}
    reasons = {str(code) for code in reason_codes or ()}
    explicit = str(record.get("transient_failure_code") or "")
    if not explicit:
        # Pre-``4af4a88`` records have no typed field.  Only the requeue caller
        # opts into the reason-code fallback; terminal projection deliberately
        # honours the typed field alone, so an untyped record with a confirmed
        # non-host performance still terminalizes exactly as it did before.
        return fallback_to_reason_codes and bool(reasons & infra_codes) and not (
            reasons & SONG_DETERMINISTIC_PROOF_REJECTION_CODES
        )
    if explicit not in infra_codes:
        return False
    if retry_cap is None:
        return True
    retries = record.get("transient_retry_count")
    exhausted = not isinstance(retries, bool) and int(retries or 0) >= int(retry_cap)
    return not (exhausted and reasons & SONG_DETERMINISTIC_PROOF_REJECTION_CODES)


def project_terminal_song_disposition(
    record: dict,
    *,
    terminal_performer_rejection_codes: Collection[object] = (),
    infra_transient_reason_codes: Collection[object] = (),
    song_infra_retry_cap: int | None = None,
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

    if song_infra_transient_is_active(
        record=record,
        reason_codes=reasons,
        infra_transient_reason_codes=infra_transient_reason_codes,
        retry_cap=song_infra_retry_cap,
    ):
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
    song_infra_retry_cap: int | None = None,
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
        # 狍哥案修复（2026-08-07）：有界重评分车道的 status="failed" 行计入
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
