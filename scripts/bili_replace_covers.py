#!/usr/bin/env python3
"""Replace Bilibili archive covers (cover-only edit) for the redesigned Li Dousha
covers. Runs ON the free host (needs the login cookie). Two phases:
  inspect  = list ALL account archives + upload the new covers to bfs + read each
             target archive's current metadata. NO edits. Saves state json.
  apply    = for each target: full-resubmit edit changing ONLY the cover, verify.
Never prints cookie / csrf / token.
"""
import base64
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

COOKIE_JSON = "/opt/bilive/app/cookie.json"
COVER_DIR = Path("/tmp/covers24")
STATE = Path("/tmp/cover_replace_state24.json")
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0 Safari/537.36"

# all 24 Li Dousha slices (BV-named PNGs already scp'd to COVER_DIR)
TARGETS = [
    "BV1WZMA6zEzE", "BV1WZMA6zE2H", "BV1qxMc6XEM9", "BV1CWMT6EE6c",
    "BV1tdMT64E47", "BV1yWMT6EEHF", "BV1yWMT6EEWD", "BV1qVjy6FEqH",
    "BV1ceMF6bESE", "BV1Z8746EE74", "BV1Wd746hE1N", "BV1sd746hEMm",
    "BV1gW7t6GERW", "BV13W7t6GEjg", "BV1AN7u6kEHB", "BV1qVjy6FExr",
    "BV12rjq6oEWb", "BV1CSjq6HEFW", "BV1mBjq6fEkt", "BV1aTjw6PEua",
    "BV1aTjw6PEwp", "BV19Pjw6KEpM", "BV1YPjw6KEGZ", "BV1k1jc6EEnS",
]


def load_cookie():
    d = json.load(open(COOKIE_JSON))
    cookies = d["data"]["cookie_info"]["cookies"]
    jar = "; ".join(f'{c["name"]}={c["value"]}' for c in cookies)
    csrf = next(c["value"] for c in cookies if c["name"] == "bili_jct")
    return jar, csrf


