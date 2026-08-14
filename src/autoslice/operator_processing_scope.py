"""运维显式把某一天纳入 tick 处理范围的唯一通道（带出处、会自动出圈）。

## 为什么需要

`free_session_autoslice.list_dates()` 只取录像根目录**最新三个**日期，另加两个
例外：`status == "source_incomplete"`，以及 `historical_source_recovery_in_progress`
（后者要求 `state["source_recoveries"]` 非空）。2026-08-10 Ivan 要求
「**把 tier1 的 4 条做了**」并明确「**87 现在需要纳入处理范围**」，但 2026-08-07
早已滑出最新三天窗口，而且它的 `source_recoveries` 是空的——两个既有例外一个都
不成立。仓里此前**没有任何**「运维显式指定某天进处理范围」的通道。

伪造 `source_recoveries` 让它假装成源修复日是编造证据，绝不允许。本模块就是那条
缺失的、显式的、可审计的通道。

## 形态与铁律

- **出处即生效条件**：授权块写在**那一天自己的 state 文件**里（key
  `operator_processing_scope`），字段集严格等值校验，必须携带 Ivan 逐字原话、
  授权时间、理由、针对哪一天、以及**被点名的候选 id**。任何字段缺失/多余/类型
  不对 → 整块失效（fail-closed，不是"部分生效"），reason_code 进日志。
- **收敛即自动退出**：v1 见 `_settled` —— 被点名的候选一旦"有了 picks 行且不再
  排队"，它就算干完了；全部干完，这一天下一个 tick 自动离开窗口，**不需要人回来
  清理**。历史 failed pick 只有携带严格 v2 schema 和
  `RECOVER_NAMED_FAILED_PICKS` 意图的逐字授权才保持未竟；v3-v5 则各自只恢复一种
  点名的 typed Talk hold。这些通道都不绕过恢复、配额或上传门。
- **硬性兜底 `expires_at`**：收敛判据依赖候选真的能被产出。万一它们因为配额/分数门
  根本坐不上席，光靠收敛会让老日期永远赖在窗口里。所以 `expires_at` 是必填项，
  到点无条件失效。
- **blast radius 只有被点名的那一天**：判据只读该日期自己的 state，窗口仍然是
  最新三天 + 既有两个例外 + 本通道点名的那一天。不是把窗口从 3 天改成 N 天。
- **不放宽任何门**：本通道只回答"这一天要不要进 tick 的处理范围"。交付门、上传门、
  配额门（`talk_quota_policy_authority.v1.json` + `talk_quota_freeze`）一字未动。
  上传唯一授权仍然是 `assets/lidousha/publication_registry.v1.json`。本通道能做的
  只有**减少**产出（把没被点名的候选压回 backlog），永远不会多坐一个席位。

## 为什么写 state 而不是仓内资产

`talk_selection_contract`（同族的"这一趟只做这几条"契约）就住在 state 里；
`runner_state_writeback` 的三方合并本来就是为了让带外写入的 state 新增在 tick 的
陈旧内存态面前存活。整备动作由 integrator 持 `runner.lock` 完成。反过来，做成
仓内资产要牵动 profile、模板 profile 对齐、OSS 导出面，blast radius 反而更大。

## 历史日期的判定边界

不要把某次 live state 的席位数、published 数或 backlog 分数写死在本模块。v1 只让仍在
queue 中的点名候选把历史日期带回 tick；v2 只让点名的 recoverable failed pick 进入既有
maintenance/requeue 判定；v3-v5 只准入其各自严格验证过的单一 Talk 恢复形态。它们都不承诺
候选一定坐上席，也不改变
`talk_quota_policy_authority.v1.json`、失败恢复 fingerprint、终态拒绝或人工 hold。

因此 integrator 必须在写 grant 前现场读取该日期 state：点名候选若是终态拒绝、已发布、
`failure_recoverable=false`，或需要另一种 typed recovery authority，本模块会收敛或拒绝，
而不是把它伪造成可重跑项。
"""

from __future__ import annotations

import copy
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone

from src.autoslice.runner_state_writeback import CANDIDATE_COLLECTIONS

