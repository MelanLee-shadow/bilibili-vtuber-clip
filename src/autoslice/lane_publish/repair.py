"""Same-BV revisions: the video and cover replaced (`video`), or the cover alone (`cover`), on the BV a successor package's
authority names.

plan        what replaces what, against what is live now; the registry row, the receipt chain and the authority must agree,
            and a revision made after an earlier video replacement must name that completion (`--predecessor-completed`).
            `--dry-run` prints the plan (step 10 compares it with the package) and writes nothing; a plan is made once.
run         under the host's lock.  video: biliup appends the new file as a second part, the creator centre keeps only that
            part and sets the new cover, title, description and tags; cover: the cover alone.  Every move is written to the
            journal first, and a run after a stop looks at the live BV before doing anything (a part that was appended is
            never appended again).  Answers 6 until the public page shows the change, 0 after.
status      0 once the change is live, 6 before.
verify-live reads the public page once more and writes the completion receipt (`same-bv-repair-completed.v1` /
            `same-bv-cover-repair-completed.v1`, VERIFIED_FRESH_LIVE); the registry row then binds it."""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any

from src.autoslice.manual_lane import receipt_chain

from . import authority, bili, manifest, registry
from .common import FAILED, GATE, NOT_YET, OK, REFUSED, Config, Publish, Refused, bind, drift, now, read_json, sha256, write_json

VIDEO, COVER = "video", "cover"
PLAN_SCHEMA = {VIDEO: "lane-publish-same-bv-repair-plan.v1", COVER: "lane-publish-same-bv-cover-repair-plan.v1"}
COMPLETED = {VIDEO: receipt_chain.VIDEO_COMPLETED, COVER: receipt_chain.COVER_COMPLETED}
ROW_STATUS = {VIDEO: "VERIFIED_SAME_BV", COVER: "VERIFIED_SAME_BV_COVER"}
SLEEP = time.sleep
CLOCK = time.monotonic          # a test replaces these two


def _journal(journal: Path, plan_id: str) -> list[dict[str, Any]]:
    if not journal.is_file():
        return []
    rows = [json.loads(line) for line in journal.read_text(encoding="utf-8").splitlines() if line.strip()]
    return [r for r in rows if r.get("plan_id") == plan_id]


def _note(journal: Path, plan_id: str, event: str, **fields: Any) -> None:
    journal.parent.mkdir(parents=True, exist_ok=True)
    with journal.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"at": now(), "plan_id": plan_id, "event": event, **fields}, ensure_ascii=False) + "\n")


def _last(rows: list[dict[str, Any]], event: str) -> dict[str, Any] | None:
    found = [r for r in rows if r["event"] == event]
    return found[-1] if found else None


