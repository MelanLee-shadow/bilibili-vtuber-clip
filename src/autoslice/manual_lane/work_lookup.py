"""Work and proper-name lookup for a doubt (TASK-5 item 5; TASK-6 C).

CPA names the work or proper name a sentence is about (`work_query`).  The lookup goes through the layers in order and
returns every result and every failure as evidence; a failing layer is recorded and the next layer runs; nothing here
ever blocks a doubt.

1. topic_entity_graph (this channel's own entity graph): a match answers the query, no network.
2. Bangumi public subject search.
3. Chinese Wikipedia search.
4. Baidu Baike lemma card, exact lemma only: the query itself first, then the formal names found by layers 2-3.
5. Web search fallback, only when no result of layers 1-4 names the query itself (fuzzy engines return something for
   almost any keyword): the Search Router MCP, only when
   the profile's manual_lane.json sets search_mcp_url or MANUAL_LANE_SEARCH_MCP_URL does (the address is written once,
   in the profile's manual_lane.json).  CPA's /responses with a
   web_search tool was probed and did not search (no web_search_call, no cited URL), so it is not a layer.

Every network layer goes through `WebLookup` (adapters.lookup): a timeout and a User-Agent on every request.
"""

from __future__ import annotations

import http.client
import json
import re
import urllib.parse
import urllib.request
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING, NotRequired, Protocol, TypedDict, cast

from . import channel
from . import text_overlap
from .layout import layout

if TYPE_CHECKING:
    from src.autoslice.cpa_gemini_client import JsonValue

BANGUMI_SEARCH_URL = "https://api.bgm.tv/v0/search/subjects"
BANGUMI_SUBJECT_URL = "https://api.bgm.tv/v0/subjects"
BANGUMI_CHARACTER_URL = "https://api.bgm.tv/v0/characters"
CHARACTER_RELATIONS = ("主角", "配角")
CHARACTER_LIMIT = 12
ZHWIKI_SEARCH_URL = "https://zh.wikipedia.org/w/api.php"
BAIKE_CARD_URL = "https://baike.baidu.com/api/openapi/BaikeLemmaCardApi"
SEARCH_MCP_ENV = "MANUAL_LANE_SEARCH_MCP_URL"
USER_AGENT = "vtuber-slice-manual-lane/1.0 (subtitle doubt lookup)"
TIMEOUT_S = 15.0
GRAPH_PATH = channel.asset("topic_entity_graph.json")


class TopicEntity(TypedDict, total=False):
    canonical_zh: str
    native_names: list[str]
    aliases: list[str]
    readings: list[str]
    entity_id: str
    kind: str


class TopicGraph(TypedDict, total=False):
    entities: list[TopicEntity]


class LookupRow(TypedDict, total=False):
    id: str | int | None
    type: int | None
    name: str | None
    name_cn: str | None
    summary: str
    entity_id: str | None
    kind: str | None
    matched: str
    title: str | None
    snippet: str
    abstract: str
    url: str | None
    layer: str


class CharacterRow(TypedDict):
    id: int
    relation: str
    name: str | None
    names_cn: list[str]


class SpokenNames(TypedDict):
    names_cn: list[str]


class LookupAttempt(TypedDict, total=False):
    query: str
    error: str


class LookupLayer(TypedDict):
    layer: str
    results: list[LookupRow]
    query: NotRequired[str]
    error: NotRequired[str]
    skipped: NotRequired[str]
    also_tried: NotRequired[list[LookupAttempt]]


class LayerSummary(TypedDict, total=False):
    layer: str
    query: str
    error: str
    skipped: str
    also_tried: list[LookupAttempt]
    n: int


class LookupResult(TypedDict):
    query: str
    source: str
    results: list[LookupRow]
    layers: list[LayerSummary]


class RpcRequest(TypedDict, total=False):
    jsonrpc: str
    id: int
    method: str
    params: dict[str, JsonValue]


