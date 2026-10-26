"""Bounded alignment helpers for chat-authority subtitle spans."""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from src.autoslice.chat_evidence import normalize_chat_text


MAX_INTERJECTION_ALIGNMENT_STATES = 512


def remove_orphaned_splice_marks(
    *,
    source: str,
    start: int,
    end: int,
    head: str,
    replacement: str,
    tail: str,
    mark_pairs: Mapping[str, str],
) -> tuple[str, tuple[str, ...]]:
    """Keep a normalized splice from retaining half of a source mark pair.

    Only an unchanged source prefix/suffix can lose a mark here, and only
    when its mate was inside the replaced interval. Words and authority
    punctuation are never edited. Already malformed inputs or a result that
    cannot be repaired using those exact source marks stay with the caller's
    existing structural guard.
    """
    combined = head + replacement + tail
    closers = frozenset(mark_pairs.values())

    def paired_positions(text: str) -> list[tuple[int, int]] | None:
        stack: list[int] = []
        pairs: list[tuple[int, int]] = []
        for index, char in enumerate(text):
            if char in mark_pairs:
                stack.append(index)
            elif char in closers:
                if not stack or mark_pairs[text[stack[-1]]] != char:
                    return None
                pairs.append((stack.pop(), index))
        return None if stack else pairs

    if paired_positions(combined) is not None:
        return combined, ()
    source_pairs = paired_positions(source)
    if source_pairs is None or not 0 <= start <= end <= len(source):
        return combined, ()
    # These comparisons validate the explicitly supplied cut, rather than
    # treating authority-generated text as an original source prefix/suffix.
    head_is_source = head == source[:start]
    tail_is_source = tail == source[end:]
    removals: set[int] = set()
    for left, right in source_pairs:
        if left < start <= right < end and head_is_source:
            removals.add(left)
        elif start <= left < end <= right and tail_is_source:
            removals.add(len(head) + len(replacement) + right - end)
    repaired = "".join(c for i, c in enumerate(combined) if i not in removals)
    if not removals or paired_positions(repaired) is None:
        return combined, ()
    return repaired, tuple(combined[i] for i in sorted(removals))


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
