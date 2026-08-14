"""Collection projection and stable merge helpers for scoped topic holds."""

from __future__ import annotations

import re
from collections.abc import Collection, Mapping


REVIEW_STATE_FIELD = "published_topic_dedup_review"
_CANDIDATE_ID_RX = re.compile(r"[A-Za-z0-9_-]{1,96}\Z")


def _candidate_id(row: Mapping[str, object]) -> str:
    return str(row.get("candidate_id") or row.get("cid") or "")


def current_rows_by_id(
    state: Mapping[str, object],
) -> dict[str, Mapping[str, object]]:
    rows: dict[str, Mapping[str, object]] = {}
    # Queues are newer than historical failed picks and intentionally win.
    for field in ("picks", "talk_backlog", "pending_talk"):
        values = state.get(field)
        if not isinstance(values, list):
            continue
        for row in values:
            if isinstance(row, Mapping) and _candidate_id(row):
                rows[_candidate_id(row)] = row
    return rows


def held_candidates(
    state: Mapping[str, object],
) -> dict[str, Mapping[str, object]]:
    current = state.get(REVIEW_STATE_FIELD)
    holds = current.get("holds") if isinstance(current, Mapping) else None
    result: dict[str, Mapping[str, object]] = {}
    for hold in holds if isinstance(holds, list) else []:
        candidate = hold.get("candidate") if isinstance(hold, Mapping) else None
        if isinstance(candidate, Mapping) and _candidate_id(candidate):
            result[_candidate_id(candidate)] = candidate
    return result


def prior_hold_records(
    state: Mapping[str, object],
) -> dict[str, Mapping[str, object]]:
    return {
        _candidate_id(hold): hold
        for hold in prior_holds_in_order(state)
        if _candidate_id(hold)
    }


def prior_holds_in_order(
    state: Mapping[str, object],
) -> list[Mapping[str, object]]:
    current = state.get(REVIEW_STATE_FIELD)
    holds = current.get("holds") if isinstance(current, Mapping) else None
    return [hold for hold in holds or [] if isinstance(hold, Mapping)] if isinstance(holds, list) else []


def normalized_candidate_allowlist(
    candidate_ids: Collection[str] | None,
) -> set[str] | None:
    if candidate_ids is None:
        return None
    if isinstance(candidate_ids, (str, bytes)):
        raise ValueError("published topic candidate allowlist is invalid")
    values = tuple(candidate_ids)
    if (
        len(values) != len(set(values))
        or any(
            not isinstance(value, str)
            or _CANDIDATE_ID_RX.fullmatch(value) is None
            for value in values
        )
    ):
        raise ValueError("published topic candidate allowlist is invalid")
    return set(values)


def merge_scoped_holds(
    prior_holds: list[Mapping[str, object]],
    refreshed_holds: list[dict[str, object]],
    *,
    candidate_ids: set[str] | None,
) -> list[dict[str, object]]:
    if candidate_ids is None:
        return refreshed_holds
    refreshed = {
        _candidate_id(hold): hold
        for hold in refreshed_holds
        if _candidate_id(hold) in candidate_ids
    }
    merged: list[dict[str, object]] = []
    consumed: set[str] = set()
    for hold in prior_holds:
        candidate_id = _candidate_id(hold)
        if candidate_id not in candidate_ids:
            # Preserve the exact nested object and its relative order.
            merged.append(hold if isinstance(hold, dict) else dict(hold))
        elif candidate_id in refreshed and candidate_id not in consumed:
            merged.append(refreshed[candidate_id])
            consumed.add(candidate_id)
    merged.extend(
        hold
        for hold in refreshed_holds
        if (
            _candidate_id(hold) in candidate_ids
            and _candidate_id(hold) not in consumed
        )
    )
    return merged
