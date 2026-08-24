#!/usr/bin/env python3
"""Materialize the exact C10 private release subtitle projection."""
from __future__ import annotations

from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.autoslice.jingting_chunker import parse_srt_cues


ROOT = Path(__file__).resolve().parent
SOURCE = ROOT / "subtitles/auto_143025_868_1094.pipeline-diagnostic.srt"
OUTPUT = ROOT / "subtitles/auto_143025_868_1094.release.srt"
DROPPED = frozenset({6, 7, 8, 9, 10, 11, 14, 15, 16, 17, 18, 19, *range(21, 63)})
REPLACEMENTS = {73: "谢谢你 arigatou"}


def stamp(ms: int) -> str:
    hours, remainder = divmod(ms, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, millis = divmod(remainder, 1_000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"


def main() -> None:
    cues = parse_srt_cues(SOURCE.read_text(encoding="utf-8"))
    blocks = []
    for source_ordinal, cue in enumerate(cues, start=1):
        if source_ordinal in DROPPED:
            continue
        text = REPLACEMENTS.get(source_ordinal, str(cue.text).strip())
        blocks.append(
            f"{len(blocks) + 1}\n{stamp(cue.start_ms)} --> {stamp(cue.end_ms)}\n{text}"
        )
    OUTPUT.write_text("\n\n".join(blocks) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
