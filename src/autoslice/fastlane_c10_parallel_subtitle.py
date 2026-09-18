"""OSS compatibility stub: private C10 parallel authority is not redistributed."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from src.autoslice.chat_authority import (
    canonicalize_hard_meme_surfaces,
    canonicalize_japanese_native_script_surfaces,
)


class C10ParallelSubtitleAuthorityError(ValueError):
    """The public snapshot cannot supply a private C10 authority."""


@dataclass(slots=True)
class ParallelFinalSurfaceContext:
    verification: dict[str, Any] | None = None
    legacy_owner_resolutions: dict[str, Any] = field(default_factory=dict)
    source_rows: dict[str, Any] = field(default_factory=dict)
    output_indexes: dict[str, list[int]] = field(default_factory=dict)
    approved_native_script_exceptions: list[dict[str, Any]] = field(default_factory=list)
    consumed_resolution_sha256s: set[str] = field(default_factory=set)


def _unavailable(*_args, **_kwargs):
    raise C10ParallelSubtitleAuthorityError("C10_PRIVATE_AUTHORITY_UNAVAILABLE")


def prepare_parallel_final_surface(
    audit: dict,
    *,
    final_text_srt: str,
    final_speaker_srt: str,
) -> ParallelFinalSurfaceContext | None:
    del final_text_srt, final_speaker_srt
    context = ParallelFinalSurfaceContext()
    if audit.get("parallel_subtitle_projection_authority") is None:
        return context
    audit["final_parallel_subtitle_projection_verification"] = {
        "status": "FAIL",
        "reason_code": "C10_PRIVATE_AUTHORITY_UNAVAILABLE",
    }
    audit["final_verification_failure"] = (
        "PARALLEL_SUBTITLE_PROJECTION_AUTHORITY_INVALID"
    )
    return None


def verify_final_script_surfaces(
    audit: dict,
    *,
    final_text_srt: str,
    final_speaker_srt: str,
    context: ParallelFinalSurfaceContext,
) -> bool:
    del context
    surfaces = (
        ("final_text_srt", final_text_srt),
        ("final_speaker_srt", final_speaker_srt),
    )
    hard_meme_failures = {}
    for surface_name, srt_text in surfaces:
        _normalized, replacements = canonicalize_hard_meme_surfaces(srt_text)
        if replacements:
            hard_meme_failures[surface_name] = replacements
    audit["final_hard_meme_surface_verification"] = {
        "status": "FAIL" if hard_meme_failures else "PASS",
        "failures": hard_meme_failures,
    }
    if hard_meme_failures:
        audit["final_verification_failure"] = (
            "UNBYPASSABLE_HARD_MEME_SURFACE_PRESENT"
        )
        return False

    failures_by_surface = {}
    for surface_name, srt_text in surfaces:
        _normalized, replacements = canonicalize_japanese_native_script_surfaces(
            srt_text
        )
        if replacements:
            failures_by_surface[surface_name] = replacements
    audit["final_japanese_native_script_verification"] = {
        "status": "FAIL" if failures_by_surface else "PASS",
        "failures": failures_by_surface,
        "authorized_exceptions": {},
    }
    if failures_by_surface:
        audit["final_verification_failure"] = "JAPANESE_ROMAJI_SURFACE_PRESENT"
        return False
    return True


def consume_parallel_legacy_owner(*_args, **_kwargs):
    return None


def finalize_parallel_legacy_owner_consumption(
    _audit: dict,
    context: ParallelFinalSurfaceContext,
) -> bool:
    return not context.legacy_owner_resolutions


render_parallel_subtitle_srt = _unavailable
build_operator_entity_successor = _unavailable
validate_parallel_subtitle_authority = _unavailable
load_parallel_subtitle_authority = _unavailable
