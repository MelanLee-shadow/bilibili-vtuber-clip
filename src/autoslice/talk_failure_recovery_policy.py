"""Failure-scoped runtime ownership for selected talk recovery."""

from __future__ import annotations

from pathlib import Path


def subtitle_authority_recovery_relatives(
    subtitle_truth_ledger: Path,
) -> tuple[str | Path, ...]:
    """Return only code/assets capable of repairing subtitle authority."""

    return (
        "scripts/produce_slice_package.py",
        "src/autoslice/producer_text_pipeline.py",
        "src/autoslice/producer_text_finalization.py",
        "src/autoslice/final_review_contract.py",
        "src/autoslice/producer_source_truth_authority.py",
        "src/autoslice/source_subtitle_truth.py",
        "src/autoslice/chat_proposals.py",
        "src/autoslice/glossary_expected_value.py",
        subtitle_truth_ledger,
    )
