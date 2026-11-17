"""What each published clip was, how it was chosen and packaged, and how it did (one private record per BV).

公开规则(ledger U412): 「现在就开始记录每一个片子的……播放数据啊、时间呀、然后标签啊、内容啊之类的。你自己定一个
研究计划。」 The plan (admin, clip-performance-plan-20261117) asks per clip for: identity; the selection-time judgement
(seven dimensions, the draws read from the *final subtitles*, collab / guests / owner mark, the day's other candidates);
the packaging (title, cover words, tags, partition, intro, publish hour and weekday, hours from the stream's end to the
upload); view / like / coin / favourite / danmaku / reply / share and the account's followers on day 1, 3, 7 and 30; and
on day 7 where the audience comes from: of the first 200 commenters, how many wear the channel owner's fan medal, own it,
wear another streamer's medal (whose), or wear none, plus the guests' follower counts.

Records are research data: they live in the host's private state, never in the public repository. Only counts are kept
about commenters -- no commenter id or name is stored. Nothing here writes to Bilibili: every call is a read.
The monthly refit reads these records; this module only collects them.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import tempfile
import time
from collections import Counter
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from src.autoslice.manual_lane import core_draws

SCHEMA = "clip-performance.v1"
CONFIG_ASSET = "clip_performance.json"
CONFIG_SCHEMA = "clip-performance-config.v1"
SNAPSHOT_DAYS = (1, 3, 7, 30)
GRACE_DAYS = {1: 1.0, 3: 1.0, 7: 2.0, 30: 7.0}    # a snapshot taken later than this past its day is not that day's
AUDIENCE_DAY = 7
COMMENTERS = 200                                    # the plan: the first 200 commenters
REPLY_PAGES = 10                                    # 20 a page
SUBTITLE_LIMIT = 4000                               # characters of the final subtitles the draws are read from
LABEL_BATCH = 5
CST = dt.timezone(dt.timedelta(hours=8))

VIEW = "https://api.bilibili.com/x/web-interface/view?bvid={bvid}"
TAGS = "https://api.bilibili.com/x/tag/archive/tags?bvid={bvid}"
FOLLOWERS = "https://api.bilibili.com/x/relation/stat?vmid={mid}"
REPLIES = "https://api.bilibili.com/x/v2/reply?type=1&oid={aid}&sort=1&pn={page}&ps=20"
MEDAL_WALL = "https://api.live.bilibili.com/xlive/web-ucenter/user/MedalWall?target_id={mid}"

Fetch = Callable[[str], dict[str, Any]]
Ask = Callable[[str], str]


class ReadRefused(RuntimeError):
    """Bilibili refused a read (risk control or a login problem): this run stops asking, the next one retries."""


@dataclass
class Readers:
    """`public` reads anonymously (a visitor cookie); `login` reads as the upload account -- only the comment list and the
    commenters' medal walls need it (anonymous readers see three comments and no medals).  Both are reads."""
    public: Fetch
    login: Fetch
    pause: Callable[[float], None] = time.sleep
    gap_s: float = 1.2
    medal_budget: int = 400
    medal_reads: int = 0


@dataclass
class Summary:
    clips: int = 0
    new: int = 0
    snapshots: Counter[str] = field(default_factory=Counter)
    audience: int = 0
    labelled: int = 0
    problems: list[str] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {"clips": self.clips, "new": self.new, "snapshots": dict(self.snapshots), "audience": self.audience,
                "labelled": self.labelled, "problems": self.problems[:50]}


