"""Step 4: who speaks in a voice collab, from the order the same words reach two streams (A12).

the channel's request;, of the clip published that day: 「字幕归属有问题」.  With three
people the host voiceprint alone hears about a third of her lines as someone else (step 4's lane_rules note), and no
threshold or retraining on his labels moved that.  The evidence that does separate them is physical: in a collab over a
voice call each participant streams their own sound, so a line reaches its speaker's own stream at once and every other
stream after the call's latency.  Against a guest's public recording of the same session the lag of a stretch of
speech (guest's stream minus ours) takes one value per speaker: lowest for the recording's owner, highest for our host,
in between for a third person (scripts/arrival_speakers.py measures it; scripts/guest_recording_audio.py finds the
recording).

Measured on the 11-23 trio with 礼墨Sumi's recording: three lag groups about 130 ms and 350 ms apart, the winning lag's
correlation above 0.9 on plain speech.

So when a guest's recording of the session is found and the clip is found in it, the arrival groups are step 4's label
engine (speaker_v8.fuse primary), and v8 or the witnesses' clusters do not outvote it (speaker_v8.DECISIVE; the one
doubt v8 raises is the last paragraph here).  The owner's own labels still stand over it.  The groups also name the
guests' lines for step 6 (guest_identity).  Without a recording, or when the clip is not in it, or it shows one group
only (one side speaks: nothing to tell apart), or no group can be given to anyone, step 4 is as before.

Who says a line (line_words) goes by where its words fall, not by who holds most of its time: a line's window is cut
by the recognition, often a word late or early, so a window holds its neighbours' voices at its edges (443680
「不是这个意思。」 90.2-91.8 s: the host's 「就不会疼了」 runs to 90.8 s, 安晚awa speaks from 91.3 s; by its time two
people).  Step 3's whole-clip recognitions time each letter (BCUT, asr/bcut.json) or word (jianying, its time spread
over its letters); clip_letters reads them (a revision's from the clip it revises: _recognition_dirs) when they are
the recognition of this cut (their utterances start where the SRT's cues do), letter_times aligns the subtitle's letters to them in order.  A letter is placed on the stretch it
starts on (its first LETTER_HEAD_MS).
- LETTER_MIN_SHARE of a line's content letters (fillers aside) placed: one person on LINE_MIN_SHARE of them is the
  line; two people one after the other, each on PART_MIN_LETTERS, with the stretches inside the line changing once
  from the one to the other and one phrase break (a mark, a space outside Latin words) between their letters: the
  line is cut at that break, the parts' times meeting at the second one's first timed letter (the letters are not
  touched; the parts go through step 4's split and wrap as a cut of the review's; v8's one probability for the whole
  line raises no doubt on it, speaker_v8.fuse).  Anything else is mixed, unless
  the line's time says the same person as its words, and read by CPA, told who is heard when in the line and on
  whose stretches its words lie (`heard`); CPA cuts such a line only at the engine's switch (speaker_v8.split_at_switch).  A line mixed between guests only keeps its label (only the name is open:
  step 6 decides it).
- Fewer letters placed (the recognitions miss them, or there are no files): the line goes by its time as before, less
  what its neighbours' placed words take at its edges: the person holding LINE_MIN_SHARE of it, otherwise mixed.  The
  neighbours' words may move the label but never settle it: a line its whole time calls two people or someone else
  is reviewed (897000 「three two one。我是不是抓太多东西了？」: the line before's misplaced letters took the host's
  104.3-105.1 s, leaving 安晚awa).
- Two adjacent lines of two people where the first one's own words run past its end (the recognition cut it early)
  meet where the first speaker stops (_switch); a line is never shortened and no text moves.
On the owner's 164 named lines of three clips (17 of them marked two people): of the 114 one-speaker lines the old
rule gave one person, 105 were his person before and are now; over the 157 lines he gave to someone 116 right, 1 cut at
the wrong break, 15 wrong and 25 undecided against 106 / 13 / 38 before; four of the two-people lines are cut right.  Of
the 140 one-speaker lines 10 are wrong against 9: two new ones lie on another person's stretch at the letter
(「现在只有两条哦」, 「为什么我完全没有在赚钱」), one old one is reviewed now; by label before the review (fuse) 4 of them
are labelled wrong unreviewed against 3 (「现在只有两条哦」), 15 reviewed against 20.

Who each group is does not need as many groups as registered people (784080 showed three groups, about
-2 / +487 / +597 ms, with two people registered, and the evidence was dropped: that evening 安晚awa sat with 礼墨Sumi
and shared her sound path, and the host's call latency changed once mid-clip).  Each group is a sound path, and a
group is the host's, a guests' path, or given to nobody:

- What tells is the host's voiceprint step 4 already has per line (speaker_v8.cue_speakers' host probability,
  concluded by the classifier's own bands: >= V8_MID_MIN_CONF the host, <= GUEST_MAX_HOST_PROB someone else), summed
  per group over the lines one group holds (LINE_MIN_SHARE): the time heard as the host and the time heard as someone
  else (host_time).  On the 19 clips of 11-23 and 11-24 found in 礼墨Sumi's recordings (52 groups) the host has 0.75
  to 1.00 of that time in the 22 host groups (the 11 highest of 11-23, known from the owner's labels and the group
  order; the 11 upper groups of 11-24, presumed: that day has no labels of his) and 0.00 to 0.17 in the 30 others.  By chunk instead of by line the two sets
  come closer (0.67 and 0.34): a line averages its chunks.
- The lag order says something only at its ends: the highest group is the host's if she speaks, the lowest the
  recording owner's path if anyone on it does.  A group between them can be a guest's own path or anyone's path at
  another latency.
- So (host_groups): the highest group is the host's and the lowest a guests' path when the voiceprint hears it that
  way more than not, on any amount.  Every other answer needs a conclusion: the host has HOST_MIN_SHARE of the
  concluded time or the others have, on GROUP_MIN_CONCLUDED_MS for a group between the ends and on
  AGAINST_ORDER_MIN_CONCLUDED_MS for an end against its order (the host silent the whole clip, or nobody but her).
  A group without that is given to nobody: its lines are not the arrival's (cue_clusters) and go by the voiceprint and
  the review as without the evidence.  No voiceprint on a group is no answer, never "the highest is the host": with
  the host silent the highest group is a guest (11-23 625460 at 40-70 s: 安晚awa).  Several host groups are one
  person whose latency moved.
- Why those amounts.  A short stretch is easily misheard: on windows of 30 to 90 s of the same clips (728 windows,
  one every 10 s) the majority of a group's concluded time is the wrong side on up to 5.9 s for the host's highest
  group (in a trio it hears an eighth of her concluded time as someone else: 21.6 s of 166.0 s on 11-23) and up to
  10.3 s at 0.56 for the lowest (on 11-24 the arrival itself gives some of the host's short lines to that path, see
  below), so an end is turned only by 10 s outside the middle third; a group between the ends was on the wrong side
  once, on 0.2 s, and one line runs one to three seconds, so it needs 3 s (the least a middle group of the full clips
  has is 4.1 s).  With these no group of the 19 clips or of the windows is given to the wrong side; the rule before
  (half of any concluded time, the highest the host's below 10 s) gave 32 window groups to the wrong side, among them
  the owner's path to the host on one misheard second (443680 at 0-30 s) and the host's second latency to a guest on
  no voiceprint line at all (298620 at 10-70 s).
- The guests' paths, lowest lag first.  With as many paths as registered guests and the lowest group among them, the
  lowest is the owner and a single remaining path the single remaining guest; paths beyond that are another guest,
  unnamed (still guests: the colour is right, only the name is missing).
- Fewer paths than registered guests, or the lowest group given to nobody: someone shares a path (sits with the
  recording's owner), did not speak, or is not placed.  The arrival then only tells the host from that path, which
  stays decisive; who of its people speaks is left to the voiceprint among them (guest_identity.arrival_among /
  voiceprint_lines: never the host, and no other route of step 6 may name the line anyone else), and a line nothing
  tells stays an unnamed guest.  The lowest path can be any registered guest, a higher one any guest but the owner.
- Fewer registered guests than people on a path (11-24: only 礼墨Sumi registered) cannot be seen from the lags: the
  host/guest layer is unaffected, and the owner's path carries the owner's name as the registration has it (with one
  registered guest no step names guest lines from it).

Where the arrival is not the last word (speaker_v8.fuse).  A frame goes to the path whose sound it matches best, so a
short line of the host's spoken while the guests' side is loud is taken by the guests' path: with two guests in one
room (784080) the arrival gives 6 of the 82 lines to the guests while the host's voiceprint is at 0.8 or more on
them (0.87 to 1.00: 「会吧」, 「等一下我打一下」, 「你都禁止一切。」), and no line the other way at 0.2 or less.  On the
owner's 11-23 names the arrival said a guest while the voiceprint concluded the host (>= 0.6) on 4 lines, all 4 his
host's (2 of them at that session's host threshold, 0.8); the other way (the arrival the host, the voiceprint someone
else) 8 lines, 4 each.  So a line the arrival gives to a guest while the voiceprint labels it the host (at the
session's own host threshold, outside its mid band) is a doubt for the review, the two routes tied; the voiceprint
saying a guest against the arrival's host is still no doubt.  11-24 has no names of the owner's: what the review
makes of these lines there is not measured."""