GRANT_SCHEMA = "operator-processing-scope-grant.v1"
FAILED_PICK_RECOVERY_GRANT_SCHEMA = "operator-processing-scope-grant.v2"
FAILED_PICK_RECOVERY_INTENT = "RECOVER_NAMED_FAILED_PICKS"
HELD_CURRENT_RERENDER_GRANT_SCHEMA = "operator-processing-scope-grant.v3"
HELD_CURRENT_RERENDER_INTENT = "RERENDER_NAMED_HELD_CURRENT_FOR_REVIEW"
SPEAKER_HOLD_RECOVERY_GRANT_SCHEMA = "operator-processing-scope-grant.v4"
SPEAKER_HOLD_RECOVERY_INTENT = "RECOVER_NAMED_SPEAKER_MANUAL_REVIEW_HOLD"
TOPIC_HOLD_RECOVERY_GRANT_SCHEMA = "operator-processing-scope-grant.v5"
TOPIC_HOLD_RECOVERY_INTENT = "RECOVER_NAMED_RESOLVED_TOPIC_DEDUP_HOLD"
SOURCE_FACT_RECOVERY_GRANT_SCHEMA = "operator-processing-scope-grant.v6"
SOURCE_FACT_RECOVERY_INTENT = "RECOVER_NAMED_SELECTED_SOURCE_FACT_REJECTION"
FINAL_REVIEW_RECOVERY_GRANT_SCHEMA = "operator-processing-scope-grant.v7"
FINAL_REVIEW_RECOVERY_INTENT = "RECOVER_NAMED_SELECTED_FINAL_REVIEW_REJECTION"
DISCLOSURE_SCHEMA = "operator-processing-scope-disclosure.v1"
FAILED_PICK_RECOVERY_DISCLOSURE_SCHEMA = "operator-processing-scope-disclosure.v2"
HELD_CURRENT_RERENDER_DISCLOSURE_SCHEMA = "operator-processing-scope-disclosure.v3"
SPEAKER_HOLD_RECOVERY_DISCLOSURE_SCHEMA = "operator-processing-scope-disclosure.v4"
TOPIC_HOLD_RECOVERY_DISCLOSURE_SCHEMA = "operator-processing-scope-disclosure.v5"
SOURCE_FACT_RECOVERY_DISCLOSURE_SCHEMA = "operator-processing-scope-disclosure.v6"
FINAL_REVIEW_RECOVERY_DISCLOSURE_SCHEMA = "operator-processing-scope-disclosure.v7"
STATE_KEY = "operator_processing_scope"
DISCLOSURE_KEY = "operator_processing_scope_disclosure"
# 出处文本的下限沿用 talk_quota_authority._authority_text 的口径：短于 8 个字符的
# "ok"/"yes" 不是出处。
_MIN_AUTHORITY_TEXT = 8
_DATE_RX = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_SHA256_RX = re.compile(r"^sha256:[0-9a-f]{64}$")
_GRANT_FIELDS = frozenset(
    {
        "schema_version",
        "grant_id",
        "recording_date",
        "reason",
        "candidate_ids",
        "user_authorization",
        "expires_at",
    }
)
_FAILED_PICK_RECOVERY_GRANT_FIELDS = _GRANT_FIELDS | {"intent"}
_HELD_CURRENT_RERENDER_GRANT_FIELDS = _FAILED_PICK_RECOVERY_GRANT_FIELDS | {
    "upload_allowed"
}
_AUTHORIZATION_FIELDS = frozenset({"quote", "timestamp"})
# 候选还"排着队"的两个集合。其余 CANDIDATE_COLLECTIONS 成员（picks / 低信心分
# 落选 / superseded …）都表示这条候选已经被处理过一轮，不再是本授权的未竟工作。
_QUEUED_COLLECTIONS = ("pending_talk", "talk_backlog")
_FINAL_REVIEW_RECEIPT_COLLECTIONS = (
    "pending_talk",
    "talk_backlog",
    "picks",
    "talk_below_confidence_threshold",
    "talk_superseded_attempts",
)
SONG_STATE_COLLECTIONS = (
    "pending_song",
    "song_backlog",
    "song_selection_backlog",
    "songs",
    "song_superseded_attempts",
)
_TERMINAL_TALK_STATUSES = frozenset(
    {"ok", "review_ready", "quarantine", "candidate_rejected", "published"}
)


@dataclass(frozen=True)
class OperatorScopeAdmission:
    """一次运维范围判定的结果（永不抛异常，坏块一律不生效）。"""

    admitted: bool
    reason_code: str
    grant_id: str | None = None
    candidate_ids: tuple[str, ...] = ()
    outstanding_candidate_ids: tuple[str, ...] = ()
    log_line: str | None = None
    detail: str = ""
    disclosure: Mapping[str, object] | None = field(default=None, repr=False)


def _row_candidate_id(row: object) -> str:
    if not isinstance(row, Mapping):
        return ""
    return str(row.get("cid") or row.get("candidate_id") or "").strip()


def _ids_in(state: Mapping[str, object], keys: Sequence[str]) -> set[str]:
    found: set[str] = set()
    for key in keys:
        rows = state.get(key)
        if not isinstance(rows, list):
            continue
        for row in rows:
            candidate_id = _row_candidate_id(row)
            if candidate_id:
                found.add(candidate_id)
    return found


def _has_final_review_recovery_receipt(state: Mapping[str, object]) -> bool:
    return any(
        isinstance(row, Mapping)
        and row.get("selected_final_review_recovery") is not None
        for key in _FINAL_REVIEW_RECEIPT_COLLECTIONS
        for row in (
            state.get(key) if isinstance(state.get(key), list) else []
        )
    )


def _text(value: object, *, minimum: int = 1) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text if len(text) >= minimum else None


def _utc(value: object) -> datetime | None:
    """严格 ISO-8601 且必须带时区；裸本地时间不算出处。"""

    text = _text(value)
    if text is None:
        return None
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        return None
    return moment.astimezone(timezone.utc)


