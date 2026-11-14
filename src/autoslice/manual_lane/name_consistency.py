"""One name, one spelling, in one clip (TASK-36; COACH-ADVICE e05 §5.1, §5.4, §5.6, §5.8; 维护者, U169).

The doubt pass decides one sentence at a time, in batches of ten, and a batch never sees how an earlier batch wrote a
name.  So one clip could write the same name three ways: a table name in one sentence, a near-homophone of it in the
next, a fan's spelling of a player name in two variants.  e05 §5.6 is the rule this pass exists for — 同一片里同一个名字
只用一种写法 — and the hand push did it last, after the words were decided.

What each side does (U169: what code can do stays in code, judgement goes to CPA):

- code: after the words are decided, scan the clip's decided subtitles and group every run of two to five Chinese
  characters whose sound is the same (name_match.sound_key: toneless pinyin, zh/z ch/c sh/s n/l, in/ing en/eng merged,
  the rule `near_names` already uses).  A Latin word joins such a group when its letters are the initials of that sound
  one per syllable (e05 §5.4's second matching rule), so `ntnt` and 南亭南亭 are one group.  Groups with one spelling
  are not touched and cost no CPA call.
- code: the spelling a group must use, where a written source already fixes it — the proper-noun table first
  (§5.1), so a group whose sound is a table entry's is written as the table writes it (its note included).
- CPA: one call per clip, every group at once: is this group a name at all, and which of its places really are that
  name.  Only the places CPA names are changed; a homophone that is an ordinary word in its sentence is left alone
  (§5.4: no global string replacement, 41-semantic-repair 第11条).  The spelling CPA chooses must already exist — the
  table, the danmaku, the title, or one of the group's own spellings; a spelling it invents from the sound is refused
  (§5.4, §5.8).  A group it cannot settle is left as it is and goes to the 未定专名 list (§5.7).

The pass never stops the clip: a failed call or an answer that does not parse leaves every sentence as it was, with the
groups recorded.  Every change is written to subs/name_consistency.json (old spelling → new spelling, the group, where
the spelling came from, CPA's reason) and the summary to PROCESS.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, TypedDict, cast

from . import name_match
from .lane_rules import rules
from .srt_text import _json_from_text

if TYPE_CHECKING:
    from src.autoslice.cpa_gemini_client import JsonValue
    from .doubt_policy import FinalDoubt
    from .runner import Ctx
    from .state import CpaEvent

CALL_ID = "name-consistency"
RECORD = "subs/name_consistency.json"
_HAN = re.compile(r"[一-鿿]+")
_LATIN_WORD = re.compile(r"[A-Za-z][A-Za-z0-9]*")
# a Latin run of one or more words, as a note writes a spelling (Shadow Lee is one name)
_LATIN_PHRASE = re.compile(r"[A-Za-z][A-Za-z0-9]*(?:[ ][A-Za-z][A-Za-z0-9]*)*")

# What CPA may answer per group (the prompt asks for this shape); anything else is no answer.
FIELDS = ("group", "is_name", "spelling", "places", "reason")

NameFix = TypedDict("NameFix", {"from": str, "to": str, "group": str, "place": str, "start": int, "end": int,
                                "spelling_source": str, "reason": str})
CueId = int | str | None


class NameChange(NameFix):
    cue_id: CueId
    doubt_id: str | None


class PlaceRecord(TypedDict):
    id: str
    cue_id: CueId
    start: int
    end: int
    spelling: str


class GroupRecord(TypedDict):
    group: str
    sound: str
    spellings: dict[str, int]
    table: list[str]
    fixed: str
    chosen: str
    decision: str
    reason: str
    asked: bool
    places: list[PlaceRecord]


class PlaceView(TypedDict):
    id: str
    cue_id: CueId
    sentence: str
    written: str
    previous: str | None
    next: str | None
    readings: dict[str, str]
    danmaku: list[str]


class NameAnswer(TypedDict, total=False):
    group: JsonValue
    is_name: JsonValue
    spelling: JsonValue
    places: list[JsonValue] | str | None
    reason: JsonValue


class NameRecord(TypedDict):
    enabled: bool
    groups: list[Group]
    changes: list[NameChange]
    undecided: list[str]
    cpa_call: CpaEvent | None
    cpa_error: str


@dataclass
class Place:
    """One occurrence of one spelling of a group, in one sentence's decided text."""
    pid: str
    cue_id: CueId
    start: int
    end: int
    spelling: str


