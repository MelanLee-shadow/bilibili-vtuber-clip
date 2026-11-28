"""The channel's core draws: which of them a candidate's main line is about, and the points the views give them.

the channel's request.  The draws and their points are the channel's asset
(assets/<profile>/core_draws.json), fitted on the account's published clips: points = how many times the views double
when the main line is on that draw (month-adjusted, shrunk by how sure the fit is) × `points_per_doubling`.  A channel
without the asset has no draws and nothing here changes its selection.

The model only says which draws a candidate's main line is on (one call per recording, all its candidates at once); the
points are read from the asset at seating time, so a refit changes the seating of kept recalls without a new call.

A channel whose asset also names `stories` and `roles` (the channel's request (of three published clips)) has the same call say, of every
candidate: what kind of thing happens in it (`story`), where the host stands in it (`role`: one marked `seated: false` is
not made), whether a viewer of the circle who did not watch this stream can follow it (`understood`), who a `host_only`
draw happened to (not the host: the draw is dropped), and the one to three moments that matter most (`peaks`, kept on
the candidate for the title step).  The story and the role do not order the seats -- the content points do, an "or"
gate where any one strong draw is enough -- they say where a draw counts: `must_cut_when` (the collab is must-cut only
where something happens between her and a colleague and she is a party to it) and `points_when` (the collab draw gives
its points only there too: the game three colleagues play is not lifted over the floor by their being together).
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping, Sequence
from functools import lru_cache
from typing import Any, Protocol

from . import channel, discover_voices

ASSET = "core_draws.json"
SCHEMA = "core-draws.v1"
TEXT_LIMIT = 1500                     # characters of a candidate's subtitles in the labelling prompt
PEAKS_MAX = 3                         # the moments that matter most, per candidate
Cues = Mapping[str, tuple[int, str]]  # a recording's subtitles: cue id -> (start ms in the recording, text)
Voices = Mapping[str, float]          # cue id -> the host voiceprint's mean probability on it (discover_voices)


class Ask(Protocol):
    """The labelling call: the prompt, and the contract its answer must meet (`accepts`: "" for a usable parsed answer,
    else why not), which the lane's call (step_io._ask_cpa) holds an answer to before caching it, asking again if not."""

    def __call__(self, prompt: str, accepts: Callable[[Any], str] | None = None) -> str: ...


class DrawsError(RuntimeError):
    """The labelling answer is not usable (the recording is left for the next run, like a failed recall call)."""


@lru_cache(maxsize=1)
def load() -> dict[str, Any] | None:
    path = channel.asset(ASSET)
    if not path.exists():
        return None
    doc = json.loads(path.read_text(encoding="utf-8"))
    draws = doc.get("draws")
    if (doc.get("schema") != SCHEMA or not isinstance(doc.get("version"), str) or not isinstance(draws, list) or not draws
            or not all(isinstance(d, dict) and isinstance(d.get("key"), str) and isinstance(d.get("points"), (int, float))
                       and isinstance(d.get("definition"), str) and isinstance(d.get("name"), str) for d in draws)
            or len({d["key"] for d in draws}) != len(draws) or not isinstance(doc.get("max_points"), (int, float))
            or not all(isinstance(d.get("must_cut", False), bool) and isinstance(d.get("takes_seat", False), bool)
                       and isinstance(d.get("compiles", False), bool) for d in draws)
            or not isinstance(doc.get("audience_points_per_level", 0), (int, float))
            or not isinstance(doc.get("min_content", 0), (int, float))
            or not isinstance(doc.get("burst_points", 0), (int, float))
            or not (isinstance(doc.get("votes", 1), int) and doc.get("votes", 1) >= 1 and doc.get("votes", 1) % 2 == 1)):
        raise DrawsError(f"{path}: schema {SCHEMA}, version, max_points, optional audience_points_per_level, min_content and "
                         f"burst_points, "
                         f"optional odd votes, draws[{{key, name, definition, points, must_cut?, takes_seat?, compiles?}}] with distinct keys")
    stories, roles = doc.get("stories"), doc.get("roles")
    if (stories is None) != (roles is None):
        raise DrawsError(f"{path}: stories and roles go together")
    if stories is not None:
        named = lambda rows: (isinstance(rows, list) and rows and all(
            isinstance(r, dict) and all(isinstance(r.get(k), str) and r[k] for k in ("key", "name", "definition")) for r in rows)
            and len({r["key"] for r in rows}) == len(rows))
        if (not named(stories) or not named(roles) or not all(isinstance(s.get("needs_peak", False), bool) for s in stories)
                or not all(isinstance(r.get("seated", True), bool) for r in roles)
                or not all(r.get(k, r["key"]) in {x["key"] for x in roles} for r in roles for k in ("unpinned", "heard_from"))):
            raise DrawsError(f"{path}: stories[{{key, name, definition, needs_peak?}}], "
                             f"roles[{{key, name, definition, seated?, unpinned?, heard_from?: a role key}}]")
        known = {"story": {s["key"] for s in stories}, "role": {r["key"] for r in roles}}
        for draw in draws:
            for name in ("must_cut_when", "points_when"):
                when = draw.get(name, {})
                if not isinstance(when, dict) or not set(when) <= set(known) or not all(
                        isinstance(v, list) and v and set(v) <= known[k] for k, v in when.items()):
                    raise DrawsError(f"{path}: {draw['key']}.{name} names story/role keys of this asset")
    return doc


