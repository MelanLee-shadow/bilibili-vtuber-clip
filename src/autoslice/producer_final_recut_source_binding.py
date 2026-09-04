"""Resolve the source interval for one producer final recut."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from src.autoslice.redelivery_source_binding import (
    RedeliverySourceBindingError,
    V2RedeliverySourceBinding,
    final_recut_absolute_source_interval,
    resolve_v2_redelivery_source_binding,
)


def resolve_final_recut_source(
    *,
    spec: Mapping[str, object],
    piece_provenance_rows: Sequence[Mapping[str, object]],
    final_start: int,
    final_end: int,
) -> tuple[V2RedeliverySourceBinding | None, int | None, int | None]:
    """Bind the producer recut to its source piece before any media write."""

    try:
        binding = resolve_v2_redelivery_source_binding(
            spec=spec,
            piece_provenance_rows=piece_provenance_rows,
            final_start=final_start,
            final_end=final_end,
        )
    except RedeliverySourceBindingError as exc:
        raise SystemExit(str(exc)) from exc
    absolute_start, absolute_end = final_recut_absolute_source_interval(
        spec,
        final_start=final_start,
        final_end=final_end,
        v2_binding=binding,
    )
    return binding, absolute_start, absolute_end