@dataclass
class Group:
    """Every spelling of one sound in this clip, with the places each stands at."""
    gid: str
    sound: str                       # "py:nán-tíng" for Chinese, "in:n-t-n-t" for a Latin word
    key: tuple[str, ...]             # the blurred pinyin syllables, or the letters of a Latin word (lower case)
    syllables: tuple[str, ...] = ()  # the unblurred pinyin syllables (a Latin spelling matches their initials)
    spellings: dict[str, list[Place]] = field(default_factory=dict)
    table: list[name_match.NameRow] = field(default_factory=list)   # table rows whose sound is this group's
    fixed: str = ""                  # the spelling a written source fixes (the table's), else ""
    chosen: str = ""                 # what is written in the end
    decision: str = ""               # table | cpa | ordinary | unresolved | cpa_failed | overlap
    reason: str = ""
    asked: bool = False              # sent to CPA

    @property
    def counts(self) -> dict[str, int]:
        return {s: len(p) for s, p in self.spellings.items()}

    @property
    def total(self) -> int:
        return sum(len(p) for p in self.spellings.values())

    def as_record(self) -> GroupRecord:
        return {"group": self.gid, "sound": self.sound, "spellings": self.counts,
                "table": [r["name"] for r in self.table], "fixed": self.fixed, "chosen": self.chosen,
                "decision": self.decision, "reason": self.reason, "asked": self.asked,
                "places": [{"id": p.pid, "cue_id": p.cue_id, "start": p.start, "end": p.end, "spelling": p.spelling}
                           for p in sorted((x for ps in self.spellings.values() for x in ps),
                                           key=lambda p: p.pid)]}


# ----- grouping (code) ----------------------------------------------------------------------------------------------
def _han_runs(text: str) -> list[tuple[int, str]]:
    return [(m.start(), m.group()) for m in _HAN.finditer(text)]


def _sound_of(text: str, *, min_syllables: int, max_syllables: int
              ) -> list[tuple[int, int, str, str, tuple[str, ...], tuple[str, ...]]]:
    """(start, end, spelling, sound id, key, raw syllables) of every Chinese run of min..max characters whose syllables
    are known.  `key` is the blurred comparison; the raw syllables are only used for a Latin word's initials, where the
    letters are what was said (n is n in `ntnt`, even though n and l are one sound for the Chinese comparison)."""
    out = []
    for offset, run in _han_runs(text):
        raw = name_match.raw_pinyin(run)
        for n in range(min_syllables, min(max_syllables, len(run)) + 1):
            for i in range(len(run) - n + 1):
                piece = run[i:i + n]
                key = name_match.sound_key(piece)
                if key is None or len(key) != n:
                    continue
                syllables = tuple(raw[i:i + n]) if raw and len(raw) == len(run) else key
                out.append((offset + i, offset + i + n, piece, "py:" + "-".join(key), key, syllables))
    return out


def _initials(syllables: Sequence[str]) -> tuple[str, ...]:
    """The initial of each syllable, as e05 §5.4 compares a Latin spelling with spoken syllables (`ntnt` against
    南亭南亭 is n t n t)."""
    return tuple(str(s)[:1] for s in syllables)


def _latin_sound(word: str) -> tuple[str, ...]:
    return tuple(ch for ch in word.lower() if ch.isalnum())


