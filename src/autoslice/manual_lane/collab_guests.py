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

The chat is not the only witness (the channel's request): the
host announces a collab in her dynamics (11-22 「明天晚上八点的女大矿工见」) and the guests post about it (安晚awa 11-23
「矿工是对的但我要晕倒了」).  So the same call also reads the Bilibili dynamics of the host and of the reviewed streamer
registry's accounts (`read_dynamics`, at the time step 6 asks, so posts after the stream count) around the broadcast day.
Each account that posted then is a candidate even when the chat hardly names her, her posts go to CPA beside her chat
lines, with what ties them to the host's (`linked`: a word both wrote then and neither before, 矿工 not 晚安, or the
host's name); a quote may be one of her chat lines or stand in one of her own posts there, and one there only for her
posts is kept on a quote from a post that holds such a tie.  The live room's title is the host's own words about the
day too (11-23 「？！女大矿工？！」, in the recorder's header of the clip's chat file, `room_title`): it is shown beside
her dynamics and its words count as hers around the day.

That one judgement is not the only way onto the day's list (安晚awa sat in 礼墨Sumi's room and spoke through
her stream; the room title, the dynamics and the chat lines by name did not say so, the judge answered 礼墨Sumi alone, the
day's file was kept as asked, and the eight clips with guests were titled 【李豆沙×礼墨Sumi】 with her lines nobody's).  The lane had
measured her presence three other ways by then, and each clip in which someone else is heard now adds what it holds of
them to the day's record (`clip_evidence`, no model):

- voice: the frozen classifier's own guest classes on the clip's ~1.2 s chunks (speaker/chunks-pred.json);
- subtitles: lines of the host that name a person and are followed by a guest's line;
- peers: the titles other uploaders gave their clips of the same session (step 0's dedup/result.json), where a 【】
  that names the host names her too.

A person these name by `routes_needed` routes (`decide`; the numbers and what they were measured on are lane_rules
collab_guests) is confirmed on the day's list.  The chat judge's answer (`chat_guests`) is a hypothesis, not proof
from chat talk alone: a person it names is confirmed only when something other than chat talk backs her — a tie in the
dynamics or live room title (`linked`), or at least one of the three measured routes (counting the clip being processed
for voice, so the first clip of a real collab day is not run as solo).  Once confirmed, a guest is recorded in
`confirmed` and stays permanently on the day's list (`confirmed` guests only grow, whatever later recomputation or
clip revisions show); unconfirmed judge candidates stay in `chat_guests` and `routes` so later clips can confirm them,
and are not written into any clip's session.  Legacy records without `confirmed` migrate on first read: guests that
pass the rule now are recorded in `confirmed`, while never-confirmed candidates come off the list.  Clips whose session
disagrees with the day's list are found `behind` / `lacking` and redone from the speaker step (`catch_up`, cli run
--refresh-guests, every unattended round).  A list written by hand (participants.json) is not touched.

Two guests on one sound path (`path_voices`, `decide`): where step 4 found the clip in a guest's own recording
(arrival.py), each lag group that is not the host's is one sound path, and the classifier's guest classes on it say
whether one person or two speak there.  Two of the day's guests on one path in enough clips is kept as the day's
`shared_path` and written into the session (`session["shared_path"]`: a list of name lists), for the steps that would
otherwise take a path for a person."""

from __future__ import annotations

import datetime as dt
import html
import json
import re
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from . import text_overlap
from .lane_rules import rules
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
_ROOM_TITLE = re.compile(r'<BililiveRecorderRecordInfo\b[^>]*?\stitle="([^"]*)"')
HEADER_BYTES = 8192   # the recorder writes its record info in the first lines of the chat XML

Dynamics = dict[str, list[dict[str, str]]]          # account (HOST for the host) -> [{"published_at", "text"}]


def streamers(ctx: Ctx) -> dict[str, list[str]]:
    """The knowledge graph's accepted people (accepted person nodes grouped by accepted alias_of edges), the host
    left out: formal name -> spellings.  Her formal name is the reviewed streamer registry's when one of her spellings
    is a registry name (the title prefix needs that), else the node's official/display name; nodes with the same formal
    name are one person (person:安晚 and person:安晚awa).  Every spelling of a group that shares one with the host's
    names is the host's (李豆沙: 豆沙, lds, 小李 ...), and any group with one of those is left out."""
    from src.autoslice.channel_envelope import HOST_NAMES, load_related_streamers

    from . import graph_context

    graph = graph_context.load(ctx).graph
    if graph is None:
        return {}
    try:
        registry = set(load_related_streamers())
    except Exception:  # noqa: BLE001 - an unreadable registry only loses the formal spelling
        registry = set()
    # accepted persons only, detection_name=false ones too: that flag keeps a short name out of subtitle name detection,
    # and the chat writing 安晚 or 礼墨 is valid evidence; candidate persons and candidate aliases never name streamers
    accepted = {nid: n for nid, n in graph.nodes.items() if n.kind == "person" and n.status == "accepted"}
    groups = [members for group in graph.alias_groups()
              if (members := [accepted[nid] for nid in group if nid in accepted])]
    alias_from = {e.from_id for e in graph.edges if e.type == "alias_of" and e.status == "accepted"}
    host = {*HOST_NAMES, HOST}
    for members in groups:
        surfaces = {s for n in members for s in n.surfaces()}
        if surfaces & host:
            host |= surfaces
    out: dict[str, set[str]] = {}
    for members in groups:
        surfaces = {s for n in members for s in n.surfaces()}
        if surfaces & host:
            continue
        formal = next((s for s in sorted(surfaces) if s in registry), None)
        if formal is None:
            primary = [n for n in members if n.id not in alias_from] or members
            node = sorted(primary, key=lambda n: n.id)[0]
            formal = node.names.get("official") or node.names.get("display") or sorted(surfaces)[0]
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


