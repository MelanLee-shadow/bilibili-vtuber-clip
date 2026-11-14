"""Mechanical evidence for the ordinary single-speaker step 3; never decides text."""
from __future__ import annotations

from collections import Counter
from difflib import SequenceMatcher
import math
import re

from . import name_match
from .recorder_events import evidence_texts
from .srt_text import _norm_text


def danmaku_evidence(ctx, duration: float) -> tuple[list[dict], list[dict]]:
    """(rows, unknown segments): the chat of the whole live session on the cut clock (step3_evidence.danmaku_timeline).
    A segment that cannot be read is reported, not guessed."""
    from .step3_evidence import danmaku_timeline

    return danmaku_timeline(ctx, duration)


def danmaku_rows(ctx, duration: float) -> list[dict]:
    return danmaku_evidence(ctx, duration)[0]


def slots(left: str, right: str) -> list[dict]:
    a, b = _norm_text(left), _norm_text(right)
    return [{'kind': tag, 'bcut': a[i:j], 'jianying': b[k:l],
             'bcut_range': [i, j], 'jianying_range': [k, l]}
            for tag, i, j, k, l in SequenceMatcher(None, a, b, autojunk=False).get_opcodes()]


def read_run(written: str, heard: str) -> int:
    """Longest stretch of consecutive syllables that a written text and a heard sentence share (near-sounds count)."""
    a, b = name_match.sound_key(_norm_text(written)), name_match.sound_key(_norm_text(heard))
    if not a or not b:
        return 0
    m = SequenceMatcher(None, a, b, autojunk=False).find_longest_match()
    return m.size if len(set(a[m.a:m.a + m.size])) >= 3 else 0   # one syllable said over and over is not a text


READ_RUN = 4   # syllables in a row: scattered shared characters are what any two sentences on one topic have

def readout_matches(cue: dict, readings: list[str], rows: list[dict]) -> list[dict]:
    hits = []
    for row in rows:
        sent = float(row['t_s'])
        before = math.inf if row.get('type') else 90   # the streamer reads missed SCs long after they were sent
        if not cue['start_s'] - before <= sent <= cue['end_s'] + 30:
            continue
        old = sent < cue['start_s'] - 90   # only a paid record reaches here: every earlier one of the session is a
        for text in evidence_texts(row):   # candidate, so a few shared characters no longer say it is being read
            norm = _norm_text(text)
            if len(norm) < 2:
                continue
            for reading in readings:
                spoken = _norm_text(reading)
                common = len(set(norm) & set(spoken))
                sounds = name_match.sound_key(norm)
                width = len(sounds) if sounds else 0   # syllables, so a Latin suffix in a name does not widen it
                phonetic = bool(width >= 2 and any(name_match.sound_key(spoken[i:i + width]) == sounds
                                               for i in range(len(spoken) - width + 1)))
                loose = common >= 3 and not old
                if norm in spoken or loose or phonetic or (old and read_run(text, reading) >= READ_RUN):
                    hits.append({'sent_s': sent, 'text': text, 'type': row.get('type', 'danmaku'),
                                 'sender': row.get('sender'), 'shared_characters': common,
                                 'pinyin_match': phonetic,
                                 'relation': 'readout_candidate' if sent <= cue['end_s'] else 'viewer_echo'})
                    break
    return hits


def senders_read_next(cues: list[dict], hits_by_cue: dict[int, list[dict]], readings_by_cue: dict[int, list[str]],
                      *, lead_s: float = 5.0) -> dict[int, list[dict]]:
    """A paid message is thanked by its sender's name right before, or as, its body is read.  For every paid record
    whose BODY is read out (READ_RUN syllables in a row are in some cue), the cue where that reading starts and the
    cues ending within `lead_s` before it get the record's sender as a name candidate - also where the spoken name
    was heard too badly to match by sound."""
    first: dict[tuple, dict] = {}
    for cue in cues:
        for hit in hits_by_cue.get(cue['id'], ()):
            if hit['type'] == 'danmaku' or hit['relation'] != 'readout_candidate' or not hit.get('sender') \
                    or hit['text'] == hit['sender']:
                continue
            if any(read_run(hit['text'], t) >= READ_RUN for t in readings_by_cue.get(cue['id'], ())):
                first.setdefault((hit['sent_s'], hit['sender']), {'cue': cue, 'hit': hit})
    found: dict[int, list[dict]] = {}
    for (sent, sender), at in first.items():
        start = at['cue']['start_s']
        for cue in cues:
            if cue['id'] == at['cue']['id'] or (cue['end_s'] <= start and start - cue['end_s'] <= lead_s):
                found.setdefault(cue['id'], []).append({'sender': sender, 'type': at['hit']['type'], 'sent_s': sent,
                                                        'body_read_from_cue': at['cue']['id']})
    return found


