"""Guard a manual-lane pack cover against the most recently reconciled lane publication.

The chooser's timestamps describe creation, not publication. The upload lock protects this check and dispatch together.
The old and new records must describe the very cover bytes bound by their respective upload manifests.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from src.autoslice import cover_layout_rotation
from .common import GATE, Refused, bind, drift, sha256


def _refuse(why: str) -> None:
    raise Refused(GATE, f"封面发布时间轮换不能确认：{why}；先核对或重做封面，不投稿")


def _object(path: Path, description: str) -> dict[str, Any]:
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        _refuse(f"{description}读不到 {path}（{type(exc).__name__}）")
    if not isinstance(obj, dict):
        _refuse(f"{description}不是对象：{path}")
    return obj


def _time(value: object) -> datetime:
    try:
        stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        _refuse(f"公开记录的时间无效：{value!r}")
    if stamp.tzinfo is None:
        _refuse(f"公开记录没有时区：{value!r}")
    return stamp


def _layout_table() -> dict:
    from src.autoslice.manual_lane import channel

    table_path = channel.profile().asset_file("cover_layout_templates", repo_root=channel.REPO_ROOT)
    return cover_layout_rotation.load_table(table_path)


def _selection(manifest_path: Path, doc: Mapping[str, Any], *, required: bool) -> tuple[str, bool, bool] | None:
    """(family, uses a pack, adjacent repeat forced), with the route bound to the identical cover that the manifest will upload."""
    clip = manifest_path.parent.parent
    record = clip / "title" / "candidates.json"
    if not record.is_file() and not required:
        return None                         # non-lane uploads use their existing gates
    route = _object(record, "封面选择记录").get("cover_route") or {}
    if not isinstance(route, dict) or route.get("method") != "skill.candidates":
        _refuse(f"{record} 不是可核对版式的候选记录")
    chosen = route.get("chosen") or {}
    template_id = chosen.get("template_id") if isinstance(chosen, dict) else None
    templates = {t["template_id"]: t for t in _layout_table()["templates"]}
    if template_id not in templates:
        _refuse(f"{record} 的样板 {template_id!r} 不认识")
    source_cover = clip / "cover" / "cover.png"
    if not source_cover.is_file() or sha256(source_cover) != str((doc.get("cover") or {}).get("sha256") or "").removeprefix("sha256:"):
        _refuse(f"{record} 的封面与清单绑定的成片封面不一致")
    candidate_id = chosen.get("id")
    choice_path = clip / "cover" / "cand" / "choose.json"
    choice = _object(choice_path, "候选选择记录")
    rows = [row for row in (choice.get("rows") or []) if isinstance(row, dict) and row.get("id") == candidate_id]
    if (not isinstance(candidate_id, str) or Path(candidate_id).name != candidate_id or choice.get("chosen") != candidate_id
            or len(rows) != 1 or (rows[0].get("cover_layout") or {}).get("template_id") != template_id):
        _refuse(f"{record} 与候选选择记录的版式不一致")
    choice_layout = rows[0].get("cover_layout") or {}
    record_layout = chosen.get("cover_layout")
    if record_layout is not None and (
        not isinstance(record_layout, dict)
        or record_layout.get("template_id") != template_id
        or record_layout.get("fallback") != choice_layout.get("fallback")
    ):
        _refuse(f"{record} 与候选选择记录的版式或回退记录不一致")
    fallback = choice_layout.get("fallback")
    adjacent_forced = isinstance(fallback, str) and "adjacent_repeat_forced" in fallback.split("+")
    candidate_image = choice_path.parent / f"{candidate_id}.png"
    if not candidate_image.is_file() or sha256(candidate_image) != sha256(source_cover):
        _refuse("选择记录对应的候选图片与成片封面不一致")
    guests = route.get("guests") or {}
    return str(templates[template_id].get("family", template_id)), isinstance(guests, dict) and guests.get("source") == "pack", adjacent_forced


def _current_is_pack(manifest_path: Path) -> bool:
    record = manifest_path.parent.parent / "title" / "candidates.json"
    if not record.is_file():
        return False
    route = _object(record, "封面选择记录").get("cover_route") or {}
    guests = route.get("guests") if isinstance(route, dict) else None
    return isinstance(guests, dict) and guests.get("source") == "pack"


def freeze(manifest_path: Path, doc: Mapping[str, Any]) -> dict[str, Any] | None:
    """Bind a new pack cover's source records to its upload manifest; non-lane routes remain unchanged."""
    if not _current_is_pack(manifest_path):
        return None
    _selection(manifest_path, doc, required=True)
    clip = manifest_path.parent.parent
    record = clip / "title" / "candidates.json"
    candidate_id = _object(record, "封面选择记录")["cover_route"]["chosen"]["id"]
    return {"record": bind(record), "choice": bind(clip / "cover" / "cand" / "choose.json"),
            "candidate_image": bind(clip / "cover" / "cand" / f"{candidate_id}.png"),
            "source_cover": bind(clip / "cover" / "cover.png")}


def _check_frozen(manifest_path: Path, doc: Mapping[str, Any]) -> None:
    binding = doc.get("cover_rotation")
    if not isinstance(binding, Mapping):
        _refuse("清单没有冻结本稿的封面选择记录，需要重新制作清单")
    for name in ("record", "choice", "candidate_image", "source_cover"):
        if problem := drift(binding.get(name), f"本稿封面 {name}"):
            _refuse(problem)
    if dict(binding) != freeze(manifest_path, doc):
        _refuse("清单绑定的封面选择记录不是本稿的当前文件")


