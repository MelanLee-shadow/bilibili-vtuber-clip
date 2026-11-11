"""AUDIO_SILENT_UNDER_SUBTITLE: the finished clip (burn/final.mp4, intro included) must have sound under its subtitles.

Step 8 asks the media host for the digital-silence stretches of the clip's audio (assets/manual_lane/remote/audio_silences.py,
one ffmpeg pass) and holds them against subs/final.srt moved by the intro's offset.  A subtitle of at least
`min_subtitle_s` whose whole time (each end within `edge_tolerance_s`) lies in one silence fails the step, naming the
line and its time.  Quiet between subtitles is not looked at.  The numbers and why: lane_rules.json `audio_under_subtitle`.
The check is the clip's, not the owner's: whichever link made the silence (cut, compilation, burn, intro) the clip is broken.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import TypedDict

from . import subtitle_track
from .adapters import remote_python
from .lane_rules import rules
from .runner import Ctx, StepOutcome
from .step_io import REMOTE_TOOLS, _fail, _run_remote

AUDIO_SILENT_UNDER_SUBTITLE = "AUDIO_SILENT_UNDER_SUBTITLE"
AUDIO_CHECK_FAILED = "AUDIO_CHECK_FAILED"


class SilentLine(TypedDict):
    number: int
    start_s: float
    end_s: float


def silent_lines(cues: Sequence[subtitle_track.TrackCue], silences: Sequence[Sequence[float]], *, offset_s: float,
                 min_subtitle_s: float, edge_tolerance_s: float) -> list[SilentLine]:
    """The lines (1-based position in `cues`, time on the finished clip's clock) at least `min_subtitle_s` long that lie
    whole, within `edge_tolerance_s` at each end, in one of `silences` (start, end)."""
    found: list[SilentLine] = []
    for number, cue in enumerate(cues, 1):
        start, end = float(cue["start_s"]) + offset_s, float(cue["end_s"]) + offset_s
        if end - start < min_subtitle_s:
            continue
        if any(s <= start + edge_tolerance_s and e >= end - edge_tolerance_s for s, e in silences):
            found.append({"number": number, "start_s": start, "end_s": end})
    return found


def check(ctx: Ctx) -> tuple[StepOutcome | None, list[str]]:
    """(the failure that stops step 8, or None; PROCESS lines)."""
    rule = rules("audio_under_subtitle")
    noise_db, min_s, tolerance_s = rule["noise_db"], rule["min_subtitle_s"], rule["edge_tolerance_s"]
    rd = ctx.remote_dir
    remote = ctx.adapters.remote
    try:
        intro = json.loads((ctx.clip_dir / "burn/intro_result.json").read_text(encoding="utf-8"))
        offset_s = int(intro["intro_offset_ms"]) / 1000
        cues = subtitle_track.parse_blocks((ctx.clip_dir / "subs/final.srt").read_text(encoding="utf-8"))
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return _fail(AUDIO_CHECK_FAILED, f"读不到片头偏移或最终字幕，无法查字幕下有没有声音：{type(exc).__name__}: {exc!s:.200}"), []
    remote.put(REMOTE_TOOLS / "audio_silences.py", f"{rd}/tools/audio_silences.py")
    body = remote_python(f"{rd}/tools/audio_silences.py {rd}/burn/final.mp4 {noise_db:g} {max(0.1, min_s - 2 * tolerance_s):g}")
    run = _run_remote(ctx, "s8-audio-silences", body + "\n", timeout=1800)
    try:
        if run.rc != 0:
            raise ValueError(f"rc={run.rc} {run.stderr[-300:]}")
        silences = json.loads(run.stdout.strip().splitlines()[-1])["silences"]
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        return _fail(AUDIO_CHECK_FAILED, f"成片 burn/final.mp4 的静音检查没有结果：{exc!s:.300}"), []
    lines = silent_lines(cues, silences, offset_s=offset_s, min_subtitle_s=min_s, edge_tolerance_s=tolerance_s)
    if lines:
        return _fail(AUDIO_SILENT_UNDER_SUBTITLE, f"成片里有 {len(lines)} 句字幕整段是数字静音（≤{noise_db:g} dB），声音掉了："
                     + "；".join(f"第 {x['number']} 句 {x['start_s']:.1f}-{x['end_s']:.1f}s" for x in lines)), []
    return None, [f"成片字幕下有声音：{sum(1 for c in cues if float(c['end_s']) - float(c['start_s']) >= min_s)} 句已查"]
