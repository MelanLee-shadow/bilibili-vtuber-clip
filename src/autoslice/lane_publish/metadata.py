"""Same-BV metadata edits: the title, the tags and the description of a published BV, nothing else.

No video is appended, no cover is uploaded, no new BV is posted, and no package, recording or media file is read: what a
published BV may be edited by is its row in the publication registry alone.  That row must be the one published row of the BV
(a BV the registry does not hold as published is refused, and so is any BV or manuscript the registry holds for review,
`hold_pending_review`).  The edit goes through the creator centre's own edit call with everything else of the archive sent
back as it is (`build_edit_payload`: the parts, the cover, the zone, the source).

metadata-plan        what the BV is to show, against what it shows now; written once.  `--dry-run` prints it and writes
                     nothing.  A value not given stays as it is online; `--fix-source-line` takes the description online and
                     leaves one of the repeated leading source lines (U430, 11-18: first publications 11-15 to 11-18 show
                     `https://live.bilibili.com/` twice).
metadata-run         the edit, under the host's lock; every move goes to the journal first and a run after a stop never
                     edits twice.  Answers 6 until the public page shows the title, tags and description of the plan, 0 after.
metadata-status      0 once the change is live, 6 before.
metadata-verify-live reads the public page and its tags once more; only when all three equal the plan it writes the completion
                     receipt (`same-bv-metadata-edit-completed.v1`, VERIFIED_FRESH_LIVE), and the registry row then binds
                     it (status VERIFIED_SAME_BV_METADATA, the new title).  The receipt binds the receipt the row bound
                     before (`previous`), so the lane's receipt chain walks through it (manual_lane/receipt_chain)."""

from __future__ import annotations

import json
import re
import uuid
from pathlib import Path
from typing import Any

from src.autoslice.manual_lane import receipt_chain
from src.autoslice.manual_lane.title_history import same_clip
from src.autoslice.upload_description import EXPECTED_SOURCE
from src.autoslice.upload_tag_policy import apply_tag_policy

from . import bili, registry
from .common import FAILED, GATE, NOT_YET, OK, REFUSED, Config, Refused, bind, now, read_json, sha256, write_json

PLAN_SCHEMA = "lane-publish-same-bv-metadata-edit-plan.v1"
COMPLETED = receipt_chain.METADATA_COMPLETED
ROW_STATUS = "VERIFIED_SAME_BV_METADATA"
HOLD = "hold_pending_review"
TITLE_MAX, DESC_MAX = 80, 2000                                   # the creator centre's own limits (tags follow upload_tag_policy)
PREFIX = re.compile(r"^【[^】]+】")                              # the channel's envelope of a title (【花礼】...)


def _journal(journal: Path, plan_id: str) -> list[dict[str, Any]]:
    if not journal.is_file():
        return []
    rows = [json.loads(line) for line in journal.read_text(encoding="utf-8").splitlines() if line.strip()]
    return [r for r in rows if r.get("plan_id") == plan_id]


def _note(journal: Path, plan_id: str, event: str, **fields: Any) -> None:
    journal.parent.mkdir(parents=True, exist_ok=True)
    with journal.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"at": now(), "plan_id": plan_id, "event": event, **fields}, ensure_ascii=False) + "\n")


def _last(rows: list[dict[str, Any]], *events: str) -> dict[str, Any] | None:
    found = [r for r in rows if r["event"] in events]
    return found[-1] if found else None