def _latin_table_groups(texts: Mapping[CueId, str]) -> list[Group]:
    """One group per proper-noun row spelled in another script in this clip: `Shadow Lee` where the clip also writes a
    Chinese name of that row, `lds` where it writes 李豆沙.

    Sound grouping cannot join them (the letters are not that name's initials), and e05 §5.1 says it plainly: an alias
    in another script, spoken in a Chinese sentence, is written the table's (or its note's) Chinese way — with §5.6 the
    same name is written one way in the whole clip.  Which row owns the spelling: a row whose note names it wins over a
    plain alias list (the note is the table's own statement about that spelling, §5.1), else the row that lists it as an
    alias.  Two neighbouring rules bound this:
    - the row's name must be Chinese.  A name that already mixes scripts (雾深Girimi) is not this case: §5.1 says a
      speaker who says only the letters of such a name is written as said (Girimi), not completed to 雾深.
    - a Chinese *alias* (小李, 豆沙) is never a target here: §5.1 says a short form the speaker said is written as said,
      so those stay their own sound groups.

    Returns one group per owning row: the places of its Latin spelling plus the places of its Chinese name, `fixed` =
    that name (what the Latin spelling is written as)."""
    rows = name_match.table_rows()
    latin_seen: dict[str, list[Place]] = {}
    # the spellings of a row in another script: its Latin aliases, and the Latin words its note names (§5.1: the note is
    # the table's own statement about how that spelling is written in Chinese)
    by_row_alias: dict[str, list[str]] = {}
    for row in rows:
        name = row["name"]
        if not _HAN.search(name) or _LATIN_WORD.search(name):
            continue                  # not a Chinese name, or one that already mixes scripts (written as said)
        spellings = [a for a in row["aliases"] if _LATIN_WORD.search(a)]
        spellings += [m.group() for m in _LATIN_PHRASE.finditer(row["note"])]
        if spellings:
            by_row_alias[name] = spellings
    alias_words = sorted({s for v in by_row_alias.values() for s in v}, key=len, reverse=True)
    for cue_id, text in texts.items():
        hits: list[tuple[int, int, str]] = []
        for alias in alias_words:
            pattern = r"\s*".join(re.escape(w) for w in alias.split())
            hits += [(m.start(), m.end(), m.group()) for m in re.finditer(pattern, str(text), flags=re.IGNORECASE)]
        taken: list[tuple[int, int]] = []
        for start, end, word in sorted(hits, key=lambda f: (-(f[1] - f[0]), f[0])):
            if any(not (end <= a or start >= b) for a, b in taken):
                continue                       # a longer alias covers these letters (Shadow Lee, not Lee)
            taken.append((start, end))
            latin_seen.setdefault(word, []).append(Place("", cue_id, start, end, word))
    by_row: dict[str, dict[str, list[Place]]] = {}
    for word, places in latin_seen.items():
        owner = next((r for r in rows if word.lower() in r["note"].lower() and _HAN.search(r["name"])), None)
        owner = owner or next((r for r in rows
                               if any(word.lower() == a.lower() for a in r["aliases"]) and _HAN.search(r["name"])
                               and not _LATIN_WORD.search(r["name"])), None)
        if owner is None:
            continue
        by_row.setdefault(owner["name"], {}).setdefault(word, []).extend(places)
    out = []
    for row in rows:
        latin_places = by_row.get(row["name"]) or {}
        if not latin_places or _LATIN_WORD.search(row["name"]):
            continue
        name = row["name"]
        han_places: dict[str, list[Place]] = {}
        for cue_id, text in texts.items():
            for match in re.finditer(re.escape(name), str(text)):
                han_places.setdefault(name, []).append(Place("", cue_id, match.start(), match.end(), name))
        key = name_match.sound_key(name) or (name,)
        group = Group(gid="", sound=_sound_id(key), key=key, syllables=key, table=[row], fixed=name)
        group.spellings.update(han_places)
        group.spellings.update(latin_places)
        out.append(group)
    return out


def _merge(by_key: dict[tuple[str, ...], Group], extra: Group) -> None:
    """Add `extra`'s places to the group of its sound, or make that group `extra` (the table's row rides along).  A
    place already in the group (the same cue, the same span) is not added twice."""
    target = by_key.get(extra.key) or by_key.setdefault(extra.key, extra)
    for spelling, places in extra.spellings.items():
        have = {(p.cue_id, p.start, p.end) for p in target.spellings.get(spelling, [])}
        target.spellings.setdefault(spelling, []).extend(
            [p for p in places if (p.cue_id, p.start, p.end) not in have])
    if extra.table and not target.table:
        target.table, target.fixed = extra.table, extra.fixed


def _nested(short: Group, long_: Group) -> bool:
    """True when `short`'s sound is a contiguous run of `long_`'s syllables and every place of `short` sits inside a place
    of `long_`: then the two groups are one name seen at two lengths (莉亚 and 奈奈莉亚)."""
    n = len(short.key)
    if len(short.key) >= len(long_.key) or not any(long_.key[i:i + n] == short.key
                                                   for i in range(len(long_.key) - n + 1)):
        return False
    long_spans: dict[CueId, list[tuple[int, int]]] = {}
    for ps in long_.spellings.values():
        for p in ps:
            long_spans.setdefault(p.cue_id, []).append((p.start, p.end))
    return all(any(p.start >= a and p.end <= b for a, b in long_spans.get(p.cue_id, []))
               for ps in short.spellings.values() for p in ps)