_HAN_RUN = re.compile(r'[\u4e00-\u9fff]+')


def misheard_heads(pairs: list[dict]) -> dict[str, list[dict]]:
    """The first two characters of every known mishearing of three or more characters -> what it stands for."""
    heads: dict[str, list[dict]] = {}
    for pair in pairs:
        han = ''.join(_HAN_RUN.findall(str(pair['misheard'])))
        if len(han) >= 3:
            row = {'standard': pair['standard'], 'known_mishearing': pair['misheard']}
            if row not in heads.setdefault(han[:2], []):
                heads[han[:2]].append(row)
    return heads


def begun_mishearings(readings_by_cue: dict[int, list[str]], heads: dict[str, list[dict]]) -> dict[int, list[dict]]:
    """A name heard badly is often written as a known mishearing only in part: some recognisers write its first
    syllables and scatter after them, the others write something else.  A cue gets a candidate when some of its
    recognitions write the beginning of a known mishearing and at least one does not - unless the same two characters
    stand somewhere in the clip where every recognition writes them (then they are an ordinary word all of them hear)."""
    ordinary, contested = set(), {}
    for cue, readings in readings_by_cue.items():
        texts = [_norm_text(r) for r in readings if r]
        if len(texts) < 2:
            continue
        for head in heads:
            wrote = [head in t for t in texts]
            if all(wrote):
                ordinary.add(head)
            elif any(wrote):
                contested.setdefault(cue, []).append(head)
    return {cue: [dict(row, heard=head) for head in found if head not in ordinary for row in heads[head]]
            for cue, found in contested.items() if any(head not in ordinary for head in found)}


def said_in_chat_before(word: str, cue: dict, rows: list[dict], *, within_s: float = 90.0) -> dict:
    """{'chat_just_before': [...]} when viewers wrote `word` in the chat within `within_s` before the cue ends (she is
    probably answering them), else {}.  At most the two latest."""
    key = _norm_text(word).casefold()
    found = [{'sent_s': float(r['t_s']), 'text': r['text']} for r in rows
             if cue['end_s'] - within_s <= float(r['t_s']) <= cue['end_s'] and key and key in _norm_text(r['text']).casefold()]
    return {'chat_just_before': found[-2:]} if found else {}


def written_by_one(text: str, others: list[str]) -> list[str]:
    """The stretches inside `text` at whose place none of the other recognisers wrote anything, where at least two of
    them did write the rest of the sentence.  Measured on the training clips, character by character: of 3914
    characters every recogniser has a counterpart for, 2 are absent from the owner's final; of the characters only
    one recogniser wrote inside a sentence the others also wrote, more than half are absent, and the ones the owner
    kept are bare interjections.  A whole cue only one recogniser wrote is not reported: those are coughs and laughs
    the agent already tells from words, and on a clip of her watching a video the owner kept two such short lines."""
    mine = _norm_text(text)
    wrote = [o for o in (_norm_text(other) for other in others) if o]
    if len(wrote) < 2 or not mine:
        return []
    alone = [True] * len(mine)
    for other in wrote:
        for tag, i1, i2, _j1, _j2 in SequenceMatcher(None, mine, other, autojunk=False).get_opcodes():
            if tag != 'delete':
                alone[i1:i2] = [False] * (i2 - i1)
    found = [mine[m.start():m.end()] for m in re.finditer('1+', ''.join('1' if x else '0' for x in alone))]
    return [] if found == [mine] else found


