"""What a clip refers to outside itself: found once, kept with the clip, read by every step.

Owner recognising a reference (a work, a person, a circle event, a saying or a meme that is quoted or
adapted) is one capability; it is not done once for the title and again for the text.  The title step already had the
lookup (circle_context: web search, one model call that writes only what the results say, sources kept); the subtitle
step, where a misheard name or saying does the damage, had none.

- the store: <clip>/context/references.json, one record per reference as it is written in the text (circle_context's
  TermContext, with the step that first asked).  Whoever needs a reference reads the store first; only what is not
  there is looked up, and the answer goes back into the store.
- the subtitle step (`for_subtitles`): one model call reads the recognisers' draft and names the references the draft
  itself does not explain, with its guess of what each stands for (a search lead, never a fact); the knowledge graph
  answers what it knows, the others are looked up; what was found goes to the subtitle agent as leads to check against
  the sound.  Made once per draft: the runs of the agent after the first read the store.
- the title step (circle_context.gather) asks for the references its reading needs: those the store holds are not
  searched again, and what the subtitle step found comes along into the title prompt.

- the news crawler (scripts/crawl_news_events.py, daily) is the other half: its candidate graph is read here before
  anything is searched (graph_context), and the circle references the clips had to look up are offered to it as
  seeds (`seeds_path`), so what one clip needed is in the graph for the next (owner recognising
  references and the news crawler belong together).

Nothing here stops a step: a failed call or search is a status in the record.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, TypedDict

from . import channel_facts
from .lane_rules import rules
from .layout import layout
from .runner import Ctx
from .srt_text import SrtCue
from .state import utc_now
from .step_io import _local, _render, ask_json

if TYPE_CHECKING:
    from .circle_context import TermContext
    from .state import CpaEvent

STORE = "context/references.json"
SPOT_CALL = "reference-spot"
LOOKUP_CALL = "reference-context"
SUBTITLES, TITLE = "subtitles", "title"


class SubtitleReferences(TypedDict):
    leads: list[dict[str, Any]]      # what the subtitle agent is shown: the references that were found
    cpa: list[CpaEvent]              # the model calls this made (none when the store answered)
    line: str                        # PROCESS


def _read(ctx: Ctx) -> dict[str, Any]:
    path = ctx.clip_dir / STORE
    try:
        doc = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    except ValueError:
        doc = {}
    return doc if isinstance(doc, dict) else {}


def load(ctx: Ctx) -> dict[str, TermContext]:
    """The clip's references by their writing in the text ({} when none was looked up yet)."""
    return {str(row["term"]): row for row in _read(ctx).get("references", []) if isinstance(row, dict) and row.get("term")}


def remember(ctx: Ctx, terms: Sequence[TermContext], stage: str, **facts: Any) -> None:
    """Put `terms` into the store (a reference already there keeps the step that first asked for it)."""
    doc = _read(ctx)
    kept = {str(row["term"]): row for row in doc.get("references", []) if isinstance(row, dict) and row.get("term")}
    for term in terms:
        kept[term["term"]] = {**term, "stage": kept.get(term["term"], {}).get("stage") or stage}
    path = _local(ctx, STORE)
    path.write_text(json.dumps({**doc, **facts, "references": list(kept.values())}, ensure_ascii=False, indent=2), encoding="utf-8")
    _offer(ctx, terms)


def seeds_path() -> Path:
    """Where the lane leaves the references its clips looked up, for the news crawler (the lane's shared assets on the
    media host)."""
    return Path(layout().work_root) / "assets" / "reference_seeds.json"


def _offer(ctx: Ctx, terms: Sequence[TermContext]) -> None:
    """Leave the circle references among `terms` for the crawler: the kinds lane_rules names (a work or a saying is
    answered by the lookup itself; a person, an event, an unexplained name is the crawler's to follow; a reference of
    no clear kind only once the lookup found what it is: unfound, it is as likely a mishearing as a name).  The name is the
    writing the lookup settled on, else the reader's guess, else the term as written.  The newest `crawler_seeds_keep`
    stay.  Where the file cannot be written (a run that is not on the media host) nothing is offered."""
    from .circle_context import FOUND

    cfg = rules("references")
    kinds, when_found = set(cfg["crawler_kinds"]), set(cfg["crawler_kinds_when_found"])
    fresh = [{"name": (t.get("wording") or t.get("likely") or t["term"]).strip(), "term": t["term"], "kind": t.get("kind", ""),
              "status": t["status"], "cid": ctx.state.data["clip"]["cid"], "seen": utc_now()}
             for t in terms if t.get("kind", "") in kinds or (t.get("kind", "") in when_found and t["status"] == FOUND)]
    fresh = [row for row in fresh if sum(ch.isalnum() for ch in row["name"]) >= 2]      # one character is no name to search
    if not fresh:
        return
    path = seeds_path()
    try:
        kept = {r["name"]: r for r in json.loads(path.read_text(encoding="utf-8"))} if path.is_file() else {}
        kept.update({r["name"]: r for r in fresh})
        rows = sorted(kept.values(), key=lambda r: str(r.get("seen") or ""), reverse=True)[:int(cfg["crawler_seeds_keep"])]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
    except (OSError, ValueError, KeyError, TypeError):
        return


