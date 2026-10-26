"""Small runtime adapter for the producer dispatcher dependency bundle."""

from __future__ import annotations

from collections.abc import Callable, Mapping
import os
import re
from typing import Any

from src.autoslice.produce_dispatch import produce_batch_windowed
from src.autoslice.runtime_storage import (
    DEFAULT_OPERATIONAL_RESERVE_BYTES,
    DEFAULT_PEAK_BYTES_PER_SECOND,
    OPERATIONAL_RESERVE_ENV,
    PEAK_BYTES_PER_SECOND_ENV,
    parse_operational_reserve_bytes,
    parse_peak_bytes_per_second,
)


MIN_FREE_BYTES_ENV = "AUTOSLICE_MIN_FREE_BYTES"
MIN_FREE_BYTES_DEFAULT = 0


def _parse_min_free_bytes(raw: str | None = None) -> int:
    """Parse the free-space pressure threshold before provider work.

    The name remains for compatibility with existing runner imports.  This is
    now a housekeeping trigger; per-item admission uses the smaller explicit
    operational reserve below.
    """

    value = os.environ.get(MIN_FREE_BYTES_ENV) if raw is None else str(raw)
    if value is None:
        return MIN_FREE_BYTES_DEFAULT
    if re.fullmatch(r"[0-9]+", value) is None:
        raise ValueError(f"{MIN_FREE_BYTES_ENV} must be a non-negative integer")
    return int(value)


def _parse_operational_reserve_bytes(raw: str | None = None) -> int:
    """Parse the post-admission runtime reserve (2 GiB by default)."""

    return parse_operational_reserve_bytes(
        os.environ.get(OPERATIONAL_RESERVE_ENV) if raw is None else raw
    )


def _parse_peak_bytes_per_second(raw: str | None = None) -> int:
    """Parse the conservative peak allocation rate (16 MiB/s by default)."""

    return parse_peak_bytes_per_second(
        os.environ.get(PEAK_BYTES_PER_SECOND_ENV) if raw is None else raw
    )


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

    from src.autoslice.execution_backend import backend_from_environment
    backend = runtime.get("EXECUTION_BACKEND")
    if backend is None:
        backend = backend_from_environment(runtime["BASE"], log=runtime["log"])

    return produce_batch_windowed(
        date,
        items,
        produce_fn,
        produce_talk_fn=runtime["produce_talk"],
        produce_song_fn=runtime["produce_song"],
        base=runtime["BASE"],
        max_parallel=runtime["MAX_PARALLEL_PRODUCE"],
        execution_backend=backend,
        min_free_bytes=runtime.get("MIN_FREE_BYTES", 0),
        operational_reserve_bytes=runtime.get(
            "OPERATIONAL_RESERVE_BYTES", DEFAULT_OPERATIONAL_RESERVE_BYTES
        ),
        peak_bytes_per_second=runtime.get(
            "CAPACITY_PEAK_BYTES_PER_SECOND", DEFAULT_PEAK_BYTES_PER_SECOND
        ),
        log=runtime["log"],
        talk_pipeline_fingerprint=runtime["talk_pipeline_fingerprint"],
        pipeline_fingerprint=runtime["pipeline_fingerprint"],
        song_pipeline_fingerprint=runtime["song_pipeline_fingerprint"],
        song_window_pre_ms=runtime["SONG_WINDOW_PRE_MS"],
        song_window_post_ms=runtime["SONG_WINDOW_POST_MS"],
        talk_window_pre_ms=runtime.get("PIECE_PRE_MS", 0),
        talk_window_post_ms=runtime.get("PIECE_POST_MS", 0),
        live_hold_active_fn=runtime["live_hold_recheck"],
        stop_requested_fn=runtime.get("producer_stop_requested"),
        prepared_result_dependency_fingerprint=runtime.get("prepared_result_dependency_fingerprint"),
        capture_talk_dependency_snapshot=runtime.get("capture_talk_dependency_snapshot"),
        capture_song_dependency_fingerprint=runtime.get("capture_song_dependency_fingerprint"),
        prepare_only=prepare_only,
        on_result=on_result,
    )
