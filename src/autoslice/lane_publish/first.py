"""A first publication: one new BV for one sealed package, never two.

Under the host's lock: refused when the registry already has the package; the ledger records `dispatched` before biliup runs and
`posted` with the BV it got, so a run after a crash resumes from the BV instead of posting again (a `dispatched` without a BV
is refused: whether it reached Bilibili has to be looked at, not guessed).  The BV is posted with the manifest's
`publish_policy`, which has to be the rule's when it is sent (a manifest made under an earlier rule is refused before anything is
sent: STALE_POLICY, step 10 makes it again).  Then: wait until the public page shows it, check that it is of the declared type
the manifest posted it with (`declared_type_problems`, against the manifest and not against today's rule, so a BV sent before a
rule change and still in review is checked for what it was sent as; an archive that is not goes no further, and no other new
BV is posted until the ledger says it was looked at: `declared_type_cleared`), add it to
the channel's collection, check it (one part named as the title, the title), write the receipt `<manifest>.uploaded.json`
(`new-bv-publication-reconciliation-authority.v1`, VERIFIED_PUBLIC, which the lane's receipt chain starts from) and the
registry row that binds it."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from . import bili, cover_rotation, manifest, registry
from .common import (FAILED, GATE, NOT_PUBLIC, QUOTA, REFUSED, Config, Publish, Refused, archive_policy_problem, bind, now,
                     read_json, sha256, write_json)
from .authority import FIRST_SCHEMA
from src.autoslice.upload_description import without_source_line

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


def declared_type_problems(view: dict[str, Any], copyright: int) -> list[str]:
    """Why the public page does not show the declared type the BV was posted with ([] when it does): `copyright` is the
    manifest's value and `rights.no_reprint` is 0.  The submission asks for `copyright` 3 (no declared type); should Bilibili store 1 instead,
    the archive would claim to be the uploader's own work and forbid reposting, which the web editor cannot take back.  This
    read-back is the only place that is seen, so a field the page does not give is a problem too, never taken as right."""
    rights = view.get("rights")
    no_reprint = rights.get("no_reprint") if isinstance(rights, dict) else None
    found = []
    if type(view.get("copyright")) is not int or view["copyright"] != copyright:
        found.append(f"线上投稿类型 copyright={view.get('copyright')!r}（投的是 {copyright}）")
    if type(no_reprint) is not int or no_reprint != 0:
        found.append(f"线上 rights.no_reprint={no_reprint!r}（应为 0：不声称自制）")
    return found


TYPE_MISMATCH, TYPE_CLEARED = "declared_type_mismatch", "declared_type_cleared"
STALE_POLICY = "清单的投稿类型不是现行规则"       # step10_run reads these words: it makes the manifest again


def manifest_policy(doc: dict[str, Any]) -> dict[str, Any]:
    """The manifest's `publish_policy`: what its BV is (to be) posted with."""
    policy = doc.get("publish_policy")
    if problem := archive_policy_problem(policy):
        raise Refused(REFUSED, f"上传清单的 publish_policy 不成立：{problem}")
    return dict(policy)


def _type_stop() -> dict[str, Any] | None:
    """The ledger row of an archive that went online with another declared type than the rule's and has not been looked at
    since (no `declared_type_cleared` row after it, written by hand like `not_posted`); None when there is none.  While it
    stands no new BV is posted: what happened to one submission would happen to the next."""
    rows = [r for r in registry.ledger_rows() if r.get("event") in (TYPE_MISMATCH, TYPE_CLEARED)]
    return rows[-1] if rows and rows[-1]["event"] == TYPE_MISMATCH else None


def _mismatch(event: str, bvid: str, digest: str, view: dict[str, Any], problems: list[str]) -> Refused:
    """The archive is public and is not what the manifest and the rule say: the ledger keeps what the page showed, and
    nothing is registered."""
    rights = view.get("rights")
    registry.ledger_add(event, manifest_sha256=digest, bvid=bvid, problems=problems,
                        public={"title": view.get("title"), "copyright": view.get("copyright"),
                                "no_reprint": rights.get("no_reprint") if isinstance(rights, dict) else None})
    return Refused(FAILED, f"{bvid} 已公开，但与清单不符（{'；'.join(problems)}）：没有登记，查清后再处理")


