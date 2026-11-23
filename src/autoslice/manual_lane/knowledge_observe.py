"""After a clip is sealed: the names it uses that the channel knowledge graph does not have yet.

Owner ("这种切片过程中补知识图的过程也很重要，李李一听就知道是李豆沙的昵称"): a nickname heard in a clip,
a person or a work it names, is knowledge the next clip needs.  The accepted graph (assets/<id>/knowledge/) changes
only by a commit; what the clips show in between is kept here, with the lane's task, as persona_observe keeps traits:

- `after_run` (every run of a clip ends in cli._finish_run): once the review is done, one model call reads the final
  subtitles, the clip's chat and super chats (danmaku/lines.json) and the graph's spellings and names at most `observe_max` items: another name of an entity the graph has
  (`alias`, with the node it belongs to) or an entity it does not have (`entity`).  An item is kept only when the name
  is no spelling of the graph yet, the node exists (for an alias), and its quotes stand, letter for letter, in the
  final subtitles and contain the name; an entity whose every quote thanks a viewer for a gift is that viewer, not
  knowledge (`viewer_thanks`).  An item already observed gets this clip as another witness.
- what is kept goes three ways: the run's output line (the reviewer commits what holds into the graph, with the
  clip as its source); the news crawler's seeds (references.offer_seeds), so a new person or event is looked up; and,
  once witnessed in `observed_min_sessions` live sessions, the subtitle agent's channel facts as an unreviewed lead.

Nothing here stops or changes a clip: a failed call is written down and the next clip tries again.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from src.autoslice.channel_profile import load_channel_profile
from src.autoslice.knowledge_graph import NODE_KINDS, Graph, load_graph

from . import persona_observe, text_overlap
from .lane_rules import rules
from .layout import REPO_ROOT
from .runner import Ctx
from .srt_text import _json_from_text
from .state import utc_now
from .step_io import _ask_cpa, _cpa_ok, _render

FILE = "knowledge/observations.json"    # under the lane's task directory: every clip of the task adds to it
MARK = "knowledge/observed.json"        # under the clip: this clip was read
CALL = "knowledge-observe"
ALIAS, ENTITY = "alias", "entity"


def graph() -> Graph:
    return load_graph(load_channel_profile(REPO_ROOT).asset_root)


def _known(g: Graph) -> str:
    """The graph's entities with their spellings, one line each, for the prompt."""
    return "\n".join(f"{n.id}：{'、'.join(dict.fromkeys(n.surfaces()))}"
                     for n in sorted(g.nodes.values(), key=lambda n: n.id) if n.status != "retired")


