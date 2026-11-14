"""Deterministic fixes applied at generation, so the review lane has nothing to find that the pipeline could have fixed.

- Subtitle shape (FMR A2/A3): cues are split until each fits the lane's visual limits (lines, characters per line,
  seconds per static cue).  The limits are read from the audit's own limits module, not copied.
- Canon (FMR A8): the repo's surface canon (channel profile) is applied with its own rule functions.
- Cut boundaries (FMR A7): each cut point is snapped to a nearby silence found by ffmpeg silencedetect, with the
  same noise threshold and minimum length the lane uses.  If no silence is found the point stays and that is recorded.

Pure functions here; the ffmpeg call itself is made by the cut step on OCI3.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from itertools import chain
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, TypedDict, cast

from src.autoslice import subtitle_display_limits
from src.autoslice.subtitle_validation import _LEADING_PUNCT_RE
from src.autoslice.surface_canon import (
    canonicalize_caption_display_surfaces,
    canonicalize_expected_value_surfaces,
    canonicalize_hard_meme_surfaces,
)

from .subtitle_shape import RELAXED, legal_breaks

if TYPE_CHECKING:
    from .srt_text import TimedText
    from .subtitle_track import TrackCue


class SubtitleAudit(Protocol):
    DEFAULT_MAX_VISUAL_LINES: int
    DEFAULT_MAX_VISUAL_LINE_CHARS: int
    LONG_STATIC_CUE_SECONDS: float


class CanonHit(TypedDict):
    surface: str
    canonical: str
    authority: str
    count: int


class CanonEvent(CanonHit):
    cue_id: int | None


NumberHit = TypedDict("NumberHit", {"kind": str, "from": str, "to": str})


class NumberEvent(NumberHit):
    cue_id: int | None


class RepeatEvent(TypedDict):
    cue_id: int | None
    kind: str


class LoneChar(TypedDict):
    start_s: float
    end_s: float
    text: str
    action: str
    why: str


class TimedWord(TypedDict):
    abs_start_s: float
    abs_end_s: float
    text: str


class NormalizeLog(TypedDict):
    canon_replacements: list[CanonEvent]
    extra_cues_from_split: int
    adjacent_repeats: list[RepeatEvent]
    lone_chars: list[LoneChar]
    numerals: list[NumberEvent]

REPO_ROOT = Path(__file__).resolve().parents[3]
SNAP_RADIUS_S = 0.5          # the cut point may move at most this far
SNAP_PROBE_S = 1.0           # silencedetect window on each side of the point
SILENCE_NOISE_DB = -38       # as the lane's A7 check
SILENCE_MIN_S = 0.1          # as the lane's A7 check


def _audit() -> SubtitleAudit:
    return cast(SubtitleAudit, subtitle_display_limits)


def text_len(text: str) -> int:
    return sum(1 for ch in text if not ch.isspace())


# ----- canon (FMR A8) -----------------------------------------------------------------------
_LAUGH_TOKEN = re.compile(r"[（(]\s*笑\s*[)）]")
_LAUGH_SOUND = re.compile(r"[哈呵嘿嘻]{2,}")   # 哈哈哈哈 and kin: laughter written out as a sound (U112)


def strip_laughter(text: str) -> str:
    """No laughter in the subtitle (U112, SUB-26): （笑） tokens and laughter written as sounds (哈哈哈) are removed;
    punctuation left dangling at the start of the remaining text is dropped."""
    text = _LAUGH_SOUND.sub("", _LAUGH_TOKEN.sub("", text))
    text = re.sub(r"^[\s。，、！？!?,.…]+", "", text)
    return re.sub(r"\s{2,}", " ", text).strip()


def canon_text(text: str) -> tuple[str, list[CanonHit]]:
    """Apply the repo's surface canon with its own rule functions; returns the text and every replacement.
    The expected-value canon (names and works, TASK-5 item 4) runs too, so 写法 is unified and 念法 is not touched."""
    replacements: list[CanonHit] = []
    for fn in (canonicalize_hard_meme_surfaces, canonicalize_caption_display_surfaces,
               canonicalize_expected_value_surfaces):
        text, hits = fn(text)
        replacements.extend(cast(list[CanonHit], hits))
    return text, replacements






_CN_DIGIT = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_CN_NUM = "零〇一二两三四五六七八九十百千万"
# measure units: one of these after a single 一 is a measured quantity (1分钟, 1千克); 天/年/周/个月 only after a number
# of two or more (一天/一年 are usually narrative: 有一天)
_UNITS_ANY = ("个小时", "分钟", "小时", "秒钟", "秒", "毫秒", "千克", "公斤", "克", "斤", "公里", "千米", "厘米", "毫米",
              "米", "毫升", "升", "块钱", "元", "岁")
_UNITS_COUNT = ("个月", "天", "周", "星期", "年")
_QUANTITY = re.compile(rf"(?<![{_CN_NUM}\d.])((?:千(?![克米])|[零〇一二两三四五六七八九十百万])+(?:点[{_CN_NUM[:-4]}]+)?)"
                       rf"({'|'.join(_UNITS_ANY + _UNITS_COUNT)})")
_WEEKDAY = re.compile(r"(?:星期|礼拜|周)[一二三四五六日天]")
# A numeral before a unit is a measured amount only when it counts.  It does not count when it orders or picks out one
# item (第二天, 下一秒, 每一秒, 同一年: an ordinal or a determiner + 一) or when it estimates (三四天, 两三年, 十五六天,
# 八九十岁: two adjacent digits one apart are a 概数, GB/T 15835): those stay in characters (U136: fixed phrases and ordinals).
_ORDINAL_MARK = "第"
_DETERMINERS = "上下每这那前后同某哪"


def _counts_amount(seg: str, start: int, num: str) -> bool:
    """Whether the numeral `num` found at seg[start:] counts the unit after it (see _ORDINAL_MARK, _DETERMINERS)."""
    before = seg[start - 1] if start else ""
    if before == _ORDINAL_MARK or (num == "一" and before != "" and before in _DETERMINERS):
        return False
    if all(ch in _CN_DIGIT for ch in num) and len(num) >= 3:   # 二零一九: a number read digit by digit
        return True
    return not any(a in _CN_DIGIT and b in _CN_DIGIT and _CN_DIGIT[b] == _CN_DIGIT[a] + 1 for a, b in zip(num, num[1:], strict=False))


_SCORE = re.compile(rf"(?<![{_CN_NUM}\d])零点([{_CN_NUM[:-4]}]+)(?!\s*[{_CN_NUM}])")
# a role 0/1: after 是/当/做/纯/个 or in 一还是零, and not followed by a classifier or a word (是一个, 当一下, 做一道)
_ROLE_AFTER = re.compile(r"(?:(?<=[是当做纯])|(?<=一个))([一零])(?=[吗啊呀吧呢嘛哦，。！？、,.!?…\s]|还是|来着|的话|和|跟|或|$)")
_ROLE_PAIR = re.compile(r"([一零])(还是|或者|和|跟)([一零])(?![个种位下次些点])")
_YURI_TOPIC = re.compile(r"攻受|做攻|做受|是攻|是受|百合|(?<![\d.])[1一]\s*(?:还是|或者)\s*[0零](?![\d.])"
                         r"|(?<![\d.])[0零]\s*(?:还是|或者)\s*[1一](?![\d.])|[是当做纯](?<![\d.])[01](?![\d.%])")


def _cn_int(text: str) -> int | None:
    """A Chinese integer of up to 万 (五, 十二, 两百三十, 一千零五, 三万); None for anything else."""
    if not text or any(ch not in _CN_NUM for ch in text):
        return None
    if all(ch in _CN_DIGIT for ch in text) and len(text) > 1:      # 二零二六: digit by digit
        return int("".join(str(_CN_DIGIT[ch]) for ch in text))
    total, section, digit = 0, 0, None
    for ch in text:
        if ch in _CN_DIGIT:
            digit = _CN_DIGIT[ch]
        elif ch in "十百千":
            section += (1 if digit is None else digit) * {"十": 10, "百": 100, "千": 1000}[ch]
            digit = None
        elif ch == "万":
            total += (section + (digit or 0)) * 10000
            section, digit = 0, None
    return total + section + (digit or 0)


def _cn_digits(text: str) -> str | None:
    return "".join(str(_CN_DIGIT[ch]) for ch in text) if text and all(ch in _CN_DIGIT for ch in text) else None


def yuri_topic(texts: Sequence[str]) -> bool:
    """Whether the subtitles talk about 0/1 as yuri roles: the subtitles' own words (攻受, 百合, 1还是0, 当1 ...), never
    the truth."""
    return any(_YURI_TOPIC.search(t) for t in texts)


def numerals(text: str, *, yuri: bool = False) -> tuple[str, list[NumberHit]]:
    """U136: the one spelling rule for numbers in subtitles.  Returns the text and each replacement.  Text inside 《》
    (a title) is left as it is."""
    hits: list[NumberHit] = []
    parts = re.split(r"(《[^》]*》)", text)

    def fix(seg: str) -> str:
        def quantity(m: re.Match[str]) -> str:
            num, unit = m.group(1), m.group(2)
            if not _counts_amount(seg, m.start(), num):
                return m.group(0)
            if "点" in num:
                whole, frac = num.split("点", 1)
                a, b = _cn_int(whole), _cn_digits(frac)
                value = None if a is None or b is None else f"{a}.{b}"
            else:
                n = _cn_int(num)
                value = None if n is None or (unit in _UNITS_COUNT and n < 2) else str(n)
            if value is None:
                return m.group(0)
            hits.append({"kind": "quantity", "from": m.group(0), "to": value + unit})
            return value + unit

        def score(m: re.Match[str]) -> str:
            frac = _cn_digits(m.group(1))
            if frac is None:
                return m.group(0)
            hits.append({"kind": "score", "from": m.group(0), "to": "0." + frac})
            return "0." + frac

        seg = _QUANTITY.sub(quantity, seg)
        seg = _SCORE.sub(score, seg)
        if yuri:
            def role(m: re.Match[str]) -> str:
                hits.append({"kind": "role", "from": m.group(1), "to": "1" if m.group(1) == "一" else "0"})
                return "1" if m.group(1) == "一" else "0"

            def pair(m: re.Match[str]) -> str:
                a, b = ("1" if m.group(1) == "一" else "0"), ("1" if m.group(3) == "一" else "0")
                hits.append({"kind": "role", "from": m.group(0), "to": a + m.group(2) + b})
                return a + m.group(2) + b
            seg = _ROLE_PAIR.sub(pair, seg)
            seg = _ROLE_AFTER.sub(role, seg)
        return seg
    def outside_weekdays(seg: str) -> str:
        pieces: list[str] = []
        start = 0
        for match in _WEEKDAY.finditer(seg):
            # A number outside a preceding weekday belongs to a duration (三周五天), not a weekday (周五).
            # An immediately adjacent preceding weekday has already consumed its own last numeral.
            if match.start() > start and seg[match.start() - 1] in _CN_NUM + "0123456789.":
                continue
            pieces.extend((fix(seg[start:match.start()]), match.group()))
            start = match.end()
        pieces.append(fix(seg[start:]))
        return "".join(pieces)

    return "".join(p if p.startswith("《") else outside_weekdays(p) for p in parts), hits


def numeral_cues(cues: Sequence[TrackCue]) -> tuple[list[TrackCue], list[NumberHit]]:
    """numerals() over a cue list, the yuri reading decided once from the whole list's own words (yuri_topic)."""
    yuri = yuri_topic([str(c["text"]) for c in cues])
    out: list[TrackCue] = []
    hits: list[NumberHit] = []
    for cue in cues:
        text, h = numerals(str(cue["text"]), yuri=yuri)
        hits.extend(h)
        out.append(cast("TrackCue", dict(cue, text=text)))
    return out, hits


