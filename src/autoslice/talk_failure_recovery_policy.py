"""Failure-scoped runtime ownership for selected talk recovery."""

from __future__ import annotations

from pathlib import Path


def subtitle_authority_recovery_relatives(
    subtitle_truth_ledger: Path,
) -> tuple[str | Path, ...]:
    """Return only code/assets capable of repairing subtitle authority."""

    return (
        "scripts/produce_slice_package.py",
        "src/autoslice/delivery_recovery.py",
        "src/autoslice/talk_failure_recovery_policy.py",
        "src/autoslice/producer_text_pipeline.py",
        "src/autoslice/producer_text_finalization.py",
        "src/autoslice/expected_value_canon_supersession.py",
        "src/autoslice/producer_package_finalization.py",
        "src/autoslice/final_review_auditor.py",
        "src/autoslice/pronoun_consistency.py",
        "src/autoslice/final_review_contract.py",
        "src/autoslice/missing_proposal_bootstrap.py",
        "src/autoslice/producer_source_truth_authority.py",
        "src/autoslice/source_subtitle_truth.py",
        "src/autoslice/chat_proposals.py",
        "src/autoslice/foreign_closed_set_rebuild.py",
        "src/autoslice/foreign_audio_witness_cache.py",
        "src/autoslice/foreign_span_witness.py",
        "src/autoslice/acoustic_witness_adjudication.py",
        "src/autoslice/talk_lane.py",
        "src/autoslice/glossary_expected_value.py",
        subtitle_truth_ledger,
    )
