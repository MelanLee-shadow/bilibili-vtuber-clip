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
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import TYPE_CHECKING, NotRequired, TypedDict, cast

from . import channel
from . import text_overlap
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

_MARK_RUN = re.compile(r"([，。！？、；：,.!?])(?:\s*[，。！？、；：,.!?])+")     # marks left side by side by a removed line: the first stays


def coverage(cue_text: str, subtitle: str) -> float:
    """The share of the cue's characters that appear, in order, in the screen subtitle (0 to 1).  Text comparison only."""
    return text_overlap.share(text_overlap.letters(cue_text), text_overlap.letters(subtitle), empty=0.0)


def own_subtitles(frames: Sequence[FrameReading]) -> bool:
    """The watched video carries its own subtitles: of the frames that show it, at least `own_subtitles_share` show one."""
    showing = [f for f in frames if f["ok"] and f.get("video_playing")]
    with_text = sum(1 for f in showing if str(f.get("video_subtitle") or "").strip())
    return bool(showing) and with_text / len(showing) >= float(rules("other_voices")["own_subtitles_share"])


def _spoken_lines(frames: Sequence[FrameReading]) -> list[tuple[float, list[str]]]:
    """(time, lines) of what each frame shows of the watched video's own subtitle, as spoken lines (letters only): without
    the pieces the reader marked as captions (a leading *) and without a piece that stays on the screen through the
    video (a title or a name plate: in `static_text_share` of the frames or more), which is not a line anybody says."""
    showing = [f for f in frames if f["ok"] and f.get("video_playing")]
    pieces = [[x for x in str(f.get("video_subtitle") or "").split() if not x.startswith("*")] for f in showing]
    seen: dict[str, int] = {}
    for row in pieces:
        for piece in set(row):
            seen[piece] = seen.get(piece, 0) + 1
    most = float(rules("other_voices")["static_text_share"]) * len(showing)
    return [(float(f["t"]), [text_overlap.letters(x) for x in row if (seen[x] < most or len(showing) < 4) and text_overlap.letters(x)])
            for f, row in zip(showing, pieces, strict=True)]


def _near(start_s: float, end_s: float, frames: Sequence[FrameReading]) -> list[str]:
    """The spoken lines the video's own subtitles show within `screen_window_s` of a cue.  A sentence and the frames
    that show its words seldom share a boundary, so the neighbouring frames are read with the cue's own."""
    window = float(rules("other_voices")["screen_window_s"])
    return [line for t, lines in _spoken_lines(frames) if start_s - window <= t <= end_s + window for line in lines]


def shown_on_screen(cue_text: str, start_s: float, end_s: float, frames: Sequence[FrameReading]) -> float:
    """The share of the cue's letters that the watched video's own subtitles show around the cue: runs of two or more
    letters found in the lines read there, or the whole of a one-letter cue."""
    text, lines = text_overlap.letters(cue_text), _near(start_s, end_s, frames)
    if not text or not lines:
        return 0.0
    if len(text) < 2:
        return float(text in lines)
    return text_overlap.share(text, " ".join(lines), min_run=2)


def without_shown(cue_text: str, start_s: float, end_s: float, frames: Sequence[FrameReading]) -> str:
    """The cue's text without the lines the video's own subtitles show (what is left is what only the host said).  A
    line is taken out only when the cue holds nearly all of it (`on_screen_min` of its letters): two letters a sentence
    happens to share with another line of the video stay.  Punctuation left at the front goes with what was removed."""
    text = text_overlap.letters(cue_text)
    gone = [False] * len(text)
    least = float(rules("other_voices")["on_screen_min"])
    for line in dict.fromkeys(_near(start_s, end_s, frames)):
        blocks = [m for m in SequenceMatcher(None, text, line, autojunk=False).get_matching_blocks() if m.size >= 2]
        if len(line) >= 2 and sum(m.size for m in blocks) >= least * len(line):
            for m in blocks:
                gone[m.a:m.a + m.size] = [True] * m.size
    out, at = [], 0                               # `at`: how many letters of the cue have been passed
    for ch in cue_text:
        if ch.isalnum():
            if not gone[at]:
                out.append(ch)
            at += 1
        else:
            out.append(ch)
    left = "".join(out)
    while left and not left[0].isalnum():
        left = left[1:]
    if any(gone):          # a stray syllable left in front of the first mark (the tail of a removed line) goes too
        head = next((i for i, ch in enumerate(left) if not ch.isalnum() and not ch.isspace()), len(left))
        if sum(ch.isalnum() for ch in left[:head]) < 2 and head < len(left):
            left = left[head:]
            while left and not left[0].isalnum():
                left = left[1:]
    return " ".join(_MARK_RUN.sub(r"\1", left).split())


def host_part(unshown: str, quoted: object) -> str:
    """The part of a cue's unshown words the review says the host speaks, when it really is a run of those words (the
    same letters in a row); otherwise all of them.  A recogniser mishears the video's speech, so a line of the video
    can stay among the unshown words under another spelling: the review, which reads the screen, says which are hers."""
    want = text_overlap.letters(quoted)
    at = text_overlap.letters(unshown).find(want) if want else -1
    if at < 0:
        return unshown
    start = end = None
    seen = 0
    for i, ch in enumerate(unshown):
        if ch.isalnum():
            if seen == at:
                start = i
            seen += 1
            if seen == at + len(want):
                end = i + 1
                break
    tail = unshown[end:]                                  # the mark that closes her words stays with them
    return (unshown[start:end] + (tail[0] if tail and not tail[0].isalnum() and not tail[0].isspace() else "")).strip()


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
    q = text_overlap.letters(quote)
    return bool(q) and any(q in text_overlap.letters(f.get("video_subtitle") or "") for f in frames if f["ok"])


def frame_line(f: FrameReading) -> str:
    if not f["ok"]:
        return f"{f['t']:.1f}s 读取失败"
    if not f.get("video_playing"):
        return f"{f['t']:.1f}s 无被看视频画面"
    sub, lang = str(f.get("video_subtitle") or ""), str(f.get("subtitle_language") or "")
    return f"{f['t']:.1f}s 被看视频画面，" + (f"字幕（{lang}）「{sub}」" if sub else "没有读到字幕")


def evidence_line(index: int, cue_text: str, frames: Sequence[FrameReading], host_prob: float | None, voice: str,
                  unshown: str = "") -> str:
    """One cue's evidence row for the review prompt: voiceprint, screen readings, text coverage.  `unshown`: the words
    of this cue that a video with its own subtitles does not show; the voiceprint then gives no direction and its
    number is not printed (a number the review reads is a number it follows)."""
    voiceprint = ("声纹被视频的声音盖住，不给方向" if unshown else
                  "声纹无可用证据" if host_prob is None else f"声纹{channel.HOST}概率 {host_prob:.2f}（{voice}）")
    screen = "；".join(frame_line(f) for f in frames) or "没有取帧"
    score, _ = best_match(cue_text, frames)
    n = len(text_overlap.letters(cue_text))
    note = f"；本句仅 {n} 字，吻合度参考价值低" if n < 3 else ""
    rest = f"；视频自带字幕，本句不在画面字幕里的字：「{unshown}」" if unshown else ""
    return f"{index + 1}「{cue_text}」：{voiceprint}；画面字幕：{screen}；与画面字幕吻合度 {round(score * 100)}%{note}{rest}"