def plan(kind: str, *, manifest_path: Path, bvid: str, out: Path, journal: Path, preserve_tags: bool,
         predecessor: Path | None, dry_run: bool) -> dict[str, Any]:
    manifest_path = manifest_path.absolute()
    doc = manifest.load_checked(manifest_path)
    auth = doc.get("recovery_publication_authority")
    if not isinstance(auth, dict) or auth.get("bvid") != bvid:
        raise Refused(GATE, f"清单的同稿件授权不是 {bvid} 的：{(auth or {}).get('bvid') if isinstance(auth, dict) else auth}")
    try:
        auth = authority.validate(auth, candidate_id=doc["candidate_id"], expected_final_title=doc["title"])
    except authority.AuthorityError as exc:
        raise Refused(GATE, f"同稿件授权重放不过：{exc}") from exc
    rows = registry.rows_for(registry.load()["entries"], bvid=bvid)
    if len(rows) != 1 or rows[0].get("candidate_id") != doc["candidate_id"]:
        raise Refused(GATE, f"出版登记里 {bvid} 的已发布行不是这个包的（{len(rows)} 条）")
    try:
        chain = receipt_chain.resolve((rows[0].get("publication_reconciliation") or {}).get("authority"))
    except receipt_chain.ReceiptChainError as exc:
        raise Refused(GATE, f"{bvid} 的回执链走不通：{exc}") from exc
    if str(chain.first_receipt) != str(auth["source_publication"]["path"]):
        raise Refused(GATE, "授权所依据的首发回执不是登记行回执链上的那一份")
    before_done = chain.predecessor_completed
    if (str(before_done) if before_done else None) != (str(predecessor.absolute()) if predecessor else None):
        raise Refused(GATE, f"上一次换视频的完成回执是 {before_done}，这次给的是 {predecessor}：按最新的那次接着改")
    config = Config.load()
    creator = bili.SESSION(config).archive_view(bvid)
    public = bili.public_view(bvid)
    if public is None:
        raise Refused(GATE, f"{bvid} 现在不是公开状态：等它公开再改")
    archive = creator.get("archive") or {}
    live_tags = [t for t in str(archive.get("tag") or "").split(",") if t]
    meta = {"title": doc["title"], "desc": doc["description"], "tags": live_tags if (preserve_tags or kind == COVER) else doc["tags"]}
    value: dict[str, Any] = {
        "schema_version": PLAN_SCHEMA[kind], "plan_id": uuid.uuid4().hex, "created_at": now(), "kind": kind, "bvid": bvid,
        "manifest": {"path": str(manifest_path), "sha256": sha256(manifest_path)}, "authorization": doc["authorization"],
        "recovery_publication_authority": auth, "package_attestation": doc["package_attestation"], "season": doc["season"],
        "before": {"public": {"aid": public.get("aid"), "cid": public.get("cid"), "title": public.get("title")},
                   "creator": {"cids": [v.get("cid") for v in creator.get("videos") or []], "title": archive.get("title"),
                               "desc": archive.get("desc"), "tags": live_tags, "cover": archive.get("cover")}},
        "predecessor_completion": {"completed": bind(predecessor)} if predecessor else None}
    if kind == VIDEO:
        value["replacement"] = {"video": doc["video"], "cover": doc["cover"]}
        value["target_metadata"] = meta
    else:
        if archive.get("title") != doc["title"] or str(archive.get("desc") or "").strip() != doc["description"].strip():
            raise Refused(GATE, "只换封面要求标题和简介与线上一字不差：不一样就是换视频或改题，不走这条路")
        value["replacement_cover"] = doc["cover"]
        value["unchanged_metadata"] = meta
        value["unchanged_cid"] = public.get("cid")
    if not dry_run:
        if out.exists():
            raise Refused(GATE, f"计划只建一次，{out} 已经有了")
        write_json(out, value)
        _note(journal, value["plan_id"], "planned", plan=str(out.absolute()))
    return value


def _load_plan(path: Path) -> tuple[str, dict[str, Any]]:
    value = read_json(path, "修订计划")
    kind = value.get("kind")
    if kind not in PLAN_SCHEMA or value.get("schema_version") != PLAN_SCHEMA[kind]:
        raise Refused(REFUSED, f"{path} 不是 lane_publish 的修订计划")
    return str(kind), value


def _file_name(url: object) -> str:
    """The image's file name: the public page serves the cover from another host and scheme than the upload answered."""
    return str(url or "").split("?")[0].rsplit("/", 1)[-1]


def _fresh_live(kind: str, value: dict[str, Any], edited: dict[str, Any]) -> dict[str, Any] | None:
    """The public page, when it shows the change; else None."""
    view = bili.public_view(value["bvid"])
    pages = (view or {}).get("pages") or []
    if not view or len(pages) != 1:
        return None
    if kind == VIDEO:
        same = view.get("cid") == edited["new_cid"] and view.get("title") == value["target_metadata"]["title"]
    else:
        same = view.get("cid") == value["unchanged_cid"] and _file_name(view.get("pic")) == _file_name(edited["cover_url"])
    return view if same else None


def status(*, plan_path: Path, journal: Path) -> int:
    _kind, value = _load_plan(plan_path)
    return OK if _last(_journal(journal, value["plan_id"]), "live") else NOT_YET


def run(*, plan_path: Path, journal: Path, dry_run: bool) -> int:
    kind, value = _load_plan(plan_path)
    plan_id = value["plan_id"]
    if dry_run:
        return OK if _last(_journal(journal, plan_id), "live") else NOT_YET
    config, timing = Config.load(), Publish.load()
    with registry.locked():
        rows = _journal(journal, plan_id)
        if _last(rows, "live"):
            return OK
        edited = _last(rows, "edited")
        if edited is None:
            for key in ("video", "cover") if kind == VIDEO else ("cover",):
                binding = value["replacement"][key] if kind == VIDEO else value["replacement_cover"]
                if problem := drift(binding, key):
                    raise Refused(GATE, f"计划之后文件变了：{problem}")
            edited = _edit(kind, value, config, timing, journal, rows)
            if edited is None:
                return NOT_YET
        view = _fresh_live(kind, value, edited)
        if view is None:
            return NOT_YET
        _note(journal, plan_id, "live", cid=view.get("cid"))
        return OK


