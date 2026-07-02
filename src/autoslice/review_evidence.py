from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Mapping, Sequence

from src.autoslice.auto_review import CandidateReview, JingtingProvenance


REVIEW_EVIDENCE_SCHEMA_VERSION = "slice-review-evidence.v1"

_REQUIRED_FIELD_REASON_CODES: tuple[tuple[str, str], ...] = (
    ("release_ready", "RELEASE_READY_MISSING"),
    ("review_required_findings", "REVIEW_REQUIRED_FINDINGS_MISSING"),
    ("foreground_song_overlap_seconds", "FOREGROUND_SONG_OVERLAP_MISSING"),
    ("song_complete", "SONG_COMPLETENESS_MISSING"),
    ("lyrics_alignment_ready", "LYRICS_ALIGNMENT_MISSING"),
    ("start_boundary_score", "START_BOUNDARY_MISSING"),
    ("end_boundary_score", "END_BOUNDARY_MISSING"),
    ("standalone_score", "STANDALONE_MISSING"),
    ("open_loop_count", "OPEN_LOOP_EVIDENCE_MISSING"),
    ("payoff_score", "PAYOFF_MISSING_EVIDENCE"),
    ("editorial_score", "EDITORIAL_SCORE_MISSING"),
    ("duplicate_similarity", "DUPLICATE_SIMILARITY_MISSING"),
    ("subtitle_alignment_p95_ms", "SUBTITLE_ALIGNMENT_MISSING"),
    ("actual_cut_error_ms", "ACTUAL_CUT_ERROR_MISSING"),
)


@dataclass(frozen=True)
class SourceCue:
    """A normalized source-timeline cue used by review evidence.

    The identity of a cue is its source-absolute timestamp.  Clip-relative
    timings are deliberately not represented here so replay/shadow evidence can
    be traced back to the original recording and re-cut deterministically.
    """

    cue_id: str
    source_start_ms: int
    source_end_ms: int
    text: str
    language: str | None = None
    kind: str = "speech"
    speaker: str | None = None
    confidence: float | None = None

    def to_manifest(self) -> dict[str, object]:
        return _drop_none(asdict(self))


@dataclass(frozen=True)
class ReviewEvidence:
    candidate_id: str
    foreground_song_overlap_seconds: float | None = None
    song_complete: bool | None = None
    lyrics_alignment_ready: bool | None = None
    start_boundary_score: float | None = None
    end_boundary_score: float | None = None
    standalone_score: float | None = None
    payoff_score: float | None = None
    open_loop_count: int | None = None
    editorial_score: float | None = None
    duplicate_similarity: float | None = None
    subtitle_alignment_p95_ms: float | None = None
    actual_cut_error_ms: float | None = None
    evidence_gaps: Sequence[str] = ()
    checks: Sequence[Mapping[str, object]] = ()
    source_cues: Sequence[SourceCue] = ()
    metadata: Mapping[str, object] = field(default_factory=dict)

    def to_manifest(self) -> dict[str, object]:
        return {
            "schema_version": REVIEW_EVIDENCE_SCHEMA_VERSION,
            "candidate_id": self.candidate_id,
            "source_cues": [cue.to_manifest() for cue in self.source_cues],
            "checks": [dict(check) for check in self.checks],
            "evidence_gaps": list(dict.fromkeys(self.evidence_gaps)),
            "metrics": {
                "foreground_song_overlap_seconds": self.foreground_song_overlap_seconds,
                "song_complete": self.song_complete,
                "lyrics_alignment_ready": self.lyrics_alignment_ready,
                "start_boundary_score": self.start_boundary_score,
                "end_boundary_score": self.end_boundary_score,
                "standalone_score": self.standalone_score,
                "payoff_score": self.payoff_score,
                "open_loop_count": self.open_loop_count,
                "editorial_score": self.editorial_score,
                "duplicate_similarity": self.duplicate_similarity,
                "subtitle_alignment_p95_ms": self.subtitle_alignment_p95_ms,
                "actual_cut_error_ms": self.actual_cut_error_ms,
            },
            "metadata": dict(self.metadata),
        }


def to_candidate_review(
    evidence: ReviewEvidence,
    jingting_provenance: JingtingProvenance | None,
    *,
    jingting_done: bool = True,
    recut_attempt: int = 0,
    max_recut_attempts: int = 2,
) -> CandidateReview:
    """Convert normalized evidence to the existing fail-closed review contract."""

    # ``evidence_gaps`` records both hard missing evidence and soft/quality
    # findings such as CONNECTIVE starts or missing payoff.  Do not turn every
    # soft finding into ``review_required_findings`` here; the existing
    # ``review_candidate`` thresholds must still be able to choose AUTO_RECUT or
    # DROP from the normalized numeric evidence.  Only truly missing required
    # fields fail closed at this conversion boundary.
    # Missing analyzer fields are handled explicitly by ``evaluate_required_evidence``
    # in ``review_candidate``.  Do not mirror those gaps into
    # ``review_required_findings``; that field is reserved for an explicit
    # human/Jingting review-required artifact.  Mirroring here causes unrelated
    # gaps such as duplicate or render-QA evidence to be mislabeled as
    # JINGTING_REVIEW_REQUIRED.
    return CandidateReview(
        candidate_id=evidence.candidate_id,
        jingting_done=jingting_done,
        release_ready=True,
        review_required_findings=(),
        foreground_song_overlap_seconds=evidence.foreground_song_overlap_seconds,
        song_complete=evidence.song_complete,
        lyrics_alignment_ready=evidence.lyrics_alignment_ready,
        start_boundary_score=evidence.start_boundary_score,
        end_boundary_score=evidence.end_boundary_score,
        standalone_score=evidence.standalone_score,
        payoff_score=evidence.payoff_score,
        open_loop_count=evidence.open_loop_count,
        editorial_score=evidence.editorial_score,
        duplicate_similarity=evidence.duplicate_similarity,
        subtitle_alignment_p95_ms=evidence.subtitle_alignment_p95_ms,
        actual_cut_error_ms=evidence.actual_cut_error_ms,
        recut_attempt=recut_attempt,
        max_recut_attempts=max_recut_attempts,
        jingting_provenance=jingting_provenance,
    )


def required_evidence_gaps(evidence: ReviewEvidence) -> tuple[str, ...]:
    gaps: list[str] = []
    for field_name, reason_code in _REQUIRED_FIELD_REASON_CODES:
        if field_name == "release_ready":
            continue
        if field_name == "review_required_findings":
            continue
        if getattr(evidence, field_name) is None:
            gaps.append(reason_code)
    return tuple(dict.fromkeys(gaps))


def _drop_none(data: Mapping[str, object]) -> dict[str, object]:
    return {key: value for key, value in data.items() if value is not None}