from __future__ import annotations

import json
import shlex
from collections.abc import Mapping, Sequence
from difflib import SequenceMatcher
from pathlib import Path
from typing import TYPE_CHECKING, NotRequired, TypedDict, cast

from . import text_overlap
from .adapters import PROFILE_ENV, REMOTE_PY, REMOTE_REPO
from .doubt_policy import FILLERS
from .speaker_v8 import (
    GUEST_LABEL,
    GUEST_MAX_HOST_PROB,
    HOST,
    SPAN_MIN_MS,
    V8_MID_MIN_CONF,
    ClusterCue,
    ClusterName,
    EngineSegment,
    MillisecondSpan,
    SpeakerDecision,
    people,
)
from .speaker_v8 import cue_clusters as _named_clusters

if TYPE_CHECKING:
    from .runner import Ctx

ENGINE = "arrival"
PATH = "speaker/arrival.json"
LINE_MIN_SHARE = 0.8            # a line is one person's when their stretches hold this share of its spoken time
HOST_MIN_SHARE = 2 / 3          # the voiceprint concludes on a group when one side has this share of its concluded time
GROUP_MIN_CONCLUDED_MS = 3_000  # ... on this much concluded time, for a group between the highest and the lowest
AGAINST_ORDER_MIN_CONCLUDED_MS = 10_000     # ... and on this much to say the highest is no host's or the lowest is hers
UNNAMED = "其他嘉宾"
SHARED_JOIN = "或"               # the label of a path several registered guests can be on: 「甲或乙」


class Recording(TypedDict):
    owner: str
    path: str
    bvid: str


class Evidence(TypedDict):
    """`named` and `persons` hold the groups given to someone; `segments` holds every group's stretches, so a line lying
    in a group given to nobody is seen as such (cue_clusters, line_persons)."""
    segments: list[EngineSegment]
    named: dict[str, ClusterName]
    persons: dict[str, str]
    owner: str
    bvid: str
    centres_ms: list[float]
    among: NotRequired[dict[str, list[str]]]         # {a shared path's label: the guests it can be}; absent in older files
    heard_ms: NotRequired[list[list[float]]]         # per group, host_time: [heard as the host, heard as someone else]
    letters: NotRequired[dict[str, list[list[str | int]]]]   # {recogniser: recognised_letters}; absent: lines go by their time


def host_time(runs: Sequence[Mapping[str, int]], groups: int, decided: Sequence[SpeakerDecision]) -> list[tuple[float, float]]:
    """Per lag group, the time (ms) its stretches hold in the lines the host's voiceprint concluded on: (lines heard as
    the host, lines heard as someone else).  A line counts for the group holding LINE_MIN_SHARE of its spoken time."""
    out = [[0.0, 0.0] for _ in range(groups)]
    for row in decided:
        p = row.get("host_prob")
        if p is None or GUEST_MAX_HOST_PROB < p < V8_MID_MIN_CONF:
            continue
        held = [0.0] * groups
        for run in runs:
            ov = min(int(row["end_ms"]), int(run["end_ms"])) - max(int(row["start_ms"]), int(run["start_ms"]))
            if ov > 0 and 0 <= int(run["cluster"]) < groups:
                held[int(run["cluster"])] += ov
        top = max(range(groups), key=held.__getitem__)
        if held[top] > 0 and held[top] >= LINE_MIN_SHARE * sum(held):
            out[top][0 if p >= V8_MID_MIN_CONF else 1] += held[top]
    return [(h, o) for h, o in out]


def host_groups(voice: Sequence[tuple[float, float]]) -> list[bool | None]:
    """Whose each lag group (lowest lag first) is, from host_time: True the host's, False a guests' path, None nobody's
    (the voiceprint does not say).  The highest group is the host's and the lowest a guests' path as soon as the
    voiceprint leans that way; anything else takes a conclusion (see the module docstring)."""
    out: list[bool | None] = []
    for k, (h, o) in enumerate(voice):
        order = True if k == len(voice) - 1 else False if k == 0 else None      # what the lag order says of the group
        need = GROUP_MIN_CONCLUDED_MS if order is None else AGAINST_ORDER_MIN_CONCLUDED_MS
        if order is not None and h != o and (h > o) == order:
            out.append(order)
        elif h + o >= need and not 1 - HOST_MIN_SHARE < h / (h + o) < HOST_MIN_SHARE:
            out.append(h > o)
        else:
            out.append(None)
    return out


def group_persons(kinds: Sequence[bool | None], host: str, owner: str, guests: Sequence[str]
                  ) -> tuple[list[str | None], dict[str, list[str]]]:
    """(the label of each lag group, lowest lag first, None for a group given to nobody; {a shared path's label: the
    guests it can be}).  `kinds` is host_groups'; `guests` are the registered guests, the recording's owner among them.
    See the module docstring for the rule."""
    paths = [k for k, kind in enumerate(kinds) if kind is False]
    others = [g for g in guests if g != owner]
    persons: list[str | None] = [host if kind else None for kind in kinds]
    among: dict[str, list[str]] = {}
    for j, k in enumerate(paths):
        can_be = others if j else [owner, *others]
        if len(paths) >= len(guests) and paths[0] == 0:      # every guest on a path of her own, the owner's the lowest
            can_be = can_be[:1] if j == 0 or (len(paths) == 2 and len(others) == 1) else []
        persons[k] = SHARED_JOIN.join(can_be) or UNNAMED
        if len(can_be) > 1:
            among[persons[k]] = can_be
    return persons, among


