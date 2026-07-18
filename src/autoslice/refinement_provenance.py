"""Provenance gates for second-listen subtitle refinements."""

from __future__ import annotations

import hashlib

from src.autoslice.source_context_executor import AgyExecutionResult


def agy_refinement_provenance(
    result: AgyExecutionResult | None,
    *,
    refined_srt: str | None = None,
) -> dict[str, object]:
    eligible = bool(
        refined_srt
        and result is not None
        and result.provider == "agy"
        and result.agy_rc == 0
        and result.provider_fallback_used is False
    )
    return {
        "schema_version": "agy-refinement-provenance.v1",
        "provider": result.provider if result is not None else None,
        "model": result.model if result is not None else None,
        "agy_rc": result.agy_rc if result is not None else None,
        "provider_fallback_used": (
            result.provider_fallback_used if result is not None else None
        ),
        "provider_request_id": (
            result.provider_request_id if result is not None else None
        ),
        "refined_srt_sha256": (
            hashlib.sha256(refined_srt.encode("utf-8")).hexdigest()
            if refined_srt
            else None
        ),
        "fidelity_witness_eligible": eligible,
    }


def agy_fidelity_witness(
    refined_srt: str | None,
    result: AgyExecutionResult | None,
) -> str | None:
    """Return only a direct AGY result as an independent fidelity witness."""

    provenance = agy_refinement_provenance(result, refined_srt=refined_srt)
    return refined_srt if provenance["fidelity_witness_eligible"] else None
