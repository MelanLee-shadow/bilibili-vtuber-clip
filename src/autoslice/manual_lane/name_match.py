"""Names of the proper-noun table in a sentence (TASK-28; COACH-ADVICE e05 §1.1 第3/6条, §5.3, §5.5, §5.7).

The table (assets/manual_lane/proper_nouns.json, U117) and the session's participant groups give the names and their
aliases.  Three questions, all for detection and the record (no text is replaced here: CPA writes the name, U107; no
global string replacement, 41-semantic-repair 第11条):
- near_names: a run of the sentence that sounds like the Chinese part of a name (toneless pinyin, the initials and
  finals ASR confuses merged) without being written as that name;
- unknown_latin: Latin-letter words of the sentence (two letters or more, no common English short word) that are
  no name or alias of the table;
- unknown_spellings: of those, the ones that neither the table nor the clip's danmaku nor its title carries (the
  「未定专名」 list).

TASK-36: `sound_key` is that same pinyin comparison, exposed so the one-spelling-per-name pass
(name_consistency.py, e05 §5.6) groups two spellings of one name by exactly the rule this module already uses;
`table_rows` returns the table with each entry's note (the note is what decides the spelling where it has one).
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Sequence
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, TypedDict, cast

from ..acoustic_pinyin import text_pinyin_tokens
from .config import unknown_participant
from .lane_rules import rules

if TYPE_CHECKING:
    from src.autoslice.cpa_gemini_client import JsonValue
    from .lane_rules import DetectionRules


class NameRow(TypedDict):
    name: str
    aliases: tuple[str, ...]
    role: str
    note: str


class _NameInput(TypedDict, total=False):
    name: JsonValue
    aliases: list[JsonValue]
    role: JsonValue
    note: JsonValue
    detection_name: bool


class UnresolvedSpelling(TypedDict, total=False):
    spelling: str
    count: int | None
    note: str

PROPER_NOUNS = Path(__file__).resolve().parents[3] / "assets" / "manual_lane" / "proper_nouns.json"
_HAN = re.compile(r"[一-鿿]+")
_LATIN = re.compile(r"[A-Za-z][A-Za-z0-9]*")
# initials and finals that ASR and speech blur: zh/z, ch/c, sh/s, n/l; in/ing, en/eng
_INITIALS = (("zh", "z"), ("ch", "c"), ("sh", "s"), ("n", "l"))
_FINALS = (("ing", "in"), ("eng", "en"))


def _blur(syllable: str) -> str:
    for a, b in _INITIALS:
        if syllable.startswith(a):
            syllable = b + syllable[len(a):]
            break
    for a, b in _FINALS:
        if syllable.endswith(a):
            syllable = syllable[: -len(a)] + b
            break
    return syllable


def _pinyin(text: str) -> list[str]:
    return [_blur(t) for t in (text_pinyin_tokens(text) or [])]


@lru_cache(maxsize=4096)
def sound_key(text: str) -> tuple[str, ...] | None:
    """The blurred pinyin of a run of Chinese characters, one syllable per character; None when the run has no
    pinyin (punctuation) or its syllable count does not match its character count.  Two spellings of one name share
    this key, which is the comparison `near_names` and TASK-36's grouping both use."""
    han = "".join(_HAN.findall(text))
    if not han:
        return None
    key = tuple(_pinyin(han))
    return key if len(key) == len(han) else None


def raw_pinyin(text: str) -> tuple[str, ...] | None:
    """The pinyin of a run of Chinese characters as it is spoken (no blur), one syllable per character; None when it does
    not line up.  A Latin spelling written from the initials (TASK-36, e05 §5.4) is measured on these: the letter is what
    was said, so 南 is n there even though n and l are one sound for the Chinese comparison."""
    han = "".join(_HAN.findall(text))
    if not han:
        return None
    syllables = tuple(t for t in (text_pinyin_tokens(han) or []))
    return syllables if len(syllables) == len(han) else None



@lru_cache(maxsize=1)
def table_rows() -> tuple[NameRow, ...]:
    """The proper-noun table as written: name, aliases, role, note, origin (the note decides the spelling where the
    table has one, e05 §5.1; it is read by name_consistency.py)."""
    rows = cast(list[_NameInput], json.loads(PROPER_NOUNS.read_text(encoding="utf-8")))
    return tuple({"name": str(r["name"]), "aliases": tuple(str(a) for a in r.get("aliases") or []),
                  "role": str(r.get("role") or ""), "note": str(r.get("note") or "")}
                 for r in rows if r.get("detection_name") is not False)


def _table() -> tuple[tuple[str, tuple[str, ...]], ...]:
    return tuple((r["name"], tuple(r["aliases"])) for r in table_rows())



