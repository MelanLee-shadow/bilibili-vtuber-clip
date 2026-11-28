"""Step 3: the sounds of a clip that are worth watching and are no words -- a burst of laughter, a scream, a shout, a
sudden strange voice -- kept beside the subtitles in subs/sound_events.json, so that the title and the cover (step 6),
which read the final subtitles only and never hear the audio again, can use them.  The owner's words and the rule:
docs/pipeline/40-subtitle-text.md 「声音事件」.

Candidates, from what steps 2-3 already have, no model:
- text: a line of a whole-clip recognition (asr/bcut.srt, asr/jianying.srt, witness/mai.json, witness/moss.json) that
  holds a run of RUN_MIN or more laughter or cry letters of one kind (「哈哈哈」「啊啊啊啊」, also inside words:
  「给我出啊啊啊啊啊」), or is only such letters, at least two, over MIN_S or longer (「呵呵」 1.2 s).  Fillers
  (嗯、呃、哦) and word-like interjections (哎呀、诶) are not such letters;
- loudness where nothing was written: a frame JUMP_DB louder than the median of the JUMP_AROUND_S on either side, at
  least the clip's voiced p90 and the loudest within JUMP_PEAK_S, that no line of the dictations (DICTATIONS: BCUT,
  剪映) covers within BLANK_PAD_S -- a cry the recognitions wrote nothing for; LOUD_PICK_S around it is heard.  Without
  the blank, loudness is no candidate: a stream's sound is compressed (a scream peaks 2-3 dB above her speech) and
  such jumps are mostly speech after a pause (/26, 11 clips: 61 jumps at 10 dB, 154 at 6 dB, every one
  inside a line BCUT or 剪映 wrote);
- chat: DANMAKU_BURST reaction comments (REACTION: 哈哈哈、笑死、好吵、吓, a 「_大笑」 emote ...) within DANMAKU_SPAN_S, with no
  other candidate in the DANMAKU_LAG_S before the first: the loudest LOUD_PICK_S of that stretch is heard.  Its place is
  a guess, so the reactions that made it do not make it strong and the readers put it last (`chat_only`).
Each candidate keeps what found it (`found_by`: 识别稿 / 响度 / 弹幕).  Candidates closer than MERGE_GAP_S are one; at most
MAX_HEARD are heard, by `points`: a chat reaction one (at most POINTS_DANMAKU_MAX), a cry (「啊啊啊」: rarer than
laughter, which nearly every clip has 5-10 times) or a loud blank two, every recognition beyond the first one,
LOUD_STRONG_DB over the clip's voiced median one.

Each is heard once by the first listening level (prompts/listen_sound_event.md: what sound, made by a person in the
stream or by the game / a video / the software; never asked who): one with a kind other than 无 is an event, timed by
the hearing within its window.  The hearing describes, it does not filter: it named a sound on nearly every candidate
and on a third of plain speech windows (40-subtitle-text「声音事件」), so what is recorded is decided by the candidate
rules above.  Strength: 强 with DANMAKU_STRONG reactions (not on a chat-only event) or a hearing of 强 at LOUD_STRONG_DB
over the clip's voiced median, 弱 for a hearing of 弱 without reactions, else 中.

Who made it is step 4's evidence, never a model's ear (`attribute`, run when step 4 is done).  The hearing's `source`
is one hearing (it changed between hearings of one sound), and neither the voiceprint nor the arrival paths tell her
voice from the game's (game_sound: a game voice scores 0.43-0.49 against her; the game plays in her own path), so a
person is named only for a sound the hearing gave to a person in the stream, on a clip step 4 did not take for a
watched video (speaker/decision.json), away from what step 3 heard the game say (subs/game_sound.json, GAME_NEAR_S):
then the arrival groups (speaker/arrival.json) when one person holds WHO_MIN_SHARE of it, or the host when step 4
heard her voice alone (subs/voices.json no others, every line hers).  A sound of the game or a video is 画面内容;
everything else 不确定.

Nothing here changes a subtitle.  The hearing does not wait out a rate limit (`_listen_once`, not `_listen_pass`): a
candidate it did not answer is left unheard (in the file and PROCESS); the loudness tool failing leaves the events
without strength numbers and the loud-blank candidates out -- neither stops the step (the subtitles are done).
Readers: `read`, `title_lines` (step 6's title), `cover_times` (step 6's cover frames).
"""

from __future__ import annotations

import json
import re
import statistics
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, TypedDict

