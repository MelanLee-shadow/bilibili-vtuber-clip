"""Mechanical evidence for the ordinary single-speaker step 3; never decides text."""
from __future__ import annotations

from collections import Counter
from difflib import SequenceMatcher
import math
import re

from . import text_overlap
from . import name_match
from .recorder_events import evidence_texts


# what the agent is told about the hints below (playbook_step3's `hints` tool)
HINTS_NOTE = ('机械候选，可能有误，只是证据：readout_matches 是同期弹幕/SC 中可能被念出或回响的原记录'
              '（relation=readout_candidate 为句前或句内已发出）；near_names 是句中与名字表拼音相近而写法不同的名字（spellings 含她的自称和昵称）；'
              '带 chat_near 的，是句前一分半钟到句后 20 秒内有观众在弹幕里写了这个名字的某种写法：观众在回应或复述她刚说的称呼，'
              '这一处多半就是这个名字，即使同音的普通词放在句里也讲得通，也写成名字；'
              'sender_of_message_read_next 是紧接着被念出正文的那条付费留言的发送者：她通常在念正文前或同一句里叫这个名字致谢，'
              '这一带若有一段听不成词、又与该名字音节数相近或部分同音的称呼，多半就是它（平台后缀不念）；'
              '若她清楚地叫的是另一个短称呼，照她叫的写；带 thanks_slot 的，是各家在“谢谢某某的”称呼那一格写的字（heard），'
              '各家字数一致（characters）且少于发送者名字的汉字数（name_characters）：她叫的是更短的昵称或简称，不是全名——'
              '按这几个字数，写成读音最接近、并与发送者名字有共同音节的短称呼，不补成全名；'
              '带 thanks_stretch 的正相反：各家从“谢谢”到句末写的汉字（heard）都比发送者名字的汉字数（name_characters）多，'
              '她念的是全名（后面接“的”或 SC），中间被识别成“的”的字也是名字的一部分——写全名，不缩成短称呼；'
              'mishearing 是频道已知误听对；begins_like_a_known_mishearing 是有的识别在这里写出了某个已知误听写法的开头（heard），'
              '有的识别写了别的，那个误听写法（known_mishearing）代表 standard。各家在这一处互不相同，就没有哪一家的写法算'
              '“现稿”可以原样保留：在 standard 和各家写法里，取最能同时解释各家音形、音节数和这句话在说谁的那个；'
              'standard 讲得通而各家的写法放在句里讲不通时写 standard（可标低把握），不因“不足以证明”留下一家的错听；'
              '带 chat_just_before 的，是句前一分半钟内有观众在弹幕里写了 standard：她多半正在回应这条弹幕，这时写 standard；'
              'only_one_recogniser_wrote 是夹在这一句里、只有 BCUT 写了而其余识别器同一位置都没有对应文字的那几个字：'
              '其余几家把这句话写出来了却都没有它，它多半是背景声或误触发，不是这句话的一部分——去掉这几个字，'
              '不把它改写或扩写成别的词句（包括复听听出的外语台词）；只有它是主播清楚说出的实词时才保留。'
              'nearby_word_sounds_alike 是句里某个字（written）和前后几句反复写到的另一个字（nearby_word，nearby_cues 是写到它的句子）'
              '只差一点读音（同声母、同声调、韵母差一个音）：这一带正在说那件事时，她多半说的是那个字'
              '；换成它放进句子讲不通、或复听明确听到原字时照原样写。'
              '由你核对后决定，系统不会替你改字。')

def danmaku_evidence(ctx, duration: float) -> tuple[list[dict], list[dict]]:
    """(rows, unknown segments): the chat of the whole live session on the cut clock (step3_evidence.danmaku_timeline).
    A segment that cannot be read is reported, not guessed."""
    from .step3_evidence import danmaku_timeline

    return danmaku_timeline(ctx, duration)


def danmaku_rows(ctx, duration: float) -> list[dict]:
    return danmaku_evidence(ctx, duration)[0]


def slots(left: str, right: str) -> list[dict]:
    a, b = text_overlap.key(left), text_overlap.key(right)
    return [{'kind': tag, 'bcut': a[i:j], 'jianying': b[k:l],
             'bcut_range': [i, j], 'jianying_range': [k, l]}
            for tag, i, j, k, l in SequenceMatcher(None, a, b, autojunk=False).get_opcodes()]


def read_run(written: str, heard: str) -> int:
    """Longest stretch of consecutive syllables that a written text and a heard sentence share (near-sounds count)."""
    a, b = name_match.sound_key(text_overlap.key(written)), name_match.sound_key(text_overlap.key(heard))
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
            norm = text_overlap.key(text)
            if len(norm) < 2:
                continue
            for reading in readings:
                spoken = text_overlap.key(reading)
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
_THANKS_SLOT = re.compile(r'谢谢([\u4e00-\u9fff]{1,6}?)的')