def _drop_nested(groups: list[Group]) -> list[Group]:
    """One group per name: of two nested groups, the table's length wins (a table entry fixes the name's length, §5.1);
    otherwise the fuller spelling wins when it has two spellings too, else the one that has them."""
    dropped: set[int] = set()
    for i, a in enumerate(groups):
        for j, b in enumerate(groups):
            if i == j or i in dropped or j in dropped:
                continue
            short, long_ = (a, b) if len(a.key) < len(b.key) else (b, a)
            if not _nested(short, long_):
                continue
            keep_long = (short.fixed and not long_.fixed) or (not long_.fixed and len(long_.spellings) < 2)
            dropped.add(groups.index(long_ if keep_long else short))
    return [g for k, g in enumerate(groups) if k not in dropped]


def _sound_id(syllables: Sequence[str]) -> str:
    return "py:" + "-".join(str(s) for s in syllables)


def scan(texts: Mapping[CueId, str], *, min_syllables: int, max_syllables: int) -> list[Group]:
    """The sound groups of the clip's decided texts, each with every place its spellings stand at, in cue order.

    A group is kept when it has two or more spellings in the clip, or when the proper-noun table writes this sound some
    other way than the clip does (then the table's spelling is the second one, e05 §5.1).  A Latin word joins the
    Chinese group whose syllables' initials are its letters (§5.4); a Latin word with no such group is a group of its
    own, so two spellings of one Latin name still group (`ntnt`, `NtNt`).  Of two groups where one's sound sits inside
    the other's and all its places inside the other's places, one is dropped (`_drop_nested`): one name, one group, and
    no place is asked about twice.  One spelling and no table entry: not a group, nothing to do, no CPA call."""
    by_sound: dict[tuple[str, ...], Group] = {}
    order: list[CueId] = list(texts)
    for cue_id in order:
        text = str(texts[cue_id])
        for start, end, piece, _sound, key, syllables in _sound_of(text, min_syllables=min_syllables,
                                                                   max_syllables=max_syllables):
            group = by_sound.setdefault(key, Group(gid="", sound=_sound_id(syllables), key=key, syllables=syllables))
            group.spellings.setdefault(piece, []).append(Place("", cue_id, start, end, piece))
    # a Latin spelling of a Chinese sound: the letters are the initials, one per syllable
    latin_of_sound = {_initials(g.syllables or g.key): g for g in by_sound.values()}
    for cue_id in order:
        text = str(texts[cue_id])
        for match in _LATIN_WORD.finditer(text):
            word = match.group()
            letters = _latin_sound(word)
            matched_group = latin_of_sound.get(letters)
            if matched_group is None:
                matched_group = by_sound.setdefault(letters, Group(gid="", sound="in:" + "-".join(letters), key=letters,
                                                                   syllables=letters))
            matched_group.spellings.setdefault(word, []).append(Place("", cue_id, match.start(), match.end(), word))
    for extra in _latin_table_groups(texts):
        _merge(by_sound, extra)
    out = []
    for group in by_sound.values():
        if not group.table:
            group.table = _table_for(group)
            group.fixed = group.fixed or _table_spelling(group, group.table)
        if len(group.spellings) < 2 and (not group.fixed or set(group.spellings) == {group.fixed}):
            continue
        out.append(group)
    out = _drop_nested(out)
    places = sorted((p for g in out for ps in g.spellings.values() for p in ps),
                    key=lambda p: (str(p.cue_id).zfill(8), p.start))
    pid = {id(p): i for i, p in enumerate(places, 1)}
    for i, group in enumerate(out, 1):
        group.gid = f"g{i:02d}"
        for ps in group.spellings.values():
            for p in ps:
                p.pid = f"p{pid[id(p)]:03d}"
    return out


def _table_for(group: Group) -> list[name_match.NameRow]:
    """The proper-noun rows this group's sound is: a Chinese key equal to the row's name or alias, or (a Latin group) a
    row spelling the same letters.  The table comes first in e05 §5.1, so its spelling is the one the group must use."""
    rows = []
    for row in name_match.table_rows():
        spellings = (row["name"], *row["aliases"])
        if group.sound.startswith("py:"):
            if any(name_match.sound_key(s) == group.key for s in spellings if _HAN.search(s)):
                rows.append(row)
        elif any(_latin_sound(s) == group.key for s in spellings):
            rows.append(row)
    return rows