def has_stories(asset: Mapping[str, Any] | None = None) -> bool:
    """Whether the channel's draws asset names `stories` and `roles` (its candidates are labelled with them, gated by them)."""
    asset = load() if asset is None else asset
    return asset is not None and "stories" in asset


def points(entry: Mapping[str, Any], asset: Mapping[str, Any] | None = None) -> float:
    """The draw points of a labelled candidate, at most the asset's `max_points` (draws stacked on one clip do not keep
    adding views); 0 when the channel has no draws or the candidate is not labelled."""
    asset = load() if asset is None else asset
    label = entry.get("core_draws")
    if asset is None or not isinstance(label, Mapping):
        return 0.0
    # `points_when`: the draw counts only on a candidate whose story and role are among the listed ones (the game three
    # colleagues play is no matter between her and one of them: it passes the content floor like any other game, or not)
    table = {d["key"]: float(d["points"]) for d in asset["draws"]
             if all(label[field] in allowed for field, allowed in d.get("points_when", {}).items())}
    return round(min(float(asset["max_points"]), sum(table.get(key, 0.0) for key in label.get("draws", []))), 2)


def content(entry: Mapping[str, Any], asset: Mapping[str, Any] | None = None) -> float:
    """What orders the candidates that passed the quality gate (the channel's request): the draw points plus the scorecard's audience-interest level times the asset's points per level -- the
    only one of the seven dimensions the views of the published clips reward.  0 when the channel has no draws asset."""
    asset = load() if asset is None else asset
    if asset is None:
        return 0.0
    card = entry.get("selection_scorecard")
    dims = card.get("dimensions") if isinstance(card, Mapping) else None
    level = dims.get("audience_salience", 0) if isinstance(dims, Mapping) else 0
    per = float(asset.get("audience_points_per_level", 0))
    return round(points(entry, asset) + per * float(level if isinstance(level, (int, float)) else 0), 2)


def burst_points(entry: Mapping[str, Any], asset: Mapping[str, Any] | None = None) -> float:
    """What a candidate holding a danmaku burst of its broadcast (discover_select._settle_bursts marks `bursts`) gains in
    the order of the seats (the channel's request): the asset's `burst_points`, fitted on the views like the draws.  Order only: it is not content,
    so it neither lifts a candidate over the floor (`floor`) nor takes anything from one without a burst.  0 when the
    channel's asset has none or the candidate holds no burst."""
    asset = load() if asset is None else asset
    return float(asset.get("burst_points", 0)) if asset is not None and entry.get("bursts") else 0.0


def floor(asset: Mapping[str, Any] | None = None) -> float | None:
    """The content points a seat of the day's quota needs (the channel's request): the asset's `min_content`, fitted on the same views;
    None when the channel has no draws asset (no content score, so no floor)."""
    asset = load() if asset is None else asset
    return None if asset is None or "min_content" not in asset else float(asset["min_content"])