if TYPE_CHECKING:
    from .runner import Ctx, StepOutcome
    from .subtitle_track import TrackCue

REPORT = "subs/sound_events.json"
LEVELS = "listen/sound-levels.json"
PROMPT = "listen_sound_event.md"
LABEL = "sound-event"
PREFIX = "SE"
TOOL = "sound_levels.py"

RUN_MIN = 3             # letters of one kind in a row: 「哈哈哈」「啊啊啊」
MIN_S = 0.6             # a line of such letters only, at least this long
MERGE_GAP_S = 1.0
MARGIN_S = 1.0          # audio the hearing gets before / after the candidate
MAX_CORE_S = 8.0        # a longer candidate is heard around its loudest part
MAX_HEARD = 8
DANMAKU_BURST = 3
DANMAKU_SPAN_S = 6.0
DANMAKU_LAG_S = 8.0     # chat reacts this long after the sound at most
LOUD_PICK_S = 1.5
LOUD_SMOOTH_S = 0.3
DANMAKU_STRONG = 3
LOUD_STRONG_DB = 6.0
POINTS_DANMAKU_MAX = 5
JUMP_DB = 10.0          # plan B3's starting point; on the 11 measured clips 6, 8 and 10 dB found the same (no blank)
JUMP_AROUND_S = 2.0
JUMP_PEAK_S = 0.5
BLANK_PAD_S = 0.2
DICTATIONS = ("bcut", "jianying")
GAME_NEAR_S = 1.0       # a sound this close to a line a hearing placed in the game is not told apart from it
WHO_MIN_SHARE = 2 / 3
TEXT, LOUD, CHAT = "识别稿", "响度", "弹幕"
CONTENT = "画面内容"
UNSURE = "不确定"
UNHEARD_KIND = "无"

LAUGH = frozenset("哈呵嘿嘻")
CRY = frozenset("啊哇嗷呜嘤吼嚎")
_MARKS = re.compile(r"[\s，。！？、；：,.!?;:…~～\-—（）()「」『』\"“”'‘’]+")
# what a viewer writes when a sound got to them; emotes are written [<name>_<face>]
REACTION = re.compile(r"哈{3,}|h{3,}|2333|笑死|笑麻|笑疯|绷不住|蚌埠|好吵|吵死|耳朵|耳膜|吓死|吓到|吓我|吓一跳|尖叫|怪叫|鬼叫|"
                      r"鸡叫|破音|变声|\[[^\]]*_(?:大笑|哈哈哈?|被吓到|吓飞|吓到|震惊)\]", re.IGNORECASE)
RECOGNISERS = (("bcut", "asr/bcut.srt"), ("jianying", "asr/jianying.srt"), ("mai", "witness/mai.json"),
               ("moss", "witness/moss.json"))


class Candidate(TypedDict):
    start_s: float
    end_s: float
    evidence: list[str]         # what made it a candidate: 「bcut 12.3–13.1 秒「哈哈哈」」, 「弹幕 3 条反应」
    found_by: list[str]         # TEXT / LOUD / CHAT
    families: list[str]         # the recognitions that wrote it
    cry: bool                   # one of them wrote cry letters (啊啊啊), not only laughter
    danmaku: list[str]          # reaction comments from its start to DANMAKU_LAG_S after its end
    loud_db: float | None       # its loudest frame over the clip's voiced median (Levels.peak); None: not measured


class Event(TypedDict):
    start_s: float
    end_s: float
    peak_s: float               # the loudest frame of the event (the cover's frame); its middle without loudness
    kind: str                   # the hearing's word: 笑声 尖叫 大喊 怪声 变声 哭腔 叹气 咳嗽 拍打 其他
    sound: str                  # the hearing's sentence: what it sounds like
    source: str                 # person (someone in the stream) / content (the game, a video, the software) / unsure
    strength: str               # 强 / 中 / 弱
    loud_db: float | None       # its loudest LOUD_SMOOTH_S over the clip's voiced median; None: not measured
    danmaku: list[str]
    evidence: list[str]
    found_by: list[str]         # the candidate's: TEXT / LOUD / CHAT
    who: str | None            # a name, 画面内容 or 不确定 -- None until step 4 is done (`attribute`)
    who_basis: str


# -- candidates ---------------------------------------------------------------------------------------------------

