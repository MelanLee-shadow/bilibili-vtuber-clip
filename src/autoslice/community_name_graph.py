"""Community names (the community-name crawler's accepted relations) folded into the candidate knowledge graph.

公开规则on the names the crawler reported: 「有进行查重吗以及，知道这些梗的来源？…海獭冲击竟然是今天才出来的
新梗？明明都已经好久了。话说这些在知识图里吗，还是额外的？」. So, for every accepted relation of
state/community_names.json:

- dedupe against the graph the lane reads (repository + runtime candidates): a spelling some other node already owns
 is reported as already known and not added again;
- its time is the earliest video the crawler holds as evidence for it (state/community_name_state.json), and, when the
 web says so, the earliest date a search result states for it (`lookup_origins`); `first_seen_at`, the day the crawler
 first saw it, is never presented as the meme's age;
- its meaning and origin come from a CPA reading of search results, kept in an origins file between runs;
- it becomes a candidate node (term, or community for a fan name) with an edge to its person: alias_of, fan_name_of,
 or related_to for a meme / associated word. The person is the graph's node with the canonical spelling, else a
 candidate person node with the official space as source.

The news-event crawler, the one writer of the runtime graph, calls `fold` in its run.
"""

from __future__ import annotations

import datetime as dt
import json
import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TypedDict

from src.autoslice.community_name_crawler import _normalized_rows, _search_rows
from src.autoslice.knowledge_graph import Edge, Graph, JsonValue, Node, Source
from src.autoslice.timely_term_crawler import BoundedHttpClient
from src.autoslice.news_event_judge import Row

SNAPSHOT_SCHEMA = "vtuber-slice.community-names.v1"   # = community_name_crawler.SNAPSHOT_SCHEMA (test pins it)
STATE_SCHEMA = "vtuber-slice.community-name-state.v2"  # = community_name_crawler.STATE_SCHEMA
ORIGINS_SCHEMA = "community-name-origins.v1"
ORIGINS_FILE = "community_name_origins.json"       # beside the runtime graph root, kept across runs
ORIGIN_BATCH = 6
ORIGIN_LIMIT = 40                                  # names looked up per run; the rest wait for the next runs
RELOOK_DAYS = 30                                   # a name the web said nothing about is asked again after this
MEANING_CHARS = 60
ORIGIN_CHARS = 100
_EDGE: dict[str, tuple[str | None, str, str | None]] = {
    "alias_of": (None, "alias_of", None),          # an alias is a node of its person's own kind (person/character)
    "fan_name_of": ("community", "fan_name_of", None),
    "meme_of": ("term", "related_to", "梗"), "associated_with": ("term", "related_to", "社区里和他相关的称呼")}
_CJK = re.compile("^[\u3400-\u9fff\u3040-\u30ff]+")
Folded = tuple[dict[str, Node], dict[str, Edge]]


class OriginError(ValueError):
    """A snapshot, origins file or CPA answer about origins is not usable."""


class Relation(TypedDict):
    entity_id: str
    canonical: str
    surface: str
    kind: str
    first_seen_at: str | None
    video_at: str | None
    video_url: str | None


class Origin(TypedDict):
    meaning: str
    origin: str
    earliest: str | None             # a date a search result or video states for it (from CPA)
    date_basis: str
    url: str | None
    looked_at: str
    first_video_at: str | None       # the oldest Bilibili video found by searching the name itself (mechanical)
    first_video_url: str | None
    probed: bool                     # whether the Bilibili search was consulted


class Video(TypedDict):
    title: str
    url: str
    published_at: str
    text: str


def _date(value: object) -> str | None:
    return value[:10] if isinstance(value, str) and re.match(r"^\d{4}-\d{2}-\d{2}", value) else None


def _str(value: object) -> str | None:
    return value if isinstance(value, str) else None