def engine(doc: Mapping[str, object], persons: Sequence[str | None], host: str
           ) -> tuple[list[EngineSegment], dict[str, ClusterName], dict[str, str]]:
    """The arrival document as a label engine of step 4: its runs as segments of clusters g0, g1, ... (lowest lag
    first), each cluster given to someone named host or guest, and who it is."""
    segments = [cast(EngineSegment, {"start_ms": int(r["start_ms"]), "end_ms": int(r["end_ms"]), "speaker": f"g{int(r['cluster'])}"})
                for r in cast("list[dict[str, int]]", doc.get("runs") or []) if 0 <= int(r["cluster"]) < len(persons)]
    who = {f"g{k}": p for k, p in enumerate(persons) if p is not None}
    return segments, {g: cast(ClusterName, {"speaker": HOST if p == host else GUEST_LABEL}) for g, p in who.items()}, who


# ----- the words of a line on the arrival's stretches ----------------------------------------------------------------
LETTER_HEAD_MS = 300        # a recognised letter is placed on its first 300 ms: a line's last letter is often stretched over
                            # what follows it (443680 「疼了」: 了 91.04-91.64 s; the host stops at 91.0 s and 安晚awa says
                            # 「不是这个意思」 from 91.3 s)
LETTER_NEAR_MS = 1_500      # a matched letter is the line's when it lies this near the line's window (the windows are a
                            # recognition's sentence times, off by up to a second: 443680 line 37 ends 1.4 s before its 了)
LETTER_MIN_SHARE = 0.7      # the words decide when this share of the line's content letters lies on someone's stretch
PART_MIN_LETTERS = 2        # each part of a line cut between two people holds this many placed content letters
WINDOW_MIN_MS = 200         # what the neighbours' words leave of a line's window is used when at least this long
SWITCH_GAP_MS = 50          # two lines meet when the second starts within this of the first one's end
SWITCH_MIN_MOVE_MS = 100    # ... and their boundary is moved when the first speaker stops at least this much later
RECOGNISERS = ("bcut", "jianying")      # step 3's whole-clip recognitions, letter (BCUT) / word (jianying) times: asr/<name>.json


class TextSpan(MillisecondSpan):
    text: str


class LinePart(TextSpan):
    person: str


class LineWords(MillisecondSpan):
    """Who says one line (line_words).  `person`: one label of the groups' persons, None when nobody holds the line;
    `parts`: the line cut between two people, in order (person None then); `mixed`: neither the words nor the time
    settle it, `person` being who holds most of it (the review reads it); `by`: "words", or "time" when too few of its
    letters are placed (its window, less the time its neighbours' placed words take); `heard`: who is heard when inside
    it, and on whose stretches its words lie, for the review; `people`: the persons its words and its time are on
    (by words; None: a time nobody holds).  start_ms / end_ms: the line's time, the end moved to where its speaker
    stops when its own words run past it (_switch)."""
    person: str | None
    parts: list[LinePart]
    mixed: bool
    by: str
    window: tuple[int, int]
    heard: str
    people: NotRequired[list[str | None]]


def recognised_letters(doc: Mapping[str, object]) -> list[list[str | int]]:
    """[[letter, start_ms, end_ms]] of a free_asr_client document (utterances[].words[]: label, start_time, end_time in
    ms); a word of several letters shares its time evenly among them."""
    out: list[list[str | int]] = []
    for utt in cast("list[dict[str, object]]", doc["utterances"]):
        for word in cast("list[dict[str, object]]", utt.get("words") or []):
            lab = text_overlap.key(word["label"])
            a, b = float(cast(float, word["start_time"])), float(cast(float, word["end_time"]))
            out += [[ch, round(a + (b - a) * k / len(lab)), round(a + (b - a) * (k + 1) / len(lab))] for k, ch in enumerate(lab)]
    return out


def letter_times(lines: Sequence[TextSpan], letters: Mapping[str, Sequence[Sequence[str | int]]]
                 ) -> list[list[tuple[int, int] | None]]:
    """Per line, per letter of its text (text_overlap.key): its recognised time, or None.  Each recogniser's letters are
    matched to the whole clip's text in order (the final text is the recognisers' words corrected, so most letters
    match), a match counting for a line when it lies within LETTER_NEAR_MS of the line's window; the first recogniser of
    RECOGNISERS that times a letter gives its time."""
    keys = [text_overlap.key(line["text"]) for line in lines]
    owner = [i for i, k in enumerate(keys) for _ in k]
    offset = [sum(len(k) for k in keys[:i]) for i in range(len(keys))]
    out: list[list[tuple[int, int] | None]] = [[None] * len(k) for k in keys]
    for name in RECOGNISERS:
        rec = letters.get(name) or []
        matcher = SequenceMatcher(None, "".join(keys), "".join(str(r[0]) for r in rec), autojunk=False)
        for block in matcher.get_matching_blocks():
            for j in range(block.size):
                i, (_, a, b) = owner[block.a + j], rec[block.b + j]
                k = block.a + j - offset[i]
                if out[i][k] is None and int(lines[i]["start_ms"]) - LETTER_NEAR_MS <= int(a) <= int(lines[i]["end_ms"]) + LETTER_NEAR_MS:
                    out[i][k] = (int(a), int(b))
    return out


def _group_at(a: int, b: int, segments: Sequence[EngineSegment]) -> str | None:
    """The group whose stretches hold most of [a, b), or None when no stretch touches it (the arrival did not hear it)."""
    held: dict[str, int] = {}
    for seg in segments:
        ov = min(b, int(seg["end_ms"])) - max(a, int(seg["start_ms"]))
        if ov > 0:
            held[str(seg["speaker"])] = held.get(str(seg["speaker"]), 0) + ov
    return max(held, key=held.__getitem__) if held else None


def _heard(a: int, b: int, segments: Sequence[EngineSegment], persons: Mapping[str, str]) -> str:
    """Who is heard when inside [a, b), in order: 「李豆沙 34.3–34.7 秒（0.4 秒）、安晚awa 35.0–35.9 秒（0.9 秒）」."""
    rows = []
    for seg in sorted(segments, key=lambda s: int(s["start_ms"])):
        s, e = max(a, int(seg["start_ms"])), min(b, int(seg["end_ms"]))
        if e > s:
            rows.append(f"{persons.get(str(seg['speaker']), '没定是谁的一路')} {s / 1000:.1f}–{e / 1000:.1f} 秒（{(e - s) / 1000:.1f} 秒）")
    return "、".join(rows) or "没听到人声"


