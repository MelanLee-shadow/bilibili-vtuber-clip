"""Narrow v5 authority for one marker-bound subtitle rejection shape."""

from __future__ import annotations

import re
from collections.abc import Mapping

from src.autoslice.runner_proxy import RunnerProxy


REVIEW_STATE_FIELD = "published_topic_dedup_review"
REVIEW_STATE_SCHEMA = "published-topic-dedup-review-state.v1"
STALE_REVIEW_STATUS = "HUMAN_TOPIC_DEDUP_REVIEW_AUTHORITY_STALE"
RECOVERY_RELEASED_RETRY_PENDING = "RELEASED_RETRY_PENDING"
RECOVERY_CONVERGED = "CONVERGED"
RECOVERY_BLOCKED = "BLOCKED"

_SHA256_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")
_REJECTION_REASON = "subtitle_authority_unresolved_backfilled"
_REJECTION_STAGE = "chat_authority_final_artifact"
_STALE_HOLD_ERROR = (
    "PublishedTopicCollisionError: current scorecard refresh receipt is missing"
)
_STALE_HOLD_KEYS = frozenset(
    {
        "candidate_id",
        "disposition",
        "reason_code",
        "score_mutated",
        "suppression_authorized",
        "upload_authorized",
        "queue_origin",
        "candidate",
        "evidence",
    }
)


def _candidate_id(row: Mapping[str, object]) -> str:
    return str(row.get("candidate_id") or row.get("cid") or "")


def is_selected_subtitle_authority_rejection(
    row: Mapping[str, object],
) -> bool:
    """Recognize only the selected 806-style final-artifact rejection."""

    rc = row.get("rc")
    return bool(
        row.get("status") == "candidate_rejected"
        and row.get("rejected_status") == "failed"
        and row.get("selected_repair") is True
        and isinstance(rc, int)
        and not isinstance(rc, bool)
        and rc == 1
        and row.get("failure_kind") == "subtitle_authority"
        and row.get("failure_stage") == _REJECTION_STAGE
        and row.get("failure_recoverable") is False
        and row.get("rejection_reason") == _REJECTION_REASON
    )


def hold_mentions_candidate(hold: object, candidate_id: str) -> bool:
    if not isinstance(hold, Mapping):
        return False
    candidate = hold.get("candidate")
    return _candidate_id(hold) == candidate_id or (
        isinstance(candidate, Mapping) and _candidate_id(candidate) == candidate_id
    )


def _is_exact_redundant_stale_hold(
    hold: object,
    *,
    candidate_id: str,
    marker_head: Mapping[str, object],
) -> bool:
    if not isinstance(hold, Mapping) or set(hold) != _STALE_HOLD_KEYS:
        return False
    candidate = hold.get("candidate")
    return bool(
        hold.get("candidate_id") == candidate_id
        and hold.get("disposition") == STALE_REVIEW_STATUS
        and hold.get("reason_code") == "PUBLISHED_TOPIC_REVIEW_AUTHORITY_STALE"
        and hold.get("score_mutated") is False
        and hold.get("suppression_authorized") is False
        and hold.get("upload_authorized") is False
        and hold.get("queue_origin") is None
        and isinstance(candidate, Mapping)
        and dict(candidate) == dict(marker_head)
        and hold.get("evidence") == {"error": _STALE_HOLD_ERROR}
    )


def _one_target_row(
    state: Mapping[str, object], candidate_id: str
) -> tuple[str, Mapping[str, object]] | None:
    rows = [
        (field, row)
        for field in (
            "pending_talk",
            "talk_backlog",
            "picks",
            "talk_below_confidence_threshold",
        )
        for row in (state.get(field) if isinstance(state.get(field), list) else [])
        if isinstance(row, Mapping) and _candidate_id(row) == candidate_id
    ]
    return rows[0] if len(rows) == 1 else None


def reconcilable_redundant_stale_hold_index(
    state: Mapping[str, object], candidate_id: str
) -> int | None:
    """Purely locate the exact stale hold copied from a selected marker head."""

    review_state = state.get(REVIEW_STATE_FIELD)
    holds = review_state.get("holds") if isinstance(review_state, Mapping) else None
    target = _one_target_row(state, candidate_id)
    if not (
        isinstance(review_state, Mapping)
        and set(review_state) == {"schema_version", "holds"}
        and review_state.get("schema_version") == REVIEW_STATE_SCHEMA
        and isinstance(holds, list)
        and target is not None
        and target[0] == "picks"
        and is_selected_subtitle_authority_rejection(target[1])
    ):
        return None
    matches = [
        index
        for index, hold in enumerate(holds)
        if hold_mentions_candidate(hold, candidate_id)
    ]
    if len(matches) != 1:
        return None
    index = matches[0]
    return (
        index
        if _is_exact_redundant_stale_hold(
            holds[index], candidate_id=candidate_id, marker_head=target[1]
        )
        else None
    )


def review_holds_are_valid(
    state: Mapping[str, object], candidate_id: str
) -> bool:
    """Accept no target hold or one reconcilable target stale hold."""

    review_state = state.get(REVIEW_STATE_FIELD)
    holds = review_state.get("holds") if isinstance(review_state, Mapping) else None
    if not (
        isinstance(review_state, Mapping)
        and set(review_state) == {"schema_version", "holds"}
        and review_state.get("schema_version") == REVIEW_STATE_SCHEMA
        and isinstance(holds, list)
    ):
        return False
    target_holds = [
        hold for hold in holds if hold_mentions_candidate(hold, candidate_id)
    ]
    return not target_holds or (
        len(target_holds) == 1
        and reconcilable_redundant_stale_hold_index(state, candidate_id) is not None
    )


def failure_recovery_fingerprint_disposition(
    row: Mapping[str, object], candidate_id: str
) -> str:
    """Compare one sealed recorded fingerprint to its exact current authority."""

    recorded = row.get("failure_recovery_fingerprint")
    if not isinstance(recorded, str) or _SHA256_RE.fullmatch(recorded) is None:
        return RECOVERY_BLOCKED
    try:
        current = RunnerProxy().talk_failure_recovery_fingerprint(
            row.get("failure_kind"), candidate_id
        )
    except Exception:  # noqa: BLE001 - recovery authority must fail closed
        return RECOVERY_BLOCKED
    if not isinstance(current, str) or _SHA256_RE.fullmatch(current) is None:
        return RECOVERY_BLOCKED
    return (
        RECOVERY_CONVERGED
        if current == recorded
        else RECOVERY_RELEASED_RETRY_PENDING
    )
