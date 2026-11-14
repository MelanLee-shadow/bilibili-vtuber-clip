"""Step 6: what the channel knowledge graph already says about the entities a clip names (graph first, web second).

The repository graph (accepted) and the news crawler's runtime candidates (sources are URLs) are read together
(news_event_graph.load_combined).  An entity is "named" when one of its spellings stands in the final subtitles; its
one-hop background goes to the title prompt as accepted background and as unverified leads that help to understand a
reference but never ground a title's assertion.  An unreadable graph never stops the step: repository graph, or none.
"""

from __future__ import annotations

import re
import tempfile
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, TypedDict
from urllib.parse import urlparse

from src.autoslice.channel_profile import load_channel_profile
from src.autoslice.knowledge_graph import EDGES_FILE, NODES_FILE, Edge, Graph, Node, Source, load_graph
from src.autoslice.combined_knowledge_graph import load_combined

from .lane_rules import rules
from .layout import REPO_ROOT, layout

if TYPE_CHECKING:
    from .runner import Ctx

COMBINED, REPO_ONLY, NONE = "combined", "repo_only", "none"
VERIFIED_HEAD = "已认可的圈内背景（来自频道知识图；只用来看懂指代和梗，标题里的事实仍以字幕为准）"
CLUES_HEAD = ("未核实的线索（仅供理解：爬虫从网页收集、尚未核实，只能帮助理解，不得作为标题断言的依据，"
              "也不据此改字幕写法）")
KIND = {"person": "人物", "group": "团体", "project": "企划", "event": "事件", "term": "词条", "work": "作品",
        "character": "角色", "game": "游戏", "community": "社群"}
RELATION = {"member_of": "隶属于", "founded": "创立了", "participates_in": "参与了", "fan_name_of": "是粉丝名，属于",
            "character_of": "是角色，出自", "related_to": "相关于"}
STATUS_TEXT = {COMBINED: "仓库图加运行时候选", REPO_ONLY: "只用仓库图", NONE: "没有用图"}


class GraphRecord(TypedDict):
    status: str                      # combined, repo_only or none
    reason: str                      # why the graph is not whole ("" when combined)
    mentioned: list[dict[str, str]]  # graph nodes with a spelling in the subtitles: id, status
    in_prompt: list[dict[str, str]]  # the nodes the prompt lines are about: id, status
    lines: dict[str, int]            # verified / clues: lines in the prompt
    skipped_search: list[str]        # unexplained references the graph covers, so no web search was made


@dataclass(frozen=True)
class Line:
    verified: bool
    own: bool                        # the entity's own summary, not a relation
    conn: int                        # how many named entities the line connects to
    newest: str                      # newest date the line carries ("" when none)
    text: str
    ids: tuple[str, ...]


@dataclass(frozen=True, eq=False)
class GraphContext:
    status: str
    reason: str
    graph: Graph | None = None
    mentioned: tuple[Node, ...] = ()
    lines: tuple[Line, ...] = ()

    def block(self) -> str:
        parts = ((VERIFIED_HEAD, [x.text for x in self.lines if x.verified]),
                 (CLUES_HEAD, [x.text for x in self.lines if not x.verified]))
        return "\n\n".join("\n".join([head, *(f"- {r}" for r in rows)]) for head, rows in parts if rows)

    def covers(self, term: str) -> bool:
        """The graph knows this reference and its background is in the prompt."""
        shown = {i for line in self.lines for i in line.ids}
        return self.graph is not None and any(n.id in shown for n in self.graph.find(term.strip()) if n.status != "retired")

    def record(self, skipped: Sequence[str] = ()) -> GraphRecord:
        shown = sorted({i for line in self.lines for i in line.ids})
        graph = self.graph
        return GraphRecord(
            status=self.status, reason=self.reason, skipped_search=list(skipped),
            mentioned=[{"id": n.id, "status": n.status} for n in self.mentioned],
            in_prompt=[{"id": i, "status": graph.nodes[i].status} for i in shown] if graph else [],
            lines={"verified": sum(x.verified for x in self.lines), "clues": sum(not x.verified for x in self.lines)})


def _why(what: str, exc: Exception) -> str:
    return f"{what}：{type(exc).__name__}: {str(exc)[:200]}"


def load(ctx: Ctx) -> GraphContext:
    """The repository graph plus the runtime candidates fetched from the media host (no lines yet); never raises."""
    try:
        repo_root = load_channel_profile(REPO_ROOT).asset_root
        repo = load_graph(repo_root)
    except Exception as exc:  # noqa: BLE001 - the step is never blocked by the graph
        return GraphContext(NONE, _why("仓库图读不到", exc))
    with tempfile.TemporaryDirectory(prefix="graph-context-") as tmp:
        try:
            for rel in (NODES_FILE, EDGES_FILE):
                ctx.adapters.remote.get(f"{layout().news_graph_root}/{rel}", Path(tmp) / rel)
        except Exception as exc:  # noqa: BLE001 - no runtime directory, or the host cannot be reached
            return GraphContext(REPO_ONLY, _why("运行时候选图读不到", exc), repo)
        try:
            return GraphContext(COMBINED, "", load_combined(repo_root, Path(tmp)))
        except Exception as exc:  # noqa: BLE001 - candidate files that do not parse or do not fit the repository graph
            return GraphContext(REPO_ONLY, _why("运行时候选图损坏", exc), repo)