def _runs(letters: str) -> dict[str, int]:
    """The longest run of laughter letters and of cry letters in `letters`."""
    best = {"laugh": 0, "cry": 0}
    run, kind = 0, None
    for ch in letters:
        k = "laugh" if ch in LAUGH else "cry" if ch in CRY else None
        run = run + 1 if k is not None and k == kind else (1 if k is not None else 0)
        kind = k
        if k is not None:
            best[k] = max(best[k], run)
    return best


def _only(letters: str, duration_s: float) -> bool:
    return len(letters) >= 2 and duration_s >= MIN_S and all(ch in LAUGH or ch in CRY for ch in letters)


def nonverbal(text: str, duration_s: float) -> bool:
    """Whether a recognised line is a candidate: RUN_MIN laughter or cry letters of one kind in a row, or only such
    letters (two at least) over MIN_S."""
    letters = _MARKS.sub("", text)
    return max(_runs(letters).values()) >= RUN_MIN or _only(letters, duration_s)


def cries(text: str, duration_s: float) -> bool:
    """A candidate line (`nonverbal`) that holds cry letters: RUN_MIN in a row, or only such letters over MIN_S."""
    letters = _MARKS.sub("", text)
    return _runs(letters)["cry"] >= RUN_MIN or (_only(letters, duration_s) and any(ch in CRY for ch in letters))


def recognised_lines(clip_dir: Path) -> list[tuple[str, float, float, str]]:
    """(recognition, start, end, text) of every whole-clip recognition step 3 kept, on the clip's clock."""
    from .srt_text import parse_srt
    out: list[tuple[str, float, float, str]] = []
    for name, rel in RECOGNISERS:
        path = clip_dir / rel
        if not path.is_file():
            continue
        if rel.endswith(".srt"):
            out += [(name, float(c["start_s"]), float(c["end_s"]), str(c["text"]))
                    for c in parse_srt(path.read_text(encoding="utf-8"))]
            continue
        doc = json.loads(path.read_text(encoding="utf-8"))
        if doc.get("status") != "OK":
            continue
        out += [(name, int(s["start_ms"]) / 1000, int(s["end_ms"]) / 1000, str(s["text"])) for s in doc["segments"]]
    return out


def reactions(chat: Sequence[Mapping[str, object]], start_s: float, end_s: float) -> list[str]:
    return [str(m["text"]) for m in chat
            if start_s <= float(m["t_s"]) <= end_s + DANMAKU_LAG_S and REACTION.search(str(m["text"]))]  # type: ignore[arg-type]


def chat_bursts(chat: Sequence[Mapping[str, object]]) -> list[float]:
    """The times of the first comment of every DANMAKU_BURST reactions within DANMAKU_SPAN_S (one per burst)."""
    times = sorted(float(m["t_s"]) for m in chat if REACTION.search(str(m["text"])))  # type: ignore[arg-type]
    out: list[float] = []
    i = 0
    while i < len(times):
        j = i
        while j + 1 < len(times) and times[j + 1] - times[i] <= DANMAKU_SPAN_S:
            j += 1
        if j - i + 1 >= DANMAKU_BURST:
            out.append(times[i])
            i = j + 1
        else:
            i += 1
    return out


