"""Search seeds for the news/event crawler, derived from data the pipeline already has.

No name is written here.  A seed is a name worth searching for recent events, projects and collaborations:

1. the channel owner (profile display name);
2. whatever the owner's own recent dynamics mention: graph or roster entries found in the text, plus names the
   crawler's CPA mention extraction read out of it (`extracted`, checked against the text by the caller);
3. the accepted group / project / event nodes of the repository graph;
4. roster members that share an affiliation with the owner (a daily rotating window, so one run stays small and
   every member is reached over a few days).
"""
from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import TypedDict

from src.autoslice.knowledge_graph import Graph

SEED_NODE_KINDS = ("group", "project", "event")
MIN_SURFACE_CHARS = 2
_EMOTE = re.compile(r"\[[^\[\]]{1,40}\]")


class Member(TypedDict):
    """The parts of a roster row (streamer_registry_crawler.validate_snapshot) the seeds read."""
    canonical: str
    official_surfaces: list[str]
    aliases: list[str]
    affiliations: list[str]


class Registry(TypedDict):
    members: list[Member]


class DynamicItem(TypedDict):
    text: str


class Dynamics(TypedDict):
    items: list[DynamicItem]


@dataclass(frozen=True)
class Seed:
    name: str
    origin: str            # owner | dynamics | graph | roster
    node_id: str | None = None


def _key(text: str) -> str:
    return "".join(ch for ch in text.casefold() if ch.isalnum())


def dynamics_texts(dynamics: Dynamics | None) -> list[str]:
    """The prose of each dynamic, with the platform's emote tags (`[name_emote]`) removed."""
    if not dynamics:
        return []
    return [" ".join(_EMOTE.sub(" ", item["text"]).split()) for item in dynamics["items"]]


def _mentioned(surface: str, haystacks: Iterable[str]) -> bool:
    key = _key(surface)
    return len(key) >= MIN_SURFACE_CHARS and any(key in _key(text) for text in haystacks)


def _member_surfaces(member: Member) -> list[str]:
    return [member["canonical"], *member["official_surfaces"], *member["aliases"]]


def _members(registry: Registry | None) -> list[Member]:
    return registry["members"] if registry else []


def owner_affiliations(registry: Registry | None, owner: str) -> set[str]:
    """Affiliations of the roster row that names the owner (by canonical name, official surface or alias)."""
    found: set[str] = set()
    for member in _members(registry):
        if any(_key(s) == _key(owner) for s in _member_surfaces(member)):
            found.update(member["affiliations"])
    return found


def _owner_ids(graph: Graph, owner: str) -> set[str]:
    """The owner's node and every node that is one of the owner's other names (alias_of group)."""
    start = {node.id for node in graph.find(owner)}
    return {nid for group in graph.alias_groups() if start & set(group) for nid in group}


def _rotate(items: Sequence[Seed], day: int, count: int) -> list[Seed]:
    if count <= 0 or not items:
        return []
    start = (day * count) % len(items)
    return [items[(start + i) % len(items)] for i in range(min(count, len(items)))]


def derive_seeds(*, owner: str, graph: Graph, registry: Registry | None, dynamics: Dynamics | None,
                 extracted: Sequence[str] = (), day: int, limit: int) -> list[Seed]:
    """At most `limit` seeds, in priority order, one per distinct name; the roster rotates with `day`."""
    texts = dynamics_texts(dynamics)
    own = _owner_ids(graph, owner)
    chosen: list[Seed] = [Seed(owner, "owner")]
    for node in graph.nodes.values():
        if node.status == "accepted" and node.kind != "term" and node.id not in own \
                and any(_mentioned(s, texts) for s in node.surfaces()):
            chosen.append(Seed(node.names["display"], "dynamics", node.id))
    affiliations = owner_affiliations(registry, owner)
    roster: list[Seed] = []
    for member in _members(registry):
        if any(_mentioned(s, texts) for s in _member_surfaces(member)):
            chosen.append(Seed(member["canonical"], "dynamics"))
        elif affiliations & set(member["affiliations"]) and _key(member["canonical"]) != _key(owner):
            roster.append(Seed(member["canonical"], "roster"))
    chosen.extend(Seed(name, "dynamics") for name in extracted)
    chosen.extend(Seed(node.names["display"], "graph", node.id) for node in graph.nodes.values()
                  if node.status == "accepted" and node.kind in SEED_NODE_KINDS)
    taken: set[str] = set()
    out: list[Seed] = []
    for seed in chosen:
        if _key(seed.name) and _key(seed.name) not in taken:
            taken.add(_key(seed.name))
            out.append(seed)
    out = out[:limit]
    fresh_roster = [s for s in roster if _key(s.name) not in taken]
    return out + _rotate(fresh_roster, day, limit - len(out))