def _spelled(surface: str, text: str, least: int, common: set[str]) -> bool:
    """detection's exclusions: a Latin spelling needs `latin_min_letters` letters, is no common word and matches as a
    whole word in any case; a spelling in other scripts needs two characters."""
    if not surface.isascii():
        return len(surface) >= 2 and surface in text
    if sum(ch.isalpha() for ch in surface) < least or surface.lower() in common:
        return False
    return re.search(rf"(?<![A-Za-z0-9]){re.escape(surface)}(?![A-Za-z0-9])", text, re.ASCII | re.IGNORECASE) is not None


def mentions(graph: Graph, texts: Iterable[str]) -> list[Node]:
    """The graph's accepted and candidate nodes with a spelling in the texts, in id order.  A node the proper-noun
    table marks detection_name=false is left out, as it is from detection."""
    text = "\n".join(texts)
    cfg = rules("detection")
    least = int(cfg.get("latin_min_letters") or 1)
    common = {w.lower() for w in cfg.get("latin_common_words") or []}
    return [n for n in graph.nodes.values()
            if n.status != "retired" and (n.subtitle or {}).get("detection_name") is not False
            and any(_spelled(s, text, least, common) for s in dict.fromkeys(n.surfaces()))]


def _clue(sources: Iterable[Source]) -> str:
    at, ref = max((s.at or "", s.ref) for s in sources)
    return f"（出处：{urlparse(ref).hostname or ref}，{at[:10] or '无日期'}）"


def _ranked(lines: list[Line]) -> list[Line]:
    """Most connected first, then newest, own summaries before relations, then by text (every pass is stable)."""
    lines = sorted(lines, key=lambda x: x.text)
    lines.sort(key=lambda x: not x.own)
    lines.sort(key=lambda x: x.newest, reverse=True)
    lines.sort(key=lambda x: x.conn, reverse=True)
    return lines


def _edge_line(graph: Graph, edge: Edge, hub: Node, other: Node, conn: int) -> Line:
    verified = edge.status == hub.status == other.status == "accepted"
    window = edge.valid or other.valid or {}
    when = f"{window.get('from') or ''}~{window.get('until') or ''}"
    source, target, nearby = (graph.name(i, "display") for i in (edge.from_id, edge.to_id, other.id))
    text = (f"{source}{RELATION[edge.type]}{target}；{nearby}（{KIND[other.kind]}）"
            + (f"：{other.summary}" if other.summary else "") + (f"（时间：{when}）" if when != "~" else ""))
    if not verified:
        text += _clue([*edge.sources, *(s for n in (hub, other) if n.status != "accepted" for s in n.sources)])
    stamps = [s.at or "" for s in (*edge.sources, *other.sources)] + [v or "" for v in window.values()]
    return Line(verified, False, conn, max(stamps), text, (hub.id, other.id))


def background(graph: Graph, nodes: Sequence[Node]) -> GraphContext:
    """One-hop background of the named entities (nodes linked by alias_of are one entity), as ranked, capped lines."""
    cfg = rules("graph_context")
    kinds, per_entity, cap = cfg["neighbour_kinds"], cfg["neighbours_per_entity"], cfg["lines_per_clip"]
    group_of = {i: tuple(g) for g in graph.alias_groups() for i in g}
    entities = sorted({group_of[n.id] for n in nodes})
    found = []
    for group in entities:
        rows = {e.id: (e, hub, o) for hub in (graph.nodes[i] for i in group)
                for e, o in graph.neighbors(hub.id, status=("accepted", "candidate"))
                if e.type != "alias_of" and o.kind in kinds and o.id not in group}
        found.append([rows[k] for k in sorted(rows)])
    conn = Counter(i for rows in found for i in {o.id for _, _, o in rows})
    lines: list[Line] = []
    for group, rows in zip(entities, found, strict=True):
        for hub in (graph.nodes[i] for i in group):
            if hub.summary and hub.status != "retired":
                text = f"{graph.name(hub.id, 'display')}（{KIND[hub.kind]}）：{hub.summary}"
                lines.append(Line(hub.status == "accepted", True, 1, max(s.at or "" for s in hub.sources),
                                  text if hub.status == "accepted" else text + _clue(hub.sources), group))
        lines.extend(_ranked([_edge_line(graph, e, hub, o, conn[o.id]) for e, hub, o in rows])[:per_entity])
    return GraphContext(COMBINED, "", graph, tuple(nodes), tuple(_ranked(lines)[:cap]))


def read(ctx: Ctx, texts: Iterable[str]) -> GraphContext:
    """Load the graph, find the entities `texts` name and build their background (no lines when it cannot be read)."""
    loaded = load(ctx)
    if loaded.graph is None:
        return loaded
    built = background(loaded.graph, mentions(loaded.graph, texts))
    return replace(loaded, mentioned=built.mentioned, lines=built.lines)


def process_line(record: GraphRecord) -> str:
    """The one PROCESS line: how the graph was read, what it brought, what it saved from the web."""
    got = record["lines"]
    text = (f"知识图：{STATUS_TEXT[record['status']]}；本片提到 {len(record['mentioned'])} 个图中实体，"
            f"进入提示 {got['verified']} 行已认可、{got['clues']} 行线索")
    if record["skipped_search"]:
        text += "；图里已有而免于上网：" + "、".join(record["skipped_search"])
    return text + (f"（{record['reason']}）" if record["reason"] else "")
