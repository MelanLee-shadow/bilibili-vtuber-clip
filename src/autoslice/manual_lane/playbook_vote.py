"""Several independent runs of the subtitle agent, one text (维护者).

Measured on the deployed lane: the same version on the same clip writes 3-16 different characters from run to run, and
of 75 places some run got wrong 55 were right in another run - a single run is partly luck.  The runs cut the clip
into sentences differently, so they are compared as whole texts, letter by letter (punctuation and spaces aside):
the run closest to the others is the lead and keeps its sentences and times; wherever most runs wrote the same other
wording at a place, that wording is written into the lead's sentence.  Pure functions: no model, no files.
"""

from __future__ import annotations

import copy
import unicodedata
from collections import Counter
from collections.abc import Mapping, Sequence
from difflib import SequenceMatcher
from typing import Any

Row = dict[str, Any]
MAX_PATCH = 12      # a longer difference is a sentence one run kept and another dropped, not a wording: left to the lead


def letters(text: str) -> tuple[str, list[int]]:
    """The letters and digits of `text` (what is said) and where each sits in `text`."""
    keep = [(ch, i) for i, ch in enumerate(text) if unicodedata.category(ch)[0] in "LN"]
    return "".join(ch for ch, _i in keep), [i for _ch, i in keep]


def spoken(rows: Sequence[Row]) -> str:
    return "".join(letters(str(r["final_text"]))[0] for r in rows if not r["drop"])


def _replace(text: str, start: int, end: int, new: str) -> str | None:
    """`text` with its letters [start, end) replaced by `new` (punctuation inside the stretch goes with it)."""
    said, where = letters(text)
    if not 0 <= start <= end <= len(said):
        return None
    a = where[start] if start < len(said) else len(text)
    b = where[end - 1] + 1 if end > start else a
    return text[:a] + new + text[b:]


def _patch(row: Row, start: int, end: int, new: str) -> str:
    """Write `new` over the row's letters [start, end); returns '' or why it cannot be done."""
    text = _replace(str(row["final_text"]), start, end, new)
    if text is None or not letters(text)[0]:
        return "would leave the sentence empty"
    parts = row.get("parts")
    if parts:
        offset = 0
        for part in parts:
            size = len(letters(str(part["text"]))[0])
            inside = offset <= start and end <= offset + size and (start < offset + size or part is parts[-1])
            if inside:
                piece = _replace(str(part["text"]), start - offset, end - offset, new)
                if piece is None or not letters(piece)[0]:
                    return "would leave a part of the sentence empty"
                part["text"] = piece
                break
            offset += size
        else:
            return "crosses two parts of the sentence"
    old = letters(str(row["final_text"]))[0][start:end]
    row["final_text"] = text
    if row.get("state") == "KEEP":
        row["state"] = "ADOPT"                 # KEEP promises the input text; this row no longer is it
    row["reason"] = f"{row.get('reason', '')}；多遍取多数：「{old}」→「{new}」"
    return ""


