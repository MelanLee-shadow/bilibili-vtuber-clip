from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from src.autoslice.auto_review import DecisionAction
from src.autoslice.boundary_resolver import AnchorCandidate, BoundaryResolution
from src.autoslice.review_evidence import SourceCue

FULL_SESSION_CANDIDATE_SCHEMA_VERSION = "full-session-candidate.v1"

_SONG_LIKE_MARKERS = (
    "啊啊",
    "啦啦",
    "君",
    "愛",
    "の",
    "baby",
    "just take",
    "唱了",
    "第八首",
    "最后の",
    "上天啊",
    "相爱",
    "爱情",
    "婚礼",
    "祝福你",
    "拥抱他",
)


@dataclass(frozen=True)
class FullSessionCandidate:
    """A source-timeline candidate selected from complete-session cues."""

    anchor: AnchorCandidate
    boundary: BoundaryResolution
    cues: tuple[SourceCue, ...]
    text_preview: str
    schema_version: str = FULL_SESSION_CANDIDATE_SCHEMA_VERSION
    content_type_hint: str = "talk"

    def to_manifest(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "candidate_id": self.anchor.candidate_id,
            "content_type_hint": self.content_type_hint,
            "requires_full_source_song_boundary_redo": self.content_type_hint == "song",
            "anchor_start_ms": self.anchor.anchor_start_ms,
            "anchor_end_ms": self.anchor.anchor_end_ms,
            "boundary_resolution": {
                "action": self.boundary.action.value,
                "resolved_start_ms": self.boundary.resolved_start_ms,
                "resolved_end_ms": self.boundary.resolved_end_ms,
                "start_boundary_score": self.boundary.start_boundary_score,
                "end_boundary_score": self.boundary.end_boundary_score,
                "reason_codes": list(self.boundary.reason_codes),
                "next_start_ms": self.boundary.next_start_ms,
                "next_end_ms": self.boundary.next_end_ms,
            },
            "text_preview": self.text_preview,
            "source_cues": [cue.to_manifest() for cue in self.cues],
        }

    def to_source_context_job(self, *, source_duration_ms: int) -> dict[str, object]:
        if self.content_type_hint == "song":
            start_ms = 0
            end_ms = source_duration_ms
        else:
            start_ms = max(0, self.boundary.next_start_ms if self.boundary.next_start_ms is not None else self.boundary.resolved_start_ms)
            end_ms = self.boundary.next_end_ms if self.boundary.next_end_ms is not None else self.boundary.resolved_end_ms
            end_ms = min(source_duration_ms, end_ms)
        return {
            "schema_version": "source-context-job-from-full-session-candidate.v1",
            "candidate_id": self.anchor.candidate_id,
            "content_type_hint": self.content_type_hint,
            "song_candidate": self.content_type_hint == "song",
            "requires_full_source_song_boundary_redo": self.content_type_hint == "song",
            "title": self.text_preview[:80],
            "timeline": {
                "source_duration_ms": source_duration_ms,
                "anchor_start_ms": self.anchor.anchor_start_ms,
                "anchor_end_ms": self.anchor.anchor_end_ms,
                "context_start_ms": start_ms,
                "context_end_ms": end_ms,
                "context_duration_ms": max(0, end_ms - start_ms),
            },
        }


def _song_anchor_boundary(anchor: AnchorCandidate) -> BoundaryResolution:
    return BoundaryResolution(
        candidate_id=anchor.candidate_id,
        action=DecisionAction.AUTO_RECUT,
        resolved_start_ms=anchor.anchor_start_ms,
        resolved_end_ms=anchor.anchor_end_ms,
        start_boundary_score=0.0,
        end_boundary_score=0.0,
        reason_codes=(
            "SONG_BOUNDARY_REDO_REQUIRED",
            "SONG_FULL_SOURCE_REQUIRED",
            "EXTERNAL_LRC_REQUIRED",
            "LYRIC_ORDER_PROOF_MISSING",
        ),
        next_start_ms=0,
        next_end_ms=None,
    )


def _overlaps_selected(candidate: FullSessionCandidate, selected: Sequence[FullSessionCandidate]) -> bool:
    start = candidate.boundary.resolved_start_ms
    end = candidate.boundary.resolved_end_ms
    for existing in selected:
        if candidate.content_type_hint == "song" and existing.content_type_hint == "song":
            overlap = min(
                candidate.anchor.anchor_end_ms, existing.anchor.anchor_end_ms
            ) - max(candidate.anchor.anchor_start_ms, existing.anchor.anchor_start_ms)
            # Song anchors are recall windows, not trusted final boundaries.
            # Any real temporal overlap is duplicate evidence for the same
            # performance; merely being adjacent (even by only a few seconds)
            # is not.  The old <180s gap rule swallowed distinct back-to-back
            # songs and was asymmetric for candidates emitted out of order.
            if overlap > 0:
                return True
            continue
        if existing.content_type_hint == "song" and candidate.content_type_hint != "song":
            anchor_gap = candidate.anchor.anchor_start_ms - existing.anchor.anchor_end_ms
            if 0 <= anchor_gap < 180_000 and _is_song_continuation_like(candidate.text_preview):
                return True
        overlap = min(end, existing.boundary.resolved_end_ms) - max(start, existing.boundary.resolved_start_ms)
        if overlap <= 0:
            continue
        shorter = min(end - start, existing.boundary.resolved_end_ms - existing.boundary.resolved_start_ms)
        if shorter > 0 and overlap / shorter >= 0.5:
            return True
    return False


def _join_text(cues: Sequence[SourceCue]) -> str:
    return " ".join(cue.text.strip() for cue in cues if cue.text.strip())


def _is_song_continuation_like(text: str) -> bool:
    compact = "".join(text.lower().split())
    return any(marker.lower() in compact for marker in _SONG_LIKE_MARKERS)