def _table_spelling(group: Group, rows: Sequence[name_match.NameRow]) -> str:
    """The table's own spelling of this sound: the name or alias whose sound is the group's, shortest first (an alias is
    what the speaker said when they said only the short form, §5.1: 按所说写)."""
    want = _table_spellings(group, rows)
    return min(want, key=lambda s: (len(_HAN.findall(s)[0]) if _HAN.search(s) else 99, len(s))) if want else ""


def _table_spellings(group: Group, rows: Sequence[name_match.NameRow]) -> list[str]:
    """Every table spelling of this group's sound (the name and the aliases alike)."""
    latin = not group.sound.startswith("py:")
    out = []
    for row in rows:
        for spelling in (row["name"], *row["aliases"]):
            same = _latin_sound(spelling) == group.key if latin else name_match.sound_key(spelling) == group.key
            if same:
                out.append(str(spelling))
    return out


def _written_sources(pool: str, title: str) -> str:
    return f"{pool}\n{title}"


def _allowed(group: Group, sources: str) -> set[str]:
    """The spellings this group may be written with (e05 §5.4): the table's, its own, and any spelling of the same sound
    the danmaku or the title carries.  Nothing invented from the sound is in this set."""
    out = set(group.spellings) | set(_table_spellings(group, group.table))
    if group.sound.startswith("py:"):
        for _offset, run in _han_runs(sources):
            for i in range(len(run) - len(group.key) + 1):
                piece = run[i:i + len(group.key)]
                if name_match.sound_key(piece) == group.key:
                    out.add(piece)
    else:
        for match in _LATIN_WORD.finditer(sources):
            if _latin_sound(match.group()) == group.key:
                out.add(match.group())
    return out


# ----- the decided texts, and writing a choice back ----------------------------------------------------------------
DECIDED_STATES = ("KEEP", "ADOPT", "FIT_CONTEXT")


def decided_text(doubt: FinalDoubt) -> str:
    """The text this doubt puts in the subtitles (before normalisation): CPA's words for an applied answer, else the
    cue's own words.  Empty for a dropped sentence (TASK-32) and for a row no writer applies."""
    if doubt.get("state") not in DECIDED_STATES:
        return ""
    if doubt.get("state") in {"ADOPT", "FIT_CONTEXT"} and str(doubt.get("final_text") or "").strip():
        return str(doubt["final_text"])
    return str(doubt.get("current") or "")


def decided_texts(doubts: Sequence[FinalDoubt]) -> dict[CueId, str]:
    return {d.get("cue_id"): decided_text(d) for d in doubts if decided_text(d)}


def _rewrite(doubt: FinalDoubt, text: str, fixes: Sequence[NameFix]) -> None:
    """Put the rewritten text on the doubt.  The writer applies `final_text` only for ADOPT / FIT_CONTEXT
    (step3_adjudicate._final_cues), so a KEEP row whose words change becomes ADOPT with those words: CPA's decision was
    the words, and they are these words with one name written the one way.  The fixes themselves are kept on the row."""
    doubt["final_text"] = text
    if doubt.get("state") == "KEEP":
        doubt["state"] = "ADOPT"
    doubt.setdefault("name_fix", [])
    doubt["name_fix"] = [*doubt["name_fix"], *fixes]


# ----- the prompt ---------------------------------------------------------------------------------------------------
def _place_view(place: Place, texts: Mapping[CueId, str], doubts_by_cue: Mapping[CueId, FinalDoubt],
                order: Sequence[CueId]) -> PlaceView:
    text = texts.get(place.cue_id, "")
    index = next((i for i, c in enumerate(order) if str(c) == str(place.cue_id)), None)
    prev = texts.get(order[index - 1]) if index else ""
    nxt = texts.get(order[index + 1]) if index is not None and index + 1 < len(order) else ""
    doubt = doubts_by_cue.get(place.cue_id) or cast("FinalDoubt", {})
    readings = {k: v for k, v in (doubt.get("readings") or {}).items() if k != "bcut"}
    return {"id": place.pid, "cue_id": place.cue_id, "sentence": text,
            "written": place.spelling, "previous": prev, "next": nxt,
            "readings": readings, "danmaku": list(doubt.get("danmaku") or [])[:6]}


