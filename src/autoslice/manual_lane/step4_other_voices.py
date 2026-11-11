"""Step 4, a single-participant clip with other voices (the streamer watching a video or an animation): the picture
evidence.  Nothing here decides who speaks; it reads frames with the cover step's CPA vision client and computes the
facts the CPA speaker review is given (lane_rules other_voices, assets/manual_lane/remote/read_screen_subtitles.py).

Two readings of the same kind, both on OCI3 against the clip's own cut.mp4:
  screen  a few frames spread over the clip: is a video someone else made playing at all?  (the signal that sends the
          clip to the other-voices route; a clip that shows none keeps uniform host)
  cue     frames inside each final cue: the video's own burned-in subtitle at that moment, which the review compares
          with the cue's recognised text.

Split out of step4_speaker.py; stage_impl re-exports the names.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import TYPE_CHECKING, NotRequired, TypedDict, cast

from .adapters import remote_python, root_script
from .lane_rules import rules
from .runner import Ctx
from .srt_text import SrtCue
from .step_io import REMOTE_TOOLS, _local, _run_remote

if TYPE_CHECKING:
    from .state import CpaEvent

SCREEN_TOOL = REMOTE_TOOLS / "read_screen_subtitles.py"
PICK_FRAME_TOOL = REMOTE_TOOLS / "pick_frame.py"   # the vision client the tool imports (same directory on the host)
VISION_EFFORT = "low"                              # pick_frame.ask's reasoning effort (the cover step's tier)
SCREEN_PURPOSE = "speaker-screen"


class FrameReading(TypedDict):
    id: str
    t: float
    ok: bool
    video_playing: NotRequired[bool]
    video_subtitle: NotRequired[str]
    subtitle_language: NotRequired[str]
    other_text: NotRequired[str]
    error: NotRequired[str]


class FrameRequest(TypedDict):
    id: str
    t: float


@dataclass
class Readings:
    """One reading pass: the frames (in request order), the paid calls to record, one PROCESS line, and `error`
    when the pass produced no usable reading at all (a tool failure: the caller stops the step)."""
    frames: list[FrameReading] = field(default_factory=list)
    calls: list[CpaEvent] = field(default_factory=list)
    line: str = ""
    error: str = ""


@dataclass
class VideoEvidence:
    """The picture evidence of one clip with other voices: the screen pass and the per-cue pass."""
    screen: Readings
    cue: Readings


def screen_requests(cues: Sequence[SrtCue]) -> list[FrameRequest]:
    """`screen_frames` frames spread evenly over the span the final cues cover (none when the rule is 0)."""
    n = int(rules("other_voices")["screen_frames"])
    if n <= 0 or not cues:
        return []
    start, end = float(cues[0]["start_s"]), float(cues[-1]["end_s"])
    return [{"id": f"s{i}", "t": round(start + (end - start) * (i + 0.5) / n, 3)} for i in range(n)]


def cue_requests(cues: Sequence[SrtCue]) -> list[FrameRequest]:
    """Frames inside each final cue: one, plus one for every `cue_frame_every_s` seconds of the cue (at most
    `cue_frames_max`), spread evenly within it.  Ids are c<cue index>-<k>."""
    cfg = rules("other_voices")
    every, most = float(cfg["cue_frame_every_s"]), int(cfg["cue_frames_max"])
    out: list[FrameRequest] = []
    for i, cue in enumerate(cues):
        start, end = float(cue["start_s"]), float(cue["end_s"])
        n = max(1, min(most, 1 + int((end - start) // every)))
        out.extend({"id": f"c{i}-{k}", "t": round(start + (end - start) * (k + 0.5) / n, 3)} for k in range(n))
    return out


def video_playing(frames: Sequence[FrameReading]) -> bool:
    """The screen pass saw a video someone else made playing in at least `screen_min_hits` readable frames."""
    hits = sum(1 for f in frames if f["ok"] and f.get("video_playing"))
    return hits >= int(rules("other_voices")["screen_min_hits"])


def read_frames(ctx: Ctx, mode: str, requests: Sequence[FrameRequest]) -> Readings:
    """Read `requests` on OCI3 (mode names the pass: files speaker/<mode>-*).  The same frames asked the same question
    are read from the earlier reading when it was complete: nothing is asked again.  A tool failure or a pass in which
    no frame could be read returns `error`."""
    rd = ctx.remote_dir
    request = {"frames": list(requests)}
    tool = SCREEN_TOOL.read_text(encoding="utf-8")
    stamp = hashlib.sha256(json.dumps([request, tool], ensure_ascii=False).encode("utf-8")).hexdigest()
    reading_local = _local(ctx, f"speaker/{mode}-readings.json")
    try:
        earlier = json.loads(reading_local.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        earlier = None
    if earlier and earlier.get("stamp") == stamp and all(f["ok"] for f in earlier["frames"]):
        return Readings(cast("list[FrameReading]", earlier["frames"]), [],
                        f"看图 {len(requests)} 帧（{mode}）沿用上次同一批帧的读数，未再调用")
    frames_local = _local(ctx, f"speaker/{mode}-frames.json")
    frames_local.write_text(json.dumps(request, ensure_ascii=False), encoding="utf-8")
    out_remote = f"{rd}/speaker/{mode}-out.json"
    ctx.adapters.remote.put(PICK_FRAME_TOOL, f"{rd}/speaker/pick_frame.py")
    ctx.adapters.remote.put(SCREEN_TOOL, f"{rd}/speaker/read_screen_subtitles.py")
    ctx.adapters.remote.put(frames_local, f"{rd}/speaker/{mode}-frames.json")
    body = (f"mkdir -p {rd}/speaker/{mode}-frames\n"
            + root_script(remote_python(f"{rd}/speaker/read_screen_subtitles.py {rd}/cut/cut.mp4 "
                                        f"{rd}/speaker/{mode}-frames {rd}/speaker/{mode}-frames.json {out_remote}"),
                          env=["cpa"], owned=f"{rd}/speaker/{mode}-frames {out_remote}", recursive=True))
    res = _run_remote(ctx, f"s4-screen-{mode}", body, timeout=1800)
    try:
        ctx.adapters.remote.get(out_remote, reading_local)
        doc = json.loads(reading_local.read_text(encoding="utf-8"))
    except (RuntimeError, OSError, ValueError) as exc:
        reading_local.unlink(missing_ok=True)
        return Readings(error=f"看图（{mode}）没有结果：rc={res.rc} {type(exc).__name__} {res.stderr[-160:]}")
    frames = cast("list[FrameReading]", doc["frames"])
    calls = call_records(mode, doc.get("calls") or [])
    doc["stamp"] = stamp
    reading_local.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
    ok = sum(1 for f in frames if f["ok"])
    line = (f"看图 {len(frames)} 帧（{mode}，CPA 视觉 {doc.get('model')}，思考档 {VISION_EFFORT}，付费 {len(calls)} 次调用）：可读 {ok} 帧"
            + (f"，失败 {len(frames) - ok} 帧（{'、'.join(sorted({str(f.get('error')) for f in frames if not f['ok']}))}）"
               if ok < len(frames) else ""))
    if not ok:
        return Readings(frames, calls, line, error=f"看图（{mode}）{len(frames)} 帧一帧都没读出来：{line}")
    return Readings(frames, calls, line)


def call_records(mode: str, calls: Sequence[dict[str, object]]) -> list[CpaEvent]:
    """The vision requests the tool made (it calls CPA directly, not through _ask_cpa) as cpa records for the state
    and PROCESS: every paid call of a step is in its record (TASK-9), as cover_vision_calls does."""
    return [cast("CpaEvent", {"call_id": f"speaker-{mode}-{i}", "model": c.get("model"), "effort": VISION_EFFORT,
                              "purpose": SCREEN_PURPOSE, "rc": c.get("rc"), "elapsed_s": c.get("elapsed_s"),
                              "kind": "vision", "usage": c.get("usage")})
            for i, c in enumerate(calls, 1)]


# ----- facts for the review ------------------------------------------------------------------------------

def _alnum(text: object) -> str:
    return "".join(ch for ch in str(text) if ch.isalnum())


def coverage(cue_text: str, subtitle: str) -> float:
    """The share of the cue's characters that appear, in order, in the screen subtitle (0 to 1).  Text comparison only."""
    a, b = _alnum(cue_text), _alnum(subtitle)
    if not a or not b:
        return 0.0
    blocks = SequenceMatcher(None, a, b, autojunk=False).get_matching_blocks()
    return sum(m.size for m in blocks) / len(a)