def _detector() -> Callable[[str, Sequence[str]], bool]:
    """Whether a text writes one of a person's spellings, as detection counts a spelling (graph_context._spelled)."""
    from .graph_context import _spelled

    cfg = rules("detection")
    least = int(cfg.get("latin_min_letters") or 1)
    common = {w.lower() for w in cfg.get("latin_common_words") or []}
    return lambda text, spellings: any(_spelled(s, text, least, common) for s in spellings)


def mentions(chat: Sequence[str], known: dict[str, list[str]]) -> dict[str, list[str]]:
    """Each person -> the chat lines that name her (any number)."""
    writes = _detector()
    return {name: [t for t in chat if writes(t, spellings)] for name, spellings in known.items()}


def room_title(clip_dir: Path) -> str:
    """The live room's title when the clip's chat file was recorded (the recorder's header of danmaku/recording.xml,
    step 3's copy of the source recording's chat), or "" (no file; a joined clip's chat carries no header)."""
    path = clip_dir / "danmaku/recording.xml"
    if not path.exists():
        return ""
    with path.open("rb") as f:
        head = f.read(HEADER_BYTES).decode("utf-8", errors="replace")
    m = _ROOM_TITLE.search(head)
    return html.unescape(m.group(1)).strip() if m else ""


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


def linked(dynamics: Dynamics, date: str, title: str = "") -> dict[str, list[str]]:
    """Accounts whose dynamics around the broadcast day tie them to the host -> what ties them: a word (two Chinese
    characters) the host's dynamics around the day or the day's live room title (`title`) hold and that neither wrote
    before (女大矿工 / 矿工, not 晚安), or one of the host's names.  A shared new word can be chance (11-18: 电脑, 小播 in
    the dynamics of four accounts), so this is shown to CPA as a lead and asked of a quote (`kept`), not taken as the
    answer."""
    host_near, host_before = _around(dynamics.get(HOST, []), date)
    fresh = _pairs([*host_near, title]) - _pairs(host_before)
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


def needed(ctx: Ctx, *, heard: bool | None = None) -> bool:
    """Someone else speaks in this clip, and its session's guests are the lane's to name: nobody is named yet, or they
    were named from the day's record (`guest_evidence`), which may hold more people by now.  A session whose guests
    were written by hand is left as written.  `heard` is step 4's answer before the lines carry speakers (the
    voiceprint hears others, step4_speaker.guests_heard); without it, a line step 4 labelled as a guest's."""
    from .config import participant_names
    from .subtitle_track import read_track

    if [n for n in participant_names(ctx.session) if n != HOST] and not ctx.session.get("guest_evidence"):
        return False
    if heard is not None:
        return heard
    return any(c.get("speaker") == GUEST_LABEL for c in read_track(ctx.clip_dir))


class GuestsUnknown(RuntimeError):
    """The CPA call that tells who is in the call failed: the step fails and is run again, rather than titling a
    collab with its guests unnamed (11-23: nine of ten clips asked at once, the gateway refused the copies of the
    one prompt in flight, and each went on unnamed)."""


# ----- what a clip itself shows of who is in the call (no model) -------------------------------------------------