# ----- shape (FMR A2/A3) --------------------------------------------------------------------
_BREAK_CHARS = "，。！？、,.!?…；;：: "


def _split_point(text: str, target: int, anchors: frozenset[int] = frozenset(), level: int = 0) -> int:
    """Index near `target` at a word boundary, preferring a source-sentence boundary (`anchors`: indices of `text`),
    then a nearby pause.  `level`: how much of the word protection is given up (subtitle_shape.legal_breaks)."""
    def allowed(i: int) -> bool:
        following = text[i:].lstrip()
        return bool(text[:i].strip()) and bool(following) and not _LEADING_PUNCT_RE.match(following)

    legal = [i for i in legal_breaks(text, level) if allowed(i)]
    if not legal:
        raise ValueError("subtitle has no legal word boundary")
    pauses = [i for i in legal if text[i - 1] in _BREAK_CHARS and abs(i - target) <= 4]
    return min([i for i in legal if i in anchors] or pauses or legal, key=lambda i: (abs(i - target), i))


def _chunks(text: str, parts: int, anchors: frozenset[int] = frozenset(), level: int = 0) -> list[str]:
    """Split text into `parts` pieces of roughly equal visible length.  `anchors`: visible offsets of `text` where a
    source sentence begins; a cut there is preferred."""
    pieces: list[str] = []
    rest = text
    for left in range(parts, 1, -1):
        target = math.ceil(text_len(rest) / left)
        done = text_len(text) - text_len(rest)
        here = frozenset(_index_for_visible(rest, a - done) for a in anchors if a > done)
        cut = _split_point(rest, _index_for_visible(rest, target), here, level)
        pieces.append(rest[:cut].strip())
        rest = rest[cut:].strip()
    pieces.append(rest)
    return [p for p in pieces if p]


