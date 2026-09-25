"""Dependency-injected adapter for fresh fast-media derivation."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path


def derive_fresh_fast_media(
    *,
    host: str,
    media_path: Path,
    claimed_segment_path: str,
    expected_segment_sha256: str,
    final_source_start_ms: int,
    final_source_end_ms: int,
    implementation: Callable[..., dict[str, object]],
    adapters: tuple[Callable[..., object], Callable[..., object], Callable[..., object]],
) -> dict[str, object]:
    """Bind the generic media primitive to this producer's patchable adapters."""

    accurate_command_builder, run_command, duration_probe = adapters
    return implementation(
        host=host,
        media_path=media_path,
        claimed_segment_path=claimed_segment_path,
        expected_segment_sha256=expected_segment_sha256,
        final_source_start_ms=final_source_start_ms,
        final_source_end_ms=final_source_end_ms,
        accurate_command_builder=accurate_command_builder,
        run_command=run_command,
        duration_probe=duration_probe,
    )


__all__ = ["derive_fresh_fast_media"]
