"""Channel knowledge graph: one node per entity, typed edges between nodes.

Files (UTF-8, one JSON object per line, sorted by id, fixed key order, final newline):
  assets/<profile>/knowledge/nodes.jsonl   assets/<profile>/knowledge/edges.jsonl
Node keys: id "<kind>:<display name at creation>" (never renamed), kind, names, aliases,
  summary (may be ""), status, sources, optional valid, optional subtitle.
  kind: person group project event term work character game community
  names: display (required) plus optional subtitle / official / tag = the spelling used for
    captions / the official account name / the upload tag. Different uses of one entity are
    different spellings of the same node, not different canonical names.
  aliases: legitimate other names. status: accepted | candidate | retired; only accepted
    nodes and edges may change caption spelling, candidates are hints only.
  sources: list of {kind: owner|official|crawler|search|ledger, ref, at (nullable)}.
  valid: {from, until}, each nullable. subtitle: object holding the remaining fields of
    this entity's row in the proper-noun table (see scripts/build_knowledge_graph.py).
Edge keys: id "<from>|<type>|<to>", type, from, to, optional note, status, sources, optional valid.
  type: member_of founded participates_in fan_name_of character_of alias_of related_to
One spelling may belong to only one accepted node, unless the nodes are linked by alias_of.
"""
from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

JsonValue = str | int | float | bool | None | list["JsonValue"] | dict[str, "JsonValue"]
NODE_KINDS = ("person", "group", "project", "event", "term", "work", "character", "game", "community")
EDGE_TYPES = ("member_of", "founded", "participates_in", "fan_name_of", "character_of", "alias_of",
              "related_to")
STATUSES = ("accepted", "candidate", "retired")
SOURCE_KINDS = ("owner", "official", "crawler", "search", "ledger")
NAME_USES = ("display", "subtitle", "official", "tag")
NODE_KEYS = ("id", "kind", "names", "aliases", "summary", "status", "sources", "valid", "subtitle")
EDGE_KEYS = ("id", "type", "from", "to", "note", "status", "sources", "valid")
NODE_OPTIONAL = ("valid", "subtitle")
EDGE_OPTIONAL = ("note", "valid")
NODES_FILE = "knowledge/nodes.jsonl"
EDGES_FILE = "knowledge/edges.jsonl"


class GraphError(ValueError):
    """The graph files violate the format defined in the module docstring."""


@dataclass(frozen=True)
class Source:
    kind: str
    ref: str
    at: str | None = None


@dataclass(frozen=True)
class Node:
    id: str
    kind: str
    names: dict[str, str]
    aliases: tuple[str, ...]
    summary: str
    status: str
    sources: tuple[Source, ...]
    valid: dict[str, str | None] | None = None
    subtitle: dict[str, JsonValue] | None = None

    def surfaces(self) -> tuple[str, ...]:
        return (*self.names.values(), *self.aliases)


@dataclass(frozen=True)
class Edge:
    id: str
    type: str
    from_id: str
    to_id: str
    status: str
    sources: tuple[Source, ...]
    note: str | None = None
    valid: dict[str, str | None] | None = None


def _key(surface: str) -> str:
    return surface.casefold() if surface.isascii() else surface


