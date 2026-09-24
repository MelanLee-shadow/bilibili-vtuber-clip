"""Deterministic summary fields for final authority-surface verification."""

from __future__ import annotations

from collections.abc import Mapping, Sequence


def record_final_authority_summary(
    audit: dict,
    *,
    required_rows: Sequence[Mapping[str, object]],
    decision_row_count: int,
    source_owner_count: int,
    baseline_owner_count: int,
    superseded_by_truth: int,
    superseded_by_redelivery: int,
    superseded_by_exact_final_cpa: int,
    superseded_by_expected_value_canon: int,
    projected_by_parallel_subtitle: int,
    superseded_by_parallel_subtitle: int,
    superseded_by_correction_pass: int = 0,
) -> bool:
    """Record final counters and return whether every required owner survived."""

    required_count = len(required_rows)
    audit["final_required_legacy_decision_count"] = (
        required_count + projected_by_parallel_subtitle
    )
    audit["final_required_source_truth_owner_count"] = source_owner_count
    audit["final_required_redelivery_baseline_owner_count"] = baseline_owner_count
    audit["final_required_decision_count"] = (
        required_count
        + projected_by_parallel_subtitle
        + source_owner_count
        + baseline_owner_count
    )
    audit["final_superseded_by_correction_pass_count"] = superseded_by_correction_pass
    audit["final_superseded_by_source_truth_count"] = superseded_by_truth
    audit["final_superseded_by_redelivery_baseline_count"] = superseded_by_redelivery
    audit["final_superseded_by_exact_final_cpa_count"] = (
        superseded_by_exact_final_cpa
    )
    audit["final_superseded_by_expected_value_canon_count"] = (
        superseded_by_expected_value_canon
    )
    audit["final_projected_by_parallel_subtitle_count"] = (
        projected_by_parallel_subtitle
    )
    audit["final_superseded_by_parallel_subtitle_count"] = (
        superseded_by_parallel_subtitle
    )
    audit["final_outside_delivery_count"] = (
        decision_row_count
        - required_count
        - projected_by_parallel_subtitle
        - superseded_by_truth
        - superseded_by_redelivery
        - superseded_by_exact_final_cpa
        - superseded_by_correction_pass
        - superseded_by_expected_value_canon
        - superseded_by_parallel_subtitle
    )
    audit["final_boundary_required_exclusion_count"] = sum(
        row.get("final_verification_scope")
        == "BOUNDARY_REQUIRED_OWNER_EXCLUDED"
        for row in required_rows
    )
    return all(
        row.get("survived_final_text_srt")
        and row.get("survived_final_speaker_srt")
        for row in required_rows
    )
