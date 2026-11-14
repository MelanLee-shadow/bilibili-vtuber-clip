"""Project CPA's sentence partitions onto observed ASR word times."""
from __future__ import annotations

import json
from collections.abc import Collection, Sequence
from difflib import SequenceMatcher
from typing import TypedDict, cast

from . import text_overlap
from . import doubt_policy, name_match
from .srt_text import SrtCue, TimedText


class SpellingRow(TypedDict):
    canonical: str
    variants: list[str]
    condition: str
    source: str


class Part(TypedDict, total=False):
    text: str
    first_word: int
    last_word: int
    first_cue: int
    last_cue: int


class Answer(TypedDict, total=False):
    parts: list[Part]


class PartitionCue(SrtCue, total=False):
    _word_span: bool


class Segment(TypedDict):
    """One source sentence's share of a merged line: its time and how many visible characters of the line it carries."""
    start_s: float
    end_s: float
    chars: int


class MergedTiming(TypedDict):
    start_s: float
    end_s: float
    segments: list[Segment]


def spelling_rows() -> list[SpellingRow]:
    """One runtime table, projected without old case-specific authority quotations."""
    rows = json.loads(name_match.PROPER_NOUNS.read_text(encoding='utf-8'))
    return [SpellingRow(canonical=row['canonical'], variants=row['variants'],
                        condition=row['condition'], source=row['source']) for row in rows]


def merged_timing(bcut: Sequence[SrtCue], final: Sequence[doubt_policy.FinalDoubt],
                  skip: Collection[int] = ()) -> dict[int, MergedTiming]:
    """Time follows the words.  A kept line whose final text carries the words of the dropped source sentences next
    to it (merged by the agent without `parts`) covers those sentences' time; its `segments` give each source
    sentence's time and share of the line's visible characters, so a later split cuts where the sources do.  Keyed
    by the line's own cue id; a line that absorbed nothing is not in the result and keeps its own source time.

    The kept lines' final texts and all source texts (kept and dropped, in order) are aligned once as character
    streams (punctuation, spaces and case removed by doubt_policy.norm_text).  A dropped source sentence belongs to
    the line holding most of its matched characters, provided those are at least half of the sentence's content
    characters (doubt_policy.sim_key: fillers such as 嗯/呃 do not count; a sentence of fillers only counts them all).
    Half is the line: a common single character (的, 是) matched by chance is far less than half of a sentence, while
    merged text carries nearly all of it.  A line extends from its own source sentence through the dropped sentences
    next to it up to the farthest one it owns; dropped sentences in between that it does not own lie inside the span.
    It never reaches another kept line's own sentence, and a dropped sentence no line owns (really deleted) is never
    an end of a span.  A line none of whose characters match its own source sentence (rewritten) keeps that sentence's
    time.  `skip`: cue ids whose time is set elsewhere (lines with `parts`); they still own their words.
    """
    cues = list(bcut)
    by_cue = {d['cue_id']: d for d in final}
    texts = [str(by_cue[c['id']].get('final_text') or c['text']) for c in cues]
    kept = [i for i, c in enumerate(cues) if not doubt_policy.is_dropped(by_cue[c['id']])]
    source = [ch for c in cues for ch in text_overlap.key(c['text'])]
    source_cue = [i for i, c in enumerate(cues) for _ in text_overlap.key(c['text'])]
    target = [ch for i in kept for ch in text_overlap.key(texts[i])]
    target_line = [i for i in kept for _ in text_overlap.key(texts[i])]
    target_start: dict[int, int] = {}
    for j, line in enumerate(target_line):
        target_start.setdefault(line, j)
    heard: list[int | None] = [None] * len(target)     # the source sentence each target character matched
    held: dict[int, dict[int, int]] = {}               # source sentence -> line -> characters matched
    for block in SequenceMatcher(None, source, target, autojunk=False).get_matching_blocks():
        for k in range(block.size):
            heard[block.b + k] = source_cue[block.a + k]
            shares = held.setdefault(source_cue[block.a + k], {})
            shares[target_line[block.b + k]] = shares.get(target_line[block.b + k], 0) + 1
    kept_set = set(kept)
    owner: dict[int, int] = {}
    for i, cue in enumerate(cues):
        if i in kept_set or i not in held:
            continue
        need = len(doubt_policy.sim_key(cue['text'])) or len(text_overlap.key(cue['text']))
        line, count = max(held[i].items(), key=lambda lc: (lc[1], -lc[0]))
        if 2 * count >= need:
            owner[i] = line
    out: dict[int, MergedTiming] = {}
    for line in kept:
        cue = cues[line]
        if cue['id'] in skip or line not in held.get(line, {}):
            continue
        lo = hi = line
        for k in range(line - 1, -1, -1):
            if k in kept_set:
                break
            lo = k if owner.get(k) == line else lo
        for k in range(line + 1, len(cues)):
            if k in kept_set:
                break
            hi = k if owner.get(k) == line else hi
        if lo == hi:
            continue
        # a visible character belongs to the source sentence of the matched character at or before it (leading ones
        # to the first); a source sentence of the span with no character of its own is part of the run before it
        labels: list[int] = []
        at, last = target_start[line], lo
        for ch in texts[line]:
            width = len(text_overlap.key(ch))
            found = heard[at] if width else None
            at += width
            if found is not None and last <= found <= hi:
                last = found
            if not ch.isspace():
                labels.append(last)
        firsts = sorted(set(labels))
        segments: list[Segment] = []
        for n, first in enumerate(firsts):
            begin = lo if n == 0 else first
            end = hi if n + 1 == len(firsts) else firsts[n + 1] - 1
            segments.append(Segment(start_s=cues[begin]['start_s'], end_s=cues[end]['end_s'],
                                    chars=labels.count(first)))
        out[cue['id']] = MergedTiming(start_s=cues[lo]['start_s'], end_s=cues[hi]['end_s'], segments=segments)
    return out