def _turns(a: int, b: int, segments: Sequence[EngineSegment], persons: Mapping[str, str]) -> list[str]:
    """The people whose stretches hold at least SPAN_MIN_MS of [a, b), in order, one entry per turn."""
    out: list[str] = []
    for seg in sorted(segments, key=lambda s: int(s["start_ms"])):
        if min(b, int(seg["end_ms"])) - max(a, int(seg["start_ms"])) >= SPAN_MIN_MS:
            p = persons.get(str(seg["speaker"]), "")
            if not out or out[-1] != p:
                out.append(p)
    return out


def _breaks(text: str, last: int, first: int) -> list[tuple[int, int]]:
    """The phrase breaks of `text` between its letters `last` and `first` (text_overlap.key order): [(the letter after
    the break, its position in text)].  A break is a mark between two letters, or a space not inside a Latin phrase."""
    pos = [p for p, ch in enumerate(text) if ch.isalnum()]
    out = []
    for k in range(last + 1, first + 1):
        gap = text[pos[k - 1] + 1:pos[k]]
        if gap.strip() or (gap and not (text[pos[k - 1]].isascii() and text[pos[k]].isascii())):
            out.append((k, pos[k]))
    return out


def line_words(lines: Sequence[TextSpan], segments: Sequence[EngineSegment], persons: Mapping[str, str],
               letters: Mapping[str, Sequence[Sequence[str | int]]]) -> list[LineWords]:
    """Who says each line, from where its words fall among the arrival's stretches (`persons`: the named groups; a
    stretch of another group is heard but nobody's).  See the module docstring.  With no letters every line goes by its
    window, as the arrival did before it read the words."""
    times = letter_times(lines, letters)
    groups = [[None if t is None else _group_at(t[0], min(t[1], t[0] + LETTER_HEAD_MS), segments) for t in row] for row in times]
    whos = [[persons.get(g) if g is not None else None for g in row] for row in groups]
    placed_at = [[t for t, w in zip(row, ws, strict=True) if t is not None and w] for row, ws in zip(times, whos, strict=True)]
    out: list[LineWords] = []
    for i, line in enumerate(lines):
        c0, c1 = int(line["start_ms"]), int(line["end_ms"])
        key, who = text_overlap.key(line["text"]), whos[i]
        content = [k for k, ch in enumerate(key) if ch not in FILLERS] or list(range(len(key)))
        placed = [(k, cast(str, who[k])) for k in content if who[k]]
        if content and len(placed) >= LETTER_MIN_SHARE * len(content):
            out.append(_by_words(line, key, times[i], groups[i], placed, segments, persons))
            continue
        # too few letters placed: the window, less the time the neighbours' placed letters take at its edges
        a = max([c0, *(min(t[1], t[0] + LETTER_HEAD_MS) for j in range(i) for t in placed_at[j] if c0 <= t[0] < c1)])
        b = min([c1, *(t[0] for j in range(i + 1, len(lines)) for t in placed_at[j] if c0 < t[0] < c1)])
        a, b = (a, b) if b - a >= WINDOW_MIN_MS else (c0, c1)
        row = cast(LineWords, dict(_by_time(a, b, segments, persons), start_ms=c0, end_ms=c1))
        if (a, b) != (c0, c1):      # the neighbours' words may move the label, never settle it
            whole = _by_time(c0, c1, segments, persons)
            row["mixed"] = row["mixed"] or whole["mixed"] or whole["person"] != row["person"]
            taken = "、".join(f"{x / 1000:.1f}–{y / 1000:.1f} 秒" for x, y in ((c0, a), (b, c1)) if y > x)
            row["heard"] = f"{whole['heard']}；前后句对上的字占掉了 {taken}，剩下的 {a / 1000:.1f}–{b / 1000:.1f} 秒里：{row['heard']}"
        out.append(row)
    for i in range(len(lines) - 1):
        moved = _switch(lines[i], lines[i + 1], out[i], out[i + 1], times[i], whos[i], times[i + 1], whos[i + 1], segments, persons)
        if moved is not None:
            out[i]["end_ms"] = out[i + 1]["start_ms"] = moved
    return out


def _by_words(line: TextSpan, key: str, times: Sequence[tuple[int, int] | None], groups: Sequence[str | None],
              placed: Sequence[tuple[int, str]], segments: Sequence[EngineSegment], persons: Mapping[str, str]) -> LineWords:
    """The line's placed content letters: one person on LINE_MIN_SHARE of them; two people one after the other, each
    on PART_MIN_LETTERS, the stretches in the line changing once from the one to the other and the text having one
    phrase break between them: the line cut there; otherwise mixed, unless its time says one person too."""
    c0, c1 = int(line["start_ms"]), int(line["end_ms"])
    count: dict[str, int] = {}
    for _, p in placed:
        count[p] = count.get(p, 0) + 1
    top = max(count, key=count.__getitem__)
    where = ["识别稿里没对上" if t is None else "没听到人声" if g is None else persons.get(g, "没定是谁的一路")
             for t, g in zip(times, groups, strict=True)]
    runs: list[tuple[int, int, str]] = []                 # (first letter, end, where) of the text's letters
    for k, w in enumerate(where):
        if runs and runs[-1][2] == w:
            runs[-1] = (runs[-1][0], k + 1, w)
        else:
            runs.append((k, k + 1, w))
    told = "、".join(f"「{key[a:b]}」{w}" for a, b, w in runs)
    row: LineWords = {"start_ms": c0, "end_ms": c1, "person": top, "parts": [], "mixed": False, "by": "words",
                      "window": (c0, c1), "heard": f"{_heard(c0, c1, segments, persons)}；字：{told}", "people": list(count)}
    if count[top] >= LINE_MIN_SHARE * len(placed):
        return row
    sides: list[tuple[str, int, int, int]] = []           # (person, first letter, last letter, letters) of the placed ones
    for k, p in placed:
        if sides and sides[-1][0] == p:
            sides[-1] = (p, sides[-1][1], k, sides[-1][3] + 1)
        else:
            sides.append((p, k, k, 1))
    if len(sides) == 2 and min(s[3] for s in sides) >= PART_MIN_LETTERS \
            and _turns(c0, c1, segments, persons) == [sides[0][0], sides[1][0]]:
        last, first = sides[0][2], sides[1][1]           # a filler between them goes with the side its own frames are on
        while last + 1 < first and where[last + 1] == sides[0][0]:
            last += 1
        while first - 1 > last and where[first - 1] == sides[1][0]:
            first -= 1
        breaks = _breaks(line["text"], last, first)
        at = next((t for t in times[breaks[0][0]:] if t is not None), None) if len(breaks) == 1 else None
        if at is not None and c0 + SPAN_MIN_MS <= at[0] <= c1 - SPAN_MIN_MS:
            cut, text = breaks[0][1], line["text"]
            row.update(person=None, parts=[{"start_ms": c0, "end_ms": at[0], "text": text[:cut].strip(), "person": sides[0][0]},
                                           {"start_ms": at[0], "end_ms": c1, "text": text[cut:].strip(), "person": sides[1][0]}])
            return row
    whole = _by_time(c0, c1, segments, persons)
    row["mixed"] = whole["mixed"] or whole["person"] != top        # the line's time says the same person: it is theirs
    row["people"] = [*count, *([] if whole["person"] in count else [whole["person"]])]
    return row


