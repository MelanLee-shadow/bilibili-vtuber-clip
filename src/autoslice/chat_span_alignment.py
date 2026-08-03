"""Bounded alignment helpers for chat-authority subtitle spans."""

from __future__ import annotations

from collections.abc import Iterable

from src.autoslice.chat_evidence import normalize_chat_text


MAX_INTERJECTION_ALIGNMENT_STATES = 512


def _delete_once_candidates(text: str, fragment: str) -> list[str]:
    positions: list[int] = []
    start = 0
    while True:
        index = text.find(fragment, start)
        if index < 0:
            break
        positions.append(index)
        start = index + 1
    if not positions:
        return [text]
    return [
        text[:index] + text[index + len(fragment) :]
        for index in positions
    ]


def strip_interjections_once(
    span_norm: str,
    interjections: Iterable[object] | None,
    *,
    required_substring: str | None = None,
) -> str:
    """Remove each declared interjection once without deleting a neighbour.

    Final verification windows include cues touching both span boundaries, so
    a short token can occur in a neighbouring cue as well as in the repaired
    cue.  With a required surface, bounded combination search chooses only a
    deletion path that leaves that surface contiguous.  Without one, behavior
    remains the historical deterministic first-occurrence deletion.
    """

    required_norm = normalize_chat_text(required_substring or "")
    if required_norm and required_norm in span_norm:
        return span_norm
    normalized_fragments = [
        normalized
        for fragment in interjections or ()
        if (normalized := normalize_chat_text(str(fragment)))
    ]
    if required_norm:
        states = [span_norm]
        for fragment in normalized_fragments:
            next_states: list[str] = []
            seen: set[str] = set()
            for state in states:
                for candidate in _delete_once_candidates(state, fragment):
                    if required_norm in candidate:
                        return candidate
                    if candidate not in seen:
                        seen.add(candidate)
                        next_states.append(candidate)
            states = next_states[:MAX_INTERJECTION_ALIGNMENT_STATES]
            if not states:
                break
        if states:
            return states[0]
    for fragment in normalized_fragments:
        candidates = _delete_once_candidates(span_norm, fragment)
        if candidates[0] != span_norm:
            span_norm = candidates[0]
    return span_norm