def must_cut(entry: Mapping[str, Any], asset: Mapping[str, Any] | None = None) -> list[str]:
    """The candidate's draws the channel makes whenever the quality gate passes (the channel's request); empty when none, or the channel has no draws asset."""
    asset = load() if asset is None else asset
    label = entry.get("core_draws")
    if asset is None or not isinstance(label, Mapping):
        return []
    # `must_cut_when`: the draw is must-cut only on a candidate whose story and role are among the listed ones (a collab
    # is must-cut where something happens between her and a colleague and she is a party to it; the game the three of them
    # play queues like any other candidate)
    keys = {d["key"] for d in asset["draws"] if d.get("must_cut")
            and all(label.get(field) in allowed for field, allowed in d.get("must_cut_when", {}).items())}
    return [k for k in label.get("draws", []) if k in keys]


def takes_seat(entry: Mapping[str, Any], asset: Mapping[str, Any] | None = None) -> bool:
    """Whether a must-cut candidate takes one of the day's seats (the channel's request): yes when every
    must-cut draw on it is marked `takes_seat`; a candidate with any seat-free must-cut draw (a collab) takes none."""
    asset = load() if asset is None else asset
    keys = must_cut(entry, asset)
    if asset is None or not keys:
        return False
    seat = {d["key"] for d in asset["draws"] if d.get("takes_seat")}
    return all(k in seat for k in keys)


def gate(entry: Mapping[str, Any], asset: Mapping[str, Any] | None = None) -> tuple[str, str] | None:
    """Why a labelled candidate of a channel with stories is not made, as (code, detail), or None when it may be.  Pass or fail,
    no points: the host only watches someone else's matter (`NOT_ABOUT_HOST`); a viewer of the circle who did not watch
    the stream cannot follow it (`NOT_UNDERSTOOD`); a story that needs its key moment has none (`NO_PEAK`)."""
    asset = load() if asset is None else asset
    if not has_stories(asset):
        return None
    found = entry["core_draws"]
    role = next(r for r in asset["roles"] if r["key"] == found["role"])
    story = next(s for s in asset["stories"] if s["key"] == found["story"])
    if not role.get("seated", True):
        return "NOT_ABOUT_HOST", f"{role['name']}：{found['why']}"[:200]
    if not found["understood"]:
        return "NOT_UNDERSTOOD", str(found["missing"])[:200]
    if story.get("needs_peak") and not entry.get("peaks"):
        return "NO_PEAK", f"{story['name']}：找不到最关键的那一下"
    return None


def focus(asset: Mapping[str, Any] | None = None) -> str:
    """What the channel picks, for the recall prompt: its two gates and its kinds of story, in the asset's own words."""
    asset = load() if asset is None else asset
    if not has_stories(asset):
        raise DrawsError("the channel's draws asset names no stories: it has no selection focus")
    host = channel.profile().display_name
    seated = "，或".join(f"{r['name']}（{r['definition']}）" for r in asset["roles"] if r.get("seated", True))
    unseated = "，或".join(f"{r['name']}（{r['definition']}）" for r in asset["roles"] if not r.get("seated", True))
    return (
        f"选什么（“她”指主播{host}）。先过两道门，只判过不过：\n"
        f"- 围绕她：她在这件事里是{seated}，才选；她只是{unseated}，不选。\n"
        "- 没看这场直播、但认识这些主播和圈内梗的观众看得懂：这件事的前因在片内，开头悬空就把 start_cue 往前接到触发点；"
        "圈内都知道的人和梗不用解释。事情讲得完整只是能不能看的底线，不是值得选的理由。\n"
        "过了门，值得选的是有看点的事，下面任何一类够好看就值得选，不分先后：\n"
        + "\n".join(f"- {s['name']}：{s['definition']}"
                    + ("。这一类要说得出其中最关键的那一下（哪一句会让人想点进来）才选；好看的照样选，"
                       "只因为讲得完整、或几位主播一起在场，不算选它的理由" if s.get("needs_peak") else "")
                    for s in asset["stories"])
        + "\n几个人一起做同一件事（一起打游戏、一起聊天）时，最值得选的是其中落在她身上的那一下。"
    )