def _by_time(a: int, b: int, segments: Sequence[EngineSegment], persons: Mapping[str, str]) -> LineWords:
    """The line by its time [a, b): the person whose stretches hold LINE_MIN_SHARE of what any stretch holds there;
    mixed when the named ones hold that much and no one person does; nobody's when the named groups do not."""
    held: dict[str, float] = {}
    for seg in segments:
        ov = min(b, int(seg["end_ms"])) - max(a, int(seg["start_ms"]))
        if ov > 0:
            held[persons.get(str(seg["speaker"]), "")] = held.get(persons.get(str(seg["speaker"]), ""), 0.0) + ov
    named = {p: v for p, v in held.items() if p}
    row: LineWords = {"start_ms": a, "end_ms": b, "person": None, "parts": [], "mixed": False, "by": "time", "window": (a, b),
                      "heard": _heard(a, b, segments, persons)}
    if named and sum(named.values()) >= LINE_MIN_SHARE * sum(held.values()):
        top = max(named, key=named.__getitem__)
        row.update(person=top, mixed=named[top] < LINE_MIN_SHARE * sum(held.values()))
    return row


def _switch(line: TextSpan, after: TextSpan, row: LineWords, nxt: LineWords, times: Sequence[tuple[int, int] | None],
            who: Sequence[str | None], times2: Sequence[tuple[int, int] | None], who2: Sequence[str | None],
            segments: Sequence[EngineSegment], persons: Mapping[str, str]) -> int | None:
    """The new boundary of two adjacent lines of two people when the first one's own words run past its end (the
    recognition cut it early): where its speaker stops (the end of their stretch holding their last placed letter), not
    past the second one's first word or stretch and leaving it SPAN_MIN_MS.  A line is never shortened: a stretch ends
    a frame's width from the voice, and a line shown a little long hides nothing."""
    p = row["person"] if not row["mixed"] and not row["parts"] else None
    q = nxt["person"] if not nxt["parts"] else None       # the second may be reviewed: the move rests on the first one's words
    edge = int(line["end_ms"])
    if p is None or q is None or p == q or abs(int(after["start_ms"]) - edge) > SWITCH_GAP_MS:
        return None
    last = next((t for t, w in zip(reversed(times), reversed(who), strict=True) if t is not None and w == p), None)
    if last is None or last[0] <= edge:
        return None
    stop = max((int(s["end_ms"]) for s in segments if persons.get(str(s["speaker"])) == p
                and min(int(s["end_ms"]), last[0] + LETTER_HEAD_MS) > max(int(s["start_ms"]), last[0])), default=None)
    first = next((t[0] for t, w in zip(times2, who2, strict=True) if t is not None and w == q), None)
    if first is None:
        first = min((int(s["start_ms"]) for s in segments if persons.get(str(s["speaker"])) == q
                     and last[0] <= int(s["start_ms"]) < int(after["end_ms"])), default=None)
    if stop is None:
        return None
    at = min(stop, first) if first is not None else stop
    return at if at - edge >= SWITCH_MIN_MOVE_MS and last[0] < at <= int(after["end_ms"]) - SPAN_MIN_MS else None


def cue_clusters(lines: Sequence[TextSpan], segments: Sequence[EngineSegment], named: Mapping[str, ClusterName],
                 persons: Mapping[str, str], letters: Mapping[str, Sequence[Sequence[str | int]]]) -> list[ClusterCue | None]:
    """Step 4's label of each line from the groups given to someone (line_words; `named` gives each group host or
    guest, `persons` who it is): {speaker, spans: the line is mixed, ms, note: who is heard when (a mixed line),
    parts: the line cut between two people [{start_ms, end_ms, text, speaker}], times: the line's time when moved}.
    A line going by its time takes the label rule (speaker_v8.cue_clusters: the label holding most of it, two labels
    of SPAN_MIN_MS each are two people) on the time it was judged on; None, as for a line the arrival did not hear,
    when the named groups hold less than LINE_MIN_SHARE of it (the rest lies in a group given to nobody)."""
    label = {persons[g]: n["speaker"] for g, n in named.items()}
    out: list[ClusterCue | None] = []
    for line, row in zip(lines, line_words(lines, segments, persons, letters), strict=True):
        if row["by"] == "time":
            span: MillisecondSpan = {"start_ms": row["window"][0], "end_ms": row["window"][1]}
            cue = _named_clusters([span], segments, named)[0]
            heard = sum(max(0, min(span["end_ms"], int(seg["end_ms"])) - max(span["start_ms"], int(seg["start_ms"]))) for seg in segments)
            if cue is None or sum(cue["ms"].values()) < LINE_MIN_SHARE * heard:
                out.append(None)
                continue
            if row["window"] != (int(line["start_ms"]), int(line["end_ms"])):     # judged on what the neighbours' words leave
                whole = cast(ClusterCue, _named_clusters([line], segments, named)[0])
                cue["spans"] = cue["spans"] or whole["spans"] or whole["speaker"] != cue["speaker"]
        else:
            whole = _named_clusters([line], segments, named)[0]
            # mixed between guests only: the label is settled (the name is not: line_persons leaves it to step 6)
            mixed = row["mixed"] and len({label.get(cast(str, p)) for p in row.get("people", [None])}) > 1
            cue = {"speaker": label[cast(str, row["person"] or row["parts"][0]["person"])], "spans": mixed,
                   "ms": whole["ms"] if whole else {}}
        if cue["spans"]:
            cue["note"] = row["heard"]
        if row["parts"]:
            cue["parts"] = [{"start_ms": p["start_ms"], "end_ms": p["end_ms"], "text": p["text"], "speaker": label[p["person"]]}
                            for p in row["parts"]]
        if (row["start_ms"], row["end_ms"]) != (int(line["start_ms"]), int(line["end_ms"])):
            cue["times"] = [row["start_ms"], row["end_ms"]]
        out.append(cue)
    return out


def line_persons(cues: Sequence[Mapping[str, object]], segments: Sequence[EngineSegment], persons: Mapping[str, str],
                 letters: Mapping[str, Sequence[Sequence[str | int]]]) -> dict[int, str]:
    """{line number: label} for the lines line_words gives to one label: a named person, or a shared path's label
    (Evidence.among); cues carry start_s / end_s / text (the subtitle track)."""
    lines: list[TextSpan] = [{"start_ms": round(float(cast(float, c["start_s"])) * 1000), "end_ms": round(float(cast(float, c["end_s"])) * 1000),
                              "text": str(c.get("text") or "")} for c in cues]
    return {n: cast(str, row["person"]) for n, row in enumerate(line_words(lines, segments, persons, letters), 1)
            if row["person"] and row["person"] != UNNAMED and not row["mixed"] and not row["parts"]}


# ----- what someone says into another person's sentence (owner, step 4's review decides) -----------------
INTERJECTION_GAP_MS = 300       # the main speaker's words on both sides of it lie this near (a neighbouring line of theirs)
INTERJECTION_MAX_LETTERS = 6    # it holds at most this many content letters ...
MAIN_MIN_SHARE = 2 / 3          # ... and the main speaker holds this share of the line's placed content letters
UNTIMED_LETTER_MS = 100         # a phrase the recognitions do not time, with less than this per content letter between
                                # the recognised words around it, was said over them


