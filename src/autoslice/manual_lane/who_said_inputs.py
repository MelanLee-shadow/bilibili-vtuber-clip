"""What this project gives `who_said` (the general speaker attribution): person cards from what the channel already
keeps, and the arrival evidence as a plugged opinion.  Measurement input: nothing in the lane calls it yet.

Person cards:

- Names: the session's participant group (name first, the rest aliases), the proper-noun table's aliases and the
  knowledge graph's names and aliases of the same person.
- The host's card: the channel's basic facts (channel_facts.spoken: persona.md without the cover section) and, with a
  task directory, what the task's clips have shown of her in several sessions (channel_facts.observed, the persona
  the clips fill in as they are made).
- Anyone else's card: their own persona when one of the repository's channel profiles is theirs (a profile whose
  display name or speaker aliases is one of their names: channel_facts.spoken of that profile's persona.md, e.g. a
  guest who is also a channel of this project), then the first sentence of every glossary entry naming them (who
  they are to the host, nicknames) and the knowledge graph's groups and projects they belong to.  A guest with no
  profile has no persona: the card says who they are, not how they talk."""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from pathlib import Path

from ..who_said import Opinion, Person
from . import channel, channel_facts
from .arrival import UNNAMED, LineWords

GLOSSARY = channel.asset("glossary.txt")
NODES = channel.asset("knowledge/nodes.jsonl")
EDGES = channel.asset("knowledge/edges.jsonl")
PROPER_NOUNS = channel.name_table("proper_nouns.json")
RELATIONS = {"member_of": "成员", "participates_in": "参与"}
PROFILES = channel.REPO_ROOT / "profiles"


NOT_SPEECH = ("形象", "封面", "首次配置")    # persona sections about the look, the cover or setting the profile up


def persona_of(names: Sequence[str]) -> str:
    """The sections of the persona of the channel profile whose display name or speaker aliases is one of the names
    that say who they are and how they talk (every `## ` section but those headed with a NOT_SPEECH word), through
    channel_facts.spoken; "" when no profile is theirs."""
    for manifest in sorted(PROFILES.glob("*/profile.json")):
        doc = json.loads(manifest.read_text(encoding="utf-8"))
        ident = doc.get("identity") or {}
        own = {ident.get("display_name"), *(ident.get("speaker_identity_aliases") or [])} - {None, ""}
        if own & set(names):
            persona = channel.REPO_ROOT / doc["assets"]["root"] / "persona.md"
            kept, skip = [], False
            for line in persona.read_text(encoding="utf-8").splitlines():
                if line.startswith("## "):
                    skip = any(w in line for w in NOT_SPEECH)
                if not skip:
                    kept.append(line)
            return channel_facts.spoken("\n".join(kept))
    return ""


def _graph() -> tuple[list[dict], list[dict]]:
    def rows(p: Path) -> list[dict]:
        return [json.loads(ln) for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()]
    return rows(NODES), rows(EDGES)


def _node_names(node: dict) -> set[str]:
    return {str(v) for v in (node.get("names") or {}).values() if v} | {str(a) for a in node.get("aliases") or []}


def names_of(name: str, group: Sequence[str] = ()) -> list[str]:
    """Every name of one person: the session's group, the proper-noun aliases, the knowledge graph's names of the
    person nodes sharing a name with it (an alias_of edge joins two nodes)."""
    nodes, edges = _graph()
    known = {name, *group}
    for row in json.loads(PROPER_NOUNS.read_text(encoding="utf-8")):
        if row.get("name") in known:
            known |= set(row.get("aliases") or [])
    by_id = {n["id"]: n for n in nodes if n.get("kind") == "person" and n.get("status") == "accepted"}
    ids = {i for i, n in by_id.items() if _node_names(n) & known}
    for e in edges:
        if (e.get("type") == "alias_of" and e.get("status") == "accepted"
                and e["from"] in by_id and e["to"] in by_id
                and (e["from"] in ids or e["to"] in ids)):
            ids |= {e["from"], e["to"]}
    for i in ids:
        if i in by_id:
            known |= _node_names(by_id[i])
    return [name, *sorted(known - {name})]