def _validate_grant(block: object) -> tuple[dict[str, object] | None, str]:
    """结构校验。返回 (规范化的块, reason_code)；坏块一律返回 (None, 原因)。"""

    if block is None:
        return None, "ABSENT"
    if not isinstance(block, Mapping):
        return None, "SCHEMA_INVALID"
    schema_version = block.get("schema_version")
    if schema_version == GRANT_SCHEMA:
        expected_fields = _GRANT_FIELDS
        intent = None
    elif schema_version == FAILED_PICK_RECOVERY_GRANT_SCHEMA:
        expected_fields = _FAILED_PICK_RECOVERY_GRANT_FIELDS
        intent = block.get("intent")
        if intent != FAILED_PICK_RECOVERY_INTENT:
            return None, "SCHEMA_INVALID"
    elif schema_version == HELD_CURRENT_RERENDER_GRANT_SCHEMA:
        expected_fields = _HELD_CURRENT_RERENDER_GRANT_FIELDS
        intent = block.get("intent")
        if (
            intent != HELD_CURRENT_RERENDER_INTENT
            or block.get("upload_allowed") is not False
        ):
            return None, "SCHEMA_INVALID"
    elif schema_version == SPEAKER_HOLD_RECOVERY_GRANT_SCHEMA:
        expected_fields = _HELD_CURRENT_RERENDER_GRANT_FIELDS
        intent = block.get("intent")
        if (
            intent != SPEAKER_HOLD_RECOVERY_INTENT
            or block.get("upload_allowed") is not False
        ):
            return None, "SCHEMA_INVALID"
    elif schema_version == TOPIC_HOLD_RECOVERY_GRANT_SCHEMA:
        expected_fields = _HELD_CURRENT_RERENDER_GRANT_FIELDS
        intent = block.get("intent")
        if (
            intent != TOPIC_HOLD_RECOVERY_INTENT
            or block.get("upload_allowed") is not False
        ):
            return None, "SCHEMA_INVALID"
    elif schema_version == SOURCE_FACT_RECOVERY_GRANT_SCHEMA:
        expected_fields = _HELD_CURRENT_RERENDER_GRANT_FIELDS
        intent = block.get("intent")
        if (
            intent != SOURCE_FACT_RECOVERY_INTENT
            or block.get("upload_allowed") is not False
        ):
            return None, "SCHEMA_INVALID"
    elif schema_version == FINAL_REVIEW_RECOVERY_GRANT_SCHEMA:
        expected_fields = _HELD_CURRENT_RERENDER_GRANT_FIELDS
        intent = block.get("intent")
        if (
            intent != FINAL_REVIEW_RECOVERY_INTENT
            or block.get("upload_allowed") is not False
        ):
            return None, "SCHEMA_INVALID"
    else:
        return None, "SCHEMA_INVALID"
    if set(block) != expected_fields:
        return None, "SCHEMA_INVALID"
    grant_id = _text(block.get("grant_id"))
    recording_date = _text(block.get("recording_date"))
    reason = _text(block.get("reason"), minimum=_MIN_AUTHORITY_TEXT)
    if grant_id is None or reason is None:
        return None, "SCHEMA_INVALID"
    if recording_date is None or not _DATE_RX.fullmatch(recording_date):
        return None, "SCHEMA_INVALID"
    raw_ids = block.get("candidate_ids")
    if not isinstance(raw_ids, list) or not raw_ids:
        return None, "SCHEMA_INVALID"
    candidate_ids: list[str] = []
    for value in raw_ids:
        candidate_id = _text(value)
        if candidate_id is None or candidate_id in candidate_ids:
            return None, "SCHEMA_INVALID"
        candidate_ids.append(candidate_id)
    if intent in {
        HELD_CURRENT_RERENDER_INTENT,
        SPEAKER_HOLD_RECOVERY_INTENT,
        TOPIC_HOLD_RECOVERY_INTENT,
        SOURCE_FACT_RECOVERY_INTENT,
        FINAL_REVIEW_RECOVERY_INTENT,
    } and len(candidate_ids) != 1:
        return None, "SCHEMA_INVALID"
    authorization = block.get("user_authorization")
    if (
        not isinstance(authorization, Mapping)
        or set(authorization) != _AUTHORIZATION_FIELDS
        or _text(authorization.get("quote"), minimum=_MIN_AUTHORITY_TEXT) is None
        or _utc(authorization.get("timestamp")) is None
    ):
        # 没有 Ivan 逐字 + 授权时间就没有出处，整块不生效。
        return None, "AUTHORITY_INCOMPLETE"
    expires_at = _utc(block.get("expires_at"))
    if expires_at is None:
        return None, "SCHEMA_INVALID"
    return (
        {
            "schema_version": schema_version,
            "grant_id": grant_id,
            "recording_date": recording_date,
            "reason": reason,
            "candidate_ids": tuple(candidate_ids),
            "intent": intent,
            "quote": str(authorization["quote"]).strip(),
            "authorized_at": _utc(authorization.get("timestamp")),
            "expires_at": expires_at,
            "upload_allowed": block.get("upload_allowed"),
        },
        "OK",
    )


def _named_recoverable_failed_pick(state: Mapping[str, object], candidate_id: str) -> bool:
    """Return true only for one unambiguously typed current failed pick.

    Missing/null ``failure_recoverable`` is the legacy "not explicitly terminal"
    shape already understood by delivery recovery.  Other non-bool values fail
    closed instead of gaining processing scope through truthiness.
    """

    rows = state.get("picks")
    if not isinstance(rows, list):
        return False
    matches = [
        row for row in rows if isinstance(row, Mapping) and _row_candidate_id(row) == candidate_id
    ]
    if len(matches) != 1:
        return False
    row = matches[0]
    return bool(
        row.get("status") == "failed"
        and (
            "failure_recoverable" not in row
            or row.get("failure_recoverable") is None
            or row.get("failure_recoverable") is True
        )
    )


def _settled(
    state: Mapping[str, object],
    candidate_id: str,
    known: set[str],
    *,
    intent: object,
) -> bool:
    """这条候选的活干完了没有。

    干完 = **认识它** 且 **它已经不在队列里**。一条候选离开 `pending_talk` /
    `talk_backlog` 只有一种去处：它已经被产出并记进 `picks`（或被判低信心分/被
    superseded 之类归档）。恢复车道把失败的 pick 重新排队时它会回到 `pending_talk`，
    于是自动重新变成"未竟"——这正是我们要的语义："这条真的处理完了吗"。而恢复
    重排本身有 `TALK_REPAIR_LIFETIME_RETRY_CAP` 封顶，所以这个来回是有界的，不会
    让授权永远活着。
    """

    if candidate_id not in known:
        return False
    if candidate_id in _ids_in(state, _QUEUED_COLLECTIONS):
        return False
    if intent == FAILED_PICK_RECOVERY_INTENT and _named_recoverable_failed_pick(
        state, candidate_id
    ):
        return False
    return True


