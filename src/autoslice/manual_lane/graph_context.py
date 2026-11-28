"""Step 6: what the channel knowledge graph already says about the entities a clip names (graph first, web second).

The repository graph (accepted) and the news crawler's runtime candidates (sources are URLs) are read together
(news_event_graph.load_combined).  An entity is "named" when one of its spellings stands in the final subtitles; its
one-hop background goes to the title prompt as accepted background and as unverified leads that help to understand a
reference but never ground a title's assertion.  An unreadable graph never stops the step: repository graph, or none.
"""

from __future__ import annotations

import datetime as dt
import re
import tempfile
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, NotRequired, TypedDict
from urllib.parse import urlparse

from src.autoslice import name_initials
from src.autoslice.channel_profile import load_channel_profile
from src.autoslice.knowledge_graph import EDGES_FILE, NODES_FILE, Edge, Graph, Node, Source, load_graph
from src.autoslice.combined_knowledge_graph import load_layers
from src.autoslice.crawl_window import CrawlWindow

from . import text_overlap
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
CLUES_PER_ENTITY = 4             # candidate edge clues kept per entity, separate from accepted neighbours_per_entity
CLUES_PER_CLIP = 10              # candidate clue lines kept per clip, separate from accepted lines_per_clip


class GraphRecord(TypedDict):
    status: str                      # combined, repo_only or none
    reason: str                      # why the graph is not whole or which lower-layer ids were shadowed ("" when clean)
    shadowed: NotRequired[list[str]] # lower-layer node/edge ids shadowed by a higher-authority layer
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
    shadowed: tuple[str, ...] = ()

    def block(self) -> str:
        parts = ((VERIFIED_HEAD, [x.text for x in self.lines if x.verified]),
                 (CLUES_HEAD, [x.text for x in self.lines if not x.verified]))
        return "\n\n".join("\n".join([head, *(f"- {r}" for r in rows)]) for head, rows in parts if rows)

    def covers(self, term: str) -> bool:
        """The graph knows this reference via accepted material and its accepted background is in the prompt."""
        shown = {i for line in self.lines if line.verified for i in line.ids}
        return self.graph is not None and any(n.id in shown for n in self.graph.find(term.strip()) if n.status == "accepted")

    def same_initials(self, term: str) -> list[str]:
        """The graph's accepted people, characters and groups one of whose spellings has the term's pinyin first letters
        (name_initials: a circle nickname is often made from the letters of the name): leads for the lookup."""
        if self.graph is None:
            return []
        return name_initials.same_letters(term, ((n.names["display"], n.surfaces()) for n in self.graph.nodes.values()
                                                 if n.status == "accepted" and n.kind in ("person", "character", "group")))

    def record(self, skipped: Sequence[str] = ()) -> GraphRecord:
        shown = sorted({i for line in self.lines for i in line.ids})
        graph = self.graph
        return GraphRecord(
            status=self.status, reason=self.reason, shadowed=list(self.shadowed), skipped_search=list(skipped),
            mentioned=[{"id": n.id, "status": n.status} for n in self.mentioned],
            in_prompt=[{"id": i, "status": graph.nodes[i].status} for i in shown] if graph else [],
            lines={"verified": sum(x.verified for x in self.lines), "clues": sum(not x.verified for x in self.lines)})


def _why(what: str, exc: Exception) -> str:
    return f"{what}：{type(exc).__name__}: {str(exc)[:200]}"


def _shadow_detail(ids: Sequence[str]) -> str:
    if not ids:
        return ""
    shown = "、".join(ids[:10])
    tail = f" 等 {len(ids)} 项" if len(ids) > 10 else ""
    return f"低层同名由高层覆盖：{shown}{tail}"


SHARED_ROOT = REPO_ROOT / "assets" / "manual_lane"   # circle-wide terms every channel reads (U430: 嗦面 is the genre's word)


