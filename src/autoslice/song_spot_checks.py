"""Bind named listening checks to the actual performed lyric intervals."""

from collections.abc import Mapping, Sequence

from src.autoslice.song_common import normalize_lyric_text


def repeated_section_spot_matches(
    lyric_texts: Sequence[str],
    alignment: Sequence[Mapping[str, object]],
    spot_ms: object,
) -> bool:
    """A repeated-section check must lie inside a later audible recurrence.

    Unlike the first-line onset check, the provider contract names the audible
    event, so an interior point of a long repeated lyric is valid. The half-open
    interval also excludes nearby silence and the first occurrence.
    """
    if len(lyric_texts) != len(alignment):
        return False
    seen: set[str] = set()
    later_intervals: list[tuple[object, object]] = []
    for text, row in zip(lyric_texts, alignment):
        normalized = normalize_lyric_text(text)
        if not normalized:
            continue
        if normalized in seen:
            later_intervals.append((row.get("cue_start_ms"), row.get("cue_end_ms")))
        seen.add(normalized)
    if not later_intervals:
        return True
    return type(spot_ms) is int and any(
        type(start) is int and type(end) is int and start <= spot_ms < end
        for start, end in later_intervals
    )
