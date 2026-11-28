#!/usr/bin/env python3
"""The audio of the guests' own public recordings of one broadcast day, for step 4's arrival evidence
(scripts/arrival_speakers.py: who speaks, from the order the same words reach two streams).

usage: guest_recording_audio.py --date YYYY-MM-DD --out-dir DIR --guest NAME [--guest NAME ...]

For each guest without audio in DIR/index.json: one public search of 「<name> 录播」, newest first; a video counts when
it was published within WINDOW_H hours of the broadcast day's start (04:00 Beijing, U429), lasts at least MIN_MINUTES,
its title or uploader carries the guest's name (the name without its Latin tail counts: 礼墨Sumi's recordings are
titled 【礼墨】), and a date written in its title is the broadcast day's (【2026.11.24-19:01】 is not 11-23's).  At most MAX_VIDEOS videos per guest; of each, the audio-only track of every part that long is saved
as DIR/<bvid>-<cid>.m4s (never the picture).  Whether a file really is the same session is not decided here: the
arrival script finds the clip in it or reports NOT_FOUND.  A guest searched without result is searched again after
RETRY_MINUTES (a recording group uploads hours after the stream).  Prints one line: GUEST_AUDIO <json of the index>.
Public endpoints only, no login; the same transport and limits as the external duplicate check.
"""
from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.autoslice.external_duplicate_check import PublicBilibili, broadcast_day_start
from src.autoslice.timely_term_crawler import BILIBILI_USER_AGENT, WHOLE_RANGE, BoundedHttpClient, CrawlError

WINDOW_H = 72
MIN_MINUTES = 30
MAX_VIDEOS = 2
RETRY_MINUTES = 60
MAX_BYTES = 900 * 1024 * 1024


def short_name(name: str) -> str:
    """A streamer's name without its Latin tail (礼墨Sumi -> 礼墨, 安晚awa -> 安晚); the name itself when nothing is left."""
    return re.sub(r"[A-Za-z0-9_ ]+$", "", name).strip() or name


def minutes(value: object) -> float:
    """A search row's duration (「274:25」 minutes:seconds, or h:m:s) in minutes."""
    parts = [float(p) for p in str(value or "0").split(":") if p.strip().replace(".", "", 1).isdigit()]
    seconds = 0.0
    for p in parts:
        seconds = seconds * 60 + p
    return seconds / 60


def title_dates(title: str) -> set[str]:
    """The dates a title writes out, as YYYYMMDD (2026.11.23,, 20261123)."""
    return {f"{y}{int(m):02d}{int(d):02d}" for y, m, d in re.findall(r"(20\d\d)[.\-/年]?(\d{1,2})[.\-/月]?(\d{1,2})", title)}


def wanted(row: dict[str, object], names: tuple[str, ...], day_start: float, day: str = "") -> bool:
    title = re.sub(r"<[^>]+>", "", str(row.get("title") or ""))
    where = title + " " + str(row.get("author") or "")
    published = float(row.get("pubdate") or 0)      # type: ignore[arg-type]
    dates = title_dates(title)
    return (day_start <= published <= day_start + WINDOW_H * 3600 and minutes(row.get("duration")) >= MIN_MINUTES
            and any(n and n in where for n in names) and (not dates or not day or day in dates))


def fetch(api: PublicBilibili, now: dt.datetime, bvid: str, out_dir: Path) -> list[dict[str, object]]:
    files: list[dict[str, object]] = []
    for page in api.pages(bvid):
        if not isinstance(page, dict) or float(page.get("duration") or 0) < MIN_MINUTES * 60:      # type: ignore[arg-type]
            continue
        cid = int(page["cid"])      # type: ignore[arg-type]
        path = out_dir / f"{bvid}-{cid}.m4s"
        if not path.is_file():
            client = BoundedHttpClient(allowed_hosts=frozenset({"bilivideo.com", "bilivideo.cn", "bilibili.com"}),
                                       max_requests=1, max_response_bytes=MAX_BYTES, timeout_seconds=900, now=now)
            body = client.fetch(api.audio_url(bvid, cid), headers={
                "User-Agent": BILIBILI_USER_AGENT, "Range": WHOLE_RANGE,
                "Referer": f"https://www.bilibili.com/video/{bvid}/"}).body
            tmp = path.with_suffix(".part")
            tmp.write_bytes(body)
            tmp.rename(path)
        files.append({"bvid": bvid, "cid": cid, "part": page.get("part"), "duration_s": page.get("duration"), "path": str(path)})
    return files


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", required=True)
    ap.add_argument("--out-dir", type=Path, required=True)
    ap.add_argument("--guest", action="append", required=True)
    args = ap.parse_args()
    args.out_dir.mkdir(parents=True, exist_ok=True)
    now = dt.datetime.now(dt.timezone.utc)
    day_start = broadcast_day_start(dt.date.fromisoformat(args.date))
    index_path = args.out_dir / "index.json"
    with open(args.out_dir / "index.lock", "w", encoding="utf-8") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        index = json.loads(index_path.read_text(encoding="utf-8")) if index_path.is_file() else {"date": args.date, "guests": {}}
        api = PublicBilibili(BoundedHttpClient(allowed_hosts=frozenset({"bilibili.com", "hdslb.com"}), max_requests=40,
                                               timeout_seconds=15, now=now), now=now)
        for name in args.guest:
            rec = index["guests"].get(name) or {}
            searched = dt.datetime.fromisoformat(rec["searched_at"]) if rec.get("searched_at") else None
            if rec.get("files") or (searched and (now - searched).total_seconds() < RETRY_MINUTES * 60):
                continue
            rec = {"searched_at": now.isoformat(), "files": [], "videos": []}
            try:
                names = (name, short_name(name))
                rows = [r for r in api.search(f"{short_name(name)} 录播") if wanted(r, names, day_start, args.date.replace("-", ""))]
                rows.sort(key=lambda r: ("录播" not in str(r.get("title")), -minutes(r.get("duration"))))
                for row in rows[:MAX_VIDEOS]:
                    bvid = str(row.get("bvid"))
                    rec["videos"].append({"bvid": bvid, "title": re.sub(r"<[^>]+>", "", str(row.get("title"))),
                                          "uploader": row.get("author")})
                    rec["files"] += fetch(api, now, bvid, args.out_dir)
            except (CrawlError, OSError, ValueError, KeyError) as exc:
                rec["error"] = f"{type(exc).__name__}: {exc}"[:300]
            index["guests"][name] = rec
            index_path.write_text(json.dumps(index, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print("GUEST_AUDIO " + json.dumps(index, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
