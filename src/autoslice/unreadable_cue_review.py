"""删过不可读字幕的成品 → 人工审阅停泊态，而不是拦死、也不是照常放行。

维护者 逐字：「……应该直接报需要审查，并且在权宜上传时也不能上传，
可以把这段字幕删掉然后出成品等待审阅，而不是拦住。」

三条硬要求分别落在哪：

1. **报「需要人工审查」** —— 本模块的停泊回执 + 报表专章
   （``render_report_section``），维护者 一眼看到「这里少了一句话，为什么少」。
2. **权宜/快速上传通道也不能上传** —— 靠状态承载，不靠回执。
   ``UNREADABLE_CUE_REVIEW_STATUS`` **不在**
   ``session_autoslice.DELIVERED_TALK_STATUSES``（``{"ok","review_ready",
   "quarantine"}``）里，于是：日审清单构建器要求 ``status == "review_ready"``
   直接跳过它 → v3 包认证拿不到 review_manifest 条目 → ``authorized_upload``
   的 ``upload()`` 连门都进不去；封面车道（``cover_maintenance``/``cover_repair``）
   只认 ``TALK_COVER_PENDING_STATUS``，不会把它偷偷提成 ``review_ready``；
   ``package_import``/``resume_frozen_talk_package``/``revive_rejected_candidates``
   同样按 ``review_ready`` 白名单拒收。``delivery_fast_path``（唯一叫得上
   「快速通道」的模块）只跳过可证明被下游覆盖的**发现/改写**阶段，不读任何状态，
   也不产生上传授权。
3. **出成品等待审阅、不拦死** —— 删除发生在终审自愈循环里，release gate 照常
   PASS，媒体照常烧录；成品路径写进停泊回执，维护者 打得开。

**停泊 ≠ 放行**，本模块沿用 ``speaker_manual_review`` 的既有范式，不另造平行状态机：
回执是收据，真正的 fail-closed 由 ``status`` 不在 ``DELIVERED_TALK_STATUSES``
里承载；出版登记（``publication_registry`` 的 ``hold_pending_review``）是仓内
已提交资产上的人工裁定，运行时**不得**代 维护者 写行（7667d9a 血泪，而且
``state/publication_registry.runtime.v1.json`` 是另一套 schema，写错会以
``PUBLICATION_RUNTIME_REGISTRY_INVALID`` 把**所有**上传一起拦掉）。

唤醒面只有人。这条与说话人停泊不同：说话人证据不足会被说话人模块的代码波唤醒，
而「这 0.92s 的音频装不下这些音节」是音频本身的确定性事实，重产一万次结论一样。
所以本状态**不进** ``TALK_RECOVERY_FAILURE_STATUSES``、不写
``failure_recovery_fingerprint`` —— 不写指纹是刻意的：写了个未登记的
``failure_kind`` 会退化成全量 pipeline 指纹，仓里任何一次改动都能把它唤醒、
每次重烧一遍完整产线。回执里把这件事写死成 ``wakes_on: HUMAN_OPERATOR_ONLY``。
"""

from __future__ import annotations

import json
import time
from collections.abc import Collection, Mapping
from pathlib import Path

from src.autoslice.unreadable_span_policy import (
    DROP_AUTHORITY,
    drops_from_audit,
    unreadable_cue_drop_audit_problem,
)


UNREADABLE_CUE_REVIEW_STATUS = "unreadable_cue_review_required"
UNREADABLE_CUE_REVIEW_SCHEMA = "unreadable-cue-review-hold.v1"
DROP_SIDECAR_SUFFIX = ".unreadable-cue-drops.json"
_SIDECAR_GLOB = f"replacement_recuts/*{DROP_SIDECAR_SUFFIX}"


def _candidate_id(record: Mapping[str, object]) -> str:
    return str(record.get("candidate_id") or record.get("cid") or "")


def drop_sidecar_path(recut_dir: Path, cid: str) -> Path:
    return Path(recut_dir) / f"{cid}{DROP_SIDECAR_SUFFIX}"


def drops_from_work_dir(work_dir: object) -> list[dict]:
    """从 produce 落盘的旁车回执里读出本次删了哪几段。

    **检测失败必须 fail-closed**：旁车文件在场就说明这条成品确实少了字幕，读不
    出来/形状不对时也照停不误 —— 与 ``speaker_guess._guess_digest_from_manifests``
    同款理由（4000 字节 summary 尾窗会截断，摘要不可信，只信落盘文件）。
    """

    if work_dir is None:
        return []
    try:
        paths = sorted(Path(str(work_dir)).glob(_SIDECAR_GLOB))
    except OSError:
        return []
    drops: list[dict] = []
    for path in paths:
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            drops.append(_unreadable_sidecar_stub(path, "RECEIPT_UNPARSEABLE"))
            continue
        if unreadable_cue_drop_audit_problem(
            document, expected_srt_sha256=None
        ) is not None:
            drops.append(_unreadable_sidecar_stub(path, "RECEIPT_INVALID"))
            continue
        rows = drops_from_audit(document)
        if not rows:
            drops.append(_unreadable_sidecar_stub(path, "RECEIPT_EMPTY"))
            continue
        for row in rows:
            row["receipt_path"] = str(path)
            drops.append(row)
    return drops


def _unreadable_sidecar_stub(path: Path, status: str) -> dict:
    return {
        "schema_version": "unreadable-cue-drop-unverifiable.v1",
        "status": status,
        "receipt_path": str(path),
        "authority": DROP_AUTHORITY,
        "upload_authorized": False,
    }