def _held_current_rerender_outstanding(
    state: Mapping[str, object],
    *,
    date: str,
    candidate_id: str,
    grant_id: str,
) -> tuple[bool | None, str]:
    """Return outstanding/converged, or ``None`` with a fail-closed reason."""

    # Local import avoids the runner -> historical scope -> operator scope ->
    # delivery recovery import cycle.  Admission executes only after the runner
    # has finished importing.
    from src.autoslice.held_current_talk_rerender import (
        BLOCKED,
        CONVERGED,
        inspect_named_held_current_talk_rerender,
    )

    inspection = inspect_named_held_current_talk_rerender(
        date,
        state,
        candidate_id=candidate_id,
        grant_id=grant_id,
    )
    if inspection.outcome == BLOCKED:
        return None, inspection.reason_code
    if inspection.outcome == CONVERGED:
        return False, inspection.reason_code
    return True, inspection.reason_code


def _speaker_hold_recovery_outstanding(
    state: Mapping[str, object], candidate_id: str
) -> tuple[bool | None, str]:
    """Inspect one v4 target without granting a generic failed-pick revival."""

    queued = [
        row
        for key in _QUEUED_COLLECTIONS
        for row in (state.get(key) if isinstance(state.get(key), list) else [])
        if isinstance(row, Mapping) and _row_candidate_id(row) == candidate_id
    ]
    picks = state.get("picks")
    matches = [
        row
        for row in (picks if isinstance(picks, list) else [])
        if isinstance(row, Mapping) and _row_candidate_id(row) == candidate_id
    ]
    if queued:
        if len(queued) != 1 or matches:
            return None, "SPEAKER_MANUAL_REVIEW_TARGET_AMBIGUOUS"
        return True, "SPEAKER_MANUAL_REVIEW_RECOVERY_QUEUED"
    if len(matches) != 1:
        return None, "SPEAKER_MANUAL_REVIEW_TARGET_NOT_UNIQUE"
    row = matches[0]
    if row.get("status") in _TERMINAL_TALK_STATUSES:
        return False, "SPEAKER_MANUAL_REVIEW_RECOVERY_TERMINAL"

    from src.autoslice.speaker_manual_review import (
        SPEAKER_MANUAL_REVIEW_SCHEMA,
        SPEAKER_MANUAL_REVIEW_STATUSES,
    )

    receipt = row.get("speaker_manual_review")
    if not (
        row.get("status") in SPEAKER_MANUAL_REVIEW_STATUSES
        and row.get("failure_kind") == "speaker_evidence"
        and row.get("failure_recoverable") is False
        and isinstance(receipt, Mapping)
        and receipt.get("schema_version") == SPEAKER_MANUAL_REVIEW_SCHEMA
        and receipt.get("status") == "PENDING_HUMAN_REVIEW"
        and receipt.get("held_status") == row.get("status")
        and receipt.get("upload_authorized") is False
    ):
        return None, "SPEAKER_MANUAL_REVIEW_HOLD_INVALID"
    try:
        from src.autoslice.runner_proxy import RunnerProxy

        current = RunnerProxy().talk_failure_recovery_fingerprint(
            "speaker_evidence", candidate_id
        )
    except Exception:  # noqa: BLE001 - authority calculation must fail closed
        return None, "SPEAKER_RECOVERY_FINGERPRINT_UNAVAILABLE"
    recorded = row.get("failure_recovery_fingerprint") or row.get(
        "pipeline_fingerprint"
    )
    if not (
        isinstance(recorded, str)
        and _SHA256_RX.fullmatch(recorded)
        and isinstance(current, str)
        and _SHA256_RX.fullmatch(current)
    ):
        return None, "SPEAKER_RECOVERY_FINGERPRINT_INVALID"
    if recorded == current:
        return None, "SPEAKER_RECOVERY_FINGERPRINT_UNCHANGED"
    return True, "SPEAKER_RECOVERY_FINGERPRINT_CHANGED"


def _topic_hold_ids(state: Mapping[str, object]) -> set[str]:
    """Return candidate ids from structurally recognizable nested topic holds."""

    review = state.get("published_topic_dedup_review")
    if not (
        isinstance(review, Mapping)
        and review.get("schema_version") == "published-topic-dedup-review-state.v1"
    ):
        return set()
    holds = review.get("holds")
    if not isinstance(holds, list):
        return set()
    found: set[str] = set()
    for hold in holds:
        if not isinstance(hold, Mapping):
            continue
        candidate_id = _row_candidate_id(hold)
        candidate = hold.get("candidate")
        if (
            candidate_id
            and isinstance(candidate, Mapping)
            and _row_candidate_id(candidate) == candidate_id
        ):
            found.add(candidate_id)
    return found


def _topic_hold_recovery_outstanding(
    state: Mapping[str, object], candidate_id: str
) -> tuple[bool | None, str]:
    """Inspect one v5 target through its canonical durable release authority."""

    try:
        from src.autoslice.published_topic_collision import (
            RECOVERY_CONVERGED,
            RECOVERY_READY_TO_RELEASE,
            RECOVERY_RELEASED_QUEUED,
            RECOVERY_RELEASED_RETRY_PENDING,
            inspect_published_topic_resolution_recovery,
        )

        disposition = inspect_published_topic_resolution_recovery(state, candidate_id)
    except Exception:  # noqa: BLE001 - authority probe must fail closed
        return None, "TOPIC_DEDUP_RESOLUTION_PROBE_UNAVAILABLE"
    if disposition == RECOVERY_READY_TO_RELEASE:
        return True, "TOPIC_DEDUP_RESOLVED_HOLD_OUTSTANDING"
    if disposition == RECOVERY_RELEASED_QUEUED:
        return True, "TOPIC_DEDUP_RELEASED_QUEUE_OUTSTANDING"
    if disposition == RECOVERY_RELEASED_RETRY_PENDING:
        return True, "TOPIC_DEDUP_RELEASED_RETRY_OUTSTANDING"
    if disposition == RECOVERY_CONVERGED:
        return False, "TOPIC_DEDUP_RECOVERY_TERMINAL"
    return None, "TOPIC_DEDUP_RECOVERY_BLOCKED"