def _row(*, bvid: str | None, cid: str | None) -> dict[str, Any]:
    """The one published registry row the edit takes its authority from (see the module docstring)."""
    try:
        entries = registry.load()["entries"]
    except (OSError, ValueError) as exc:
        raise Refused(GATE, f"读不到出版登记，不改：{type(exc).__name__}: {exc!s:.400}") from exc
    if bvid:
        rows = registry.rows_for(entries, bvid=bvid)
    else:
        rows = [r for r in registry.rows_for(entries) if same_clip(str(r.get("candidate_id") or ""), str(cid))]
    if len(rows) != 1:
        raise Refused(GATE, f"出版登记里 {bvid or cid} 的已发布行有 {len(rows)} 条，应为 1 条：没有已发布登记就没有改的依据，不改")
    row = rows[0]
    held = [r for r in entries if r.get("status") == HOLD
            and (r.get("bvid") == row["bvid"] or r.get("candidate_id") == row["candidate_id"])]
    if held:
        raise Refused(GATE, f"出版登记里 {row['bvid']}（{row['candidate_id']}）有搁置待审的记录（{HOLD}：{held[0].get('note') or '无备注'}）："
                            "放行前不改")
    projection = row.get("publication_reconciliation") or {}
    if projection.get("content_coverage") is not None:
        raise Refused(GATE, f"{row['bvid']} 的登记带合并内容覆盖（content_coverage）：元数据修订不处理这种稿件")
    if not isinstance(projection.get("authority"), dict) or type(projection.get("aid")) is not int \
            or type(projection.get("cid")) is not int:
        raise Refused(GATE, f"{row['bvid']} 的登记行没有绑定回执或 aid/cid：不改")
    return row


def _parse_tags(text: str) -> list[str]:
    tags = [t.strip() for t in text.split(",")]
    if not tags or any(not t for t in tags):
        raise Refused(REFUSED, f"标签要用英文逗号分隔、不能有空的：{text!r}")
    return tags


def _tags(text: str, *, live_tags: list[str] | None = None) -> list[str]:
    tags = _parse_tags(text)
    settled = apply_tag_policy(tags, previous=live_tags, on_overflow="refuse")
    if not settled.ok:
        raise Refused(REFUSED, f"标签不符合策略（{'；'.join(settled.reasons)}）：{tags}")
    return list(settled.tags)


def collapse_source_line(desc: str) -> str:
    """The description with a repeated leading source line left once (what Bilibili itself puts ahead of a reposted archive's
    description, sent along too by the first publications of 11-15 to 11-18).  Only archives published as reposts (what the
    pipeline posted before) have the line; one published without a declared type has none and is left as it is."""
    head = EXPECTED_SOURCE + "\n"
    while desc.startswith(head + head):
        desc = desc[len(head):]
    return desc


def _same(a: str | None, b: str | None) -> bool:
    return str(a or "").strip() == str(b or "").strip()


