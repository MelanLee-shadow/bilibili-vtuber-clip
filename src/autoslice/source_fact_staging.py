"""Resolve provider or sealed candidate source-fact authority before cover work."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from src.autoslice.addressee_attribution import (
    SpeakerGuessReviewRequired,
    build_addressee_evidence,
)
from src.autoslice.deterministic_text_surface_resolution import (
    DeterministicTextSurfaceResolutionError,
    consume_deterministic_text_surface_authority,
    load_deterministic_text_surface_authority,
)
from src.autoslice.llm_client import LlmCall
from src.autoslice.manual_title_keep_authority import (
    ManualTitleKeepAuthorityError,
    blocked_source_fact_review,
    load_manual_title_keep_authority,
    validate_manual_title_keep_authority,
)
from src.autoslice.speaker_guess import SPEAKER_GUESS_STATUS
from src.autoslice.source_fact_review import (
    authorize_deterministic_text_narrowing,
    authorize_manual_title_keep,
    resolve_source_fact_entity_context,
    review_and_repair_source_facts,
)


@dataclass(frozen=True, slots=True)
class InitialSourceFactResolution:
    review: dict[str, object] | None
    violation: str | None
    authority_error: str | None
    authority_status: str
    manual_title_keep_consumption: dict[str, object] | None


def _blocked(
    *,
    violation: str,
    reason: str,
    detail: object,
) -> InitialSourceFactResolution:
    return InitialSourceFactResolution(
        review=None,
        violation=violation,
        authority_error=f"source_fact_review_failed:{reason}:{detail}",
        authority_status="BLOCKED_SOURCE_FACT_REVIEW",
        manual_title_keep_consumption=None,
    )


def resolve_initial_source_fact_review(
    *,
    candidate_id: str,
    title_source: str,
    title: str,
    selection_hook: str,
    story_contract: object,
    record: Mapping[str, object],
    cues: Sequence[object],
    source_fact_llm_call: LlmCall | None,
    recovery_publication_authority: object,
    title_authority_status: str,
    title_llm_enabled: bool,
    prior_authority_error: str | None,
) -> InitialSourceFactResolution:
    """Choose exactly one source-fact lane without provider fallback on drift."""

    if prior_authority_error is not None:
        return InitialSourceFactResolution(
            review=None,
            violation=None,
            authority_error=prior_authority_error,
            authority_status=title_authority_status,
            manual_title_keep_consumption=None,
        )
    speaker_manifest = record.get("speaker_finalization")
    if (
        isinstance(speaker_manifest, Mapping)
        and speaker_manifest.get("status") == SPEAKER_GUESS_STATUS
    ):
        try:
            build_addressee_evidence(record, cues)
        except SpeakerGuessReviewRequired:
            # The guess is fully rebound before skipping the whole source-fact
            # lane.  In particular, stale manual authorities cannot consume it
            # and no provider sees guessed speaker turns as addressee evidence.
            return InitialSourceFactResolution(
                review=None,
                violation=None,
                authority_error=None,
                authority_status=title_authority_status,
                manual_title_keep_consumption=None,
            )
    try:
        keep_authority = load_manual_title_keep_authority(candidate_id)
    except (ManualTitleKeepAuthorityError, OSError, ValueError) as exc:
        return _blocked(
            violation="manual_title_keep_authority_invalid",
            reason="MANUAL_TITLE_KEEP_AUTHORITY_INVALID",
            detail=exc,
        )
    try:
        deterministic_authority = load_deterministic_text_surface_authority(candidate_id)
    except (DeterministicTextSurfaceResolutionError, OSError, ValueError) as exc:
        return _blocked(
            violation="deterministic_text_surface_authority_invalid",
            reason="DETERMINISTIC_TEXT_SURFACE_AUTHORITY_INVALID",
            detail=exc,
        )
    if source_fact_llm_call is None and keep_authority is None and deterministic_authority is None:
        return InitialSourceFactResolution(
            review=None,
            violation=None,
            authority_error=None,
            authority_status=title_authority_status,
            manual_title_keep_consumption=None,
        )

    final_transcript, speaker_evidence = build_addressee_evidence(record, cues)
    contract = story_contract if isinstance(story_contract, Mapping) else {}
    context_prompt = str(contract.get("clip_context_prompt") or "")
    scorecard = contract.get("selection_scorecard")
    subtitle_value = record.get("subtitle_path")
    final_srt = Path(str(subtitle_value)) if subtitle_value else None
    try:
        if deterministic_authority is not None:
            if title_source != "ivan_manual_override" or final_srt is None:
                raise DeterministicTextSurfaceResolutionError(
                    "DETERMINISTIC_TEXT_NARROWING_RUNTIME_TITLE_MODE_INVALID"
                )
            entity_context = resolve_source_fact_entity_context(
                candidate_id=candidate_id,
                final_reviewed_srt_path=final_srt,
            )
            binding = entity_context.get("candidate_binding")
            if not isinstance(binding, Mapping):
                raise DeterministicTextSurfaceResolutionError(
                    "DETERMINISTIC_TEXT_NARROWING_RUNTIME_SOURCE_BINDING_INVALID"
                )
            consumption = consume_deterministic_text_surface_authority(
                deterministic_authority,
                candidate_id=candidate_id,
                source_recording_basename=str(binding.get("source_recording_basename") or ""),
                source_sha256=str(binding.get("source_sha256") or ""),
                absolute_source_start_ms=int(binding.get("absolute_source_start_ms")),
                absolute_source_end_ms=int(binding.get("absolute_source_end_ms")),
                reviewed_srt_path=final_srt,
                speaker_evidence=speaker_evidence.speaker_evidence,
                failed_source_fact_review=(deterministic_authority.failed_source_fact_receipt),
                original_title=title,
                original_selection_hook=selection_hook,
                final_transcript=final_transcript,
                clip_context_prompt=context_prompt,
                selection_scorecard=scorecard,
                entity_context=entity_context,
            )
            review = authorize_deterministic_text_narrowing(consumption)
            return InitialSourceFactResolution(
                review=review,
                violation=None,
                authority_error=None,
                authority_status=title_authority_status,
                manual_title_keep_consumption=None,
            )
        if keep_authority is not None:
            if title_source != "ivan_manual_override" or final_srt is None:
                raise ManualTitleKeepAuthorityError("MANUAL_TITLE_KEEP_RUNTIME_TITLE_MODE_INVALID")
            consumption = validate_manual_title_keep_authority(
                keep_authority,
                candidate_id=candidate_id,
                title=title,
                selection_hook=selection_hook,
                final_transcript=final_transcript,
                clip_context_prompt=context_prompt,
                selection_scorecard=scorecard,
                final_reviewed_srt_path=final_srt,
                speaker_evidence=speaker_evidence.speaker_evidence,
                entity_context=resolve_source_fact_entity_context(
                    candidate_id=candidate_id,
                    final_reviewed_srt_path=final_srt,
                ),
            )
            review = authorize_manual_title_keep(
                blocked_source_fact_review(keep_authority),
                consumption=consumption,
            )
            return InitialSourceFactResolution(
                review=review,
                violation=None,
                authority_error=None,
                authority_status=title_authority_status,
                manual_title_keep_consumption=consumption,
            )
    except DeterministicTextSurfaceResolutionError as exc:
        return _blocked(
            violation="deterministic_text_surface_authority_invalid",
            reason="DETERMINISTIC_TEXT_SURFACE_AUTHORITY_INVALID",
            detail=exc,
        )
    except ManualTitleKeepAuthorityError as exc:
        return _blocked(
            violation="manual_title_keep_authority_invalid",
            reason="MANUAL_TITLE_KEEP_AUTHORITY_INVALID",
            detail=exc,
        )
    except (OSError, TypeError, ValueError) as exc:
        deterministic = deterministic_authority is not None
        return _blocked(
            violation=(
                "deterministic_text_surface_authority_invalid"
                if deterministic
                else "manual_title_keep_authority_invalid"
            ),
            reason=(
                "DETERMINISTIC_TEXT_SURFACE_AUTHORITY_INVALID"
                if deterministic
                else "MANUAL_TITLE_KEEP_AUTHORITY_INVALID"
            ),
            detail=exc,
        )

    assert source_fact_llm_call is not None
    review = review_and_repair_source_facts(
        selection_hook=selection_hook,
        title=title,
        final_transcript=final_transcript,
        clip_context_prompt=context_prompt,
        speaker_transcript=speaker_evidence.transcript,
        speaker_evidence=speaker_evidence.speaker_evidence,
        llm_call=source_fact_llm_call,
        selection_scorecard=scorecard,
        title_repair_allowed=not (
            recovery_publication_authority is not None
            or title_authority_status == "RESOLVED_MANUAL"
            or title_authority_status == "RESOLVED_PUBLIC_TEXT_SURFACE_AUTHORITY"
            or title_source == "ivan_manual_override"
        ),
        enforce_automatic_title_style=title_llm_enabled,
        candidate_id=candidate_id,
        final_reviewed_srt_path=final_srt,
    )
    return InitialSourceFactResolution(
        review=review,
        violation=None,
        authority_error=None,
        authority_status=title_authority_status,
        manual_title_keep_consumption=None,
    )
