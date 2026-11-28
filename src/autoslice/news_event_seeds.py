"""Search seeds for the news/event crawler, derived from data the pipeline already has.

No name is written here.  A seed is a name worth searching for recent events, projects and collaborations:

1. the channel owner (profile display name);
2. whatever the owner's own recent dynamics mention: graph or roster entries found in the text, plus names the
   crawler's CPA mention extraction read out of it (`extracted`, checked against the text by the caller);
3. the accepted group / project / event nodes of the repository graph;
4. roster members that share an affiliation with the owner (a daily rotating window, so one run stays small and
   every member is reached over a few days);
5. what the owner's clips referred to and the pipeline had to look up (`clip_names`: the lane's reference store,
   manual_lane/references.py).  A name a clip needed and the graph did not know is the best lead there is for what
   the graph should know tomorrow; they come right after the owner's own dynamics.
"""
from __future__ import annotations

import json
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TypedDict

from src.autoslice.knowledge_graph import Graph, JsonValue
from src.autoslice.manual_lane.text_overlap import stands

SEED_NODE_KINDS = ("group", "project", "event")
WORK_SEEDS_FILE = "yuri_work_seeds.json"
WORK_SEEDS_SCHEMA = "vtuber-slice.work-seeds.v1"
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
    origin: str            # owner | dynamics | clips | graph | roster
    node_id: str | None = None


@dataclass(frozen=True)
class WorkSeed:
    name: str
    aliases: tuple[str, ...] = ()
    category: str = "yuri"


def load_work_seeds(repo_root: Path) -> list[WorkSeed]:
    """Optional class seed file for works (`yuri_work_seeds.json`); [] when the channel has none."""
    path = repo_root / WORK_SEEDS_FILE
    if not path.is_file():
        return []
    raw: JsonValue = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or raw.get("schema_version") != WORK_SEEDS_SCHEMA:
        raise ValueError(f"{path} must be an object with schema_version {WORK_SEEDS_SCHEMA!r}")
    category = str(raw.get("category") or "yuri").strip() or "yuri"
    works = raw.get("works")
    if not isinstance(works, list):
        raise ValueError(f"{path} works must be a list")
    out: list[WorkSeed] = []
    seen: set[str] = set()
    for idx, entry in enumerate(works):
        raw_name = entry.get("name") if isinstance(entry, dict) else None
        if not isinstance(entry, dict) or not isinstance(raw_name, str) or not raw_name.strip():
            raise ValueError(f"{path} works[{idx}].name must be a non-empty string")
        name = raw_name.strip()
        if name in seen:
            raise ValueError(f"{path} duplicate work name {name!r}")
        seen.add(name)
        raw_aliases = entry.get("aliases") or []
        if not isinstance(raw_aliases, list) or any(not isinstance(a, str) for a in raw_aliases):
            raise ValueError(f"{path} works[{idx}].aliases must be a list of strings")
        alias_texts = [a.strip() for a in raw_aliases if isinstance(a, str)]
        aliases = tuple(dict.fromkeys(a for a in alias_texts if a and a != name))
        out.append(WorkSeed(name=name, aliases=aliases, category=category))
    return out


def clip_seed_names(path: Path | None) -> list[str]:
    """The names in the lane's reference seeds file (manual_lane/references.py writes it: a list of rows with `name`
    and `seen`), newest first; [] when there is no file or it cannot be read: the crawler has its other seeds."""
    try:
        rows = json.loads(path.read_text(encoding="utf-8")) if path is not None else []
    except (OSError, ValueError):
        return []
    rows = [r for r in rows if isinstance(r, dict) and isinstance(r.get("name"), str) and r["name"].strip()] if isinstance(rows, list) else []
    return [r["name"].strip() for r in sorted(rows, key=lambda r: str(r.get("seen") or ""), reverse=True)]


def _key(text: str) -> str:
    return "".join(ch for ch in text.casefold() if ch.isalnum())


def plain_text(text: str) -> str:
    """A dynamic's prose with the platform's emote tags (`[name_emote]`) removed."""
    return " ".join(_EMOTE.sub(" ", text).split())


def dynamics_texts(dynamics: Dynamics | None) -> list[str]:
    """The prose of each dynamic (`plain_text`)."""
    if not dynamics:
        return []
    return [plain_text(item["text"]) for item in dynamics["items"]]


def _mentioned(surface: str, haystacks: Iterable[str]) -> bool:
    """The surface is written in one of the texts (the seam of another word said twice is not it: 晚安晚安 / 安晚)."""
    key = _key(surface)
    return len(key) >= MIN_SURFACE_CHARS and any(stands(key, _key(text)) for text in haystacks)


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
    """The owner's accepted node and every accepted node that is one of the owner's other names (accepted alias_of group)."""
    start = {node.id for node in graph.find(owner) if node.status == "accepted"}
    return {nid for group in graph.alias_groups() if start & set(group) for nid in group}


def _rotate(items: Sequence[Seed], day: int, count: int) -> list[Seed]:
    if count <= 0 or not items:
        return []
    start = (day * count) % len(items)
    return [items[(start + i) % len(items)] for i in range(min(count, len(items)))]


def derive_seeds(*, owner: str, graph: Graph, registry: Registry | None, dynamics: Dynamics | None,
                 extracted: Sequence[str] = (), day: int, limit: int, clip_names: Sequence[str] = ()) -> list[Seed]:
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
    chosen.extend(Seed(name, "clips") for name in clip_names)
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