class Interjection(TypedDict):
    """A phrase of a line (or a whole line) that may be said into another person's sentence (interjections): `main`
    says the sentence around it, `by` the named person heard inside it; `at` its time, between the main speaker's words around it; `text` the phrase; `span` the text it takes in
    its line ([a, b) of text positions: the phrase and the marks after it), what goes when it is not subtitled; `whole`
    the line is all of it; `cut`: for a phrase at an edge of its line said by a named person, where the line is cut
    when it is that person's own line (the first timed letter of the second part), else None; `together`: the main
    speaker is heard while it is said (_together) -- only then may it go unsubtitled; `heard` who is heard when, and on
    whose stretches the line's letters lie, for the review."""
    line: int
    main: str
    by: str
    at: tuple[int, int]
    text: str
    span: tuple[int, int]
    whole: bool
    cut: int | None
    together: bool
    heard: str


def interjections(lines: Sequence[TextSpan], segments: Sequence[EngineSegment], persons: Mapping[str, str],
                  letters: Mapping[str, Sequence[Sequence[str | int]]]) -> list[Interjection]:
    """The phrases of the clip said into another person's sentence (owner an interjection need not be
    subtitled).  Only candidates: whether the words are an interjection (附和、感叹、无实义、说了半句) or a line of their
    own is a reading of meaning, step 4's review's (step4_speaker._interjection_rows).

    A phrase (letters between two phrase breaks, _breaks, or the line's edges) is a candidate when none of its letters
    lies on the main speaker's stretches, it holds at most INTERJECTION_MAX_LETTERS content letters, and the main
    speaker's words lie on both sides of it: inside its line, or in their own line touching it (INTERJECTION_GAP_MS).
    The main speaker is who holds MAIN_MIN_SHARE of the line's placed content letters (at least PART_MIN_LETTERS), or,
    for a whole line, who holds the lines on both sides of it.  Someone else named must be heard for SPAN_MIN_MS
    between the main speaker's words around it.  A stretch where nobody is heard is no evidence of who said what and
    gives no candidate: 897000 「挖矿挖的」 (14.5-14.9 s on no stretch, between the host's 「要不我们今天，」 and
    「我们今天先玩到这」) is 安晚awa's own sentence with the host's words in the background, which the recognitions did
    not take (the owner's note of her main sentence, the host's words behind it need no subtitle);
    reading it as the host's sentence with an interjection left 安晚awa's line out.  Letters the recognitions do not
    time while the main speaker goes on are no interjection by themselves.  A line cut between two people
    (line_words' parts) or mixed is not one person's sentence.
    The owner lets go only what is said while the main speaker talks (「在期间」「同时说话」): a phrase said after the main
    speaker stops, the other one answering and the main one going on (1064220 「是吗？」 between two lines of the guest's
    path, 50 ms on each side, nobody else heard in it), is a turn of its own and kept (`together` False).  Such a phrase
    is still a candidate where it may be the other person's own line at the edge of the main speaker's line (`cut`),
    for the review to cut it off; otherwise it is none."""
    rows = line_words(lines, segments, persons, letters)
    times = letter_times(lines, letters)
    whos = [[None if t is None else persons.get(g) if (g := _group_at(t[0], min(t[1], t[0] + LETTER_HEAD_MS), segments)) else None
             for t in row] for row in times]
    out: list[Interjection] = []
    for i, line in enumerate(lines):
        key, text = text_overlap.key(line["text"]), line["text"]
        pos = [p for p, ch in enumerate(text) if ch.isalnum()]
        if not key or len(pos) != len(key):
            continue
        found = _line_main(i, lines, rows, key, whos[i])
        if found is None:
            continue
        main, edges = found
        for a, b in zip(edges, edges[1:], strict=False):
            got = _phrase(i, a, b, main, lines, rows, key, pos, whos[i], times, segments, persons)
            if got is not None:
                out.append(got)
    return out


def _single(rows: Sequence[LineWords], j: int) -> str | None:
    """The one person of line j (not cut in parts, not mixed), or None."""
    r = rows[j] if 0 <= j < len(rows) else None
    return r["person"] if r is not None and r["person"] and not r["mixed"] and not r["parts"] else None


def _touches(lines: Sequence[TextSpan], rows: Sequence[LineWords], j: int, i: int, main: str) -> bool:
    """Line j is the main speaker's own and meets line i (INTERJECTION_GAP_MS)."""
    if _single(rows, j) != main:
        return False
    a, b = (j, i) if j < i else (i, j)
    return int(lines[b]["start_ms"]) - int(lines[a]["end_ms"]) <= INTERJECTION_GAP_MS


def _line_main(i: int, lines: Sequence[TextSpan], rows: Sequence[LineWords], key: str,
               who: Sequence[str | None]) -> tuple[str, list[int]] | None:
    """(main speaker, phrase edges) of line i: the whole line between the main speaker's own lines, or who holds
    MAIN_MIN_SHARE of its placed content letters with its phrase breaks; None when the line is nobody's sentence."""
    count: dict[str, int] = {}
    for k, ch in enumerate(key):
        if ch not in FILLERS and who[k]:
            count[cast(str, who[k])] = count.get(cast(str, who[k]), 0) + 1
    whole = _single(rows, i - 1)
    if whole and whole == _single(rows, i + 1) and _touches(lines, rows, i - 1, i, whole) \
            and _touches(lines, rows, i + 1, i, whole) and not any(w == whole for w in who):
        return whole, [0, len(key)]
    if not count or rows[i]["parts"] or rows[i]["mixed"]:
        return None
    main = max(count, key=count.__getitem__)
    if count[main] < MAIN_MIN_SHARE * sum(count.values()) or count[main] < PART_MIN_LETTERS:
        return None
    return main, [0, *(k for k, _ in _breaks(lines[i]["text"], 0, len(key) - 1)), len(key)]