def req(url, jar, *, data=None, method="GET", as_json=False):
    headers = {"Cookie": jar, "User-Agent": UA, "Referer": "https://member.bilibili.com/"}
    body = None
    if data is not None:
        if as_json:
            body = json.dumps(data, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        else:
            body = urllib.parse.urlencode(data).encode("utf-8")
            headers["Content-Type"] = "application/x-www-form-urlencoded"
    r = urllib.request.Request(url, data=body, method=method, headers=headers)
    with urllib.request.urlopen(r, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def list_all_archives(jar):
    out = []
    pn = 1
    while True:
        d = req(f"https://member.bilibili.com/x/web/archives?pn={pn}&ps=30&status=is_pubing,pubed,not_pubed", jar)
        if d.get("code") != 0:
            print("  archives list code", d.get("code"), d.get("message"))
            break
        data = d.get("data") or {}
        audits = data.get("arc_audits") or []
        for a in audits:
            arc = a.get("Archive") or a.get("archive") or {}
            out.append({"bvid": arc.get("bvid"), "aid": arc.get("aid"), "title": arc.get("title"),
                        "cover": arc.get("cover"), "state": arc.get("state"), "ptime": arc.get("ptime")})
        page = data.get("page") or {}
        total = page.get("count") or page.get("total") or 0
        if pn * 30 >= total or not audits:
            break
        pn += 1
        time.sleep(0.5)
    return out


def upload_cover(png: Path, jar, csrf):
    b64 = "data:image/png;base64," + base64.b64encode(png.read_bytes()).decode()
    d = req(f"https://member.bilibili.com/x/vu/web/cover/up?csrf={csrf}", jar,
            data={"cover": b64, "csrf": csrf}, method="POST")
    if d.get("code") != 0:
        raise SystemExit(f"cover.up failed for {png.name}: {d.get('code')} {d.get('message')}")
    return (d["data"]["url"]).strip()


def archive_view(bvid, jar):
    return req(f"https://member.bilibili.com/x/vupre/web/archive/view?bvid={bvid}", jar)


def public_view(bvid):
    r = urllib.request.Request(f"https://api.bilibili.com/x/web-interface/view?bvid={bvid}",
                               headers={"User-Agent": UA})
    with urllib.request.urlopen(r, timeout=30) as resp:
        return json.loads(resp.read().decode())


def do_inspect():
    jar, csrf = load_cookie()
    print("=== ALL ACCOUNT ARCHIVES (since account start) ===")
    allarc = list_all_archives(jar)
    for a in allarc:
        tgt = " <TARGET>" if a["bvid"] in TARGETS else ""
        print(f"  {a['bvid']} state={a['state']} | {a['title']}{tgt}")
    print(f"  total archives: {len(allarc)}")
    extras = [a["bvid"] for a in allarc if a["bvid"] not in TARGETS and (a["state"] or 0) >= 0]
    print(f"  NOT in current 8 targets: {extras}")

    state = {"uploaded_url": {}, "archive": {}, "all_archives": allarc}
    print("\n=== UPLOAD NEW COVERS + READ CURRENT METADATA (no edits) ===")
    for bvid in TARGETS:
        png = COVER_DIR / f"{bvid}.png"
        if not png.is_file():
            print(f"  {bvid}: MISSING cover file")
            continue
        url = upload_cover(png, jar, csrf)
        state["uploaded_url"][bvid] = url
        v = archive_view(bvid, jar)
        arc = (v.get("data") or {}).get("archive") or {}
        vids = (v.get("data") or {}).get("videos") or []
        state["archive"][bvid] = {"archive": arc, "videos": vids}
        print(f"  {bvid}: uploaded={url}")
        print(f"      title={arc.get('title')!r} tid={arc.get('tid')} copyright={arc.get('copyright')} cur_cover={arc.get('cover')}")
        print(f"      videos={[{'cid':x.get('cid'),'filename':x.get('filename'),'title':x.get('title')} for x in vids]}")
        print(f"      archive_keys={sorted(arc.keys())}")
    STATE.write_text(json.dumps(state, ensure_ascii=False, indent=2))
    print(f"\nsaved state -> {STATE}")


def do_apply(only=None):
    jar, csrf = load_cookie()
    state = json.loads(STATE.read_text())
    targets = only or TARGETS
    for bvid in targets:
        new_cover = state["uploaded_url"].get(bvid)
        rec = state["archive"].get(bvid) or {}
        arc = rec.get("archive") or {}
        vids = rec.get("videos") or []
        if not new_cover or not arc:
            print(f"  {bvid}: SKIP (missing url/archive)")
            continue
        old_title, old_tag = arc.get("title"), arc.get("tag")
        tag = ",".join(old_tag) if isinstance(old_tag, list) else (old_tag or "")
        # Curated cover-only edit: preserve exactly what biliup set; change ONLY cover.
        payload = {
            "aid": arc.get("aid"),
            "title": arc.get("title"),
            "desc": arc.get("desc", "") or "",
            "desc_format_id": arc.get("desc_format_id", 0) or 0,
            "tag": tag,
            "tid": arc.get("tid"),
            "copyright": arc.get("copyright", 2) or 2,
            "source": arc.get("source", "") or "",
            "cover": new_cover,
            "dynamic": arc.get("dynamic", "") or "",
            "no_reprint": arc.get("no_reprint", 0) or 0,
            "videos": [{"cid": v.get("cid"), "filename": v.get("filename"),
                        "title": v.get("title", ""), "desc": v.get("desc", "")} for v in vids],
            "csrf": csrf,
        }
        try:
            d = req(f"https://member.bilibili.com/x/vu/web/edit?csrf={csrf}", jar,
                    data=payload, method="POST", as_json=True)
        except urllib.error.HTTPError as e:
            print(f"  {bvid}: EDIT HTTP {e.code}: {e.read()[:200]}")
            continue
        code = d.get("code")
        time.sleep(1.5)
        # Verify MEMBER-side (authoritative + immediate); the public API pic is CDN-cached.
        v2 = archive_view(bvid, jar)
        arc2 = (v2.get("data") or {}).get("archive") or {}
        mcover = arc2.get("cover") or ""
        new_hash = new_cover.split("/bfs/")[-1].split(".")[0]
        cover_ok = new_hash in mcover
        title_ok = arc2.get("title") == old_title
        tag_ok = (",".join(arc2["tag"]) if isinstance(arc2.get("tag"), list) else (arc2.get("tag") or "")) == tag
        print(f"  {bvid}: edit_code={code} | COVER_UPDATED={cover_ok} TITLE_INTACT={title_ok} TAG_INTACT={tag_ok}")
        print(f"      member_cover={mcover}")


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "inspect"
    if mode == "inspect":
        do_inspect()
    else:
        # mode == "apply" [bvid ...]  -> apply to specific BVs, or all if none given
        do_apply(sys.argv[2:] or None)
