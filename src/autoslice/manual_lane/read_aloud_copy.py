"""Step 3: where a subtitle reads a chat record aloud word for word but for one or two characters, those characters
are written as the record has them (公开规则11-16: 「只要是知道是念弹幕，就抄着弹幕就完了。弹幕写成什么样就是什么样」).

The subtitle agent is told to copy a read-aloud record from the chat lookup; when it took the text from the picture
instead, the picture reading can be a character off (1086800_trim: the SC card 「…唱南町歌，就不想了（」 read back as
南酮). This settles that mechanically on the decided subtitles, after the agent and before the canonical writing:

- a record counts when it was sent before the sentence ends and no earlier than its look-back (90 s for a comment,
 the whole broadcast for an SC, as for every read-aloud lookup); gifts and guards are no message bodies;
- the sentence and the record are compared on their letters and characters (no punctuation, no spaces); a stretch
 counts when it is the same on both sides but for one or two Chinese characters, with at least four equal characters
 around them, and the sentence reads at least six of the record's characters in all and, of a longer record, eight
 or most of it (a word of her own inside the reading does not count against it: 1677890 「他们提的是黑白小猪的猪屎」) (the difference may start or end both texts: 11-16 「冲b币换电池」 was rewritten
 充 at the start) -- she is plainly reading it, and only the written form of those characters differs;
- and what she said is the record's: the written characters sound like the record's (the same blurred pinyin,
 name_match.sound_key: a homophone written another way, 充/冲), or a recogniser heard the record's sound there and
 not the written one (南酮: both recognisers heard nán tīng, the sound of 南町). When she reads a comment back in
 her own words -- another pronoun, another word, a joke on it (你的ID for 我的ID, 主播 for 主包, 祥子 for 样子) --
 the sound differs and nothing is changed (the 11-18 sweep over 34 lane clips found 27 such stretches);
- the sentence's own words around it (her reply, what she adds) are never touched, nor anything when two records
 disagree on the same character.

An imported push (an owner's or a ruled text) is its own authority and is not rewritten.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from difflib import SequenceMatcher
from pathlib import Path
from typing import TypedDict

from .evidence_attach import DANMAKU_BEFORE_S
from .name_match import sound_key
from .recorder_events import event_lookback_s

MIN_SPAN = 6        # equal + differing characters of one stretch
MIN_EQUAL = 4       # equal characters around the difference (both sides together)
MAX_DIFF = 2        # differing characters in one stretch
LONG_SPAN = 8       # ...and, for a record longer than that, at least this many or
MOST = 0.6          # this share of the record


class Copy(TypedDict):
    cue: int
    at: int             # position of the character in the cue's text
    written: str
    record_has: str
    record: str
    dt_s: float


def _cjk(ch: str) -> bool:
    return "㐀" <= ch <= "鿿"


def _content(text: str) -> tuple[str, list[int]]:
    keep = [(i, ch) for i, ch in enumerate(text) if ch.isalnum()]
    return "".join(ch for _i, ch in keep), [i for i, _ch in keep]


class Stretch(TypedDict):
    at: list[int]       # positions of the differing characters in the sentence's text
    written: str
    record_has: str
    left: str           # the equal character just before ('' at the start)
    right: str          # ... and just after ('' at the end)


def differences(text: str, record: str) -> list[Stretch]:
    """Each stretch where `text` reads `record` but for one or two Chinese characters."""
    a, pos = _content(text)
    b, _ = _content(record)
    ops = SequenceMatcher(None, a, b, autojunk=False).get_opcodes()
    out: list[Stretch] = []
    for k, (tag, i1, i2, j1, j2) in enumerate(ops):
        if tag != "replace" or not 0 < i2 - i1 == j2 - j1 <= MAX_DIFF:
            continue
        before = ops[k - 1] if k > 0 else None          # None: the difference starts both texts
        after = ops[k + 1] if k + 1 < len(ops) else None  # None: it ends both
        if (before and before[0] != "equal") or (after and after[0] != "equal"):
            continue
        left = before[2] - before[1] if before else 0
        right = after[2] - after[1] if after else 0
        read = sum(e2 - e1 for t, e1, e2, _f1, _f2 in ops if t == "equal") + (i2 - i1)   # her few words inserted aside
        if left + right < MIN_EQUAL or read < max(MIN_SPAN, min(LONG_SPAN, MOST * len(b))):
            continue
        if not all(_cjk(ch) for ch in a[i1:i2] + b[j1:j2]):
            continue
        out.append({"at": pos[i1:i2], "written": a[i1:i2], "record_has": b[j1:j2],
                    "left": a[i1 - 1] if left else "", "right": a[i2] if right else ""})
    return out


def _contains(heard: tuple[str, ...], part: tuple[str, ...] | None) -> bool:
    return bool(part) and any(heard[i:i + len(part)] == part for i in range(len(heard) - len(part) + 1))  # type: ignore[arg-type]


def she_said_the_record(st: Stretch, heard: Sequence[str]) -> bool:
    """The written characters sound like the record's, or a recogniser heard the record's sound and not theirs."""
    if sound_key(st["written"]) is not None and sound_key(st["written"]) == sound_key(st["record_has"]):
        return True
    keys = [k for k in (sound_key(h) for h in heard) if k]
    rec = sound_key(st["left"] + st["record_has"] + st["right"])
    own = sound_key(st["left"] + st["written"] + st["right"])
    return any(_contains(k, rec) for k in keys) and not any(_contains(k, own) for k in keys)


def copies(cues: Sequence[Mapping[str, object]], rows: Sequence[Mapping[str, object]],
           heard: Sequence[Sequence[str]] = ()) -> list[Copy]:
    """The characters to write as the records have them, by cue number (1-based).  `heard[n - 1]`: what the
    recognisers wrote over the n-th sentence's time."""
    out: list[Copy] = []
    for n, cue in enumerate(cues, 1):
        text, start, end = str(cue["text"]), float(str(cue["start_s"])), float(str(cue["end_s"]))
        found: dict[int, tuple[str, str, Mapping[str, object]]] = {}
        clash: set[int] = set()
        for row in rows:
            kind = row.get("type")
            if kind in {"gift", "guard"}:
                continue
            t = float(str(row["t_s"]))
            before = event_lookback_s(str(kind)) if kind else DANMAKU_BEFORE_S
            if not start - before <= t <= end:
                continue
            for st in differences(text, str(row["text"])):
                if not she_said_the_record(st, heard[n - 1] if n <= len(heard) else ()):
                    continue
                for at, written, has in zip(st["at"], st["written"], st["record_has"], strict=True):
                    if at in found and found[at][1] != has:
                        clash.add(at)
                    found.setdefault(at, (written, has, row))
        for at in sorted(set(found) - clash):
            written, has, row = found[at]
            out.append({"cue": n, "written": written, "record_has": has, "record": str(row["text"]),
                        "dt_s": round(float(str(row["t_s"])) - start, 1), "at": at})
    return out


