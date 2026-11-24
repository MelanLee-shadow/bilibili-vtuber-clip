"""Step 3: words a game or a piece of software says are not subtitled (owner, U469: 「这个有几个是系统提示音，
系统提示音你怎么还给这个加成字幕了？就那个什么Ladies and gentlemen什么MVP那个，这个不应该加呀」).

The subtitle agent has the rule (subtitle_playbook_rules R21) and still wrote them: 316670 redone with R21 on 11-23
kept a countdown 「10」 it wrote itself and 「three two」 that the jianying orphan rule added after it, and the step 9
review let 「ten」 pass.  The voiceprint does not tell them apart either (a game voice scored 0.43-0.49 against the host,
13 of her 174 lines under 0.55).  So it is settled mechanically on the decided subtitles, by asking the audio:

- candidates: a line holding a clause written only in Latin letters and digits, between punctuation marks or the
  line's ends -- the announcer's English, a countdown, 「MVP」 -- whole or before / after her own words (397290
  「six，哇，哇！…」); letters inside a word of hers (「awawa是猪」「F键」) are no clause of their own;
- each candidate is heard by every listening level and once more by the first level, under other window names so the
  listening tool does not hand back its first answer (`AGAIN`), told the line and asked which of its parts a person in
  the stream said (reading along or counting with the game included) and which the game, the software or a video
  played by itself (prompts/listen_game_sound.md).  The second hearing of the first level is what makes two: since
  the second level names no served model (lane_rules listening.models, 维护者: no Carbon), so it never
  answers -- the first version of this check asked the two levels only and, on 11-24, removed none of the four
  countdowns of 850930, 397290 and 1493180 that the first level placed in the game;
- a foreign clause every answering hearing places in a game part, two hearings or more answering, is removed with the
  punctuation after it; a line with nothing left is removed, a line that keeps her words starts (or ends) where the
  first hearing heard them start (or the game part start), within the line's own time; her Chinese words are never
  removed here, whatever a hearing says;
- a line fewer than two hearings answered is heard once more by the first level (`THIRD`): a hearing that comes back
  with no part at all has not answered, and 1493180's 「Three, two.」「Three, two, one.」 (11-23) stayed because 3.8's
  first hearing came back empty while its second placed them in the game;
- anything a hearing calls a person's, or unsure, stays; with one hearing answering nothing is removed (PROCESS says so);
- what was removed is recorded as decided for the package step (package_guards.record_left_out), so it does not stop on
  the dictation that heard it;
- a candidate no level answered: the step stops (GAME_SOUND_UNHEARD) -- an unheard line is not a person's.

Not candidates: a game's voice lines in Chinese (none in the lane's clips so far; R21 and the step 9 review are what
looks at them).  An imported push (the owner's or a ruled text) is its own authority and is not rewritten.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, TypedDict

if TYPE_CHECKING:
    from .runner import Ctx
    from .subtitle_track import TrackCue

PROMPT = "listen_game_sound.md"
LABEL = "game-sound"
PREFIX = "GS"
AGAIN = "GR"            # the first level's second hearing: its own window names and its own slug (`<slug>-again`)
THIRD = "GT"            # its third, for the lines fewer than two hearings answered (`<slug>-third`)
MARGIN_S = 1.0          # audio before / after the line, never past the midpoint of the gap to a neighbour
MIN_KEPT_S = 0.5        # a line that keeps her words keeps at least this long
REPORT = "subs/game_sound.json"
REASON = "游戏/软件自己的声音"
BY = "复听判定不出字幕"
UNHEARD = "GAME_SOUND_UNHEARD"

_SEP = re.compile(r"([，。！？、；：,.!?;:…~～（）()「」『』\"“”\n]+)")
_FOREIGN = re.compile(r"[A-Za-z0-9][A-Za-z0-9 '\-]*")
_NUMBERS = {w: str(i) for i, w in enumerate(
    "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen "
    "eighteen nineteen twenty".split())}
SOURCES = ("game", "person", "unsure")


class Verdict(TypedDict):
    cue: int                    # 1-based number in final.srt before the check
    text: str
    foreign: list[int]          # positions of the foreign clauses in parts(text)
    levels: dict[str, list[int]]   # hearing (level slug, `<slug>-again`) -> the foreign clauses it placed in a game part
    removed: list[int]          # the clauses every answering hearing placed there (two hearings or more answered)
    start_s: float | None       # where the first hearing heard the line's kept part start / the game part start
    end_s: float | None


def parts(text: str) -> list[str]:
    """`text` as clause, separator, clause, separator, ...: a clause at every even position, the separator after it
    at the next (the last one, or a clause, may be empty)."""
    return [*_SEP.split(text), ""]


def foreign_clauses(text: str) -> list[int]:
    """Positions (in parts(text)) of the clauses written only in Latin letters, digits, spaces, apostrophes, hyphens."""
    pieces = parts(text)
    return [i for i in range(0, len(pieces), 2) if pieces[i].strip() and _FOREIGN.fullmatch(pieces[i].strip())]


def key(text: str) -> str:
    """Letters and characters to compare on: lower case, a number word as its digits, no spaces or punctuation."""
    tokens = re.findall(r"[a-z]+|\d+|[^\W\d_a-z]", text.lower())
    return "".join(_NUMBERS.get(t, t) for t in tokens)


def candidates(cues: Sequence[TrackCue]) -> list[int]:
    """0-based positions of the cues that hold a foreign clause."""
    return [i for i, c in enumerate(cues) if foreign_clauses(str(c["text"]))]


def windows(cues: Sequence[TrackCue], found: Sequence[int]) -> list[dict]:
    """One listening window per candidate, carrying the line (listen_windows.py hands `text` to the prompt)."""
    out = []
    for i in found:
        a0, b0 = float(cues[i]["start_s"]), float(cues[i]["end_s"])
        lo = a0 - MARGIN_S if i == 0 else max(a0 - MARGIN_S, (float(cues[i - 1]["end_s"]) + a0) / 2)
        hi = b0 + MARGIN_S if i + 1 == len(cues) else min(b0 + MARGIN_S, (b0 + float(cues[i + 1]["start_s"])) / 2)
        out.append({"win": f"{PREFIX}{i + 1:03d}", "core": [a0, b0], "audio": [round(max(0.0, min(lo, a0)), 3),
                                                                              round(max(hi, b0), 3)],
                    "cue_ids": [i + 1], "tempo": 1.0, "triggers": ["game_sound"],
                    "text": " ".join(str(cues[i]["text"]).split())})
    return out


def again_windows(wins: Sequence[dict], prefix: str = AGAIN) -> list[dict]:
    """The windows of a further hearing by the first level: the same audio and line under their own names."""
    return [{**w, "win": prefix + str(w["win"]).removeprefix(PREFIX)} for w in wins]


def heard_again(records: Sequence[Mapping[str, object]], prefix: str = AGAIN, suffix: str = "again") -> list[dict]:
    """A further hearing's records under the windows' own names and the hearing's slug (`<slug>-<suffix>`)."""
    def name(win: str) -> str:
        return PREFIX + win[len(prefix):] if win.startswith(prefix) else win
    return [{**r, "win": name(str(r.get("win", ""))), "slug": f"{r.get('slug')}-{suffix}"} for r in records]


