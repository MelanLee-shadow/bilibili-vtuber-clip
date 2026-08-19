"""说话人证据不足 → 人工审阅停泊态，而不是终态判死。

维护者 裁定：「说话人证据不足应该转人工审阅，不是判死」，理由是
**说话人分离是刚开的功能**——用一个新功能的不成熟去毙掉本来可用的内容。

时间线（为什么这条路径今天才咬人）：``34b9b26``(7/12) 建立了证据不足 fail-closed；
``ee29e08``(7/13) 把 ``uniform_host`` 定为交付默认，这条 fail-closed 路径整整
休眠了三周；生产 cron 2026-08-07 起改用 ``AUTOSLICE_SPEAKER_MODE=auto``
（free 上还留着 ``crontab.backup-20260807-speakermode``），路径复活，于是
``auto_220747_1271_1323``/``auto_213135_62_138`` 被判死。本模块只处置这条
复活的路径，不碰 7/13 的 uniform_host 裁定（那条覆盖的是"证据够但不做归属"）。

**停泊 ≠ 放行**，fail-closed 由既有机制承载，本模块不新造一套平行状态：

- 复用既有状态名 ``speaker_review_required`` / ``speaker_evidence_insufficient``。
  它们本来就在 ``delivery_recovery.TALK_RECOVERY_FAILURE_STATUSES`` 里，而
  **从来不在** ``DELIVERED_TALK_STATUSES``（``{"ok","review_ready","quarantine"}``）里
  ——于是天然不可上传、不可 ``review_ready``、进不了日审清单
  （``build_daily_review_manifest.py`` 要求 ``status == "review_ready"``）。
- 既有范式就在同一个函数里：``apply_talk_backfill_rejection_policy`` 的
  exact-recovery 分支下 backfill 被抑制时，这两个状态**本来就**原样留存、
  不改写成 ``candidate_rejected``。本模块把普通车道拉到同一处置而已。
- 出版登记（``publication_registry.py`` 的 ``hold_pending_review``）是**上传**
  的唯一授权门，承载的是仓内已提交资产上的人工裁定；停泊件根本没有产物可传，
  运行时不得代 维护者 往那份登记里写行（7667d9a 血泪：hold 要写仓内资产）。

**为什么不是 ``failure_recoverable=True``**：True 会把它送进基础设施重试车道
（``INFRASTRUCTURE_WAIT_FAILURE_KINDS`` 定时重排），而证据不足重试一万次还是
不足；``True`` 还会让 runner 把整批当外部故障中断。停泊件的唤醒只有两条，
两条都已经在 ``talk_failure_recovery_fingerprint("speaker_evidence", cid)``
的指纹里：说话人模块代码波，或 维护者 落一份 ``candidate_speaker_override_path``
人工覆盖件。指纹不变就一动不动，没有每 tick 空转。
"""

from __future__ import annotations

import time
from collections.abc import Collection, Mapping


SPEAKER_MANUAL_REVIEW_SCHEMA = "speaker-manual-review-hold.v1"

# 两条来源语义不同，但处置同命（见 module docstring 与 report 里的分列）：
#   speaker_review_required          —— 流水线主动要求人看，带 hash-bound cue 清单
#   speaker_evidence_insufficient    —— 主播 clip anchor 不足，只有 failure_message
# 两者 failure_kind 同为 speaker_evidence、修复权威同为人工覆盖件、
# backfill 原因码同为 speaker_identity_unresolved_backfilled。
SPEAKER_MANUAL_REVIEW_STATUSES = frozenset(
    {
        "speaker_review_required",
        "speaker_evidence_insufficient",
    }
)
SPEAKER_BACKFILL_REJECTION_REASON = "speaker_identity_unresolved_backfilled"

_STATUS_LABELS = {
    "speaker_review_required": "流水线要求人工复核（带 cue 清单）",
    "speaker_evidence_insufficient": "主播声纹锚点不足（无 cue 清单）",
}


def _candidate_id(record: Mapping[str, object]) -> str:
    return str(record.get("candidate_id") or record.get("cid") or "")


