#!/usr/bin/env python3
"""Update Bilibili archive tags (tag-only edit), plan-driven. Runs ON free.

Modeled on the earlier proven cover-only edit lane (member edits on this
account): member archive/view read-back -> full resubmit edit changing ONLY the
tag field -> member-side verify. Never prints cookie / csrf.

plan.json: {"BV...": {"tags": "a,b,c", "title_expect": "【李豆沙】..."}}
  - title_expect is a mis-mapping guard: the live archive title must start with
    it (titles can gain suffixes via edits, so prefix-match).
  - tags is the COMPLETE replacement line (base tags included), comma-joined.

modes:
  inspect  --plan plan.json --state state.json
      read-only: list account archives, resolve every plan bvid, record current
      metadata. Refuses nothing; reports MISSING/TITLE_MISMATCH for humans.
  apply    --plan plan.json --state state.json --results results.json [--only BV ...]
      per bvid: fresh view -> guards -> skip if tag set already equals target ->
      tag-only edit -> verify tag updated AND title/cover/desc intact.
      If the edit is rejected and the message looks like a tag-count limit, retry
      once with the first 10 tags (doubles as the 10-vs-12 cap probe; result is
      recorded per bvid as tag_cap_probe).

Tag-count policy: plans may carry up to 12 tags; the retry path establishes the
real cap empirically and records it instead of guessing.
"""
import argparse
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

COOKIE_JSON = "/opt/bilive/app/cookie.json"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0 Safari/537.36"
MAX_TAG_CHARS = 20


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
    out, pn = [], 1
    while True:
        d = req(
            f"https://member.bilibili.com/x/web/archives?pn={pn}&ps=30&status=is_pubing,pubed,not_pubed",
            jar,
        )
        if d.get("code") != 0:
            print("  archives list code", d.get("code"), d.get("message"))
            break
        data = d.get("data") or {}
        audits = data.get("arc_audits") or []
        for a in audits:
            arc = a.get("Archive") or a.get("archive") or {}
            out.append(
                {
                    "bvid": arc.get("bvid"),
                    "aid": arc.get("aid"),
                    "title": arc.get("title"),
                    "tag": arc.get("tag"),
                    "state": arc.get("state"),
                    "ptime": arc.get("ptime"),
                }
            )
        page = data.get("page") or {}
        total = page.get("count") or page.get("total") or 0
        if pn * 30 >= total or not audits:
            break
        pn += 1
        time.sleep(0.5)
    return out


def archive_view(bvid, jar):
    return req(f"https://member.bilibili.com/x/vupre/web/archive/view?bvid={bvid}", jar)


def norm_tag_line(tag) -> str:
    if isinstance(tag, list):
        return ",".join(t.strip() for t in tag if str(t).strip())
    return ",".join(t.strip() for t in str(tag or "").split(",") if t.strip())


def tag_set(tag) -> tuple:
    return tuple(sorted(norm_tag_line(tag).split(","))) if norm_tag_line(tag) else ()


def validate_plan_tags(line: str) -> list[str]:
    problems = []
    tags = [t.strip() for t in line.split(",") if t.strip()]
    if not tags:
        problems.append("empty tag line")
    if len(tags) != len({t.casefold() for t in tags}):
        problems.append("duplicate tags")
    if len(tags) > 12:
        problems.append(f"{len(tags)} tags > 12 (hard refuse)")
    for t in tags:
        if len(t) > MAX_TAG_CHARS:
            problems.append(f"tag too long: {t!r}")
        if any(ch in t for ch in ",，\n\t"):
            problems.append(f"tag has separator char: {t!r}")
    return problems


def looks_like_tag_limit_error(code, message) -> bool:
    msg = str(message or "")
    return code != 0 and ("标签" in msg or "tag" in msg.lower()) and any(w in msg for w in ("多", "超", "限", "个数", "数量"))


def do_inspect(plan: dict, state_path: Path):
    jar, _csrf = load_cookie()
    allarc = list_all_archives(jar)
    by_bvid = {a["bvid"]: a for a in allarc}
    print(f"account archives: {len(allarc)}")
    state = {"all_archives": allarc, "targets": {}}
    problems = 0
    for bvid, want in plan.items():
        bad = validate_plan_tags(want["tags"])
        arc_l = by_bvid.get(bvid)
        if not arc_l:
            print(f"  {bvid}: MISSING from account archives")
            problems += 1
            continue
        v = archive_view(bvid, jar)
        arc = (v.get("data") or {}).get("archive") or {}
        vids = (v.get("data") or {}).get("videos") or []
        title = arc.get("title") or ""
        expect = want.get("title_expect") or ""
        title_ok = title.startswith(expect) if expect else True
        cur = norm_tag_line(arc.get("tag"))
        state["targets"][bvid] = {"archive": arc, "videos": vids}
        flag = "" if title_ok and not bad else f"  <-- {'TITLE_MISMATCH ' if not title_ok else ''}{';'.join(bad)}"
        if not title_ok or bad:
            problems += 1
        print(f"  {bvid}: state={arc_l['state']}{flag}")
        print(f"      title={title!r}")
        print(f"      cur_tags={cur}")
        print(f"      new_tags={norm_tag_line(want['tags'])}")
        time.sleep(0.4)
    state_path.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"saved state -> {state_path} | problems={problems}")
    return 2 if problems else 0