class Graph:
    def __init__(self, nodes: Iterable[Node], edges: Iterable[Edge]) -> None:
        self.nodes = {node.id: node for node in sorted(nodes, key=lambda n: n.id)}
        self.edges = tuple(sorted(edges, key=lambda e: e.id))
        self._index: dict[str, list[Node]] = defaultdict(list)
        for node in self.nodes.values():
            for surface in dict.fromkeys(_key(s) for s in node.surfaces()):
                self._index[surface].append(node)

    def find(self, surface: str) -> list[Node]:
        return list(self._index.get(_key(surface), ()))

    def neighbors(self, node_id: str, *, types: Sequence[str] | None = None,
                  status: Sequence[str] = ("accepted",)) -> list[tuple[Edge, Node]]:
        found = []
        for edge in self.edges:
            if node_id not in (edge.from_id, edge.to_id) or edge.status not in status:
                continue
            if types is not None and edge.type not in types:
                continue
            other = self.nodes[edge.to_id if edge.from_id == node_id else edge.from_id]
            if other.status in status:
                found.append((edge, other))
        return found

    def name(self, node_id: str, use: str) -> str:
        if use not in NAME_USES:
            raise ValueError(f"unknown name use: {use!r}")
        names = self.nodes[node_id].names
        return names.get(use) or names["display"]

    def alias_groups(self) -> list[list[str]]:
        """Connected components of nodes linked by non-retired alias_of edges."""
        parent = {node_id: node_id for node_id in self.nodes}

        def root(node_id: str) -> str:
            while parent[node_id] != node_id:
                parent[node_id] = parent[parent[node_id]]
                node_id = parent[node_id]
            return node_id

        for edge in self.edges:
            if edge.type == "alias_of" and edge.status != "retired":
                parent[root(edge.from_id)] = root(edge.to_id)
        groups: dict[str, list[str]] = defaultdict(list)
        for node_id in self.nodes:
            groups[root(node_id)].append(node_id)
        return sorted(groups.values())


def _fail(where: str, message: str) -> GraphError:
    return GraphError(f"{where}: {message}")


def _text(value: object, where: str, *, empty: bool = False) -> str:
    if not isinstance(value, str) or not (value or empty):
        raise _fail(where, "must be a non-empty string")
    return value


def _choice(value: object, allowed: tuple[str, ...], where: str) -> str:
    text = _text(value, where)
    if text not in allowed:
        raise _fail(where, f"{text!r} not in {allowed}")
    return text


def _record(line: str, where: str, keys: tuple[str, ...], optional: tuple[str, ...]) -> dict[str, JsonValue]:
    raw = json.loads(line)
    if not isinstance(raw, dict):
        raise _fail(where, "must be a JSON object")
    missing = [k for k in keys if k not in optional and k not in raw]
    unknown = [k for k in raw if k not in keys]
    if missing or unknown:
        raise _fail(where, f"missing keys {missing}, unknown keys {unknown}")
    return raw


def _sources(value: JsonValue, where: str) -> tuple[Source, ...]:
    if not isinstance(value, list) or not value:
        raise _fail(where, "sources must be a non-empty list")
    out = []
    for item in value:
        if not isinstance(item, dict) or set(item) != {"kind", "ref", "at"}:
            raise _fail(where, "each source needs exactly kind, ref, at")
        at = None if item["at"] is None else _text(item["at"], where)
        out.append(Source(_choice(item["kind"], SOURCE_KINDS, where), _text(item["ref"], where), at))
    return tuple(out)


def _valid(value: JsonValue, where: str) -> dict[str, str | None] | None:
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {"from", "until"}:
        raise _fail(where, "valid needs exactly from, until")
    return {k: None if v is None else _text(v, where) for k, v in value.items()}


def _node(line: str, where: str) -> Node:
    raw = _record(line, where, NODE_KEYS, NODE_OPTIONAL)
    names = raw["names"]
    if not isinstance(names, dict) or "display" not in names or not set(names) <= set(NAME_USES):
        raise _fail(where, "names needs display and only display/subtitle/official/tag")
    aliases = raw["aliases"]
    if not isinstance(aliases, list):
        raise _fail(where, "aliases must be a list")
    subtitle = raw.get("subtitle")
    if subtitle is not None and not isinstance(subtitle, dict):
        raise _fail(where, "subtitle must be an object")
    return Node(
        id=_text(raw["id"], where), kind=_choice(raw["kind"], NODE_KINDS, where),
        names={k: _text(v, where) for k, v in names.items()},
        aliases=tuple(_text(a, where) for a in aliases), summary=_text(raw["summary"], where, empty=True),
        status=_choice(raw["status"], STATUSES, where), sources=_sources(raw["sources"], where),
        valid=_valid(raw.get("valid"), where), subtitle=subtitle)


