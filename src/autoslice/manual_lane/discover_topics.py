"""`discover`: the evening's candidates on the same matter are found, so they take one seat and become one clip.

Every recording (half an hour) is selected by itself: the model that reads 23:00 has not seen 22:30.  On it
picked 「谁是谁的宠物」 at 22:41 and again at 23:03, the second going on from the first's conclusion; both were made, each
on a seat, and three other clips had none.  同话题的两条应合并成一条.

After every recording of a day is selected, one call (prompts/discover_topics.md) reads what each of the day's talk
candidates was picked for -- its selection reason and its key moments, not its subtitles -- and answers which of them
are one matter: the later one goes on from what the earlier one settled, or the same joke comes round again.  The next
thing a conversation moves on to is not that, nor is the same person, mood or game.  (Measured on 11-23 and 11-24: given
the whole subtitles the model also joined clips that only mention the other's joke in passing -- the 21:50 clip on who
streams with whom into the two pet clips -- and given only each clip's first and last lines it missed the pet pair in
one run of three.)  The code checks only the shape (every member a candidate, none in two groups, two
or more to a group).  A grouped candidate carries `topic` (the first member's clip id), `topic_theme` and `topic_why`;
seating gives the group one seat (discover_select._fold_topics) and discover joins its members into one clip with the
same-session compilation (compilation.compile_session).  The answer is kept in `<day>/topics.json` and read back while
the candidates are the same.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from . import channel
from .discover_media import write_json

SCHEMA = "manual-lane.discover-topics.v1"
FILE = "topics.json"
PURPOSE = "discover-topics"
_STAMP = re.compile(r"_\d{8}-(\d{2})-(\d{2})-(\d{2})$")


class TopicsError(ValueError):
    """The grouping answer is not usable (the day is seated without groups, and the ledger says so)."""


def _clock(entry: Mapping[str, Any]) -> str:
    """When the candidate begins, on the wall clock of the recording's file name (HH:MM), else its minute in the file."""
    stamp = _STAMP.search(str(entry["source"]["name"]))
    seconds = int(entry["start_ms"]) // 1000
    if stamp is None:
        return f"{seconds // 60:02d}:{seconds % 60:02d}"
    total = int(stamp.group(1)) * 3600 + int(stamp.group(2)) * 60 + int(stamp.group(3)) + seconds
    return f"{total // 3600 % 24:02d}:{total % 3600 // 60:02d}"


def _ordered(pool: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    return sorted((e for e in pool if e.get("kind") == "talk"), key=lambda e: (str(e["source"]["name"]), int(e["start_ms"])))


def prompt(candidates: Sequence[Mapping[str, Any]]) -> str:
    blocks = []
    for number, entry in enumerate(candidates, start=1):
        peaks = "\n".join(f"- 「{' '.join(str(p['quote']).split())}」" + (f"（{p['why']}）" if p.get("why") else "")
                          for p in entry.get("peaks") or [])
        blocks.append(f"候选 {number}（{_clock(entry)} 开始）\n选片理由：{entry.get('hook', '')}"
                      + (f"\n最关键的几句：\n{peaks}" if peaks else ""))
    return channel.prompt("discover_topics.md").replace("{candidates}", "\n\n".join(blocks))


def parse(raw: str, count: int) -> list[dict[str, Any]]:
    match = re.search(r"\{.*\}", raw, re.S)
    try:
        doc = json.loads(match.group(0) if match else raw)
    except json.JSONDecodeError as exc:
        raise TopicsError(f"not JSON: {exc}") from exc
    rows = doc.get("groups") if isinstance(doc, dict) else None
    if not isinstance(rows, list):
        raise TopicsError("no `groups` list")
    groups, used = [], set()
    for row in rows:
        members = row.get("members") if isinstance(row, dict) else None
        theme = str(row.get("theme") or "").strip() if isinstance(row, dict) else ""
        if (not isinstance(members, list) or len(set(members)) < 2 or len(set(members)) != len(members) or not theme
                or not all(isinstance(m, int) and not isinstance(m, bool) and 1 <= m <= count for m in members)
                or used & set(members)):
            raise TopicsError(f"bad group {str(row)[:200]}")
        used |= set(members)
        groups.append({"members": sorted(members), "theme": theme[:120], "why": str(row.get("why") or "").strip()[:300]})
    return groups


def assign(pool: list[dict[str, Any]], day_dir: Path, ask: Callable[[str], str]) -> dict[str, Any]:
    """Set `topic`, `topic_theme`, `topic_why` on the candidates of `pool` that are one matter with another, and return the
    record kept in `<day_dir>/topics.json` (its `groups` name clip ids).  With fewer than two talk candidates nothing
    is asked.  A failed call raises what `ask` raises; an unusable answer raises TopicsError."""
    for entry in pool:
        for key in ("topic", "topic_theme", "topic_why"):
            entry.pop(key, None)
    candidates = _ordered(pool)
    cids = [str(e["cid"]) for e in candidates]
    path = day_dir / FILE
    if len(candidates) < 2:
        return {"schema": SCHEMA, "candidates": cids, "groups": []}
    text = prompt(candidates)
    fingerprint = hashlib.sha256(text.encode("utf-8")).hexdigest()
    kept = json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
    if kept is not None and kept.get("schema") == SCHEMA and kept.get("prompt_sha256") == fingerprint:
        record = kept
    else:
        groups = parse(ask(text), len(candidates))
        record = {"schema": SCHEMA, "prompt_sha256": fingerprint, "candidates": cids,
                  "groups": [{"members": [cids[m - 1] for m in g["members"]], "theme": g["theme"], "why": g["why"]} for g in groups]}
        write_json(path, record)
    by_cid = {str(e["cid"]): e for e in pool}
    for group in record["groups"]:
        for cid in group["members"]:
            by_cid[cid].update(topic=group["members"][0], topic_theme=group["theme"], topic_why=group["why"])
    return record
