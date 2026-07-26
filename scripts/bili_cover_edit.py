#!/usr/bin/env python3
"""Authorized cover-only edit for one published Bilibili archive.

Parameterized replacement for the retired one-off scripts (cover_only_edit.py,
bili_replace_covers.py hardcoded target lists). Edits change ONLY the cover:
title/desc/tag/tid/videos are read back from the archive and resubmitted
verbatim, so an edit can never silently rewrite公开面文字. Editing an archive
does not consume the daily upload quota (established 2026-07-14 playbook).

Runs on the host that owns the member cookie (free). Never prints cookies.
Writes a hash-bound receipt: old/new cover URL, local PNG sha256, and the
public read-back verification.

Example:
  python3 scripts/bili_cover_edit.py --bvid BV1E93L6rErV \
      --cover new-cover.png --receipt 424.cover-edit.receipt.json \
      --expect-title 李豆沙
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) Chrome/122"


def _load_cookie(path: Path) -> tuple[str, str]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    cookies = payload["data"]["cookie_info"]["cookies"]
    header = "; ".join(f"{c['name']}={c['value']}" for c in cookies)
    csrf = next(c["value"] for c in cookies if c["name"] == "bili_jct")
    return header, csrf


def _api(url: str, cookie: str, body=None, *, as_json=True):
    headers = {
        "User-Agent": UA,
        "Referer": "https://member.bilibili.com/",
        "Origin": "https://member.bilibili.com",
        "Cookie": cookie,
    }
    data = None
    if body is not None:
        if as_json:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json;charset=UTF-8"
        else:
            data = urllib.parse.urlencode(body).encode("utf-8")
    request = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.load(response)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bvid", required=True)
    parser.add_argument("--cover", required=True, help="new cover PNG path")
    parser.add_argument(
        "--cookie-json", default="/opt/bilive/app/cookie.json"
    )
    parser.add_argument("--receipt", required=True, help="receipt JSON output")
    parser.add_argument(
        "--expect-title",
        default="",
        help="refuse unless the live title contains this substring",
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    cover_path = Path(args.cover)
    cover_bytes = cover_path.read_bytes()
    cover_sha = hashlib.sha256(cover_bytes).hexdigest()
    cookie, csrf = _load_cookie(Path(args.cookie_json))

    view = _api(
        f"https://member.bilibili.com/x/vupre/web/archive/view?bvid={args.bvid}",
        cookie,
    )
    if view.get("code") != 0:
        print(f"REFUSE: archive view {view.get('code')} {view.get('message')}",
              file=sys.stderr)
        return 2
    archive = view["data"]["archive"]
    videos = view["data"]["videos"]
    if not videos:
        print("REFUSE: archive has no videos", file=sys.stderr)
        return 2
    if args.expect_title and args.expect_title not in str(archive.get("title")):
        print(
            f"REFUSE: live title {archive.get('title')!r} does not contain "
            f"{args.expect_title!r}",
            file=sys.stderr,
        )
        return 2
    old_cover = archive.get("cover")
    if args.dry_run:
        print(json.dumps({
            "status": "DRY_RUN",
            "bvid": args.bvid,
            "title": archive.get("title"),
            "old_cover": old_cover,
            "new_cover_sha256": "sha256:" + cover_sha,
        }, ensure_ascii=False))
        return 0

    upload = _api(
        f"https://member.bilibili.com/x/vu/web/cover/up?csrf={csrf}",
        cookie,
        {"cover": "data:image/png;base64,"
                  + base64.b64encode(cover_bytes).decode("ascii")},
        as_json=False,
    )
    if upload.get("code") != 0:
        print(f"REFUSE: cover up {upload.get('code')} {upload.get('message')}",
              file=sys.stderr)
        return 2
    new_cover_url = upload["data"]["url"]
    body = {
        "aid": archive["aid"],
        "title": archive["title"],
        "copyright": archive.get("copyright", 2),
        "source": archive.get("source", ""),
        "tid": archive.get("tid", 21),
        "cover": new_cover_url,
        "tag": archive.get("tag", ""),
        "desc": archive.get("desc", ""),
        "desc_format_id": archive.get("desc_format_id", 0),
        "dynamic": archive.get("dynamic", ""),
        "videos": [
            {
                "filename": part["filename"],
                "title": part.get("title") or "P1",
                "cid": part.get("cid"),
            }
            for part in videos
        ],
        "csrf": csrf,
    }
    edit = _api(
        f"https://member.bilibili.com/x/vu/web/edit?csrf={csrf}", cookie, body
    )
    if edit.get("code") != 0:
        print(f"REFUSE: edit {edit.get('code')} {edit.get('message')}",
              file=sys.stderr)
        return 2

    verified_cover = None
    for _ in range(6):
        time.sleep(5)
        check = _api(
            f"https://member.bilibili.com/x/vupre/web/archive/view?bvid={args.bvid}",
            cookie,
        )
        if check.get("code") == 0:
            verified_cover = check["data"]["archive"].get("cover")
            if verified_cover and verified_cover != old_cover:
                break
    receipt = {
        "schema_version": "bili-cover-edit-receipt.v1",
        "edited_at": datetime.now(timezone.utc).isoformat(),
        "bvid": args.bvid,
        "aid": archive["aid"],
        "title": archive["title"],
        "old_cover_url": old_cover,
        "uploaded_cover_url": new_cover_url,
        "readback_cover_url": verified_cover,
        "local_cover_path": str(cover_path.resolve()),
        "local_cover_sha256": "sha256:" + cover_sha,
        "edit_code": edit.get("code"),
        "status": (
            "VERIFIED_EDITED"
            if verified_cover and verified_cover != old_cover
            else "EDIT_SUBMITTED_READBACK_PENDING"
        ),
    }
    Path(args.receipt).write_text(
        json.dumps(receipt, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps({k: receipt[k] for k in ("status", "bvid", "readback_cover_url")},
                     ensure_ascii=False))
    return 0 if receipt["status"] == "VERIFIED_EDITED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
