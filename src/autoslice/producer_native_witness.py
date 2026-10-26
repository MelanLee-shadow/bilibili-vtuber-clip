"""Producer configuration seams for opt-in native audio witnesses."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path


def _native_provider(
    spec: Mapping[str, object],
    field: str,
) -> str | None:
    provider = spec.get(field)
    return provider if provider in {"moss", "mai"} else None


def ensure_local_native_audio_budget(
    spec: Mapping[str, object],
    source_media: Path,
) -> str | None:
    """Register the explicit local budget before any optional lane is built."""

    provider = _native_provider(spec, "local_audio_witness_provider")
    if provider is None:
        return None
    from src.autoslice.supplement_audio_budget import ensure_budget

    config = spec.get("local_audio_witness_budget")
    limits = dict(config) if isinstance(config, Mapping) else {}
    ensure_budget(source_media, **limits)
    return provider


def native_foreign_script_kwargs(
    spec: Mapping[str, object],
) -> dict[str, str]:
    """Preserve the legacy no-keyword call shape when native mode is off."""

    provider = _native_provider(spec, "foreign_script_witness_provider")
    return {"native_provider": provider} if provider is not None else {}


__all__ = [
    "ensure_local_native_audio_budget",
    "native_foreign_script_kwargs",
]