def load_config(path: Path | None = None) -> dict[str, Any] | None:
    """The channel's research settings (assets/<profile>/clip_performance.json): the owner's Bilibili uid (whose fan medal
    marks her own audience) and the uids of guests the streamer registry lacks.  None when the channel has none."""
    path = path or core_draws.channel.asset(CONFIG_ASSET)
    if not path.exists():
        return None
    doc = json.loads(path.read_text(encoding="utf-8"))
    if (doc.get("schema") != CONFIG_SCHEMA or not isinstance(doc.get("owner_mid"), int)
            or not isinstance(doc.get("known_mids", {}), dict)
            or not all(isinstance(v, int) for v in doc.get("known_mids", {}).values())):
        raise ValueError(f"{path}: schema {CONFIG_SCHEMA}, owner_mid (int), known_mids {{name: uid}}")
    return doc


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def write_json(path: Path, doc: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(doc, stream, ensure_ascii=False, indent=1, sort_keys=True)
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def _srt_text(path: Path) -> str:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return ""
    keep = [line.strip() for line in lines
            if line.strip() and not line.strip().isdigit() and "-->" not in line]
    return "\n".join(keep)


# --- what the lane knows about a published clip -------------------------------------------------------------------

def lane_clips(task: Path) -> dict[str, dict[str, Any]]:
    """Every BV the lane published and verified public, from its clip directories.  A revision is posted over the same
    BV: the latest one gives the packaging, the first one the selection."""
    found: dict[str, list[tuple[str, Path]]] = {}
    for proof in sorted((task / "clips").glob("*/upload/authorized-upload-manifest.json.uploaded.json")):
        doc = _read_json(proof)
        if not isinstance(doc, dict) or doc.get("status") != "VERIFIED_PUBLIC" or not doc.get("bvid"):
            continue
        clip_dir = proof.parent.parent
        online = _read_json(clip_dir / "upload" / "online.json") or {}
        found.setdefault(str(doc["bvid"]), []).append((str(online.get("written_at") or doc.get("verified_at") or ""), clip_dir))
    out: dict[str, dict[str, Any]] = {}
    for bvid, dirs in found.items():
        dirs.sort()
        latest, first = dirs[-1][1], dirs[0][1]
        state = _read_json(task / "state" / f"{first.name}.json") or {}
        latest_state = _read_json(task / "state" / f"{latest.name}.json") or {}
        out[bvid] = {"cid": first.name, "latest_cid": latest.name, "dir": latest, "state": state,
                     "latest_state": latest_state, "first_dir": first}
    return out


def _recall_entry(task: Path, clip: Mapping[str, Any]) -> dict[str, Any] | None:
    """The clip's candidate as the selection saw it: the recall of its recording (it carries the seven dimensions, the
    draws label and the owner mark); the selection's own entry file when the recall is gone."""
    date, source, cid = str(clip.get("recording_date", "")), str(clip.get("source_name", "")), str(clip.get("cid", ""))
    stem = source.removesuffix(".flv")
    candidate = re.sub(r"_\d{8}(_r\d+)?$", "", cid)
    for path in (task / "discover" / date / stem / "recall.json", Path(str(clip.get("selection_file") or "/nonexistent"))):
        doc = _read_json(path) if path.is_file() else None
        for entry in (doc or {}).get("input_pool", []) if isinstance(doc, dict) else []:
            if isinstance(entry, dict) and entry.get("candidate_id") in (candidate, clip.get("candidate_id")):
                return entry
    return None


def _ledger_day(task: Path, date: str) -> dict[str, Any]:
    ledger = _read_json(task / "discover" / "ledger.json") or {}
    day = (ledger.get("days") or {}).get(date)
    return day if isinstance(day, dict) else {}


def _stream_end(day: Mapping[str, Any]) -> float | None:
    """When the day's stream ended: the last recorded segment's last write (the recorder closes it at the end)."""
    times = []
    for segment in day.get("segments") or []:
        if isinstance(segment, dict) and not segment.get("stub"):
            try:
                times.append(float(str(segment.get("mtime"))))
            except ValueError:
                continue
    return max(times) if times else None


def selection_facts(task: Path, lane: Mapping[str, Any]) -> dict[str, Any]:
    clip = (lane["state"].get("clip") or {})
    entry = _recall_entry(task, clip) or {}
    card = entry.get("selection_scorecard") or {}
    session = lane["state"].get("session") or {}
    participants = [names for names in session.get("participants") or [] if isinstance(names, list) and names]
    return {
        "recording_date": clip.get("recording_date"),
        "source_name": clip.get("source_name"),
        "start_s": round(int(clip.get("start_ms", 0)) / 1000, 1),
        "end_s": round(int(clip.get("end_ms", 0)) / 1000, 1),
        "hook": clip.get("hook") or entry.get("hook"),
        "kind": entry.get("kind"),
        "dimensions": card.get("dimensions"),
        "effective_score": card.get("effective_score"),
        "tier": card.get("tier"),
        "recall_draws": entry.get("core_draws"),
        "owner_marked": bool(entry.get("owner_mark")),
        "speaker_mode": session.get("speaker_mode"),
        "guests": [names[0] for names in participants[1:]],
    }


def packaging(lane: Mapping[str, Any] | None, view: Mapping[str, Any], tags: list[str],
              stream_end: float | None) -> dict[str, Any]:
    published = int(view.get("pubdate") or 0)
    at = dt.datetime.fromtimestamp(published, CST) if published else None
    out: dict[str, Any] = {
        "title": view.get("title"), "tags": tags, "tid": view.get("tid"), "tname": view.get("tname") or None,
        "duration_s": view.get("duration"),
        "published_at": at.astimezone(dt.timezone.utc).isoformat() if at else None,
        "publish_hour_cst": at.hour if at else None, "publish_weekday_cst": at.isoweekday() if at else None,
        "upload_gap_hours": round((published - stream_end) / 3600, 1) if published and stream_end else None,
    }
    if lane is not None:
        spec = _read_json(lane["dir"] / "cover" / "spec.json") or {}
        intro = _read_json(lane["dir"] / "burn" / "intro_result.json") or {}
        out["cover_lines"] = [str(line.get("text", "")) for line in spec.get("lines") or [] if isinstance(line, dict)]
        out["intro"] = intro.get("intro_id") if intro.get("status") == "PREPENDED" else None
    return out


# --- reads -------------------------------------------------------------------------------------------------------

def _data(payload: Mapping[str, Any], what: str) -> Any:
    if payload.get("code") != 0:
        raise ReadRefused(f"{what}: code {payload.get('code')} {str(payload.get('message', ''))[:80]}")
    return payload.get("data")


def followers(readers: Readers, mid: int) -> int | None:
    data = _data(readers.public(FOLLOWERS.format(mid=mid)), f"followers {mid}")
    return int(data["follower"]) if isinstance(data, dict) and isinstance(data.get("follower"), int) else None


def snapshot(view: Mapping[str, Any], account_followers: int | None, now: dt.datetime) -> dict[str, Any]:
    stat = view.get("stat") or {}
    age = (now.timestamp() - int(view.get("pubdate") or now.timestamp())) / 3600
    return {"at": now.isoformat(), "age_hours": round(age, 1), "account_followers": account_followers,
            **{key: stat.get(key) for key in ("view", "like", "coin", "favorite", "danmaku", "reply", "share")}}


def due(record: Mapping[str, Any], age_days: float) -> tuple[list[str], list[str]]:
    """The day-k snapshots to take now, and the ones whose day passed before this record existed (missed, never faked)."""
    taken = record.get("snapshots") or {}
    missed = set(record.get("missed") or [])
    take, miss = [], []
    for day in SNAPSHOT_DAYS:
        key = f"d{day}"
        if key in taken or key in missed or age_days < day:
            continue
        (take if age_days <= day + GRACE_DAYS[day] else miss).append(key)
    return take, miss


def commenters(readers: Readers, aid: int, skip_mid: int | None) -> list[int]:
    """The first commenters (top-level comments, Bilibili's default 'hot' order), distinct, without the uploader."""
    seen: list[int] = []
    for page in range(1, REPLY_PAGES + 1):
        data = _data(readers.login(REPLIES.format(aid=aid, page=page)), f"replies {aid} p{page}") or {}
        replies = data.get("replies") or []
        if not replies:
            break
        for reply in replies:
            mid = int((reply.get("member") or {}).get("mid") or 0)
            if mid and mid != skip_mid and mid not in seen:
                seen.append(mid)
        readers.pause(readers.gap_s)
        if len(seen) >= COMMENTERS:
            break
    return seen[:COMMENTERS]


def audience(readers: Readers, aid: int, skip_mid: int | None, owner_mid: int) -> dict[str, Any] | None:
    """Counts of where the commenters' fan medals point; None when this run's medal budget cannot cover the clip (the
    next run takes it whole -- a partial sample is never recorded)."""
    mids = commenters(readers, aid, skip_mid)
    if readers.medal_reads + len(mids) > readers.medal_budget:
        return None
    counts: Counter[str] = Counter()
    other: Counter[str] = Counter()
    for mid in mids:
        data = _data(readers.login(MEDAL_WALL.format(mid=mid)), "medal wall") or {}
        readers.medal_reads += 1
        medals = [m for m in data.get("list") or [] if isinstance(m, dict)]
        infos = [(m.get("medal_info") or {}, str(m.get("target_name") or "")) for m in medals]
        worn = [(info, name) for info, name in infos if info.get("wearing_status") == 1]
        owns_owner = any(info.get("target_id") == owner_mid for info, _ in infos)
        counts["owns_owner_medal"] += owns_owner
        if not medals:
            counts["no_medal"] += 1
        if not worn:
            counts["wears_none"] += 1
        elif worn[0][0].get("target_id") == owner_mid:
            counts["wears_owner"] += 1
        else:
            counts["wears_other"] += 1
            other[worn[0][1] or str(worn[0][0].get("target_id"))] += 1
        readers.pause(readers.gap_s)
    return {"commenters": len(mids), **{k: counts[k] for k in ("wears_owner", "wears_other", "wears_none",
                                                                 "owns_owner_medal", "no_medal")},
            "wears_other_by_streamer": dict(other.most_common())}


# --- the draws, read from the final subtitles --------------------------------------------------------------------

def label_from_subtitles(records: list[dict[str, Any]], texts: Mapping[str, str], ask: Ask,
                         asset: Mapping[str, Any]) -> int:
    """Label, in place, the records whose final-subtitle draws are missing or of an older asset version."""
    todo = [r for r in records if r["bvid"] in texts and texts[r["bvid"]]
            and (r.get("draws_final_subtitles") or {}).get("version") != asset["version"]]
    for start in range(0, len(todo), LABEL_BATCH):
        batch = todo[start:start + LABEL_BATCH]
        entries = [{"candidate_id": r["bvid"], "hook": "", "text": texts[r["bvid"]]} for r in batch]
        labels = core_draws.parse(ask(core_draws.prompt(entries, asset, text_limit=SUBTITLE_LIMIT)),
                                  [r["bvid"] for r in batch], asset)
        for record in batch:
            record["draws_final_subtitles"] = labels[record["bvid"]]
    return len(todo)


# --- one run -----------------------------------------------------------------------------------------------------

def _guest_mid(name: str, config: Mapping[str, Any], registry: Iterable[Mapping[str, Any]]) -> int | None:
    if name in config.get("known_mids", {}):
        return int(config["known_mids"][name])
    for member in registry:
        surfaces = {member.get("canonical"), *member.get("aliases", []), *member.get("official_surfaces", [])}
        if name in surfaces and isinstance(member.get("official_mid"), int):
            return int(member["official_mid"])
    return None


def _create(bvid: str, view: Mapping[str, Any], tags: list[str], info: Mapping[str, Any] | None,
            seed: Mapping[str, Any] | None, task: Path | None, now: dt.datetime) -> dict[str, Any]:
    clip = ((info or {}).get("state") or {}).get("clip") or {}
    day = _ledger_day(task, str(clip.get("recording_date"))) if task is not None and info is not None else {}
    record: dict[str, Any] = {
        "schema": SCHEMA, "bvid": bvid, "aid": view.get("aid"), "created_at": now.isoformat(),
        "source": "lane" if info is not None else "seed", "channel": core_draws.channel.PROFILE_ID,
        "lane_cid": (info or {}).get("cid"), "revision_cid": (info or {}).get("latest_cid"),
        "selection": selection_facts(task, info) if task is not None and info is not None else None,
        "packaging": packaging(info, view, tags, _stream_end(day)), "snapshots": {}, "missed": []}
    if seed is not None:
        record["seed"] = {k: v for k, v in seed.items() if k != "bvid"}
    return record


def run(*, task: Path | None, out_dir: Path, readers: Readers, ask: Ask | None, now: dt.datetime,
        config: Mapping[str, Any], seeds: list[dict[str, Any]] | None = None,
        registry: list[dict[str, Any]] | None = None) -> Summary:
    """Bring every record up to date: create it the first time (lane clips with their selection, seeded older BVs with
    what the public page says), take the snapshots that are due, sample the audience on day 7, label the draws.
    A refused read is reported and skips that clip (or, for the upload account's reads, the rest of the audience
    sampling); the next run retries."""
    summary = Summary()
    lane = lane_clips(task) if task is not None else {}
    seed_by_bvid = {str(s["bvid"]): s for s in seeds or []}
    wanted = sorted(set(lane) | set(seed_by_bvid))
    texts: dict[str, str] = {}
    records: list[dict[str, Any]] = []
    account: dict[int, int | None] = {}
    for bvid in wanted:
        info, path = lane.get(bvid), out_dir / f"{bvid}.json"
        record = _read_json(path) or {}
        try:
            view = _data(readers.public(VIEW.format(bvid=bvid)), f"view {bvid}")
            if not isinstance(view, dict):
                raise ReadRefused(f"view {bvid}: no data")
            if not record:
                tags = [str(t.get("tag_name")) for t in _data(readers.public(TAGS.format(bvid=bvid)), f"tags {bvid}") or []
                        if isinstance(t, dict) and t.get("tag_name")]
                record = _create(bvid, view, tags, info, seed_by_bvid.get(bvid), task, now)
                summary.new += 1
            upload_mid = int((view.get("owner") or {}).get("mid") or 0)
            if upload_mid and upload_mid not in account:
                account[upload_mid] = followers(readers, upload_mid)
        except ReadRefused as exc:
            summary.problems.append(str(exc))
            continue
        age_days = (now.timestamp() - int(view.get("pubdate") or now.timestamp())) / 86400
        take, miss = due(record, age_days)
        current = snapshot(view, account.get(upload_mid), now)
        record["latest"] = current
        for key in take:
            record["snapshots"][key] = current
            summary.snapshots[key] += 1
        record["missed"] = sorted(set(record.get("missed") or []) | set(miss))
        if "audience" not in record and age_days >= AUDIENCE_DAY and readers.medal_reads < readers.medal_budget:
            try:
                sample = audience(readers, int(view["aid"]), upload_mid or None, int(config["owner_mid"]))
                if sample is not None:
                    sample.update(at=now.isoformat(), age_days=round(age_days, 1),
                                  late=age_days > AUDIENCE_DAY + GRACE_DAYS[AUDIENCE_DAY])
                    guest_followers: dict[str, int | None] = {}
                    for name in (record.get("selection") or {}).get("guests") or []:
                        mid = _guest_mid(name, config, registry or [])
                        guest_followers[name] = followers(readers, mid) if mid else None
                    sample["guest_followers"] = guest_followers
                    record["audience"] = sample
                    summary.audience += 1
            except ReadRefused as exc:
                summary.problems.append(f"{bvid}: {exc}")
                readers.medal_budget = readers.medal_reads      # no more upload-account reads this run
        if info is not None:
            texts[bvid] = _srt_text(info["dir"] / "pkg" / "final.srt")
        records.append(record)
        write_json(path, record)
        summary.clips += 1
    asset = core_draws.load()
    if ask is not None and asset is not None:
        try:
            summary.labelled = label_from_subtitles(records, texts, ask, asset)
        except core_draws.DrawsError as exc:
            summary.problems.append(f"draws: {exc}")
        for record in records:
            write_json(out_dir / f"{record['bvid']}.json", record)
    return summary