def hearings(models: Sequence[Mapping[str, object]]) -> list[str]:
    """Every level, then the first level's second hearing; the first one's timing is used."""
    return [*(str(m["slug"]) for m in models), f"{models[0]['slug']}-again"]


def answer(record: Mapping[str, object]) -> list[dict] | None:
    """The parts a level's record gives ([{source, text, start_s?}]), or None when it gave none it could be read on."""
    if record.get("status") != "OK" or not isinstance(record.get("lines"), list):
        return None
    rows = [r for r in record["lines"] if isinstance(r, dict) and r.get("source") in SOURCES  # type: ignore[union-attr]
            and isinstance(r.get("text"), str)]
    return rows or None


def placed_in_game(text: str, rows: Sequence[Mapping[str, object]]) -> list[int]:
    """The foreign clauses of `text` that `rows` put in a game part: the clause is inside a game part's text and in no
    other part's, or every part is a game part (a countdown the line spells 10 and the level ten)."""
    pieces = parts(text)
    game = [key(str(r["text"])) for r in rows if r["source"] == "game"]
    other = [key(str(r["text"])) for r in rows if r["source"] != "game"]
    every = all(r["source"] == "game" for r in rows)
    out = []
    for i in foreign_clauses(text):
        k = key(pieces[i])
        if every or (any(k in g for g in game) and not any(k in o for o in other)):
            out.append(i)
    return out