def edit_with_tags(jar, csrf, arc, vids, tag_line: str):
    payload = {
        "aid": arc.get("aid"),
        "title": arc.get("title"),
        "desc": arc.get("desc", "") or "",
        "desc_format_id": arc.get("desc_format_id", 0) or 0,
        "tag": tag_line,
        "tid": arc.get("tid"),
        "copyright": arc.get("copyright", 2) or 2,
        "source": arc.get("source", "") or "",
        "cover": arc.get("cover", "") or "",
        "dynamic": arc.get("dynamic", "") or "",
        "no_reprint": arc.get("no_reprint", 0) or 0,
        "videos": [
            {"cid": v.get("cid"), "filename": v.get("filename"), "title": v.get("title", ""), "desc": v.get("desc", "")}
            for v in vids
        ],
        "csrf": csrf,
    }
    return req(f"https://member.bilibili.com/x/vu/web/edit?csrf={csrf}", jar, data=payload, method="POST", as_json=True)


def do_apply(plan: dict, state_path: Path, results_path: Path, only=None):
    jar, csrf = load_cookie()
    results = []
    rc = 0
    for bvid, want in plan.items():
        if only and bvid not in only:
            continue
        row = {"bvid": bvid, "at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
        results.append(row)
        bad = validate_plan_tags(want["tags"])
        if bad:
            row["status"] = "REFUSED_PLAN"
            row["problems"] = bad
            print(f"  {bvid}: REFUSED_PLAN {bad}")
            rc = 2
            continue
        v = archive_view(bvid, jar)
        arc = (v.get("data") or {}).get("archive") or {}
        vids = (v.get("data") or {}).get("videos") or []
        if not arc.get("aid"):
            row["status"] = "MISSING"
            print(f"  {bvid}: MISSING archive")
            rc = 2
            continue
        expect = want.get("title_expect") or ""
        if expect and not (arc.get("title") or "").startswith(expect):
            row["status"] = "TITLE_MISMATCH"
            row["live_title"] = arc.get("title")
            print(f"  {bvid}: TITLE_MISMATCH live={arc.get('title')!r}")
            rc = 2
            continue
        old_line = norm_tag_line(arc.get("tag"))
        new_line = norm_tag_line(want["tags"])
        row["old_tags"], row["new_tags"] = old_line, new_line
        if tag_set(old_line) == tag_set(new_line):
            row["status"] = "SKIP_ALREADY"
            print(f"  {bvid}: SKIP_ALREADY")
            continue
        d = edit_with_tags(jar, csrf, arc, vids, new_line)
        code, message = d.get("code"), d.get("message")
        row["edit_code"], row["edit_message"] = code, message
        applied_line = new_line
        if code != 0 and looks_like_tag_limit_error(code, message) and len(new_line.split(",")) > 10:
            trimmed = ",".join(new_line.split(",")[:10])
            row["tag_cap_probe"] = {"attempted": len(new_line.split(",")), "rejected_message": message}
            print(f"  {bvid}: tag-limit rejection at {len(new_line.split(','))} tags -> retry with 10")
            time.sleep(1.5)
            d = edit_with_tags(jar, csrf, arc, vids, trimmed)
            code, message = d.get("code"), d.get("message")
            row["edit_code_retry"], row["edit_message_retry"] = code, message
            applied_line = trimmed
        if code != 0:
            row["status"] = "EDIT_FAILED"
            print(f"  {bvid}: EDIT_FAILED code={code} message={message}")
            rc = 2
            continue
        time.sleep(1.5)
        v2 = archive_view(bvid, jar)
        arc2 = (v2.get("data") or {}).get("archive") or {}
        live_line = norm_tag_line(arc2.get("tag"))
        tag_ok = tag_set(live_line) == tag_set(applied_line)
        title_ok = arc2.get("title") == arc.get("title")
        cover_ok = arc2.get("cover") == arc.get("cover")
        desc_ok = (arc2.get("desc") or "") == (arc.get("desc") or "")
        row["verify"] = {"tag_ok": tag_ok, "title_ok": title_ok, "cover_ok": cover_ok, "desc_ok": desc_ok}
        row["live_tags"] = live_line
        row["status"] = "UPDATED" if (tag_ok and title_ok and cover_ok and desc_ok) else "VERIFY_FAILED"
        if row["status"] == "VERIFY_FAILED":
            rc = 2
        if "tag_cap_probe" in row:
            row["tag_cap_probe"]["settled_count"] = len(live_line.split(","))
        print(
            f"  {bvid}: edit_code={row['edit_code']} | TAG_UPDATED={tag_ok} TITLE_INTACT={title_ok} "
            f"COVER_INTACT={cover_ok} DESC_INTACT={desc_ok}"
        )
        print(f"      live_tags={live_line}")
        results_path.write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
        time.sleep(2.0)
    results_path.write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"saved results -> {results_path}")
    return rc


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("inspect", "apply"))
    parser.add_argument("--plan", required=True)
    parser.add_argument("--state", default="/tmp/tag_update_state.json")
    parser.add_argument("--results", default="/tmp/tag_update_results.json")
    parser.add_argument("--only", nargs="*", default=None)
    args = parser.parse_args()
    plan = json.loads(Path(args.plan).read_text(encoding="utf-8"))
    if args.mode == "inspect":
        return do_inspect(plan, Path(args.state))
    return do_apply(plan, Path(args.state), Path(args.results), only=args.only)


if __name__ == "__main__":
    sys.exit(main())
