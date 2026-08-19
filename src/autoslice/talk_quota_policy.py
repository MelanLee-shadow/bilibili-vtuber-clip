"""Mutually exclusive quota scopes for ordinary talk-candidate selection."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from src.autoslice.game_context import session_game_context_is_resolved
from src.autoslice.segment_scene_context import (
    SegmentSceneContextError,
    validate_segment_scene_context,
)
from src.autoslice.talk_quota_authority import resolve_quota_grant


# 历史锚点，**不是**政策来源：维护者（回忆）「88 这个 3D live 场放宽到
# 15 个」曾被实现成事件 lane 全局常量，与游戏 lane 同型病（一条按日裁定变成所有
# 事件日的默认）。维护者 逐字「追认。88改成15，85。日常还是5，并没有分数
# 限制。」后，15/85 只属于，写在 assets 的授权条目里；没有条目的事件日
# 回落 default_policy 的 5 席/无额外席。
EVENT_TALK_PICK_CAP = 15
EVENT_EXTRA_SLOT_MIN_SCORE = 85.0
_LEGACY_SESSION_ID = "legacy-date-session"
_SESSION_DATE_RX = re.compile(
    r"^live-(\d{8})T(?:\d{6}|unknown)(?:[+-]\d{4})?$"
)
_SEGMENT_DATE_RX = re.compile(r"_(\d{8})-\d{2}-\d{2}-\d{2}$")


@dataclass(frozen=True)
class TalkQuotaPolicy:
    kind: str
    scope_key: str
    cap: int
    extra_slot_min_score: float | None
    recording_date: str | None
    # 出处：``asset:<entry_id>`` / ``asset:default_policy`` /
    # ``default:authority_absent`` / ``default:authority_invalid``。selection
    # 把它写进日志与 state，好让「这条为什么进/不进」可以事后复算。
    policy_source: str = "default:authority_absent"


def _source_segment(item: Mapping[str, object]) -> str | None:
    value = str(item.get("segment_path") or item.get("segment") or "").strip()
    return Path(value).name if value else None


def _date_from_raw(raw: str) -> str:
    return f"{raw[0:4]}-{raw[4:6]}-{raw[6:8]}"


def _validated_scene(item: Mapping[str, object]) -> dict[str, object] | None:
    segment = _source_segment(item)
    if segment is None:
        return None
    try:
        return validate_segment_scene_context(
            item.get("segment_scene_context"), expected_segment=segment
        )
    except SegmentSceneContextError:
        return None


def _recording_date(
    item: Mapping[str, object], scene: Mapping[str, object] | None
) -> str | None:
    if scene is not None:
        value = str(scene.get("recording_date") or "")
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            return value
    segment = _source_segment(item)
    if segment is not None and (match := _SEGMENT_DATE_RX.search(Path(segment).stem)):
        return _date_from_raw(match.group(1))
    session_id = str(item.get("session_id") or "")
    if match := _SESSION_DATE_RX.match(session_id):
        return _date_from_raw(match.group(1))
    return None


def resolve_talk_quota_policy(
    item: Mapping[str, object],
    *,
    state_root: Path,
    default_cap: int = 5,
    authority_path: Path | None = None,
) -> TalkQuotaPolicy:
    """Resolve one candidate to exactly one accounting scope and its quota.

    Scope identity is unchanged: a RESOLVED game date is GAME, then landscape
    event, then ordinary talk.  Policies never stack — an event-looking segment
    on a date already governed by the game authority remains in its GAME scope;
    everything not positively proven is TALK.

    The *numbers* attached to that scope no longer come from lane constants.
    They come from the dated authority asset (``talk_quota_authority``), which
    is matched on ``(recording_date, scope)`` exactly.  A date with no entry
    falls back to the document default (5 席 / 无额外席) and, if even that is
    unavailable, to ``default_cap`` — never to another date's grant and never
    to a lane constant.  维护者(逐字):「追认。88改成15，85。日常还是5，
    并没有分数限制。」
    """

    scene = _validated_scene(item)
    recording_date = _recording_date(item, scene)
    session_id = str(item.get("session_id") or _LEGACY_SESSION_ID)
    if recording_date is not None and session_game_context_is_resolved(
        recording_date, Path(state_root)
    ):
        kind = "game"
    elif scene is not None and scene.get("scene_kind") == "event":
        kind = "event"
    else:
        kind = "talk"
    grant = resolve_quota_grant(
        recording_date, kind, default_cap=default_cap, path=authority_path
    )
    return TalkQuotaPolicy(
        kind,
        f"{kind}:{session_id}",
        grant.cap,
        grant.extra_slot_min_score,
        recording_date,
        grant.source,
    )