def candidates(lines: Sequence[tuple[str, float, float, str]], chat: Sequence[Mapping[str, object]],
               duration_s: float, levels: Levels | None) -> list[Candidate]:
    """Merged candidates, in time order; `levels` None: the clip's loudness was not measured."""
    raw: list[Candidate] = [
        {"start_s": a, "end_s": b, "evidence": [f"{name} {a:.1f}–{b:.1f} 秒「{text}」"], "found_by": [TEXT],
         "families": [name], "cry": cries(text, b - a), "danmaku": [], "loud_db": None}
        for name, a, b, text in lines if b > a and nonverbal(text, b - a)]
    dictated = [(a, b) for name, a, b, text in lines if name in DICTATIONS and _MARKS.sub("", text)]
    if levels is not None and any(name in DICTATIONS for name, *_ in lines):
        for t, jump in levels.jumps():
            if not any(a - BLANK_PAD_S <= t <= b + BLANK_PAD_S for a, b in dictated):
                raw.append({"start_s": t - LOUD_PICK_S / 2, "end_s": t + LOUD_PICK_S / 2, "found_by": [LOUD],
                            "evidence": [f"响度 {t:.1f} 秒比前后 {JUMP_AROUND_S:g} 秒高 {jump:.0f} dB，BCUT、剪映都没写字"],
                            "families": [], "cry": False, "danmaku": [], "loud_db": None})
    for t in chat_bursts(chat):
        if any(c["start_s"] <= t and c["end_s"] >= t - DANMAKU_LAG_S for c in raw):
            continue
        lo, hi = max(0.0, t - DANMAKU_LAG_S), max(0.0, t - 0.5)
        span = levels.loudest(lo, hi) if levels is not None and hi > lo else None
        a, b = span if span is not None else (lo, hi)
        if b > a:
            raw.append({"start_s": a, "end_s": b, "evidence": [f"弹幕 {t:.1f} 秒起一阵反应，听它前面最响的一段"],
                        "found_by": [CHAT], "families": [], "cry": False, "danmaku": [], "loud_db": None})
    raw.sort(key=lambda c: (c["start_s"], c["end_s"]))
    merged: list[Candidate] = []
    for c in raw:
        if merged and c["start_s"] <= merged[-1]["end_s"] + MERGE_GAP_S:
            m = merged[-1]
            m["end_s"] = max(m["end_s"], c["end_s"])
            m["evidence"] += c["evidence"]
            m["found_by"] = [k for k in (TEXT, LOUD, CHAT) if k in m["found_by"] or k in c["found_by"]]
            m["families"] = sorted({*m["families"], *c["families"]})
            m["cry"] = m["cry"] or c["cry"]
        else:
            merged.append({**c, "evidence": list(c["evidence"]), "found_by": list(c["found_by"]),
                           "families": list(c["families"])})
    for m in merged:
        m["start_s"], m["end_s"] = round(max(0.0, m["start_s"]), 2), round(min(duration_s, m["end_s"]), 2)
        m["danmaku"] = reactions(chat, m["start_s"], m["end_s"])
        peak = levels.peak(m["start_s"], m["end_s"]) if levels is not None else None
        m["loud_db"] = peak[1] if peak else None
    return [m for m in merged if m["end_s"] > m["start_s"]]


def points(c: Candidate) -> int:
    return (min(len(c["danmaku"]), POINTS_DANMAKU_MAX) + 2 * (c["cry"] or LOUD in c["found_by"])
            + max(0, len(c["families"]) - 1) + (c["loud_db"] is not None and c["loud_db"] >= LOUD_STRONG_DB))


def chat_only(found_by: Sequence[str]) -> bool:
    """Found by a chat burst alone: where the sound is was guessed, and its reactions are what made it a candidate."""
    return list(found_by) == [CHAT]


def ranked(found: Sequence[Candidate]) -> list[Candidate]:
    """The ones to hear first: most `points`, then the earliest."""
    return sorted(found, key=lambda c: (-points(c), c["start_s"]))


# -- loudness -----------------------------------------------------------------------------------------------------