def decide(cues: Sequence[TrackCue], wins: Sequence[dict], records: Sequence[Mapping[str, object]],
           slugs: Sequence[str]) -> list[Verdict]:
    """A verdict per window from the records of the passes; `slugs` the hearings asked, the first one's timing is used."""
    out: list[Verdict] = []
    for win in wins:
        n = int(win["cue_ids"][0])
        text = str(cues[n - 1]["text"])
        answers = {s: a for s in slugs for r in records
                   if r.get("win") == win["win"] and r.get("slug") == s and (a := answer(r)) is not None}
        levels = {s: placed_in_game(text, a) for s, a in answers.items()}
        removed = sorted(set.intersection(*(set(v) for v in levels.values()))) if len(levels) >= 2 else []
        start = end = None
        timing = next((answers[s] for s in slugs if s in answers), None)
        if removed and timing is not None:
            start, end = _kept_times(text, removed, timing, float(win["audio"][0]), win["core"])
        out.append({"cue": n, "text": text, "foreign": foreign_clauses(text), "levels": levels, "removed": removed,
                    "start_s": start, "end_s": end})
    return out


def _kept_times(text: str, removed: Sequence[int], rows: Sequence[Mapping[str, object]], offset: float,
                core: Sequence[float]) -> tuple[float | None, float | None]:
    """When the removed clauses open (or close) a line that keeps words: where the level heard its first part that is
    not a game part start (or the game part after its last other part start), on the clip's clock, inside the line's
    time; None where it does not or the level gave no such time."""
    pieces = parts(text)
    said = [i for i in range(0, len(pieces), 2) if pieces[i].strip()]
    if all(i in removed for i in said):
        return None, None
    a0, b0 = float(core[0]), float(core[1])

    def at(row: Mapping[str, object]) -> float | None:
        value = row.get("start_s")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        t = offset + float(value)
        return t if a0 <= t <= b0 else None
    start = end = None
    if said[0] in removed:
        first = next((at(r) for r in rows if r["source"] != "game"), None)
        start = first if first is not None and first <= b0 - MIN_KEPT_S else None
    if said[-1] in removed:
        last_other = max(i for i, r in enumerate(rows) if r["source"] != "game") if any(
            r["source"] != "game" for r in rows) else None
        after = [at(r) for r in rows[last_other + 1:]] if last_other is not None else []
        t = after[0] if after else None
        end = t if t is not None and t >= (start if start is not None else a0) + MIN_KEPT_S else None
    return start, end


def apply(cues: list[TrackCue], verdicts: Sequence[Verdict]) -> tuple[list[TrackCue], list[dict]]:
    """The cues with the removed clauses gone, and what was left out ([{start_s, end_s, text, reason}])."""
    by_cue = {v["cue"]: v for v in verdicts if v["removed"]}
    out: list[TrackCue] = []
    left_out: list[dict] = []
    for n, cue in enumerate(cues, 1):
        v = by_cue.get(n)
        if v is None:
            out.append(cue)
            continue
        pieces = parts(str(cue["text"]))
        removed_text = "，".join(pieces[i].strip() for i in v["removed"])
        kept = "".join(pieces[i] + pieces[i + 1] for i in range(0, len(pieces), 2) if i not in v["removed"])
        kept = kept.lstrip(" ，,、；;：:\n").rstrip(" ，,、；;：:\n")
        a0, b0 = float(cue["start_s"]), float(cue["end_s"])
        if not key(kept):
            left_out.append({"start_s": a0, "end_s": b0, "text": removed_text, "reason": REASON})
            continue
        start = a0 if v["start_s"] is None else v["start_s"]
        end = b0 if v["end_s"] is None else v["end_s"]
        if start > a0:
            left_out.append({"start_s": a0, "end_s": start, "text": removed_text, "reason": REASON})
        if end < b0:
            left_out.append({"start_s": end, "end_s": b0, "text": removed_text, "reason": REASON})
        out.append({**cue, "text": kept, "start_s": start, "end_s": end})
    return out, left_out


