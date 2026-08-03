from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from src.autoslice.auto_review import DecisionAction


@dataclass(frozen=True)
class AnchorCandidate:
    """CPA/semantic candidate treated as an interesting anchor, not final bounds."""

    candidate_id: str
    anchor_start_ms: int
    anchor_end_ms: int


@dataclass(frozen=True)
class TalkCue:
    cue_id: str
    start_ms: int
    end_ms: int
    text: str
    starts_topic: bool = False
    ends_topic: bool = False
    has_payoff: bool = False
    open_loop_delta: int = 0


@dataclass(frozen=True)
class BoundaryPolicy:
    # Short complete talk clips are allowed below this; this is a publish warning,
    # not a mechanical padding target.
    min_publish_ms: int = 20_000
    soft_max_ms: int = 180_000
    hard_max_ms: int = 300_000
    start_publish_score: float = 0.92
    end_publish_score: float = 0.95


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


_CONNECTIVE_PREFIXES = (
    "然后",
    "所以",
    "但是",
    "因为",
    "结果",
    "接着",
    "后来",
    "而且",
    "不过",
)


def resolve_talk_boundary(
    anchor: AnchorCandidate,
    cues: Sequence[TalkCue],
    *,
    policy: BoundaryPolicy | None = None,
) -> BoundaryResolution:
    """Resolve talk clip boundaries from source-context cues.

    The anchor is only a recall hint.  This prototype chooses natural utterance /
    topic boundaries around the anchor and fails closed when a natural closure is
    unavailable within the hard maximum.  It deliberately does not pad short
    complete clips to 60 seconds and does not truncate long open clips at 180s.
    """

    policy = policy or BoundaryPolicy()
    ordered_cues = sorted(cues, key=lambda cue: (cue.start_ms, cue.end_ms, cue.cue_id))
    if not ordered_cues:
        return BoundaryResolution(
            candidate_id=anchor.candidate_id,
            action=DecisionAction.DROP,
            resolved_start_ms=anchor.anchor_start_ms,
            resolved_end_ms=anchor.anchor_end_ms,
            start_boundary_score=0.0,
            end_boundary_score=0.0,
            reason_codes=("NO_CONTEXT_CUES",),
        )

    anchor_cues = _cues_overlapping(ordered_cues, anchor.anchor_start_ms, anchor.anchor_end_ms)
    anchor_start_cue = anchor_cues[0] if anchor_cues else _nearest_cue_at_or_after(ordered_cues, anchor.anchor_start_ms)
    anchor_end_cue = anchor_cues[-1] if anchor_cues else _nearest_cue_at_or_before(ordered_cues, anchor.anchor_end_ms)

    start_cue, start_score, start_reasons = _resolve_start_cue(ordered_cues, anchor_start_cue)
    end_cue, end_score, end_reasons, open_loops_at_anchor_end = _resolve_end_cue(
        ordered_cues,
        anchor_end_cue,
        start_cue,
        anchor.anchor_end_ms,
    )

    resolved_start_ms = start_cue.start_ms if start_cue else anchor.anchor_start_ms
    resolved_end_ms = anchor_end_cue.end_ms if anchor_end_cue else anchor.anchor_end_ms
    natural_end_ms = end_cue.end_ms if end_cue else None

    reasons: list[str] = []
    reasons.extend(start_reasons)

    if natural_end_ms is None:
        reasons.append("NO_NATURAL_CLOSURE")
        reasons.extend(end_reasons)
        return BoundaryResolution(
            candidate_id=anchor.candidate_id,
            action=DecisionAction.DROP,
            resolved_start_ms=resolved_start_ms,
            resolved_end_ms=resolved_end_ms,
            start_boundary_score=start_score,
            end_boundary_score=end_score,
            reason_codes=tuple(dict.fromkeys(reasons)),
        )

    natural_duration_ms = natural_end_ms - resolved_start_ms
    if natural_duration_ms > policy.hard_max_ms:
        reasons.append("HARD_MAX_WITHOUT_CLOSURE")
        reasons.extend(end_reasons or ["END_BOUNDARY_LOW"])
        return BoundaryResolution(
            candidate_id=anchor.candidate_id,
            action=DecisionAction.DROP,
            resolved_start_ms=resolved_start_ms,
            resolved_end_ms=resolved_end_ms,
            start_boundary_score=start_score,
            end_boundary_score=end_score,
            reason_codes=tuple(dict.fromkeys(reasons)),
        )

    needs_recut = False
    next_start_ms: int | None = None
    next_end_ms: int | None = None

    if start_score < policy.start_publish_score:
        needs_recut = True
        reasons.append("START_BOUNDARY_LOW")
        next_start_ms = resolved_start_ms

    # If the anchor already closed all open loops at a payoff and only needs the
    # immediate reaction cue, resolve locally without sending it through recut.
    # Otherwise, a natural closure beyond the anchor means the candidate was an
    # anchor-only window and needs a longer source-context recut.
    if (natural_end_ms > anchor.anchor_end_ms and open_loops_at_anchor_end > 0) or end_score < policy.end_publish_score:
        needs_recut = True
        reasons.append("END_BOUNDARY_LOW")
        next_end_ms = natural_end_ms

    if open_loops_at_anchor_end > 0:
        reasons.append("OPEN_LOOPS_PRESENT")

    if natural_duration_ms > policy.soft_max_ms:
        reasons.append("SOFT_MAX_EXCEEDED")

    if needs_recut:
        if next_start_ms is None:
            next_start_ms = resolved_start_ms
        return BoundaryResolution(
            candidate_id=anchor.candidate_id,
            action=DecisionAction.AUTO_RECUT,
            resolved_start_ms=resolved_start_ms,
            resolved_end_ms=anchor_end_cue.end_ms if anchor_end_cue else anchor.anchor_end_ms,
            start_boundary_score=start_score,
            end_boundary_score=end_score,
            reason_codes=tuple(dict.fromkeys(reasons)),
            next_start_ms=next_start_ms,
            next_end_ms=next_end_ms,
        )

    upload_reasons = list(dict.fromkeys(reasons))
    if natural_duration_ms < policy.min_publish_ms:
        upload_reasons.append("SHORT_BUT_COMPLETE")

    return BoundaryResolution(
        candidate_id=anchor.candidate_id,
        action=DecisionAction.AUTO_UPLOAD,
        resolved_start_ms=resolved_start_ms,
        resolved_end_ms=natural_end_ms,
        start_boundary_score=start_score,
        end_boundary_score=end_score,
        reason_codes=tuple(upload_reasons),
    )