def _phrase(i: int, a: int, b: int, main: str, lines: Sequence[TextSpan], rows: Sequence[LineWords], key: str,
            pos: Sequence[int], who: Sequence[str | None], times: Sequence[Sequence[tuple[int, int] | None]],
            segments: Sequence[EngineSegment], persons: Mapping[str, str]) -> Interjection | None:
    """The letters [a, b) of line i as a candidate (interjections), or None."""
    time = times[i]
    n = sum(1 for k in range(a, b) if key[k] not in FILLERS)
    if any(who[k] == main for k in range(a, b)) or n > INTERJECTION_MAX_LETTERS:
        return None
    before = [k for k in range(a) if who[k] == main]
    after = [k for k in range(b, len(key)) if who[k] == main]
    if not (before or _touches(lines, rows, i - 1, i, main)) or not (after or _touches(lines, rows, i + 1, i, main)):
        return None
    c0, c1, text = int(lines[i]["start_ms"]), int(lines[i]["end_ms"]), lines[i]["text"]
    # its time: from the main speaker's last letter before it (its head) to their first after it, or beyond the
    # line's edge into the main speaker's own line touching it
    t0 = min(time[before[-1]][1], time[before[-1]][0] + LETTER_HEAD_MS) if before else c0 - INTERJECTION_GAP_MS  # type: ignore[index]
    t1 = time[after[0]][0] if after else c1 + INTERJECTION_GAP_MS                                              # type: ignore[index]
    by = _someone_else(min(t0, t1), max(t0, t1), main, segments, persons)
    if by is None:
        return None
    whole = (a, b) == (0, len(key))
    first = next((t[0] for t in time[a:b] if t is not None), None)
    cut = None if (a and b < len(key)) or whole else first if a else time[after[0]][0]           # type: ignore[index]
    if cut is not None and not c0 + SPAN_MIN_MS <= cut <= c1 - SPAN_MIN_MS:
        cut = None
    own = [t for t in time[a:b] if t is not None]
    # when it is said: its letters (each on its first LETTER_HEAD_MS), else the line (whole) or the gap in the main
    # speaker's words it fills
    o0, o1 = ((own[0][0], max(min(t[1], t[0] + LETTER_HEAD_MS) for t in own)) if own else (c0, c1) if whole
              else (min(t0, t1), max(t0, t1)))
    together = _held(o0, o1, main, segments, persons) >= SPAN_MIN_MS or (not own and _no_room(i, a, b, n, times))
    if not together and cut is None:
        return None
    told = "、".join(f"「{ch}」{w or '没对上或没听到'}" for ch, w in zip(key, who, strict=True))
    return {"line": i, "main": main, "by": by, "at": (t0, t1), "text": text[pos[a]:pos[b - 1] + 1],
            "span": (pos[a], pos[b] if b < len(key) else len(text)), "whole": whole, "cut": cut, "together": together,
            "heard": f"{_heard(min(t0, t1), max(t0, t1), segments, persons)}；字：{told}"}


def _no_room(i: int, a: int, b: int, n: int, times: Sequence[Sequence[tuple[int, int] | None]]) -> bool:
    """The recognised words just before and just after letters [a, b) of line i (in its line, else in the lines around
    it) leave less than UNTIMED_LETTER_MS for each of its n content letters: the recognitions heard someone talk on
    through it."""
    before = [t for t in times[i][:a] if t is not None] or ([t for t in times[i - 1] if t is not None] if i else [])
    after = [t for t in times[i][b:] if t is not None] or ([t for t in times[i + 1] if t is not None] if i + 1 < len(times) else [])
    if not before or not after:
        return False
    return after[0][0] - min(before[-1][1], before[-1][0] + LETTER_HEAD_MS) < n * UNTIMED_LETTER_MS


def _held(a: int, b: int, person: str, segments: Sequence[EngineSegment], persons: Mapping[str, str]) -> int:
    """How long `person` is heard in [a, b) (ms)."""
    return sum(max(0, min(b, int(seg["end_ms"])) - max(a, int(seg["start_ms"]))) for seg in segments
               if persons.get(str(seg["speaker"])) == person)


def _someone_else(a: int, b: int, main: str, segments: Sequence[EngineSegment], persons: Mapping[str, str]) -> str | None:
    """The named person other than `main` heard longest in [a, b), when that is SPAN_MIN_MS; else None."""
    held: dict[str, int] = {}
    for seg in segments:
        s, e = max(a, int(seg["start_ms"])), min(b, int(seg["end_ms"]))
        p = persons.get(str(seg["speaker"]))
        if e > s and p and p != main:
            held[p] = held.get(p, 0) + e - s
    top = max(held, key=held.__getitem__, default="")
    return top if top and held[top] >= SPAN_MIN_MS else None


