"""Who the host is in a call with, on a day whose session names no guest (11-23: step 4 heard 安晚awa and 礼墨Sumi by
voiceprint, the session named only her, and every title came out as 「游戏队友…」: the skill names the people of a
collab, 60-title 点名).

The ordinary path for a named collab is the session's participants (a participants.json by hand, until now the only
source).  This fills it from the evidence the lane already holds: the whole session's chat (danmaku/session.json,
step 3) is searched for the names of the knowledge graph's people; those the chat names often enough
go to one CPA call that tells a person in the call from one only talked about, quoting chat lines; a name is kept only
when each quote stands letter for letter in the chat lines that name it.  The answer is kept per broadcast day in the
task directory, so every clip of the day names the same guests, and is written into the clip's session block, where
title, cover, tags and review read the participants."""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from . import text_overlap
from .speaker_v8 import GUEST_LABEL, HOST

if TYPE_CHECKING:
    from .runner import Ctx

MIN_LINES = 5        # chat lines naming a streamer over the whole session before she is asked about
SHOWN = 20           # chat lines per candidate shown to CPA
CACHE = "knowledge/collab-guests-{date}.json"


def streamers(ctx: Ctx) -> dict[str, list[str]]:
    """The knowledge graph's people (graph_context: the channel's graph, the shared layer and the crawler's runtime
    candidates), the host left out: formal name -> spellings.  Her formal name is the reviewed streamer registry's when
    one of her spellings is a registry name (the title prefix needs that), else the node's display name; nodes with the
    same formal name are one person (person:安晚 and person:安晚awa).  Every spelling of a node that shares one with the
    host's names is the host's (李豆沙: 豆沙, lds, 小李 ...), and a node with one of those is left out."""
    from src.autoslice.channel_envelope import HOST_NAMES, load_related_streamers

    from . import graph_context

    graph = graph_context.load(ctx).graph
    if graph is None:
        return {}
    try:
        registry = set(load_related_streamers())
    except Exception:  # noqa: BLE001 - an unreadable registry only loses the formal spelling
        registry = set()
    # every person, detection_name=false ones too: that flag keeps a short name out of the subtitles' name detection,
    # and the chat writing 安晚 or 礼墨 is exactly the evidence wanted here
    nodes = [n for n in graph.nodes.values() if n.kind == "person" and n.status != "retired"]
    host = {*HOST_NAMES, HOST}
    host |= {s for n in nodes if set(n.surfaces()) & host for s in n.surfaces()}
    out: dict[str, set[str]] = {}
    for node in nodes:
        surfaces = set(node.surfaces())
        if surfaces & host:
            continue
        formal = next((s for s in sorted(surfaces) if s in registry),
                      node.names.get("official") or node.names.get("display") or sorted(surfaces)[0])
        out.setdefault(formal, set()).update(surfaces)
    folded = {name: {x.casefold() for x in spellings} for name, spellings in out.items()}
    # a node whose spellings another person has all of is that person (person:awa beside 安晚awa)
    within = {a for a in folded for b in folded if a != b and folded[a] < folded[b]}
    return {name: sorted(spellings) for name, spellings in out.items() if name not in within}


def session_chat(clip_dir: Path) -> list[str]:
    """The session's chat texts (step 3's whole-session cache; the clip's own lines when there is none)."""
    for name in ("danmaku/session.json", "danmaku/lines.json"):
        path = clip_dir / name
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            rows = data["rows"] if isinstance(data, dict) else data
            return [str(r.get("text") or "") for r in rows if isinstance(r, dict) and "type" not in r and r.get("text")]
    return []


def candidates(chat: Sequence[str], known: dict[str, list[str]]) -> dict[str, list[str]]:
    """People the chat names in at least MIN_LINES lines, with those lines (a spelling counts as detection counts it:
    graph_context._spelled)."""
    from .graph_context import _spelled
    from .lane_rules import rules

    cfg = rules("detection")
    least = int(cfg.get("latin_min_letters") or 1)
    common = {w.lower() for w in cfg.get("latin_common_words") or []}
    found = {name: [t for t in chat if any(_spelled(s, t, least, common) for s in spellings)]
             for name, spellings in known.items()}
    return {name: lines for name, lines in found.items() if len(lines) >= MIN_LINES}