def _spot_problem(value: object) -> str:
    """Why the reading's answer is not usable, or "": an object whose `references` is a list of rows (an empty list is
    an answer: the draft refers to nothing; a bare empty list is not readable as an answer at all)."""
    rows = value.get("references") if isinstance(value, dict) else None
    if not isinstance(rows, list):
        return "not an object with a references list"
    for row in rows:
        if not (isinstance(row, dict) and isinstance(row.get("term"), str) and row["term"].strip()
                and isinstance(row.get("cues"), list) and all(type(n) is int for n in row["cues"])
                and all(isinstance(row.get(key, ""), str) for key in ("kind", "why", "likely"))):
            return "each item must be an object with term, cues (numbers), kind, why and likely"
    return ""


def _spot(ctx: Ctx, numbered: dict[int, str], cpa_calls: list[CpaEvent]) -> tuple[list[dict[str, Any]], str]:
    """(the references the draft makes, why there are none): one model call."""
    most = int(rules("references")["spot_max_terms"])
    prompt = _render("reference_spot.md", persona=channel_facts.spoken(), max_terms=str(most), lines="\n".join(numbered.values()))
    try:
        answer, reason = ask_json(ctx, SPOT_CALL, prompt, purpose=SPOT_CALL, shape="REFERENCE_SPOT_SHAPE", cpa_calls=cpa_calls,
                                  problem=_spot_problem)
    except Exception as exc:  # noqa: BLE001 - a lead for the agent, never a reason to stop the step
        return [], f"{type(exc).__name__}: {str(exc)[:160]}"
    if reason:
        return [], reason
    seen: dict[str, dict[str, Any]] = {}
    for row in answer["references"]:
        cues = [n for n in row["cues"] if n in numbered]
        if cues and row["term"].strip() not in seen:
            seen[row["term"].strip()] = {**row, "term": row["term"].strip(), "cues": cues}
    return list(seen.values())[:most], ""


def _leads(found: Sequence[TermContext]) -> list[dict[str, Any]]:
    from .circle_context import FOUND, _domain

    return [{"heard": t["term"], "cues": t["cues"], "is": t["note"], "wording": t.get("wording", ""),
             "sources": list(dict.fromkeys(_domain(u) for u in t["sources"]))} for t in found if t["status"] == FOUND]


def _graph_lead(graph: Any, row: dict[str, Any], name: str) -> dict[str, Any]:
    """What the knowledge graph says of a reference the draft makes, as a lead of the same shape as a looked-up one."""
    nodes = [n for n in graph.graph.find(name) if n.status != "retired"]
    ids = {n.id for n in nodes}
    about = [line.text for line in graph.lines if ids & set(line.ids)]
    return {"heard": row["term"], "cues": row["cues"], "is": "；".join(about)[:300],
            "wording": nodes[0].names["display"] if nodes else name, "sources": ["知识图"]}


def for_subtitles(ctx: Ctx, draft: Sequence[SrtCue]) -> SubtitleReferences:
    """The references of the recognisers' draft, for the subtitle agent.  A store made from this very draft is read back
    (no call); otherwise the draft is read, the graph and the web are asked, and the store is written."""
    from . import circle_context, graph_context

    numbered = {int(c["id"]): f"{c['id']} {' '.join(str(c['text']).split())}" for c in draft}
    sha = hashlib.sha256("\n".join(numbered.values()).encode()).hexdigest()
    doc = _read(ctx)
    if doc.get("draft_sha256") == sha:
        found = [t for t in load(ctx).values() if t.get("stage") == SUBTITLES]
        return {"leads": [*doc.get("graph_leads", []), *_leads(found)], "cpa": [], "line": doc.get("line", "")}
    calls: list[CpaEvent] = []
    spotted, why_none = _spot(ctx, numbered, calls)
    # the graph (repository + the news crawler's candidates) is asked under the draft's writing and the reader's guess
    graph = graph_context.read(ctx, [*numbered.values(), *(row.get("likely", "") for row in spotted)])
    known: dict[str, str] = {}                     # the draft's writing -> the name the graph knows it under
    for row in spotted:
        for name in (row["term"], row.get("likely", "").strip()):
            if name and graph.covers(name):
                known.setdefault(row["term"], name)
    asked = [circle_context.TermContext(term=row["term"], cues=row["cues"], why=row.get("why", ""), queries=[], status="",
                                        note="", sources=[], reason="", likely=row.get("likely", "").strip(),
                                        kind=row.get("kind", "")) for row in spotted if row["term"] not in known]
    circle_context.look_up(ctx, asked, numbered, calls, LOOKUP_CALL)
    from_graph = [_graph_lead(graph, row, known[row["term"]]) for row in spotted if row["term"] in known]
    line = ("外部指称：" + ("、".join(f"{t['term']}（{circle_context._outcome(t)}）" for t in asked) or "底稿里没有需要另查的指称")
            + (f"；知识图已有：{'、'.join(known)}" if known else "") + (f"；未能读底稿（{why_none}）" if why_none else ""))
    remember(ctx, asked, SUBTITLES, draft_sha256=sha, line=line, graph=graph.record(list(known)), graph_leads=from_graph)
    return {"leads": [*from_graph, *_leads(asked)], "cpa": calls, "line": line}