def spam_matches(cue: dict, readings: list[str], rows: list[dict]) -> list[dict]:
    nearby = [r for r in rows if not r.get('type')
              and cue['start_s'] - 15 <= r['t_s'] <= cue['end_s'] + 15]
    counts = Counter(_norm_text(r['text']) for r in nearby)
    hits = []
    for text, count in counts.items():
        if count < 3 or len(text) < 2:
            continue
        # A repeated short word inside one spam line is evidence for that word too.
        terms = {text}
        for n in range(2, len(text) // 2 + 1):
            if len(text) % n == 0 and text == text[:n] * (len(text) // n):
                terms.add(text[:n])
        for term in sorted(terms):
            key = _spam_sound_key(term)
            if not key:
                continue
            for source, reading in enumerate(readings):
                norm = _norm_text(reading)
                for i in range(len(norm) - len(term) + 1):
                    span = norm[i:i + len(term)]
                    if _spam_sound_key(span) == key:
                        hits.append({'text': term, 'spam_line': text, 'count': count, 'asr_index': source,
                                     'asr_span': span, 'pinyin': list(key),
                                     'sent_s': [r['t_s'] for r in nearby if _norm_text(r['text']) == text]})
    return hits


def _spam_sound_key(text: str) -> tuple[str, ...] | None:
    key = name_match.sound_key(text)
    return tuple(syllable[:-1] if syllable.endswith('ng') else syllable for syllable in key) if key else None


def word_times(document: dict | list) -> list[dict]:
    """Normalised free_asr_client segments use milliseconds; absent words stay absent."""
    segments = document if isinstance(document, list) else document['utterances']
    return [{'start_s': float(w['start_time']) / 1000, 'end_s': float(w['end_time']) / 1000,
             'text': str(w['label'])}
            for segment in segments for w in segment.get('words', [])]


def cue_words(cue: dict, words: list[dict]) -> dict:
    own = [w for w in words if cue['start_s'] <= (w['start_s'] + w['end_s']) / 2 < cue['end_s']]
    counts = Counter(_norm_text(w['text']) for w in own if _norm_text(w['text']))
    return {'words': own, 'repeats': [{'text': text, 'count': count,
                                     'times': [[w['start_s'], w['end_s']] for w in own
                                               if _norm_text(w['text']) == text]}
                                    for text, count in counts.items() if count > 1],
            'available': bool(words)}


def classify(left: str, right: str, names: list) -> dict:
    """Only a real acoustic difference triggers audio. Rule-only items still go to CPA."""
    a, b = _norm_text(left), _norm_text(right)
    clean = lambda s: re.sub(r'[哈呵嘻嘿咳]+|[额呃]', '', s)
    if a == b:
        return {'kind': 'rule_only', 'reasons': ['R12-R15: inspect meaning, names and text conventions']}
    if not a and not b:
        return {'kind': 'rule_only', 'reasons': ['R8: both ASR empty']}
    if clean(a) == clean(b) or not clean(a + b):
        return {'kind': 'rule_only', 'reasons': ['R13-R14: laughter/hesitation/format']}
    if a and b and name_match.raw_pinyin(a) == name_match.raw_pinyin(b) \
            and name_match.raw_pinyin(a) is not None:
        return {'kind': 'rule_only', 'reasons': ['R11: homophones decided by meaning']}
    differences = [s for s in slots(left, right) if s['kind'] != 'equal']
    known = {spelling for name, aliases in names for spelling in (name, *aliases)}
    if differences and all(s['bcut'] in known or s['jianying'] in known for s in differences):
        return {'kind': 'rule_only', 'reasons': ['R3/R14: registered name surface; respect table conditions']}
    return {'kind': 'needs_audio', 'reasons': ['R6/R7/R10: differing spoken content, omissions or repeat count'],
            'slots': differences}


def grouped_windows(cues: list[dict], doubts: list[dict], duration: float) -> list[dict]:
    by_id = {c['id']: c for c in cues}
    groups = []
    for doubt in doubts:
        if doubt['audio_class']['kind'] != 'needs_audio':
            continue
        cue = by_id[doubt['cue_id']]
        lo, hi = max(0., cue['start_s'] - 1.5), min(duration, cue['end_s'] + 1.5)
        trigger = {'doubt_id': doubt['id'], 'cue_id': cue['id'], **doubt['audio_class']}
        if groups and lo <= groups[-1]['audio'][1] + 1 and hi - groups[-1]['audio'][0] <= 11:
            group = groups[-1]
            group['audio'][1] = hi
            group['core'][1] = cue['end_s']
            group['cue_ids'].append(cue['id'])
            group['triggers'].append(trigger)
        else:
            groups.append({'win': f'PB{len(groups):03d}', 'audio': [lo, hi],
                           'core': [cue['start_s'], cue['end_s']], 'cue_ids': [cue['id']],
                           'tempo': 1., 'triggers': [trigger]})
    return groups


def left_open(record: dict | None) -> bool:
    if record is None or record.get('status') != 'OK' or not record.get('lines'):
        return True
    return any(re.search(r'[（(]\s*[?？]\s*[)）]|没听清|听不清|不确定|UNCERTAIN',
                         ln if isinstance(ln, str) else str(ln.get('text', '')), re.I)
               for ln in record['lines'])
