"""Shared SRT parsing and production ASS layout/rendering helpers."""

from __future__ import annotations

from pathlib import Path
from functools import lru_cache
import re
from typing import Sequence

from src.autoslice.review_evidence import SourceCue
from src.autoslice.term_lexicon import load_discovered_term_lexicon, normalize_text


def _parse_srt(path: Path, *, source_offset_ms: int = 0) -> list[SourceCue]:
    raw = path.read_text(encoding="utf-8-sig").replace("\r\n", "\n").replace("\r", "\n")
    lexicon = load_discovered_term_lexicon(path)
    cues: list[SourceCue] = []
    for index, block in enumerate(raw.split("\n\n"), start=1):
        lines = [line for line in block.splitlines() if line.strip()]
        if len(lines) < 2:
            continue
        if "-->" in lines[0]:
            timing = lines[0]
            text_lines = lines[1:]
        else:
            timing = lines[1]
            text_lines = lines[2:]
        if "-->" not in timing:
            continue
        start, end = [part.strip() for part in timing.split("-->", 1)]
        text = normalize_text("\n".join(text_lines).strip(), lexicon=lexicon)
        kind = (
            "singing"
            if any(marker in text for marker in ("《", "啦", "アイドル", "言って"))
            else "speech"
        )
        cues.append(
            SourceCue(
                cue_id=f"u_{index:06d}",
                source_start_ms=source_offset_ms + _parse_time_ms(start),
                source_end_ms=source_offset_ms + _parse_time_ms(end),
                text=text,
                language="zh",
                kind=kind,
                confidence=1.0,
            )
        )
    return cues


def _parse_time_ms(value: str) -> int:
    hhmmss, millis = value.replace(",", ".").split(".", 1)
    hours, minutes, seconds = [int(part) for part in hhmmss.split(":")]
    return ((hours * 60 + minutes) * 60 + seconds) * 1000 + int(millis[:3].ljust(3, "0"))


def _write_sapphire_ass_from_srt(srt_path: Path, ass_path: Path) -> None:
    cues = _parse_srt(srt_path)
    event_rows = []
    rows = [(cue.source_start_ms, cue.source_end_ms, cue.text) for cue in cues]
    for _, sub_start_ms, sub_end_ms, sub_text in _layout_cue_sequence_for_display(rows):
        text = _ass_escape_text(sub_text)
        event_rows.append(
            f"Dialogue: 0,{_format_ass_time(sub_start_ms)},{_format_ass_time(sub_end_ms)},Default,,0,0,0,,{text}"
        )
    ass_path.parent.mkdir(parents=True, exist_ok=True)
    # Typography retains the approved sapphire72 metrics emitted by
    # .agent/skills/song-lyrics-timeline-aligner/scripts/align_timed_lyrics.py
    # write_ass at --play-res 1920x1080: Fontsize 72 belongs to the 1080p
    # PlayRes with margins 60,60,40, Shadow 2, BackColour &H70000000
    ass_path.write_text(
        "\n".join(
            [
                "[Script Info]",
                "ScriptType: v4.00+",
                "PlayResX: 1920",
                "PlayResY: 1080",
                "WrapStyle: 2",
                "ScaledBorderAndShadow: yes",
                "",
                "[V4+ Styles]",
                "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
                "Style: Default,Microsoft YaHei,72,&H00FFFFFF,&H000000FF,&H00BA520F,&H70000000,0,0,0,0,100,100,0,0,1,3,2,2,60,60,40,1",
                "",
                "[Events]",
                "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
                *event_rows,
                "",
            ]
        ),
        encoding="utf-8",
    )