def thanks_slot(wrote: dict[str, str], sender: str) -> dict:
    """What each recogniser wrote where she says the sender's name in 谢谢<name>的, when one of them heard that shape.
    The other recognisers' letters at the same places are read off an alignment that keeps the length there (they may
    have heard 谢谢 as something else: 289470, BCUT 谢谢老莎的, JianYing 先上了三个).  Reported when two or more
    recognisers give that place and all of them the same number of characters, fewer than the Chinese part of the
    sender's name: she called the sender by a shorter name, not the full one.  {} otherwise."""
    name = ''.join(_HAN_RUN.findall(str(sender)))
    texts = {k: text_overlap.key(v) for k, v in wrote.items() if v}
    for anchor_name, anchor in texts.items():
        found = _THANKS_SLOT.search(anchor)
        if not found:
            continue
        i, j = found.span(1)
        heard = {anchor_name: anchor[i:j]}
        for other, text in texts.items():
            if other == anchor_name:
                continue
            for tag, a0, a1, b0, b1 in SequenceMatcher(None, anchor, text, autojunk=False).get_opcodes():
                if a0 <= i and j <= a1 and tag in ('equal', 'replace') and a1 - a0 == b1 - b0:
                    heard[other] = text[b0 + i - a0:b0 + j - a0]
        sizes = {len(v) for v in heard.values()}
        if len(heard) >= 2 and len(sizes) == 1 and 1 < sizes.pop() < len(name):   # one character is 我的/你的, not a name
            return {'thanks_slot': {'heard': heard, 'characters': j - i, 'name_characters': len(name)}}
        return {}
    return {}


def thanks_stretch(wrote: dict[str, str], sender: str) -> dict:
    """The opposite case of thanks_slot: every recogniser that wrote 谢谢 in this cue begins what follows with the sound
    of the name's first character and writes, from there to the end of the cue, one to three Chinese characters more
    than the Chinese part of the sender's name (the name plus 的/SC).  289470: sender 我听说我听说, BCUT
    谢谢我的手机的什么枪 (8), JianYing 谢谢我手机的手机壳 (7) — she read the whole name; the 的 after three characters
    split it in the recognisers, it is not a shorter nickname.  {} when fewer than two recognisers wrote 谢谢 here."""
    name = ''.join(_HAN_RUN.findall(str(sender)))
    first = name_match.sound_key(name[:1]) if name else None
    heard = {}
    for recogniser, text in wrote.items():
        text = text_overlap.key(text or '')
        if '谢谢' in text:
            heard[recogniser] = ''.join(_HAN_RUN.findall(text[text.rindex('谢谢') + 2:]))
    if (len(name) < 2 or first is None or len(heard) < 2
            or any(not len(name) < len(v) <= len(name) + 3 or name_match.sound_key(v[0]) != first
                   for v in heard.values())):
        return {}
    return {'thanks_stretch': {'heard': heard, 'name_characters': len(name)}}




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
        texts = [text_overlap.key(r) for r in readings if r]
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
    key = text_overlap.key(word).casefold()
    found = [{'sent_s': float(r['t_s']), 'text': r['text']} for r in rows
             if cue['end_s'] - within_s <= float(r['t_s']) <= cue['end_s'] and key and key in text_overlap.key(r['text']).casefold()]
    return {'chat_just_before': found[-2:]} if found else {}


def said_in_chat_around(spellings: list[str], cue: dict, rows: list[dict], *, before_s: float = 90.0,
                        after_s: float = 20.0) -> dict:
    """{'chat_near': [...]} when viewers wrote one of a name's `spellings` in the chat from `before_s` before the cue ends
    to `after_s` after it (they answer her, or echo the name she just said), else {}.  At most the two nearest."""
    keys = [k for k in (text_overlap.key(s).casefold() for s in spellings) if k]
    found = [{'sent_s': float(r['t_s']), 'text': r['text']} for r in rows
             if cue['end_s'] - before_s <= float(r['t_s']) <= cue['end_s'] + after_s
             and any(k in text_overlap.key(r['text']).casefold() for k in keys)]
    found.sort(key=lambda r: abs(r['sent_s'] - cue['end_s']))
    return {'chat_near': sorted(found[:2], key=lambda r: r['sent_s'])} if found else {}


def written_by_one(text: str, others: list[str]) -> list[str]:
    """The stretches inside `text` at whose place none of the other recognisers wrote anything, where at least two of
    them did write the rest of the sentence.  Measured on the training clips, character by character: of 3914
    characters every recogniser has a counterpart for, 2 are absent from the owner's final; of the characters only
    one recogniser wrote inside a sentence the others also wrote, more than half are absent, and the ones the owner
    kept are bare interjections.  A whole cue only one recogniser wrote is not reported: those are coughs and laughs
    the agent already tells from words, and on a clip of her watching a video the owner kept two such short lines."""
    mine = text_overlap.key(text)
    wrote = [o for o in (text_overlap.key(other) for other in others) if o]
    if len(wrote) < 2 or not mine:
        return []
    alone = [True] * len(mine)
    for other in wrote:
        for tag, i1, i2, _j1, _j2 in SequenceMatcher(None, mine, other, autojunk=False).get_opcodes():
            if tag != 'delete':
                alone[i1:i2] = [False] * (i2 - i1)
    found = [mine[m.start():m.end()] for m in re.finditer('1+', ''.join('1' if x else '0' for x in alone))]
    return [] if found == [mine] else found