def _cues_overlapping(cues: Sequence[TalkCue], start_ms: int, end_ms: int) -> list[TalkCue]:
    return [cue for cue in cues if cue.end_ms > start_ms and cue.start_ms < end_ms]


def _nearest_cue_at_or_after(cues: Sequence[TalkCue], ms: int) -> TalkCue | None:
    for cue in cues:
        if cue.end_ms >= ms:
            return cue
    return cues[-1] if cues else None


def _nearest_cue_at_or_before(cues: Sequence[TalkCue], ms: int) -> TalkCue | None:
    previous = None
    for cue in cues:
        if cue.start_ms <= ms:
            previous = cue
        else:
            break
    return previous


def _resolve_start_cue(
    cues: Sequence[TalkCue], anchor_start_cue: TalkCue | None
) -> tuple[TalkCue | None, float, list[str]]:
    if anchor_start_cue is None:
        return None, 0.0, ["NO_START_CUE"]

    if _is_good_start(anchor_start_cue):
        return anchor_start_cue, 0.97, []

    # If the anchor begins with a connective, walk back to the nearest explicit
    # topic start.  This proves the anchor was not a final cut start.
    anchor_index = cues.index(anchor_start_cue)
    for cue in reversed(cues[: anchor_index + 1]):
        if cue.starts_topic:
            return cue, 0.60, ["ANCHOR_STARTS_WITH_CONNECTIVE"]

    return anchor_start_cue, 0.60, ["ANCHOR_STARTS_WITH_CONNECTIVE"]


def _resolve_end_cue(
    cues: Sequence[TalkCue],
    anchor_end_cue: TalkCue | None,
    start_cue: TalkCue | None,
    anchor_end_ms: int,
) -> tuple[TalkCue | None, float, list[str], int]:
    if anchor_end_cue is None:
        return None, 0.0, ["NO_END_CUE"], 0

    start_index = cues.index(start_cue) if start_cue in cues else 0
    anchor_end_index = cues.index(anchor_end_cue)
    open_loops = 0
    open_loops_at_anchor_end = 0
    payoff_seen = False

    for index, cue in enumerate(cues[start_index:], start=start_index):
        open_loops = max(0, open_loops + cue.open_loop_delta)
        payoff_seen = payoff_seen or cue.has_payoff
        if index == anchor_end_index:
            open_loops_at_anchor_end = open_loops
        if cue.end_ms < anchor_end_ms:
            continue
        if cue.ends_topic and open_loops == 0 and payoff_seen:
            score = 0.98 if (cue.end_ms <= anchor_end_ms or open_loops_at_anchor_end == 0) else 0.70
            return cue, score, ([] if score >= 0.95 else ["END_BOUNDARY_LOW"]), open_loops_at_anchor_end

    return None, 0.50, ["OPEN_LOOPS_PRESENT"], open_loops_at_anchor_end


def _is_good_start(cue: TalkCue) -> bool:
    text = cue.text.strip()
    return cue.starts_topic and not text.startswith(_CONNECTIVE_PREFIXES)
