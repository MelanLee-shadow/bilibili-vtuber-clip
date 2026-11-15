"""A first publication: one new BV for one sealed package, never two.

Under the host's lock: refused when the registry already has the package; the ledger records `dispatched` before biliup runs and
`posted` with the BV it got, so a run after a crash resumes from the BV instead of posting again (a `dispatched` without a BV
is refused: whether it reached Bilibili has to be looked at, not guessed).  Then: wait until the public page shows it, add it to
the channel's collection, check it (one part named as the title, the title), write the receipt `<manifest>.uploaded.json`
(`new-bv-publication-reconciliation-authority.v1`, VERIFIED_PUBLIC, which the lane's receipt chain starts from) and the
registry row that binds it."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from . import bili, manifest, registry
from .common import FAILED, GATE, NOT_PUBLIC, QUOTA, REFUSED, Config, Publish, Refused, bind, now, read_json, sha256, write_json
from .authority import FIRST_SCHEMA

SLEEP = time.sleep
CLOCK = time.monotonic          # a test replaces these two


def receipt_path(manifest_path: Path) -> Path:
    return manifest_path.with_name(manifest_path.name + ".uploaded.json")


def wait_public(bvid: str, *, wait_s: float, poll_s: float) -> dict[str, Any] | None:
    deadline = CLOCK() + wait_s
    while True:
        view = bili.public_view(bvid)
        if view and view.get("cid"):
            return view
        if CLOCK() >= deadline:
            return None
        SLEEP(poll_s)


def _post(config: Config, doc: dict[str, Any], manifest_path: Path, digest: str) -> str:
    registry.ledger_add("dispatched", manifest=str(manifest_path), manifest_sha256=digest, candidate_id=doc["candidate_id"])
    log = manifest_path.parent / "biliup-upload.log"
    rc, bvid, quota = bili.upload(config, video=Path(doc["video"]["path"]), cover=Path(doc["cover"]["path"]), title=doc["title"],
                                  tags=list(doc["tags"]), desc=doc["description"], log=log)
    if bvid is None:
        registry.ledger_add("not_posted", manifest_sha256=digest, rc=rc, quota=quota)
        if quota:
            raise Refused(QUOTA, f"REFUSE: account quota code 21566 (滚动 24 小时投稿数已满)；biliup 返回 {rc}，没有投出：{log}")
        tail = log.read_text(encoding="utf-8", errors="replace")[-600:] if log.is_file() else ""
        raise Refused(FAILED, f"biliup 返回 {rc}，没有拿到 BV（没有投出）：{tail}")
    registry.ledger_add("posted", manifest_sha256=digest, bvid=bvid, rc=rc)
    return bvid


def upload(manifest_path: Path) -> dict[str, Any]:
    manifest_path = manifest_path.absolute()
    done = receipt_path(manifest_path)
    if done.is_file() and read_json(done, "首发回执").get("status") == "VERIFIED_PUBLIC":
        return read_json(done, "首发回执")
    doc = manifest.load_checked(manifest_path)
    if "recovery_publication_authority" in doc:
        raise Refused(REFUSED, "这是修订包的清单：已发布稿件的修订走 repair-plan / cover-repair-plan，不投新稿")
    config, timing, digest = Config.load(), Publish.load(), sha256(manifest_path)
    with registry.locked():
        if registry.rows_for(registry.load()["entries"], candidate_id=doc["candidate_id"]):
            raise Refused(GATE, f"出版登记里 {doc['candidate_id']} 已经发布过：不投第二个 BV（要改就做后继片走同稿件修订）")
        mine = [r for r in registry.ledger_rows() if r.get("manifest_sha256") == digest]
        posted = [r for r in mine if r["event"] == "posted"]
        if posted:
            bvid = str(posted[-1]["bvid"])                 # an earlier run posted it: only the waiting is left
        elif mine and mine[-1]["event"] == "dispatched":
            raise Refused(GATE, f"上一次上传在 {mine[-1]['at']} 发出、账本里没有 BV：先查 {manifest_path.parent / 'biliup-upload.log'} "
                          "和创作中心是否已有这条稿件；确认没有投出，在账本里记一行 not_posted 再重试")
        else:
            bvid = _post(config, doc, manifest_path, digest)
        view = wait_public(bvid, wait_s=timing.public_wait_s, poll_s=timing.public_poll_s)
        if view is None:
            raise Refused(NOT_PUBLIC, f"{bvid} 已投出，{timing.public_wait_s:g} 秒内还没有公开（多半在审核）：稍后再跑同一条 upload，"
                          "它从这个 BV 接着核对，不会再投一次")
        season = doc["season"]
        bili.SESSION(config).season_episode_add(int(season["section_id"]), aid=int(view["aid"]), cid=int(view["cid"]),
                                                title=doc["title"])
        pages = view.get("pages") or []
        problems = [p for p in (
            None if view.get("title") == doc["title"] else f"线上标题「{view.get('title')}」",
            None if len(pages) == 1 else f"分 P 数 {len(pages)}",
            None if pages and pages[0].get("part") == doc["title"] else f"分 P 名「{pages[0].get('part') if pages else None}」") if p]
        if problems:
            raise Refused(FAILED, f"{bvid} 已公开，但与清单不符（{'；'.join(problems)}）：没有登记，查清后再处理")
        tags = bili.public_tags(bvid)
        receipt = {
            "schema_version": FIRST_SCHEMA, "status": "VERIFIED_PUBLIC", "bvid": bvid, "aid": int(view["aid"]),
            "cid": int(view["cid"]), "candidate_id": doc["candidate_id"], "recording_date": doc["recording_date"],
            "title": doc["title"], "evidence": {"manifest": bind(manifest_path)}, "season": season,
            "public": {"title": view.get("title"), "pages": len(pages), "tags": tags},
            "notes": [] if set(doc["tags"]) <= set(tags) else [f"线上标签少了：{sorted(set(doc['tags']) - set(tags))}"],
            "verified_at": now()}
        write_json(done, receipt)
        registry.add_published({"candidate_id": doc["candidate_id"], "recording_date": doc["recording_date"], "bvid": bvid,
                                "aid": receipt["aid"], "title": doc["title"], "status": "published",
                                "publication_reconciliation": {"status": "VERIFIED_PUBLIC", "authority": bind(done),
                                                               "published_at": receipt["verified_at"]}})
        registry.ledger_add("published", manifest_sha256=digest, bvid=bvid)
    return receipt
