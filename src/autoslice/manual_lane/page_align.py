"""Which doubt belongs to a listening-page line (page builders only; no pipeline rule lives here).

A final subtitle line is re-split and re-merged by normalize, so it does not keep the BCUT cue id of the doubt.  The
line therefore takes the doubt whose absolute window overlaps it the most, and only when that overlap is more than half
of the line's own duration.  A line that does not pass gets no doubt: its basis and confidence stay empty.  A slight
overlap with a neighbouring line (tens of milliseconds) never lends its basis.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TypeVar, TypedDict

MIN_OVERLAP_FRACTION = 0.5


class AlignedDoubt(TypedDict, total=False):
    id: str
    abs_start_ms: float | str
    abs_end_ms: float | str
    state: str
    confidence: str


_Doubt = TypeVar("_Doubt", bound=AlignedDoubt)


def match_doubt(start_ms: float, end_ms: float, doubts: Sequence[_Doubt]) -> _Doubt | None:
    """The doubt with the largest overlap, if that overlap is strictly more than half of the line; else None.
    Doubts without absolute times (auto-kept cues) never match."""
    duration = float(end_ms) - float(start_ms)
    if duration <= 0:
        return None
    best: _Doubt | None = None
    best_overlap = 0.0
    for doubt in doubts:
        if "abs_start_ms" not in doubt or "abs_end_ms" not in doubt:
            continue
        overlap = min(float(end_ms), float(doubt["abs_end_ms"])) - max(float(start_ms), float(doubt["abs_start_ms"]))
        if overlap > best_overlap:
            best, best_overlap = doubt, overlap
    if best is None or best_overlap <= MIN_OVERLAP_FRACTION * duration:
        return None
    return best


def overlap_fraction(start_ms: float, end_ms: float, doubt: AlignedDoubt) -> float:
    """The share of the line's duration covered by the doubt's window (for the self-check report)."""
    duration = float(end_ms) - float(start_ms)
    if duration <= 0:
        return 0.0
    overlap = min(float(end_ms), float(doubt["abs_end_ms"])) - max(float(start_ms), float(doubt["abs_start_ms"]))
    return max(0.0, overlap) / duration
