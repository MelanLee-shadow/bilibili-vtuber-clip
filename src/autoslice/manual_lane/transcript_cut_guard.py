"""Guard ensuring ASR transcripts and final subtitles do not overrun `cut/probe.json` duration.

Kept in a leaf module (only runner, srt_text, state) so both `step3_subtitles` and `package_guards` import it
without pulling package-level subtitle/audio helpers back into `step3_subtitles`.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

from .runner import StepOutcome
from .srt_text import parse_srt
from .state import TRANSCRIPT_CUT_MISMATCH

CUT_DURATION_TOLERANCE_S = 0.5


def _probe_duration(clip_dir: Path) -> float | None:
    path = clip_dir / "cut/probe.json"
    if not path.is_file():
        return None
    doc = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(doc, dict):
        return None
    fmt = doc.get("format")
    if not isinstance(fmt, dict) or fmt.get("duration") is None:
        return None
    duration = float(fmt["duration"])
    return duration if math.isfinite(duration) and duration > 0 else None


def transcript_cut_mismatch(clip_dir: Path, *, include_final: bool = True) -> str:
    """Check that every existing ASR transcript (`asr/*.srt`) and `subs/final.srt` ends within cut duration + 0.5 s."""
    duration = _probe_duration(clip_dir)
    if duration is None:
        return ""
    asr_dir = clip_dir / "asr"
    paths = sorted(p for p in asr_dir.glob("*.srt") if p.is_file()) if asr_dir.is_dir() else []
    if include_final and (final := clip_dir / "subs/final.srt").is_file():
        paths.append(final)
    limit = duration + CUT_DURATION_TOLERANCE_S + 1e-9
    for path in paths:
        cues = parse_srt(path.read_text(encoding="utf-8"))
        if not cues:
            continue
        last_end = max(float(c["end_s"]) for c in cues)
        if last_end > limit:
            rel = path.relative_to(clip_dir).as_posix()
            return (f"{rel} 末句结束于 {last_end:.3f}s，超过剪辑时长 {duration:.3f}s "
                    f"+ {CUT_DURATION_TOLERANCE_S:g}s（cut/probe.json）")
    return ""


def transcript_cut_guard(clip_dir: Path, *, include_final: bool = True) -> StepOutcome | None:
    if why := transcript_cut_mismatch(clip_dir, include_final=include_final):
        return StepOutcome(False, reason=TRANSCRIPT_CUT_MISMATCH, note=why)
    return None
