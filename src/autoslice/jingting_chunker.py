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
from typing import Sequence

DEFAULT_TARGET_CHUNK_MS = 300_000
DEFAULT_MAX_CHUNK_MS = 390_000
DEFAULT_MIN_SPLIT_GAP_MS = 1_000
CHUNK_TAIL_PAD_MS = 1_000

_SRT_TIME_RX = re.compile(r"(\d{2}):(\d{2}):(\d{2})[,.](\d{1,3})")


@dataclass(frozen=True)
class SrtCue:
    index: str
    start_ms: int
    end_ms: int
    text: str


@dataclass(frozen=True)
class JingtingChunk:
    """One agy job: media window [media_start_ms, media_end_ms) of the context
    clip plus the draft cues inside it, re-based to the window start."""

    chunk_index: int
    media_start_ms: int
    media_end_ms: int
    cues: tuple[SrtCue, ...]

    @property
    def media_duration_ms(self) -> int:
        return max(1, self.media_end_ms - self.media_start_ms)

    def chunk_srt_text(self) -> str:
        blocks = []
        for cue in self.cues:
            start = max(0, cue.start_ms - self.media_start_ms)
            end = max(start + 1, cue.end_ms - self.media_start_ms)
            blocks.append(f"{cue.index}\n{_format_srt_time(start)} --> {_format_srt_time(end)}\n{cue.text}")
        return "\n\n".join(blocks) + ("\n" if blocks else "")


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


def plan_jingting_chunks(
    draft_srt_text: str,
    *,
    target_chunk_ms: int = DEFAULT_TARGET_CHUNK_MS,
    max_chunk_ms: int = DEFAULT_MAX_CHUNK_MS,
    min_split_gap_ms: int = DEFAULT_MIN_SPLIT_GAP_MS,
) -> list[JingtingChunk]:
    """Split a context draft SRT into sequential chunks of ~target_chunk_ms.

    Splits happen only in the silence gap between two cues (each cue belongs
    entirely to one chunk).  Past ``target_chunk_ms`` the first gap of at least
    ``min_split_gap_ms`` becomes the boundary; past ``max_chunk_ms`` any cue
    boundary does.  Media windows meet at the midpoint of the split gap so
    edge cues keep their full audio.
    """

    cues = parse_srt_cues(draft_srt_text)
    if not cues:
        return []

    groups: list[list[SrtCue]] = []
    current: list[SrtCue] = []
    chunk_anchor_ms = 0
    for position, cue in enumerate(cues):
        current.append(cue)
        if position + 1 >= len(cues):
            break
        next_cue = cues[position + 1]
        elapsed_ms = cue.end_ms - chunk_anchor_ms
        gap_ms = next_cue.start_ms - cue.end_ms
        should_split = (elapsed_ms >= target_chunk_ms and gap_ms >= min_split_gap_ms) or elapsed_ms >= max_chunk_ms
        if should_split:
            groups.append(current)
            current = []
            chunk_anchor_ms = cue.end_ms + max(0, gap_ms) // 2
    if current:
        groups.append(current)

    chunks: list[JingtingChunk] = []
    previous_boundary_ms = 0
    for chunk_index, group in enumerate(groups):
        if chunk_index + 1 < len(groups):
            next_group_start = groups[chunk_index + 1][0].start_ms
            boundary_ms = group[-1].end_ms + max(0, next_group_start - group[-1].end_ms) // 2
        else:
            boundary_ms = group[-1].end_ms + CHUNK_TAIL_PAD_MS
        chunks.append(
            JingtingChunk(
                chunk_index=chunk_index,
                media_start_ms=previous_boundary_ms,
                media_end_ms=max(previous_boundary_ms + 1, boundary_ms),
                cues=tuple(group),
            )
        )
        previous_boundary_ms = boundary_ms
    return chunks


def merge_refined_chunks(
    draft_srt_text: str,
    refined_chunks: Sequence[tuple[JingtingChunk, str]],
) -> str:
    """Transplant refined cue texts back onto the original draft timeline.

    The refined chunk SRTs must carry exactly the chunk's cue indices; any
    missing/extra/reordered index fails loudly instead of producing a silently
    misaligned subtitle file.
    """

    draft_cues = parse_srt_cues(draft_srt_text)
    if not draft_cues:
        raise ValueError("draft SRT has no parseable cues")

    refined_text_by_index: dict[str, str] = {}
    for chunk, refined_text in refined_chunks:
        refined_cues = parse_srt_cues(refined_text)
        expected = [cue.index for cue in chunk.cues]
        actual = [cue.index for cue in refined_cues]
        if expected != actual:
            raise ValueError(
                f"chunk {chunk.chunk_index} refined cue indices mismatch: expected {expected[:5]}...({len(expected)}) got {actual[:5]}...({len(actual)})"
            )
        for cue in refined_cues:
            refined_text_by_index[cue.index] = cue.text

    missing = [cue.index for cue in draft_cues if cue.index not in refined_text_by_index]
    if missing:
        raise ValueError(f"refined chunks did not cover draft cues: {missing[:8]} ({len(missing)} missing)")

    blocks = []
    for cue in draft_cues:
        text = refined_text_by_index[cue.index].strip() or cue.text
        blocks.append(f"{cue.index}\n{_format_srt_time(cue.start_ms)} --> {_format_srt_time(cue.end_ms)}\n{text}")
    return "\n\n".join(blocks) + "\n"


def _parse_srt_time_ms(value: str) -> int | None:
    match = _SRT_TIME_RX.search(value)
    if not match:
        return None
    hours, minutes, seconds, millis = match.groups()
    return ((int(hours) * 60 + int(minutes)) * 60 + int(seconds)) * 1000 + int(millis.ljust(3, "0"))


def _format_srt_time(ms: int) -> str:
    seconds, millis = divmod(max(0, ms), 1000)
    minutes, sec = divmod(seconds, 60)
    hours, minute = divmod(minutes, 60)
    return f"{hours:02d}:{minute:02d}:{sec:02d},{millis:03d}"