def _source_fact_recovery_outstanding(
    state: Mapping[str, object], *, candidate_id: str, grant_id: str
) -> tuple[bool | None, str]:
    """Inspect the v6 rejection/queue through its strict typed leaf."""

    from src.autoslice.selected_source_fact_recovery import (
        BLOCKED,
        CONVERGED,
        OUTSTANDING,
        READY_TO_REQUEUE,
        inspect_selected_source_fact_recovery,
    )

    inspection = inspect_selected_source_fact_recovery(
        state, candidate_id=candidate_id, grant_id=grant_id
    )
    if inspection.outcome == BLOCKED:
        return None, inspection.reason_code
    if inspection.outcome == CONVERGED:
        return False, inspection.reason_code
    if inspection.outcome in {READY_TO_REQUEUE, OUTSTANDING}:
        return True, inspection.reason_code
    return None, "SELECTED_SOURCE_FACT_RECOVERY_UNKNOWN_DISPOSITION"


def _final_review_recovery_outstanding(
    state: Mapping[str, object], *, candidate_id: str, grant_id: str
) -> tuple[bool | None, str]:
    """Inspect one strict v7 final-review recovery lineage."""

    from src.autoslice.selected_final_review_recovery import (
        BLOCKED,
        CONVERGED,
        OUTSTANDING,
        READY_TO_REQUEUE,
        inspect_selected_final_review_recovery,
    )

    inspection = inspect_selected_final_review_recovery(
        state, candidate_id=candidate_id, grant_id=grant_id
    )
    if inspection.outcome == BLOCKED:
        return None, inspection.reason_code
    if inspection.outcome == CONVERGED:
        return False, inspection.reason_code
    if inspection.outcome in {READY_TO_REQUEUE, OUTSTANDING}:
        return True, inspection.reason_code
    return None, "SELECTED_FINAL_REVIEW_RECOVERY_UNKNOWN_DISPOSITION"