VOICE, SUBTITLES, PEERS = "voice", "subtitles", "peers"       # the routes read from the lane's own products
LINKED = "linked"                                              # a dynamics or room-title tie to the host
CHAT = "chat"                                                  # the judge's answer on the chat and the dynamics
VOICE_PREDS = "speaker/chunks-pred.json"       # the frozen classifier's probabilities of every class for each chunk
VOICE_UNITS = "speaker/chunks-units.json"      # where each chunk lies in the clip
PEER_VIDEOS = "dedup/result.json"              # step 0: other uploaders' clips of the session
_BRACKET = re.compile(r"【([^【】]*)】")


def _confident(clip_dir: Path, min_prob: float) -> tuple[list[str], list[tuple[str, str]]]:
    """(the classifier's guest classes, [(chunk id, the guest class it holds with at least `min_prob`)]) of the clip's
    chunk run; ([], []) for a clip without one (no voiceprint workspace, a revision)."""
    path = clip_dir / VOICE_PREDS
    if not path.exists():
        return [], []
    classes: dict[str, None] = {}
    held = []
    for row in json.loads(path.read_text(encoding="utf-8"))["predictions"]:
        if not row.get("ok"):
            continue
        probs = {str(k): float(v) for k, v in row["probs"].items()}
        classes.update(dict.fromkeys(k for k in probs if k != HOST))
        top = max(probs, key=probs.__getitem__)
        if top != HOST and probs[top] >= min_prob:
            held.append((str(row["id"]), top))
    return list(classes), held


def voice_heard(clip_dir: Path, min_prob: float) -> dict[str, int]:
    """{each guest class of the classifier: the clip's chunks that are hers with at least `min_prob`}.  The classes are
    a closed set: a voice that is none of them (a guest it was not trained on, a watched video, the host singing) falls
    into one of them too, so this never names a person alone (`decide`)."""
    classes, held = _confident(clip_dir, min_prob)
    return {name: sum(1 for _id, top in held if top == name) for name in classes}


def path_voices(clip_dir: Path, min_prob: float) -> list[dict[str, int]]:
    """Per sound path of the guests in the clip's arrival evidence (arrival.py: a lag group that is not the host's),
    {guest class: the confident chunks lying on it}; [] without the evidence.  A chunk lies on the path whose
    stretches hold arrival.LINE_MIN_SHARE of the time any stretch covers of it."""
    from . import arrival

    evidence = arrival.load(clip_dir)
    classes, held = _confident(clip_dir, min_prob)
    if evidence is None or not held:
        return []
    spans = {str(u["id"]): (int(u["s"]), int(u["e"]))
             for u in json.loads((clip_dir / VOICE_UNITS).read_text(encoding="utf-8"))["units"]}
    paths = [label for label, named in evidence["named"].items() if named["speaker"] == GUEST_LABEL]
    out = {label: dict.fromkeys(classes, 0) for label in paths}
    for chunk, top in held:
        a, b = spans[chunk]
        on: dict[str, int] = {}
        for seg in evidence["segments"]:
            ov = min(b, int(seg["end_ms"])) - max(a, int(seg["start_ms"]))
            if ov > 0:
                on[str(seg.get("speaker"))] = on.get(str(seg.get("speaker")), 0) + ov
        if on and (label := max(on, key=on.__getitem__)) in out and on[label] >= arrival.LINE_MIN_SHARE * sum(on.values()):
            out[label][top] += 1
    return [out[label] for label in paths]


def answered(track: Sequence[Mapping[str, Any]], known: Mapping[str, Sequence[str]], within: int) -> dict[str, int]:
    """{person: the host's lines that name her and have a guest's line among the `within` lines after}.  A name the
    host says while a guest answers is someone in the talk or someone talked about: one route, never enough alone."""
    from .subtitle_track import plain_text

    writes = _detector()
    out: dict[str, int] = {}
    for i, cue in enumerate(track):
        if cue.get("speaker") != HOST or not any(c.get("speaker") == GUEST_LABEL for c in track[i + 1:i + 1 + within]):
            continue
        text = plain_text(str(cue["text"]))
        for name, spellings in known.items():
            if writes(text, spellings):
                out[name] = out.get(name, 0) + 1
    return out