def build_prompt(groups: Sequence[Group], texts: Mapping[CueId, str], doubts_by_cue: Mapping[CueId, FinalDoubt],
                 cue_order: Sequence[CueId], sources: str, title: str, table: Sequence[name_match.NameRow]) -> str:
    from .step_io import _render

    order = [c for c in cue_order if c in texts] or list(texts)
    rows = []
    for group in groups:
        group.asked = True
        places = sorted((x for ps in group.spellings.values() for x in ps), key=lambda p: p.pid)
        rows.append({"group": group.gid, "same_sound": "、".join(f"{s}×{n}" for s, n in group.counts.items()),
                     "table": [{"name": r["name"], "aliases": list(r["aliases"]), "role": r["role"], "note": r["note"]}
                               for r in group.table] or "无",
                     "fixed_by_source": group.fixed,
                     "may_write": sorted(_allowed(group, sources)),
                     "places": [_place_view(p, texts, doubts_by_cue, order) for p in places]})
    return _render("name_consistency.md", groups=json.dumps(rows, ensure_ascii=False, indent=1),
                   table=json.dumps([{"name": r["name"], "aliases": list(r["aliases"]), "role": r["role"],
                                      "note": r["note"]} for r in table], ensure_ascii=False, indent=1),
                   title=title or "（无）", written_sources="\n".join(sources.splitlines()[:400]) or "（无）")


# ----- CPA's answer -------------------------------------------------------------------------------------------------
def parse_answers(text: str) -> dict[str, NameAnswer]:
    """CPA's rows by group id.  An answer the step cannot read is no answer (the caller keeps every sentence)."""
    rows = _json_from_text(text)
    if not isinstance(rows, list):
        raise ValueError("name consistency answer is not a JSON array")
    out: dict[str, NameAnswer] = {}
    for row in rows:
        if isinstance(row, dict) and row.get("group"):
            out[str(row["group"])] = cast(NameAnswer, row)
    return out


def _chosen_spelling(group: Group, row: NameAnswer, sources: str) -> tuple[str, str]:
    """(spelling, why) from CPA's row.  The table wins where it has this sound (§5.1: the table and its note come
    first, so CPA's own choice does not override it); otherwise CPA's spelling must already exist in a written source
    (§5.4).  ("", reason) when the group cannot be settled."""
    asked = str(row.get("spelling") or "").strip()
    if group.fixed:
        note = next((r["note"] for r in group.table if r["note"]), "")
        why = f"专名表写法「{group.fixed}」" + (f"（note：{note}）" if note else "") + "（e05 §5.1/§5.6，不由 CPA 另选）"
        said = str(row.get("reason") or "").strip()
        return group.fixed, why + (f"；CPA：{said}" if said else "")
    if not asked:
        return "", "CPA 没有给出写法"
    allowed = _allowed(group, sources)
    if asked not in allowed:
        return "", f"CPA 给的写法「{asked}」不在专名表、弹幕、标题或本组已有写法里（e05 §5.4：不许按读音自拼）"
    return asked, f"CPA 定「{asked}」"