def operator_scope_admission(
    state: Mapping[str, object],
    *,
    date: str | None = None,
    now: datetime | None = None,
) -> OperatorScopeAdmission:
    """这一天要不要因为运维授权而进 tick 的处理范围。

    ``date`` 是这份 state 文件自己的日期。传了就必须与授权块里的
    ``recording_date`` 完全一致——否则一块被复制到别的日子的授权在那边不生效。
    """

    grant, reason_code = _validate_grant(state.get(STATE_KEY))
    if grant is None:
        if reason_code == "ABSENT":
            return OperatorScopeAdmission(False, reason_code)
        return OperatorScopeAdmission(
            False,
            reason_code,
            log_line=f"operator scope grant ignored ({reason_code})",
        )
    grant_id = str(grant["grant_id"])
    candidate_ids: tuple[str, ...] = grant["candidate_ids"]  # type: ignore[assignment]
    intent = grant.get("intent")
    if date is not None and grant["recording_date"] != date:
        return OperatorScopeAdmission(
            False,
            "DATE_MISMATCH",
            grant_id,
            candidate_ids,
            log_line=(
                f"operator scope grant {grant_id} ignored (DATE_MISMATCH: "
                f"grant is for {grant['recording_date']})"
            ),
        )
    moment = now or datetime.now(timezone.utc)
    if intent in {
        TOPIC_HOLD_RECOVERY_INTENT,
        SOURCE_FACT_RECOVERY_INTENT,
        FINAL_REVIEW_RECOVERY_INTENT,
    } and moment >= grant["expires_at"]:  # type: ignore[operator]
        # v5 may inspect repository-bound resolution bytes and v6 computes a
        # live recovery fingerprint.  Expiry wins before either performs I/O.
        return OperatorScopeAdmission(
            False,
            "EXPIRED",
            grant_id,
            candidate_ids,
            candidate_ids,
            log_line=(
                f"operator scope grant {grant_id} EXPIRED at "
                f"{grant['expires_at'].isoformat()} with "  # type: ignore[union-attr]
                f"{len(candidate_ids)}/{len(candidate_ids)} candidate(s) unfinished"
            ),
        )
    known = _ids_in(state, CANDIDATE_COLLECTIONS)
    unknown = (
        ()
        if intent == TOPIC_HOLD_RECOVERY_INTENT
        else tuple(cid for cid in candidate_ids if cid not in known)
    )
    if unknown:
        # 打错字/点名了别的日子的候选：整块不生效。否则一个永远不可能收敛的
        # id 会把这一天永久钉在窗口里——正是"老日期赖着"的那个洞。
        return OperatorScopeAdmission(
            False,
            "UNKNOWN_CANDIDATE",
            grant_id,
            candidate_ids,
            log_line=(
                f"operator scope grant {grant_id} ignored (UNKNOWN_CANDIDATE: {','.join(unknown)})"
            ),
            detail=",".join(unknown),
        )
    if (
        intent in {HELD_CURRENT_RERENDER_INTENT, SPEAKER_HOLD_RECOVERY_INTENT}
        and moment >= grant["expires_at"]  # type: ignore[operator]
    ):
        # Expiry is the hard backstop and must win before registry/source/chat
        # I/O.  Otherwise an unavailable prerequisite could keep a recognizable
        # v3 scope fail-closed forever even after its authority expired.
        return OperatorScopeAdmission(
            False,
            "EXPIRED",
            grant_id,
            candidate_ids,
            candidate_ids,
            log_line=(
                f"operator scope grant {grant_id} EXPIRED at "
                f"{grant['expires_at'].isoformat()} with "  # type: ignore[union-attr]
                f"{len(candidate_ids)}/{len(candidate_ids)} candidate(s) unfinished"
            ),
        )
    if intent == HELD_CURRENT_RERENDER_INTENT:
        is_outstanding, held_reason = _held_current_rerender_outstanding(
            state,
            date=str(grant["recording_date"]),
            candidate_id=candidate_ids[0],
            grant_id=grant_id,
        )
        if is_outstanding is None:
            return OperatorScopeAdmission(
                False,
                held_reason,
                grant_id,
                candidate_ids,
                log_line=(
                    f"operator scope grant {grant_id} blocked ({held_reason})"
                ),
                detail=held_reason,
            )
        outstanding = candidate_ids if is_outstanding else ()
    elif intent == SPEAKER_HOLD_RECOVERY_INTENT:
        is_outstanding, speaker_reason = _speaker_hold_recovery_outstanding(
            state, candidate_ids[0]
        )
        if is_outstanding is None:
            return OperatorScopeAdmission(
                False,
                speaker_reason,
                grant_id,
                candidate_ids,
                log_line=f"operator scope grant {grant_id} blocked ({speaker_reason})",
                detail=speaker_reason,
            )
        outstanding = candidate_ids if is_outstanding else ()
    elif intent == TOPIC_HOLD_RECOVERY_INTENT:
        is_outstanding, topic_reason = _topic_hold_recovery_outstanding(
            state, candidate_ids[0]
        )
        if is_outstanding is None:
            return OperatorScopeAdmission(
                False,
                topic_reason,
                grant_id,
                candidate_ids,
                log_line=f"operator scope grant {grant_id} blocked ({topic_reason})",
                detail=topic_reason,
            )
        outstanding = candidate_ids if is_outstanding else ()
    elif intent == SOURCE_FACT_RECOVERY_INTENT:
        is_outstanding, source_fact_reason = _source_fact_recovery_outstanding(
            state,
            candidate_id=candidate_ids[0],
            grant_id=grant_id,
        )
        if is_outstanding is None:
            return OperatorScopeAdmission(
                False,
                source_fact_reason,
                grant_id,
                candidate_ids,
                log_line=(
                    f"operator scope grant {grant_id} blocked ({source_fact_reason})"
                ),
                detail=source_fact_reason,
            )
        outstanding = candidate_ids if is_outstanding else ()
    elif intent == FINAL_REVIEW_RECOVERY_INTENT:
        is_outstanding, final_review_reason = _final_review_recovery_outstanding(
            state,
            candidate_id=candidate_ids[0],
            grant_id=grant_id,
        )
        if is_outstanding is None:
            return OperatorScopeAdmission(
                False,
                final_review_reason,
                grant_id,
                candidate_ids,
                log_line=(
                    f"operator scope grant {grant_id} blocked ({final_review_reason})"
                ),
                detail=final_review_reason,
            )
        outstanding = candidate_ids if is_outstanding else ()
    else:
        outstanding = tuple(
            cid for cid in candidate_ids if not _settled(state, cid, known, intent=intent)
        )
    if moment >= grant["expires_at"]:  # type: ignore[operator]
        return OperatorScopeAdmission(
            False,
            "EXPIRED",
            grant_id,
            candidate_ids,
            outstanding,
            log_line=(
                f"operator scope grant {grant_id} EXPIRED at "
                f"{grant['expires_at'].isoformat()} with "  # type: ignore[union-attr]
                f"{len(outstanding)}/{len(candidate_ids)} candidate(s) unfinished"
            ),
        )
    if not outstanding:
        return OperatorScopeAdmission(
            False,
            "CONVERGED",
            grant_id,
            candidate_ids,
            log_line=(
                f"operator scope grant {grant_id} CONVERGED "
                f"({len(candidate_ids)}/{len(candidate_ids)} done) — "
                "date leaves the processing window"
            ),
        )
    disclosure = {
        "schema_version": (
            HELD_CURRENT_RERENDER_DISCLOSURE_SCHEMA
            if intent == HELD_CURRENT_RERENDER_INTENT
            else SPEAKER_HOLD_RECOVERY_DISCLOSURE_SCHEMA
            if intent == SPEAKER_HOLD_RECOVERY_INTENT
            else TOPIC_HOLD_RECOVERY_DISCLOSURE_SCHEMA
            if intent == TOPIC_HOLD_RECOVERY_INTENT
            else SOURCE_FACT_RECOVERY_DISCLOSURE_SCHEMA
            if intent == SOURCE_FACT_RECOVERY_INTENT
            else FINAL_REVIEW_RECOVERY_DISCLOSURE_SCHEMA
            if intent == FINAL_REVIEW_RECOVERY_INTENT
            else FAILED_PICK_RECOVERY_DISCLOSURE_SCHEMA
            if intent == FAILED_PICK_RECOVERY_INTENT
            else DISCLOSURE_SCHEMA
        ),
        "grant_id": grant_id,
        "recording_date": grant["recording_date"],
        "candidate_ids": list(candidate_ids),
        "outstanding_candidate_ids": list(outstanding),
        "quote": grant["quote"],
    }
    if intent in {
        FAILED_PICK_RECOVERY_INTENT,
        HELD_CURRENT_RERENDER_INTENT,
        SPEAKER_HOLD_RECOVERY_INTENT,
        TOPIC_HOLD_RECOVERY_INTENT,
        SOURCE_FACT_RECOVERY_INTENT,
        FINAL_REVIEW_RECOVERY_INTENT,
    }:
        disclosure["intent"] = intent
    if intent in {
        HELD_CURRENT_RERENDER_INTENT,
        SPEAKER_HOLD_RECOVERY_INTENT,
        TOPIC_HOLD_RECOVERY_INTENT,
        SOURCE_FACT_RECOVERY_INTENT,
        FINAL_REVIEW_RECOVERY_INTENT,
    }:
        disclosure["upload_allowed"] = False
    return OperatorScopeAdmission(
        True,
        "ADMITTED",
        grant_id,
        candidate_ids,
        outstanding,
        log_line=(
            f"operator scope grant {grant_id} admits this date "
            f"({len(outstanding)}/{len(candidate_ids)} candidate(s) outstanding: "
            f"{','.join(outstanding)})"
        ),
        disclosure=disclosure,
    )