def _edit(kind: str, value: dict[str, Any], config: Config, timing: Publish, journal: Path,
          rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    session, bvid, plan_id = bili.SESSION(config), value["bvid"], value["plan_id"]
    current = session.archive_view(bvid)
    new_cid = None
    if kind == VIDEO:
        known = set(value["before"]["creator"]["cids"])
        fresh = [v for v in current.get("videos") or [] if v.get("cid") not in known]
        appends = [r["event"] for r in rows if r["event"] in ("append_started", "append_failed")]
        if not fresh and (not appends or appends[-1] == "append_failed"):     # never a second append of one that may be on its way
            _note(journal, plan_id, "append_started")
            try:
                session.biliup_append(bvid, Path(value["replacement"]["video"]["path"]))
            except (RuntimeError, OSError) as exc:
                _note(journal, plan_id, "append_failed", error=str(exc)[:300])
                raise Refused(FAILED, f"biliup append 失败：{exc}") from exc
        deadline = CLOCK() + timing.new_part_wait_s
        while not fresh:
            current = session.archive_view(bvid)
            fresh = [v for v in current.get("videos") or [] if v.get("cid") not in known]
            if fresh or CLOCK() >= deadline:
                break
            SLEEP(timing.public_poll_s)
        if not fresh:
            return None                    # appended, not listed yet: the next run looks again and does not append again
        new_cid = int(fresh[0]["cid"])
        _note(journal, plan_id, "appended", new_cid=new_cid)
    cover_path = Path((value["replacement"]["cover"] if kind == VIDEO else value["replacement_cover"])["path"])
    cover_url = session.cover_up(cover_path.read_bytes())
    meta = value["target_metadata"] if kind == VIDEO else value["unchanged_metadata"]
    payload = session.build_edit_payload(current, title=meta["title"], cover_url=cover_url, keep_only_cid=new_cid,
                                         tag=",".join(meta["tags"]))
    payload["desc"] = meta["desc"]
    session.edit_archive(payload)
    edited = {"new_cid": new_cid, "cover_url": cover_url}
    _note(journal, plan_id, "edited", **edited)
    return edited


def verify_live(*, plan_path: Path, journal: Path, out: Path) -> dict[str, Any]:
    kind, value = _load_plan(plan_path)
    rows = _journal(journal, value["plan_id"])
    edited = _last(rows, "edited")
    if edited is None or not _last(rows, "live"):
        raise Refused(GATE, "这个计划还没有在线上生效（run 还没答 0）：不写完成回执")
    view = _fresh_live(kind, value, edited)
    if view is None:
        raise Refused(FAILED, f"{value['bvid']} 的公开页不再是改后的样子：不写完成回执")
    receipt: dict[str, Any] = {
        "schema_version": COMPLETED[kind], "status": "VERIFIED_FRESH_LIVE", "rc": 0, "bvid": value["bvid"],
        "aid": view.get("aid"), "candidate_id": value["recovery_publication_authority"]["candidate_id"],
        "plan": {"path": str(plan_path.absolute()), "sha256": sha256(plan_path), "plan_id": value["plan_id"]},
        "manifest": value["manifest"], "live_snapshot": {"public": {"cid": view.get("cid"), "title": view.get("title"),
                                                                     "pic": view.get("pic")}},
        "verified_at": now()}
    if kind == VIDEO:
        receipt.update(new_cid=int(edited["new_cid"]), replacement=value["replacement"])
    else:
        receipt.update(unchanged_cid=value["unchanged_cid"], new_cover=value["replacement_cover"])
    with registry.locked():
        write_json(out, receipt)
        registry.rebind(value["bvid"], status=ROW_STATUS[kind], receipt=bind(out), aid=int(view["aid"]),
                        cid=int(receipt["new_cid"] if kind == VIDEO else receipt["unchanged_cid"]), title=str(view.get("title")))
    return receipt
