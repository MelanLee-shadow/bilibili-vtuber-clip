"""Who the host is in a call with, on a day whose session names no guest (11-23: step 4 heard 安晚awa and 礼墨Sumi by
voiceprint, the session named only her, and every title came out as 「游戏队友…」: the skill names the people of a
collab, 60-title 点名).

The ordinary path for a named collab is the session's participants (a participants.json by hand, until now the only
source).  This fills it from the evidence the lane already holds: the whole session's chat (danmaku/session.json,
step 3) is searched for the names of the knowledge graph's people; those the chat names often enough
go to one CPA call that tells a person in the call from one only talked about, quoting chat lines; a name is kept only
when each quote stands letter for letter in the chat lines that name it.  The answer is kept per broadcast day in the
task directory, so every clip of the day names the same guests, and is written into the clip's session block, where
title, cover, tags and review read the participants.

The chat is not the only witness (owner, U468: 「这个联动完全可以直接从新闻Crawler把这个动态一抓就知道了」): the
host announces a collab in her dynamics (11-22 「明天晚上八点的女大矿工见」) and the guests post about it (安晚awa 11-23
「矿工是对的但我要晕倒了」).  So the same call also reads the Bilibili dynamics of the host and of the reviewed streamer
registry's accounts (`read_dynamics`, at the time step 6 asks, so posts after the stream count) around the broadcast day.
Each account that posted then is a candidate even when the chat hardly names her, her posts go to CPA beside her chat
lines, with what ties them to the host's (`linked`: a word both wrote then and neither before, 矿工 not 晚安, or the
host's name); a quote may be one of her chat lines or stand in one of her own posts there, and one there only for her
posts is kept on a quote from a post that holds such a tie."""

from __future__ import annotations

import datetime as dt
import json
import re
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from . import text_overlap
from .speaker_v8 import GUEST_LABEL, HOST

if TYPE_CHECKING:
    from .runner import Ctx

MIN_LINES = 5        # chat lines naming a streamer over the whole session before she is asked about
SHOWN = 20           # chat lines per candidate shown to CPA
CACHE = "knowledge/collab-guests-{date}.json"
PEERS_READ = 8       # registry accounts whose dynamics are read for one day (most named in the chat first)
AROUND_DAYS = 1      # a dynamic is about the broadcast day D when posted from D-1 to D+1, Beijing time
MIN_QUOTE = 4        # letters a quote from a dynamic must have (a dynamic is long; a quote is a part of it)
_BEIJING = dt.timezone(dt.timedelta(hours=8))
_HAN_PAIR = re.compile(r"(?=([一-鿿]{2}))")
_UID = re.compile(r"space\.bilibili\.com/(\d+)")

Dynamics = dict[str, list[dict[str, str]]]          # account (HOST for the host) -> [{"published_at", "text"}]


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
    return {name: lines for name, lines in mentions(chat, known).items() if len(lines) >= MIN_LINES}


def mentions(chat: Sequence[str], known: dict[str, list[str]]) -> dict[str, list[str]]:
    """Each person -> the chat lines that name her (any number)."""
    from .graph_context import _spelled
    from .lane_rules import rules

    cfg = rules("detection")
    least = int(cfg.get("latin_min_letters") or 1)
    common = {w.lower() for w in cfg.get("latin_common_words") or []}
    return {name: [t for t in chat if any(_spelled(s, t, least, common) for s in spellings)]
            for name, spellings in known.items()}


def accounts() -> dict[str, int]:
    """The reviewed streamer registry's people (the names a title prefix may use) -> the uid of their homepage."""
    from src.autoslice.channel_envelope import load_related_streamers

    out = {}
    for name, row in load_related_streamers().items():
        m = _UID.search(row.homepage)
        if m and name != HOST:
            out[name] = int(m.group(1))
    return out


def read_dynamics(uids: Mapping[str, int]) -> tuple[Dynamics, list[str]]:
    """The recent dynamics (the first page of each account's opus feed, as the dynamics crawler reads it) of the host,
    whose uid comes from the channel's live room, and of each account; (dynamics, what could not be read).  Read once per
    broadcast day (the day's record keeps the answer), from the media host."""
    from . import dynamics_feed
    from .layout import layout

    out: Dynamics = {}
    errors = []
    targets: list[tuple[str, Callable[[], int]]] = [(HOST, lambda: dynamics_feed.room_uid(layout().room_id))]
    targets += [(name, lambda uid=uid: uid) for name, uid in uids.items()]
    for name, uid_of in targets:
        try:
            out[name] = dynamics_feed.recent(uid_of())
        except (dynamics_feed.FeedError, OSError, ValueError) as exc:
            errors.append(f"{name}: {type(exc).__name__}: {exc!s:.200}")
    return out, errors


def _around(items: Sequence[Mapping[str, str]], date: str) -> tuple[list[str], list[str]]:
    """(texts posted from D-1 to D+1 Beijing time, texts posted before that)."""
    day = dt.date.fromisoformat(date[:10])
    first, last = day - dt.timedelta(days=AROUND_DAYS), day + dt.timedelta(days=AROUND_DAYS)
    near, before = [], []
    for item in items:
        posted = dt.datetime.fromisoformat(str(item["published_at"]).replace("Z", "+00:00")).astimezone(_BEIJING).date()
        if first <= posted <= last:
            near.append(str(item["text"]))
        elif posted < first:
            before.append(str(item["text"]))
    return near, before