def _index_for_visible(text: str, visible: int) -> int:
    count = 0
    for index, ch in enumerate(text):
        if not ch.isspace():
            count += 1
            if count >= visible:
                return index + 1
    return len(text)


def wrap_lines(text: str, max_lines: int, max_chars: int, level: int = 0) -> list[str]:
    """Lines for one piece, each within max_chars visible characters.  Caller guarantees the piece fits."""
    text = text.strip()
    if len(text) <= max_chars:
        return [text]
    lines = _chunks(text, max_lines, level=level)
    return lines


def _fit_pieces(text: str, need: int, min_need: int, max_need: int, audit: SubtitleAudit,
                anchors: frozenset[int] = frozenset()) -> tuple[list[str], list[list[str]]]:
    # Try the duration-driven count first, as before; if fragments merge back into an invalid long line,
    # also try the smaller feasible counts before declaring the text unsplittable.
    # A break that keeps every name and foreign word whole first; when the text has none (a run longer than a line), the
    # protection is given up level by level rather than the clip (a viewer's hyphenated Latin name of 18
    # letters after two characters stopped a clip of the first unattended round).
    for level, need in ((lv, n) for lv in RELAXED for n in chain(range(need, max_need + 1), range(min_need, need))):
        try:
            pieces = _merge_fragments([text] if need == 1 else _chunks(text, need, anchors, level))
            wrapped = [wrap_lines(piece, audit.DEFAULT_MAX_VISUAL_LINES, audit.DEFAULT_MAX_VISUAL_LINE_CHARS, level)
                       for piece in pieces]
        except ValueError:
            continue
        if all(len(lines) <= audit.DEFAULT_MAX_VISUAL_LINES
               and all(len(line) <= audit.DEFAULT_MAX_VISUAL_LINE_CHARS for line in lines)
               and not any(_LEADING_PUNCT_RE.match(line) for line in lines[1:])
               for lines in wrapped):
            return pieces, wrapped
    raise ValueError("cannot split subtitle within visual limits without starting a line with punctuation")


