"""Joining two timed readings that may repeat each other at their boundary (U159, TASK-17).

The relation: two readings A (earlier) and B (later), each with its time span, where the end of A equals the start of
B for k characters.  That is either one speech written twice (a listening window, or a listener's window, ran into the
neighbouring sentence) or the speaker really saying it again (没有钱了没有钱了没有钱啊, said three times).  The text
alone cannot tell which, so the longest-overlap merge is wrong for the second case.  The decision comes from the
whole-clip readings of the same audio (BCUT, jianying, MAI, MOSS): over the span of A and B, the candidate join
A + B[k:] (k = 0, or any length at which A's end equals B's start) that is closest to those readings is taken.  A
source counts only when its words lie on both sides of the boundary; with no such source, nothing is merged (k = 0),
and so is a tie.

One function for every consumer: step 3 trims a listening / window-ASR reading against the neighbouring sentences
with it (stage_impl._trimmed_readings), and scoring joins truth labels written per window with it
(score.dedup_truth); the upload version (T/release) is to use the same function.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Callable, Mapping, Sequence
from typing import TYPE_CHECKING, TypedDict, cast

if TYPE_CHECKING:
    from .srt_text import TimeSpan, TimedText

MIN_OVERLAP = 2     # a repeat of one character is never a candidate (the old rules' floor, TASK-15 PREREG)


class OverlapInfo(TypedDict, total=False):
    candidates: list[int]
    sources: list[str]
    k: int
    reason: str
    costs: dict[int, int]
    timed_repetitions: list[int]
    prev: str | int | None
    next: str | int | None
    deleted: str
    side: str
    field: str


def fold(text: str) -> str:
    """Default comparison form: NFKC, case-folded, punctuation / space / symbols removed."""
    return "".join(ch for ch in unicodedata.normalize("NFKC", text).casefold() if unicodedata.category(ch)[0] not in "PZSC")


def edit_distance(a: str, b: str) -> int:
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i] + [0] * len(b)
        for j, cb in enumerate(b, 1):
            cur[j] = min(prev[j - 1] + (ca != cb), prev[j] + 1, cur[j - 1] + 1)
        prev = cur
    return prev[-1]


def _timed_chars(segs: Sequence[TimedText], norm: Callable[[str], str]) -> list[tuple[float, str]]:
    """The characters of a source's segments with an estimated time each (spread evenly over the segment)."""
    out: list[tuple[float, str]] = []
    for s in sorted(segs, key=lambda x: (float(x["start_s"]), float(x["end_s"]))):
        text = norm(str(s.get("text", "")))
        a, z = float(s["start_s"]), float(s["end_s"])
        step = (z - a) / max(1, len(text))
        out.extend((a + (i + 0.5) * step, ch) for i, ch in enumerate(text))
    return out


def evidence_over(sources: Mapping[str, Sequence[TimedText]], boundary_s: float, lo_s: float, hi_s: float,
                  norm: Callable[[str], str] = fold) -> dict[str, str]:
    """Each whole-clip source's words in [lo_s, hi_s], for the sources that have words on both sides of the boundary."""
    out: dict[str, str] = {}
    for name, segs in sources.items():
        chars = [(t, ch) for t, ch in _timed_chars(segs, norm) if lo_s <= t <= hi_s]
        if any(t < boundary_s for t, _ in chars) and any(t > boundary_s for t, _ in chars):
            out[name] = "".join(ch for _, ch in chars)
    return out


def overlap_k(left: str, right: str, *, boundary_s: float, lo_s: float, hi_s: float,
              sources: Mapping[str, Sequence[TimedText]], norm: Callable[[str], str] = fold,
              min_overlap: int = MIN_OVERLAP) -> tuple[int, OverlapInfo]:
    """How many characters at the start of `right` repeat the end of `left` as one speech written twice (0: none).

    `left` covers up to the boundary, `right` from it; [lo_s, hi_s] is the span of both.  Returns (k, info) where info
    names the candidates, the sources used and each candidate's distance to them."""
    a, b = norm(left), norm(right)
    cands = [k for k in range(min(len(a), len(b)), min_overlap - 1, -1) if a[-k:] == b[:k]]
    if not cands:
        return 0, {}
    ev = evidence_over(sources, boundary_s, lo_s, hi_s, norm)
    info: OverlapInfo = {"candidates": cands, "sources": sorted(ev)}
    if not ev:
        return 0, cast(OverlapInfo, dict(info, k=0, reason="no whole-clip reading on both sides of the boundary"))
    costs = {k: sum(edit_distance(a + b[k:], text) for text in ev.values()) for k in [0, *cands]}
    # A recogniser may miss a stutter in one reading.  Its edit distance must not turn two separately timed
    # occurrences of the overlap into one: flattening the source discards exactly that positive evidence.
    repeated = [k for k in cands if any(
        any(a[-k:] in norm(str(s.get("text", ""))) and lo_s <= float(s["start_s"])
            and float(s["end_s"]) <= boundary_s for s in segs)
        and any(a[-k:] in norm(str(s.get("text", ""))) and boundary_s <= float(s["start_s"])
                and float(s["end_s"]) <= hi_s for s in segs)
        for segs in sources.values())]
    eligible = [k for k in [0, *cands] if k not in repeated]
    k = min(eligible, key=lambda kk: (costs[kk], kk))      # a tie keeps both copies
    return k, cast(OverlapInfo, dict(info, k=k, costs=costs, timed_repetitions=repeated))


