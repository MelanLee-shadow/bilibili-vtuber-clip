"""Mutually exclusive quota scopes for ordinary talk-candidate selection."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from src.autoslice.game_context import talk_pick_cap
from src.autoslice.segment_scene_context import (
    SegmentSceneContextError,
    validate_segment_scene_context,
)


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
) -> TalkQuotaPolicy:
    """Resolve one candidate to exactly one accounting scope.

    Priority is existing RESOLVED game policy, then landscape event, then
    ordinary talk.  Policies never stack: an event-looking segment on a date
    already governed by the existing game authority remains in its GAME
    scope; everything not positively proven is TALK.

    F13(Ivan 2026-08-09,裁定失落案重申):「我记得我当时说过 88 这个 3D live 场
    放宽到 15 个,然后当天的杂谈场认为是独立的,自然有 5 个」。
    """

    scene = _validated_scene(item)
    recording_date = _recording_date(item, scene)
    session_id = str(item.get("session_id") or _LEGACY_SESSION_ID)
    if recording_date is not None:
        game_cap, game_gate = talk_pick_cap(
            recording_date, Path(state_root), default_cap=default_cap
        )
        if game_gate is not None:
            return TalkQuotaPolicy(
                "game",
                f"game:{session_id}",
                game_cap,
                game_gate,
                recording_date,
            )
    if scene is not None and scene.get("scene_kind") == "event":
        return TalkQuotaPolicy(
            "event",
            f"event:{session_id}",
            EVENT_TALK_PICK_CAP,
            EVENT_EXTRA_SLOT_MIN_SCORE,
            recording_date,
        )
    return TalkQuotaPolicy(
        "talk",
        f"talk:{session_id}",
        default_cap,
        None,
        recording_date,
    )