def _source_clock(text: str, start_s: float, end_s: float,
                  segments: Sequence[Mapping[str, float]] | None) -> list[tuple[int, int, float, float]] | None:
    """(first visible offset, end offset, start_s, end_s) per source sentence of a merged line, or None when the
    segments do not describe this text and span (the line was changed after they were made)."""
    if not segments or sum(int(seg["chars"]) for seg in segments) != text_len(text):
        return None
    if abs(float(segments[0]["start_s"]) - start_s) > 1e-6 or abs(float(segments[-1]["end_s"]) - end_s) > 1e-6:
        return None
    clock, offset = [], 0
    for seg in segments:
        clock.append((offset, offset + int(seg["chars"]), float(seg["start_s"]), float(seg["end_s"])))
        offset += int(seg["chars"])
    return clock


def _clock_time(clock: Sequence[tuple[int, int, float, float]], offset: int, *, ending: bool) -> float:
    """Time of a cut at visible `offset`: the source sentence's own edge on a sentence boundary, else the proportion
    inside the one source sentence the cut falls in."""
    for first, end, begin_s, finish_s in clock:
        if (first < offset <= end) if ending else (first <= offset < end):
            if offset == (end if ending else first):
                return finish_s if ending else begin_s
            return begin_s + (finish_s - begin_s) * (offset - first) / (end - first)
    raise ValueError("cut outside the merged line")


def split_cue(text: str, start_s: float, end_s: float, audit: SubtitleAudit,
              segments: Sequence[Mapping[str, float]] | None = None) -> list[TimedText]:
    """One or more cues covering the same span, each within the lane's visual limits.  `segments`: the source
    sentences a merged line was made of ({start_s, end_s, chars} in order); cuts then prefer their boundaries and take
    the sources' own times there, so no cut time is interpolated across two sources."""
    cap_chars = audit.DEFAULT_MAX_VISUAL_LINES * audit.DEFAULT_MAX_VISUAL_LINE_CHARS
    duration = end_s - start_s
    min_need = max(math.ceil(text_len(text) / cap_chars), 1)
    max_need = max(1, text_len(text))
    need = min(max(min_need, math.ceil(duration / audit.LONG_STATIC_CUE_SECONDS)), max_need)
    clock = _source_clock(text, start_s, end_s, segments)
    anchors = frozenset(first for first, _end, _a, _b in clock[1:]) if clock else frozenset()
    try:
        pieces, wrapped = _fit_pieces(text, need, min_need, max_need, audit, anchors)
    except ValueError:
        if not anchors:
            raise
        pieces, wrapped = _fit_pieces(text, need, min_need, max_need, audit)
    if clock:
        out: list[TimedText] = []
        offset = 0
        for piece, lines in zip(pieces, wrapped, strict=True):
            width = text_len(piece)
            out.append({"start_s": _clock_time(clock, offset, ending=False),
                        "end_s": _clock_time(clock, offset + width, ending=True), "text": "\n".join(lines)})
            offset += width
        return out
    total = sum(max(1, text_len(p)) for p in pieces)
    out = []
    cursor = start_s
    for index, (piece, lines) in enumerate(zip(pieces, wrapped, strict=True)):
        share = max(1, text_len(piece)) / total
        end = end_s if index == len(pieces) - 1 else cursor + duration * share
        out.append({"start_s": cursor, "end_s": end, "text": "\n".join(lines)})
        cursor = end
    return out