def plan(*, bvid: str | None, cid: str | None, title: str | None, tags: str | None, desc_file: Path | None,
         fix_source_line: bool, authorized_by: str, quote: str, out: Path, journal: Path, dry_run: bool) -> dict[str, Any]:
    if not authorized_by.strip() or not quote.strip():
        raise Refused(REFUSED, "要写明谁要求这次修订、原话是什么（--authorized-by / --quote）")
    row = _row(bvid=bvid, cid=cid)
    bvid = str(row["bvid"])
    projection = row["publication_reconciliation"]
    config = Config.load()
    creator = bili.SESSION(config).archive_view(bvid)
    public = bili.public_view(bvid)
    if public is None:
        raise Refused(GATE, f"{bvid} 现在不是公开状态（审核中或不可见）：等它公开再改")
    if public.get("aid") != projection["aid"] or public.get("cid") != projection["cid"]:
        raise Refused(GATE, f"{bvid} 线上的 aid/cid（{public.get('aid')}/{public.get('cid')}）与登记的"
                            f"（{projection['aid']}/{projection['cid']}）不符：登记与线上不一致，不改")
    archive = creator.get("archive") or {}
    videos = creator.get("videos") or []
    if len(videos) != 1 or len(public.get("pages") or []) != 1:
        raise Refused(GATE, f"{bvid} 不是单 P 稿件（创作中心 {len(videos)} P）：不改")
    live_tags = [t.strip() for t in str(archive.get("tag") or "").split(",") if t.strip()]
    live_desc = str(archive.get("desc") or "")
    if desc_file is not None and fix_source_line:
        raise Refused(REFUSED, "--desc-file 和 --fix-source-line 只能给一个")
    target_desc = (desc_file.read_text(encoding="utf-8").rstrip("\n") if desc_file is not None
                   else collapse_source_line(live_desc) if fix_source_line else live_desc)
    if tags is None:
        target_tags = live_tags
    else:
        parsed_tags = _parse_tags(tags)
        target_tags = live_tags if parsed_tags == live_tags else _tags(tags, live_tags=live_tags)
    target = {"title": (title if title is not None else str(archive.get("title") or "")).strip(),
              "tags": target_tags, "desc": target_desc}
    if not target["title"] or len(target["title"]) > TITLE_MAX or len(target["desc"]) > DESC_MAX:
        raise Refused(REFUSED, f"标题为空或超过 {TITLE_MAX} 字、或简介超过 {DESC_MAX} 字")
    prefix = PREFIX.match(str(archive.get("title") or ""))
    if prefix and not target["title"].startswith(prefix.group(0)):
        raise Refused(REFUSED, f"新标题要保留线上标题的前缀 {prefix.group(0)}：{target['title']!r}")
    if (target["title"] == archive.get("title") and target["tags"] == live_tags and _same(target["desc"], live_desc)):
        raise Refused(GATE, f"{bvid} 线上的标题、标签、简介已经是这样：没有要改的")
    value: dict[str, Any] = {
        "schema_version": PLAN_SCHEMA, "plan_id": uuid.uuid4().hex, "created_at": now(), "bvid": bvid,
        "candidate_id": row["candidate_id"], "recording_date": row["recording_date"],
        "registry_row": {"status": projection.get("status"), "aid": projection["aid"], "cid": projection["cid"],
                         "title": projection.get("title"), "authority": dict(projection["authority"])},
        "authorization": {"by": authorized_by.strip(), "quote": quote.strip(), "at": now()},
        "before": {"public": {"aid": public.get("aid"), "cid": public.get("cid"), "title": public.get("title"),
                              "desc": public.get("desc")},
                   "creator": {"cids": [v.get("cid") for v in videos], "title": archive.get("title"), "desc": live_desc,
                               "tags": live_tags, "cover": archive.get("cover")}},
        "target": target}
    if not dry_run:
        if out.exists():
            raise Refused(GATE, f"计划只建一次，{out} 已经有了")
        write_json(out, value)
        _note(journal, value["plan_id"], "planned", plan=str(out.absolute()))
    return value


def _load_plan(path: Path) -> dict[str, Any]:
    value = read_json(path, "元数据修订计划")
    if value.get("schema_version") != PLAN_SCHEMA:
        raise Refused(REFUSED, f"{path} 不是 lane_publish 的元数据修订计划")
    return value


def _still_bound(value: dict[str, Any]) -> dict[str, Any]:
    """The registry row of the plan's BV, still the one the plan was made against (no other repair has moved it since)."""
    row = _row(bvid=value["bvid"], cid=None)
    if dict(row["publication_reconciliation"]["authority"]) != value["registry_row"]["authority"]:
        raise Refused(GATE, f"计划之后 {value['bvid']} 的登记行换了回执（别的修订已经上线）：按现在的线上重新建计划")
    return row


def _shows(view: dict[str, Any] | None, tags: list[str], value: dict[str, Any]) -> list[str]:
    """What of the plan's target the public page does not show (empty: all of it)."""
    target = value["target"]
    if not view:
        return ["公开页不可见"]
    wrong = []
    if len(view.get("pages") or []) != 1 or view.get("cid") != value["before"]["public"]["cid"]:
        wrong.append(f"cid/分P 变了（{view.get('cid')}）")
    if view.get("title") != target["title"]:
        wrong.append(f"标题是 {view.get('title')!r}")
    if not _same(view.get("desc"), target["desc"]):
        wrong.append(f"简介是 {str(view.get('desc'))[:200]!r}")
    if sorted(tags) != sorted(target["tags"]):
        wrong.append(f"标签是 {tags}")
    return wrong


def _read_live(bvid: str) -> tuple[dict[str, Any] | None, list[str]]:
    view = bili.public_view(bvid)
    return view, (bili.public_tags(bvid) if view else [])


def status(*, plan_path: Path, journal: Path) -> int:
    value = _load_plan(plan_path)
    return OK if _last(_journal(journal, value["plan_id"]), "live") else NOT_YET