def load(ctx: Ctx) -> GraphContext:
    """The shared circle terms and the channel's graph plus the runtime candidates fetched from the media host (no lines
    yet); never raises."""
    try:
        repo_root = load_channel_profile(REPO_ROOT).asset_root
        load_graph(repo_root)                       # the channel's own graph must read on its own
        repo = load_layers([SHARED_ROOT, repo_root])
    except Exception as exc:  # noqa: BLE001 - the step is never blocked by the graph
        return GraphContext(NONE, _why("仓库图读不到", exc))
    repo_shadow = _shadow_detail(repo.shadowed)
    with tempfile.TemporaryDirectory(prefix="graph-context-") as tmp:
        try:
            for rel in (NODES_FILE, EDGES_FILE):
                ctx.adapters.remote.get(f"{layout().news_graph_root}/{rel}", Path(tmp) / rel)
        except Exception as exc:  # noqa: BLE001 - no runtime directory, or the host cannot be reached
            detail = _why("运行时候选图读不到", exc)
            return GraphContext(REPO_ONLY, f"{detail}；{repo_shadow}" if repo_shadow else detail,
                                repo, shadowed=repo.shadowed)
        try:
            combined = load_layers([SHARED_ROOT, repo_root, Path(tmp)])
        except Exception as exc:  # noqa: BLE001 - candidate files that do not parse or do not fit the repository graph
            detail = _why("运行时候选图损坏", exc)
            return GraphContext(REPO_ONLY, f"{detail}；{repo_shadow}" if repo_shadow else detail,
                                repo, shadowed=repo.shadowed)
        return GraphContext(COMBINED, _shadow_detail(combined.shadowed), combined, shadowed=combined.shadowed)


def _spelled(surface: str, text: str, least: int, common: set[str]) -> bool:
    """detection's exclusions: a Latin spelling needs `latin_min_letters` letters, is no common word and matches as a
    whole word in any case; a spelling in other scripts needs two characters and is not the seam of another word said
    twice (text_overlap.stands: 「晚安晚安」 does not name 安晚)."""
    if not surface.isascii():
        return len(surface) >= 2 and text_overlap.stands(surface, text)
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


def _when(valid: dict[str, str | None] | None, on: dt.date | None) -> str:
    """The item's time as news, said against the clip's day: an item over by then is said to be over, one not begun
    is said to be ahead (owner a festival event is news only around its own weeks)."""
    window = valid or {}
    start, end = window.get("from"), window.get("until")
    if not start and not end:
        return ""
    text = f"（时间：{start or ''}~{end or ''}"
    if on is not None and end and dt.date.fromisoformat(end) < on:
        text += "；本片录制时已结束，不是当时的新闻"
    elif on is not None and start and dt.date.fromisoformat(start) > on:
        text += "；本片录制时还没开始"
    return text + "）"


def _placed(valid: dict[str, str | None] | None, around: CrawlWindow | None) -> bool:
    """Whether an item with this time belongs near the clip's day (nine months back, six ahead); undated items stay."""
    return around is None or not valid or around.holds(valid.get("from"), valid.get("until"))


def _edge_line(graph: Graph, edge: Edge, hub: Node, other: Node, conn: int, on: dt.date | None = None) -> Line:
    verified = edge.status == hub.status == other.status == "accepted" and edge.type != "alias_of"
    window = edge.valid or other.valid or {}
    source, target, nearby = (graph.name(i, "display") for i in (edge.from_id, edge.to_id, other.id))
    if edge.type == "alias_of":
        text = f"{source}可能是{target}的别名（未核实）" + _when(window, on)
    else:
        text = (f"{source}{RELATION[edge.type]}{target}；{nearby}（{KIND[other.kind]}）"
                + (f"：{other.summary}" if other.summary else "") + _when(window, on))
    if not verified:
        text += _clue([*edge.sources, *(s for n in (hub, other) if n.status != "accepted" for s in n.sources)])
    stamps = [s.at or "" for s in (*edge.sources, *other.sources)] + [v or "" for v in window.values()]
    return Line(verified, False, conn, max(stamps), text, (hub.id, other.id))


