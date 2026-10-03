"""Resolve current final-media duration without trusting stale intro witnesses."""
from __future__ import annotations

from typing import Mapping


class FinalMediaDurationBindingError(ValueError):
    """No valid current or compatibility final-media duration exists."""


def _positive_duration_ms(value: object) -> int | None:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value <= 0
    ):
        return None
    return value


def final_burn_duration_binding(
    burned_preview: object,
) -> dict[str, object]:
    """Return current EOS and disclose any stale branding-intro witness.

    ``burned_preview.verification`` is the current final-media probe. Older
    records stored a whole-media duration under
    ``branding_intro.verification``; retain that as a compatibility fallback
    only, and never let a stale nested value override a present current probe.
    """

    if not isinstance(burned_preview, Mapping):
        raise FinalMediaDurationBindingError("burned preview is missing")
    current_verification = burned_preview.get("verification")
    current_duration = _positive_duration_ms(
        current_verification.get("duration_ms")
        if isinstance(current_verification, Mapping)
        else None
    )
    branding = burned_preview.get("branding_intro")
    legacy_verification = (
        branding.get("verification")
        if isinstance(branding, Mapping)
        else None
    )
    legacy_duration = _positive_duration_ms(
        legacy_verification.get("duration_ms")
        if isinstance(legacy_verification, Mapping)
        else None
    )
    if current_duration is not None:
        selected = current_duration
        source = "burned_preview.verification.duration_ms"
    elif legacy_duration is not None:
        selected = legacy_duration
        source = (
            "burned_preview.branding_intro.verification.duration_ms:"
            "legacy_fallback"
        )
    else:
        raise FinalMediaDurationBindingError(
            "no positive final-media duration witness exists"
        )
    mismatch_ms = (
        legacy_duration - current_duration
        if current_duration is not None and legacy_duration is not None
        else None
    )
    return {
        "final_duration_ms": selected,
        "final_duration_source": source,
        "current_final_duration_ms": current_duration,
        "legacy_branding_intro_duration_ms": legacy_duration,
        "duration_witness_mismatch_ms": mismatch_ms,
        "duration_witness_mismatch": bool(
            mismatch_ms is not None and abs(mismatch_ms) > 500
        ),
    }


__all__ = [
    "FinalMediaDurationBindingError",
    "final_burn_duration_binding",
]