TOPIC_WINDOW_CUES = 8      # cues on each side of the sentence
TOPIC_MIN_CUES = 4         # a character written in at least this many of them is what this stretch is talking about
COMMON_CHARS = frozenset("的地得了着过是不在有我你他她它们这那就都也还很吗呢吧啊呀哦嗯么什个一说要会能可到去来上下里看想没对好把被"
                         "给让和跟与而又再才只人家大小多少")
_INITIALS = ("zh", "ch", "sh", "b", "p", "m", "f", "d", "t", "n", "l", "g", "k", "h", "j", "q", "x", "r", "z", "c", "s",
             "y", "w")


def _syllable(ch: str) -> tuple[str, str, str] | None:
    """(initial, final, tone) of one character as pypinyin reads it alone; tone 5 is the neutral tone."""
    from pypinyin import Style, pinyin

    read = pinyin(ch, style=Style.TONE3, heteronym=False)
    if not read or not read[0] or not read[0][0].isascii() or not read[0][0][:1].isalpha():
        return None
    syl = read[0][0]
    tone = syl[-1] if syl[-1].isdigit() else "5"
    syl = syl.rstrip("012345")
    initial = next((i for i in _INITIALS if syl.startswith(i)), "")
    return initial, syl[len(initial):], tone


def _one_sound_apart(a: str, b: str) -> bool:
    """Same initial (not a bare vowel), same tone, finals one letter apart: 敢 gan3 / 改 gai3."""
    sa, sb = _syllable(a), _syllable(b)
    if sa is None or sb is None or not sa[0] or sa[0] != sb[0] or sa[2] != sb[2] or sa[1] == sb[1]:
        return False
    fa, fb = sa[1], sb[1]
    if len(fa) == len(fb):
        return sum(x != y for x, y in zip(fa, fb)) == 1
    short, long_ = sorted((fa, fb), key=len)
    return len(long_) - len(short) == 1 and any(long_[:i] + long_[i + 1:] == short for i in range(len(long_)))


def nearby_near_sounds(readings_by_cue: dict[int, list[str]], order: list[int]) -> dict[int, list[dict]]:
    """Per cue: a character every recogniser's reading of it may hold that sounds one sound apart from a character the
    cues around it keep writing (TOPIC_MIN_CUES of the TOPIC_WINDOW_CUES on each side), while the sentence itself does
    not write that character.  Owner 「你敢吗你敢吗」 was 「你改嘛你改嘛」 in a stretch about changing an ID
    (改 in six cues around it) — the context should have corrected it.  On the final subtitles of 20 recent clips this
    reports 23 places, about one a clip, that one among them."""
    han = {cid: [set(re.findall(r"[\u4e00-\u9fff]", t)) for t in readings_by_cue.get(cid, [])] for cid in order}
    found: dict[int, list[dict]] = {}
    for i, cid in enumerate(order):
        around = order[max(0, i - TOPIC_WINDOW_CUES):i] + order[i + 1:i + 1 + TOPIC_WINDOW_CUES]
        where: dict[str, list[int]] = {}
        for other in around:
            for ch in set().union(*han[other]) if han[other] else ():
                where.setdefault(ch, []).append(other)
        mine = set().union(*han[cid]) if han[cid] else set()
        rows = []
        for written in sorted(mine - COMMON_CHARS):
            if len(where.get(written, ())) > 1:
                continue
            for word, cues in sorted(where.items()):
                if (len(cues) >= TOPIC_MIN_CUES and word not in COMMON_CHARS and word not in mine
                        and _one_sound_apart(written, word)):
                    rows.append({"written": written, "nearby_word": word, "nearby_cues": cues})
        if rows:
            found[cid] = rows
    return found


def spam_matches(cue: dict, readings: list[str], rows: list[dict]) -> list[dict]:
    nearby = [r for r in rows if not r.get('type')
              and cue['start_s'] - 15 <= r['t_s'] <= cue['end_s'] + 15]
    counts = Counter(text_overlap.key(r['text']) for r in nearby)
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
                norm = text_overlap.key(reading)
                for i in range(len(norm) - len(term) + 1):
                    span = norm[i:i + len(term)]
                    if _spam_sound_key(span) == key:
                        hits.append({'text': term, 'spam_line': text, 'count': count, 'asr_index': source,
                                     'asr_span': span, 'pinyin': list(key),
                                     'sent_s': [r['t_s'] for r in nearby if text_overlap.key(r['text']) == text]})
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
    counts = Counter(text_overlap.key(w['text']) for w in own if text_overlap.key(w['text']))
    return {'words': own, 'repeats': [{'text': text, 'count': count,
                                     'times': [[w['start_s'], w['end_s']] for w in own
                                               if text_overlap.key(w['text']) == text]}
                                    for text, count in counts.items() if count > 1],
            'available': bool(words)}


def classify(left: str, right: str, names: list) -> dict:
    """Only a real acoustic difference triggers audio. Rule-only items still go to CPA."""
    a, b = text_overlap.key(left), text_overlap.key(right)
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