def shape_decided_cues(cues: Sequence[TimedText]) -> list[TimedText]:
    """Apply visual shape to decided text without changing its words or outside times."""
    audit = _audit()
    shaped: list[TimedText] = []
    for cue in cues:
        text = cue['text']
        if '\n' not in text and len(text) <= audit.DEFAULT_MAX_VISUAL_LINE_CHARS:
            shaped.append(cue)
            continue
        pieces = split_cue(' '.join(text.split()), cue['start_s'], cue['end_s'], audit,
                           cast('dict[str, Sequence[Mapping[str, float]]]', cue).get('segments'))
        if ''.join(text.split()) != ''.join(''.join(piece['text'].split()) for piece in pieces):
            raise ValueError('subtitle shaping changed decided text')
        shaped.extend(pieces)
    return shaped


def _merge_fragments(pieces: list[str]) -> list[str]:
    """A split never cuts one sentence down to a one-character piece (any character: the pieces are parts of one
    utterance, one speaker, touching).  Lone characters between cues are join_lone_chars'."""
    merged = list(pieces)
    index = 0
    while index < len(merged) and len(merged) > 1:
        if text_len(merged[index]) <= 1:
            if index > 0:
                merged[index - 1] = merged[index - 1] + merged[index]
                del merged[index]
                index -= 1
                continue
            merged[index + 1] = merged[index] + merged[index + 1]
            del merged[index]
            continue
        index += 1
    return merged


def _norm(text: str) -> str:
    return re.sub(r"[\W_]+", "", text).lower()


# A reading's words that belong to a neighbouring sentence are trimmed by overlap_join.trim_reading (U159: decided by
# the whole-clip readings, so a real repeat is kept); the old longest-overlap trim that lived here is gone.


def words_in_cue(words: Sequence[TimedWord], start_s: float, end_s: float) -> str:
    """Word-level timing, when a listening or ASR output has it: keep only the words whose middle lies in the cue."""
    kept = [str(w["text"]) for w in words
            if start_s - 0.5 <= (float(w["abs_start_s"]) + float(w["abs_end_s"])) / 2 <= end_s + 0.5]
    return "".join(kept).strip()


# The ASR join tolerance: two cues closer than this (or overlapping) may be one speech recorded twice (the same window
# the doubt scan uses, doubt_policy / stage_impl ±0.3 s).
ADJACENT_JOIN_S = 0.3


def _adjacent_overlap(prev_norm: str, cur_norm: str, *, min_len: int = 3) -> tuple[str, int]:
    """(kind, k) of a repeat at the join of two sentences, or ("", 0).  tail: the end of the previous sentence is the
    start of this one; head: both start with the same words.  Both at least `min_len` normalised characters."""
    for k in range(min(len(prev_norm), len(cur_norm)), min_len - 1, -1):
        if prev_norm[-k:] == cur_norm[:k]:
            return "tail", k
    for k in range(min(len(prev_norm), len(cur_norm)), min_len - 1, -1):
        if prev_norm[:k] == cur_norm[:k]:
            return "head", k
    return "", 0


def join_repeat(prev: TimedText | None, cue: TimedText) -> str:
    """A doubt reason, never an edit (TASK-9): the cue starts with the words that end (tail) or start (head) the
    previous cue and the two touch or overlap.  That is either one speech recorded by two cues or the speaker repeating
    or restarting (我们这最 / 我们最近由于…); only the adjudicator, reading both cues in context, can tell which.
    Returns the reason text, or "" when there is no such repeat."""
    if prev is None or float(cue["start_s"]) - float(prev["end_s"]) > ADJACENT_JOIN_S:
        return ""
    kind, k = _adjacent_overlap(_norm(str(prev["text"])), _norm(str(cue["text"])))
    if not kind:
        return ""
    where = "结尾" if kind == "tail" else "开头"
    return f"本句开头 {k} 个字与上一句{where}相同且两句时间相接：可能是同一段话被识别了两次，也可能是说话人重复或改口"


def release_min_cue_s() -> float:
    """The release check's shortest cue (subtitle_validation.validate_srt_text min_cue_ms), read, not copied."""
    from src.autoslice.subtitle_validation import validate_srt_text

    return cast(float, (validate_srt_text.__kwdefaults__ or {}).get("min_cue_ms", 300)) / 1000


