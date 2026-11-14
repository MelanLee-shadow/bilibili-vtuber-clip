from __future__ import annotations
import re
import sys
from pathlib import Path
from typing import Any
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _text_len(text: str) -> int:
    return sum(1 for ch in text if not ch.isspace())


def _parse_srt_time(value: str) -> float:
    hms, ms = value.split(",", 1)
    h, m, s = hms.split(":")
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000.0


def _parse_srt(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    text = path.read_bytes().decode("utf-8")
    cues: list[dict[str, Any]] = []
    for block in re.split(r"\n\s*\n", text.strip()):
        lines = block.splitlines()
        if len(lines) < 3 or "-->" not in lines[1]:
            continue
        start_raw, end_raw = [part.strip() for part in lines[1].split("-->", 1)]
        try:
            start = _parse_srt_time(start_raw)
            end = _parse_srt_time(end_raw)
        except Exception:
            continue
        cues.append(
            {"index": lines[0], "start": start, "end": end, "timing": lines[1], "lines": lines[2:]}
        )
    return cues


