"""Session-scoped lexical candidates from repeated raw transcript surfaces.

The raw ASR draft may preserve a deliberate wordplay surface that later
transcription/correction normalizes into a common word.  Repetition in the
same session can nominate that raw surface for the ordinary closed-set judge,
but it never authorizes a mutation and never writes a global glossary.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any, Iterable

from src.autoslice.jingting_chunker import parse_srt_cues


PROVENANCE_SCHEMA = "session-transcript-recurrence-provenance.v1"
DEFAULT_MAX_CUE_DISTANCE = 6
DEFAULT_CANDIDATE_LIMIT = 8
_LEXICAL_SURFACE = re.compile(r"^[A-Za-z0-9_\-\u3400-\u9fff]+$")
_HAN = re.compile(r"[\u3400-\u9fff]")


@dataclass(frozen=True)
class _Edit:
    suspect: str
    replacement: str
    current_start: int
    current_end: int
    proposed_start: int
    proposed_end: int


def _single_edit(current: str, proposed: str) -> _Edit | None:
    if current == proposed:
        return None
    prefix = 0
    while (
        prefix < len(current)
        and prefix < len(proposed)
        and current[prefix] == proposed[prefix]
    ):
        prefix += 1
    suffix = 0
    while (
        suffix < len(current) - prefix
        and suffix < len(proposed) - prefix
        and current[len(current) - suffix - 1]
        == proposed[len(proposed) - suffix - 1]
    ):
        suffix += 1
    current_end = len(current) - suffix if suffix else len(current)
    proposed_end = len(proposed) - suffix if suffix else len(proposed)
    suspect = current[prefix:current_end]
    replacement = proposed[prefix:proposed_end]
    if not suspect or not replacement or len(suspect) > 24 or len(replacement) > 24:
        return None
    if abs(len(suspect) - len(replacement)) > 8:
        return None
    return _Edit(
        suspect=suspect,
        replacement=replacement,
        current_start=prefix,
        current_end=current_end,
        proposed_start=prefix,
        proposed_end=proposed_end,
    )


def _surface_occurrences(
    cues: Iterable[Any], surface: str, *, target_cue_index: int, max_distance: int
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    folded_surface = surface.casefold()
    for cue_index, cue in enumerate(cues, start=1):
        if abs(cue_index - target_cue_index) > max_distance:
            continue
        folded_text = cue.text.casefold()
        start = 0
        while True:
            start = folded_text.find(folded_surface, start)
            if start < 0:
                break
            end = start + len(surface)
            rows.append(
                {
                    "cue_index": cue_index,
                    "start_ms": cue.start_ms,
                    "end_ms": cue.end_ms,
                    "start_codepoint": start,
                    "end_codepoint": end,
                    "cue_text_sha256": "sha256:"
                    + hashlib.sha256(cue.text.encode("utf-8")).hexdigest(),
                }
            )
            start = end
    return rows


def _recurring_surface(
    *,
    proposed: str,
    edit: _Edit,
    draft_cues: list[Any],
    current_cues: list[Any],
    target_cue_index: int,
    max_distance: int,
) -> tuple[str, list[dict[str, object]]] | None:
    eligible: list[tuple[tuple[int, int, int, str], str, list[dict[str, object]]]] = []
    for width in range(2, min(8, len(proposed)) + 1):
        min_start = max(0, edit.proposed_end - width)
        max_start = min(edit.proposed_start, len(proposed) - width)
        for start in range(min_start, max_start + 1):
            end = start + width
            if start > edit.proposed_start or end < edit.proposed_end:
                continue
            surface = proposed[start:end]
            if not _LEXICAL_SURFACE.fullmatch(surface) or not _HAN.search(surface):
                continue
            occurrences = _surface_occurrences(
                draft_cues,
                surface,
                target_cue_index=target_cue_index,
                max_distance=max_distance,
            )
            distinct_cues = sorted(
                {int(row["cue_index"]) for row in occurrences}
            )
            if len(distinct_cues) < 2:
                continue
            current_occurrences = _surface_occurrences(
                current_cues,
                surface,
                target_cue_index=target_cue_index,
                max_distance=max_distance,
            )
            if len(current_occurrences) >= len(occurrences):
                continue
            span = max(distinct_cues) - min(distinct_cues)
            priority = (-len(distinct_cues), width, span, surface)
            eligible.append((priority, surface, occurrences))
    if not eligible:
        return None
    _priority, surface, occurrences = min(eligible, key=lambda item: item[0])
    return surface, occurrences


def session_transcript_recurrence_candidates(
    *,
    raw_draft_srt: str,
    current_srt: str,
    enabled: bool = True,
    max_cue_distance: int = DEFAULT_MAX_CUE_DISTANCE,
    limit: int = DEFAULT_CANDIDATE_LIMIT,
) -> list[dict[str, object]]:
    """Return candidate-only draft surfaces repeated in nearby session cues.

    Alignment is deliberately strict: a raw/current cue must share the exact
    timing key.  A recurrence must occupy at least two distinct nearby cues and
    be underrepresented in the current transcript.  Every returned candidate
    still requires a fresh candidate-blind acoustic observation at its target.
    """

    if (
        not enabled
        or isinstance(max_cue_distance, bool)
        or not isinstance(max_cue_distance, int)
        or max_cue_distance < 1
        or isinstance(limit, bool)
        or not isinstance(limit, int)
        or limit < 1
    ):
        return []
    draft_cues = [cue for cue in parse_srt_cues(raw_draft_srt) if cue.text.strip()]
    current_cues = [cue for cue in parse_srt_cues(current_srt) if cue.text.strip()]
    current_by_timing: dict[tuple[int, int], list[tuple[int, Any]]] = {}
    for index, cue in enumerate(current_cues, start=1):
        current_by_timing.setdefault((cue.start_ms, cue.end_ms), []).append(
            (index, cue)
        )
    raw_sha256 = hashlib.sha256(raw_draft_srt.encode("utf-8")).hexdigest()
    current_sha256 = hashlib.sha256(current_srt.encode("utf-8")).hexdigest()
    candidates: list[dict[str, object]] = []
    for raw_index, draft_cue in enumerate(draft_cues, start=1):
        matches = current_by_timing.get((draft_cue.start_ms, draft_cue.end_ms), [])
        if len(matches) != 1:
            continue
        cue_index, current_cue = matches[0]
        if cue_index != raw_index:
            continue
        edit = _single_edit(current_cue.text, draft_cue.text)
        if edit is None:
            continue
        recurrence = _recurring_surface(
            proposed=draft_cue.text,
            edit=edit,
            draft_cues=draft_cues,
            current_cues=current_cues,
            target_cue_index=raw_index,
            max_distance=max_cue_distance,
        )
        if recurrence is None:
            continue
        surface, occurrences = recurrence
        occurrence_cues = sorted(
            {int(row["cue_index"]) for row in occurrences}
        )
        provenance = {
            "schema_version": PROVENANCE_SCHEMA,
            "kind": "session_transcript_recurrence",
            "scope": "session",
            "session_scope_id": "sha256:" + raw_sha256,
            "source_srt_sha256": "sha256:" + raw_sha256,
            "current_srt_sha256": "sha256:" + current_sha256,
            "surface": surface,
            "occurrence_count": len(occurrences),
            "occurrence_cue_count": len(occurrence_cues),
            "occurrence_positions": occurrences,
            "max_cue_distance": max_cue_distance,
            "mutation_authorized": False,
            "global_glossary_authorized": False,
        }
        candidates.append(
            {
                "cue": cue_index,
                "kind": "context",
                "suspect": edit.suspect,
                "proposed_full_cue": draft_cue.text,
                "repair_class": "phonetic",
                "source_surface": surface,
                "base_text_sha256": hashlib.sha256(
                    current_cue.text.encode("utf-8")
                ).hexdigest(),
                "evidence_cue_ids": [
                    index for index in occurrence_cues if index != raw_index
                ],
                "candidate_provenance": provenance,
                "candidate_origin": "session_transcript_recurrence",
                "why": (
                    "same-session raw transcript repeats the proposed lexical "
                    "surface in nearby cues; candidate only, each target still "
                    "requires candidate-blind acoustic fit and CPA judgment"
                ),
            }
        )
    candidates.sort(
        key=lambda row: (
            int(row["cue"]),
            str((row["candidate_provenance"] or {}).get("surface") or ""),
        )
    )
    return candidates[:limit]
