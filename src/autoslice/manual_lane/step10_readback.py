"""What is online after step 10, read back and compared with what was meant to go online: "this manuscript is now this, because
of these files".

The reads: Bilibili's public part list (`x/player/pagelist`: one part, its name, its length, its cid), the media host's ffprobe for
the length of the finished video, and the publication registry row of the manuscript (title_history, through the root
boundary).  The signed `x/web-interface/wbi/view` read is not made: the part list already carries everything this check
needs (part count, part name, length, cid), and the upload tool's own live verification has read the page, the cover and the
tags.  A mismatch is reported, never repaired: what is online was changed by the tool and is not rolled back.

Checks: one part; its name equals the title letter for letter; its length is the finished video's within one second; its cid is the
new one after a video replacement (the completion receipt's `new_cid`, different from the one before), the unchanged one after
a cover replacement, the uploaded one after a first publication; the registry holds the published row of the BV; the package has
become one that was used online (package_guards.protected), so it cannot be overwritten."""

from __future__ import annotations

import datetime as dt
import json
import urllib.request
from collections.abc import Callable
from typing import Any

from . import package_guards, title_history
from .runner import Ctx
from .step10_check import read_json
from .step10_commands import Job, duration
from .step10_route import COVER, FIRST, VIDEO
from .step_io import _run_remote

PAGELIST = "https://api.bilibili.com/x/player/pagelist?bvid={bvid}"
USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
LENGTH_TOLERANCE_S = 1.0


def _fetch(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=30) as response:
        return bytes(response.read())


FETCH: Callable[[str], bytes] = _fetch      # the one network read; a test replaces it


def _cids(job: Job) -> tuple[int | None, int | None]:
    """(the cid that must be online now, the cid that was online before) from the tool's own receipts."""
    if job.route == FIRST:
        return int(read_json(job.uploaded)["cid"]), None
    plan, done = read_json(job.plan), read_json(job.completed)
    if job.route == VIDEO:
        return int(done["new_cid"]), int(plan["before"]["public"]["cid"])
    return int(done["unchanged_cid"]), int(plan["unchanged_cid"])


def _length(ctx: Ctx, job: Job) -> float:
    result = _run_remote(ctx, "s10-duration", duration(job.package_file(".mp4")), timeout=120)
    if result.rc:
        raise RuntimeError(f"ffprobe rc={result.rc}: {result.stderr[-200:]}")
    return float(result.stdout.strip())


def verify_online(ctx: Ctx, job: Job, manuscript: str) -> tuple[dict[str, Any], list[str]]:
    """(the record of what was read, the mismatches); the record is written into online.json whatever the mismatches are."""
    problems: list[str] = []
    record: dict[str, Any] = {"read_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "bvid": job.bvid}
    try:
        cid_now, cid_before = _cids(job)
        seconds = _length(ctx, job)
        payload = json.loads(FETCH(PAGELIST.format(bvid=job.bvid)).decode("utf-8"))
        parts = payload.get("data") if isinstance(payload, dict) and payload.get("code") == 0 else None
        if not isinstance(parts, list):
            raise ValueError(f"part list answered code {payload.get('code') if isinstance(payload, dict) else payload!r}")
        row = title_history.published_row(ctx, manuscript)
    except (OSError, ValueError, KeyError, TypeError, RuntimeError) as exc:
        return record, [f"回读读不到：{type(exc).__name__}: {exc!s:.300}"]
    record.update(video_seconds=seconds, cid_expected=cid_now, cid_before=cid_before, registry_row=row,
                  parts=[{k: p.get(k) for k in ("cid", "page", "part", "duration")} for p in parts if isinstance(p, dict)])
    if len(parts) != 1:
        problems.append(f"分 P 数是 {len(parts)}，应为 1")
    else:
        part = parts[0]
        if part.get("part") != job.title:
            problems.append(f"分 P 名与标题不一致：分 P 名「{part.get('part')}」，标题「{job.title}」")
        if not isinstance(part.get("duration"), (int, float)) or abs(part["duration"] - seconds) > LENGTH_TOLERANCE_S:
            problems.append(f"时长不符：线上 {part.get('duration')} 秒，成片 {seconds:.3f} 秒（容差 {LENGTH_TOLERANCE_S:g} 秒）")
        if part.get("cid") != cid_now:
            problems.append(f"线上分 P 的 cid 是 {part.get('cid')}，回执给的是 {cid_now}")
        if job.route == VIDEO and cid_now == cid_before:
            problems.append(f"换视频后 cid 没有变（仍是 {cid_before}）")
        if job.route == COVER and cid_now != cid_before:
            problems.append(f"只换封面，cid 却变了：{cid_before} → {cid_now}")
    if row is None or row["status"] != "published" or row["bvid"] != job.bvid:
        problems.append(f"出版登记里该稿件的已发布行不是 {job.bvid}：{row}")
    record["package_protected"] = package_guards.protected(ctx, job.package) is not None
    if not record["package_protected"]:
        problems.append("成片包还没有成为“线上用过的包”（出版登记/回执链没有认到它），仍可被覆盖")
    return record, problems
