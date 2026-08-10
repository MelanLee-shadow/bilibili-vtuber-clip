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
- **收敛即自动退出**：见 `_settled` —— 被点名的候选一旦"有了 picks 行且不再排队"，
  它就算干完了；全部干完，这一天下一个 tick 自动离开窗口，**不需要人回来清理**。
- **硬性兜底 `expires_at`**：收敛判据依赖候选真的能被产出。万一它们因为配额/分数门
  根本坐不上席（2026-08-07 就是这种情况，见下），光靠收敛会让老日期永远赖在窗口
  里——既有注释点名过这个风险。所以 `expires_at` 是必填项，到点无条件失效。
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

## 2026-08-07 的实测前提（写在这里免得下一个人重踩）

按 free 上 8/7 的真实 state：该场 quota scope 是 `game:live-20260807Tunknown`，
`cap=10`、`extra_slot_min_score=85`，已 produced 5 席（3 published + 1 review_ready
+ 1 media_ready_cover_pending），4 条 failed 行的 `talk_repair_retry_count` 都是
6/7，早过 `TALK_REPAIR_LIFETIME_RETRY_CAP=3`，`reserved_for_revival=0`。于是新准入
的第一个位次就是 6 > `MAX_TALK_PICKS=5`，**每一个**新候选都要过 85 分门；而
`talk_backlog` 里最高分只有 82.75，四条 tier-1 是 75.5/72.25/70.5/69.5。

**结论：光把 8/7 放进窗口，一条也坐不上席。** 要真的产出那四条，必须由 Ivan 在
`assets/lidousha/talk_quota_policy_authority.v1.json` 里改 2026-08-07 那条的分数门
（带他的逐字出处）——那是既有的、正确的配额通道，不在本模块的权限之内。
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone

from src.autoslice.runner_state_writeback import CANDIDATE_COLLECTIONS

GRANT_SCHEMA = "operator-processing-scope-grant.v1"
DISCLOSURE_SCHEMA = "operator-processing-scope-disclosure.v1"
STATE_KEY = "operator_processing_scope"
DISCLOSURE_KEY = "operator_processing_scope_disclosure"
# 出处文本的下限沿用 talk_quota_authority._authority_text 的口径：短于 8 个字符的
# "ok"/"yes" 不是出处。
_MIN_AUTHORITY_TEXT = 8
_DATE_RX = re.compile(r"^\d{4}-\d{2}-\d{2}$")
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
_AUTHORIZATION_FIELDS = frozenset({"quote", "timestamp"})
# 候选还"排着队"的两个集合。其余 CANDIDATE_COLLECTIONS 成员（picks / 低信心分
# 落选 / superseded …）都表示这条候选已经被处理过一轮，不再是本授权的未竟工作。
_QUEUED_COLLECTIONS = ("pending_talk", "talk_backlog")


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
    if not isinstance(block, Mapping) or set(block) != _GRANT_FIELDS:
        return None, "SCHEMA_INVALID"
    if block.get("schema_version") != GRANT_SCHEMA:
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
            "grant_id": grant_id,
            "recording_date": recording_date,
            "reason": reason,
            "candidate_ids": tuple(candidate_ids),
            "quote": str(authorization["quote"]).strip(),
            "authorized_at": _utc(authorization.get("timestamp")),
            "expires_at": expires_at,
        },
        "OK",
    )


def _settled(state: Mapping[str, object], candidate_id: str, known: set[str]) -> bool:
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
    return candidate_id not in _ids_in(state, _QUEUED_COLLECTIONS)


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
    known = _ids_in(state, CANDIDATE_COLLECTIONS)
    unknown = tuple(cid for cid in candidate_ids if cid not in known)
    if unknown:
        # 打错字/点名了别的日子的候选：整块不生效。否则一个永远不可能收敛的
        # id 会把这一天永久钉在窗口里——正是"老日期赖着"的那个洞。
        return OperatorScopeAdmission(
            False,
            "UNKNOWN_CANDIDATE",
            grant_id,
            candidate_ids,
            log_line=(
                f"operator scope grant {grant_id} ignored (UNKNOWN_CANDIDATE: "
                f"{','.join(unknown)})"
            ),
            detail=",".join(unknown),
        )
    outstanding = tuple(
        cid for cid in candidate_ids if not _settled(state, cid, known)
    )
    moment = now or datetime.now(timezone.utc)
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
        disclosure={
            "schema_version": DISCLOSURE_SCHEMA,
            "grant_id": grant_id,
            "recording_date": grant["recording_date"],
            "candidate_ids": list(candidate_ids),
            "outstanding_candidate_ids": list(outstanding),
            "quote": grant["quote"],
        },
    )


def hold_talk_outside_operator_scope(
    state: dict, *, now: datetime | None = None
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

    admission = operator_scope_admission(state, now=now)
    if not admission.admitted:
        state.pop(DISCLOSURE_KEY, None)
        return []
    if state.get("talk_selection_contract") is not None:
        state.pop(DISCLOSURE_KEY, None)
        return []
    allowed = set(admission.candidate_ids)
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
    disclosure = dict(admission.disclosure or {})
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