def operator_talk_scope(
    state: Mapping[str, object],
    *,
    date: str,
    now: datetime | None = None,
) -> tuple[str, ...] | None:
    """Freeze one admitted operator grant as a Talk-only allowlist.

    v1 admits named queued Talk work; v2 admits named recoverable failed Talk
    picks; v3-v6 admit one narrowly typed Talk recovery each.  None is an
    authority to discover, refill, recover, or produce Song work from the same
    date.  Callers must compute this once at tick entry and retain the tuple for
    the whole tick.  Recomputing after a named candidate reaches a terminal row
    could make the grant ``CONVERGED`` mid-tick and accidentally restore
    ordinary backfill.
    """

    moment = now or datetime.now(timezone.utc)
    block = state.get(STATE_KEY)
    if _has_final_review_recovery_receipt(state) and not (
        isinstance(block, Mapping)
        and block.get("schema_version") == FINAL_REVIEW_RECOVERY_GRANT_SCHEMA
    ):
        # A durable v7 lineage is itself a fail-closed capability marker.  Its
        # queue may not fall through into broad Talk production after the grant
        # is deleted, replaced, or becomes unrecognizable.
        return ()
    if not isinstance(block, Mapping):
        return None
    schema_version = block.get("schema_version")
    if schema_version == GRANT_SCHEMA:
        if "intent" in block:
            return None
    elif schema_version == FAILED_PICK_RECOVERY_GRANT_SCHEMA:
        if block.get("intent") != FAILED_PICK_RECOVERY_INTENT:
            return None
    elif schema_version == HELD_CURRENT_RERENDER_GRANT_SCHEMA:
        if (
            block.get("intent") != HELD_CURRENT_RERENDER_INTENT
            or block.get("upload_allowed") is not False
        ):
            # A recognizable held-current grant must fail closed instead of
            # falling through to ordinary Talk/Song work on a latest-three day.
            return ()
    elif schema_version == SPEAKER_HOLD_RECOVERY_GRANT_SCHEMA:
        if (
            block.get("intent") != SPEAKER_HOLD_RECOVERY_INTENT
            or block.get("upload_allowed") is not False
        ):
            return ()
    elif schema_version == TOPIC_HOLD_RECOVERY_GRANT_SCHEMA:
        if (
            block.get("intent") != TOPIC_HOLD_RECOVERY_INTENT
            or block.get("upload_allowed") is not False
        ):
            return ()
    elif schema_version == SOURCE_FACT_RECOVERY_GRANT_SCHEMA:
        if (
            block.get("intent") != SOURCE_FACT_RECOVERY_INTENT
            or block.get("upload_allowed") is not False
        ):
            return ()
    elif schema_version == FINAL_REVIEW_RECOVERY_GRANT_SCHEMA:
        if (
            block.get("intent") != FINAL_REVIEW_RECOVERY_INTENT
            or block.get("upload_allowed") is not False
        ):
            return ()
    else:
        return None
    admission = operator_scope_admission(state, date=date, now=moment)
    if (
        not admission.admitted
        or not admission.candidate_ids
        or not isinstance(admission.disclosure, Mapping)
    ):
        if (
            schema_version == FINAL_REVIEW_RECOVERY_GRANT_SCHEMA
            and admission.reason_code != "CONVERGED"
        ):
            # A recognizable v7 grant keeps this tick fail-closed even after
            # expiry.  Its durable receipt remains protected from broad
            # maintenance, and expiry must not silently reopen unrelated Talk
            # or Song work on a date that is otherwise still in the live window.
            return ()
        if (
            schema_version
            in {
                HELD_CURRENT_RERENDER_GRANT_SCHEMA,
                SPEAKER_HOLD_RECOVERY_GRANT_SCHEMA,
                TOPIC_HOLD_RECOVERY_GRANT_SCHEMA,
                SOURCE_FACT_RECOVERY_GRANT_SCHEMA,
            }
            and admission.reason_code not in {"CONVERGED", "EXPIRED"}
        ):
            return ()
        return None
    talk_ids = _ids_in(state, ("pending_talk", "talk_backlog", "picks"))
    if schema_version == TOPIC_HOLD_RECOVERY_GRANT_SCHEMA:
        talk_ids.update(_topic_hold_ids(state))
    song_ids = _ids_in(state, SONG_STATE_COLLECTIONS)
    if any(cid not in talk_ids or cid in song_ids for cid in admission.candidate_ids):
        # An admitted but mixed/non-Talk scope must not fall through to broad
        # ordinary processing.  Historical Song needs its own typed authority.
        return ()
    if state.get("talk_selection_contract") is not None:
        # Two independent exact-selection authorities must never be merged.
        # An empty-but-active scope keeps the date Talk-only and performs no
        # work until an operator removes the conflicting contract.
        return ()
    return admission.candidate_ids