def _span(item: TimeSpan) -> tuple[float, float]:
    return float(item["start_s"]), float(item["end_s"])


def join_items(items: Sequence[TimedText], sources: Mapping[str, Sequence[TimedText]], *,
               norm: Callable[[str], str] = fold) -> tuple[list[TimedText], list[OverlapInfo]]:
    """Timed items ({start_s, end_s, text}, e.g. truth labels written per listening window) in time order with each
    repeat at a join counted once: the k characters chosen by overlap_k are removed from the earlier item.  Texts are
    returned in `norm` form.  Returns (items, log of the joins with a candidate)."""
    ordered = sorted(items, key=lambda t: (float(t["start_s"]), float(t["end_s"]), str(t.get("id", ""))))
    texts = [norm(str(t["text"])) for t in ordered]
    out = [cast("TimedText", dict(t, text=x)) for t, x in zip(ordered, texts, strict=True)]
    log: list[OverlapInfo] = []
    for i in range(len(ordered) - 1):
        (a0, a1), (b0, b1) = _span(ordered[i]), _span(ordered[i + 1])
        k, info = overlap_k(texts[i], texts[i + 1], boundary_s=(a1 + b0) / 2, lo_s=min(a0, b0), hi_s=max(a1, b1),
                            sources=sources, norm=norm)
        if info:
            log.append(cast(OverlapInfo, dict(info, prev=ordered[i].get("id"), next=ordered[i + 1].get("id"),
                                             deleted=texts[i][len(texts[i]) - k:] if k else "")))
        if k:
            out[i]["text"] = out[i]["text"][:len(out[i]["text"]) - k]
    return out, log


def _drop_prefix(raw: str, k: int, norm: Callable[[str], str]) -> str:
    """`raw` without the characters that make up the first k characters of norm(raw)."""
    for index in range(len(raw) + 1):
        if len(norm(raw[:index])) >= k:
            return raw[index:]
    return ""


def _drop_suffix(raw: str, k: int, norm: Callable[[str], str]) -> str:
    for index in range(len(raw), -1, -1):
        if len(norm(raw[index:])) >= k:
            return raw[:index]
    return ""


def trim_reading(text: str, cue: TimeSpan, prev: TimedText | None, nxt: TimedText | None,
                 sources: Mapping[str, Sequence[TimedText]]) -> tuple[str, list[OverlapInfo]]:
    """A reading of one sentence (cue: {start_s, end_s}) without the words that belong to its neighbours (prev / nxt:
    {start_s, end_s, text} or None): a leading part that repeats the previous sentence's end, and a trailing part that
    repeats the next sentence's start, each only when overlap_k decides it is one speech written twice.  Returns the
    reading and a log of the decisions with a candidate."""
    raw = text.strip()
    log: list[OverlapInfo] = []
    c0, c1 = _span(cue)
    for side, neighbour in (("prev", prev), ("next", nxt)):
        if neighbour is None:
            continue
        n0, n1 = _span(neighbour)
        # The neighbour has two text lanes, not one concatenated utterance.  The same evidence-based join
        # decision applies to each; ties / missing evidence / an actual repetition still keep the words.
        for field in ("text", "other_speaker_text"):
            text = str(neighbour.get(field) or "")
            if not raw or not text:
                continue
            left, right = (text, raw) if side == "prev" else (raw, text)
            boundary = (n1 + c0) / 2 if side == "prev" else (c1 + n0) / 2
            lo, hi = (n0, c1) if side == "prev" else (c0, n1)
            k, info = overlap_k(left, right, boundary_s=boundary, lo_s=lo, hi_s=hi, sources=sources)
            if info:
                log.append(cast(OverlapInfo, dict(info, side=side, field=field)))
            if k:
                raw = (_drop_prefix(raw, k, fold) if side == "prev" else _drop_suffix(raw, k, fold)).strip()
                if not fold(raw):
                    raw = ""
    return raw, log