@dataclass(frozen=True)
class Community:
    """What the news crawler folds in: the relations (with their earliest videos) and where origins are kept."""
    relations: tuple[Relation, ...]
    origins_path: Path


def _read(path: Path, schema: str) -> dict[str, JsonValue]:
    doc = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(doc, dict) or doc.get("schema_version") != schema:
        raise OriginError(f"{path}: not {schema}")
    return doc


def load(names_path: Path, state_path: Path | None, origins_path: Path, notes: list[str]) -> Community:
    """The snapshot must be readable (raises otherwise); without the state the names keep no video date, with a note."""
    snapshot = _read(names_path, SNAPSHOT_SCHEMA)
    state = None
    if state_path is not None:
        try:
            state = _read(state_path, STATE_SCHEMA)
        except (OSError, ValueError) as exc:
            notes.append(f"community name state unavailable (no earliest videos): {type(exc).__name__}: {str(exc)[:200]}")
    return Community(tuple(relations(snapshot, state)), Path(origins_path))


def _list(doc: dict[str, JsonValue] | None, name: str) -> list[dict[str, JsonValue]]:
    value = (doc or {}).get(name)
    return [v for v in value if isinstance(v, dict)] if isinstance(value, list) else []


def relations(snapshot: dict[str, JsonValue], state: dict[str, JsonValue] | None) -> list[Relation]:
    """The accepted relations, each with the earliest video the crawler holds for it (when the state is readable)."""
    earliest: dict[tuple[str | None, str | None], tuple[str, str]] = {}
    for row in _list(state, "mappings"):
        dated = [(str(e["published_at"]), _str(e.get("url")) or "") for e in _list(row, "evidence")
                 if _date(e.get("published_at"))]
        if dated:
            earliest[(_str(row.get("entity_id")), _str(row.get("surface")))] = min(dated)
    out: list[Relation] = []
    for rel in _list(snapshot, "relations"):
        entity, canonical, surface, kind = (_str(rel.get(k)) for k in ("entity_id", "canonical", "surface",
                                                                       "relation_kind"))
        if not (entity and canonical and surface and kind in _EDGE):
            raise OriginError(f"bad relation {str(rel)[:200]}")
        first = earliest.get((entity, surface))
        out.append(Relation(entity_id=entity, canonical=canonical, surface=surface, kind=kind,
                            first_seen_at=_str(rel.get("first_seen_at")),
                            video_at=_date(first[0]) if first else None, video_url=first[1] or None if first else None))
    return out


def key(rel: Relation) -> str:
    return f"{rel['entity_id']}|{rel['surface']}"


def _origin(value: JsonValue) -> Origin:
    if not isinstance(value, dict) or not isinstance(value.get("looked_at"), str):
        raise OriginError(f"bad origin {str(value)[:200]}")
    return Origin(meaning=_str(value.get("meaning")) or "", origin=_str(value.get("origin")) or "",
                  earliest=_str(value.get("earliest")), date_basis=_str(value.get("date_basis")) or "",
                  url=_str(value.get("url")), looked_at=str(value["looked_at"]),
                  first_video_at=_str(value.get("first_video_at")), first_video_url=_str(value.get("first_video_url")),
                  probed=value.get("probed") is True)


def load_origins(path: Path) -> dict[str, Origin]:
    if not path.is_file():
        return {}
    doc = _read(path, ORIGINS_SCHEMA)
    got = doc.get("origins")
    if not isinstance(got, dict):
        raise OriginError(f"{path}: origins must be an object")
    return {k: _origin(v) for k, v in got.items()}


