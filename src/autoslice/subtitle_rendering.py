"""Shared SRT parsing and production ASS layout/rendering helpers."""

from __future__ import annotations

from pathlib import Path
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
        kind = "singing" if any(marker in text for marker in ("《", "啦", "アイドル", "言って")) else "speech"
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
    for cue in cues:
        for sub_start_ms, sub_end_ms, sub_text in _layout_cue_for_display(
            cue.source_start_ms, cue.source_end_ms, cue.text
        ):
            text = _ass_escape_text(sub_text)
            event_rows.append(
                f"Dialogue: 0,{_format_ass_time(sub_start_ms)},{_format_ass_time(sub_end_ms)},Default,,0,0,0,,{text}"
            )
    ass_path.parent.mkdir(parents=True, exist_ok=True)
    # header must byte-match the approved sapphire72 spec emitted by
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
                "WrapStyle: 0",
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

_TEXT_BREAK_STRONG = "。！？…；;!?"
_TEXT_BREAK_WEAK = "，、,: ：~〜 "


def _split_text_segments(text: str) -> list[str]:
    """Split cue text into natural phrase segments at punctuation boundaries."""

    normalized = " ".join(text.replace("\r", "\n").split())
    segments: list[str] = []
    current = ""
    for char in normalized:
        current += char
        if char in _TEXT_BREAK_STRONG or char in _TEXT_BREAK_WEAK:
            if current.strip():
                segments.append(current.strip())
            current = ""
    if current.strip():
        segments.append(current.strip())
    # hard-split any single segment that alone exceeds the line limit
    result: list[str] = []
    for segment in segments:
        while len(segment) > ASS_MAX_CHARS_PER_LINE:
            result.append(segment[:ASS_MAX_CHARS_PER_LINE])
            segment = segment[ASS_MAX_CHARS_PER_LINE:]
        if segment:
            result.append(segment)
    return result or ([normalized] if normalized else [])


def _pack_segments(segments: Sequence[str], max_chars: int) -> list[str]:
    chunks: list[str] = []
    current = ""
    for segment in segments:
        if current and len(current) + len(segment) > max_chars:
            chunks.append(current)
            current = segment
        else:
            current += segment
    if current:
        chunks.append(current)
    return chunks


def _layout_cue_for_display(
    start_ms: int,
    end_ms: int,
    text: str,
) -> list[tuple[int, int, str]]:
    """Viewability contract (维护者,): at most 28 chars per visual
    line, at most 2 lines per dialogue, single line preferred.  Over-long cue
    text is split into sequential sub-cues (time allocated by text share)
    instead of stacking 3-4 lines that cover half the screen."""

    segments = _split_text_segments(text)
    if not segments:
        return []
    duration_ms = max(0, end_ms - start_ms)
    # prefer single-line chunks; fall back to 2-line chunks when the cue is too
    # short to give each single-line sub-cue a readable minimum duration
    chunks = _pack_segments(segments, ASS_MAX_CHARS_PER_LINE)
    if len(chunks) > 1 and duration_ms // len(chunks) < ASS_MIN_SUBCUE_MS:
        chunks = _pack_segments(segments, ASS_MAX_CHARS_PER_LINE * ASS_MAX_VISUAL_LINES)
    total_chars = sum(len(chunk) for chunk in chunks) or 1
    result: list[tuple[int, int, str]] = []
    cursor_ms = start_ms
    for index, chunk in enumerate(chunks):
        if index == len(chunks) - 1:
            chunk_end_ms = end_ms
        else:
            chunk_end_ms = min(end_ms, cursor_ms + max(1, (duration_ms * len(chunk)) // total_chars))
        display = _wrap_ass_text(chunk)
        if chunk_end_ms > cursor_ms and display:
            result.append((cursor_ms, chunk_end_ms, display))
        cursor_ms = chunk_end_ms
    return result


def _wrap_ass_text(text: str, *, max_chars: int = ASS_MAX_CHARS_PER_LINE) -> str:
    """Wrap one display chunk to at most 2 visual lines of <= max_chars,
    breaking at a punctuation boundary near the middle when possible."""

    line = " ".join(text.replace("\r", "\n").split())
    if len(line) <= max_chars:
        return line
    # choose the break closest to the middle, preferring natural boundaries
    candidates = [
        index + 1
        for index, char in enumerate(line[:-1])
        if char in _TEXT_BREAK_STRONG or char in _TEXT_BREAK_WEAK
    ]
    valid = [i for i in candidates if 0 < i <= max_chars and len(line) - i <= max_chars]
    if valid:
        break_at = min(valid, key=lambda i: abs(i - len(line) / 2))
    else:
        # no natural boundary: break at the middle, clamped so both halves fit
        break_at = min(max_chars, max(len(line) - max_chars, (len(line) + 1) // 2))
    first, second = line[:break_at].rstrip(), line[break_at:].lstrip()
    return f"{first}\\N{second}" if second else first


def _ass_escape_text(text: str) -> str:
    return text.replace("{", "（").replace("}", "）")


def _escape_ffmpeg_filter_path(path: Path | str) -> str:
    return str(path).replace("\\", "\\\\").replace("'", "\\'").replace(":", "\\:")
