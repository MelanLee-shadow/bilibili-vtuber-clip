"""Chunk planning for the jingting second-listen (agy refine) stage.

Whole-session contexts (30min / 1.2GB) are exactly where gemini-3.5-flash is
unreliable: >30min videos are a documented empty-response failure mode, and the
practitioner consensus for transcription-accuracy work is 5-10 minute chunks
(antigravity-cli#76, gemini-cli#24290, Gemini video-understanding docs).  This
module splits a context draft SRT into ~5 minute chunks at cue gaps, so each
agy call sees a small clip plus the draft cues that belong to it, and merges
the per-chunk refined texts back onto the untouched original timeline.

Draft timing stays the single authority end to end: chunk drafts keep the
original cue indices, merge only transplants text, and the merged output is
timing-identical to the input draft by construction.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


_SRT_TIME_RX = re.compile(r"(\d{2}):(\d{2}):(\d{2})[,.](\d{1,3})")


@dataclass(frozen=True)
class SrtCue:
    index: str
    start_ms: int
    end_ms: int
    text: str


def parse_srt_cues(srt_text: str) -> list[SrtCue]:
    cues: list[SrtCue] = []
    raw = srt_text.replace("\r\n", "\n").replace("\r", "\n")
    for block_number, block in enumerate(raw.split("\n\n"), start=1):
        lines = [line for line in block.splitlines() if line.strip()]
        if len(lines) < 2:
            continue
        if "-->" in lines[0]:
            index = str(block_number)
            timing_line = lines[0]
            text_lines = lines[1:]
        else:
            index = lines[0].strip()
            timing_line = lines[1] if len(lines) > 1 else ""
            text_lines = lines[2:]
        if "-->" not in timing_line:
            continue
        start_raw, end_raw = [part.strip() for part in timing_line.split("-->", 1)]
        start_ms = _parse_srt_time_ms(start_raw)
        end_ms = _parse_srt_time_ms(end_raw)
        if start_ms is None or end_ms is None:
            continue
        cues.append(SrtCue(index=index, start_ms=start_ms, end_ms=end_ms, text="\n".join(text_lines).strip()))
    return cues


def _parse_srt_time_ms(value: str) -> int | None:
    match = _SRT_TIME_RX.search(value)
    if not match:
        return None
    hours, minutes, seconds, millis = match.groups()
    return ((int(hours) * 60 + int(minutes)) * 60 + int(seconds)) * 1000 + int(millis.ljust(3, "0"))