def merge(samples: Sequence[Sequence[Row]]) -> tuple[list[Row], dict[str, Any]]:
    """The lead run's rows with the majority wording written in, and a record of every place.  With fewer than three
    runs there is no majority: the lead's rows come back unchanged."""
    said = [spoken(rows) for rows in samples]
    closeness = [sum(SequenceMatcher(None, mine, other, autojunk=False).ratio()
                     for k, other in enumerate(said) if k != n) for n, mine in enumerate(said)]
    lead = max(range(len(samples)), key=lambda n: (closeness[n], -n))
    rows = copy.deepcopy(list(samples[lead]))
    record: dict[str, Any] = {"samples": len(samples), "lead": lead, "closeness": [round(c, 4) for c in closeness],
                              "applied": [], "unapplied": []}
    if len(samples) < 3:
        return rows, record
    votes: dict[tuple[int, int], Counter[str]] = {}
    for n, other in enumerate(said):
        if n == lead:
            continue
        for tag, i1, i2, j1, j2 in SequenceMatcher(None, said[lead], other, autojunk=False).get_opcodes():
            if tag != "equal":
                votes.setdefault((i1, i2), Counter())[other[j1:j2]] += 1
    starts, at = [], 0                         # where each kept row's letters begin in the lead's text
    kept = [r for r in rows if not r["drop"]]
    for r in kept:
        starts.append(at)
        at += len(letters(str(r["final_text"]))[0])
    done = len(said[lead]) + 1
    for (i1, i2), counter in sorted(votes.items(), reverse=True):       # right to left: earlier offsets stay valid
        new, count = counter.most_common(1)[0]
        if count * 2 <= len(samples) or i2 > done:
            continue
        done = i1
        place = {"from": said[lead][i1:i2], "to": new, "runs": count}
        index = max((k for k, s in enumerate(starts) if s <= i1), default=0)
        if i1 == i2 and index + 1 < len(starts) and starts[index + 1] == i1:
            index += 1                         # an insertion on a sentence border goes to the sentence it precedes
        row = kept[index] if kept else None
        size = len(letters(str(row["final_text"]))[0]) if row else 0
        if row is None:
            why = "no sentence to write into"
        elif max(len(new), i2 - i1) > MAX_PATCH:
            why = "longer than a wording"
        elif i2 - starts[index] > size:
            why = "crosses two sentences"
        else:
            why = _patch(row, i1 - starts[index], i2 - starts[index], new)
        (record["unapplied"] if why else record["applied"]).append(dict(place, **({"why": why} if why else {"id": row["id"]})))
    _count_agreement(rows, said)
    return rows, record


def _count_agreement(rows: Sequence[Row], said: Sequence[str]) -> None:
    """Write into every kept sentence how many of the runs wrote exactly its final letters (`runs_same`).  A difference
    on a sentence border counts against both sentences."""
    kept = [r for r in rows if not r["drop"]]
    spans, at = [], 0
    for r in kept:
        size = len(letters(str(r["final_text"]))[0])
        spans.append((at, at + size))
        at += size
    final = spoken(rows)
    same = [0] * len(kept)
    for other in said:
        touched = set()
        for tag, i1, i2, _j1, _j2 in SequenceMatcher(None, final, other, autojunk=False).get_opcodes():
            if tag != "equal":
                touched.update(k for k, (a, b) in enumerate(spans) if a <= i2 and i1 <= b and (i1 < b or i1 == i2) and (a < i2 or i1 == i2))
        for k in range(len(kept)):
            same[k] += k not in touched
    for r, count in zip(kept, same, strict=True):
        r["runs_same"] = count


# How likely a sentence is to hold a real error, from what is known when it is written.  Measured on 3321
# sentences of clips with the owner's final text (11 rounds; calib.py of the subtitle-accuracy tools): the share of
# sentences with a real error is 1% at score 0, 9% at 1, 24% at 2, 25% at 3, 39% at 4 and 71% at 5; the sentences
# scoring 2 or more are 14% of all and hold 62% of the wrong letters.
def risk(row: Mapping[str, Any]) -> int:
    """Low stated confidence 2, each run that wrote something else 1 (at most 2), a changed draft 1."""
    others = max(0, min(2, 3 - int(row.get("runs_same", 3))))
    return 2 * (row.get("confidence") == "低") + others + (row.get("state") != "KEEP")


def risk_lines(rows: Sequence[Row], most: int = 5, least: int = 3) -> list[str]:
    """For the step's record: the sentences most likely to be wrong, for whoever looks at the clip."""
    worst = sorted((r for r in rows if not r["drop"] and risk(r) >= least), key=lambda r: -risk(r))[:most]
    if not worst:
        return []
    return ["最可能有错的句子（按历史对错校准的风险分，满分 5）：" + "；".join(
        f"{r['id']}「{str(r['final_text'])[:24]}」{risk(r)} 分" for r in worst)]
