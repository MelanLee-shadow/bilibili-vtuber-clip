"""Small runtime adapter for the producer dispatcher dependency bundle."""

from __future__ import annotations

from collections.abc import Callable, Mapping
import os
import re
from typing import Any

from src.autoslice.produce_dispatch import produce_batch_windowed


MIN_FREE_BYTES_ENV = "AUTOSLICE_MIN_FREE_BYTES"
MIN_FREE_BYTES_DEFAULT = 0


def _parse_min_free_bytes(raw: str | None = None) -> int:
    """Parse the optional free-space floor before provider work."""

    value = os.environ.get(MIN_FREE_BYTES_ENV) if raw is None else str(raw)
    if value is None:
        return MIN_FREE_BYTES_DEFAULT
    if re.fullmatch(r"[0-9]+", value) is None:
        raise ValueError(f"{MIN_FREE_BYTES_ENV} must be a non-negative integer")
    return int(value)


def produce_batch(
    date: str,
    items: list[dict],
    produce_fn: Callable[..., dict],
    runtime: Mapping[str, Any],
    *,
    prepare_only: bool = False,
    on_result: Callable[[int, dict, dict], bool | None] | None = None,
) -> list[dict]:
    """Dispatch with the runner's live, monkeypatchable policy dependencies."""

    return produce_batch_windowed(
        date,
        items,
        produce_fn,
        produce_talk_fn=runtime["produce_talk"],
        produce_song_fn=runtime["produce_song"],
        base=runtime["BASE"],
        max_parallel=runtime["MAX_PARALLEL_PRODUCE"],
        min_free_bytes=runtime.get("MIN_FREE_BYTES", 0),
        log=runtime["log"],
        talk_pipeline_fingerprint=runtime["talk_pipeline_fingerprint"],
        pipeline_fingerprint=runtime["pipeline_fingerprint"],
        song_pipeline_fingerprint=runtime["song_pipeline_fingerprint"],
        song_window_pre_ms=runtime["SONG_WINDOW_PRE_MS"],
        song_window_post_ms=runtime["SONG_WINDOW_POST_MS"],
        live_hold_active_fn=runtime["live_hold_recheck"],
        prepare_only=prepare_only,
        on_result=on_result,
    )