def entries(participants: Iterable[Sequence[str]] = ()) -> list[tuple[str, tuple[str, ...]]]:
    """(name, aliases) of the table and of the session's participant groups (first = the name)."""
    out = list(_table())
    known = {n for n, _ in out}
    out.extend((str(g[0]), tuple(str(a) for a in g[1:])) for g in participants
               if g and not unknown_participant(str(g[0])) and str(g[0]) not in known)
    return out


def _spellings(names: Sequence[tuple[str, tuple[str, ...]]]) -> list[tuple[str, str]]:
    """(spelling, the name it belongs to) for the name and every alias."""
    return [(s, name) for name, aliases in names for s in (name, *aliases)]


def near_names(text: str, names: Sequence[tuple[str, tuple[str, ...]]]) -> list[str]:
    """Names whose Chinese part (two or more characters) the sentence says in other characters."""
    hits: list[str] = []
    spellings = _spellings(names)
    written = {s for s, _ in spellings if s in text}
    for spelling, name in spellings:
        han = "".join(_HAN.findall(spelling))
        if len(han) < 2 or name in hits or spelling in written:
            continue
        want = _pinyin(han)
        if len(want) != len(han):
            continue
        for run in _HAN.findall(text):
            got = _pinyin(run)
            if len(got) != len(run):
                continue
            for i in range(len(run) - len(han) + 1):
                if run[i:i + len(han)] != han and got[i:i + len(han)] == want \
                        and not any(run[i:i + len(han)] in s for s in written):
                    hits.append(name)
                    break
            if name in hits:
                break
    return hits


def _latin_known(names: Sequence[tuple[str, tuple[str, ...]]]) -> set[str]:
    return {w.lower() for s, _ in _spellings(names) for w in _LATIN.findall(s)}


def unknown_latin(text: str, names: Sequence[tuple[str, tuple[str, ...]]]) -> list[str]:
    """Latin-letter words of the sentence that could be an unresolved name: at least lane_rules detection
    latin_min_letters letters, no name or alias of the table, not a common English short word (latin_common_words;
    TASK-29 ruling 6: ok, no, get, my, oh ... were false doubts)."""
    cfg = rules("detection")
    least = int(cfg.get("latin_min_letters") or 1)
    skip = _latin_known(names) | {str(w).lower() for w in cfg.get("latin_common_words") or []}
    return sorted({w for w in _LATIN.findall(text)
                   if sum(ch.isalpha() for ch in w) >= least and w.lower() not in skip})


def unknown_spellings(texts: Iterable[str], names: Sequence[tuple[str, tuple[str, ...]]],
                      sources: Iterable[str]) -> list[UnresolvedSpelling]:
    """Latin-letter spellings in the subtitles that the table, the danmaku and the title (`sources`) do not carry:
    [{"spelling", "count"}] (the 未定专名 list for PROCESS; a spelling must exist in a written source, §5.4)."""
    pool = " ".join(sources).lower()
    counts: dict[str, int] = {}
    for t in texts:
        for w in unknown_latin(t, names):
            if w.lower() not in pool:
                counts[w] = counts.get(w, 0) + 1
    return [{"spelling": w, "count": n} for w, n in sorted(counts.items())]


def unknown_line(texts: Iterable[str], participants: Iterable[Sequence[str]], sources: Iterable[str],
                 extra: Iterable[UnresolvedSpelling] = ()) -> str:
    """The PROCESS line of the 未定专名 list (COACH e05 §5.4: a spelling must exist in a written source).  `extra` are
    rows another producer could not settle (TASK-36: the name-consistency groups CPA left open, each spelling with how
    often it stands and why), shown in the same list so the reader has one place to look."""
    rows = unknown_spellings(texts, entries(participants), sources)
    seen = {r["spelling"] for r in rows}
    rows += [cast(UnresolvedSpelling, {**dict(r), "count": r.get("count")})
             for r in extra if str(r.get("spelling")) not in seen]
    return ("未定专名（字母写法在名字表、弹幕和选题钩子里都没有，e05 §5.4；只记录）："
            + ("、".join(f"{r['spelling']}×{r['count']}" for r in rows) if rows else "无"))


def detection_reasons(text: str, names: Sequence[tuple[str, tuple[str, ...]]], cfg: DetectionRules) -> list[str]:
    """The name doubts of one sentence (lane_rules detection.near_name, detection.latin_unknown)."""
    out = []
    if cfg.get("near_name"):
        out += [f"近音名字：本句可能说的是名字表里的「{n}」（拼音相近、写法不同）" for n in near_names(text, names)]
    if cfg.get("latin_unknown"):
        latin = unknown_latin(text, names)
        if latin:
            out.append("含拉丁字母「" + "、".join(latin) + "」，名字表里查不到")
    return out