def apply(cues: list[dict[str, object]], found: Sequence[Copy]) -> None:
    for c in found:
        cue = cues[c["cue"] - 1]
        text = str(cue["text"])
        cue["text"] = text[:c["at"]] + c["record_has"] + text[c["at"] + 1:]


def process_line(found: Sequence[Copy]) -> str:
    if not found:
        return "- 念读照原记录：没有只差一两个字的念读句"
    items = "；".join(f"第 {c['cue']} 句「{c['written']}」→「{c['record_has']}」（原记录：{c['record'][:40]}）" for c in found)
    return f"- 念读照原记录：改 {len(found)} 处——{items}"


def heard_over(clip_dir: Path, cues: Sequence[Mapping[str, object]]) -> list[list[str]]:
    """For each sentence, what the recognisers (asr/bcut.srt, asr/jianying.srt) wrote over its time."""
    from . import subtitle_track as st

    asr = [st.parse_blocks(p.read_text(encoding="utf-8"))
           for p in (clip_dir / "asr/bcut.srt", clip_dir / "asr/jianying.srt") if p.is_file()]
    return [[str(seg["text"]) for track in asr for seg in track
             if float(str(seg["start_s"])) < float(str(c["end_s"])) and float(str(seg["end_s"])) > float(str(c["start_s"]))]
            for c in cues]


def settle(clip_dir: Path, process: list[str]) -> list[Copy]:
    """subs/final.srt with the read-aloud characters written as the chat records (subs/../danmaku/lines.json) have
    them; the changes go to PROCESS and to subs/read_aloud_copy.json."""
    from . import subtitle_track as st

    lines = clip_dir / "danmaku/lines.json"
    path = clip_dir / "subs/final.srt"
    if not lines.is_file() or not path.is_file():
        process.append("- 念读照原记录：没有本片弹幕记录，未核对")
        return []
    cues = st.parse_blocks(path.read_text(encoding="utf-8"))
    found = copies(cues, json.loads(lines.read_text(encoding="utf-8")), heard_over(clip_dir, cues))
    if found:
        apply(cues, found)        # type: ignore[arg-type]
        path.write_text(st.format_blocks(cues, labels=True), encoding="utf-8")
    (clip_dir / "subs/read_aloud_copy.json").write_text(json.dumps(found, ensure_ascii=False, indent=1) + "\n",
                                                        encoding="utf-8")
    process.append(process_line(found))
    return found