def apply_answers(groups: Sequence[Group], answers: Mapping[str, NameAnswer], sources: str
                  ) -> tuple[dict[CueId, list[NameFix]], list[Group]]:
    """Describe the changes per cue id: [{from, to, group, place, start, end, spelling_source, reason}], non-overlapping
    (the longest place wins where two groups' places overlap).  Returns (changes, groups with no decision).

    The split e05 and the task book draw: CPA names the places that really are the name (nothing else is ever touched —
    a homophone that is an ordinary word in its sentence stays), and the spelling is one written source already has.
    Where the proper-noun table has this sound it fixes the spelling (its note included, §5.1) and CPA's answer only adds
    the reason; CPA still says which places are the name, so a phrase that merely sounds like the table's name is not
    rewritten on the table's word alone."""
    wanted: list[tuple[Group, Place, str, str]] = []
    undecided: list[Group] = []
    for group in groups:
        row = answers.get(group.gid)
        if not row:
            group.decision, group.reason = "unresolved", "CPA 没有回答这一组（留原样）"
            undecided.append(group)
            continue
        if row.get("is_name") is False:
            group.decision, group.reason = "ordinary", str(row.get("reason") or "CPA 判定不是名字")
            continue
        spelling, why = _chosen_spelling(group, row, sources)
        if not spelling:
            group.decision, group.reason = "unresolved", why
            undecided.append(group)
            continue
        group.chosen, group.decision, group.reason = spelling, ("table" if group.fixed else "cpa"), why
        named = {str(x) for x in (row.get("places") or [])}
        hits = [place for ps in group.spellings.values() for place in ps if place.pid in named]
        if not hits:      # CPA names no place: nothing is changed, and the group is not left open (it is settled)
            group.decision = "unchanged"
            group.reason = why + "；CPA 没有点名任何位置，全组留原样"
            continue
        wanted.extend((group, place, spelling, why) for place in hits)
    # one replacement per span: the longest place first, then the earlier one (a place inside a longer one is skipped)
    wanted.sort(key=lambda item: (-(item[1].end - item[1].start), str(item[1].cue_id).zfill(8), item[1].start))
    taken: dict[CueId, list[tuple[int, int]]] = {}
    changes: dict[CueId, list[NameFix]] = {}
    skipped: list[Group] = []
    for group, place, spelling, why in wanted:
        spans = taken.setdefault(place.cue_id, [])
        if any(not (place.end <= a or place.start >= b) for a, b in spans):
            group.decision, group.reason = "overlap", "与更长的同名位置重叠（只改长的那个）"
            skipped.append(group)
            continue
        spans.append((place.start, place.end))
        if spelling == place.spelling:
            continue
        changes.setdefault(place.cue_id, []).append(
            {"from": place.spelling, "to": spelling, "group": group.gid, "place": place.pid,
             "start": place.start, "end": place.end, "spelling_source": ("table" if group.fixed else "cpa"),
             "reason": why})
    for group in groups:
        if group.decision in {"", "overlap"}:
            group.decision = "overlap" if group.decision == "overlap" else "unchanged"
            group.reason = group.reason or "点到的位置本来就写成这一种"
    return changes, undecided


# ----- the pass -----------------------------------------------------------------------------------------------------
def danmaku_pool(clip_dir: Path) -> list[str]:
    """The clip's chat text, senders and gift names (`danmaku/lines.json`), one of the two written sources a
    spelling may come from (e05 §5.4).  Empty when the step wrote none."""
    path = clip_dir / "danmaku" / "lines.json"
    if not path.exists():
        return []
    try:
        from .recorder_events import evidence_texts

        return [text for row in json.loads(path.read_text(encoding="utf-8")) for text in evidence_texts(row)]
    except (OSError, ValueError, AttributeError, TypeError):
        return []