def peer_titles(clip_dir: Path, known: Mapping[str, Sequence[str]]) -> dict[str, dict[str, Any]]:
    """{bvid: {"title", "names"}} for the other uploaders' videos step 0 confirmed as this session's
    (external_duplicate_check.same_session: the title or description carries the day) whose title has a 【】 that
    names the host and, in the same 【】, known people: a clipper's own list of who is in the clip (11-24
    「玩的有点大的女大组三人【礼墨&安晚&李豆沙】」; a name outside the 【】 is only talked about, 11-23 「【礼墨×李豆沙×安晚】
    今晚联动怎么有南町nightin啊」)."""
    path = clip_dir / PEER_VIDEOS
    if not path.exists():
        return {}
    writes, host = _detector(), sorted(_host_spellings())
    out: dict[str, dict[str, Any]] = {}
    for video in json.loads(path.read_text(encoding="utf-8")).get("videos") or []:
        title = str(video.get("title") or "")
        names = sorted({name for part in _BRACKET.findall(title) if writes(part, host)
                        for name, spellings in known.items() if writes(part, spellings)})
        if video.get("same_session_confirmed") and names:
            out[str(video["bvid"])] = {"title": title, "names": names}
    return out


def _watched_video(clip_dir: Path) -> bool:
    """Step 4 took the other voices of this clip for a video the host is watching (step4_speaker.OTHER_VOICES_MODE)."""
    from .step4_speaker import OTHER_VOICES_MODE

    path = clip_dir / "speaker/decision.json"
    return path.exists() and json.loads(path.read_text(encoding="utf-8")).get("mode") == OTHER_VOICES_MODE


def clip_evidence(ctx: Ctx, known: Mapping[str, Sequence[str]]) -> dict[str, Any]:
    """What this clip adds to the day's record: see the three routes above, and the guests' sound paths.  Where the
    other voices are a watched video's, they are nobody in the call (a streamer's own video played back is her voice
    and her name): such a clip adds other uploaders' titles only."""
    from .subtitle_track import read_track

    cfg = rules("collab_guests")
    min_prob = float(cfg["voice_min_prob"])
    if _watched_video(ctx.clip_dir):
        return {VOICE: dict.fromkeys(voice_heard(ctx.clip_dir, min_prob), 0), SUBTITLES: {},
                PEERS: peer_titles(ctx.clip_dir, known), "paths": []}
    return {VOICE: voice_heard(ctx.clip_dir, min_prob),
            SUBTITLES: answered(read_track(ctx.clip_dir), known, int(cfg["answer_within"])),
            PEERS: peer_titles(ctx.clip_dir, known),
            "paths": path_voices(ctx.clip_dir, min_prob)}


