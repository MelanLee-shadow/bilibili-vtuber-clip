from __future__ import annotations

from dataclasses import dataclass

from src.autoslice.auto_review import DecisionAction


@dataclass(frozen=True)
class AnchorCandidate:
    """CPA/semantic candidate treated as an interesting anchor, not final bounds."""

    candidate_id: str
    anchor_start_ms: int
    anchor_end_ms: int


@dataclass(frozen=True)
class BoundaryResolution:
    candidate_id: str
    action: DecisionAction
    resolved_start_ms: int
    resolved_end_ms: int
    start_boundary_score: float
    end_boundary_score: float
    reason_codes: tuple[str, ...] = ()
    next_start_ms: int | None = None
    next_end_ms: int | None = None

    @property
    def duration_ms(self) -> int:
        return max(0, self.resolved_end_ms - self.resolved_start_ms)