def _edge(line: str, where: str) -> Edge:
    raw = _record(line, where, EDGE_KEYS, EDGE_OPTIONAL)
    note = raw.get("note")
    return Edge(
        id=_text(raw["id"], where), type=_choice(raw["type"], EDGE_TYPES, where),
        from_id=_text(raw["from"], where), to_id=_text(raw["to"], where),
        status=_choice(raw["status"], STATUSES, where), sources=_sources(raw["sources"], where),
        note=None if note is None else _text(note, where), valid=_valid(raw.get("valid"), where))


def _lines(path: Path) -> list[tuple[str, str]]:
    return [(f"{path.name}:{n}", line) for n, line in enumerate(path.read_text("utf-8").splitlines(), 1)
            if line.strip()]


def _validate(graph: Graph, node_ids: list[str], edge_ids: list[str]) -> None:
    for kind, ids in (("node", node_ids), ("edge", edge_ids)):
        dup = sorted({i for i in ids if ids.count(i) > 1})
        if dup:
            raise GraphError(f"duplicate {kind} ids: {dup}")
    for edge in graph.edges:
        if edge.id != f"{edge.from_id}|{edge.type}|{edge.to_id}":
            raise _fail(edge.id, "edge id must be <from>|<type>|<to>")
        for end in (edge.from_id, edge.to_id):
            if end not in graph.nodes:
                raise _fail(edge.id, f"endpoint {end!r} is not a node")
    group_of = {nid: i for i, group in enumerate(graph.alias_groups()) for nid in group}
    clashes = []
    for surface, nodes in graph._index.items():
        accepted = [n for n in nodes if n.status == "accepted"]
        if len({group_of[n.id] for n in accepted}) > 1:
            clashes.append(f"{surface!r}: {[n.id for n in accepted]}")
    if clashes:
        raise GraphError("one spelling in several accepted nodes without alias_of: " + "; ".join(clashes))


def load_graph(profile_root: Path) -> Graph:
    node_rows = [_node(line, where) for where, line in _lines(Path(profile_root) / NODES_FILE)]
    edge_rows = [_edge(line, where) for where, line in _lines(Path(profile_root) / EDGES_FILE)]
    graph = Graph(node_rows, edge_rows)
    _validate(graph, [n.id for n in node_rows], [e.id for e in edge_rows])
    return graph


def _source_json(source: Source) -> dict[str, JsonValue]:
    return {"kind": source.kind, "ref": source.ref, "at": source.at}


def _valid_json(valid: dict[str, str | None] | None) -> JsonValue:
    if valid is None:
        return None
    out: dict[str, JsonValue] = {k: v for k, v in valid.items()}
    return out


def _ordered(keys: tuple[str, ...], values: dict[str, JsonValue]) -> dict[str, JsonValue]:
    return {k: values[k] for k in keys if k in values}


def _row(text_keys: tuple[str, ...], optional: tuple[str, ...], values: dict[str, JsonValue]) -> str:
    kept = {k: v for k, v in values.items() if k not in optional or v not in (None, "")}
    return json.dumps(_ordered(text_keys, kept), ensure_ascii=False) + "\n"


def dump_graph(graph: Graph, profile_root: Path) -> None:
    node_text = "".join(_row(NODE_KEYS, NODE_OPTIONAL, {
        "id": n.id, "kind": n.kind, "names": _ordered(NAME_USES, dict(n.names)), "aliases": list(n.aliases),
        "summary": n.summary, "status": n.status, "sources": [_source_json(s) for s in n.sources],
        "valid": _valid_json(n.valid), "subtitle": n.subtitle}) for n in graph.nodes.values())
    edge_text = "".join(_row(EDGE_KEYS, EDGE_OPTIONAL, {
        "id": e.id, "type": e.type, "from": e.from_id, "to": e.to_id, "note": e.note, "status": e.status,
        "sources": [_source_json(s) for s in e.sources], "valid": _valid_json(e.valid)}) for e in graph.edges)
    root = Path(profile_root)
    (root / NODES_FILE).parent.mkdir(parents=True, exist_ok=True)
    (root / NODES_FILE).write_text(node_text, encoding="utf-8")
    (root / EDGES_FILE).write_text(edge_text, encoding="utf-8")