def _format_ass_time(ms: int) -> str:
    # round (not floor) to centiseconds — flooring made every cue start up to
    # 9ms early, which compounds with other sources of "subtitles feel early"
    total_centiseconds = max(0, (int(ms) + 5) // 10)
    centiseconds = total_centiseconds % 100
    total_seconds = total_centiseconds // 100
    seconds = total_seconds % 60
    total_minutes = total_seconds // 60
    minutes = total_minutes % 60
    hours = total_minutes // 60
    return f"{hours}:{minutes:02d}:{seconds:02d}.{centiseconds:02d}"


ASS_MAX_CHARS_PER_LINE = 28
ASS_MAX_VISUAL_LINES = 2
ASS_MIN_SUBCUE_MS = 700
# 24 full-width glyphs * 72 px = 1728px, inside the unchanged 1800px content
# width (1920 - 2*60 margins), including outline. Do not let libass re-wrap
# a planned word at arbitrary glyph boundaries. 28 remains the audit ceiling.
ASS_SAFE_DISPLAY_CHARS = 24
ASS_MAX_JOIN_GAP_MS = 120
ASS_MAX_JOIN_DURATION_MS = 6000

_TEXT_BREAK_STRONG = "。！？…；;!?"
_TEXT_BREAK_WEAK = "，、,: ：~〜 "


@lru_cache(maxsize=1)
def _layout_protected_terms() -> frozenset[str]:
    # Existing profile glossary/referent authority, used only for display
    # segmentation. It never replaces a word or certifies an acoustic reading.
    from src.autoslice.term_authority import protected_terms

    return protected_terms()


def _unsafe_word_cuts(text: str) -> set[int]:
    blocked: set[int] = set()
    for term in _layout_protected_terms():
        for match in re.finditer(re.escape(term), text, re.IGNORECASE):
            blocked.update(range(match.start() + 1, match.end()))
    # Latin words, acronyms and numbers should not be split into glyphs either.
    for match in re.finditer(r"[A-Za-z0-9]+(?:[._'’-][A-Za-z0-9]+)*", text):
        blocked.update(range(match.start() + 1, match.end()))
    return blocked


def _term_crosses_cue_boundary(left: str, right: str) -> bool:
    seam = len(left)
    text = left + right
    return any(
        match.start() < seam < match.end()
        for term in _layout_protected_terms()
        for match in re.finditer(re.escape(term), text, re.IGNORECASE)
    )


def _small_boundary_shift(left: str, right: str) -> tuple[str, str]:
    """Move <=2 lexical characters (plus attached punctuation), never merge cues.

    The concatenated spelling is immutable.  Natural punctuation breaks ties;
    phrase length alone is not a reason to advance an entire next sentence.
    """
    text = left + right
    seam = len(left)
    if not _term_crosses_cue_boundary(left, right):
        return left, right
    blocked = _unsafe_word_cuts(text)
    closing = "，。！？、；：,.!?;:…~〜）)]】』」”’"
    punctuation = closing + "（([【『「“‘ "
    candidates = []
    for cut in range(max(1, seam - 4), min(len(text), seam + 5)):
        moved = text[min(cut, seam) : max(cut, seam)]
        lexical = sum(char not in punctuation for char in moved)
        new_left, new_right = text[:cut], text[cut:]
        if (
            cut == seam
            or cut in blocked
            or not 1 <= lexical <= 2
            or max(len(new_left), len(new_right)) > ASS_SAFE_DISPLAY_CHARS
            or not new_left.strip(punctuation)
            or not new_right.strip(punctuation)
            or new_right[0] in closing
        ):
            continue
        # Prefer the smallest lexical adjustment.  Punctuation must stay with
        # its phrase; do not carry the rest of that phrase across the cue seam.
        score = (lexical, 0 if new_left[-1] in closing else 1, abs(cut - seam))
        candidates.append((score, cut))
    if not candidates:
        return left, right
    cut = min(candidates)[1]
    return text[:cut], text[cut:]


def _layout_cue_sequence_for_display(
    cues: Sequence[tuple[int, int, str]],
    *,
    continuity_keys: Sequence[object] | None = None,
) -> list[tuple[int, int, int, str]]:
    """Source-index/start/end/display projection with bounded seam adjustments.

    Keep both events and both time intervals.  Only move a split word's small
    prefix/suffix between touching same-speaker cues. The source SRT remains
    byte-identical; deterministic long-line layout continues independently.
    """
    keys = list(continuity_keys) if continuity_keys is not None else [None] * len(cues)
    if len(keys) != len(cues):
        raise ValueError("display continuity key count differs from source cues")
    texts = [" ".join(text.replace("\r", "\n").split()) for _, _, text in cues]
    original_joined = "".join(texts)
    for index in range(len(cues) - 1):
        start, end, _ = cues[index]
        next_start, next_end, _ = cues[index + 1]
        if (
            keys[index] == keys[index + 1]
            and 0 <= next_start - end <= ASS_MAX_JOIN_GAP_MS
            and end < next_end
            and next_end - start <= ASS_MAX_JOIN_DURATION_MS
        ):
            texts[index], texts[index + 1] = _small_boundary_shift(texts[index], texts[index + 1])
    if "".join(texts) != original_joined:
        raise ValueError("display boundary adjustment changed words")
    return [
        (index, a, b, text)
        for index, ((start, end, _), visible) in enumerate(zip(cues, texts, strict=True))
        for a, b, text in _layout_cue_for_display(start, end, visible)
    ]


def _safe_long_segments(segment: str, max_chars: int) -> list[str]:
    result: list[str] = []
    while len(segment) > max_chars:
        blocked = _unsafe_word_cuts(segment)
        valid = [i for i in range(1, max_chars + 1) if i not in blocked]
        if not valid:
            raise ValueError("an indivisible subtitle word exceeds display width")
        cut = valid[-1]
        result.append(segment[:cut])
        segment = segment[cut:]
    if segment:
        result.append(segment)
    return result


def _split_text_segments(text: str) -> list[str]:
    """Natural punctuation first; word-safe length split only when necessary."""
    normalized = " ".join(text.replace("\r", "\n").split())
    blocked = _unsafe_word_cuts(normalized)
    segments: list[str] = []
    start = 0
    for index, char in enumerate(normalized):
        if char in _TEXT_BREAK_STRONG + _TEXT_BREAK_WEAK and index + 1 not in blocked:
            segments.append(normalized[start : index + 1])
            start = index + 1
    if start < len(normalized):
        segments.append(normalized[start:])
    return [
        part
        for segment in segments
        for part in _safe_long_segments(segment, ASS_SAFE_DISPLAY_CHARS)
    ]


def _pack_segments(segments: Sequence[str], max_chars: int) -> list[str]:
    chunks: list[str] = []
    current = ""
    for segment in segments:
        if current and len(current) + len(segment) > max_chars:
            chunks.append(current.rstrip())
            current = segment.lstrip()
        else:
            current += segment
    if current:
        chunks.append(current.rstrip())
    return chunks


def _layout_cue_for_display(start_ms: int, end_ms: int, text: str) -> list[tuple[int, int, str]]:
    """Prefer one complete phrase per line, with <=2 safe visual lines.

    This is presentation only. Existing long cues retain bounded sequential
    display subcues; no recognized term is cut in half to balance line lengths.
    """
    segments = _split_text_segments(text)
    if not segments:
        return []
    duration_ms = max(0, end_ms - start_ms)
    chunks = _pack_segments(segments, ASS_SAFE_DISPLAY_CHARS)
    if len(chunks) > 1 and duration_ms // len(chunks) < ASS_MIN_SUBCUE_MS:
        chunks = _pack_segments(segments, ASS_SAFE_DISPLAY_CHARS * ASS_MAX_VISUAL_LINES)
    total_chars = sum(len(chunk) for chunk in chunks) or 1
    result: list[tuple[int, int, str]] = []
    cursor_ms = start_ms
    for index, chunk in enumerate(chunks):
        chunk_end_ms = (
            end_ms
            if index == len(chunks) - 1
            else min(end_ms, cursor_ms + max(1, (duration_ms * len(chunk)) // total_chars))
        )
        display = _wrap_ass_text(chunk)
        if chunk_end_ms > cursor_ms and display:
            result.append((cursor_ms, chunk_end_ms, display))
        cursor_ms = chunk_end_ms
    return result


def _wrap_ass_text(text: str, *, max_chars: int = ASS_MAX_CHARS_PER_LINE) -> str:
    line = " ".join(text.replace("\r", "\n").split())
    max_chars = min(max_chars, ASS_SAFE_DISPLAY_CHARS)
    if len(line) <= max_chars:
        return line
    blocked = _unsafe_word_cuts(line)
    valid = [
        i
        for i in range(1, len(line))
        if i <= max_chars and len(line) - i <= max_chars and i not in blocked
    ]
    if not valid:
        raise ValueError("subtitle cannot fit two lines without splitting a protected word")
    natural = [i for i in valid if line[i - 1] in _TEXT_BREAK_STRONG + _TEXT_BREAK_WEAK]
    cut = min(natural or valid, key=lambda i: abs(i - len(line) / 2))
    return line[:cut].rstrip() + r"\N" + line[cut:].lstrip()


def _ass_escape_text(text: str) -> str:
    return text.replace("{", "（").replace("}", "）")


def _escape_ffmpeg_filter_path(path: Path | str) -> str:
    return str(path).replace("\\", "\\\\").replace("'", "\\'").replace(":", "\\:")