class LookupProvider(Protocol):
    web_configured: bool

    def bangumi(self, query: str) -> list[LookupRow]: ...
    def bangumi_characters(self, subject_id: int) -> list[CharacterRow]: ...
    def zhwiki(self, query: str) -> list[LookupRow]: ...
    def baike(self, query: str) -> list[LookupRow]: ...
    def web(self, query: str) -> list[LookupRow]: ...


def topic_hits(query: str, graph: TopicGraph) -> list[LookupRow]:
    """Entities whose Chinese name, native names, aliases or readings match the query (either contains the other)."""
    q = text_overlap.key(query)
    if len(q) < 2:
        return []
    hits: list[LookupRow] = []
    for entity in graph.get("entities", []):
        surfaces = [entity.get("canonical_zh", ""), *entity.get("native_names", []), *entity.get("aliases", []),
                    *entity.get("readings", [])]
        for surface in surfaces:
            s = text_overlap.key(str(surface))
            if len(s) >= 2 and (s == q or s in q or q in s):
                hits.append({"name": entity.get("canonical_zh"), "entity_id": entity.get("entity_id"),
                             "kind": entity.get("kind"), "matched": str(surface)})
                break
    return hits


def _strip_html(text: str) -> str:
    return re.sub(r"<[^>]+>", "", str(text or ""))


def _chinese_spellings(infobox: object) -> list[str]:
    """The Chinese spellings of a Bangumi character page: the value of 简体中文名, and the values of the 别名 entries
    keyed 第二中文名 or any key containing 中文; de-duplicated, in page order."""
    names: list[str] = []
    for item in infobox if isinstance(infobox, list) else []:
        key, value = item.get("key"), item.get("value")
        if key == "简体中文名" and isinstance(value, str):
            names.append(value.strip())
        elif key == "别名" and isinstance(value, list):
            names += [str(v.get("v") or "").strip() for v in value
                      if isinstance(v, dict) and "中文" in str(v.get("k") or "")]
    return list(dict.fromkeys(n for n in names if n))


_NAME_SPLIT = re.compile(r"[·・•\s]+")
_HAN_RUN = re.compile(r"[一-鿿]")


def spoken_names(characters: Sequence[SpokenNames]) -> list[str]:
    """The names a speaker could say for these characters: each Chinese spelling whole and each part of it split at
    ·、・、•、space; only those with two or more Chinese characters; de-duplicated, in order."""
    out: list[str] = []
    for character in characters:
        for spelling in character.get("names_cn") or []:
            for candidate in (spelling, *_NAME_SPLIT.split(spelling)):
                candidate = candidate.strip()
                if len(_HAN_RUN.findall(candidate)) >= 2 and candidate not in out:
                    out.append(candidate)
    return out