def decide(clips: Mapping[str, Mapping[str, Any]], chat: Sequence[str],
           guests: Sequence[str] = (), *,
           links: Mapping[str, Sequence[str]] | None = None,
           confirmed: Mapping[str, Any] | Sequence[str] = ()) -> tuple[dict[str, dict[str, Any]], list[str], list[list[str]]]:
    """From the clips' evidence (`clip_evidence` by clip id), the judge's guests `chat`, the day's guests so far,
    the dynamics/room-title ties `links`, and already-confirmed guests `confirmed`: ({person: what each route holds of
    her}, the confirmed day's guests, the sound paths two of them share).

    A route names a person when (lane_rules collab_guests): linked - `links[name]` is non-empty (only confirms a judge
    candidate, never counts toward `routes_needed` for someone unnamed by the judge); voice - `voice_min_clips` clips
    (or 1 clip when named in `chat`) each hold `voice_min_chunks` confident chunks of her class; subtitles - the host
    named her and a guest answered `answered_min` times over `answered_min_clips` clips; peers - `peer_min_videos`
    videos.  A person in `chat` is confirmed when at least one non-chat-talk route (linked or a measured route) backs
    her; an unnamed person needs `routes_needed` measured routes.  Anyone in `confirmed` stays permanently on the
    day's guests; unconfirmed candidates stay in `routes` with `chat=True` and `routes=[]`, and are not on the list.
    The voice counts only on a day where everyone the other routes, the judge, and `confirmed` name is a class of the
    classifier (its classes are a closed set: with a guest it does not know in the call, her voice is counted as one of
    them).
    Two guests share a path when, in `shared_path_min_clips` clips, one guests' path holds both: the lesser of the two
    with `shared_path_min_chunks` confident chunks and `shared_path_min_share` of the path's."""
    cfg = rules("collab_guests")
    links = links or {}
    chat_set = set(chat)
    prior = list(confirmed)
    prior_set = set(prior)
    seen: dict[str, dict[str, Any]] = {}

    def row(name: str) -> dict[str, Any]:
        return seen.setdefault(name, {CHAT: name in chat_set, LINKED: list(links.get(name) or []),
                                        VOICE: 0, SUBTITLES: [0, 0], PEERS: [], "routes": []})

    classes = {name for ev in clips.values() for name in (ev.get(VOICE) or {})}
    for ev in clips.values():
        for name, n in (ev.get(VOICE) or {}).items():
            row(name)[VOICE] += n >= int(cfg["voice_min_chunks"])
        for name, n in (ev.get(SUBTITLES) or {}).items():
            row(name)[SUBTITLES] = [row(name)[SUBTITLES][0] + n, row(name)[SUBTITLES][1] + 1]
        for bvid, video in (ev.get(PEERS) or {}).items():
            for name in video["names"]:
                row(name)[PEERS] = sorted({*row(name)[PEERS], bvid})
    for name in [*prior, *guests, *chat]:
        row(name)
    need = int(cfg["routes_needed"])

    def named(*, voice: bool) -> list[str]:
        active = set(prior_set)
        for name, got in seen.items():
            is_chat = bool(got[CHAT])
            got["routes"] = [route for route, rings in (
                (LINKED, is_chat and bool(got[LINKED])),
                (VOICE, voice and got[VOICE] >= (1 if is_chat else int(cfg["voice_min_clips"]))),
                (SUBTITLES, got[SUBTITLES][0] >= int(cfg["answered_min"]) and got[SUBTITLES][1] >= int(cfg["answered_min_clips"])),
                (PEERS, len(got[PEERS]) >= int(cfg["peer_min_videos"]))) if rings]
            if len(got["routes"]) >= (1 if is_chat else need):
                active.add(name)
        ordered: list[str] = []
        for n in (*prior, *guests, *chat, *sorted(active)):
            if n in active and n not in ordered:
                ordered.append(n)
        return ordered

    out = named(voice=False)
    if set(out) <= classes:       # everyone named without the voice is a class of the classifier: its classes count
        out = named(voice=True)
    together: dict[tuple[str, str], int] = {}
    for ev in clips.values():
        pairs = set()
        for path in ev.get("paths") or ():
            on = sorted(((n, name) for name, n in path.items() if name in out), reverse=True)[:2]
            if (len(on) == 2 and on[1][0] >= int(cfg["shared_path_min_chunks"])
                    and on[1][0] >= float(cfg["shared_path_min_share"]) * sum(path.values())):
                pairs.add(tuple(sorted((on[0][1], on[1][1]), key=out.index)))
        for pair in pairs:
            together[cast("tuple[str, str]", pair)] = together.get(cast("tuple[str, str]", pair), 0) + 1
    shared = [list(pair) for pair, n in sorted(together.items()) if n >= int(cfg["shared_path_min_clips"])]
    return {n: got for n, got in seen.items() if got[CHAT] or got["routes"] or n in out}, out, shared


# ----- the day's record ---------------------------------------------------------------------------------------------

def _chat_record(ctx: Ctx, date: str, known: dict[str, list[str]], ask: Callable[[str, str], dict[str, Any] | None]) -> dict[str, Any]:
    """The judge's part of the day's record, asked once: the people the chat names often enough or who posted around
    the day, their lines and posts, one CPA call, and the guests whose quotes stand (`kept`)."""
    chat = session_chat(ctx.clip_dir)
    said = mentions(chat, known)
    named = {name: lines for name, lines in said.items() if len(lines) >= MIN_LINES}
    uids = accounts()
    uids = dict(sorted(uids.items(), key=lambda kv: -len(said.get(kv[0], [])))[:PEERS_READ])
    dynamics, unread = read_dynamics(uids)
    title = room_title(ctx.clip_dir)
    links = linked(dynamics, date, title)
    for name, items in dynamics.items():
        if name != HOST and _around(items, date)[0]:
            named.setdefault(name, said.get(name, []))
    posted = {name: _around(dynamics.get(name, []), date)[0] for name in named}
    host_posted = _around(dynamics.get(HOST, []), date)[0]
    seen = {"room_title": title, "host": host_posted, "accounts": {n: v for n, v in posted.items() if v}, "linked": links, "unread": unread}
    if not named:
        return {"date": date, "candidates": {}, "dynamics": seen, "chat_guests": []}
    from .step_io import _render

    prompt = _render("collab_guests.md", room_title=title or "（没读到）",
                     host_dynamics="\n".join(f"- {t}" for t in host_posted) or "（没有）",
                     candidates="\n\n".join(_candidate_block(name, lines, posted[name], links.get(name))
                                            for name, lines in named.items()))
    answer = ask("collab-guests", prompt)
    if answer is None:
        raise GuestsUnknown("CPA did not answer who is in the call (collab-guests)")
    return {"date": date, "candidates": {n: len(v) for n, v in named.items()}, "dynamics": seen,
            "answer": answer, "chat_guests": kept(answer, named, posted, links)}