def resolve(ctx: Ctx, doubts: Sequence[FinalDoubt], cue_order: Sequence[CueId], *,
            ask: Callable[..., CpaEvent] | None = None) -> NameRecord:
    """One pass over one clip's decided subtitles: group the spellings of one sound, decide the spelling where a written
    source fixes it, ask CPA once about every group (which are names, and which places are), and apply only the places
    it names.  The doubt rows are changed in place — the caller's own list, so the four-state count, the 未定专名 list
    and doubts.json all describe the text that is written.  Returns the record."""
    from .step_io import _ask_cpa, _cpa_ok

    cfg = rules("name_consistency")
    record: NameRecord = {"enabled": bool(cfg.get("enabled", True)), "groups": [], "changes": [],
                              "undecided": [], "cpa_call": None, "cpa_error": ""}
    if not record["enabled"] or not doubts:
        return record
    texts = decided_texts(doubts)
    groups = scan(texts, min_syllables=int(cfg.get("min_syllables") or 2),
                  max_syllables=int(cfg.get("max_syllables") or 5))
    if not groups:
        return record
    record["groups"] = groups
    pool = danmaku_pool(ctx.clip_dir)
    title = str((ctx.state.data.get("clip") or {}).get("hook") or "")
    sources = _written_sources("\n".join(pool), title)
    doubts_by_cue: dict[CueId, FinalDoubt] = {d.get("cue_id"): d for d in doubts}
    prompt = build_prompt(groups, texts, doubts_by_cue, cue_order, sources, title, name_match.table_rows())
    reply = (ask or _ask_cpa)(ctx, CALL_ID, prompt)
    record["cpa_call"] = cast("CpaEvent", {k: v for k, v in reply.items() if k not in {"response_text", "http_log"}})
    answers: dict[str, NameAnswer] = {}
    if not _cpa_ok(reply):
        record["cpa_error"] = "CPA_CALL_FAILED"
    else:
        try:
            answers = parse_answers(reply["response_text"])
        except ValueError as exc:
            record["cpa_error"] = f"CPA_RESPONSE_UNPARSEABLE: {exc}"
    if record["cpa_error"]:      # every group is left as it is, and the step never stops on this pass
        for group in groups:
            group.decision, group.reason = "cpa_failed", "CPA 调用或回答失败（留原样）"
        record["undecided"] = [g.gid for g in groups]
        write_record(ctx.clip_dir, record)
        return record
    changes, undecided = apply_answers(groups, answers, sources)
    by_cue: dict[CueId, FinalDoubt] = {d.get("cue_id"): d for d in doubts}
    for cue_id, fixes in changes.items():
        doubt = by_cue.get(cue_id)
        if doubt is None:
            continue
        text = decided_text(doubt)
        for fix in sorted(fixes, key=lambda f: -f["start"]):        # right to left: offsets stay valid
            text = text[:fix["start"]] + fix["to"] + text[fix["end"]:]
        _rewrite(doubt, text, fixes)
        record["changes"].extend(cast(NameChange, dict(fix, cue_id=cue_id, doubt_id=doubt.get("id")))
                                 for fix in sorted(fixes, key=lambda f: f["start"]))
    record["undecided"] = [g.gid for g in undecided]
    write_record(ctx.clip_dir, record)
    return record


def write_record(clip_dir: Path, record: NameRecord) -> Path:
    """subs/name_consistency.json: every group with its spellings and how it was decided, every change (old spelling →
    new spelling, the group and the place, where the spelling came from, CPA's reason), the groups left open, and the
    CPA call (what was asked is under cpa/name-consistency.prompt.txt, what it said beside it)."""
    path = clip_dir / RECORD
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = {"groups": [g.as_record() for g in groups_of(record)], "changes": record.get("changes") or [],
           "undecided": record.get("undecided") or [], "cpa_call": record.get("cpa_call"),
           "cpa_error": record.get("cpa_error") or ""}
    path.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
    return path


# ----- PROCESS and the 未定专名 list --------------------------------------------------------------------------------
def groups_of(record: NameRecord) -> list[Group]:
    return [g for g in (record.get("groups") or []) if isinstance(g, Group)]


def summary_line(record: NameRecord) -> str:
    """PROCESS: how many groups, what each was decided as, and every change with where its spelling came from."""
    groups = groups_of(record)
    if not groups:
        return "同名统一写法（e05 §5.6，TASK-36）：全片没有同一读音的多写法，没有改名"
    counts: dict[str, int] = {}
    for group in groups:
        counts[group.decision or "unchanged"] = counts.get(group.decision or "unchanged", 0) + 1
    head = (f"同名统一写法（e05 §5.6，TASK-36）：读音相同的多写法 {len(groups)} 组"
            f"（专名表定 {counts.get('table', 0)}，CPA 定 {counts.get('cpa', 0)}，"
            f"CPA 判为普通词 {counts.get('ordinary', 0)}，定不下 {counts.get('unresolved', 0) + counts.get('cpa_failed', 0)}）；"
            f"改名 {len(record.get('changes') or [])} 处")
    if record.get("cpa_error"):
        head += f"；{record['cpa_error']}，全片未改"
    detail = "；".join(f"{c.get('cue_id')}「{c['from']}」→「{c['to']}」（{c['group']}，"
                      f"{'专名表' if c.get('spelling_source') == 'table' else 'CPA'}）"
                      for c in record.get("changes") or [])
    return head + ("：" + detail if detail else "")


def undecided_rows(record: NameRecord) -> list[name_match.UnresolvedSpelling]:
    """The groups CPA could not settle, as 未定专名 rows (e05 §5.7): each spelling with how often it stands."""
    out: list[name_match.UnresolvedSpelling] = []
    for group in groups_of(record):
        if group.gid not in (record.get("undecided") or []):
            continue
        for spelling, places in group.counts.items():
            out.append({"spelling": spelling, "count": places, "note": group.reason})
    return out