def _numbered(entry: Mapping[str, Any], cues: Cues, voices: Voices | None) -> str:
    def line(cue: str) -> str:
        tag = discover_voices.mark(voices.get(cue)) if voices is not None else ""
        return f"#{cue} " + (f"〔{tag}〕" if tag else "") + " ".join(cues[cue][1].split())
    return "\n".join(line(str(cue)) for cue in entry["cue_ids"])


def prompt(entries: Sequence[Mapping[str, Any]], asset: Mapping[str, Any], text_limit: int = TEXT_LIMIT, *,
           cues: Cues | None = None, voices: Voices | None = None, vote: int = 1) -> str:
    """The labelling prompt.  With `cues` (the recording's subtitles) and an asset with stories it also asks for the story, the
    role, whether the clip can be followed, who a host-only draw happened to, and the peaks; the subtitles are then given
    whole, one numbered line per cue, each marked with what the host's voiceprint heard when `voices` is given.
    Without `cues` it asks for the draws alone (clip_performance labels published
    clips from their title and description).  `vote` > 1 is a further, independent asking of the same question (`label`)."""
    lines = [f"- {d['key']}（{d['name']}）：{d['definition']}" for d in asset["draws"]]
    head = ("下面是一场直播里召回的切片候选（每条给了选片理由和字幕）。请判断每条的主线落在哪些看点上。\n"
            "只算主线：片子大部分在讲它、或它就是片子的笑点/落点；只顺带提一句、或只出现一个名字的不算。一条可以没有看点，也可以有几个。\n"
            "看点（key：定义）：\n" + "\n".join(lines))
    if cues is None or not has_stories(asset):
        items = [{"id": e["candidate_id"], "hook": str(e.get("hook", "")), "text": str(e.get("text", ""))[:text_limit]} for e in entries]
        return (head + "\n\n候选：\n" + json.dumps(items, ensure_ascii=False, indent=1) +
                '\n\n只输出 JSON：{"labels": [{"id": "候选 id", "draws": ["key", ...], "why": "一句话：主线是什么、为什么是这些看点"}]}，每个候选恰好一条。')
    host = channel.profile().display_name
    only = [d for d in asset["draws"] if d.get("host_only")]
    stories = "\n".join(f"- {s['key']}（{s['name']}）：{s['definition']}" for s in asset["stories"])
    roles = "\n".join(f"- {r['key']}（{r['name']}）：{r['definition']}" for r in asset["roles"])
    legend = (f"#编号 〔声纹〕 文本；〔她〕是声纹听出这一句是{host}，〔别人〕是听出不是她，没有标记的是声纹拿不准。声纹偶尔会错，"
              "和内容明显冲突时按内容判断" if voices is not None else f"#编号 文本；没有说话人标注，一行可能是{host}，也可能是别人")
    items = "\n\n".join(f"候选 {e['candidate_id']}\n选片理由：{e.get('hook', '')}\n字幕（{legend}）：\n"
                         f"{_numbered(e, cues, voices)}" for e in entries)
    subjects = ""
    if only:
        subjects = ("\n- subjects：draws 里有 " + "、".join(d["key"] for d in only) + f" 时必填，写这件事出在谁身上："
                    f'{{"看点 key": {{"host": 是{host}本人填 true、是别人（嘉宾、队友、观众）填 false, "who": "出事的是谁"}}}}。'
                    f"出事的不是{host}本人，就不要标这个看点。")
    return (
        head + f"\n\n另外，对每条候选再答下面几项（“她”指主播{host}）：\n"
        "- story：这件事是哪一类，只选一个最贴的。同时够得上两类时选排在前面的那类。\n" + stories + "\n"
        "- role：她在这件事里的位置，只选一个，her_line 抄一句她本人的原话作证。看这件事的看点落在谁身上：看点是她说的话、"
        "冲着她说的话，或说的就是她（她被冻住、被坑、被下禁令、被调侃、被定性），而且窗内有她本人的反应，是 party；"
        "几个人一起做一件事、看点不落在哪一个人身上，是 participant；看点在别人身上（别人自己出事、别人之间的话），"
        "她只是应一声、帮个忙、在旁边笑，是 bystander。"
        + ("哪句是她说的，先看行首的声纹标记。" if voices is not None else "字幕没有说话人标注，几个人一起播时很多句子不是她说的。")
        + "定 party 要在字幕里找得到把这件事钉在她身上的一句："
        + ("看点那一句就是标〔她〕的句子、" if voices is not None else "")
        + "有人叫她（名字、昵称，或“你”明确冲着她）、说的是她的人设或身份、"
        "或她自己说起自己遇到的事；pin 写那一句的 #编号。找不到这样一句、这件事换谁说都说得通的，不是 party"
        "（之后有了说话人还会再判一次）。\n" + roles + "\n"
        "- understood：一个没看这场直播、但认识这些主播和圈内梗的观众，只看这一段字幕能不能看懂这件事。前因在片内就算看得懂；"
        "圈内都知道的人和梗不用解释；开头接着片外的话说、而片内又补不出前因的，填 false，missing 写缺了什么。\n"
        f"- peaks：这一段里最关键的那一下，1 到 {PEAKS_MAX} 处，按重要程度排。每处写 cue（那句话的 #编号）、quote（那句原话，照字幕抄，"
        "可以连着抄相邻的一两句）、who（按内容判断是谁说的，判断不了留空）、why（一句话：为什么是这一下）。"
        "最关键的一下是让人想点进来看的那句话或那个反转，不是整段的概括；确实找不到就给空数组。" + subjects + "\n\n候选：\n\n" + items +
        '\n\n只输出 JSON：{"labels": [{"id": "候选 id", "draws": ["key", ...], "why": "一句话：主线是什么、为什么是这些看点", '
        '"story": "key", "role": "key", "pin": 整数或null, "her_line": "她的一句原话或空", "understood": true或false, "missing": "缺什么或空", '
        + ('"subjects": {"看点 key": {"host": true或false, "who": "谁"}}, ' if only else "") +
        '"peaks": [{"cue": 整数, "quote": "原话", "who": "说话人或空", "why": "一句话"}]}]}，每个候选恰好一条。'
        + (f"\n\n（同一个问题的第 {vote} 次独立判断。）" if vote > 1 else "")
    )