def _candidate_block(name: str, lines: Sequence[str], posted: Sequence[str], words: Sequence[str] | None) -> str:
    head = f"### {name}（弹幕里 {len(lines)} 条提到）"
    chat = "\n".join(f"- {t}" for t in lines[:SHOWN]) or "- （弹幕里没有提到她）"
    own = ("\n她这几天自己发的动态：\n" + "\n".join(f"- {t}" for t in posted)) if posted else ""
    shared = (f"\n她的动态和主播这几天的动态或直播间标题都写到、两人以前都没写过的词：{'、'.join(words)}"
              if words else "")
    return f"{head}\n{chat}{own}{shared}"


def _task(ctx: Ctx) -> Path:
    return ctx.clip_dir.parent.parent


def _published_matching(task: Path, date: str, pred: Callable[[list[str]], bool]) -> list[str]:
    out = []
    for path in sorted((task / "state").glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        session = data.get("session") or {}
        if (session.get("recording_date") == date and session.get("guest_evidence") is not None
                and data["clip"].get("status") != "SUPERSEDED"
                and (data.get("steps") or {}).get("upload", {}).get("status") == "DONE"
                and pred([str(g[0]) for g in session.get("participants") or [] if g])):
            out.append(str(data["clip"]["cid"]))
    return out


def _published_without(task: Path, date: str, names: Sequence[str]) -> list[str]:
    """The day's clips that went online named from the day's record while it lacked one of `names` (their titles are
    the owner's to change, not the lane's)."""
    return _published_matching(task, date, lambda have: any(n not in have for n in names))


def _published_with(task: Path, date: str, names: Sequence[str]) -> list[str]:
    """The day's clips that went online named from the day's record while it held one of `names` (their titles are
    the owner's to change, not the lane's)."""
    return _published_matching(task, date, lambda have: any(n in have for n in names))


@contextmanager
def _day_lock(cache: Path) -> Iterator[None]:
    """The day's lock: held by whoever reads or rewrites the day's record."""
    import fcntl

    cache.parent.mkdir(parents=True, exist_ok=True)
    with open(cache.with_suffix(".lock"), "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def _recompute(record: dict[str, Any], task: Path, *, clip: str | None = None) -> bool:
    """Recompute routes, confirmed guests, and shared_path on `record` under the current rule; returns whether changed."""
    from .state import utc_now
    from .step_io import superseded

    before = [str(g) for g in record.get("guests") or []]
    had_chat = "chat_guests" in record
    had_confirmed = "confirmed" in record
    chat = [str(g) for g in record.setdefault("chat_guests", before)]
    raw_confirmed = record.get("confirmed")
    confirmed: dict[str, dict[str, Any]] = (
        {str(k): dict(v) for k, v in raw_confirmed.items() if isinstance(v, Mapping)}
        if isinstance(raw_confirmed, Mapping) else {}
    )
    live = {c: ev for c, ev in (record.get("clips") or {}).items() if not superseded(task, c)}
    links = (record.get("dynamics") or {}).get("linked") or {}
    routes, guests, shared = decide(live, chat, before, links=links, confirmed=confirmed)
    new_confirmed = dict(confirmed)
    now: str | None = None
    for g in guests:
        if g not in new_confirmed:
            if now is None:
                now = utc_now()
            got = routes[g]
            new_confirmed[g] = {"by": [*([CHAT] if got[CHAT] else []), *got["routes"]], "at": now, "clip": clip}
    changed = (not had_chat or not had_confirmed or record.get("confirmed") != new_confirmed
               or record.get("routes") != routes or record.get("guests") != guests
               or record.get("shared_path") != shared)
    record["confirmed"] = new_confirmed
    record["routes"], record["guests"], record["shared_path"] = routes, guests, shared
    return changed


def _read_record(task: Path, cache: Path, *, persist: bool = False, persist_migration: bool = False) -> dict[str, Any]:
    """Read a stored day record under its lock and recompute it under the current rule; persist if requested."""
    from .step_io import _write_atomic

    with _day_lock(cache):
        record = cast(dict[str, Any], json.loads(cache.read_text(encoding="utf-8")))
        migrating = "confirmed" not in record
        changed = _recompute(record, task)
        if changed and (persist or (persist_migration and migrating)):
            _write_atomic(cache, json.dumps(record, ensure_ascii=False, indent=1))
        return record


def migrate_day_record(task: Path, date: str) -> None:
    """If the day's record exists without `confirmed` (pre-G1 format), migrate and persist it before a clip is superseded."""
    if not date:
        return
    cache = task / CACHE.format(date=date)
    if cache.exists():
        _read_record(task, cache, persist_migration=True)


def _day_record(ctx: Ctx, cache: Path, date: str, ask: Callable[[str, str], dict[str, Any] | None]) -> dict[str, Any]:
    """The day's record after this clip: read from the task directory (the judge asked first when there is none),
    this clip's evidence put in place of what it gave before, and the guests decided again.  All of it while holding
    the day's lock, so the clips of one day that arrive together wait for the first instead of sending the same
    prompt side by side, and none loses another's evidence."""
    from .state import utc_now
    from .step_io import _write_atomic

    with _day_lock(cache):
        existed = cache.exists()
        known = streamers(ctx)
        record = (cast(dict[str, Any], json.loads(cache.read_text(encoding="utf-8"))) if existed
                  else _chat_record(ctx, date, known, ask))
        before = [str(g) for g in record.get("guests") or []]
        cid = str(ctx.state.data["clip"]["cid"])
        if existed and "confirmed" not in record:
            # a record kept before confirmations were recorded: who its own evidence confirms is settled first, from
            # the record as it was read, before this clip's evidence (made under today's list) replaces what it held
            _recompute(record, _task(ctx))
        record.setdefault("clips", {})[cid] = clip_evidence(ctx, known)
        _recompute(record, _task(ctx), clip=cid)
        added = [g for g in record["guests"] if g not in before]
        removed = [g for g in before if g not in record["guests"]]
        if added and existed:      # the list grew after it was first kept: who was named without them
            record.setdefault("added", []).append({
                "at": utc_now(), "clip": cid, "guests": added, "by": {g: record["routes"][g]["routes"] for g in added},
                "judge_said_not": [g for g in added if g in ((record.get("answer") or {}).get("not_in_call") or [])],
                "published_without": _published_without(_task(ctx), date, added)})
        if removed and existed:    # unconfirmed candidates came off the list on recompute
            record.setdefault("removed", []).append({
                "at": utc_now(), "clip": cid, "guests": removed,
                "published_with": _published_with(_task(ctx), date, removed)})
        _write_atomic(cache, json.dumps(record, ensure_ascii=False, indent=1))
        return record


def _is_host_group(group: Sequence[Any]) -> bool:
    return any(str(s).strip() == HOST for s in group)


def _session_guests(session: Mapping[str, Any]) -> list[str]:
    """Named guests currently in `session["participants"]` (excluding the host and anonymous placeholders)."""
    from .config import unknown_participant

    groups = [g for g in session.get("participants") or () if g]
    has_host = any(_is_host_group(g) for g in groups)
    out: list[str] = []
    for i, group in enumerate(groups):
        if _is_host_group(group) or (not has_host and i == 0):
            continue
        name = str(group[0]).strip()
        if name and not unknown_participant(name):
            out.append(name)
    return out


def _evidence_by(g: str, routes: Mapping[str, Any] | None, confirmed: Mapping[str, Any]) -> list[str]:
    if routes is not None and g in routes and routes[g].get("routes"):
        return [*([CHAT] if routes[g].get(CHAT) else []), *routes[g]["routes"]]
    if isinstance(confirmed.get(g), Mapping) and confirmed[g].get("by"):
        return [str(r) for r in confirmed[g]["by"]]
    if routes is not None and g in routes and routes[g].get(CHAT):
        return [CHAT]
    return []


def _apply(ctx: Ctx, record: Mapping[str, Any], name: str) -> list[str]:
    """Sync the clip's session with the day's confirmed guests (adding missing ones and dropping stale unconfirmed
    ones) and shared sound paths; returns the guests added or removed."""
    from .config import unknown_participant

    session = cast(dict[str, Any], ctx.session)
    have = _session_guests(session)
    want = [str(g) for g in record.get("guests") or []]
    added = [g for g in want if g not in have]
    removed = [g for g in have if g not in want]
    changed = [*added, *removed]
    shared = [list(p) for p in record.get("shared_path") or []]
    routes = record.get("routes")
    confirmed = record.get("confirmed") if isinstance(record.get("confirmed"), Mapping) else {}
    evidence = {"source": "collab_guests", "record": name, "guests": want,
                "by": {g: _evidence_by(g, routes, confirmed) for g in want}}
    prev_groups = list(session.get("participants") or [])
    has_host = any(g and _is_host_group(g) for g in prev_groups)
    is_base = [not g or _is_host_group(g) or (not has_host and i == 0) or unknown_participant(str(g[0]))
               for i, g in enumerate(prev_groups)]
    keep_base = [g for g, base in zip(prev_groups, is_base) if base]
    by_name = {str(g[0]).strip(): g for g, base in zip(prev_groups, is_base) if not base}
    new_participants = [*keep_base, *(by_name.get(g, [g]) for g in want)]
    if (not changed and session.get("participants") == new_participants
            and session.get("guest_evidence") == evidence and (session.get("shared_path") or []) == shared):
        return []
    session["participants"] = new_participants
    session["guest_evidence"] = evidence
    if shared or "shared_path" in session:
        session["shared_path"] = shared
    ctx.state.save()
    return changed


def name_guests(ctx: Ctx, ask: Callable[[str, str], dict[str, Any] | None], *, heard: bool | None = None) -> list[str]:
    """When `needed`: put this clip's evidence into the day's record, and sync the clip's session with the day's
    confirmed guests; returns the names added or removed.  `ask` sends the judge's prompt and returns the parsed answer,
    None when the call failed (GuestsUnknown); it is asked once a day (_day_record)."""
    if not needed(ctx, heard=heard):
        return []
    date = str(ctx.session.get("recording_date") or "")
    cache = _task(ctx) / CACHE.format(date=date)
    return _apply(ctx, _day_record(ctx, cache, date, ask), cache.name)


def process_line(ctx: Ctx, changed: Sequence[str]) -> str:
    """The PROCESS line of a clip whose session synced guests with the day's record: who was added or removed, by which
    routes, and what the record says of people sharing a sound path."""
    evidence = ctx.session.get("guest_evidence") or {}
    said = {CHAT: "整场弹幕与动态的判断", LINKED: "动态或直播间标题关联", VOICE: "声纹分类",
            SUBTITLES: "字幕里主播点名后嘉宾接话", PEERS: "同场他人切片标题"}
    listed = set(evidence.get("guests") or ())
    added = [g for g in changed if g in listed]
    removed = [g for g in changed if g not in listed]
    parts = []
    if added:
        by = evidence.get("by") or {}
        who = "、".join(f"{g}（{'＋'.join(said.get(r, r) for r in by.get(g, ())) or '当天名单早先认定，现有片的证据已不够'}）"
                       for g in added)
        parts.append(f"写进 {who}")
    if removed:
        parts.append(f"移出未证实候选 {'、'.join(removed)}")
    now = "、".join(evidence.get("guests") or ()) or "无"
    line = f"本场连线：本片场次{'、'.join(parts) or '未改'}；当天名单 {now}（{evidence.get('record', '')}）"
    shared = cast("dict[str, Any]", ctx.session).get("shared_path") or []
    return line + "".join(f"；{'、'.join(p)} 共用一路声音" for p in shared)


def lacking(ctx: Ctx) -> list[str]:
    """Guests the day's confirmed record and the clip's session disagree on (guests to add first, then stale
    never-confirmed guests to remove); [] for a clip that never asked the record, one whose people were written by hand,
    and a day without a record.  Migrates a legacy record without `confirmed` on first read; otherwise read-only."""
    cache = _task(ctx) / CACHE.format(date=str(ctx.session.get("recording_date") or ""))
    if ctx.session.get("guest_evidence") is None or not cache.exists():
        return []
    record = _read_record(_task(ctx), cache, persist_migration=True)
    have = _session_guests(ctx.session)
    want = [str(g) for g in record.get("guests") or []]
    return [*(g for g in want if g not in have), *(g for g in have if g not in want)]


def behind(ctx: Ctx) -> list[str]:
    """For a clip whose session was named from the day's record (its steps may all be done by now): put what its
    products show today into the record (or recompute and persist the stored record when subtitles are being remade),
    and return the guests its session disagrees on (`lacking`).  Nobody is asked."""
    date = str(ctx.session.get("recording_date") or "")
    cache = _task(ctx) / CACHE.format(date=date)
    if ctx.session.get("guest_evidence") is None or not cache.exists():
        return []

    def not_asked(_call_id: str, _prompt: str) -> dict[str, Any] | None:
        raise AssertionError("the day's record exists: the judge is not asked again")
    if (ctx.clip_dir / "subs/final.srt").exists():
        _day_record(ctx, cache, date, not_asked)
    else:
        _read_record(_task(ctx), cache, persist=True)
    return lacking(ctx)


def catch_up(ctx: Ctx) -> list[str]:
    """Sync the session of a clip that is `behind` with the recomputed day's record; returns the guests added or
    removed.  Nobody is asked and no clip evidence is added: the clip adds its own when its steps run again."""
    cache = _task(ctx) / CACHE.format(date=str(ctx.session.get("recording_date") or ""))
    record = _read_record(_task(ctx), cache, persist=True)
    return _apply(ctx, record, cache.name)
