"""Step 6, after the main line is settled and before the title is written: look up the circle references it needs.

The merge (title_understanding) names the references the main line or the punchline depends on and neither the subtitles
nor persona.md explain (`unexplained`).  The knowledge graph comes first (graph_context): a reference the graph knows,
and whose background is already in the prompt, is not searched.  For each of the first three of the others the lane
searches the web through the lookup adapter's Search Router (work_lookup.WebLookup, only when MANUAL_LANE_SEARCH_MCP_URL
is configured), asks one model call (purpose `title-context`) to write the background from the results alone, and keeps
only sources that really are among the results.  Every reference ends in one status and leaves a trace in title/context.json and in PROCESS:

- FOUND: a note with at least one source;
- NOT_FOUND: searched, and the results do not explain it;
- UNCONFIGURED: no search entry is configured;
- FAILED: the search or the model call failed.

No status stops the step.  The FOUND notes go to the title prompt (and the taste ranking) after the main line, as help
to read references and jokes only; every fact of a title is still the subtitles'.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING, Protocol, TypedDict, cast
from urllib.parse import urlparse

from src.autoslice.channel_envelope import HOST_NAME

from .graph_context import GraphContext, GraphRecord, process_line
from .runner import Ctx
from .srt_text import _json_from_text
from .step_io import _ask_cpa, _cpa_ok, _local, _log, _render

if TYPE_CHECKING:
    from .state import CpaEvent
    from .title_understanding import Merged, Unexplained

PURPOSE = "title-context"
CALL_ID = "title-context"
MAX_TERMS = 3
RESULTS_KEPT = 8
SNIPPET_MAX = 300
NEIGHBOURS = 2                      # subtitle sentences kept before and after a cue
CUES_KEPT = 3                       # cues per reference whose neighbourhood goes to the model
SHAPE_REASON = "TITLE_CONTEXT_SHAPE"
FOUND, NOT_FOUND, UNCONFIGURED, FAILED = "FOUND", "NOT_FOUND", "UNCONFIGURED", "FAILED"
BLOCK_HEAD = "圈内背景（自动检索，带出处；只用来看懂指代和梗，标题里的事实仍以字幕为准）"


class Hit(TypedDict):
    title: str
    url: str
    snippet: str


class Query(TypedDict):
    query: str
    results: list[Hit]            # the first RESULTS_KEPT results
    error: str                    # "" when the search ran


class TermContext(TypedDict):
    term: str
    cues: list[int]
    why: str
    queries: list[Query]
    status: str                   # FOUND, NOT_FOUND, UNCONFIGURED or FAILED
    note: str                     # only when FOUND
    sources: list[str]            # only when FOUND; each is the url of a kept result
    reason: str                   # why the status is not FOUND ("" when FOUND)


class CircleContext(TypedDict):
    terms: list[TermContext]
    beyond_limit: list[str]       # references the merge named past the MAX_TERMS
    call_id: str                  # the model call that wrote the notes ("" when none was made)
    graph: GraphRecord            # what the knowledge graph gave this clip and which references it saved from the web


class TermSummary(TypedDict):
    term: str
    status: str


class ModelRow(TypedDict, total=False):
    """One item of the model's answer (prompts/title_context.md), after _answer_problem."""
    term: str
    found: bool
    note: str
    sources: list[str]
    why_not: str


class Searcher(Protocol):
    """The lookup adapter's web search (work_lookup.WebLookup.web): the Search Router's rows with title, url, snippet."""
    web_configured: bool

    def web(self, query: str, *, limit: int) -> object: ...


def _numbered(lines: str) -> dict[int, str]:
    """The prompt's numbered subtitle lines by their number."""
    found: dict[int, str] = {}
    for line in lines.splitlines():
        match = re.match(r"(\d+) ", line)
        if match:
            found[int(match.group(1))] = line
    return found


def _neighbourhood(numbered: dict[int, str], cues: Sequence[int]) -> list[str]:
    wanted = sorted({n for cue in cues[:CUES_KEPT] for n in range(cue - NEIGHBOURS, cue + NEIGHBOURS + 1)} & numbered.keys())
    return [numbered[n] for n in wanted]