def _peaks(row: Mapping[str, Any], entry: Mapping[str, Any], cues: Cues) -> list[tuple[str, dict[str, Any]]]:
    """The peaks of one label row that name a cue of the candidate and quote something: (cue id, {quote, who, at_ms, why}),
    `at_ms` the start of that cue in the recording.  A peak that names no cue of the candidate is left out (the model's own
    slip must not fail the recording); a `peaks` that is not a list is a broken answer."""
    rows = row.get("peaks")
    if not isinstance(rows, list):
        raise DrawsError(f"peaks of {entry['candidate_id']} is not a list")
    inside = {str(c) for c in entry["cue_ids"]}
    out = []
    for peak in rows:
        cue = str(peak.get("cue")).lstrip("#") if isinstance(peak, dict) else ""
        quote = str(peak.get("quote") or "").strip() if isinstance(peak, dict) else ""
        if cue in inside and quote:
            out.append((cue, {"quote": quote[:200], "who": str(peak.get("who") or "").strip()[:40], "at_ms": cues[cue][0],
                              "why": str(peak.get("why") or "").strip()[:200]}))
    return out[:PEAKS_MAX]


def parse(raw: str, ids: Sequence[str], asset: Mapping[str, Any], *, entries: Mapping[str, Mapping[str, Any]] | None = None,
          cues: Cues | None = None, voices: Voices | None = None) -> dict[str, dict[str, Any]]:
    """The labels of an answer, by candidate id.  With `entries` and `cues` (the selection of a channel with stories) each label also
    carries story, role, her_line, understood, missing and `peaks`; a host-only draw that happened to someone else is
    dropped and named in `dropped`.  `voices` are the voiceprint readings the prompt was marked with."""
    match = re.search(r"\{.*\}", raw, re.S)
    try:
        doc = json.loads(match.group(0) if match else raw)
    except json.JSONDecodeError as exc:
        raise DrawsError(f"not JSON: {exc}") from exc
    keys = {d["key"] for d in asset["draws"]}
    full = entries is not None and cues is not None and has_stories(asset)
    out: dict[str, dict[str, Any]] = {}
    for row in doc.get("labels", []) if isinstance(doc, dict) else []:
        cid, draws = row.get("id") if isinstance(row, dict) else None, row.get("draws") if isinstance(row, dict) else None
        if cid not in ids or cid in out or not isinstance(draws, list) or not all(isinstance(d, str) and d in keys for d in draws):
            raise DrawsError(f"bad label row {str(row)[:200]}")
        out[cid] = {"version": asset["version"], "draws": sorted(set(draws)), "why": str(row.get("why", "")).strip()[:300]}
        if not full:
            continue
        if (row.get("story") not in {s["key"] for s in asset["stories"]} or row.get("role") not in {r["key"] for r in asset["roles"]}
                or not isinstance(row.get("understood"), bool)):
            raise DrawsError(f"bad story, role or understood in label row {str(row)[:200]}")
        dropped = []
        for draw in asset["draws"]:
            if draw.get("host_only") and draw["key"] in out[cid]["draws"]:
                subject = (row.get("subjects") or {}).get(draw["key"]) if isinstance(row.get("subjects"), dict) else None
                if not isinstance(subject, dict) or not isinstance(subject.get("host"), bool):
                    raise DrawsError(f"{cid}: {draw['key']} without its subject")
                if not subject["host"]:
                    dropped.append({"draw": draw["key"], "who": str(subject.get("who") or "").strip()[:40]})
        # a role that needs a line pinning the matter on her (`unpinned`: the role it is without one).  Before step 4 no line
        # has a speaker, and in a call of three the model credits her with a colleague's line (11-23 21:41, 「一个破石头到底
        # 有什么好吵的」, said by a guest): the role must rest on a line only she can be meant by, inside the candidate (with
        # voiceprint marks, also a highlight the voiceprint heard as hers).
        role, pin = row["role"], row.get("pin")
        unpinned = next(r.get("unpinned") for r in asset["roles"] if r["key"] == role)
        pinned = isinstance(pin, int) and not isinstance(pin, bool) and str(pin) in {str(c) for c in entries[cid]["cue_ids"]}
        if unpinned and not pinned:
            role = unpinned
        # the role whose highlight is her own line (`heard_from`: the role labelled where it is): 11-23 20:11, 「我们是团队」
        # 「我们不是敌人啊」, first peak of the model's own answer and heard as hers, was still labelled a participant.  Two
        # witnesses must agree that the line is hers: the voiceprint, and who the model itself says, from the content, spoke
        # it -- the voiceprint alone hears a guest as the host where her voice comes through a colleague's room (11-24: of
        # the lag group 礼墨 and 安晚 shared, 28% of the chunks were classed as the host's)
        peaks = _peaks(row, entries[cid], cues)
        heard = next((r["key"] for r in asset["roles"] if r.get("heard_from") == role), None)
        if (heard and peaks and voices is not None and discover_voices.mark(voices.get(peaks[0][0])) == "她"
                and _names_host(peaks[0][1]["who"])):
            role, pin, pinned = heard, int(peaks[0][0]), True
        out[cid].update(draws=[d for d in out[cid]["draws"] if d not in {x["draw"] for x in dropped}],
                        story=row["story"], role=role, pin=pin if pinned else None, labelled=row["role"] if role != row["role"] else None,
                        her_line=str(row.get("her_line") or "").strip()[:200],
                        understood=row["understood"], missing=str(row.get("missing") or "").strip()[:200], dropped=dropped,
                        peaks=[record for _, record in peaks])
    if set(out) != set(ids):
        raise DrawsError(f"labels for {sorted(set(ids) - set(out))} missing")
    return out