def partitioned_cues(bcut: list[SrtCue], final: list[doubt_policy.FinalDoubt],
                     answers: dict[str, Answer], words: list[TimedText],
                     duration: float) -> list[SrtCue] | None:
    """Use only word spans explicitly selected by CPA; no interpolated split times."""
    if not any('parts' in row for row in answers.values()):
        return None
    by_cue = {d['cue_id']: d for d in final}
    source_cues = {c['id']: c for c in bcut}
    merged = merged_timing(bcut, final, {c['id'] for c in bcut if 'parts' in answers[by_cue[c['id']]['id']]})
    out: list[PartitionCue] = []
    for cue in bcut:
        d = by_cue[cue['id']]
        row = answers[d['id']]
        if doubt_policy.is_dropped(d):
            continue
        if 'parts' not in row:
            out.append(cast(PartitionCue, {'id': cue['id'], 'start_s': cue['start_s'], 'end_s': cue['end_s'],
                                           'text': d.get('final_text') or cue['text'], '_word_span': False,
                                           **merged.get(cue['id'], {})}))
            continue
        if not isinstance(row['parts'], list):
            raise ValueError('CPA parts must be a list')
        for part in row['parts']:
            if 'first_word' in part:
                first, last = part['first_word'], part['last_word']
                if not isinstance(first, int) or not isinstance(last, int) or not 0 <= first <= last < len(words):
                    raise ValueError('CPA word span is outside the supplied source words')
                start, end = words[first]['start_s'], words[last]['end_s']
            else:
                first, last = part['first_cue'], part['last_cue']
                if first not in source_cues or last not in source_cues:
                    raise ValueError('CPA cue span is outside the supplied source cues')
                start, end = source_cues[first]['start_s'], source_cues[last]['end_s']
            text = str(part['text']).strip()
            if not text or not 0 <= start < end <= duration:
                raise ValueError('CPA selected an invalid source word span')
            out.append({'id': cue['id'], 'start_s': start, 'end_s': end, 'text': text, '_word_span': True})
    # Original cue timestamps can overlap the adjacent selected word by a few
    # milliseconds. Keep the selected word boundary, trimming the old cue's
    # padding; when both are selected, use the later word's observed start.
    for left, right in zip(out, out[1:]):
        if left['end_s'] <= right['start_s']:
            continue
        if left['_word_span'] and not right['_word_span']:
            starts = [w['start_s'] for w in words if left['end_s'] <= w['start_s'] < right['end_s']]
            right['start_s'] = min(starts) if starts else left['end_s']
        elif right['_word_span'] and not left['_word_span']:
            ends = [w['end_s'] for w in words if left['start_s'] < w['end_s'] <= right['start_s']]
            left['end_s'] = max(ends) if ends else right['start_s']
        elif left['_word_span'] and right['_word_span']:
            left['end_s'] = right['start_s']
        else:
            continue
        if left['start_s'] >= left['end_s'] or right['start_s'] >= right['end_s']:
            raise ValueError('CPA selected word spans in an impossible order')
    for cue in out:
        del cue['_word_span']
    return cast(list[SrtCue], out)