def load(clip_dir: Path) -> Evidence | None:
    """The clip's arrival evidence as step 4 recorded it, or None."""
    try:
        doc = json.loads((clip_dir / PATH).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return cast(Evidence, doc) if isinstance(doc, dict) and doc.get("segments") else None


def _recognition_dirs(ctx: Ctx) -> list[tuple[Path, str]]:
    """(local, media host) clip directories that may keep step 3's recognition of this cut: the clip's own, then those
    of the clips it is a revision of, nearest first.  A revision takes step 3 from the clip it revises and only cut/
    is copied on the host (revise_run), so its own directory there has no asr/."""
    from .state import ClipState, StateError

    task = ctx.clip_dir.parent.parent
    out, origin = [(ctx.clip_dir, ctx.remote_dir)], ctx.state.data.get("revised_from")
    while isinstance(origin, dict) and origin.get("cid") and str(origin["cid"]) not in {d.name for d, _ in out}:
        cid = str(origin["cid"])
        try:
            state = ClipState.load(task / "state" / f"{cid}.json")
        except (OSError, ValueError, StateError):
            break
        out.append((task / "clips" / cid, str(state.data["clip"]["remote_dir"])))
        origin = state.data.get("revised_from")
    return out


def clip_letters(ctx: Ctx) -> tuple[dict[str, list[list[str | int]]], list[str]]:
    """The clip's recognised letters by recogniser (RECOGNISERS), and why a recogniser has none.  Step 3 keeps each
    whole-clip recognition on the media host as asr/<name>.json beside the asr/<name>.srt it made from it; it is fetched
    here (from the clip's directory, or a revision's predecessors': _recognition_dirs) when this directory lacks it.
    A trim's directory has only its own recognition's SRT, and a file whose sentences are not this clip's SRT's is
    another recognition: not used."""
    import shutil

    from .srt_text import parse_srt
    from .step_io import _local

    out: dict[str, list[list[str | int]]] = {}
    why: list[str] = []
    dirs = _recognition_dirs(ctx)
    for name in RECOGNISERS:
        local, srt = _local(ctx, f"asr/{name}.json"), ctx.clip_dir / f"asr/{name}.srt"
        for kept, remote in dirs:
            if local.exists():
                break
            if (kept / f"asr/{name}.json").is_file():
                shutil.copyfile(kept / f"asr/{name}.json", local)
                break
            try:
                ctx.adapters.remote.get(f"{remote}/asr/{name}.json", local)
            except (OSError, RuntimeError):
                local.unlink(missing_ok=True)
        if not local.exists():
            why.append(f"{name} 没有逐字时间文件")
            continue
        doc = json.loads(local.read_text(encoding="utf-8"))
        starts = [round(c["start_s"] * 1000) for c in parse_srt(srt.read_text(encoding="utf-8"))] if srt.exists() else None
        if starts != [int(u["start_time"]) for u in doc["utterances"]]:
            why.append(f"{name} 的逐字时间文件和本片的识别稿（asr/{name}.srt）不是同一次识别")
            continue
        out[name] = recognised_letters(doc)
    return out, why


def _recordings(ctx: Ctx, guests: Sequence[str]) -> list[Recording]:
    """The guests' recordings of the clip's broadcast day on the media host (found and fetched once per day)."""
    from .step_io import _run_remote

    out_dir = f"{Path(ctx.remote_dir).parent}/guest-audio"
    cmd = (f"cd {REMOTE_REPO} && env {PROFILE_ENV} {REMOTE_PY} -B scripts/guest_recording_audio.py "
           f"--date {shlex.quote(str(ctx.session['recording_date']))} --out-dir {shlex.quote(out_dir)} "
           + " ".join(f"--guest {shlex.quote(g)}" for g in guests))
    res = _run_remote(ctx, "s4-guest-audio", cmd + "\n", timeout=3600)
    line = next((ln for ln in res.stdout.splitlines() if ln.startswith("GUEST_AUDIO ")), "")
    try:
        index = json.loads(line.removeprefix("GUEST_AUDIO "))
    except ValueError:
        return []
    return [{"owner": owner, "path": str(f["path"]), "bvid": str(f["bvid"])}
            for owner, rec in (index.get("guests") or {}).items() if owner in guests for f in rec.get("files") or []]


def _found_line(evidence: Evidence, host: str, guests: Sequence[str], kinds: Sequence[bool | None]) -> str:
    """The PROCESS line of a clip whose groups were given to people: the groups, what the voiceprint heard in each,
    and what the arrival leaves to the voiceprint."""
    who, centres, owner = evidence["persons"], evidence["centres_ms"], evidence["owner"]
    gaps = "、".join(f"{who.get(f'g{k}', '未定')} {c:+.0f} 毫秒" for k, c in enumerate(centres))
    heard = "、".join(f"{c:+.0f} 毫秒组 {h / 1000:.1f}:{o / 1000:.1f}" for c, (h, o) in zip(centres, evidence["heard_ms"], strict=True))
    line = (f"到达时间证据：本片在{owner}的公开录播（{evidence['bvid']}）里对上，同一句话到两边直播的时间差分成 "
            f"{len(centres)} 组（{gaps}；谁的直播里先出现就是谁说的），按它定每句是谁。各组是谁按声纹定："
            f"声纹有结论的句子里听成{host}和听成别人的秒数为 {heard}；最高一组偏{host}就是{host}、最低一组偏别人就是嘉宾那一路，"
            f"其余要一边占到三分之二（中间的组满 {GROUP_MIN_CONCLUDED_MS // 1000} 秒，最高组判成嘉宾、最低组判成{host}"
            f"满 {AGAINST_ORDER_MIN_CONCLUDED_MS // 1000} 秒）")
    if kinds.count(True) > 1:
        line += f"；{kinds.count(True)} 组都是{host}（同一个人，通话延迟中途变过）"
    if None in kinds:
        line += f"；{kinds.count(None)} 组声纹结论不够、没分给谁，主要落在这些组里的句子不按到达时间，照声纹和复核"
    if evidence["among"]:
        line += (f"；登记的嘉宾 {len(guests)} 位、定下来的嘉宾声音只有 {kinds.count(False)} 路，有人共用一路或没定下来（"
                 + "；".join(evidence["among"]) + f"）：到达时间只分{host}和这一路的嘉宾，"
                 "是哪一位按声纹在这几位之间分，分不出的不写名字")
    elif kinds[0] is False:
        line += f"；最低一组是录播主人{owner}那一路，同屋而没登记的人也在这一组里，到达时间分不出"
    return line


def _words_line(letters: Mapping[str, object], why: Sequence[str]) -> str:
    """What the PROCESS line says of the words' times."""
    names = {"bcut": "必剪逐字", "jianying": "剪映逐词"}
    if not letters:
        return f"；没有字的时间（{'；'.join(why)}），每句按它的整段时间里谁占八成以上（旧办法）"
    return (f"；每句按它的字落在谁说话的时间里定（字的时间：{'、'.join(names.get(n, n) for n in letters)}"
            + (f"；{'；'.join(why)}" if why else "") + "）")


def for_clip(ctx: Ctx, decided: Sequence[SpeakerDecision]) -> tuple[Evidence | None, str]:
    """The clip's arrival evidence (written to PATH) and the PROCESS line saying what was found; None when a guest's
    recording of the session was not found, the clip is not in it, it shows fewer than two groups, or the voiceprint
    gives no group to anyone.  `decided` are step 4's voiceprint rows of the clip's lines (speaker_v8.cue_speakers):
    they say whose each group is."""
    from .step_io import _local, _run_remote

    participants = [list(g) for g in ctx.session.get("participants") or []]
    local = _local(ctx, PATH)
    local.unlink(missing_ok=True)
    if len(participants) < 2 or people(ctx.session) != len(participants):
        return None, ""     # an unnamed guest (连线) or a recorded speaker_count: the people are not the named groups
    host, guests = participants[0][0], [g[0] for g in participants[1:]]
    recordings = _recordings(ctx, guests)
    if not recordings:
        return None, "到达时间证据：没找到嘉宾当天的公开录播，说话人照旧按声纹"
    rd, clip = ctx.remote_dir, ctx.state.data["clip"]
    source = f"{clip['source_dir']}/{clip['source_name']}" if clip.get("source_dir") and clip.get("source_name") else ""
    tried = []
    for rec in recordings:
        out = f"{rd}/speaker/arrival-{rec['bvid']}.json"
        cmd = (f"cd {REMOTE_REPO} && {REMOTE_PY} -B scripts/arrival_speakers.py --cut {rd}/cut/cut.mp4 "
               f"--guest {shlex.quote(rec['path'])} --out {out} --people {len(participants)}"
               + (f" --source {shlex.quote(source)} --start-s {int(clip['start_ms']) / 1000:.3f} "
                  f"--end-s {int(clip['end_ms']) / 1000:.3f}" if source else ""))
        res = _run_remote(ctx, "s4-arrival", f"mkdir -p {rd}/speaker\n{cmd}\n", timeout=3600)
        raw = _local(ctx, f"speaker/arrival-{rec['bvid']}.raw.json")
        if "ARRIVAL " not in res.stdout:
            tried.append(f"{rec['bvid']} 工具失败")
            continue
        ctx.adapters.remote.get(out, raw)
        doc = json.loads(raw.read_text(encoding="utf-8"))
        centres = [float(c) for c in doc.get("centres_ms") or []]
        if doc.get("status") != "OK":      # NOT_FOUND, or FEW_GROUPS: fewer than two groups
            tried.append(f"{rec['bvid']} 里没找到这一段" if doc.get("status") == "NOT_FOUND" else
                         f"{rec['bvid']} 里只量到 {len(centres)} 组时间差（全片只有一边在说话时就是一组，要两组才分得出人）")
            continue
        voice = host_time(doc["runs"], len(centres), decided)
        kinds = host_groups(voice)
        if all(kind is None for kind in kinds):
            tried.append(f"{rec['bvid']} 里量到 {len(centres)} 组时间差，但每组的声纹结论都不够定它是谁（听成{host}:听成别人的秒数 "
                         + "、".join(f"{h / 1000:.1f}:{o / 1000:.1f}" for h, o in voice) + "）")
            continue
        persons, among = group_persons(kinds, host, rec["owner"], guests)
        segments, named, who = engine(doc, persons, host)
        letters, no_letters = clip_letters(ctx)
        evidence: Evidence = {"segments": segments, "named": named, "persons": who, "owner": rec["owner"], "bvid": rec["bvid"],
                              "centres_ms": centres, "among": among, "heard_ms": [[round(h), round(o)] for h, o in voice],
                              "letters": letters}
        local.write_text(json.dumps(evidence, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        return evidence, _found_line(evidence, host, guests, kinds) + _words_line(letters, no_letters)
    return None, "到达时间证据：没用上（" + "；".join(tried) + "），说话人照旧按声纹"
