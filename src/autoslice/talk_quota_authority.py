"""按录制日期的话题配额授权面——cap 与额外席位分数门的唯一政策来源。

2026-08-08 `4af4a88` 事故：Ivan「8.8切片配额到20条，分数在85分以上即可」是一条
**按日期**的裁定，却被写进了游戏 lane 的全局常量（`GAME_SESSION_TALK_PICK_CAP`
10→20、`GAME_SESSION_EXTRA_SLOT_MIN_SCORE` 90→85）。8/8 的 session_game_context
是 NO_MATCH，游戏 lane 根本不触发——那次改动**没管到 8/8**，却把全库唯一
RESOLVED 的游戏日 **8/7 回溯放宽了**，四条 89.0/87.25/86.75/86.0 因此进席
（考据 `docs/reviews/2026-08-10-talk-pick-quota-forensics.md`）。

根因不是那两个数字，而是 cap 与分数门**每个 tick 由常量实时重算**、state 里没有
「该候选准入时生效的政策」——于是任何一次常量改动都能回溯改写一个已经裁定过的
日子的合法性，且无人察觉。

治法两件，缺一不可：
1. 本资产：按 `recording_date` + `scope` 的显式授权条目，每条自带 Ivan 逐字出处；
2. `talk_quota_freeze`：候选拿到席位时冻结当时生效的政策，后续 tick 复用冻结值。

**fail-closed**：资产缺该日期条目 → 回落代码默认（5 席 / 无额外席），
**绝不**回落到「最近一次全局常量」或任何别的日期的条目。
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from src.autoslice.channel_profile import ChannelProfileError
from src.autoslice.channel_profile import load_channel_profile as _load_channel_profile

QUOTA_AUTHORITY_SCHEMA = "lidousha-talk-quota-policy-authority.v1"
PROFILE_ASSET_KEY = "talk_quota_policy_authority"
# 基础席位数（= runner.MAX_TALK_PICKS）。1..5 席从不看分数门；把 cap 抬到 5 以上
# 就是在开「额外席位」，必须同时给出分数门——否则 cap=20/门=null 等于无限放宽。
BASE_TALK_PICK_CAP = 5
SCOPES = frozenset({"game", "event", "talk"})
_DATE_RX = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent


class TalkQuotaAuthorityError(ValueError):
    """The committed quota authority asset is malformed."""


@dataclass(frozen=True)
class TalkQuotaGrant:
    """One resolved quota grant plus the authority it came from."""

    cap: int
    extra_slot_min_score: float | None
    source: str


def _default_authority_path() -> Path | None:
    """Profile-derived asset path; ``None`` for a profile without the key.

    A channel whose profile does not register the asset simply has no dated
    authority — it stays at the code default rather than inheriting anyone
    else's widening.
    """

    try:
        return _load_channel_profile(_REPO_ROOT).asset_file(PROFILE_ASSET_KEY)
    except (ChannelProfileError, OSError, ValueError):
        return None


DEFAULT_AUTHORITY_PATH = _default_authority_path()


def _authority_text(value: object, *, label: str) -> str:
    text = str(value or "").strip()
    if len(text) < 8:
        raise TalkQuotaAuthorityError(f"{label} authority quote missing")
    return text


def _cap(value: object, *, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise TalkQuotaAuthorityError(f"{label} cap invalid")
    return value


def _gate(value: object, *, label: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TalkQuotaAuthorityError(f"{label} extra_slot_min_score invalid")
    if not 0.0 <= float(value) <= 100.0:
        raise TalkQuotaAuthorityError(f"{label} extra_slot_min_score out of range")
    return float(value)


def _policy_body(payload: Mapping[str, object], *, label: str) -> tuple[int, float | None]:
    cap = _cap(payload.get("cap"), label=label)
    gate = _gate(payload.get("extra_slot_min_score"), label=label)
    if cap > BASE_TALK_PICK_CAP and gate is None:
        # 「日常还是5，并没有分数限制」= 普通日没有额外席，不是额外席位门=0。
        raise TalkQuotaAuthorityError(f"{label} grants extra slots without a score gate")
    return cap, gate


def _default_policy_body(payload: Mapping[str, object]) -> tuple[int, float | None]:
    """The document default may only ever be the base seats, never a widening.

    A dateless knob that can widen the whole library is the disease this asset
    exists to cure; ``default_policy`` carries Ivan 2026-08-10「日常还是5，并没有
    分数限制」as authority for the code default, and nothing more.
    """

    cap, gate = _policy_body(payload, label="default_policy")
    if cap > BASE_TALK_PICK_CAP or gate is not None:
        raise TalkQuotaAuthorityError("default_policy may not widen the base cap")
    return cap, gate


def validate_talk_quota_policy_authority(payload: object) -> dict:
    """Structurally validate the authority document; raise on malformation."""

    if not isinstance(payload, Mapping):
        raise TalkQuotaAuthorityError("quota authority payload is not a mapping")
    if payload.get("schema_version") != QUOTA_AUTHORITY_SCHEMA:
        raise TalkQuotaAuthorityError("quota authority schema unsupported")
    _authority_text(payload.get("authority"), label="document")
    default_policy = payload.get("default_policy")
    if not isinstance(default_policy, Mapping):
        raise TalkQuotaAuthorityError("quota authority default_policy missing")
    _default_policy_body(default_policy)
    _authority_text(default_policy.get("authority"), label="default_policy")
    entries = payload.get("entries")
    if not isinstance(entries, list):
        raise TalkQuotaAuthorityError("quota authority entries invalid")
    seen_ids: set[str] = set()
    seen_scopes: set[tuple[str, str]] = set()
    for row in entries:
        if not isinstance(row, Mapping):
            raise TalkQuotaAuthorityError("quota authority entry is not a mapping")
        entry_id = str(row.get("entry_id") or "").strip()
        if not entry_id:
            raise TalkQuotaAuthorityError("quota authority entry_id missing")
        if entry_id in seen_ids:
            raise TalkQuotaAuthorityError(f"quota authority duplicate entry_id {entry_id}")
        seen_ids.add(entry_id)
        recording_date = str(row.get("recording_date") or "")
        if not _DATE_RX.fullmatch(recording_date):
            raise TalkQuotaAuthorityError(f"{entry_id} recording_date invalid")
        scope = str(row.get("scope") or "")
        if scope not in SCOPES:
            raise TalkQuotaAuthorityError(f"{entry_id} scope invalid")
        key = (recording_date, scope)
        if key in seen_scopes:
            raise TalkQuotaAuthorityError(
                f"quota authority duplicate {recording_date}/{scope}"
            )
        seen_scopes.add(key)
        _policy_body(row, label=entry_id)
        _authority_text(row.get("authority"), label=entry_id)
    return dict(payload)


def load_talk_quota_policy_authority(path: Path | None = None) -> dict | None:
    """Load and validate the committed authority, or ``None`` when absent."""

    authority_path = path or DEFAULT_AUTHORITY_PATH
    if authority_path is None or not Path(authority_path).is_file():
        return None
    try:
        payload = json.loads(Path(authority_path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise TalkQuotaAuthorityError("quota authority unreadable") from exc
    return validate_talk_quota_policy_authority(payload)


def resolve_quota_grant(
    recording_date: str | None,
    scope: str,
    *,
    default_cap: int = BASE_TALK_PICK_CAP,
    path: Path | None = None,
) -> TalkQuotaGrant:
    """Return the quota grant in force for one recording date and scope.

    Never raises: a missing, unreadable or malformed authority denies every
    widening instead of taking down the tick, and the refusal is visible in
    ``TalkQuotaGrant.source`` (which selection discloses into state and logs).
    Lookup is an **exact** ``(recording_date, scope)`` match — there is no
    nearest-date, no most-recent-entry and no lane-constant fallback.
    """

    try:
        document = load_talk_quota_policy_authority(path)
    except TalkQuotaAuthorityError:
        return TalkQuotaGrant(default_cap, None, "default:authority_invalid")
    if document is None:
        return TalkQuotaGrant(default_cap, None, "default:authority_absent")
    if recording_date is not None and _DATE_RX.fullmatch(str(recording_date)):
        for row in document["entries"]:
            if row["recording_date"] == recording_date and row["scope"] == scope:
                cap, gate = _policy_body(row, label=str(row["entry_id"]))
                return TalkQuotaGrant(cap, gate, f"asset:{row['entry_id']}")
    # 无该日期条目 → 代码默认与文档默认里更紧的那个，额外席位一律没有。
    # 文档默认只是这条默认的授权出处，不是能悄悄放宽全库的旋钮。
    document_cap, _gate_unused = _default_policy_body(document["default_policy"])
    return TalkQuotaGrant(min(default_cap, document_cap), None, "asset:default_policy")