def _names_host(who: str) -> bool:
    """Whether the speaker a label row names is the host (one of the channel profile's names for her)."""
    host = channel.profile()
    return any(name in who for name in {host.display_name, *host.self_reference_aliases, *host.speaker_identity_aliases})


def _conditional(found: Mapping[str, Any], asset: Mapping[str, Any]) -> bool:
    """Whether a label carries a draw that counts, or is must-cut, only on some stories and roles (`points_when`,
    `must_cut_when`): its seat then turns on one reading of the story and the role."""
    return any(d["key"] in found["draws"] and (d.get("points_when") or d.get("must_cut_when")) for d in asset["draws"])


def _combine(answers: Sequence[Mapping[str, dict[str, Any]]], asset: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """One label per candidate from several independent answers (an odd number): the story and the role are the
    middle of the answers in the asset's own order (with three answers, the one at least two of them give), a draw and
    `understood` are what more than half say; the line, the pin and the peaks are those of an answer that gives the
    chosen story and role.  Every answer's story, role, draws and understood are kept (`votes`)."""
    stories, roles = [s["key"] for s in asset["stories"]], [r["key"] for r in asset["roles"]]
    out: dict[str, dict[str, Any]] = {}
    for cid in answers[0]:
        rows = [answer[cid] for answer in answers]
        half = len(rows) // 2
        story = sorted((r["story"] for r in rows), key=stories.index)[half]
        role = sorted((r["role"] for r in rows), key=roles.index)[half]
        understood = sum(bool(r["understood"]) for r in rows) > half
        draws = sorted(d for d in {d for r in rows for d in r["draws"]} if sum(d in r["draws"] for r in rows) > half)
        base = next((r for r in rows if r["story"] == story and r["role"] == role),
                    next((r for r in rows if r["role"] == role), rows[0]))
        missing = next((r["missing"] for r in rows if r["understood"] == understood), "")
        out[cid] = dict(base, story=story, role=role, understood=understood, missing=missing, draws=draws,
                        dropped=[x for x in base["dropped"] if x["draw"] not in draws],
                        votes=[{k: r[k] for k in ("story", "role", "draws", "understood")} for r in rows])
    return out


def pending(pool: Sequence[Mapping[str, Any]]) -> bool:
    """Whether any candidate of `pool` has no label of the asset's version (a channel without the asset: never)."""
    asset = load()
    return asset is not None and any((e.get("core_draws") or {}).get("version") != asset["version"] for e in pool)


def label(pool: list[dict[str, Any]], ask: Ask, cues: Cues | None = None, voices: Voices | None = None) -> bool:
    """Label, in place, the candidates of `pool` that have no label of the asset's version.  Returns whether any changed.
    The candidates of a channel with stories are labelled from the recording's subtitles (`cues`, required then); their peaks are
    written on the candidate itself (`peaks`), the key the title step reads.  `voices` (cue id -> host voiceprint
    probability) marks each line with whose voice it is."""
    asset = load()
    if asset is None:
        return False
    todo = [e for e in pool if (e.get("core_draws") or {}).get("version") != asset["version"]]
    if not todo:
        return False
    if has_stories(asset) and cues is None:
        raise DrawsError("a channel with stories labels its candidates from the recording's subtitles: no cues given")
    ids, entries = [e["candidate_id"] for e in todo], {e["candidate_id"]: e for e in todo}

    # an answer `parse` turns down (11-25 replays: the story key gag written among the draws, 7 of 97) is no answer: the
    # call asks again rather than keep it, so a cached bad answer cannot fail the recording on every run
    def usable(value: Any) -> str:
        try:
            parse(json.dumps(value, ensure_ascii=False), ids, asset, entries=entries, cues=cues, voices=voices)
        except DrawsError as exc:
            return f"CORE_DRAWS_UNUSABLE ({exc})"
        return ""

    def asked(text: str) -> dict[str, dict[str, Any]]:
        return parse(ask(text, accepts=usable), ids, asset, entries=entries, cues=cues, voices=voices)

    labels = asked(prompt(todo, asset, cues=cues, voices=voices))
    # a seat that turns on one reading of the story and the role is not left to one answer: the same question is asked
    # `votes` times in all and the answers are combined (_combine).  Asked once, 11-23 20:11 was labelled 「其他完整的事」
    # in one replay of fifteen, and the collab's points, and the clip, were gone.
    votes = int(asset.get("votes", 1)) if has_stories(asset) and cues is not None else 1
    if votes > 1 and any(_conditional(found, asset) for found in labels.values()):
        answers = [labels] + [asked(prompt(todo, asset, cues=cues, voices=voices, vote=n)) for n in range(2, votes + 1)]
        labels = _combine(answers, asset)
    for entry in todo:
        found = labels[entry["candidate_id"]]
        if "peaks" in found:
            entry["peaks"] = found.pop("peaks")
        entry["core_draws"] = found
    return True