def park_for_manual_review(
    record: dict,
    *,
    reason: str,
    migrated_from: Mapping[str, object] | None = None,
    guess: Mapping[str, object] | None = None,
    artifacts: Mapping[str, object] | None = None,
) -> dict:
    """给停泊件盖一份自述回执；``status`` 保持它自己的说话人状态不动。

    回执是收据不是状态机（与 ``song_terminal_disposition`` /
    ``backfill_suppressed_by_exact_contract`` 同款惯例）：真正的 fail-closed
    仍由 ``status`` 不在 ``DELIVERED_TALK_STATUSES`` 里承载。

    ``guess``/``artifacts`` 是 第二次裁定（「它必须无论如何至少先猜
    一个说话人，我才能审查」）带来的：停泊件现在**有成品**了。两个字段各有硬用途——
    ``guess`` 让报表一眼说清"这条的说话人是猜的、哪几句最可能错"；``artifacts``
    把成品路径写进 state，free 的容量清理在删 out/媒体前会扫 state 引用，不写
    进去的成品会被当孤儿清掉（2026-07/08 两次误删的血泪）。
    """

    existing = record.get("speaker_manual_review")
    existing = existing if isinstance(existing, Mapping) else {}
    held_at = existing.get("held_at") or time.strftime(
        "%Y-%m-%dT%H:%M:%SZ", time.gmtime()
    )
    # 同一条 pick 会被盖两次章：produce 收尾先盖（带成品与 guess 回执），紧接着
    # 运行时主循环的 backfill 政策按状态又盖一次（只知道原因码）。第二次必须
    # **继承**第一次的成品面，否则 维护者 的审阅入口和防误删引用当场蒸发。
    guess = guess if guess is not None else existing.get("speaker_guess")
    artifacts = artifacts if artifacts is not None else existing.get("review_artifacts")
    held_status = str(record.get("status") or "")
    receipt: dict[str, object] = {
        "schema_version": SPEAKER_MANUAL_REVIEW_SCHEMA,
        "status": "PENDING_HUMAN_REVIEW",
        "disposition": "AWAITING_HUMAN_SPEAKER_REVIEW",
        "held_status": held_status,
        "reason": reason,
        # 显式写死：停泊件永远不是上传授权，也不是 review_ready。
        "upload_authorized": False,
        "review_authority": "HUMAN_OPERATOR",
        "resolution": "SPEAKER_TURN_OVERRIDE_OR_EXPLICIT_REJECTION",
        # 说明唤醒面，免得后人以为它靠定时重试自愈。
        "wakes_on": "speaker_evidence_recovery_fingerprint_change",
        "held_at": held_at,
    }
    if record.get("failure_message"):
        receipt["failure_message"] = str(record["failure_message"])[:600]
    if record.get("speaker_review_manifest"):
        receipt["speaker_review_manifest"] = record["speaker_review_manifest"]
    unresolved = record.get("speaker_review_context_unresolved_cues")
    if isinstance(unresolved, int) and not isinstance(unresolved, bool):
        receipt["context_unresolved_cues"] = unresolved
    if migrated_from is not None:
        receipt["migrated_from"] = dict(migrated_from)
    if guess is not None:
        receipt["speaker_guess"] = dict(guess)
        # 有产物的停泊 vs 完全没产物的停泊：维护者 的审阅动作不同（前者是打开成品
        # 改几句，后者是根本没得看），所以回执里显式分开，不靠调用方猜。
        receipt["disposition"] = "AWAITING_HUMAN_SPEAKER_CORRECTION_ON_GUESSED_DELIVERY"
        receipt["wakes_on"] = "speaker_evidence_recovery_fingerprint_change"
    if artifacts:
        receipt["review_artifacts"] = dict(artifacts)
    record["speaker_manual_review"] = receipt
    return receipt


def is_speaker_manual_review_hold(record: Mapping[str, object]) -> bool:
    return (
        record.get("status") in SPEAKER_MANUAL_REVIEW_STATUSES
        and isinstance(record.get("speaker_manual_review"), Mapping)
    )