def hold_talk_outside_operator_scope(
    state: dict,
    *,
    now: datetime | None = None,
    frozen_candidate_ids: Sequence[str] | None = None,
) -> list[dict]:
    """把没被点名的话题候选压出本 tick 的准入池，返回被压下的行。

    Ivan 2026-08-10 逐字「**把 tier1 的 4 条做了**」——只放这一天进窗口是不够的：
    `prioritize()` 每个 tick 把整份 `talk_backlog` 收回 `pending_talk` 重排，名额
    有富余时 tier-2 会一起坐进席位，白烧几小时机时。

    这个过滤器只做**收窄**：席位数、分数门、Tier 排序、冻结章全都不碰，只是让没被
    点名的候选这一轮不进准入池。调用方必须在 `prioritize()` 收尾时把返回的行交回
    `release_operator_scope_held_talk`，因为 `prioritize()` 最后会整体覆写
    `state["talk_backlog"]`。

    存在 `talk_selection_contract` 时一律不介入：那条精确恢复契约有自己的一整套
    闭环校验，两个"只做这几条"的机制不许互相踩。
    """

    if state.get("talk_selection_contract") is not None:
        state.pop(DISCLOSURE_KEY, None)
        return []
    if frozen_candidate_ids is None:
        admission = operator_scope_admission(state, now=now)
        if not admission.admitted:
            state.pop(DISCLOSURE_KEY, None)
            return []
        allowed = set(admission.candidate_ids)
        disclosure = dict(admission.disclosure or {})
    else:
        candidate_ids = tuple(frozen_candidate_ids)
        if (
            not candidate_ids
            or len(candidate_ids) != len(set(candidate_ids))
            or any(_text(value) != value for value in candidate_ids)
        ):
            raise ValueError("frozen operator Talk scope is invalid")
        allowed = set(candidate_ids)
        block = state.get(STATE_KEY)
        if not isinstance(block, Mapping):
            raise ValueError("frozen operator Talk scope lost its grant")
        authorization = block.get("user_authorization")
        intent = block.get("intent")
        disclosure = {
            "schema_version": (
                HELD_CURRENT_RERENDER_DISCLOSURE_SCHEMA
                if intent == HELD_CURRENT_RERENDER_INTENT
                else SPEAKER_HOLD_RECOVERY_DISCLOSURE_SCHEMA
                if intent == SPEAKER_HOLD_RECOVERY_INTENT
                else TOPIC_HOLD_RECOVERY_DISCLOSURE_SCHEMA
                if intent == TOPIC_HOLD_RECOVERY_INTENT
                else SOURCE_FACT_RECOVERY_DISCLOSURE_SCHEMA
                if intent == SOURCE_FACT_RECOVERY_INTENT
                else FINAL_REVIEW_RECOVERY_DISCLOSURE_SCHEMA
                if intent == FINAL_REVIEW_RECOVERY_INTENT
                else FAILED_PICK_RECOVERY_DISCLOSURE_SCHEMA
                if intent == FAILED_PICK_RECOVERY_INTENT
                else DISCLOSURE_SCHEMA
            ),
            "grant_id": block.get("grant_id"),
            "recording_date": block.get("recording_date"),
            "candidate_ids": list(candidate_ids),
            "outstanding_candidate_ids": list(candidate_ids),
            "quote": (
                authorization.get("quote")
                if isinstance(authorization, Mapping)
                else None
            ),
            "scope_mode": "FROZEN_FOR_TICK_TALK_ONLY",
        }
        if intent is not None:
            disclosure["intent"] = intent
        if intent in {
            HELD_CURRENT_RERENDER_INTENT,
            SPEAKER_HOLD_RECOVERY_INTENT,
            TOPIC_HOLD_RECOVERY_INTENT,
            SOURCE_FACT_RECOVERY_INTENT,
            FINAL_REVIEW_RECOVERY_INTENT,
        }:
            disclosure["upload_allowed"] = False
    pending = state.get("pending_talk")
    if not isinstance(pending, list):
        return []
    keep: list[dict] = []
    held: list[dict] = []
    for item in pending:
        candidate_id = _row_candidate_id(item)
        # 认不出 id 的行不许被本机制吞掉——宁可让它照常走原有判定。
        if candidate_id and candidate_id not in allowed:
            held.append(item)
        else:
            keep.append(item)
    state["pending_talk"] = keep
    disclosure["held_candidate_ids"] = [_row_candidate_id(item) for item in held]
    state[DISCLOSURE_KEY] = disclosure
    return held


def release_operator_scope_held_talk(state: dict, held: Sequence[dict]) -> None:
    """把被压下的候选原样放回 backlog（`prioritize()` 覆写 backlog 之后调用）。"""

    if not held:
        return
    backlog = state.get("talk_backlog")
    if not isinstance(backlog, list):
        backlog = []
    seen = {row_id for row in backlog if (row_id := _row_candidate_id(row))}
    backlog.extend(
        item
        for item in held
        if not (candidate_id := _row_candidate_id(item)) or candidate_id not in seen
    )
    state["talk_backlog"] = backlog


def snapshot_song_state_collections(
    state: Mapping[str, object],
) -> dict[str, tuple[bool, object]]:
    """Freeze the exact Song-lane preimage for one Talk-only tick."""

    return {
        key: (key in state, copy.deepcopy(state.get(key)))
        for key in SONG_STATE_COLLECTIONS
    }


def restore_song_state_collections(
    state: dict, snapshot: Mapping[str, tuple[bool, object]] | None
) -> bool:
    """Restore a frozen Song preimage while retaining unrelated Talk changes."""

    if snapshot is None:
        return False
    changed = False
    for key in SONG_STATE_COLLECTIONS:
        present, value = snapshot[key]
        if present:
            if key not in state or state[key] != value:
                changed = True
            state[key] = copy.deepcopy(value)
        else:
            changed = changed or key in state
            state.pop(key, None)
    return changed