def background(graph: Graph, nodes: Sequence[Node], on: dt.date | None = None) -> GraphContext:
    """One-hop background of the named entities (nodes linked by accepted alias_of are one entity), as ranked, capped
    lines.  Accepted lines are selected first under `neighbours_per_entity`/`lines_per_clip` exactly as on the
    repository graph alone; candidate clue lines follow under `CLUES_PER_ENTITY`/`CLUES_PER_CLIP` and never displace
    accepted lines or change `covers`.  With the clip's day `on`, items outside nine months back / six ahead are left
    out and ended items are marked over."""
    cfg = rules("graph_context")
    around = CrawlWindow.around(on) if on is not None else None
    nodes = [n for n in nodes if _placed(n.valid, around)]
    kinds, per_entity, cap = cfg["neighbour_kinds"], cfg["neighbours_per_entity"], cfg["lines_per_clip"]
    group_of = {i: tuple(g) for g in graph.alias_groups() for i in g}

    accepted_entities = sorted({group_of[n.id] for n in nodes if n.status == "accepted"})
    accepted_found: list[list[tuple[Edge, Node, Node]]] = []
    for group in accepted_entities:
        by_edge = {e.id: (e, hub, o) for hub in (graph.nodes[i] for i in group)
                   for e, o in graph.neighbors(hub.id, status=("accepted",))
                   if e.type != "alias_of" and o.kind in kinds and o.id not in group
                   and _placed(e.valid, around) and _placed(o.valid, around)}
        accepted_found.append([by_edge[k] for k in sorted(by_edge)])
    accepted_conn = Counter(i for found in accepted_found for i in {o.id for _, _, o in found})
    accepted_raw: list[Line] = []
    for group, links in zip(accepted_entities, accepted_found, strict=True):
        for hub in (graph.nodes[i] for i in group):
            if hub.summary and hub.status == "accepted":
                text = f"{graph.name(hub.id, 'display')}（{KIND[hub.kind]}）：{hub.summary}{_when(hub.valid, on)}"
                accepted_raw.append(Line(True, True, 1, max(s.at or "" for s in hub.sources), text, group))
        accepted_raw.extend(_ranked([_edge_line(graph, e, hub, o, accepted_conn[o.id], on)
                                     for e, hub, o in links])[:per_entity])
    accepted_lines = _ranked(accepted_raw)[:cap]

    all_entities = sorted({group_of[n.id] for n in nodes})
    clue_found: list[list[tuple[Edge, Node, Node]]] = []
    for group in all_entities:
        by_edge = {e.id: (e, hub, o) for hub in (graph.nodes[i] for i in group)
                   for e, o in graph.neighbors(hub.id, status=("accepted", "candidate"))
                   if (e.type == "alias_of" or o.kind in kinds) and o.id not in group
                   and not (e.status == hub.status == o.status == "accepted" and e.type != "alias_of")
                   and _placed(e.valid, around) and _placed(o.valid, around)}
        clue_found.append([by_edge[k] for k in sorted(by_edge)])
    clue_conn = Counter(i for found in clue_found for i in {o.id for _, _, o in found})
    clue_raw: list[Line] = []
    for group, links in zip(all_entities, clue_found, strict=True):
        for hub in (graph.nodes[i] for i in group):
            if hub.summary and hub.status == "candidate":
                text = f"{graph.name(hub.id, 'display')}（{KIND[hub.kind]}）：{hub.summary}{_when(hub.valid, on)}"
                clue_raw.append(Line(False, True, 1, max(s.at or "" for s in hub.sources), text + _clue(hub.sources), group))
        clue_raw.extend(_ranked([_edge_line(graph, e, hub, o, clue_conn[o.id], on)
                                 for e, hub, o in links])[:CLUES_PER_ENTITY])
    clue_lines = _ranked(clue_raw)[:CLUES_PER_CLIP]
    return GraphContext(COMBINED, "", graph, tuple(nodes), tuple([*accepted_lines, *clue_lines]))


def clip_day(ctx: Ctx) -> dt.date | None:
    """The day the clip was recorded (the clip record's recording_date), the day its news is placed against."""
    day = ctx.state.data.get("clip", {}).get("recording_date")
    return dt.date.fromisoformat(str(day)) if day else None


def read(ctx: Ctx, texts: Iterable[str], chat: Iterable[str] = ()) -> GraphContext:
    """Load the graph, find the entities `texts` name and build their background (no lines when it cannot be read).
    `chat` (the clip's danmaku) brings in circle terms only: viewers name what they see (「嗦面」 when the host cries,
    U430), while the people they name are not the clip's subject."""
    loaded = load(ctx)
    if loaded.graph is None:
        return loaded
    named = mentions(loaded.graph, texts)
    seen = {n.id for n in named}
    named += [n for n in mentions(loaded.graph, chat) if n.kind == "term" and n.id not in seen]
    built = background(loaded.graph, named, clip_day(ctx))
    return replace(loaded, mentioned=built.mentioned, lines=built.lines)


def process_line(record: GraphRecord) -> str:
    """The one PROCESS line: how the graph was read, what it brought, what it saved from the web."""
    got = record["lines"]
    text = (f"知识图：{STATUS_TEXT[record['status']]}；本片提到 {len(record['mentioned'])} 个图中实体，"
            f"进入提示 {got['verified']} 行已认可、{got['clues']} 行线索")
    if record["skipped_search"]:
        text += "；图里已有而免于上网：" + "、".join(record["skipped_search"])
    return text + (f"（{record['reason']}）" if record["reason"] else "")
