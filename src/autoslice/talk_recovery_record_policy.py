"""Narrow legacy/supplemental record shapes admitted to Talk recovery."""

from __future__ import annotations

from collections.abc import Mapping

from src.autoslice.foreign_source_failure_evidence import witness_row_provider_transient


def cover_route_retry_is_eligible(record: Mapping[str, object]) -> bool:
    """Match the existing bounded screenshot-route retry branch."""

    return bool(
        record.get("status") == "failed"
        and record.get("failure_recoverable") is True
        and record.get("failure_kind") == "cover_route_regeneration"
        and isinstance(record.get("cover_route_regeneration_fingerprint"), str)
        and int(record.get("cover_route_regeneration_attempts") or 0) > 0
    )


def _legacy_exact_backfill_rejection(record: Mapping[str, object]) -> bool:
    status = record.get("rejected_status")
    reason = record.get("rejection_reason")
    if status == "boundary_unrepairable":
        return reason == "unsafe_boundary_backfilled"
    if status in {"speaker_review_required", "speaker_evidence_insufficient"}:
        return reason == "speaker_identity_unresolved_backfilled"
    if status != "failed":
        return False
    return (record.get("failure_kind"), reason) in {
        ("subtitle_authority", "subtitle_authority_unresolved_backfilled"),
        ("story_contract", "story_contract_unresolved_backfilled"),
    }


def _provider_backfilled_foreign_rejection(
    record: Mapping[str, object],
) -> bool:
    if not (
        record.get("status") == "candidate_rejected"
        and record.get("rejected_status") == "failed"
        and record.get("failure_kind") == "subtitle_authority"
        and record.get("failure_stage") == "foreign_source_transcription"
        and record.get("rejection_reason")
        == "subtitle_authority_unresolved_backfilled"
    ):
        return False
    violation = record.get("gate_violation")
    if not isinstance(violation, Mapping):
        return False
    unresolved = violation.get("unresolved_findings")
    rows = violation.get("witness_rows")
    if not isinstance(unresolved, list) or not unresolved or not isinstance(rows, list):
        return False
    unresolved_indexes = {
        row.get("cue_index") for row in unresolved if isinstance(row, Mapping)
    }
    relevant = [
        row
        for row in rows
        if isinstance(row, Mapping) and row.get("cue_index") in unresolved_indexes
    ]
    return bool(relevant) and all(witness_row_provider_transient(dict(row)) for row in relevant)


def supplemental_recovery_candidate(
    record: Mapping[str, object],
    *,
    candidate_id: str,
    exact_contract_ids: set[str],
) -> bool:
    """Recognize only the bounded pre-policy and selected-authority shapes."""

    return bool(
        (
            record.get("status") == "candidate_rejected"
            and candidate_id in exact_contract_ids
            and _legacy_exact_backfill_rejection(record)
        )
        or (
            record.get("status") == "candidate_rejected"
            and record.get("selected_repair") is True
            and record.get("failure_kind") == "subtitle_authority"
            and record.get("failure_stage")
            in {
                "chat_authority_finalization",
                "chat_authority_final_artifact",
                "final_review_provider_budget",
                "foreign_source_transcription",
            }
            and record.get("rejection_reason")
            == "subtitle_authority_unresolved_backfilled"
        )
        or _provider_backfilled_foreign_rejection(record)
    )