class WebLookup:
    """The network side of the lookup (adapters.lookup).  Each method raises on a network error; lookup() records it."""

    def __init__(self, *, timeout: float = TIMEOUT_S, search_mcp_url: str | None = None) -> None:
        self.timeout = timeout
        self.search_mcp_url = search_mcp_url if search_mcp_url is not None else layout().search_mcp_url

    def _open(self, req: urllib.request.Request) -> tuple[http.client.HTTPMessage, str]:
        req.add_header("User-Agent", USER_AGENT)
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            return resp.headers, resp.read().decode("utf-8", "replace")

    def bangumi(self, query: str, *, limit: int = 5) -> list[LookupRow]:
        body = json.dumps({"keyword": query, "sort": "match", "filter": {"type": [2, 1]}}).encode("utf-8")
        _h, text = self._open(urllib.request.Request(BANGUMI_SEARCH_URL, data=body, method="POST",
                                                     headers={"Content-Type": "application/json"}))
        data = json.loads(text)
        return [{"id": item.get("id"), "name": item.get("name"), "name_cn": item.get("name_cn"),
                 "summary": str(item.get("summary") or "")[:160], "type": item.get("type")}
                for item in (data.get("data") or [])[:limit]]

    def bangumi_characters(self, subject_id: int, *, limit: int = CHARACTER_LIMIT) -> list[CharacterRow]:
        """The subject's lead and supporting characters (at most `limit`, in the order Bangumi returns them), each with
        the Chinese spellings its character page carries."""
        _h, text = self._open(urllib.request.Request(f"{BANGUMI_SUBJECT_URL}/{int(subject_id)}/characters"))
        related = [row for row in json.loads(text) if row.get("relation") in CHARACTER_RELATIONS][:limit]
        out: list[CharacterRow] = []
        for row in related:
            _h, detail = self._open(urllib.request.Request(f"{BANGUMI_CHARACTER_URL}/{int(row['id'])}"))
            out.append({"id": row["id"], "relation": row["relation"], "name": row.get("name"),
                        "names_cn": _chinese_spellings(json.loads(detail).get("infobox"))})
        return out

    def zhwiki(self, query: str, *, limit: int = 3) -> list[LookupRow]:
        params = urllib.parse.urlencode({"action": "query", "list": "search", "srsearch": query, "format": "json",
                                         "srlimit": limit})
        _h, text = self._open(urllib.request.Request(f"{ZHWIKI_SEARCH_URL}?{params}"))
        data = json.loads(text)
        return [{"title": row.get("title"), "snippet": _strip_html(row.get("snippet"))[:160]}
                for row in (data.get("query") or {}).get("search", [])[:limit]]

    def baike(self, query: str) -> list[LookupRow]:
        """Exact lemma only: the card's title must be the query itself."""
        params = urllib.parse.urlencode({"scope": 103, "format": "json", "appid": 379020, "bk_key": query,
                                         "bk_length": 300})
        _h, text = self._open(urllib.request.Request(f"{BAIKE_CARD_URL}?{params}"))
        data = json.loads(text) if text.strip() else {}
        title = str(data.get("title") or data.get("key") or "")
        if not title or text_overlap.key(title) != text_overlap.key(query):
            return []
        return [{"title": title, "abstract": str(data.get("abstract") or "")[:300], "url": data.get("url")}]

    def web(self, query: str, *, limit: int = 5) -> list[LookupRow]:
        """Search Router MCP (streamable HTTP): initialize, keep Mcp-Session-Id, notifications/initialized,
        tools/call free_search.  Only when configured; the caller checks `web_configured` first."""
        if not self.search_mcp_url:
            raise RuntimeError(f"{SEARCH_MCP_ENV} not configured")
        base = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}

        def post(payload: RpcRequest, session: str | None) -> tuple[http.client.HTTPMessage, str]:
            headers = dict(base, **({"Mcp-Session-Id": session} if session else {}))
            return self._open(urllib.request.Request(self.search_mcp_url, data=json.dumps(payload).encode("utf-8"),
                                                     method="POST", headers=headers))

        headers, _ = post({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                           "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                                      "clientInfo": {"name": "vtuber-slice-manual-lane", "version": "1.0"}}}, None)
        session = headers.get("Mcp-Session-Id") or headers.get("mcp-session-id")
        post({"jsonrpc": "2.0", "method": "notifications/initialized"}, session)
        _h, text = post({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                         "params": {"name": "free_search", "arguments": {"query": query}}}, session)
        return _mcp_results(text)[:limit]

    @property
    def web_configured(self) -> bool:
        return bool(self.search_mcp_url)


def _mcp_results(text: str) -> list[LookupRow]:
    """The tools/call result, from a JSON body or an SSE stream: [{title, url, snippet}] where the tool gives them."""
    payloads = []
    for line in text.splitlines():
        if line.startswith("data:"):
            payloads.append(line[5:].strip())
    if not payloads:
        payloads = [text]
    for raw in payloads:
        try:
            msg = json.loads(raw)
        except ValueError:
            continue
        if "error" in msg:
            raise RuntimeError(f"MCP error: {str(msg['error'])[:200]}")
        result = msg.get("result") or {}
        rows: list[LookupRow] = []
        structured = result.get("structuredContent")
        if isinstance(structured, dict):
            rows = structured.get("results") or []
        if not rows:
            for item in result.get("content") or []:
                if item.get("type") == "text":
                    try:
                        inner = json.loads(item["text"])
                        rows = cast(list[LookupRow], inner.get("results") if isinstance(inner, dict) else inner)
                    except ValueError:
                        rows = [{"snippet": item["text"][:300]}]
        return [{"title": r.get("title"), "url": r.get("url"), "snippet": str(r.get("snippet") or r.get("content")
                                                                                 or "")[:200]}
                for r in rows if isinstance(r, dict)]
    return []


def _formal_names(layers: Sequence[LookupLayer]) -> list[str]:
    names: list[str] = []
    for layer in layers:
        for row in layer.get("results") or []:
            for k in ("name_cn", "name", "title"):
                v = str(row.get(k) or "").strip()
                if v and v not in names:
                    names.append(v)
    return names[:4]


def lookup(query: str, graph: TopicGraph, *, web: LookupProvider) -> LookupResult:
    """{query, source, results, layers}: `layers` records every layer tried (results or error); `source` names the
    layers that found something; `results` is their union, each row tagged with its layer."""
    layers: list[LookupLayer] = []
    hits = topic_hits(query, graph)
    layers.append({"layer": "topic_entity_graph", "results": hits})
    if not hits:
        calls: list[tuple[str, Callable[[], list[LookupRow]]]] = [
            ("bangumi", lambda: web.bangumi(query)), ("zhwiki", lambda: web.zhwiki(query))]
        for name, call in calls:
            layers.append(_try(name, query, call))
        baike = _try("baike", query, lambda: web.baike(query))
        if not baike["results"]:
            for formal in _formal_names(layers):
                def try_baike(f: str = formal) -> list[LookupRow]:
                    return web.baike(f)

                again = _try("baike", formal, try_baike)
                baike.setdefault("also_tried", []).append(cast(LookupAttempt,
                    {"query": formal, **{k: v for k, v in again.items() if k in ("error",)}}))
                if again["results"]:
                    baike["results"] = again["results"]
                    break
        layers.append(baike)
        if not any(_mentions(query, row) for layer in layers for row in layer["results"]):
            if getattr(web, "web_configured", False):
                layers.append(_try("search_router", query, lambda: web.web(query)))
            else:
                layers.append({"layer": "search_router", "results": [], "skipped": f"{SEARCH_MCP_ENV} 未配置"})
    found = [layer for layer in layers if layer["results"]]
    return {"query": query, "source": "+".join(layer["layer"] for layer in found) or "none",
            "results": [cast(LookupRow, dict(row, layer=layer["layer"]))
                        for layer in found for row in layer["results"]],
            "layers": [cast(LayerSummary, {k: v for k, v in layer.items() if k != "results"}
                            | {"n": len(layer["results"])}) for layer in layers]}


def _mentions(query: str, row: LookupRow) -> bool:
    """A result names the query itself (in any text field).  Fuzzy search engines return something for almost any
    keyword; only a result that carries the query's own words counts as found for the web fallback decision."""
    q = text_overlap.key(query)
    return bool(q) and any(q in text_overlap.key(str(v)) for v in row.values() if isinstance(v, str))


def _try(name: str, query: str, call: Callable[[], list[LookupRow]]) -> LookupLayer:
    try:
        return {"layer": name, "query": query, "results": list(call() or [])}
    except Exception as exc:  # noqa: BLE001 - recorded as evidence; the next layer runs, the doubt is never blocked
        return {"layer": name, "query": query, "results": [], "error": f"{type(exc).__name__}: {str(exc)[:200]}"}