def load(task: Path) -> list[dict[str, Any]]:
    try:
        rows = json.loads((task / FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return rows if isinstance(rows, list) else []


def _sessions(row: dict[str, Any]) -> int:
    return len({w.get("recording_date") for w in row.get("witnesses", [])})


def describe(row: dict[str, Any], g: Graph | None = None) -> str:
    if row["type"] == ALIAS:
        owner = g.name(row["of"], "display") if g is not None and row["of"] in g.nodes else row["of"].split(":", 1)[-1]
        return f"「{row['name']}」是「{owner}」的另一个叫法"
    return f"「{row['name']}」（{row.get('kind') or '名字'}）" + (f"：{row['why']}" if row.get("why") else "")


def lines(task: Path) -> list[str]:
    """The observed names several sessions bear out, one line each, for a prompt."""
    least = int(rules("knowledge_observe")["observed_min_sessions"])
    return [f"- {describe(row)}（{_sessions(row)} 场直播的切片里出现过，尚未审定）"
            for row in load(task) if _sessions(row) >= least]


def chat(ctx: Ctx) -> list[str]:
    """The clip's chat and super chats as step 3 wrote them (danmaku/lines.json), one text a line.  A name the viewers
    use often stands only here: the subtitles carry what ASR heard (11-14: 「李李」 in the chat, "lily" in the subtitles)."""
    try:
        rows = json.loads((ctx.clip_dir / "danmaku/lines.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [" ".join(r["text"].split()) for r in rows if isinstance(r, dict) and isinstance(r.get("text"), str)
            and r["text"].strip() and r.get("type") in (None, "sc")]


def _kept(answer: object, texts: tuple[str, ...], g: Graph) -> list[dict[str, Any]]:
    """The answer's items that pass: a name the graph does not spell yet, quotes that contain it and stand letter for
    letter in the subtitles or in one chat line, and for an alias a node of the graph."""
    rows, seen = [], set()
    for item in (answer.get("items") if isinstance(answer, dict) else None) or []:
        if not (isinstance(item, dict) and isinstance(item.get("name"), str) and isinstance(item.get("quotes"), list)):
            continue
        name, kind = item["name"].strip(), item.get("type")
        if kind not in (ALIAS, ENTITY) or len(text_overlap.letters(name)) < 2 or g.find(name) or name in seen:
            continue
        if kind == ALIAS and item.get("of") not in g.nodes:
            continue
        quotes = [str(q).strip() for q in item["quotes"]
                  if text_overlap.letters(name) in text_overlap.letters(q)
                  and any(text_overlap.letters(q) in t for t in texts)]
        if not quotes or kind == ENTITY and all(_thanks_viewer(q) for q in quotes):
            continue
        seen.add(name)
        row = {"type": kind, "name": name, "why": str(item.get("why") or "").strip(), "quotes": quotes}
        if kind == ALIAS:
            row["of"] = item["of"]
        else:
            row["kind"] = item.get("kind") if item.get("kind") in NODE_KINDS else ""
        rows.append(row)
    return rows[:int(rules("knowledge_observe")["observe_max"])]


def _thanks_viewer(quote: str) -> bool:
    """The line thanks a viewer for a gift (lane_rules knowledge_observe.viewer_thanks): the name in it is a viewer's."""
    cfg = rules("knowledge_observe")["viewer_thanks"]
    return any(w in quote for w in cfg["thanks"]) and any(w.lower() in quote.lower() for w in cfg["gifts"])


def _same(a: dict[str, Any], b: dict[str, Any]) -> bool:
    return a["type"] == b["type"] and a["name"] == b["name"] and a.get("of") == b.get("of")


def after_run(ctx: Ctx) -> str:
    """Read the sealed clip once.  Returns a line for the run's output ("" when there was nothing to do)."""
    from .references import offer_seeds

    mark = ctx.clip_dir / MARK
    if ctx.state.step("review").get("status") != "DONE" or mark.is_file():
        return ""
    try:
        g = graph()
        subtitles, _own = persona_observe.host_text(ctx)
        said = chat(ctx)
        texts = (text_overlap.letters(subtitles), *(text_overlap.letters(line) for line in said))
        record = _ask_cpa(ctx, CALL, _render("knowledge_observe.md", known=_known(g), lines=subtitles,
                                             chat="\n".join(said) or "（这条片没有弹幕记录）",
                                             most=str(rules("knowledge_observe")["observe_max"])), purpose=CALL)
        found = _kept(_json_from_text(record["response_text"]), texts, g) if _cpa_ok(record) else None
    except Exception as exc:  # noqa: BLE001 - an observation is never a reason to fail a clip that is done
        return f"知识图观察：未完成（{type(exc).__name__}: {str(exc)[:120]}），下次运行再读"
    if found is None:
        return "知识图观察：模型没有回答，下次运行再读"
    task = persona_observe.task_of(ctx)
    rows = load(task)
    cid = ctx.state.data["clip"]["cid"]
    witness = {"cid": cid, "recording_date": ctx.session.get("recording_date")}
    for item in found:
        row = next((r for r in rows if _same(r, item)), None)
        if row is None:
            row = {k: v for k, v in item.items() if k != "quotes"} | {"witnesses": []}
            rows.append(row)
        if all(w.get("cid") != cid for w in row["witnesses"]):
            row["witnesses"].append({**witness, "quotes": item["quotes"]})
    for path, doc in ((task / FILE, rows), (mark, {"found": found, "model": record.get("model"), "effort": record.get("effort")})):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    offer_seeds([{"name": item["name"], "term": item["name"], "kind": f"knowledge_{item['type']}", "status": "observed",
                  "cid": cid, "seen": utc_now()} for item in found])
    return "知识图观察：" + ("；".join(describe(item, g) for item in found) if found else "这条片没有知识图里还没有的名字")