def _pairs(texts: Sequence[str]) -> set[str]:
    return {m.group(1) for t in texts for m in _HAN_PAIR.finditer(t)}


def _host_spellings() -> set[str]:
    from src.autoslice.channel_envelope import HOST_NAMES

    return {HOST, *HOST_NAMES}


def linked(dynamics: Dynamics, date: str) -> dict[str, list[str]]:
    """Accounts whose dynamics around the broadcast day tie them to the host -> what ties them: a word (two Chinese
    characters) the host's dynamics around the day hold and that neither wrote before (女大矿工 / 矿工, not 晚安), or one
    of the host's names.  A shared new word can be chance (11-18: 电脑, 小播 in the dynamics of four accounts), so this is
    shown to CPA as a lead and asked of a quote (`kept`), not taken as the answer."""
    host_near, host_before = _around(dynamics.get(HOST, []), date)
    fresh = _pairs(host_near) - _pairs(host_before)
    host = _host_spellings()
    out = {}
    for name, items in dynamics.items():
        if name == HOST:
            continue
        near, before = _around(items, date)
        words = sorted((_pairs(near) & fresh) - _pairs(before))
        words += sorted(s for s in host if any(s in t for t in near))
        if words:
            out[name] = words
    return out


def kept(answer: Any, named: dict[str, list[str]], posted: Mapping[str, Sequence[str]] | None = None,
         links: Mapping[str, Sequence[str]] | None = None) -> list[str]:
    """The guests of the answer: a candidate, each of whose quotes is one of the chat lines that name her or stands in
    one of her own dynamics around the day (`posted`, at least MIN_QUOTE letters).  One the chat names in fewer than
    MIN_LINES lines is there for her dynamics, and is kept only when one of her quotes stands in a dynamic of hers that
    holds what ties her to the host (`links`)."""
    posted, links = posted or {}, links or {}
    out = []
    for item in (answer.get("in_call") or []) if isinstance(answer, dict) else []:
        name = str(item.get("name") or "").strip() if isinstance(item, dict) else ""
        quotes = [str(q).strip() for q in item.get("quotes") or []] if name in named else []
        lines = {text_overlap.key(t) for t in named.get(name, [])}
        own = [text_overlap.key(t) for t in posted.get(name, [])]
        tied = [text_overlap.key(t) for t in posted.get(name, []) if any(w in t for w in links.get(name, []))]

        def stands(q: str, texts: Sequence[str]) -> bool:
            k = text_overlap.key(q)
            return len(k) >= MIN_QUOTE and any(k in t for t in texts)
        if not quotes or not all(text_overlap.key(q) in lines or stands(q, own) for q in quotes) or name in out:
            continue
        if len(named.get(name, [])) >= MIN_LINES or any(stands(q, tied) for q in quotes):
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
        chat, known = session_chat(ctx.clip_dir), streamers(ctx)
        said = mentions(chat, known)
        named = {name: lines for name, lines in said.items() if len(lines) >= MIN_LINES}
        uids = accounts()
        uids = dict(sorted(uids.items(), key=lambda kv: -len(said.get(kv[0], [])))[:PEERS_READ])
        dynamics, unread = read_dynamics(uids)
        links = linked(dynamics, date)
        for name, items in dynamics.items():
            if name != HOST and _around(items, date)[0]:
                named.setdefault(name, said.get(name, []))
        posted = {name: _around(dynamics.get(name, []), date)[0] for name in named}
        host_posted = _around(dynamics.get(HOST, []), date)[0]
        seen = {"host": host_posted, "accounts": {n: v for n, v in posted.items() if v}, "linked": links, "unread": unread}
        record: dict[str, Any] = {"date": date, "candidates": {}, "dynamics": seen, "guests": []}
        if named:
            from .step_io import _render

            prompt = _render("collab_guests.md", host_dynamics="\n".join(f"- {t}" for t in host_posted) or "（没有）",
                             candidates="\n\n".join(_candidate_block(name, lines, posted[name], links.get(name))
                                                    for name, lines in named.items()))
            answer = ask("collab-guests", prompt)
            if answer is None:
                raise GuestsUnknown("CPA did not answer who is in the call (collab-guests)")
            record = {"date": date, "candidates": {n: len(v) for n, v in named.items()}, "dynamics": seen,
                      "answer": answer, "guests": kept(answer, named, posted, links)}
        cache.write_text(json.dumps(record, ensure_ascii=False, indent=1), encoding="utf-8")
        return record


def _candidate_block(name: str, lines: Sequence[str], posted: Sequence[str], words: Sequence[str] | None) -> str:
    head = f"### {name}（弹幕里 {len(lines)} 条提到）"
    chat = "\n".join(f"- {t}" for t in lines[:SHOWN]) or "- （弹幕里没有提到她）"
    own = ("\n她这几天自己发的动态：\n" + "\n".join(f"- {t}" for t in posted)) if posted else ""
    shared = f"\n她的动态和主播这几天的动态都写到、两人以前都没写过的词：{'、'.join(words)}" if words else ""
    return f"{head}\n{chat}{own}{shared}"


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
        session["guest_evidence"] = {"source": "session_chat+dynamics", "record": str(cache.name), "guests": guests}
        ctx.state.save()
    return guests