def release_contiguous_gap_s() -> float:
    """The largest gap across which the repo's producer joins a release-rejected cue into its neighbour
    (cue_split_hygiene.merge_release_grade_cues contiguous_gap_ms), read, not copied.  A wider gap is a silence: a
    lone character is never joined across it (80-package-delivery: 不能为凑字数跨静音并句)."""
    from src.autoslice.cue_split_hygiene import merge_release_grade_cues

    return cast(float, (merge_release_grade_cues.__kwdefaults__ or {}).get("contiguous_gap_ms", 150)) / 1000


def lone_char_positions(cues: Sequence[TrackCue]) -> list[int]:
    """0-based positions of the cues the release check rejects as an isolated single character
    (subtitle_validation SRT_SINGLE_CJK_CHARACTER).  The check itself decides: its closed set of standalone utterances
    (interjections, 有, 噗) and its adjacent name-echo exemption are inherited, never restated here."""
    from src.autoslice.subtitle_validation import validate_srt_text

    from .subtitle_track import format_blocks

    verdict = validate_srt_text(format_blocks(cues, labels=False))
    return sorted({int(e["block"]) - 1 for e in verdict["errors"] if e.get("code") == "SRT_SINGLE_CJK_CHARACTER"})


def _join_text(left: str, right: str) -> str:
    """Two cues read as one line: no comma added where either side already has punctuation at the join (the joining
    of merge_release_grade_cues)."""
    if re.search(r"[，。！？!?,、…~—]$", left) or re.search(r"^[，。！？!?,、…~—]", right):
        return left + right
    return left + "，" + right


def join_lone_chars(cues: Sequence[TrackCue], *, one_speaker: bool = False
                    ) -> tuple[list[TrackCue], list[LoneChar]]:
    """No isolated content character on its own line (80-package-delivery, lidousha-srt-release-policy.v1): a cue the
 release check rejects as a lone character joins the neighbouring cue of the same speaker that touches it (gap at
 most release_contiguous_gap_s; the nearer side, the next one on a tie, as the repo's producer does). Never across
 a silence and never into another speaker's line; nothing is added. A lone character that no such neighbour can
 take is not subtitled (公开规则11-02, U138: 「对」「那」能分就分出来，不能分出来可以不进入字幕，不计入打分; an exception for
 isolated single characters only, decided by the release check itself, never widened to other cues). Two cues are
 the same speaker when both carry the same `speaker` label, or when the caller says the session has one speaker
 (`one_speaker`); unlabelled cues of a linked session are left for the step that labels them (step 4), so nothing
 is dropped before the speaker is known. The joined cue is shaped again (split_cue) so it stays within the lane's
 visual limits. Returns the cues and one row per lone character:
 {"start_s", "end_s", "text", "action": "joined_prev" | "joined_next" | "dropped" | "deferred", "why"}. The scorer
 applies the same function to the truth (score.lone_char_rule), so a dropped character counts on neither side."""
    from .subtitle_track import plain_text

    gap_max = release_contiguous_gap_s()
    out = [cast("TrackCue", dict(c)) for c in cues]
    rows: list[LoneChar] = []
    settled: set[tuple[float, str]] = set()

    def same(a: TrackCue, b: TrackCue) -> bool:
        return one_speaker or (bool(a.get("speaker")) and a.get("speaker") == b.get("speaker"))

    while True:
        target: tuple[int, tuple[float, str, int] | None] | None = None
        for pos in lone_char_positions(out):
            cue = out[pos]
            key = (round(float(cue["start_s"]), 3), str(cue["text"]))
            if key in settled:
                continue
            sides = []
            for side, nb in (("prev", pos - 1), ("next", pos + 1)):
                if not 0 <= nb < len(out):
                    continue
                gap = (float(cue["start_s"]) - float(out[nb]["end_s"]) if side == "prev"
                       else float(out[nb]["start_s"]) - float(cue["end_s"]))
                if -1e-6 <= gap <= gap_max + 1e-6 and same(cue, out[nb]):
                    sides.append((round(max(0.0, gap), 6), side, nb))
            if not sides:
                if not one_speaker and not cue.get("speaker"):
                    rows.append({"start_s": float(cue["start_s"]), "end_s": float(cue["end_s"]),
                                 "text": plain_text(str(cue["text"])), "action": "deferred",
                                 "why": "说话人未知（联动场次，待第 4 步标出说话人后再并或不出字幕）"})
                    settled.add(key)
                    continue
                target = (pos, None)
                break
            target = (pos, min(sides))
            break
        if target is None:
            return out, rows
        if target[1] is None:   # no same-speaker neighbour touches it: not subtitled (U138)
            pos = target[0]
            cue = out.pop(pos)
            rows.append({"start_s": float(cue["start_s"]), "end_s": float(cue["end_s"]),
                         "text": plain_text(str(cue["text"])), "action": "dropped",
                         "why": f"两侧没有同一说话人、间隔不超过 {gap_max:.2f} 秒的句子（不跨静音、不并进别人的话），不出字幕"})
            continue
        pos, (_gap, side, nb) = target[0], target[1]
        first, second = (out[nb], out[pos]) if side == "prev" else (out[pos], out[nb])
        text = _join_text(plain_text(str(first["text"])), plain_text(str(second["text"])))
        start, end = float(first["start_s"]), float(second["end_s"])
        rows.append({"start_s": float(out[pos]["start_s"]), "end_s": float(out[pos]["end_s"]),
                     "text": plain_text(str(out[pos]["text"])),
                     "action": f"joined_{side}", "why": f"并入同一说话人紧挨着的句子：{text}"})
        extra = {k: v for k, v in out[nb].items() if k not in {"start_s", "end_s", "text"}}
        pieces = [cast("TrackCue", dict(extra, **piece)) for piece in split_cue(text, start, end, _audit())]
        lo, hi = min(pos, nb), max(pos, nb)
        out[lo:hi + 1] = pieces