def _hits(rows: object) -> list[Hit]:
    return [Hit(title=str(row.get("title") or ""), url=str(row.get("url") or ""),
                snippet=str(row.get("snippet") or "")[:SNIPPET_MAX])
            for row in (rows if isinstance(rows, list) else [])[:RESULTS_KEPT] if isinstance(row, dict)]


def _mark(item: TermContext, status: str, reason: str = "", note: str = "", sources: Sequence[str] = ()) -> None:
    item["status"], item["reason"], item["note"], item["sources"] = status, reason, note, list(sources)


def _search(provider: Searcher, term: str) -> list[Query]:
    """The term's two queries; a search that raises is recorded on its query and the other still runs."""
    out: list[Query] = []
    for text in (f"{term} {HOST_NAME}", f"{term} 虚拟主播"):
        try:
            rows = provider.web(text, limit=RESULTS_KEPT)
            out.append(Query(query=text, results=_hits(rows), error=""))
        except Exception as exc:  # noqa: BLE001 - recorded on the query; the step is never blocked by a search
            out.append(Query(query=text, results=[], error=f"{type(exc).__name__}: {str(exc)[:200]}"))
    return out


def _results(item: TermContext) -> list[Hit]:
    """The term's kept results of both queries, one per url."""
    seen: dict[str, Hit] = {}
    for query in item["queries"]:
        for hit in query["results"]:
            seen.setdefault(hit["url"] or f"{len(seen)}:{hit['title']}", hit)
    return list(seen.values())


def _answer_problem(value: object, terms: Sequence[str]) -> str:
    """Why a model answer is not usable, or "": a list with one object per asked term (found true/false, the optional
    note and why_not strings, sources a list of strings)."""
    if not isinstance(value, list):
        return "not a list"
    for row in value:
        if not (isinstance(row, dict) and isinstance(row.get("term"), str) and isinstance(row.get("found"), bool)
                and isinstance(row.get("note", ""), str) and isinstance(row.get("why_not", ""), str)
                and isinstance(row.get("sources", []), list)
                and all(isinstance(u, str) for u in row.get("sources", []))):
            return "each item must be an object with term, found (true/false), note, sources (strings) and why_not"
    answered = [row["term"] for row in value]
    if sorted(answered) != sorted(terms):
        return f"the terms answered {answered} are not the terms asked {list(terms)}"
    return ""


def _contract(terms: Sequence[str]) -> Callable[[object], str]:
    def accepts(value: object) -> str:
        problem = _answer_problem(value, terms)
        return f"{SHAPE_REASON} ({problem})" if problem else ""
    return accepts


def _failure(record: CpaEvent) -> str:
    return str(record.get("reason") or record.get("stderr_tail") or "no answer").strip().splitlines()[0][:200]


def _settle(item: TermContext, row: ModelRow) -> None:
    """Take the model's row for one term: FOUND only with a note and a source that is really among the results."""
    allowed = {hit["url"] for hit in _results(item) if hit["url"]}
    sources = [u for u in dict.fromkeys(row.get("sources", [])) if u in allowed]
    note = row.get("note", "").strip()
    if row["found"] and note and sources:
        _mark(item, FOUND, note=note, sources=sources)
    elif row["found"]:
        _mark(item, NOT_FOUND, "模型说查到了，但没有说明或没有出处在检索结果里")
    else:
        _mark(item, NOT_FOUND, row.get("why_not", "").strip() or "检索结果说明不了")


def _explain(ctx: Ctx, pending: list[TermContext], numbered: dict[int, str], cpa_calls: list[CpaEvent]) -> str:
    """One model call for the terms that have results; returns its call id.  A failure marks those terms FAILED."""
    items = [{"term": t["term"], "cues": t["cues"], "why": t["why"], "subtitles": _neighbourhood(numbered, t["cues"]),
              "results": _results(t)} for t in pending]
    prompt = _render("title_context.md", items=json.dumps(items, ensure_ascii=False, indent=1))
    terms = [t["term"] for t in pending]
    reason = ""
    answer: object = None
    record: CpaEvent | None = None
    try:
        record = _ask_cpa(ctx, CALL_ID, prompt, purpose=PURPOSE, accepts=_contract(terms))
    except Exception as exc:  # noqa: BLE001 - recorded as the reason; the step is never blocked by this call
        reason = f"{type(exc).__name__}: {str(exc)[:200]}"
    if record is not None:
        cpa_calls.append(_log(record))
        if not _cpa_ok(record):
            reason = _failure(record)
        else:
            try:
                answer = _json_from_text(record["response_text"])
            except ValueError as exc:
                reason = f"CPA_RESPONSE_UNPARSEABLE ({str(exc)[:160]})"
            else:
                reason = _answer_problem(answer, terms)
    if reason or not isinstance(answer, list):
        for item in pending:
            _mark(item, FAILED, reason or "no answer")
        return CALL_ID
    rows = {str(row["term"]): cast(ModelRow, row) for row in answer}
    for item in pending:
        _settle(item, rows[item["term"]])
    return CALL_ID