def _names_in(text: str, names: Sequence[str]) -> bool:
    """A name of the person is written in the text: a Latin name as a whole word (awa is not in kawaii)."""
    for n in names:
        if len(n) < 2:
            continue
        if n.isascii():
            if re.search(rf"(?<![A-Za-z]){re.escape(n)}(?![A-Za-z])", text, re.IGNORECASE):
                return True
        elif n in text:
            return True
    return False


def _glossary_lines(names: Sequence[str]) -> list[str]:
    """The first sentence of every glossary entry whose first sentence names the person (the entry is about them)."""
    out = []
    for line in GLOSSARY.read_text(encoding="utf-8").splitlines():
        first = line.strip().lstrip("- ").strip().split("。", 1)[0].strip()
        if first and _names_in(first, names) and first + "。" not in out:
            out.append(first + "。")
    return out


def _relations(names: Sequence[str]) -> list[str]:
    nodes, edges = _graph()
    accepted_ids = {n["id"] for n in nodes if n.get("status") == "accepted"}
    ids = {n["id"] for n in nodes if n.get("kind") == "person" and n.get("status") == "accepted" and _node_names(n) & set(names)}
    title = {n["id"]: (n.get("names") or {}).get("display") or n["id"] for n in nodes if n.get("status") == "accepted"}
    return sorted({f"{RELATIONS[e['type']]}：{title.get(e['to'], e['to'])}" for e in edges
                   if e.get("status") == "accepted" and e.get("from") in ids
                   and e.get("to") in accepted_ids and e.get("type") in RELATIONS})


def cards(participants: Sequence[Sequence[str]], *, host: str, task: Path | None = None) -> list[Person]:
    """One Person per participant group of a session (first name is the person)."""
    out: list[Person] = []
    for group in participants:
        name = str(group[0])
        names = names_of(name, [str(g) for g in group[1:]])
        if name == host:
            card = "\n".join(filter(None, [channel_facts.spoken(), channel_facts.observed(task)]))
        else:
            card = "\n".join(filter(None, [persona_of(names), *(f"- {g}" for g in _glossary_lines(names)),
                                            *(f"- {r}" for r in _relations(names))]))
        out.append({"name": name, "aliases": names[1:], "card": card})
    return out


# Step 4: the arrival, found, is the label engine and no voice route contests it; by its words it is surer than by its
# time.  Both are deciding (combine.PLUG_DECIDES) and heavier than any other deciding source the lane may plug in (the
# trained voiceprint goes below ARRIVAL_TIME), so it decides whatever order the sources are passed in.
ARRIVAL_WORDS = 0.98     # a line the arrival gives one person by where its words fall
ARRIVAL_TIME = 0.95      # ... by its time (too few of its letters placed)


def arrival_opinions(rows: Sequence[LineWords], among: dict[str, list[str]], people: Sequence[str]) -> list[Opinion]:
    """The arrival's line_words rows as who_said's plugged opinions.  A line it gives one named person: that person; a
    line on a path several guests share (`among`, Evidence.among): everyone of `people` off that path is ruled out; a
    line it cuts, calls mixed, or does not hear: no opinion (weight 0)."""
    out: list[Opinion] = []
    for row in rows:
        person = row["person"]
        if person is None or row["mixed"] or row["parts"] or person == UNNAMED:
            out.append({"person": None, "weight": 0.0, "kind": "none", "why": ""})
            continue
        weight = ARRIVAL_WORDS if row["by"] == "words" else ARRIVAL_TIME
        why = f"到达时间（{'按字' if row['by'] == 'words' else '按时间'}）：{row['heard']}"
        if person in among:
            others = sorted(set(people) - set(among[person]))
            out.append({"person": None, "weight": weight, "kind": "arrival", "why": why, "not_people": others})
        else:
            out.append({"person": person, "weight": weight, "kind": "arrival", "why": why})
    return out