def by_cue(frames: Sequence[FrameReading]) -> dict[int, list[FrameReading]]:
    """The cue-pass readings grouped by cue index (frame ids are c<cue>-<k>)."""
    out: dict[int, list[FrameReading]] = {}
    for f in frames:
        out.setdefault(int(f["id"][1:].split("-")[0]), []).append(f)
    return out


def best_match(cue_text: str, frames: Sequence[FrameReading]) -> tuple[float, str]:
    """(coverage, subtitle) of the cue's best matching readable screen subtitle; (0, "") when none was read."""
    best, text = 0.0, ""
    for f in frames:
        sub = str(f.get("video_subtitle") or "") if f["ok"] and f.get("video_playing") else ""
        if sub and (score := coverage(cue_text, sub)) > best:
            best, text = score, sub
    return best, text


def quote_in_readings(quote: str, frames: Sequence[FrameReading]) -> bool:
    """The review's cited screen-subtitle words really were read from one of this cue's frames."""
    q = _alnum(quote)
    return bool(q) and any(q in _alnum(f.get("video_subtitle") or "") for f in frames if f["ok"])


def frame_line(f: FrameReading) -> str:
    if not f["ok"]:
        return f"{f['t']:.1f}s 读取失败"
    if not f.get("video_playing"):
        return f"{f['t']:.1f}s 无被看视频画面"
    sub, lang = str(f.get("video_subtitle") or ""), str(f.get("subtitle_language") or "")
    return f"{f['t']:.1f}s 被看视频画面，" + (f"字幕（{lang}）「{sub}」" if sub else "没有读到字幕")


def evidence_line(index: int, cue_text: str, frames: Sequence[FrameReading], host_prob: float | None, voice: str) -> str:
    """One cue's evidence row for the review prompt: voiceprint, screen readings, text coverage."""
    voiceprint = ("声纹无可用证据" if host_prob is None else f"声纹李豆沙概率 {host_prob:.2f}（{voice}）")
    screen = "；".join(frame_line(f) for f in frames) or "没有取帧"
    score, _ = best_match(cue_text, frames)
    n = len(_alnum(cue_text))
    note = f"；本句仅 {n} 字，吻合度参考价值低" if n < 3 else ""
    return f"{index + 1}「{cue_text}」：{voiceprint}；画面字幕：{screen}；与画面字幕吻合度 {round(score * 100)}%{note}"