def release_shape(cues: Sequence[TrackCue], *, one_speaker: bool = False
                  ) -> tuple[list[TrackCue], list[LoneChar]]:
    """The release shape of a cue list, the one function every subtitle writer passes through (step 3's assembly, the
    import, step 4's labels, a review fix): numeral_cues (U136 number spelling), _sequence (no overlap, no cue under
    the release minimum), then join_lone_chars (no isolated content character).  `one_speaker`: see join_lone_chars.
    Returns (cues, join_lone_chars' rows: what was joined or not subtitled, for the writer to record)."""
    return join_lone_chars(_sequence(numeral_cues(cues)[0]), one_speaker=one_speaker)


def _sequence(cues: Sequence[TrackCue]) -> list[TrackCue]:
    """Release subtitles never overlap in time (subtitle_validation SRT_CUE_OVERLAP, the upload lane's contract) and
    no cue is shorter than the release minimum (SRT_CUE_TOO_SHORT).  Two lines that overlap (two people talking at
    once, U110) become two consecutive lines in start order, both kept: the boundary is the later line's own start, or
    later when the earlier line has not yet had its share of the shared time (by text length); each side keeps at
    least the release minimum.  No line ends later than the latest end the producer wrote (the media end is not known
    here; SRT_END_AFTER_MEDIA).  Text and every other key (speaker) are untouched."""
    min_s = release_min_cue_s()
    out = [cast("TrackCue", dict(c)) for c in sorted(cues, key=lambda c: (float(c["start_s"]), float(c["end_s"])))]
    cap = max((float(c["end_s"]) for c in out), default=0.0)
    for index in range(1, len(out)):
        a, b = out[index - 1], out[index]
        a0, a1, b0, b1 = float(a["start_s"]), float(a["end_s"]), float(b["start_s"]), float(b["end_s"])
        if b0 < a1 - 1e-9:
            la, lb = max(1, len(_norm(str(a["text"])))), max(1, len(_norm(str(b["text"]))))
            share = a0 + (min(a1, b1) - a0) * la / (la + lb)
            boundary = min(max(b0, share, a0 + min_s), cap - min_s)
            a["end_s"], b["start_s"] = boundary, boundary
            b["end_s"] = min(max(b1, boundary + min_s), cap)
    for index, cue in enumerate(out):
        need = min_s - (float(cue["end_s"]) - float(cue["start_s"]))
        if need <= 1e-9:
            continue
        nxt = out[index + 1] if index + 1 < len(out) else None
        prev = out[index - 1] if index > 0 else None
        # the shortfall comes from the free time after the cue, then from the next cue (kept at the minimum), then
        # the free time before it, then the previous cue
        free_after = (float(nxt["start_s"]) - float(cue["end_s"])) if nxt else cap - float(cue["end_s"])
        take = max(0.0, min(need, free_after))
        cue["end_s"] = float(cue["end_s"]) + take
        need -= take
        if need > 1e-9 and nxt is not None:
            give = max(0.0, min(need, float(nxt["end_s"]) - float(nxt["start_s"]) - min_s))
            cue["end_s"] = float(cue["end_s"]) + give
            nxt["start_s"] = float(nxt["start_s"]) + give
            need -= give
        if need > 1e-9:
            free_before = float(cue["start_s"]) - (float(prev["end_s"]) if prev else 0.0)
            take = max(0.0, min(need, free_before))
            cue["start_s"] = float(cue["start_s"]) - take
            need -= take
        if need > 1e-9 and prev is not None:
            give = max(0.0, min(need, float(prev["end_s"]) - float(prev["start_s"]) - min_s))
            prev["end_s"] = float(prev["end_s"]) - give
            cue["start_s"] = float(cue["start_s"]) - give
    return out