def run(*, plan_path: Path, journal: Path, dry_run: bool) -> int:
    value = _load_plan(plan_path)
    plan_id, bvid, target = value["plan_id"], value["bvid"], value["target"]
    if dry_run:
        return OK if _last(_journal(journal, plan_id), "live") else NOT_YET
    config = Config.load()
    with registry.locked():
        rows = _journal(journal, plan_id)
        if _last(rows, "live"):
            return OK
        if _last(rows, "edited") is None:
            _still_bound(value)
            session = bili.SESSION(config)
            current = session.archive_view(bvid)
            archive = current.get("archive") or {}
            if [v.get("cid") for v in current.get("videos") or []] != value["before"]["creator"]["cids"]:
                raise Refused(GATE, f"计划之后 {bvid} 的分 P 变了：按现在的线上重新建计划")
            already = (archive.get("title") == target["title"] and _same(archive.get("desc"), target["desc"])
                       and [t.strip() for t in str(archive.get("tag") or "").split(",") if t.strip()] == target["tags"])
            last = _last(rows, "edit_started", "edit_failed")
            if not already and last is not None and last["event"] == "edit_started":
                raise Refused(GATE, f"上次编辑 {bvid} 发出后没有结果记录，创作中心也不是计划的样子：先人工看创作中心再决定，"
                                    "不盲目再编辑一次（每次编辑都会重新送审）")
            if not already:
                payload = session.build_edit_payload(current, title=target["title"], tag=",".join(target["tags"]))
                payload["desc"] = target["desc"]
                _note(journal, plan_id, "edit_started")
                try:
                    session.edit_archive(payload)
                except (RuntimeError, OSError) as exc:
                    _note(journal, plan_id, "edit_failed", error=str(exc)[:300])
                    raise Refused(FAILED, f"创作中心编辑 {bvid} 失败：{exc!s:.300}") from exc
            _note(journal, plan_id, "edited", already=already)
        view, tags = _read_live(bvid)
        if _shows(view, tags, value):
            return NOT_YET
        _note(journal, plan_id, "live", cid=view.get("cid") if view else None)
        return OK


def verify_live(*, plan_path: Path, journal: Path, out: Path) -> dict[str, Any]:
    value = _load_plan(plan_path)
    if out.is_file():           # written before: done when the row binds it (a stop between the two is finished below)
        done = read_json(out, "元数据修订完成回执")
        row = _row(bvid=value["bvid"], cid=None)
        if done.get("plan", {}).get("plan_id") == value["plan_id"] \
                and dict(row["publication_reconciliation"]["authority"]) == bind(out):
            return done
    rows = _journal(journal, value["plan_id"])
    if _last(rows, "edited") is None or not _last(rows, "live"):
        raise Refused(GATE, "这个计划还没有在线上生效（metadata-run 还没答 0）：不写完成回执")
    view, tags = _read_live(value["bvid"])
    if wrong := _shows(view, tags, value):
        raise Refused(FAILED, f"{value['bvid']} 的公开页读回与计划不符：{'；'.join(wrong)}：不写完成回执、不改登记")
    assert view is not None
    receipt: dict[str, Any] = {
        "schema_version": COMPLETED, "status": "VERIFIED_FRESH_LIVE", "rc": 0, "bvid": value["bvid"], "aid": view.get("aid"),
        "candidate_id": value["candidate_id"], "recording_date": value["recording_date"],
        "unchanged_cid": value["before"]["public"]["cid"],
        "plan": {"path": str(plan_path.absolute()), "sha256": sha256(plan_path), "plan_id": value["plan_id"]},
        "previous": value["registry_row"]["authority"], "authorization": value["authorization"],
        "target": value["target"], "before": value["before"],
        "live_snapshot": {"public": {"cid": view.get("cid"), "title": view.get("title"), "desc": view.get("desc"),
                                     "tags": tags}},
        "verified_at": now()}
    with registry.locked():
        _still_bound(value)
        write_json(out, receipt)
        registry.rebind(value["bvid"], status=ROW_STATUS, receipt=bind(out), aid=int(view["aid"]),
                        cid=int(receipt["unchanged_cid"]), title=str(view.get("title")))
    return receipt
