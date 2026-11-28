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
from typing import TYPE_CHECKING, Any, NotRequired, TypedDict

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
SCHEMA_VERSION = "references.v3"
SPOT_CALL = "reference-spot"
LOOKUP_CALL = "reference-context"
SUBTITLES, TITLE = "subtitles", "title"


class SubtitleReferences(TypedDict):
    leads: list[dict[str, Any]]      # what the subtitle agent is shown: verified references
    clues: NotRequired[list[dict[str, Any]]]  # unverified candidate leads (never canonical wording)
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
    offer_seeds(fresh)


def offer_seeds(fresh: Sequence[dict[str, Any]]) -> None:
    """Add rows ({name, term, kind, status, cid, seen}) to the crawler's seeds; the newest `crawler_seeds_keep` stay.
    Also used by knowledge_observe for the names a sealed clip showed."""
    cfg = rules("references")
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
                                  goes_on=True,
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


def _graph_clue(graph: Any, row: dict[str, Any], name: str) -> dict[str, Any]:
    """What candidate knowledge-graph material says about a reference, never reading accepted nodes or canonical wording."""
    nodes = [n for n in graph.graph.find(name) if n.status == "candidate"]
    ids = {n.id for n in nodes}
    about = [line.text for line in graph.lines if not line.verified and (ids & set(line.ids))]
    other = bool(nodes) and name not in nodes[0].names.values()
    alias_note = f"{name}可能是{nodes[0].names['display']}的另一个叫法（未核实）" if other else ""
    return {"heard": row["term"], "cues": row["cues"],
            "is": "；".join([*([alias_note] if alias_note else []), *about])[:300],
            "wording": "", "verified": False, "status": "candidate", "sources": ["知识图（未核实候选）"]}


def _graph_lead(graph: Any, row: dict[str, Any], name: str) -> dict[str, Any]:
    """What the knowledge graph says of a reference the draft makes, keeping verified and candidate material apart."""
    accepted = [n for n in graph.graph.find(name) if n.status == "accepted"]
    if not accepted:
        return _graph_clue(graph, row, name)
    ids = {n.id for n in accepted}
    about = [line.text for line in graph.lines if line.verified and (ids & set(line.ids))]
    other = name not in accepted[0].names.values()      # a nickname stays as it was said: the name is in `is`
    alias_note = f"{name}是{accepted[0].names['display']}的另一个叫法" if other else ""
    wording = name if other else accepted[0].names["display"]
    return {"heard": row["term"], "cues": row["cues"],
            "is": "；".join([*([alias_note] if alias_note else []), *about])[:300],
            "wording": wording, "verified": True, "status": "accepted", "sources": ["知识图（已认可）"]}


def for_subtitles(ctx: Ctx, draft: Sequence[SrtCue]) -> SubtitleReferences:
    """The references of the recognisers' draft, for the subtitle agent.  A cache entry under `subtitles` hits only for
    the exact `draft_sha256` at `SCHEMA_VERSION` (returning the exact leads and clues computed for that draft); in every
    other case everything is recomputed and looked up from scratch without cross-draft reuse."""
    from . import circle_context, graph_context

    numbered = {int(c["id"]): f"{c['id']} {' '.join(str(c['text']).split())}" for c in draft}
    sha = hashlib.sha256("\n".join(numbered.values()).encode()).hexdigest()
    doc = _read(ctx)
    cached = doc.get("subtitles")
    if (isinstance(cached, dict)
            and cached.get("schema_version") == SCHEMA_VERSION
            and cached.get("draft_sha256") == sha
            and isinstance(cached.get("leads"), list)
            and isinstance(cached.get("clues"), list)):
        return {"leads": list(cached["leads"]), "clues": list(cached["clues"]),
                "cpa": [], "line": str(cached.get("line", ""))}
    calls: list[CpaEvent] = []
    spotted, why_none = _spot(ctx, numbered, calls)
    # the graph (repository + the news crawler's candidates) is asked under the draft's writing and the reader's guess;
    # only accepted entries (`graph.covers`) suppress web lookup, while candidate entries pass along as unverified clues
    graph = graph_context.read(ctx, [*numbered.values(), *(row.get("likely", "") for row in spotted)])
    known: dict[str, str] = {}                     # the draft's writing -> the accepted name the graph knows it under
    cand_known: dict[str, str] = {}                # the draft's writing -> candidate name (clue only, still searched)
    for row in spotted:
        for name in (row["term"], row.get("likely", "").strip()):
            if name and graph.covers(name):
                known.setdefault(row["term"], name)
        if row["term"] not in known and graph.graph is not None:
            for name in (row["term"], row.get("likely", "").strip()):
                if name and any(n.status == "candidate" for n in graph.graph.find(name)):
                    cand_known.setdefault(row["term"], name)
                    break
    asked = [circle_context.TermContext(term=row["term"], cues=row["cues"], why=row.get("why", ""), queries=[], status="",
                                        note="", sources=[], reason="", likely=row.get("likely", "").strip(),
                                        kind=row.get("kind", ""), same_initials=graph.same_initials(row["term"]))
             for row in spotted if row["term"] not in known]
    circle_context.look_up(ctx, asked, numbered, calls, LOOKUP_CALL)
    from_graph = [_graph_lead(graph, row, known[row["term"]]) for row in spotted if row["term"] in known]
    from_clues = [clue for row in spotted if row["term"] in cand_known
                  if (clue := _graph_clue(graph, row, cand_known[row["term"]]))["is"]]
    clue_terms = [str(clue["heard"]) for clue in from_clues]
    leads = [*from_graph, *_leads(asked)]
    line = ("外部指称：" + ("、".join(f"{t['term']}（{circle_context._outcome(t)}）" for t in asked) or "底稿里没有需要另查的指称")
            + (f"；知识图已有：{'、'.join(known)}" if known else "")
            + (f"；候选线索：{'、'.join(clue_terms)}" if clue_terms else "")
            + (f"；未能读底稿（{why_none}）" if why_none else ""))
    remember(ctx, asked, SUBTITLES, schema_version=SCHEMA_VERSION,
             graph=graph.record(list(known)),
             subtitles={"schema_version": SCHEMA_VERSION, "draft_sha256": sha,
                        "leads": leads, "clues": from_clues, "line": line})
    return {"leads": leads, "clues": from_clues, "cpa": calls, "line": line}
