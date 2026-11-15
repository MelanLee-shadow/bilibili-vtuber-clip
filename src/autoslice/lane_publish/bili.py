"""Everything that talks to Bilibili: biliup for the media, the creator-centre calls (bilibili_member_api) for edits and the
collection, and the public pages for reading back.  Three seams a test replaces: FETCH (one public GET), RUN (one biliup
process), SESSION (the creator-centre session)."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

from src.autoslice.bilibili_member_api import QUOTA_FREQUENCY_CODE, BiliSession

from .common import Config, Publish, Refused, REFUSED

VIEW = "https://api.bilibili.com/x/web-interface/view?bvid={bvid}"
TAGS = "https://api.bilibili.com/x/tag/archive/tags?bvid={bvid}"
USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
BVID = re.compile(r'"?bvid"?: String\("(BV[0-9A-Za-z]{10})"\)')     # biliup's debug print of the submit answer


def _fetch(url: str) -> dict[str, Any]:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Referer": "https://www.bilibili.com/"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def _run(argv: list[str], *, cwd: Path, log: Path, timeout: float) -> int:
    with log.open("ab") as out:
        return subprocess.run(argv, cwd=str(cwd), stdout=out, stderr=subprocess.STDOUT, timeout=timeout, check=False).returncode


FETCH: Callable[[str], dict[str, Any]] = _fetch
RUN: Callable[..., int] = _run


def session(config: Config) -> BiliSession:
    return BiliSession(cookie_path=config.cookies, biliup_cookie_path=config.cookies, biliup_bin=config.biliup_bin)


SESSION: Callable[[Config], BiliSession] = session


def public_view(bvid: str) -> dict[str, Any] | None:
    """The public page's data (title, aid, cid, pages, ...), or None while it is not public (under review, or gone)."""
    payload = FETCH(VIEW.format(bvid=bvid))
    return payload.get("data") if payload.get("code") == 0 and isinstance(payload.get("data"), dict) else None


def public_tags(bvid: str) -> list[str]:
    payload = FETCH(TAGS.format(bvid=bvid))
    return [str(t.get("tag_name") or "") for t in payload.get("data") or [] if isinstance(t, dict) and t.get("tag_name")]


def _part_name_problem(title: str) -> str | None:
    """biliup names the single part after the file it is given, so the title has to be a file name."""
    if "/" in title or title.startswith(("-", ".")) or any(ord(c) < 32 for c in title):
        return "标题里有 / 、以 - 或 . 开头、或有控制字符"
    if len((title + ".mp4").encode("utf-8")) > 255:
        return "标题加 .mp4 超过 255 字节"
    return None


def upload(config: Config, *, video: Path, cover: Path, title: str, tags: list[str], desc: str, log: Path,
           timeout: float = 1700) -> tuple[int, str | None, bool]:
    """(biliup's return code, the BV it was given, whether the account's submission quota refused it).  The part is a hard link
    `<title>.mp4` to the sealed video beside it (never a copy), removed afterwards."""
    if problem := _part_name_problem(title):
        raise Refused(REFUSED, f"标题当不了分 P 文件名：{problem}")
    policy = Publish.load()
    holder = Path(tempfile.mkdtemp(prefix=".upload-", dir=video.parent))
    try:
        part = holder / f"{title}.mp4"
        part.hardlink_to(video)
        argv = [str(config.biliup_bin), "-u", config.cookies.name, "upload", str(part), "--cover", str(cover),
                "--copyright", str(policy.copyright), "--source", policy.source, "--tid", str(policy.tid),
                "--title", title, "--tag", ",".join(tags), "--desc", desc, "--submit", "app"]
        rc = RUN(argv, cwd=config.cookies.parent, log=log, timeout=timeout)
    finally:
        shutil.rmtree(holder, ignore_errors=True)
    words = log.read_text(encoding="utf-8", errors="replace") if log.is_file() else ""
    found = BVID.search(words)
    return rc, found.group(1) if found else None, str(QUOTA_FREQUENCY_CODE) in words
