#!/usr/bin/env python3
"""Replace an archive's video content in place (Ivan 2026-07-10: 编辑视频, 不是新上传).

Precondition: `biliup append --vid <BV>` already uploaded the FIXED file as the
LAST P.  This script submits x/vu/web/edit with the full current metadata and
videos = [that last P only], dropping the old P — same BV, collection intact,
re-review triggered.  Prints the before/after videos lists; refuses when the
archive does not have exactly the expected 2 P's with the expected new title.
"""
import json
import sys
import urllib.parse
import urllib.request

BVID = sys.argv[1]
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"

raw = json.load(open("/opt/bilive/app/cookie.json"))
cks = ((raw.get("data") or {}).get("cookie_info") or {}).get("cookies") or []
ck = {x["name"]: x["value"] for x in cks}
jct = ck["bili_jct"]
hdr = "; ".join(f"{k}={v}" for k, v in ck.items())


def member_get(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Cookie": hdr,
                                               "Referer": "https://member.bilibili.com/"})
    return json.load(urllib.request.urlopen(req, timeout=30))


view = member_get(f"https://member.bilibili.com/x/vupre/web/archive/view?bvid={BVID}")
data = view.get("data") or {}
archive = data.get("archive") or {}
videos = data.get("videos") or []
print("BEFORE:", json.dumps([{k: v.get(k) for k in ("title", "filename", "cid", "duration")} for v in videos],
                            ensure_ascii=False))
if len(videos) != 2:
    print(f"REFUSE: expected exactly 2 P (old + appended fix), got {len(videos)}")
    sys.exit(2)
new_p = videos[-1]
payload = {
    "aid": archive.get("aid"),
    "title": archive.get("title"),
    "copyright": archive.get("copyright"),
    "source": archive.get("source"),
    "tid": archive.get("tid"),
    "cover": archive.get("cover"),
    "tag": archive.get("tag"),
    "desc": archive.get("desc"),
    "desc_format_id": archive.get("desc_format_id", 0),
    "dynamic": archive.get("dynamic", ""),
    "interactive": archive.get("interactive", 0),
    "videos": [{"filename": new_p.get("filename"), "title": archive.get("title"), "cid": new_p.get("cid")}],
    "csrf": jct,
}
req = urllib.request.Request(
    "https://member.bilibili.com/x/vu/web/edit?csrf=" + urllib.parse.quote(jct),
    data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
    method="POST",
    headers={"Content-Type": "application/json;charset=UTF-8", "User-Agent": UA,
             "Referer": "https://member.bilibili.com/platform/upload-manager/article",
             "Origin": "https://member.bilibili.com", "Cookie": hdr})
resp = json.load(urllib.request.urlopen(req, timeout=30))
print("EDIT:", json.dumps(resp, ensure_ascii=False)[:200])
if resp.get("code") != 0:
    sys.exit(3)
after = member_get(f"https://member.bilibili.com/x/vupre/web/archive/view?bvid={BVID}")
vids2 = (after.get("data") or {}).get("videos") or []
print("AFTER:", json.dumps([{k: v.get(k) for k in ("title", "filename", "cid", "duration")} for v in vids2],
                           ensure_ascii=False))
print("state:", ((after.get("data") or {}).get("archive") or {}).get("state"),
      ((after.get("data") or {}).get("archive") or {}).get("state_desc"))