def park_for_unreadable_cue_review(
    record: dict,
    *,
    drops: Collection[Mapping[str, object]],
    artifacts: Mapping[str, object] | None = None,
    held_status: str | None = None,
) -> dict:
    """给删过字幕的成品盖一份自述回执；调用方负责把 ``status`` 压到停泊态。"""

    existing = record.get("unreadable_cue_review")
    existing = existing if isinstance(existing, Mapping) else {}
    held_at = existing.get("held_at") or time.strftime(
        "%Y-%m-%dT%H:%M:%SZ", time.gmtime()
    )
    artifacts = (
        artifacts if artifacts is not None else existing.get("review_artifacts")
    )
    rows = [dict(drop) for drop in drops]
    receipt: dict[str, object] = {
        "schema_version": UNREADABLE_CUE_REVIEW_SCHEMA,
        "status": "PENDING_HUMAN_REVIEW",
        "disposition": "AWAITING_HUMAN_UNREADABLE_CUE_REVIEW",
        "held_status": str(
            held_status if held_status is not None else record.get("status") or ""
        ),
        "reason": "acoustic_witness_reported_physically_unreadable_span",
        "authority": DROP_AUTHORITY,
        # 显式写死：停泊件永远不是上传授权，也永远不是 review_ready。
        "upload_authorized": False,
        "review_authority": "HUMAN_OPERATOR",
        "resolution": "HUMAN_RESTORES_OR_ACCEPTS_THE_DELETED_LINE",
        # 说明唤醒面，免得后人以为它靠定时重试或代码波自愈。
        "wakes_on": "HUMAN_OPERATOR_ONLY",
        "dropped_cue_count": len(rows),
        "dropped_cues": rows,
        "held_at": held_at,
    }
    if artifacts:
        receipt["review_artifacts"] = dict(artifacts)
    record["unreadable_cue_review"] = receipt
    return receipt


def is_unreadable_cue_review_hold(record: Mapping[str, object]) -> bool:
    return (
        record.get("status") == UNREADABLE_CUE_REVIEW_STATUS
        and isinstance(record.get("unreadable_cue_review"), Mapping)
    )


def pending_unreadable_cue_review_rows(
    picks: Collection[object], *, exact_ids: Collection[str] = ()
) -> list[dict]:
    return [
        row
        for row in picks
        if isinstance(row, dict)
        and row.get("status") == UNREADABLE_CUE_REVIEW_STATUS
        and (not exact_ids or _candidate_id(row) in exact_ids)
    ]


def report_notes(
    picks: Collection[object], *, exact_ids: Collection[str] = ()
) -> list[str]:
    rows = pending_unreadable_cue_review_rows(picks, exact_ids=exact_ids)
    if not rows:
        return []
    return [f"{len(rows)} 条删过不可读字幕等待人工审阅（停泊，禁传）"]


def _drop_cells(row: Mapping[str, object]) -> list[str]:
    receipt = row.get("unreadable_cue_review")
    receipt = receipt if isinstance(receipt, Mapping) else {}
    cells: list[str] = []
    for drop in receipt.get("dropped_cues") or []:
        if not isinstance(drop, Mapping):
            continue
        if drop.get("schema_version") == "unreadable-cue-drop-unverifiable.v1":
            cells.append(
                f"⚠️ 删除回执不可核验（{drop.get('status')}）：{drop.get('receipt_path')}"
            )
            continue
        start = drop.get("matched_start_ms")
        end = drop.get("matched_end_ms")
        cells.append(
            f"cue {drop.get('cue_index')} "
            f"[{_ms(start)}→{_ms(end)}] "
            f"「{drop.get('deleted_text')}」 "
            f"· {drop.get('witness_reason_code')}：{drop.get('witness_detail')}"
        )
    return cells


def _ms(value: object) -> str:
    if isinstance(value, bool) or not isinstance(value, int):
        return "?"
    return f"{value // 1000}.{value % 1000:03d}s"


def render_report_section(
    picks: Collection[object], *, exact_ids: Collection[str] = ()
) -> list[str]:
    """维护者 一眼能看到「这条少了一句话，是因为它物理上听不清」的那张表。"""

    rows = pending_unreadable_cue_review_rows(picks, exact_ids=exact_ids)
    if not rows:
        return []
    lines = [
        "",
        "## 删过不可读字幕，等待人工审阅（停泊态，**不是**拒绝，也**不可上传**）",
        "",
        "> 声学证人判定这几段音频**物理上不可读**（例：11 个音节塞进 0.92s，"
        "超出普通话音节率上限），既不是「机器没跑」也不是「内容有问题」。按 维护者 "
        "2026-08-10 裁定：删掉那一句字幕、照常出成品、落停泊态等人看，而不是把整条"
        "候选拦死。**删除是有损的**——下表逐条列出删了哪一段、原文是什么、证人的"
        "原始判据是什么。",
        "",
        "> 这些行**永远不会被自动上传**：状态不在 `DELIVERED_TALK_STATUSES` 里，"
        "进不了日审清单，因此每一条上传路径都拿不到授权。看过之后：认可就人工把"
        "这条改回 `review_ready`（或按现有通道补一句正确文字后重产），认为不该发就"
        "显式改成 `candidate_rejected` 并写明出处。**它不会自己醒**。",
        "",
        "| candidate | hook | 删除段（时间 · 原文 · 证人判据） | 成品 |",
        "|---|---|---|---|",
    ]
    for row in rows:
        receipt = row.get("unreadable_cue_review")
        receipt = receipt if isinstance(receipt, Mapping) else {}
        artifacts = receipt.get("review_artifacts")
        artifacts = artifacts if isinstance(artifacts, Mapping) else {}
        cells = _drop_cells(row) or ["—"]
        lines.append(
            f"| `{_candidate_id(row) or '?'}` "
            f"| {row.get('hook') or '—'} "
            f"| {'<br>'.join(cells)} "
            f"| {artifacts.get('burned_video') or '—'} |"
        )
    return lines
