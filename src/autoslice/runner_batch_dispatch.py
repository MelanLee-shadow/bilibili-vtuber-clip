"""Small runtime adapter for the producer dispatcher dependency bundle."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from src.autoslice.produce_dispatch import produce_batch_windowed


def produce_batch(
    date: str,
    items: list[dict],
    produce_fn: Callable[..., dict],
    runtime: Mapping[str, Any],
    *,
    prepare_only: bool = False,
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
        log=runtime["log"],
        talk_pipeline_fingerprint=runtime["talk_pipeline_fingerprint"],
        pipeline_fingerprint=runtime["pipeline_fingerprint"],
        song_pipeline_fingerprint=runtime["song_pipeline_fingerprint"],
        song_window_pre_ms=runtime["SONG_WINDOW_PRE_MS"],
        song_window_post_ms=runtime["SONG_WINDOW_POST_MS"],
        live_hold_active_fn=runtime["live_hold_recheck"],
        prepare_only=prepare_only,
    )