def restore_fossilized_speaker_holds(
    state: dict,
    *,
    candidate_ids: Collection[str] | None = None,
) -> int:
    """把 维护者 裁定之前化石化的说话人拒绝行迁回停泊态。

    只认这一种精确形状（与 ``_is_legacy_exact_backfill_rejection`` 同款窄识别）：
    ``candidate_rejected`` + ``rejected_status`` 是两个说话人状态之一 +
    ``rejection_reason == speaker_identity_unresolved_backfilled``。别的拒绝
    一律不碰——这不是通用复活器（那是 ``scripts/revive_rejected_candidates.py``）。
    """

    restored = 0
    allowed = set(candidate_ids) if candidate_ids is not None else None
    for record in state.get("picks") or []:
        if not isinstance(record, dict):
            continue
        if allowed is not None and _candidate_id(record) not in allowed:
            continue
        held_status = record.get("rejected_status")
        if (
            record.get("status") != "candidate_rejected"
            or held_status not in SPEAKER_MANUAL_REVIEW_STATUSES
            or record.get("rejection_reason") != SPEAKER_BACKFILL_REJECTION_REASON
        ):
            continue
        record["status"] = str(held_status)
        record.pop("rejected_status", None)
        record.pop("rejection_reason", None)
        park_for_manual_review(
            record,
            reason=SPEAKER_BACKFILL_REJECTION_REASON,
            migrated_from={
                "schema_version": "speaker-manual-review-migration.v1",
                "status": "candidate_rejected",
                "rejection_reason": SPEAKER_BACKFILL_REJECTION_REASON,
                "authority": "REVIEWER_2026-08-10_SPEAKER_EVIDENCE_GOES_TO_HUMAN_REVIEW",
            },
        )
        restored += 1
    return restored


def pending_speaker_review_rows(
    picks: Collection[object], *, exact_ids: Collection[str] = ()
) -> list[dict]:
    return [
        row
        for row in picks
        if isinstance(row, dict)
        and row.get("status") in SPEAKER_MANUAL_REVIEW_STATUSES
        and (not exact_ids or _candidate_id(row) in exact_ids)
    ]


def report_notes(
    picks: Collection[object], *, exact_ids: Collection[str] = ()
) -> list[str]:
    rows = pending_speaker_review_rows(picks, exact_ids=exact_ids)
    if not rows:
        return []
    return [f"{len(rows)} 条等待人工说话人审阅（停泊，禁传）"]


def render_report_section(
    picks: Collection[object], *, exact_ids: Collection[str] = ()
) -> list[str]:
    """维护者 一眼能看到"这条在等我看"的那张表。"""

    rows = pending_speaker_review_rows(picks, exact_ids=exact_ids)
    if not rows:
        return []
    lines = [
        "",
        "## 等待人工说话人审阅（停泊态，**不是**拒绝，也**不可上传**）",
        "",
        "> 说话人分离是新开的功能，证据不足不判死（维护者 2026-08-10）。这些候选停在"
        "队列里等人看：既不会交付、不会进 `review_ready`、不会被上传，也不会每 tick "
        "空转重试。**唯一的推进面是人**——落一份候选级说话人人工覆盖件（或说话人"
        "模块本身的代码波）会改变 `speaker_evidence` 恢复指纹，下个 tick 自动重产；"
        "看过确认没救就把这行显式改回 `candidate_rejected` 并写明出处。",
        "",
        "> 「成品」一列非空的行**已经有可以打开看的视频和逐句说话人标注**（维护者 "
        "2026-08-10「它必须无论如何至少先猜一个说话人，我才能审查」）。那份归属是"
        "**猜的**：`猜法` 说清这次降到哪一级，`存疑句` 是逐句证据缺口的条数——"
        "改这几句就够，不用整片重标。逐句清单在成品同名的 `.speaker.json` 里"
        "（`speaker_guess.low_confidence_cues`），改完照常走说话人覆盖件通道。",
        "",
        "| candidate | 停泊类型 | hook | 阻塞证据 | cue 清单 | 猜法 | 存疑句 | 成品 |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for row in rows:
        receipt = row.get("speaker_manual_review")
        receipt = receipt if isinstance(receipt, Mapping) else {}
        manifest = receipt.get("speaker_review_manifest") or row.get(
            "speaker_review_manifest"
        )
        unresolved = receipt.get("context_unresolved_cues")
        guess = receipt.get("speaker_guess")
        guess = guess if isinstance(guess, Mapping) else {}
        artifacts = receipt.get("review_artifacts")
        artifacts = artifacts if isinstance(artifacts, Mapping) else {}
        lines.append(
            f"| `{_candidate_id(row) or '?'}` "
            f"| {_STATUS_LABELS.get(str(row.get('status')), str(row.get('status')))} "
            f"| {row.get('hook') or '—'} "
            f"| {str(receipt.get('failure_message') or row.get('failure_message') or '—')[:160]} "
            f"| {(str(manifest) + (f'（未定 cue {unresolved}）' if unresolved else '')) if manifest else '—'} "
            f"| {guess.get('rung_label') or '—'} "
            f"| {guess.get('low_confidence_cue_count') if guess.get('low_confidence_cue_count') is not None else '—'} "
            f"| {artifacts.get('burned_video') or '—'} |"
        )
    return lines
