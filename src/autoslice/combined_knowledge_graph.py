"""The repository knowledge graph plus the news crawler's runtime candidates, validated together.

Split out of news_event_graph so the lane reads the combined graph without loading the news judge;
news_event_graph re-exports `load_combined`."""
from __future__ import annotations

from collections.abc import Iterable, Sequence
from pathlib import Path

from src.autoslice.knowledge_graph import (
    EDGES_FILE, NODES_FILE, Edge, Graph, Node, check_unique_ids, read_edges, read_nodes,
    validate_edge_id, validate_graph)


class LayeredGraph(Graph):
    """A graph joined from ordered authority layers, recording lower-layer ids shadowed by a higher layer."""

    def __init__(
        self,
        nodes: Iterable[Node],
        edges: Iterable[Edge],
        *,
        shadowed_nodes: Sequence[str] = (),
        shadowed_edges: Sequence[str] = (),
    ) -> None:
        super().__init__(nodes, edges)
        self.shadowed_nodes: tuple[str, ...] = tuple(shadowed_nodes)
        self.shadowed_edges: tuple[str, ...] = tuple(shadowed_edges)

    @property
    def shadowed(self) -> tuple[str, ...]:
        return (*self.shadowed_nodes, *self.shadowed_edges)


def load_combined(repo_root: Path, runtime_root: Path | None) -> LayeredGraph:
    """Repository graph plus the runtime candidates, joined in authority order (a missing runtime graph adds nothing)."""
    repo = Path(repo_root)
    for rel in (NODES_FILE, EDGES_FILE):
        if not (repo / rel).is_file():
            raise FileNotFoundError(repo / rel)
    roots = [repo, Path(runtime_root)] if runtime_root is not None else [repo]
    return load_layers(roots)


def load_layers(roots: Sequence[Path]) -> LayeredGraph:
    """Several knowledge roots read as one graph, in authority order (highest first): the circle-wide terms
    every channel shares (assets/manual_lane/knowledge), the channel's graph, then any runtime candidates.

    When an id appears in more than one layer, the higher-authority layer's entry stands untouched and the lower
    layer's duplicate is shadowed (reported on ``shadowed_nodes`` / ``shadowed_edges``); lower-layer edges pointing
    at a shadowed node id resolve to the standing node.  Within-layer duplicates, dangling edges, and format errors
    still raise.
    """
    nodes_by_id: dict[str, Node] = {}
    edges_by_id: dict[str, Edge] = {}
    shadowed_nodes: set[str] = set()
    shadowed_edges: set[str] = set()
    for root in roots:
        root_path = Path(root)
        nodes_path = root_path / NODES_FILE
        edges_path = root_path / EDGES_FILE
        layer_nodes = read_nodes(nodes_path) if nodes_path.is_file() else []
        layer_edges = read_edges(edges_path) if edges_path.is_file() else []
        check_unique_ids("node", [n.id for n in layer_nodes])
        check_unique_ids("edge", [e.id for e in layer_edges])
        for edge in layer_edges:
            validate_edge_id(edge)
        for node in layer_nodes:
            if node.id in nodes_by_id:
                shadowed_nodes.add(node.id)
            else:
                nodes_by_id[node.id] = node
        for edge in layer_edges:
            if edge.id in edges_by_id:
                shadowed_edges.add(edge.id)
            else:
                edges_by_id[edge.id] = edge
    graph = LayeredGraph(
        nodes_by_id.values(),
        edges_by_id.values(),
        shadowed_nodes=sorted(shadowed_nodes),
        shadowed_edges=sorted(shadowed_edges),
    )
    validate_graph(graph)
    return graph
