"""Bind exact redelivery intervals to their sole delivered source piece."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from src.autoslice.piece_roles import single_content_piece_index


V2_SCHEMA_VERSION = "subtitle-redelivery-baseline.v2"


class RedeliverySourceBindingError(ValueError):
    """A v2 reviewed interval cannot be bound to one delivered source."""


@dataclass(frozen=True)
class V2RedeliverySourceBinding:
    absolute_source_start_ms: int
    absolute_source_end_ms: int
    source_recording_basename: str
    source_sha256: str


def resolve_v2_redelivery_source_binding(
    *,
    spec: Mapping[str, object],
    piece_provenance_rows: Sequence[Mapping[str, object]],
    final_start: int,
    final_end: int,
) -> V2RedeliverySourceBinding | None:
    """Resolve one content source while ignoring only trailing reserve pieces."""

    config = spec.get("subtitle_redelivery_baseline")
    if not isinstance(config, Mapping) or config.get("schema_version") != V2_SCHEMA_VERSION:
        return None
    pieces = spec.get("pieces") or []
    try:
        content_index = single_content_piece_index(pieces)
    except (TypeError, ValueError) as exc:
        raise RedeliverySourceBindingError(
            "REDELIVERY_BASELINE_V2_REQUIRES_ONE_BOUND_SOURCE_PIECE"
        ) from exc
    if len(piece_provenance_rows) != len(pieces):
        raise RedeliverySourceBindingError(
            "REDELIVERY_BASELINE_V2_REQUIRES_ONE_BOUND_SOURCE_PIECE"
        )
    piece = pieces[content_index]
    provenance = piece_provenance_rows[content_index]
    try:
        piece_start_ms = int(piece["start_ms"])
        piece_end_ms = int(piece["end_ms"])
        padded_content_start_ms = sum(
            int(row["end_ms"]) - int(row["start_ms"])
            for row in pieces[:content_index]
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise RedeliverySourceBindingError(
            "REDELIVERY_BASELINE_V2_SOURCE_PIECE_INTERVAL_INVALID"
        ) from exc
    padded_content_end_ms = padded_content_start_ms + piece_end_ms - piece_start_ms
    if (
        piece_end_ms <= piece_start_ms
        or final_start < padded_content_start_ms
        or final_end <= final_start
        or final_end > padded_content_end_ms
    ):
        raise RedeliverySourceBindingError(
            "REDELIVERY_BASELINE_V2_FINAL_INTERVAL_OUTSIDE_CONTENT_PIECE"
        )
    source_path = str(provenance.get("source_path") or "").strip()
    source_sha256 = str(provenance.get("source_sha256") or "").strip()
    if not source_path or not source_sha256:
        raise RedeliverySourceBindingError(
            "REDELIVERY_BASELINE_V2_SOURCE_PROVENANCE_MISSING"
        )
    content_relative_start_ms = final_start - padded_content_start_ms
    content_relative_end_ms = final_end - padded_content_start_ms
    return V2RedeliverySourceBinding(
        absolute_source_start_ms=piece_start_ms + content_relative_start_ms,
        absolute_source_end_ms=piece_start_ms + content_relative_end_ms,
        source_recording_basename=Path(source_path).name,
        source_sha256=source_sha256,
    )


def final_recut_absolute_source_interval(
    spec: Mapping[str, object],
    *,
    final_start: int,
    final_end: int,
    v2_binding: V2RedeliverySourceBinding | None,
) -> tuple[int | None, int | None]:
    """Absolute delivered interval for provenance, when it is unambiguous."""

    if v2_binding is not None:
        return (
            v2_binding.absolute_source_start_ms,
            v2_binding.absolute_source_end_ms,
        )
    pieces = spec.get("pieces") or []
    if len(pieces) != 1:
        return None, None
    try:
        piece_start_ms = int(pieces[0]["start_ms"])
    except (KeyError, TypeError, ValueError):
        return None, None
    return piece_start_ms + final_start, piece_start_ms + final_end
