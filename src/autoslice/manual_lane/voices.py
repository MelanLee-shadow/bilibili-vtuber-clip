"""Is anyone but the host heard in this clip?

Asked once, before the subtitles are written, from the host's voiceprint alone (compute on the media host, no paid
model): the share of the clip's speech that does not sound like the host, and the longest stretch of it.  The answer
decides whether the whole clip goes to the paid whole-clip recognisers (step3_evidence._asr_witnesses): measured on the
training clips, they do not lower the errors of a clip in which only the host speaks, and they do when a guest or a
watched video is heard (lane_rules other_voices.voice_source).

A channel without a voiceprint workspace, or a run of it that fails, gives no answer: the caller then keeps what it
would have done without asking."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import TypedDict

from . import speaker_v8
from .lane_rules import rules
from .layout import layout
from .runner import Ctx
from .srt_text import parse_srt
from .step_io import _local, linked_session


class Heard(TypedDict):
    others: bool | None           # None: no answer (no voiceprint workspace, or its run failed)
    basis: str                    # the sentence that goes into PROCESS
    share: float | None           # of the scored speech time, the part that confidently is not the host
    longest_run: int | None       # the most consecutive chunks that confidently are not the host


def measure(chunks: Mapping[int, Sequence[speaker_v8.ChunkPrediction]]) -> tuple[float, int]:
    """(share of the scored time that confidently is not the host, longest run of such chunks in time order)."""
    rows = sorted((c for per_cue in chunks.values() for c in per_cue), key=lambda c: c["s"])
    total = sum(c["e"] - c["s"] for c in rows)
    other = [float(c["host_prob"]) <= speaker_v8.GUEST_MAX_HOST_PROB for c in rows]
    share = sum(c["e"] - c["s"] for c, o in zip(rows, other, strict=True) if o) / total if total else 0.0
    run = best = 0
    for o in other:
        run = run + 1 if o else 0
        best = max(best, run)
    return share, best


def others_heard(ctx: Ctx) -> Heard:
    """The answer for this clip, kept in subs/voices.json (asked again, the voiceprint run is reused)."""
    from .step4_speaker import voice_chunks       # step 4 owns the voiceprint run; it imports the step-3 modules

    cfg = rules("other_voices")
    if linked_session(ctx.session):
        heard: Heard = {"others": True, "basis": "联动场次（两位以上参与者）", "share": None, "longest_run": None}
    elif not all(layout().speaker_v8.values()):
        heard = {"others": None, "basis": "没有配置声纹工作区，不判断片里有没有他人的声音", "share": None, "longest_run": None}
    else:
        chunks, error = voice_chunks(ctx, parse_srt(_local(ctx, "asr/bcut.srt").read_text(encoding="utf-8")))
        if error or not chunks:
            heard = {"others": None, "basis": f"声纹分段没有结果（{error or '没有可判的分段'}），不判断片里有没有他人的声音",
                     "share": None, "longest_run": None}
        else:
            share, run = measure(chunks)
            others = share >= float(cfg["voice_min_share"]) or run >= int(cfg["voice_min_run"])
            heard = {"others": others, "share": round(share, 4), "longest_run": run,
                     "basis": f"声纹分段（约 1.2 秒一段）：不像主播的占 {share:.1%}，最长连续 {run} 段"
                              f"（占比达到 {float(cfg['voice_min_share']):.0%} 或连续 {int(cfg['voice_min_run'])} 段算有他人的声音）"}
    out = _local(ctx, "subs/voices.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(heard, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return heard
