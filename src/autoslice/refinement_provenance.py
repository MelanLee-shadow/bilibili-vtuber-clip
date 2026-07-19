"""Provenance gates for second-listen subtitle refinements."""

from __future__ import annotations

import hashlib
from pathlib import Path

from src.autoslice.source_context_executor import AgyExecutionResult


def agy_refinement_provenance(
    result: AgyExecutionResult | None,
    *,
    refined_srt: str | None = None,
    draft_srt: str | None = None,
    media_path: Path | None = None,
) -> dict[str, object]:
    refined_sha256 = (
        hashlib.sha256(refined_srt.encode("utf-8")).hexdigest()
        if refined_srt
        else None
    )
    draft_sha256 = (
        hashlib.sha256(draft_srt.encode("utf-8")).hexdigest()
        if draft_srt
        else None
    )
    media_sha256 = (
        _sha256_file(media_path)
        if media_path is not None and media_path.is_file()
        else None
    )
    attestation_bound = bool(
        refined_srt
        and draft_srt
        and media_sha256
        and result is not None
        and result.source_media_sha256 == media_sha256
        and result.draft_srt_sha256 == draft_sha256
        and result.refined_srt_sha256 == refined_sha256
        and result.timing_validated is True
        and result.audio_input_attested is True
        and isinstance(result.chunk_count, int)
        and result.chunk_count > 0
        and isinstance(result.agy_chunk_count, int)
        and isinstance(result.api_fallback_chunk_count, int)
        and len(result.chunk_attestations) == result.chunk_count
        and result.agy_chunk_count + result.api_fallback_chunk_count
        == result.chunk_count
        and all(
            row.timing_validated
            and row.audio_input_attested
            and len(row.media_sha256) == 64
            and len(row.draft_srt_sha256) == 64
            and len(row.refined_srt_sha256) == 64
            and row.executed_provider in {"agy", "gemini_api"}
            for row in result.chunk_attestations
        )
        and {row.chunk_index for row in result.chunk_attestations}
        == set(range(result.chunk_count))
        and sum(
            row.executed_provider == "agy"
            for row in result.chunk_attestations
        )
        == result.agy_chunk_count
        and sum(
            row.executed_provider == "gemini_api"
            for row in result.chunk_attestations
        )
        == result.api_fallback_chunk_count
    )
    independent_eligible = bool(
        attestation_bound
        and result is not None
        and result.provider == "agy"
        and result.agy_rc == 0
        and result.provider_fallback_used is False
        and result.executed_provider == "agy"
        and result.agy_chunk_count == result.chunk_count
        and result.api_fallback_chunk_count == 0
    )
    corroborating_eligible = bool(
        attestation_bound
        and result is not None
        and result.provider == "agy"
        and result.agy_rc == 0
        and result.provider_fallback_used is True
        and result.executed_provider in {"gemini_api", "agy+gemini_api"}
        and isinstance(result.api_fallback_chunk_count, int)
        and result.api_fallback_chunk_count > 0
    )
    return {
        "schema_version": "agy-refinement-provenance.v2",
        "provider": result.provider if result is not None else None,
        "model": result.model if result is not None else None,
        "agy_rc": result.agy_rc if result is not None else None,
        "provider_fallback_used": (
            result.provider_fallback_used if result is not None else None
        ),
        "provider_request_id": (
            result.provider_request_id if result is not None else None
        ),
        "requested_provider": (
            result.requested_provider if result is not None else None
        ),
        "executed_provider": (
            result.executed_provider if result is not None else None
        ),
        "source_media_sha256": media_sha256,
        "draft_srt_sha256": draft_sha256,
        "refined_srt_sha256": refined_sha256,
        "declared_source_media_sha256": (
            result.source_media_sha256 if result is not None else None
        ),
        "declared_draft_srt_sha256": (
            result.draft_srt_sha256 if result is not None else None
        ),
        "declared_refined_srt_sha256": (
            result.refined_srt_sha256 if result is not None else None
        ),
        "timing_validated": (
            result.timing_validated if result is not None else None
        ),
        "audio_input_attested": (
            result.audio_input_attested if result is not None else None
        ),
        "chunk_count": result.chunk_count if result is not None else None,
        "agy_chunk_count": (
            result.agy_chunk_count if result is not None else None
        ),
        "api_fallback_chunk_count": (
            result.api_fallback_chunk_count if result is not None else None
        ),
        "chunk_attestations": (
            [
                {
                    "chunk_index": row.chunk_index,
                    "media_start_ms": row.media_start_ms,
                    "media_end_ms": row.media_end_ms,
                    "media_sha256": row.media_sha256,
                    "draft_srt_sha256": row.draft_srt_sha256,
                    "refined_srt_sha256": row.refined_srt_sha256,
                    "executed_provider": row.executed_provider,
                    "timing_validated": row.timing_validated,
                    "audio_input_attested": row.audio_input_attested,
                }
                for row in result.chunk_attestations
            ]
            if result is not None
            else []
        ),
        "attestation_bound": attestation_bound,
        "witness_tier": (
            "independent_audio"
            if independent_eligible
            else ("context_bound_audio" if corroborating_eligible else "none")
        ),
        "fidelity_witness_eligible": independent_eligible,
        "corroborating_audio_eligible": corroborating_eligible,
    }


def agy_fidelity_witness(
    refined_srt: str | None,
    result: AgyExecutionResult | None,
    *,
    draft_srt: str | None = None,
    media_path: Path | None = None,
) -> str | None:
    """Return only a direct AGY result as an independent fidelity witness."""

    provenance = agy_refinement_provenance(
        result,
        refined_srt=refined_srt,
        draft_srt=draft_srt,
        media_path=media_path,
    )
    return refined_srt if provenance["fidelity_witness_eligible"] else None


def agy_corroborating_witness(
    refined_srt: str | None,
    result: AgyExecutionResult | None,
    *,
    draft_srt: str,
    media_path: Path,
) -> str | None:
    """Return a hash-bound fallback listen only as span-level corroboration."""

    provenance = agy_refinement_provenance(
        result,
        refined_srt=refined_srt,
        draft_srt=draft_srt,
        media_path=media_path,
    )
    return refined_srt if provenance["corroborating_audio_eligible"] else None


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