def shape_cues(cues: Sequence[TimedText]) -> list[TrackCue]:
    """Canon, number spelling, split and wrap only: the shape of new text for some cues of a list (a review fix), before
    the whole list takes its release shape (release_shape).  Never joins or drops a lone character: that needs the
    neighbours."""
    return normalize_cues(cues, release_shape=False)[0]


def normalize_cues(cues: Sequence[TimedText], *, one_speaker: bool = False, release_shape: bool = True
                   ) -> tuple[list[TrackCue], NormalizeLog]:
    """Canon every cue, split and wrap to the lane's visual limits, then the release shape (overlapping lines become
    consecutive, a lone content character joins its same-speaker neighbour: release_shape; `one_speaker` as there).
    The words are the producer's (adjudicator, reviewer fix, import): a repeat at a join is listed in the log
    (`adjacent_repeats`) and never removed here (TASK-9: CPA's choice stands; the old removal deleted real restarts
    such as 我们最近由于… after 我们这最).  Keys other than time and text (speaker) go to every piece.  Returns the
    cues and a log (`lone_chars`: join_lone_chars' rows)."""
    audit = _audit()
    out: list[TrackCue] = []
    replaced: list[CanonEvent] = []
    dups: list[RepeatEvent] = []
    split = 0
    prev: TimedText | None = None
    yuri = yuri_topic([canon_text(str(c["text"]))[0] for c in cues])   # one reading for the whole list (U136)
    number_hits: list[NumberEvent] = []
    for cue in cues:
        text, hits = canon_text(str(cue["text"]))
        text, nums = numerals(text, yuri=yuri)
        number_hits.extend(cast(NumberEvent, dict(h, cue_id=cue.get("id"))) for h in nums)
        replaced.extend(cast(CanonEvent, dict(h, cue_id=cue.get("id"))) for h in hits)
        plain = " ".join(text.split())
        if not _norm(plain):
            continue
        here: TimedText = {"start_s": float(cue["start_s"]), "end_s": float(cue["end_s"]), "text": plain}
        if join_repeat(prev, here):
            dups.append({"cue_id": cast("int | None", cue.get("id")),
                         "kind": _adjacent_overlap(_norm(cast("TimedText", prev)["text"]), _norm(plain))[0]})
        prev = here
        extra = {k: v for k, v in cue.items() if k not in {"id", "start_s", "end_s", "text"}}
        pieces = split_cue(plain, here["start_s"], here["end_s"], audit)
        if len(pieces) > 1:
            split += len(pieces) - 1
        out.extend(cast("TrackCue", dict(extra, **piece)) for piece in pieces)
    shaped, lone = join_lone_chars(_sequence(out), one_speaker=one_speaker) if release_shape else (out, [])
    return shaped, {"canon_replacements": replaced, "extra_cues_from_split": split, "adjacent_repeats": dups,
                    "lone_chars": lone, "numerals": number_hits}


# ----- silence snapping (FMR A7) ------------------------------------------------------------
def silence_intervals(log_text: str, window_start_s: float) -> list[tuple[float, float]]:
    """Absolute (start, end) of each silence reported by silencedetect; end is inf when unterminated."""
    starts = [float(m) for m in re.findall(r"silence_start: (-?[\d.]+)", log_text)]
    ends = [float(m) for m in re.findall(r"silence_end: (-?[\d.]+)", log_text)]
    out: list[tuple[float, float]] = []
    for index, start in enumerate(starts):
        end = ends[index] if index < len(ends) else math.inf
        out.append((max(0.0, start + window_start_s), end + window_start_s if math.isfinite(end) else math.inf))
    return out


def snap_point(point_s: float, intervals: Sequence[tuple[float, float]], *, role: str) -> float | None:
    """Closest moment to `point_s`, within SNAP_RADIUS_S, that the lane's silence test accepts.

    role "start": the cut begins inside a silence that lasts at least SILENCE_MIN_S from that moment.
    role "end": the cut ends inside a silence that has lasted at least SILENCE_MIN_S before that moment.
    Returns None when no silence qualifies (the point then stays as it was).
    """
    lo, hi = point_s - SNAP_RADIUS_S, point_s + SNAP_RADIUS_S
    best: float | None = None
    for start, end in intervals:
        if role == "start":
            a, b = max(start, lo), min(end - SILENCE_MIN_S, hi)
        else:
            a, b = max(start + SILENCE_MIN_S, lo), min(end, hi)
        if a <= b + 1e-9:
            candidate = min(max(point_s, a), b)
            if best is None or abs(candidate - point_s) < abs(best - point_s):
                best = candidate
    return best