def save_origins(path: Path, origins: dict[str, Origin]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps({"schema_version": ORIGINS_SCHEMA, "origins": dict(sorted(origins.items()))},
                              ensure_ascii=False, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def _short(canonical: str) -> str:
    match = _CJK.match(canonical)
    return match.group(0) if match and len(match.group(0)) >= 2 else canonical


Item = tuple[Relation, Sequence[Row], Sequence[Video]]
VIDEOS_IN_PROMPT = 8


def origin_prompt(today: str, items: Sequence[Item]) -> str:
    blocks: list[JsonValue] = []
    for rel, rows, videos in items:
        results: list[JsonValue] = [{"title": r.get("title"), "url": r["url"], "snippet": r.get("snippet")} for r in rows]
        clips: list[JsonValue] = [{"title": v["title"], "url": v["url"], "published": v["published_at"][:10],
                                   "text": v["text"]} for v in videos[:VIDEOS_IN_PROMPT]]
        blocks.append({"key": key(rel), "name": rel["surface"], "of": rel["canonical"], "kind": rel["kind"],
                       "results": results, "bilibili_videos": clips})
    return (
        f"今天是 {today}。下面每条是 B 站虚拟主播圈里一个称呼或梗（name），以及它所指的主播（of），附网页搜索结果"
        "（results）和按这个名字搜到的 B 站视频（bilibili_videos，按发布日期从早到晚）。\n"
        "请只根据这些材料，说明它是什么意思、从哪来、最早什么时候出现。\n"
        f"- meaning：一句话说清意思（不超过 {MEANING_CHARS} 字）；结果里看不出就写空字符串，不要猜。\n"
        f"- origin：出处或起因（哪次直播、哪个视频、什么事），不超过 {ORIGIN_CHARS} 字；看不出写空字符串。\n"
        "- earliest：结果里写明的、它最早出现的日期 YYYY-MM-DD；结果里没有写明日期就写 null。\n"
        "- date_basis：earliest 所依据的原文日期文字，照抄；earliest 为 null 时写空字符串。\n"
        "- url：支持 earliest 或 origin 的那条结果或视频的 url，必须是给出的 url 之一；没有就写 null。\n\n"
        + json.dumps(blocks, ensure_ascii=False, indent=1)
        + '\n\n只输出 JSON：{"origins": [{"key": "...", "meaning": "...", "origin": "...", "earliest": null, '
          '"date_basis": "", "url": null}]}，每个 key 恰好一条。'
    )


def parse_origins(raw: str, items: Sequence[Item], today: str) -> tuple[dict[str, Origin], list[str]]:
    """The usable rows, and what was wrong with the others (a row that breaks the contract is dropped alone: that name
    is asked again next run).  An answer that is not the requested JSON at all raises."""
    match = re.search(r"\{.*\}", raw, re.S)
    try:
        doc = json.loads(match.group(0) if match else raw)
    except json.JSONDecodeError as exc:
        raise OriginError(f"not JSON: {exc}") from exc
    rows = doc.get("origins") if isinstance(doc, dict) else None
    if not isinstance(rows, list):
        raise OriginError("no origins list")
    urls = {key(rel): {r["url"] for r in found} | {v["url"] for v in videos} for rel, found, videos in items}
    first = {key(rel): videos[0] if videos else None for rel, _, videos in items}
    out: dict[str, Origin] = {}
    problems: list[str] = []
    answered: set[str] = set()
    for row in rows:
        k = _str(row.get("key")) if isinstance(row, dict) else None
        if not isinstance(row, dict) or k is None or k not in urls or k in answered:
            problems.append(f"bad origin row {str(row)[:200]}")
            continue
        answered.add(k)
        earliest, basis, url = row.get("earliest"), (_str(row.get("date_basis")) or "").strip(), row.get("url")
        if earliest is not None and (not isinstance(earliest, str) or _date(earliest) != earliest
                                     or earliest > today or not basis):
            problems.append(f"{k}: earliest {earliest!r} needs a past YYYY-MM-DD and its date text")
            continue
        if url is not None and url not in urls[k]:
            problems.append(f"{k}: url is not one of the results")
            continue
        video = first[k]
        out[k] = Origin(meaning=(_str(row.get("meaning")) or "").strip()[:MEANING_CHARS],
                        origin=(_str(row.get("origin")) or "").strip()[:ORIGIN_CHARS],
                        earliest=earliest, date_basis=basis[:80], url=_str(url), looked_at=today,
                        first_video_at=video["published_at"][:10] if video else None,
                        first_video_url=video["url"] if video else None, probed=True)
    problems += [f"{k}: no answer" for k in sorted(set(urls) - answered)]
    return out, problems


def probe_videos(client: BoundedHttpClient, *, endpoint: str, max_results: int, query: str, mixin_key: str,
                 now: dt.datetime) -> list[Video]:
    """Bilibili video search for one name (totalrank, first page, any age), with the community-name crawler's own
    search and filter (title, description or tags must carry the name), oldest first.  The crawler's search stays
    member-anchored and windowed; this one dates a name."""
    raw, _ = _search_rows(client, endpoint=endpoint, query=query, order="totalrank", page=1,
                          max_results=max_results, mixin_key=mixin_key, signed_at=now)
    # An answer without a result list raises (CrawlError): seen for 鼠鼠, which has 43 videos, so it is a failed
    # search (asked again next run), not "no videos".
    rows = _normalized_rows(raw, member={"canonical": query, "official_surfaces": [], "aliases": []}, now=now,
                            lookback_days=100 * 366)
    return sorted((Video(title=str(r["title"]), url=str(r["url"]), published_at=str(r["published_at"]),
                         text=str(r["description"])[:160]) for r in rows), key=lambda v: v["published_at"])


def _due(rel: Relation, origins: dict[str, Origin], today: dt.date, probing: bool) -> bool:
    got = origins.get(key(rel))
    if got is None or (probing and not got["probed"]):
        return True
    if got["meaning"]:
        return False
    return (today - dt.date.fromisoformat(got["looked_at"])).days >= RELOOK_DAYS


def _video_dated(prior: Origin | None, video: Video, today: dt.date) -> Origin:
    """The name dated by its oldest Bilibili video although CPA's answer for it was unusable: what an earlier run
    wrote is kept, and a name without a meaning stays due (looked at RELOOK_DAYS ago) so the next run asks again."""
    out = Origin(meaning="", origin="", earliest=None, date_basis="", url=None,
                 looked_at=(today - dt.timedelta(days=RELOOK_DAYS)).isoformat(), first_video_at=None,
                 first_video_url=None, probed=True)
    if prior is not None and prior["meaning"]:
        out = prior.copy()
    out["first_video_at"], out["first_video_url"], out["probed"] = video["published_at"][:10], video["url"], True
    return out


def lookup_origins(rels: Sequence[Relation], origins: dict[str, Origin], *,
                   search: Callable[[str], Sequence[Row]], ask_json: Callable[[str], str], today: dt.date,
                   videos: Callable[[str], Sequence[Video]] | None = None, limit: int = ORIGIN_LIMIT,
                   notes: list[str] | None = None) -> dict[str, int]:
    """Look up, in place, the meaning and origin of up to `limit` names that have none yet (or were looked up before
    the Bilibili search was there).  Returns counts; each failure is also a line in `notes`."""
    stats = {"due": 0, "searched": 0, "search_failed": 0, "videos_found": 0, "video_search_failed": 0,
             "judged": 0, "judge_failed": 0}
    notes = [] if notes is None else notes
    due = [r for r in rels if _due(r, origins, today, videos is not None)]
    stats["due"] = len(due)
    items: list[Item] = []
    for rel in due[:limit]:
        rows: list[Row] = []
        found: list[Video] = []
        try:
            rows = list(search(f"{rel['surface']} {_short(rel['canonical'])} 梗 由来"))[:5]
            stats["searched"] += 1
        except Exception as exc:  # noqa: BLE001 - one failed search leaves this name for the next run
            stats["search_failed"] += 1
            notes.append(f"origin search {rel['surface']!r}: {type(exc).__name__}: {str(exc)[:200]}")
            continue
        if videos is not None:
            try:
                found = list(videos(rel["surface"]))
                stats["videos_found"] += bool(found)
            except Exception as exc:  # noqa: BLE001 - asked again next run
                stats["video_search_failed"] += 1
                notes.append(f"bilibili search {rel['surface']!r}: {type(exc).__name__}: {str(exc)[:200]}")
                continue
        items.append((rel, rows, found))
    for start in range(0, len(items), ORIGIN_BATCH):
        batch = items[start:start + ORIGIN_BATCH]
        try:
            got, problems = parse_origins(ask_json(origin_prompt(today.isoformat(), batch)), batch, today.isoformat())
        except Exception as exc:  # noqa: BLE001 - the batch's names are asked again next run
            got, problems = {}, []
            notes.append(f"origin batch {start // ORIGIN_BATCH}: {type(exc).__name__}: {str(exc)[:200]}")
        for rel, _, seen in batch:  # a failed answer voids what CPA wrote, never the mechanical video date
            if key(rel) not in got and seen:
                origins[key(rel)] = _video_dated(origins.get(key(rel)), seen[0], today)
        origins.update(got)
        stats["judged"] += len(got)
        stats["judge_failed"] += len(batch) - len(got)
        notes.extend(f"origin batch {start // ORIGIN_BATCH}: {p}" for p in problems)
    return stats


def _space(rel: Relation) -> tuple[str, str]:
    """Where the person is: the official space for a Bilibili uid, else the crawler's own entry (kind, ref)."""
    kind, _, mid = rel["entity_id"].partition(":")
    if kind == "bilibili" and mid.isdigit():
        return "official", f"https://space.bilibili.com/{mid}"
    return "crawler", f"community_names.json#{rel['entity_id']}"


def _person(graph: Graph, rel: Relation, at: str) -> tuple[str, str, Node | None]:
    """The id and kind of the person the name belongs to, and the candidate node to add when the graph has none."""
    for spelling in dict.fromkeys((rel["canonical"], _short(rel["canonical"]))):
        hits = [n for n in graph.find(spelling) if n.status != "retired" and n.kind in ("person", "character")]
        for status in ("accepted", "candidate"):
            same = [n for n in hits if n.status == status]
            if len(same) == 1:
                return same[0].id, same[0].kind, None
    node = Node(id=f"person:{rel['canonical']}", kind="person", names={"display": rel["canonical"]}, aliases=(),
                summary="", status="candidate", sources=(Source(kind=_space(rel)[0], ref=_space(rel)[1], at=at),))
    return node.id, node.kind, node


def _ours(node: Node, node_id: str) -> bool:
    """The node an earlier run of this fold wrote for the same name (any other owner of the spelling, a repository
    node of the same id included, means the graph already knows it)."""
    return node.id == node_id and node.status == "candidate" and all(s.kind == "crawler" for s in node.sources)


def fold(rels: Iterable[Relation], origins: dict[str, Origin], known: Graph, *, at: str
         ) -> tuple[Folded, dict[str, JsonValue]]:
    """Candidate nodes and edges for the names, and the report: which are new to the graph, which it already knew.
    Their sources carry `at` (this run): while the crawler's snapshot lists a name it stays in the graph."""
    folded, report, _ = _fold(rels, origins, known, at)
    return folded, report


def _fold(rels: Iterable[Relation], origins: dict[str, Origin], known: Graph, at: str
          ) -> tuple[Folded, dict[str, JsonValue], set[str]]:
    new: set[str] = set()
    nodes: dict[str, Node] = {}
    edges: dict[str, Edge] = {}
    known_names: list[JsonValue] = []
    added: list[JsonValue] = []
    for rel in rels:
        fixed_kind, edge_type, note = _EDGE[rel["kind"]]
        person_id, person_kind, person = _person(Graph([*known.nodes.values(), *nodes.values()], known.edges), rel, at)
        kind = fixed_kind or person_kind
        node_id = f"{kind}:{rel['surface']}"
        owners = [n for n in known.find(rel["surface"]) if n.status != "retired" and not _ours(n, node_id)]
        owners += [n for n in nodes.values() if rel["surface"] in n.surfaces() and n.id != node_id]
        if owners:
            known_names.append({"name": rel["surface"], "of": rel["canonical"], "in_graph_as": owners[0].id})
            continue
        if person is not None:
            nodes.setdefault(person.id, person)
        new.add(key(rel))
        got = origins.get(key(rel))
        dated = [(d, why, ref) for d, why, ref in (
            (got["earliest"] if got else None, "stated", got["url"] if got else None),
            (got["first_video_at"] if got else None, "bilibili_search", got["first_video_url"] if got else None),
            (rel["video_at"], "crawler_evidence", rel["video_url"])) if d]
        earliest, basis, earliest_ref = min(dated, key=lambda d: d[0]) if dated else (None, None, None)
        ref = earliest_ref or rel["video_url"] or (got["url"] if got else None) or _space(rel)[1]
        meaning = got["meaning"] if got else ""
        summary = "；".join(p for p in (meaning, f"来源：{got['origin']}" if got and got["origin"] else "") if p)
        nodes[node_id] = Node(id=node_id, kind=kind, names={"display": rel["surface"]}, aliases=(), summary=summary,
                              status="candidate", sources=(Source(kind="crawler", ref=ref, at=at),),
                              valid={"from": earliest, "until": None} if earliest else None)
        edge = Edge(id=f"{node_id}|{edge_type}|{person_id}", type=edge_type, from_id=node_id, to_id=person_id,
                    status="candidate", sources=(Source(kind="crawler", ref=ref, at=at),), note=note)
        edges[edge.id] = edge
        added.append({"name": rel["surface"], "of": rel["canonical"], "kind": rel["kind"], "earliest": earliest,
                      "earliest_source": basis,
                      "earliest_ref": ref, "meaning": meaning, "crawler_first_saw": rel["first_seen_at"]})
    report: dict[str, JsonValue] = {"relations": len(added) + len(known_names), "in_graph_now": added,
                                    "already_known": known_names}
    return (nodes, edges), report, new


def run(community: Community, known: Graph, *, search: Callable[[str], Sequence[Row]] | None,
        ask_json: Callable[[str], str], today: dt.date, at: str, notes: list[str], owner: str | None = None,
        videos: Callable[[str], Sequence[Video]] | None = None, limit: int = ORIGIN_LIMIT,
        ) -> tuple[Folded, dict[str, JsonValue]]:
    """Look up origins (when `search` is given: the run still has time), keep them, and fold the names into candidates.
    An unreadable origins file is left as it is (not overwritten) and this run folds without origins."""
    try:
        origins, keep = load_origins(community.origins_path), True
    except (OSError, ValueError) as exc:
        notes.append(f"community origins unreadable, not updated: {type(exc).__name__}: {str(exc)[:200]}")
        origins, keep = {}, False
    stats: dict[str, JsonValue] = {}
    if search is not None:  # only names new to the graph: one it already knows has its own entry there
        _, _, new = _fold(community.relations, {}, known, at)
        ordered = sorted((r for r in community.relations if key(r) in new), key=lambda r: r["canonical"] != owner)
        counts = lookup_origins(ordered, origins, search=search, ask_json=ask_json, today=today, videos=videos,
                                limit=limit, notes=notes)
        stats = {k: v for k, v in counts.items()}
        if keep:
            save_origins(community.origins_path, origins)
    candidates, report = fold(community.relations, origins, known, at=at)
    report["origins"] = {"known": sum(1 for o in origins.values() if o["meaning"]), **stats}
    return candidates, report