def gather(ctx: Ctx, merged: Merged, lines: str, cpa_calls: list[CpaEvent], graph: GraphContext) -> CircleContext:
    """Search and explain the merge's unexplained references that the graph does not cover, write title/context.json,
    return the result.  `lines` is the numbered subtitles the title prompt carries (the cues in `unexplained` are its
    numbers)."""
    asked: list[Unexplained] = []
    in_graph: list[str] = []
    for entry in merged.get("unexplained", []):
        if graph.covers(entry["term"]):
            in_graph.append(entry["term"].strip())
        elif all(entry["term"].strip() != other["term"].strip() for other in asked):
            asked.append(entry)
    terms = [TermContext(term=e["term"].strip(), cues=list(e["cues"]), why=e["why"], queries=[], status="", note="",
                         sources=[], reason="") for e in asked[:MAX_TERMS]]
    provider: Searcher = ctx.adapters.lookup
    configured = getattr(provider, "web_configured", False) is True      # an unconfigured adapter must not read as set
    pending: list[TermContext] = []
    for item in terms:
        if not configured:
            _mark(item, UNCONFIGURED, "未配置检索入口")
            continue
        item["queries"] = _search(provider, item["term"])
        if not _results(item):
            errors = [q["error"] for q in item["queries"] if q["error"]]
            if len(errors) == len(item["queries"]):
                _mark(item, FAILED, "；".join(dict.fromkeys(errors)))
            else:
                _mark(item, NOT_FOUND, "检索没有返回结果")
            continue
        pending.append(item)
    call_id = _explain(ctx, pending, _numbered(lines), cpa_calls) if pending else ""
    found = CircleContext(terms=terms, beyond_limit=[e["term"] for e in asked[MAX_TERMS:]], call_id=call_id,
                          graph=graph.record(list(dict.fromkeys(in_graph))))
    _local(ctx, "title/context.json").write_text(json.dumps(found, ensure_ascii=False, indent=2), encoding="utf-8")
    return found


def _domain(url: str) -> str:
    return urlparse(url).hostname or url


def block(found: CircleContext) -> str:
    """The FOUND notes as one block that follows the main line in the title prompt; "" when none was found."""
    rows = [f"- {t['term']}：{t['note']}（出处：{'、'.join(dict.fromkeys(_domain(u) for u in t['sources']))}）"
            for t in found["terms"] if t["status"] == FOUND]
    return "\n".join([BLOCK_HEAD, *rows]) if rows else ""


def _outcome(item: TermContext) -> str:
    if item["status"] == FOUND:
        return f"已查到（出处 {len(item['sources'])} 个）"
    if item["status"] == UNCONFIGURED:
        return "未检索（未配置检索入口）"
    return f"{'检索失败' if item['status'] == FAILED else '未查清'}（{item['reason']}）"


def process_lines(found: CircleContext) -> list[str]:
    """PROCESS: one line per reference with what became of it, or one line saying there was none to look up."""
    head = [process_line(found["graph"])]
    if not found["terms"]:
        return [*head, "圈内背景：主线里没有需要另查的指称"]
    out = [*head, *(f"圈内背景：{t['term']} → {_outcome(t)}" for t in found["terms"])]
    if found["beyond_limit"]:
        out.append(f"圈内背景：另有 {len(found['beyond_limit'])} 个指称超过上限 {MAX_TERMS}，未检索："
                   + "、".join(found["beyond_limit"]))
    return out


def summary(found: CircleContext) -> list[TermSummary]:
    """What title/candidates.json keeps of it: each reference and its status."""
    return [TermSummary(term=t["term"], status=t["status"]) for t in found["terms"]]