def check(manifest_path: Path, doc: Mapping[str, Any], rows: Sequence[Mapping[str, Any]],
          entries: Sequence[Mapping[str, Any]]) -> None:
    """Refuse a repeated family or an unresolved newer dispatch; no publication side effects are made here."""
    if "cover_rotation" not in doc:
        if _current_is_pack(manifest_path):
            _refuse("清单没有冻结本稿的封面选择记录，需要重新制作清单")
        return
    _check_frozen(manifest_path, doc)
    current = _selection(manifest_path, doc, required=True)
    this_digest = sha256(manifest_path)
    latest: dict[str, str] = {}
    for row in rows:
        digest, event = row.get("manifest_sha256"), row.get("event")
        if isinstance(digest, str) and event in ("dispatched", "posted", "published", "not_posted"):
            latest[digest] = str(event)
    if any(digest != this_digest and event in ("dispatched", "posted") for digest, event in latest.items()):
        _refuse("前一稿已发出或仍待公开核对，实际先后尚未确定")
    previous = next((r for r in reversed(rows) if r.get("event") == "published"), None)
    published = [r for r in entries if r.get("status") == "published"]
    if previous is None:
        if published:
            _refuse("出版登记已有稿件，但本车道没有上一条公开回执")
        return
    stamp_raw, bvid = str(previous.get("at") or ""), str(previous.get("bvid") or "")
    if not stamp_raw or not bvid:
        _refuse("上一条公开记录缺少时间或 BV")
    stamp = _time(stamp_raw)
    for entry in published:
        reconciliation = entry.get("publication_reconciliation") or {}
        if not isinstance(reconciliation, Mapping):
            _refuse("出版登记的公开记录格式不明")
        # A newer first-publication reconciliation from another uploader is not in this ledger's ordering.
        if entry.get("bvid") != bvid and reconciliation.get("status") == "VERIFIED_PUBLIC":
            if not reconciliation.get("reconciled_at"):
                _refuse(f"出版登记里 {entry.get('bvid')} 的公开核对没有记时间，无法确定它与上一稿的先后")
            if _time(reconciliation.get("reconciled_at")) >= stamp:
                _refuse("另一个投稿入口有更新的公开记录，需要先核对真实顺序")
    registered = [r for r in published if r.get("bvid") == bvid]
    if len(registered) != 1:
        _refuse("上一稿在出版登记中不是唯一的公开稿")
    digest = previous.get("manifest_sha256")
    dispatched = next((r for r in reversed(rows) if r.get("event") == "dispatched" and r.get("manifest_sha256") == digest), None)
    if not dispatched or not dispatched.get("manifest"):
        _refuse("上一稿缺少与公开回执对应的首发清单")
    prior_path = Path(str(dispatched["manifest"]))
    if not prior_path.is_file() or sha256(prior_path) != digest:
        _refuse("上一稿首发清单缺失或已变化")
    prior_doc = _object(prior_path, "上一稿清单")
    if "cover_rotation" in prior_doc:
        _check_frozen(prior_path, prior_doc)
    if problem := drift(prior_doc.get("cover"), "上一稿封面"):
        _refuse(problem)
    # A later metadata edit does not change the cover. A cover/video repair may, so follow the existing receipt chain
    # and refuse if its current cover bytes cannot be tied to the first-publication record below.
    from src.autoslice.manual_lane import receipt_chain

    try:
        chain = receipt_chain.resolve((registered[0].get("publication_reconciliation") or {}).get("authority"))
    except (OSError, ValueError) as exc:
        _refuse(f"上一稿的公开/修订回执无法复放（{exc}）")
    reconciliation = registered[0].get("publication_reconciliation") or {}
    expected_schema = {"VERIFIED_PUBLIC": receipt_chain.FIRST_SCHEMA, "VERIFIED_SAME_BV": receipt_chain.VIDEO_COMPLETED,
                       "VERIFIED_SAME_BV_COVER": receipt_chain.COVER_COMPLETED,
                       "VERIFIED_SAME_BV_METADATA": receipt_chain.METADATA_COMPLETED}.get(reconciliation.get("status"))
    if not expected_schema or _object(chain.bound, "上一稿登记回执").get("schema_version") != expected_schema:
        _refuse("上一稿的登记状态和绑定回执不一致")
    receipt = _object(chain.first_receipt, "上一稿首发回执")
    evidence = receipt.get("evidence")
    bound = evidence.get("manifest") if isinstance(evidence, Mapping) else None
    if drift(bound, "上一稿回执的清单") or (bound or {}).get("sha256", "").removeprefix("sha256:") != digest:
        _refuse("上一稿公开回执和车道账本的清单不一致")
    if chain.online_cover_sha256 and chain.online_cover_sha256 != str(prior_doc["cover"]["sha256"]).removeprefix("sha256:"):
        _refuse("上一稿封面已修订，需先确认线上封面的版式")
    prior = _selection(prior_path, prior_doc, required=True)
    if prior is not None and current is not None and prior[0] == current[0] and not current[2]:
        _refuse(f"上一稿 {bvid} 与本稿都是 {current[0]}，需重做其中一个版式")
