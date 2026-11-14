"""Formal spellings of the names a clip's subtitles mention, for the title (step 6).

The proper-noun table (assets/manual_lane/proper_nouns.json) gives each name a `canonical` spelling and the
`variants` it is also written as.  For one clip, the entries whose canonical or any variant occurs in the final
subtitles (a one-character canonical is ignored), plus the session's participants, are the names the title prompt
gives as "write it this way, not that way".  Two things are then checked on a candidate title:

- a spelling that differs from a formal name only in letter case is corrected to the formal one (`fix_case`);
- a non-formal variant (two characters or longer) in the body is a self-check failure (`non_formal`).

`prefix_repeat` only records that a partner already named in the prefix is named again in the body.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from functools import lru_cache
from typing import TypedDict

from .name_match import PROPER_NOUNS

NO_NAMES = "按字幕里的写法"


class NameFix(TypedDict):
    written: str
    formal: str


class NonFormal(TypedDict):
    written: str
    formal: str


@dataclass(frozen=True)
class NameEntry:
    canonical: str
    others: tuple[str, ...]      # the other spellings of the name: "do not write it as"


@lru_cache(maxsize=1)
def _table() -> tuple[NameEntry, ...]:
    rows = json.loads(PROPER_NOUNS.read_text(encoding="utf-8"))
    return tuple(NameEntry(str(r["canonical"]), tuple(str(v) for v in r.get("variants") or [] if v != r["canonical"]))
                 for r in rows)


def _ascii_edges(name: str) -> tuple[str, str]:
    """Look-arounds that keep an ASCII-edged name from matching inside a longer Latin word."""
    word = "A-Za-z0-9"
    return (f"(?<![{word}])" if re.match(f"[{word}]", name[0]) else "",
            f"(?![{word}])" if re.match(f"[{word}]", name[-1]) else "")


@dataclass(frozen=True)
class CanonicalNames:
    entries: tuple[NameEntry, ...]

    def prompt_text(self) -> str:
        if not self.entries:
            return NO_NAMES
        return "；".join(e.canonical + (f"（不要写成：{'、'.join(e.others)}）" if e.others else "")
                         for e in self.entries)

    def fix_case(self, text: str) -> tuple[str, list[NameFix]]:
        """Every spelling in `text` equal to a formal name ignoring case, but written differently, becomes the formal one."""
        fixes: list[NameFix] = []
        for entry in self.entries:
            before, after = _ascii_edges(entry.canonical)
            pattern = re.compile(before + re.escape(entry.canonical) + after, re.IGNORECASE)

            def replace(match: re.Match[str], formal: str = entry.canonical) -> str:
                if match.group(0) != formal:
                    fixes.append({"written": match.group(0), "formal": formal})
                return formal
            text = pattern.sub(replace, text)
        return text, fixes

    def non_formal(self, body: str) -> list[NonFormal]:
        """The non-formal spellings (two characters or longer) of a selected name found in `body`.  A spelling that
        is only a part of the formal one counts where it stands outside the formal spelling."""
        hits: list[NonFormal] = []
        for entry in self.entries:
            outside = body.replace(entry.canonical, "")
            for other in entry.others:
                if len(other) < 2:
                    continue
                if other in (outside if other in entry.canonical else body):
                    hits.append({"written": other, "formal": entry.canonical})
        return hits


_CJK_RUN = re.compile(r"[\u4e00-\u9fff]{2,}")


def _subtitle_code(entry: NameEntry) -> bool:
    """The table is the subtitles' spelling authority.  Where its canonical is a Latin/digit code and a variant is a
    Chinese name (xxsk for 星汐, 142 for 伊索尔), the code is how the subtitles write what she says, not how a title
    names the person: such an entry gives the title no formal spelling."""
    return not _CJK_RUN.search(entry.canonical) and entry.canonical.isascii() and any(_CJK_RUN.search(v) for v in entry.others)


def canonical_names(subtitle_text: str, participants: Iterable[str]) -> CanonicalNames:
    """The formal names for one clip: the table's entries mentioned in `subtitle_text`, then the participants' names."""
    table = _table()
    chosen: list[NameEntry] = []
    for entry in table:
        if len(entry.canonical) == 1 or _subtitle_code(entry):
            continue
        if entry.canonical in subtitle_text or any(v in subtitle_text for v in entry.others):
            chosen.append(entry)
    seen = {e.canonical for e in chosen}
    by_canonical = {e.canonical: e for e in table}
    for name in participants:
        if name in seen:
            continue
        chosen.append(by_canonical.get(name) or NameEntry(name, ()))
        seen.add(name)
    return CanonicalNames(tuple(chosen))


def prefix_repeat(partners: Sequence[str], body: str) -> bool:
    """A partner named in the prefix whose full formal name is written again in the body."""
    return any(p and p in body for p in partners)
