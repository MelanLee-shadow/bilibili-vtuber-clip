"""Repair exceptional cue times only where an independent timed transcript supports the same words."""
from __future__ import annotations

from difflib import SequenceMatcher
from typing import Any

from src.autoslice.subtitle_validation import read_speed_error

from .playbook_vote import letters

# Some recognisers' neighbouring sentence boundaries disagree by a frame or two.
BOUNDARY_TOL_S = 0.15
MIN_ANCHOR = 3


def _candidate(cue: dict, source: list[dict], low: float, high: float) -> tuple[float, float, dict] | None:
    """Require ordered text anchors inside and outside the cue, and at least half the final text matched.

    Each outside anchor belongs to a whole timed ASR word/segment; timestamps are never interpolated from letters.
    Evidence crossing another subtitle by more than BOUNDARY_TOL_S is not assigned to this cue.
    """
    start, end = float(cue['start_s']), float(cue['end_s'])
    target = letters(str(cue['text']))[0].lower()
    pieces = [s for s in source if float(s['end_s']) > low and float(s['start_s']) < high
              and float(s['start_s']) >= low - BOUNDARY_TOL_S
              and float(s['end_s']) <= high + BOUNDARY_TOL_S and letters(str(s['text']))[0]]
    heard, owners = '', []
    for index, item in enumerate(pieces):
        text = letters(str(item['text']))[0].lower()
        heard += text
        owners.extend([index] * len(text))
    matched = 0
    anchors: dict[str, list[tuple[int, int, float, float, str]]] = {'before': [], 'inside': [], 'after': []}
    for match in SequenceMatcher(None, target, heard, autojunk=False).get_matching_blocks():
        matched += match.size
        # A word can be a single Han character. Join consecutive matched words, but never interpolate their times.
        run: list[tuple[int, int, int]] = []
        last_side = ''
        for offset in range(match.size + 1):
            owner = owners[match.b + offset] if offset < match.size else None
            if owner is None:
                side = ''
            elif float(pieces[owner]['end_s']) <= start + BOUNDARY_TOL_S:
                side = 'before'
            elif float(pieces[owner]['start_s']) >= end - BOUNDARY_TOL_S:
                side = 'after'
            else:
                side = 'inside'
            if side != last_side and run:
                if len(run) >= MIN_ANCHOR:
                    first, last = run[0], run[-1]
                    group = [pieces[k] for k in dict.fromkeys(owner for _, owner, _ in run)]
                    segment_start = owners.index(first[1])
                    segment_end = len(owners) - 1 - owners[::-1].index(last[1])
                    word = target[first[0]:last[0] + 1]
                    unique = target.count(word) == 1 and heard.count(word) == 1
                    at_edge = (last_side == 'inside' or
                               (last_side == 'before' and first[2] <= segment_start + 1) or
                               (last_side == 'after' and last[2] >= segment_end - 1))
                    if at_edge and (last_side == 'inside' or unique):
                        anchors[last_side].append((first[0], last[0], float(group[0]['start_s']),
                                                   float(group[-1]['end_s']), ''.join(str(p['text']) for p in group)))
                run = []
            if owner is not None:
                run.append((match.a + offset, owner, match.b + offset))
            last_side = side
    if matched * 2 < len(target) or not anchors['inside']:
        return None
    before = [a for a in anchors['before'] if a[0] <= 1]
    after = [a for a in anchors['after'] if a[1] >= len(target) - 2]
    new_start = max(low, min(a[2] for a in before)) if before else start
    new_end = min(high, max(a[3] for a in after)) if after else end
    new_start, new_end = min(start, new_start), max(end, new_end)
    if new_start == start and new_end == end:
        return None
    return new_start, new_end, {'matched_characters': matched, 'target_characters': len(target),
                                'anchors': [{'start_s': a[2], 'end_s': a[3], 'text': a[4]}
                                            for a in before + after]}


def repair(cues: list[dict], sources: list[tuple[str, list[dict]]], duration: float) -> tuple[list[dict], list[dict]]:
    """Extend high-rate cues to matched ASR boundaries, inside the available gap; keep all text and neighbours."""
    out = [dict(c) for c in cues]
    repairs: list[dict[str, Any]] = []
    for i, cue in enumerate(out):
        start, end = float(cue['start_s']), float(cue['end_s'])
        if not read_speed_error(str(cue['text']), round(start * 1000), round(end * 1000)):
            continue
        low = float(out[i - 1]['end_s']) if i else 0.
        high = float(out[i + 1]['start_s']) if i + 1 < len(out) else duration
        for name, source in sources:
            found = _candidate(cue, source, low, high)
            if found is None:
                continue
            new_start, new_end, evidence = found
            cue['start_s'], cue['end_s'] = new_start, new_end
            if cue.get('segments'):
                segments = [dict(s) for s in cue['segments']]
                segments[0]['start_s'], segments[-1]['end_s'] = new_start, new_end
                cue['segments'] = segments
            repairs.append({'id': cue.get('id'), 'text': cue['text'], 'old': [start, end],
                            'new': [new_start, new_end], 'source': name, **evidence})
            break
    return out, repairs