def _post(config: Config, doc: dict[str, Any], policy: dict[str, Any], manifest_path: Path, digest: str) -> str:
    registry.ledger_add("dispatched", manifest=str(manifest_path), manifest_sha256=digest, candidate_id=doc["candidate_id"],
                        publish_policy=policy)
    log = manifest_path.parent / "biliup-upload.log"
    rc, bvid, quota = bili.upload(config, video=Path(doc["video"]["path"]), cover=Path(doc["cover"]["path"]), title=doc["title"],
                                  tags=list(doc["tags"]), desc=without_source_line(doc["description"]), policy=policy, log=log)
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
    policy = manifest_policy(doc)
    with registry.first_upload_locked():
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
            if held := _type_stop():
                raise Refused(GATE, f"{held.get('bvid')} 在 {held.get('at')} 投出后，线上的投稿类型不是规则值"
                              f"（{'；'.join(held.get('problems') or [])}）：查清之前不再投新稿。处理完那条稿件，"
                              f"在账本里记一行 {TYPE_CLEARED} 再投")
            if policy != timing.archive_policy():
                raise Refused(GATE, f"{STALE_POLICY}：清单写的是 {policy}，现行规则 lane_rules.json upload.publish 是 "
                              f"{timing.archive_policy()}；没有投出。按现行规则重做清单（make-manifest）再投")
            cover_rotation.check(manifest_path, doc, registry.ledger_rows(), registry.load()["entries"])
            bvid = _post(config, doc, policy, manifest_path, digest)
        view = wait_public(bvid, wait_s=timing.public_wait_s, poll_s=timing.public_poll_s)
        if view is None:
            raise Refused(NOT_PUBLIC, f"{bvid} 已投出，{timing.public_wait_s:g} 秒内还没有公开（多半在审核）：稍后再跑同一条 upload，"
                          "它从这个 BV 接着核对，不会再投一次")
        if wrong_type := declared_type_problems(view, policy["copyright"]):    # before anything more is done to this archive
            raise _mismatch(TYPE_MISMATCH, bvid, digest, view, wrong_type)
        season = doc["season"]
        bili.SESSION(config).season_episode_add(int(season["section_id"]), aid=int(view["aid"]), cid=int(view["cid"]),
                                                title=doc["title"])
        pages = view.get("pages") or []
        problems = [p for p in (
            None if view.get("title") == doc["title"] else f"线上标题「{view.get('title')}」",
            None if len(pages) == 1 else f"分 P 数 {len(pages)}",
            None if pages and pages[0].get("part") == doc["title"] else f"分 P 名「{pages[0].get('part') if pages else None}」") if p]
        if problems:
            raise _mismatch("public_mismatch", bvid, digest, view, problems)
        tags = bili.public_tags(bvid)
        receipt = {
            "schema_version": FIRST_SCHEMA, "status": "VERIFIED_PUBLIC", "bvid": bvid, "aid": int(view["aid"]),
            "cid": int(view["cid"]), "candidate_id": doc["candidate_id"], "recording_date": doc["recording_date"],
            "title": doc["title"], "evidence": {"manifest": bind(manifest_path)}, "season": season,
            "public": {"title": view.get("title"), "pages": len(pages), "tags": tags, "copyright": view["copyright"],
                       "no_reprint": view["rights"]["no_reprint"]},
            "notes": [] if set(doc["tags"]) <= set(tags) else [f"线上标签少了：{sorted(set(doc['tags']) - set(tags))}"],
            "verified_at": now()}
        write_json(done, receipt)
        registry.add_published(candidate_id=doc["candidate_id"], recording_date=doc["recording_date"], bvid=bvid,
                               aid=receipt["aid"], cid=receipt["cid"], title=doc["title"], authority=bind(done))
        registry.ledger_add("published", manifest_sha256=digest, bvid=bvid)
    return receipt
