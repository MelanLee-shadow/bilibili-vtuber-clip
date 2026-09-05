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
        "src/autoslice/final_review_carryover_retry.py",
        "src/autoslice/final_review_failure_compaction.py",
        "src/autoslice/final_review_provider_budget_retry.py",
        "src/autoslice/final_review_provider_budget.py",
        "src/autoslice/final_review_carryover.py",
        "src/autoslice/talk_delivery_recovery.py",
        "src/autoslice/talk_failure_recovery_policy.py",
        "src/autoslice/talk_recovery_record_policy.py",
        "src/autoslice/producer_text_pipeline.py",
        "src/autoslice/producer_text_finalization.py",
        "src/autoslice/expected_value_canon_supersession.py",
        "src/autoslice/producer_package_finalization.py",
        "src/autoslice/final_review_auditor.py",
        "src/autoslice/closed_set_proposal_rebuild.py",
        "src/autoslice/exact_source_transcript_contract.py",
        "src/autoslice/exact_source_transcript_provider.py",
        "src/autoslice/exact_source_transcript_provider_policy.py",
        "src/autoslice/exact_source_transcript_runtime.py",
        "src/autoslice/exact_source_transcript_authority.py",
        "src/autoslice/entity_audio_verifier.py",
        "src/autoslice/entity_audio_gemini_web.py",
        "src/autoslice/acoustic_witness_protocol.py",
        "scripts/gemini_web_subscription.py",
        "src/autoslice/agy_gemini_client.py",
        "src/autoslice/gemini_backup_policy.py",
        "src/autoslice/llm_client.py",
        "scripts/gemini_slice_jingting.py",
        "src/autoslice/read_aloud_llm_verifier.py",
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
