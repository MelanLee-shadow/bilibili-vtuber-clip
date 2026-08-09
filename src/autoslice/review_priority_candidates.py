"""Assemble deterministic candidate-only inputs for correction review."""

from __future__ import annotations

from pathlib import Path
from typing import Mapping, Sequence

from src.autoslice.fidelity_review_candidates import fidelity_review_candidates
from src.autoslice.session_transcript_recurrence import (
    session_transcript_recurrence_candidates,
)


def review_priority_candidates(
    padded: Path,
    current_srt: str,
) -> list[dict[str, object]]:
    """Combine F16 fidelity-kept and F17 session-recurrence candidates."""

    candidates = fidelity_review_candidates(padded, current_srt)
    try:
        raw_draft_srt = padded.with_suffix(".asr_draft.srt").read_text(
            encoding="utf-8", errors="replace"
        )
    except OSError:
        raw_draft_srt = ""
    if raw_draft_srt:
        candidates.extend(
            session_transcript_recurrence_candidates(
                raw_draft_srt=raw_draft_srt,
                current_srt=current_srt,
            )
        )
    return candidates


def review_priority_candidate_counts(
    candidates: Sequence[Mapping[str, object]],
) -> dict[str, int]:
    """Split audit counts without conflating F16 and F17 priority rows."""

    origins = [str(row.get("candidate_origin") or "") for row in candidates]
    return {
        "fidelity_candidate_count": sum(
            origin in {"draft_fidelity_kept", "fidelity_guard_reverted_candidate"}
            for origin in origins
        ),
        "session_transcript_recurrence_candidate_count": origins.count(
            "session_transcript_recurrence"
        ),
    }
