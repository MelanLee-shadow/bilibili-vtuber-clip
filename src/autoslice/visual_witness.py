"""Provider routing for visual evidence: CPA primary, AGY fallback only."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path


Probe = Callable[..., dict[str, object]]


def _route(
    *,
    cpa_call: Callable[[], dict[str, object]],
    agy_call: Callable[[], dict[str, object]],
    answer_validator: Callable[[str], bool] | None = None,
) -> dict[str, object]:
    primary = cpa_call()
    primary_answer_usable = bool(
        primary.get("status") == "OBSERVED"
        and (
            answer_validator is None
            or answer_validator(str(primary.get("answer") or ""))
        )
    )
    if primary_answer_usable:
        primary["routing"] = {
            "preferred_provider": "cpa",
            "selected_provider": "cpa",
            "fallback_used": False,
        }
        return primary
    primary_status = primary.get("status")
    primary_reason = primary.get("reason_code")
    if primary_status == "OBSERVED":
        primary_status = "OBSERVED_UNUSABLE"
        primary_reason = "PRIMARY_ANSWER_CONTRACT_INVALID"
    fallback = agy_call()
    fallback["routing"] = {
        "preferred_provider": "cpa",
        "selected_provider": str(fallback.get("provider") or "agy"),
        "fallback_used": True,
        "primary_status": primary_status,
        "primary_reason_code": primary_reason,
        "primary_model": primary.get("model"),
        "primary_receipt": primary,
    }
    return fallback


def image_vision_probe(
    image_path: Path,
    question: str,
    *,
    api_base: str = "",
    api_key: str = "",
    timeout_seconds: float = 90.0,
    max_tokens: int = 1024,
    max_width: int = 1280,
    cpa_probe: Probe | None = None,
    agy_probe: Probe | None = None,
    answer_validator: Callable[[str], bool] | None = None,
) -> dict[str, object]:
    """Ask CPA first; use AGY only when the CPA witness is unavailable."""

    if cpa_probe is None:
        from .cpa_frame_witness import image_vision_probe as cpa_probe
    if agy_probe is None:
        from .agy_frame_witness import image_vision_probe as agy_probe
    return _route(
        cpa_call=lambda: cpa_probe(
            image_path,
            question,
            api_base=api_base,
            api_key=api_key,
            timeout_seconds=timeout_seconds,
            max_tokens=max_tokens,
            max_width=max_width,
        ),
        agy_call=lambda: agy_probe(
            image_path,
            question,
            api_base="",
            api_key="",
            timeout_seconds=max(timeout_seconds, 360.0),
            max_tokens=max_tokens,
            max_width=max_width,
        ),
        answer_validator=answer_validator,
    )


def frame_vision_probe(
    media_path: Path,
    ms: int,
    question: str,
    *,
    api_base: str = "",
    api_key: str = "",
    timeout_seconds: float = 90.0,
    max_tokens: int = 1024,
    cpa_probe: Probe | None = None,
    agy_probe: Probe | None = None,
    answer_validator: Callable[[str], bool] | None = None,
) -> dict[str, object]:
    """Ask CPA first for one video frame; retain AGY as a bounded fallback."""

    if cpa_probe is None:
        from .cpa_frame_witness import frame_vision_probe as cpa_probe
    if agy_probe is None:
        from .agy_frame_witness import frame_vision_probe as agy_probe
    return _route(
        cpa_call=lambda: cpa_probe(
            media_path,
            ms,
            question,
            api_base=api_base,
            api_key=api_key,
            timeout_seconds=timeout_seconds,
            max_tokens=max_tokens,
        ),
        agy_call=lambda: agy_probe(
            media_path,
            ms,
            question,
            api_base="",
            api_key="",
            timeout_seconds=max(timeout_seconds, 360.0),
            max_tokens=max_tokens,
        ),
        answer_validator=answer_validator,
    )