class Levels:
    """The clip's loudness per frame (sound_levels.py): smoothed over LOUD_SMOOTH_S, and its voiced median."""

    def __init__(self, doc: Mapping[str, object]):
        self.hop = float(doc["hop_s"])  # type: ignore[arg-type]
        db = [float(x) for x in doc["db"]]  # type: ignore[union-attr]
        f0 = [float(x) for x in doc["f0"]]  # type: ignore[union-attr]
        k = max(1, round(LOUD_SMOOTH_S / self.hop))
        self.smooth = [statistics.fmean(db[max(0, i - k // 2): i + k // 2 + 1]) for i in range(len(db))]
        voiced = [d for d, f in zip(db, f0, strict=True) if f > 0] or db
        self.median = statistics.median(voiced)
        self.p90 = statistics.quantiles(voiced, n=10)[-1] if len(voiced) > 1 else voiced[0]

    def jumps(self) -> list[tuple[float, float]]:
        """(time, dB over the median of the JUMP_AROUND_S on either side) of the frames JUMP_DB louder than that,
        at least the voiced p90 and the loudest within JUMP_PEAK_S (the first of a level stretch)."""
        sm, w = self.smooth, round(JUMP_AROUND_S / self.hop)
        k, near = round(LOUD_SMOOTH_S / self.hop), round(JUMP_PEAK_S / self.hop)
        out: list[tuple[float, float]] = []
        for i, level in enumerate(sm):
            if level < self.p90 or level < max(sm[max(0, i - near): i + near + 1]):
                continue
            if out and i * self.hop - out[-1][0] <= JUMP_PEAK_S:
                continue
            around = sm[max(0, i - w): max(0, i - k)] + sm[i + k: i + w]
            if around and level - statistics.median(around) >= JUMP_DB:
                out.append((round(i * self.hop, 2), round(level - statistics.median(around), 1)))
        return out

    def _frames(self, a: float, b: float) -> range:
        return range(max(0, int(a / self.hop)), min(len(self.smooth), int(b / self.hop) + 1))

    def peak(self, a: float, b: float) -> tuple[float, float] | None:
        """(time of the loudest frame in [a, b], its loudness over the voiced median)."""
        frames = self._frames(a, b)
        if not frames:
            return None
        i = max(frames, key=self.smooth.__getitem__)
        return round(i * self.hop, 2), round(self.smooth[i] - self.median, 1)

    def loudest(self, lo: float, hi: float) -> tuple[float, float] | None:
        width = LOUD_PICK_S
        if hi - lo <= width:
            return (lo, hi) if hi > lo else None
        best, at = None, lo
        t = lo
        while t + width <= hi:
            frames = self._frames(t, t + width)
            if frames:
                mean = statistics.fmean(self.smooth[i] for i in frames)
                if best is None or mean > best:
                    best, at = mean, t
            t += self.hop * 2
        return (round(at, 2), round(at + width, 2)) if best is not None else None


def _levels(ctx: Ctx) -> tuple[Levels | None, str]:
    """The clip's loudness, measured on the media host (TOOL on cut/), or None and why not."""
    from .adapters import remote_python
    from .step_io import REMOTE_TOOLS, _run_remote

    rd = ctx.remote_dir
    media = "cut.mp3" if (ctx.clip_dir / "cut/cut.mp3").exists() and not (ctx.clip_dir / "cut/cut.mp4").exists() else "cut.mp4"
    try:
        ctx.adapters.remote.put(REMOTE_TOOLS / TOOL, f"{rd}/tools/{TOOL}")
        run = _run_remote(ctx, "s3-sound-levels", remote_python(f"{rd}/tools/{TOOL} {rd}/cut/{media}") + "\n",
                          timeout=900)
        if run.rc != 0:
            raise ValueError(f"rc={run.rc} {run.stderr[-300:]}")
        doc = json.loads(run.stdout.strip().splitlines()[-1])
        levels = Levels(doc)
    except (OSError, RuntimeError, ValueError, KeyError, IndexError, TypeError) as exc:
        return None, f"响度没量到（{type(exc).__name__}: {exc!s:.200}）"
    (ctx.clip_dir / LEVELS).parent.mkdir(parents=True, exist_ok=True)
    (ctx.clip_dir / LEVELS).write_text(json.dumps(doc), encoding="utf-8")
    return levels, ""


# -- hearing ------------------------------------------------------------------------------------------------------

def windows(found: Sequence[Candidate], duration_s: float, levels: Levels | None, prefix: str = PREFIX) -> list[dict]:
    out = []
    for n, c in enumerate(found, 1):
        a, b = c["start_s"], c["end_s"]
        if b - a > MAX_CORE_S:
            mid = levels.peak(a, b)[0] if levels is not None and levels.peak(a, b) else (a + b) / 2  # type: ignore[index]
            a, b = max(a, mid - MAX_CORE_S / 2), min(b, mid + MAX_CORE_S / 2)
        out.append({"win": f"{prefix}{n:03d}", "core": [a, b],
                    "audio": [round(max(0.0, a - MARGIN_S), 3), round(min(duration_s, b + MARGIN_S), 3)],
                    "tempo": 1.0, "triggers": ["sound_event"]})
    return out


def heard(record: Mapping[str, object] | None) -> dict | None:
    """The hearing's sound ({kind, sound, source, strength, start_s?, end_s?}), {} when it heard none (无), None when
    it gave no answer that can be read."""
    if record is None or record.get("status") != "OK" or not isinstance(record.get("lines"), list):
        return None
    rows = [r for r in record["lines"] if isinstance(r, dict) and isinstance(r.get("kind"), str)]  # type: ignore[union-attr]
    if not rows:
        return None
    named = [r for r in rows if r["kind"].strip() not in ("", UNHEARD_KIND) and str(r.get("sound") or "").strip()]
    return dict(named[0]) if named else {}


def _seconds(row: Mapping[str, object], key: str) -> float | None:
    value = row.get(key)
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def strength(said: str, loud_db: float | None, danmaku: int, by_chat: bool = False) -> str:
    """`by_chat`: a chat-only event, whose reactions made it and so cannot also measure it."""
    if (danmaku >= DANMAKU_STRONG and not by_chat) or (said == "强" and loud_db is not None and loud_db >= LOUD_STRONG_DB):
        return "强"
    if said == "弱" and danmaku == 0:
        return "弱"
    return "中"


def event(c: Candidate, win: Mapping[str, object], said: Mapping[str, object], levels: Levels | None,
          chat: Sequence[Mapping[str, object]]) -> Event:
    """The event a hearing named: its time inside the window when the hearing gave one, else the window's core."""
    offset = float(win["audio"][0])  # type: ignore[index]
    lo, hi = float(win["audio"][0]), float(win["audio"][1])  # type: ignore[index]
    a, b = float(win["core"][0]), float(win["core"][1])  # type: ignore[index]
    s, e = _seconds(said, "start_s"), _seconds(said, "end_s")
    if s is not None and lo <= offset + s < hi:
        a = offset + s
        b = min(hi, offset + e) if e is not None and offset + e > a else max(a + 0.3, b)
    a, b = round(a, 2), round(b, 2)
    peak = levels.peak(a, b) if levels is not None else None
    danmaku = reactions(chat, a, b) or c["danmaku"]
    source = str(said.get("source") or "unsure")
    return {"start_s": a, "end_s": b, "peak_s": peak[0] if peak else round((a + b) / 2, 2), "kind": str(said["kind"]).strip(),
            "sound": str(said["sound"]).strip(), "source": source if source in ("person", "content", "unsure") else "unsure",
            "strength": strength(str(said.get("strength") or ""), peak[1] if peak else None, len(danmaku),
                                 chat_only(c["found_by"])),
            "loud_db": peak[1] if peak else None, "danmaku": danmaku, "evidence": c["evidence"],
            "found_by": c["found_by"], "who": None, "who_basis": ""}


def _write(clip: Path, report: Mapping[str, object]) -> None:
    (clip / REPORT).parent.mkdir(parents=True, exist_ok=True)
    (clip / REPORT).write_text(json.dumps(report, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


def settle(ctx: Ctx, process: list[str], *, prefix: str = PREFIX) -> None:
    """Step 3's sound events (REPORT) beside its decided subtitles; a PROCESS line.  Changes no subtitle."""
    from .step3_evidence import LISTEN_MODELS, _listen_once, upload_listen_tools

    clip = ctx.clip_dir
    duration = float(ctx.state.data["clip"]["end_ms"] - ctx.state.data["clip"]["start_ms"]) / 1000
    chat_file = clip / "danmaku/lines.json"
    chat = json.loads(chat_file.read_text(encoding="utf-8")) if chat_file.is_file() else []
    lines = recognised_lines(clip)
    report: dict[str, object] = {"candidates": 0, "heard": 0, "not_heard": [], "events": [], "levels": ""}
    if (not any(name in DICTATIONS for name, *_ in lines) and not chat_bursts(chat)
            and not any(b > a and nonverbal(text, b - a) for _, a, b, text in lines)):
        _write(clip, report)        # nothing to hear, and no dictation a loud blank could be told by
        process.append("- 声音事件：识别稿里没有笑声/叫声，没有 BCUT/剪映稿可比空白，弹幕没有成阵的反应，未复听")
        return
    levels, why = _levels(ctx)
    report["levels"] = why or "OK"
    found = candidates(lines, chat, duration, levels)
    if not found:
        _write(clip, report)
        process.append("- 声音事件：识别稿里没有笑声/叫声，没有识别稿空白处的响度突升，弹幕没有成阵的反应，未复听"
                       + (f"；{why}" if why else ""))
        return
    order = ranked(found)
    asked, later = order[:MAX_HEARD], order[MAX_HEARD:]
    wins = windows(asked, duration, levels, prefix)
    models = list(LISTEN_MODELS[:1])
    upload_listen_tools(ctx, PROMPT)
    # a side note to the decided subtitles: a rate limit is not waited out (_listen_pass would, up to 2 hours)
    records = _listen_once(ctx, f"{LABEL}-{prefix.lower()}", wins, models, prompt=PROMPT, prepared=True) or []
    by_win = {str(r.get("win")): r for r in records if r.get("slug") == models[0]["slug"]}
    events: list[Event] = []
    unheard: list[dict] = []
    for c, w in zip(asked, wins, strict=True):
        record = by_win.get(str(w["win"]))
        said = heard(record)
        if said is None:
            limited = record is not None and record.get("status") == "QUOTA"
            unheard.append({"start_s": c["start_s"], "end_s": c["end_s"], "evidence": c["evidence"],
                            "why": "复听被限流，不等" if limited else "复听没有回答"})
        elif said:
            events.append(event(c, w, said, levels, chat))
    answered = len(asked) - len(unheard)
    unheard += [{"start_s": c["start_s"], "end_s": c["end_s"], "evidence": c["evidence"],
                 "why": f"超过每片 {MAX_HEARD} 处的复听上限"} for c in later]
    events.sort(key=lambda e: e["start_s"])
    report.update(candidates=len(found), heard=answered, not_heard=unheard, events=events)
    _write(clip, report)
    process.append(f"- 声音事件：候选 {len(found)} 处，复听 {len(asked)} 处，记下 {len(events)} 个（{REPORT}）"
                   + "".join(f"；{e['start_s']:.1f}–{e['end_s']:.1f} 秒 {e['kind']}（{e['strength']}）" for e in events)
                   + (f"；{len(unheard)} 处没听（{'、'.join(sorted({u['why'] for u in unheard}))}）" if unheard else "")
                   + (f"；{why}" if why else ""))


# -- who (after step 4) -------------------------------------------------------------------------------------------

def _arrival_who(a_ms: int, b_ms: int, evidence: Mapping[str, object]) -> tuple[str, str]:
    persons = evidence["persons"] if isinstance(evidence.get("persons"), dict) else {}
    held: dict[tuple[str, bool], int] = {}  # by person, since step 4 may name two paths as the same person
    for seg in evidence["segments"]:  # type: ignore[attr-defined]
        ov = min(b_ms, int(seg["end_ms"])) - max(a_ms, int(seg["start_ms"]))
        if ov > 0:
            person = persons.get(str(seg["speaker"]))  # type: ignore[union-attr]
            key = (str(person), True) if person else (str(seg["speaker"]), False)
            held[key] = held.get(key, 0) + ov
    if not held:
        return UNSURE, "到达时间：这段没有落在任何一路声音上"
    (who, named) = max(held, key=held.__getitem__)
    share = held[(who, named)] / sum(held.values())
    if share < WHO_MIN_SHARE or not named:
        return UNSURE, f"到达时间：这段里几个人的声音都有（最多的一个占 {share:.0%}）" if named else "到达时间：这一路没定是谁"
    return who, f"到达时间：这段 {share:.0%} 落在{who}那一路"


def _one_voice(clip_dir: Path, host_label: str) -> bool:
    """Step 4 heard the host alone: the voiceprint found no other voice (subs/voices.json) and every line is hers."""
    from . import subtitle_track as st
    voices, speakers = clip_dir / "subs/voices.json", clip_dir / "speaker/speaker.srt"
    if not voices.is_file() or not speakers.is_file() or json.loads(voices.read_text(encoding="utf-8")).get("others") is not False:
        return False
    return all(c.get("speaker") == host_label for c in st.parse_blocks(speakers.read_text(encoding="utf-8")))


def _game_lines(clip_dir: Path) -> list[tuple[float, float]]:
    """The lines step 3's game-sound check heard a game part in (any hearing; subs/game_sound.json, game_sound.py);
    none when the check did not run (an imported push)."""
    path = clip_dir / "subs/game_sound.json"
    if not path.is_file():
        return []
    return [(float(v["line_s"][0]), float(v["line_s"][1])) for v in json.loads(path.read_text(encoding="utf-8"))
            if any(v["levels"].values())]


def who(e: Mapping[str, object], *, video: bool, game: Sequence[tuple[float, float]],
        evidence: Mapping[str, object] | None, alone: bool) -> tuple[str, str]:
    """(who, why) of one event: a person only where nothing says the sound may be the game's or a video's."""
    from . import channel

    a, b = float(e["start_s"]), float(e["end_s"])  # type: ignore[arg-type]
    if e["source"] == "content":
        return CONTENT, "复听：游戏、视频或软件自己的声音"
    if e["source"] != "person":
        return UNSURE, "复听听不出是直播间的人还是游戏/视频的声音"
    if video:
        return UNSURE, "第4步判画面在放视频：视频里的人和直播间的人分不开（不凭音色认人）"
    near = next(((ga, gb) for ga, gb in game if ga - GAME_NEAR_S < b and a < gb + GAME_NEAR_S), None)
    if near is not None:
        return UNSURE, f"第3步在 {near[0]:.1f}–{near[1]:.1f} 秒听出游戏/软件自己的声音，这一声分不开"
    if evidence is not None:
        return _arrival_who(round(a * 1000), round(b * 1000), evidence)
    if alone:
        return channel.HOST, "复听判为直播间的人；第4步整片只听到主播一人（声纹没听到他人，每句都是主播）"
    return UNSURE, "片里不止一人，又没有到达时间证据（不凭音色认人）"


def attribute(clip_dir: Path) -> str | None:
    """Who made each sound event (`who`), from step 4's evidence and step 3's game-sound check; written into REPORT.
    Returns the PROCESS line, or None when step 3 left no record."""
    from . import arrival, channel
    from .collab_guests import _watched_video

    path = clip_dir / REPORT
    if not path.is_file():
        return None
    report = json.loads(path.read_text(encoding="utf-8"))
    evidence = arrival.load(clip_dir)
    alone = evidence is None and _one_voice(clip_dir, channel.HOST_LABEL)
    video, game = _watched_video(clip_dir), _game_lines(clip_dir)
    for e in report["events"]:
        e["who"], e["who_basis"] = who(e, video=video, game=game, evidence=evidence, alone=alone)  # type: ignore[arg-type]
    path.write_text(json.dumps(report, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    if not report["events"]:
        return None
    return "- 声音事件的发声人：" + "；".join(f"{e['start_s']:.1f} 秒 {e['kind']} {e['who']}" for e in report["events"])


def with_who(step4: Callable[[Ctx], StepOutcome]) -> Callable[[Ctx], StepOutcome]:
    """Step 4, then who made step 3's sound events (`attribute`) when it is done."""
    def run(ctx: Ctx) -> StepOutcome:
        outcome = step4(ctx)
        if outcome.ok and (line := attribute(ctx.clip_dir)):
            outcome.process.append(line)
        return outcome
    run.__name__ = getattr(step4, "__name__", "step4_speaker")
    run.__doc__ = step4.__doc__
    return run


# -- readers ------------------------------------------------------------------------------------------------------

def read(clip_dir: Path) -> list[Event] | None:
    """The clip's sound events (REPORT), or None when its step 3 made none (a clip before)."""
    path = clip_dir / REPORT
    if not path.is_file():
        return None
    return list(json.loads(path.read_text(encoding="utf-8"))["events"])


def _where(cues: Sequence[TrackCue], t: float) -> str:
    for n, c in enumerate(cues, 1):
        if float(c["start_s"]) <= t <= float(c["end_s"]):
            return f"第 {n} 句时"
    before = sum(1 for c in cues if float(c["end_s"]) < t)
    if before == 0:
        return "第 1 句前"
    return f"第 {before} 句后" if before == len(cues) else f"第 {before}、{before + 1} 句之间"


def title_lines(clip_dir: Path) -> list[str]:
    """The sound events not of the game or a video, one line each, for the materials step 6 reads beside the final
    subtitles (numbered as subs/final.srt): where, who (不确定 stays so), what, how strong, the chat's reaction; a
    chat-only event says its place is a guess."""
    from . import subtitle_track as st

    events = read(clip_dir) or []
    cues = st.parse_blocks((clip_dir / "subs/final.srt").read_text(encoding="utf-8"))
    out = []
    for e in events:
        if e["source"] == "content":
            continue
        chat = f"，弹幕 {len(e['danmaku'])} 条反应：" + "、".join(dict.fromkeys(e["danmaku"][:4])) if e["danmaku"] else ""
        guess = "；只凭弹幕反应找的位置，可能不是这一声" if chat_only(e["found_by"]) else ""
        out.append(f"{_where(cues, e['peak_s'])}（{e['start_s']:.1f}–{e['end_s']:.1f} 秒）{e['who'] or UNSURE}："
                   f"{e['kind']}（{e['strength']}{chat}{guess}）——{e['sound']}")
    return out


def cover_times(clip_dir: Path) -> list[float]:
    """The peak frames of the sound events not of the game or a video, for the cover's frames: the ones a recognition
    or the loudness found first, strongest first; the chat-only ones (their place a guess) last."""
    rank = {"强": 0, "中": 1, "弱": 2}
    events = [e for e in read(clip_dir) or [] if e["source"] != "content"]
    return [e["peak_s"] for e in sorted(events, key=lambda e: (chat_only(e["found_by"]), rank.get(e["strength"], 3),
                                                               -len(e["danmaku"])))]
