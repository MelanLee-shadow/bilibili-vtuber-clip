"""The news/event crawler's runtime candidate graph.

`<runtime root>/knowledge/{nodes,edges}.jsonl` use the format defined in `knowledge_graph` and hold only
status=candidate nodes and edges whose sources are `search` rows (ref = the result URL, at = fetch time).  An edge may
point at an accepted node of the repository graph (that node is not copied here), so the runtime files alone do not
satisfy `load_graph`; `load_combined` is the reader: repository graph + runtime candidates, validated together by the
same `load_graph`.  Accepted data is never changed; a reader that wants only confirmed facts ignores this graph.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import shutil
import tempfile
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import TypeVar

from src.autoslice.knowledge_graph import (
    EDGES_FILE, NODES_FILE, Edge, Graph, JsonValue, Node, Source, dump_graph, load_graph)
from src.autoslice.news_event_judge import Finding

SOURCES_PER_ITEM = 5
RETAIN_DAYS = 120
REPORT_FILE = "crawl_report.json"
Candidates = tuple[dict[str, Node], dict[str, Edge]]
Item = TypeVar("Item")


class Collision(ValueError):
    """A finding names something whose id is already taken by a retired node."""


def _resolve(known: Graph, *, node_id: str | None, name: str) -> Node | None:
    """An existing node: the id CPA gave if the graph has it, else the one node with that spelling (an accepted node
    wins over a candidate of the same spelling)."""
    if node_id and node_id in known.nodes:
        return known.nodes[node_id]
    hits = [n for n in known.find(name) if n.status != "retired"]
    for status in ("accepted", "candidate"):
        same = [n for n in hits if n.status == status]
        if same:
            return same[0] if len(same) == 1 else None
    return None


def _newest(sources: Iterable[Source]) -> tuple[Source, ...]:
    by_ref: dict[tuple[str, str], Source] = {}
    for s in sources:
        old = by_ref.get((s.kind, s.ref))
        if old is None or (s.at or "") > (old.at or ""):
            by_ref[(s.kind, s.ref)] = s
    return tuple(sorted(by_ref.values(), key=lambda s: (s.at or "", s.ref), reverse=True)[:SOURCES_PER_ITEM])


def _merge_node(old: Node, new: Node) -> Node:
    return Node(id=old.id, kind=old.kind, names=old.names,
                aliases=tuple(dict.fromkeys((*old.aliases, *new.aliases))), summary=new.summary or old.summary,
                status="candidate", sources=_newest((*old.sources, *new.sources)), valid=new.valid or old.valid)


def _merge_edge(old: Edge, new: Edge) -> Edge:
    return Edge(id=old.id, type=old.type, from_id=old.from_id, to_id=old.to_id, status="candidate",
                sources=_newest((*old.sources, *new.sources)), note=new.note or old.note, valid=new.valid or old.valid)


def _put(table: dict[str, Item], key: str, item: Item, merge: Callable[[Item, Item], Item]) -> None:
    table[key] = merge(table[key], item) if key in table else item


def with_candidates(graph: Graph, candidates: Candidates) -> Graph:
    """`graph` plus candidate nodes and edges (a candidate replaces a node of the same id)."""
    return Graph({**graph.nodes, **candidates[0]}.values(), [*graph.edges, *candidates[1].values()])


def _one_finding(finding: Finding, known: Graph, repo_edges: set[str], now: str) -> Candidates:
    nodes: dict[str, Node] = {}
    edges: dict[str, Edge] = {}
    src = Source("search", finding.url, now)

    def target(kind: str, name: str, node_id: str | None, *, aliases: tuple[str, ...] = (), summary: str = "",
               valid: dict[str, str | None] | None = None) -> str:
        found = _resolve(known, node_id=node_id, name=name)
        if found is not None and found.status == "accepted":
            return found.id
        if found is None and f"{kind}:{name}" in known.nodes:  # a retired node keeps its id: no candidate reuses it
            raise Collision(f"{kind}:{name}")
        base = found or Node(id=f"{kind}:{name}", kind=kind, names={"display": name}, aliases=(), summary="",
                             status="candidate", sources=(src,))
        display = base.names["display"]
        others = tuple(dict.fromkeys(a for a in (*aliases, name) if a != display))  # a new spelling becomes an alias
        fresh = Node(id=base.id, kind=base.kind, names=base.names, aliases=others, summary=summary,
                     status="candidate", sources=(src,), valid=valid)
        _put(nodes, fresh.id, fresh, _merge_node)
        return fresh.id

    window = {"from": finding.valid_from, "until": finding.valid_until} \
        if finding.valid_from or finding.valid_until else None
    item = target(finding.kind, finding.name, finding.id, aliases=finding.aliases, summary=finding.summary,
                  valid=window)
    for part in finding.participants:
        who = target(part.kind, part.name, part.id)
        edge_id = f"{who}|{part.relation}|{item}"
        if who != item and edge_id not in repo_edges:
            _put(edges, edge_id, Edge(id=edge_id, type=part.relation, from_id=who, to_id=item, status="candidate",
                                      sources=(src,), note=finding.summary or None), _merge_edge)
    return nodes, edges


def candidates_from(findings: Iterable[Finding], known: Graph, repo_edges: set[str], *, now: str
                    ) -> tuple[Candidates, int]:
    """New or re-confirmed candidate nodes and edges for these findings, and how many findings were left out.
    Accepted nodes of `known` are referenced by id, not copied; an edge the repository already holds
    (`repo_edges`) is skipped."""
    nodes: dict[str, Node] = {}
    edges: dict[str, Edge] = {}
    left_out = 0
    for finding in findings:
        try:
            more_nodes, more_edges = _one_finding(finding, known, repo_edges, now)
        except Collision:
            left_out += 1
            continue
        for key, node in more_nodes.items():
            _put(nodes, key, node, _merge_node)
        for key, edge in more_edges.items():
            _put(edges, key, edge, _merge_edge)
    return (nodes, edges), left_out


def merge(first: Candidates, second: Candidates) -> Candidates:
    """The union of two candidate sets; an item in both keeps its aliases and the newest sources of each."""
    nodes, edges = dict(first[0]), dict(first[1])
    for key, node in second[0].items():
        _put(nodes, key, node, _merge_node)
    for key, edge in second[1].items():
        _put(edges, key, edge, _merge_edge)
    return nodes, edges


def accumulate(previous: Candidates, fresh: Candidates, *, repo: Graph, now: str) -> Candidates:
    """Previous candidates plus this run's.  An item whose newest source is older than RETAIN_DAYS drops out with the
    edges that point at it, so a one-off search hit does not live forever."""
    nodes, edges = merge(previous, fresh)
    cutoff = (dt.datetime.fromisoformat(now.replace("Z", "+00:00")) - dt.timedelta(days=RETAIN_DAYS)).isoformat(
        timespec="seconds")

    def live(sources: tuple[Source, ...]) -> bool:
        return max((s.at or "" for s in sources), default="") >= cutoff

    nodes = {k: n for k, n in nodes.items() if live(n.sources)}
    edges = {k: e for k, e in edges.items() if live(e.sources)
             and all(end in nodes or end in repo.nodes for end in (e.from_id, e.to_id))}
    return nodes, edges


def _combined_root(repo_root: Path, runtime_root: Path | None, scratch: Path) -> Path:
    """A scratch profile root whose knowledge files are the repository's followed by the runtime candidates'."""
    (scratch / NODES_FILE).parent.mkdir(parents=True, exist_ok=True)
    for rel in (NODES_FILE, EDGES_FILE):
        text = (repo_root / rel).read_text("utf-8")
        if runtime_root is not None and (runtime_root / rel).is_file():
            text += (runtime_root / rel).read_text("utf-8")
        (scratch / rel).write_text(text, "utf-8")
    return scratch


def load_combined(repo_root: Path, runtime_root: Path | None) -> Graph:
    """Repository graph plus the runtime candidates, validated together (a missing runtime graph adds nothing)."""
    with tempfile.TemporaryDirectory(prefix="news-event-graph-") as tmp:
        return load_graph(_combined_root(Path(repo_root), runtime_root and Path(runtime_root), Path(tmp)))


def load_previous(repo_root: Path, runtime_root: Path) -> Candidates:
    """The candidates already on disk; raises GraphError when they no longer validate against the repository."""
    if not (Path(runtime_root) / NODES_FILE).is_file():
        return {}, {}
    combined = load_combined(repo_root, runtime_root)
    repo = load_graph(Path(repo_root))
    repo_edges = {e.id for e in repo.edges}
    return ({k: n for k, n in combined.nodes.items() if k not in repo.nodes},
            {e.id: e for e in combined.edges if e.id not in repo_edges})


def write_runtime_graph(repo_root: Path, runtime_root: Path, candidates: Candidates,
                        report: Mapping[str, JsonValue]) -> Graph:
    """Write a new runtime root next to `runtime_root`, validate it with the repository graph, then swap it in.

    Nothing at `runtime_root` changes unless the new files validate; the swap is two renames, so a reader sees the
    old or the new directory (or, for an instant between the renames, none, which readers treat as "no candidates").
    """
    runtime_root = Path(runtime_root)
    runtime_root.parent.mkdir(parents=True, exist_ok=True)
    staged = Path(tempfile.mkdtemp(prefix=f".{runtime_root.name}.new-", dir=runtime_root.parent))
    try:
        dump_graph(Graph(candidates[0].values(), candidates[1].values()), staged)
        combined = load_combined(repo_root, staged)  # raises GraphError on any format or endpoint violation
        (staged / REPORT_FILE).write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                                          "utf-8")
        for path in (staged, *staged.rglob("*")):
            path.chmod(0o755 if path.is_dir() else 0o644)
        retired = runtime_root.with_name(f".{runtime_root.name}.old-{os.getpid()}")
        if runtime_root.exists():
            os.rename(runtime_root, retired)
        os.rename(staged, runtime_root)
        shutil.rmtree(retired, ignore_errors=True)
        return combined
    finally:
        shutil.rmtree(staged, ignore_errors=True)