def settle(ctx: Ctx, process: list[str]) -> tuple[str, str] | None:
    """subs/final.srt without the words the game or the software said; the verdicts go to REPORT and PROCESS.
    Returns (reason, note) when the step must stop (a candidate no level answered), else None."""
    from . import subtitle_track as st
    from .package_guards import record_left_out
    from .step3_evidence import LISTEN_MODELS, _listen_pass, upload_listen_tools

    path = ctx.clip_dir / "subs/final.srt"
    report = ctx.clip_dir / REPORT
    cues = st.parse_blocks(path.read_text(encoding="utf-8"))
    found = candidates(cues)
    if not found:
        report.write_text("[]\n", encoding="utf-8")
        process.append("- 游戏/软件声音检查：没有只写外文或数字的分句，未复听")
        return None
    wins = windows(cues, found)
    models = list(LISTEN_MODELS)
    upload_listen_tools(ctx, PROMPT)
    records = _listen_pass(ctx, LABEL, wins, models, prompt=PROMPT, prepared=True)
    again = None if records is None else _listen_pass(ctx, f"{LABEL}-again", again_windows(wins), models[:1],
                                                      prompt=PROMPT, prepared=True)
    if records is None or again is None:
        return UNHEARD, f"游戏/软件声音检查：复听工具失败，{len(wins)} 句只写外文或数字的字幕没有判定"
    heard, slugs = [*records, *heard_again(again)], hearings(models)
    verdicts = decide(cues, wins, heard, slugs)
    short = [w for w, v in zip(wins, verdicts, strict=True) if len(v["levels"]) < 2]
    third = None if not short else _listen_pass(ctx, f"{LABEL}-third", again_windows(short, THIRD), models[:1],
                                                prompt=PROMPT, prepared=True)
    if third:
        heard, slugs = [*heard, *heard_again(third, THIRD, "third")], [*slugs, f"{models[0]['slug']}-third"]
        verdicts = decide(cues, wins, heard, slugs)
    report.write_text(json.dumps(verdicts, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    unheard = [v for v in verdicts if not v["levels"]]
    if unheard:
        return UNHEARD, ("游戏/软件声音检查：复听没有回答，这几句不知道是不是游戏的声音："
                         + "；".join(f"第 {v['cue']} 句「{v['text']}」" for v in unheard))
    kept, left_out = apply(cues, verdicts)
    if left_out:
        path.write_text(st.format_blocks(kept, labels=True), encoding="utf-8")
        record_left_out(ctx.clip_dir, left_out, BY)
    process.append(process_line(verdicts))
    return None


def process_line(verdicts: Sequence[Verdict]) -> str:
    def said(v: Verdict) -> str:
        pieces = parts(v["text"])
        return "、".join(pieces[i].strip() for i in v["removed"])
    removed = [v for v in verdicts if v["removed"]]
    one = [v for v in verdicts if len(v["levels"]) == 1 and any(v["levels"].values())]
    line = (f"- 游戏/软件声音检查：{len(verdicts)} 句有只写外文或数字的分句，每遍回答的复听（至少两遍）都判为游戏/软件声音而删去 {len(removed)} 句"
            + ("（" + "；".join(f"第 {v['cue']} 句「{v['text']}」删「{said(v)}」" for v in removed) + "）" if removed else ""))
    if one:
        line += "；只有一遍复听回答、未删：" + "；".join(f"第 {v['cue']} 句「{v['text']}」" for v in one)
    return line
