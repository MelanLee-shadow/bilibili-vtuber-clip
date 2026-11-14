"""When a line's own words were said, read from the recognisers' cue times.

A subtitle list is one line after another, so where two voices overlap the lines are placed in turn and a line can sit
later than its words (normalize._sequence).  That is right while both lines are shown.  When one of them is not shown
(the watched video's own lines, step 5), the shown line has no reason to wait for it: its time is the time of its own
words, and the recognisers heard those words with their own cue times.
"""
from __future__ import annotations

from collections.abc import Sequence
from difflib import SequenceMatcher

from .srt_text import SrtCue


def _letters(text: object) -> str:
    return "".join(ch for ch in str(text) if ch.isalnum()).lower()


def _common(a: str, b: str) -> int:
    """Letters of `a` found in `b` in runs of two or more, in order (a run of one is chance)."""
    return sum(m.size for m in SequenceMatcher(None, a, b, autojunk=False).get_matching_blocks() if m.size >= 2)


def spoken_span(text: str, tracks: Sequence[Sequence[SrtCue]], lo: float, hi: float) -> tuple[float, float] | None:
    """(start, end) of the recogniser cues that carry `text`, looked for between `lo` and `hi`; None when no recogniser
    heard at least half of the line there.  A cue carries the line when at least half of the cue's letters are the
    line's (a cue that is mostly somebody else's words is not hers).  Of the recognisers, the one whose cues cover the
    most of the line is read; equal cover: the shorter span."""
    want = _letters(text)
    if len(want) < 2:
        return None
    best: tuple[float, float, tuple[float, float]] | None = None
    for cues in tracks:
        own = [c for c in cues if float(c["end_s"]) > lo and float(c["start_s"]) < hi
               and 2 * _common(_letters(c["text"]), want) >= len(_letters(c["text"])) > 0]
        if not own:
            continue
        cover = _common(want, "".join(_letters(c["text"]) for c in own)) / len(want)
        span = (float(own[0]["start_s"]), float(own[-1]["end_s"]))
        key = (cover, span[0] - span[1], span)
        if 2 * cover >= 1 and (best is None or key[:2] > best[:2]):
            best = key
    return best[2] if best else None
