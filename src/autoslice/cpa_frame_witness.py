"""Deprecated fail-closed shim for the retired CPA image-witness route.

CPA is text-only. Runtime callers must use :mod:`agy_frame_witness`. These
names remain only to make stale imports fail closed instead of silently
sending an unsupported ``input_image`` request and recording false evidence.
"""

from __future__ import annotations

from pathlib import Path


SCHEMA_VERSION = "cpa-frame-witness.v1"


def image_vision_probe(
    image_path: Path,
    question: str,
    *,
    api_base: str,
    api_key: str,
    model: str = "gpt-5.6-sol",
    timeout_seconds: float = 90.0,
    max_tokens: int = 1024,
    max_width: int = 1280,
) -> dict[str, object]:
    """Refuse unsupported CPA image input without making a network request."""

    del api_base, api_key, model, timeout_seconds, max_tokens, max_width
    return {
        "schema_version": SCHEMA_VERSION,
        "image_path": str(image_path),
        "question": question,
        "provider": "cpa",
        "status": "UNAVAILABLE",
        "reason_code": "CPA_TEXT_ONLY",
        "error": "CPA does not accept image evidence; use agy_frame_witness",
    }


def frame_vision_probe(
    media_path: Path,
    ms: int,
    question: str,
    *,
    api_base: str,
    api_key: str,
    model: str = "gpt-5.6-sol",
    timeout_seconds: float = 90.0,
    max_tokens: int = 1024,
) -> dict[str, object]:
    """Refuse unsupported CPA frame input without making a network request."""

    del api_base, api_key, model, timeout_seconds, max_tokens
    return {
        "schema_version": SCHEMA_VERSION,
        "media_path": str(media_path),
        "frame_ms": int(ms),
        "question": question,
        "provider": "cpa",
        "status": "UNAVAILABLE",
        "reason_code": "CPA_TEXT_ONLY",
        "error": "CPA does not accept image evidence; use agy_frame_witness",
    }