def kept(answer: Any, named: dict[str, list[str]]) -> list[str]:
    """The guests of the answer: a candidate, each of whose quotes is one of the chat lines that name her."""
    out = []
    for item in (answer.get("in_call") or []) if isinstance(answer, dict) else []:
        name = str(item.get("name") or "").strip() if isinstance(item, dict) else ""
        quotes = [str(q).strip() for q in item.get("quotes") or []] if name in named else []
        lines = {text_overlap.key(t) for t in named.get(name, [])}
        if quotes and all(text_overlap.key(q) in lines for q in quotes) and name not in out:
            out.append(name)
    return out


def needed(ctx: Ctx) -> bool:
    """The session names nobody but the host, and step 4 heard someone else in this clip."""
    from .config import participant_names
    from .subtitle_track import read_track

    if [n for n in participant_names(ctx.session) if n != HOST] or ctx.session.get("guest_evidence"):
        return False
    return any(c.get("speaker") == GUEST_LABEL for c in read_track(ctx.clip_dir))


class GuestsUnknown(RuntimeError):
    """The CPA call that tells who is in the call failed: the step fails and is run again, rather than titling a
    collab with its guests unnamed (11-23: nine of ten clips asked at once, the gateway refused the copies of the
    one prompt in flight, and each went on unnamed)."""


def _day_record(ctx: Ctx, cache: Path, date: str, ask: Callable[[str, str], dict[str, Any] | None]) -> dict[str, Any]:
    """The day's answer: read from the task directory, or asked once while holding the day's lock, so the clips of
    one day that reach step 6 together wait for the first instead of sending the same prompt side by side."""
    import fcntl

    cache.parent.mkdir(parents=True, exist_ok=True)
    with open(cache.with_suffix(".lock"), "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if cache.exists():
            return cast(dict[str, Any], json.loads(cache.read_text(encoding="utf-8")))
        named = candidates(session_chat(ctx.clip_dir), streamers(ctx))
        record: dict[str, Any] = {"date": date, "candidates": {}, "guests": []}
        if named:
            from .step_io import _render

            prompt = _render("collab_guests.md", candidates="\n\n".join(
                f"### {name}（弹幕里 {len(lines)} 条提到）\n" + "\n".join(f"- {t}" for t in lines[:SHOWN])
                for name, lines in named.items()))
            answer = ask("collab-guests", prompt)
            if answer is None:
                raise GuestsUnknown("CPA did not answer who is in the call (collab-guests)")
            record = {"date": date, "candidates": {n: len(v) for n, v in named.items()}, "answer": answer,
                      "guests": kept(answer, named)}
        cache.write_text(json.dumps(record, ensure_ascii=False, indent=1), encoding="utf-8")
        return record


def name_guests(ctx: Ctx, ask: Callable[[str, str], dict[str, Any] | None]) -> list[str]:
    """Fill the clip's session participants with the day's guests when `needed`; returns the names added.  `ask`
    sends the prompt and returns the parsed answer, None when the call failed (GuestsUnknown).  The day's answer is
    asked once and kept in the task directory (_day_record)."""
    if not needed(ctx):
        return []
    date = str(ctx.session.get("recording_date") or "")
    cache = ctx.clip_dir.parent.parent / CACHE.format(date=date)
    record = _day_record(ctx, cache, date, ask)
    guests = [str(g) for g in record.get("guests") or []]
    if guests:
        session = cast(dict[str, Any], ctx.session)
        session["participants"] = [*(session.get("participants") or []), *([g] for g in guests)]
        session["guest_evidence"] = {"source": "session_chat", "record": str(cache.name), "guests": guests}
        ctx.state.save()
    return guests
