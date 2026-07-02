#!/usr/bin/env python3
"""Align external timed lyrics to a clip by first-lyric anchor.

The default behavior is a pure global shift. Stretching is intentionally opt-in.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path


TIME_RE = re.compile(r"\[(\d{1,2}):(\d{2})(?:[.:](\d{1,3}))?\]")
SRT_RE = re.compile(r"^(?:(\d+):)?(\d{1,2}):(\d{2})(?:[,.](\d{1,3}))?$")


@dataclass
class Lyric:
    source_time: float
    text: str


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def parse_anchor(value: str) -> float:
    value = value.strip()
    if re.fullmatch(r"\d+(?:\.\d+)?", value):
        return float(value)
    match = SRT_RE.match(value)
    if not match:
        raise ValueError(f"Bad timestamp: {value!r}")
    hours_s, minutes_s, seconds_s, frac_s = match.groups()
    frac = (frac_s or "0")[:3].ljust(3, "0")
    return (
        int(hours_s or 0) * 3600
        + int(minutes_s) * 60
        + int(seconds_s)
        + int(frac) / 1000
    )


def srt_time(seconds: float) -> str:
    ms_total = max(0, int(round(seconds * 1000)))
    hours, rem = divmod(ms_total, 3600_000)
    minutes, rem = divmod(rem, 60_000)
    secs, ms = divmod(rem, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{ms:03d}"


def ass_time(seconds: float) -> str:
    cs_total = max(0, int(round(seconds * 100)))
    hours, rem = divmod(cs_total, 3600 * 100)
    minutes, rem = divmod(rem, 60 * 100)
    secs, cs = divmod(rem, 100)
    return f"{hours}:{minutes:02d}:{secs:02d}.{cs:02d}"


def parse_lrc(path: Path, *, keep_credits: bool) -> list[Lyric]:
    lyrics: list[Lyric] = []
    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        matches = list(TIME_RE.finditer(raw_line))
        if not matches:
            continue
        text = TIME_RE.sub("", raw_line).strip()
        if not text:
            continue
        if not keep_credits and re.search(r"作词|作曲|编曲|制作人|Composer|Lyricist", text, re.I):
            continue
        for match in matches:
            minutes, seconds, frac = match.groups()
            source_time = int(minutes) * 60 + int(seconds)
            if frac:
                source_time += int(frac[:3].ljust(3, "0")) / 1000
            lyrics.append(Lyric(source_time=source_time, text=text))
    lyrics.sort(key=lambda item: item.source_time)
    if not lyrics:
        raise ValueError(f"No timed lyric lines parsed from {path}")
    return lyrics


def escape_ass(text: str) -> str:
    return text.replace("\\", "\\\\").replace("{", "(").replace("}", ")").replace("\n", r"\N")


def write_srt(cues: list[tuple[float, float, str]], out: Path) -> None:
    blocks = []
    for index, (start, end, text) in enumerate(cues, 1):
        blocks.append(f"{index}\n{srt_time(start)} --> {srt_time(end)}\n{text}\n")
    out.write_text("\n".join(blocks), encoding="utf-8")


def write_ass(cues: list[tuple[float, float, str]], out: Path, *, play_res: str) -> None:
    width_s, height_s = play_res.lower().split("x", 1)
    width = int(width_s)
    height = int(height_s)
    font_size = 72 if height >= 1080 else 48
    margin_h = 60 if width >= 1920 else 40
    margin_v = 40 if height >= 1080 else 30
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {width}
PlayResY: {height}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Microsoft YaHei,{font_size},&H00FFFFFF,&H000000FF,&H00BA520F,&H70000000,0,0,0,0,100,100,0,0,1,3,2,2,{margin_h},{margin_h},{margin_v},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    lines = [header]
    for start, end, text in cues:
        lines.append(f"Dialogue: 0,{ass_time(start)},{ass_time(end)},Default,,0,0,0,,{escape_ass(text)}\n")
    out.write_text("".join(lines), encoding="utf-8")


def build_cues(
    lyrics: list[Lyric],
    *,
    lrc_first: float,
    clip_first: float,
    scale: float,
    max_duration: float,
    min_duration: float,
    tail_pad: float,
    media_duration: float | None,
) -> tuple[list[tuple[float, float, str]], list[str]]:
    warnings: list[str] = []
    mapped = [
        (clip_first + (lyric.source_time - lrc_first) * scale, lyric.text, lyric.source_time)
        for lyric in lyrics
    ]
    cues: list[tuple[float, float, str]] = []
    for idx, (start, text, _source_time) in enumerate(mapped):
        next_start = mapped[idx + 1][0] if idx + 1 < len(mapped) else None
        if next_start is None:
            end = start + tail_pad
        else:
            natural_gap = next_start - start
            end = min(next_start, start + max_duration)
            if natural_gap > max_duration + 0.5:
                warnings.append(
                    f"long_gap_capped cue={idx + 1} gap={natural_gap:.3f}s text={text!r}"
                )
        if end - start < min_duration:
            end = start + min_duration
            if next_start is not None and end > next_start:
                end = max(start + 0.1, next_start)
        if media_duration is not None and end > media_duration:
            end = media_duration
        if end <= start:
            warnings.append(f"dropped_nonpositive cue={idx + 1} text={text!r}")
            continue
        cues.append((start, end, text))
    return cues, warnings


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--lrc", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--clip-first", required=True, help="Clip time for first sung lyric, e.g. 00:00:19.200")
    parser.add_argument("--lrc-first", help="External source first lyric time. Defaults to first parsed lyric.")
    parser.add_argument("--clip-last", help="Clip time for final lyric start or checked final anchor.")
    parser.add_argument("--lrc-last", help="External source final lyric time. Defaults to last parsed lyric.")
    parser.add_argument("--allow-stretch", action="store_true", help="Apply linear stretch from first to last anchors.")
    parser.add_argument("--tail-threshold", type=float, default=0.75)
    parser.add_argument("--max-duration", type=float, default=6.0)
    parser.add_argument("--min-duration", type=float, default=0.8)
    parser.add_argument("--tail-pad", type=float, default=6.5)
    parser.add_argument("--media-duration", type=float)
    parser.add_argument("--keep-credits", action="store_true")
    parser.add_argument("--report", type=Path)
    parser.add_argument("--ass-out", type=Path)
    parser.add_argument("--play-res", default="1280x720")
    args = parser.parse_args()

    lyrics = parse_lrc(args.lrc, keep_credits=args.keep_credits)
    clip_first = parse_anchor(args.clip_first)
    lrc_first = parse_anchor(args.lrc_first) if args.lrc_first else lyrics[0].source_time
    lrc_last = parse_anchor(args.lrc_last) if args.lrc_last else lyrics[-1].source_time
    clip_last = parse_anchor(args.clip_last) if args.clip_last else None

    offset = clip_first - lrc_first
    scale = 1.0
    tail_delta = None
    warnings: list[str] = []
    if clip_last is not None:
        predicted_last = clip_first + (lrc_last - lrc_first)
        tail_delta = clip_last - predicted_last
        if abs(tail_delta) > args.tail_threshold:
            warnings.append(
                f"tail_delta_exceeds_threshold delta={tail_delta:.3f}s threshold={args.tail_threshold:.3f}s"
            )
        if args.allow_stretch:
            denominator = lrc_last - lrc_first
            if denominator <= 0:
                raise ValueError("Cannot stretch: lrc last anchor is not after first anchor")
            scale = (clip_last - clip_first) / denominator
            warnings.append(f"stretch_enabled scale={scale:.9f}")

    cues, cue_warnings = build_cues(
        lyrics,
        lrc_first=lrc_first,
        clip_first=clip_first,
        scale=scale,
        max_duration=args.max_duration,
        min_duration=args.min_duration,
        tail_pad=args.tail_pad,
        media_duration=args.media_duration,
    )
    warnings.extend(cue_warnings)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    write_srt(cues, args.out)
    if args.ass_out:
        args.ass_out.parent.mkdir(parents=True, exist_ok=True)
        write_ass(cues, args.ass_out, play_res=args.play_res)

    report = {
        "method": "external_lrc_first_anchor_global_shift",
        "lrc": str(args.lrc),
        "lrc_sha256": sha256_file(args.lrc),
        "out": str(args.out),
        "out_sha256": sha256_file(args.out),
        "cue_count": len(cues),
        "lrc_first": lrc_first,
        "clip_first": clip_first,
        "offset": offset,
        "lrc_last": lrc_last,
        "clip_last": clip_last,
        "tail_delta": tail_delta,
        "scale": scale,
        "stretch_used": args.allow_stretch,
        "warnings": warnings,
    }
    if args.ass_out:
        report["ass_out"] = str(args.ass_out)
        report["ass_sha256"] = sha256_file(args.ass_out)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
