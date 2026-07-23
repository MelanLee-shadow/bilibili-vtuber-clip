"""Terminal state projection for ordinary and exact recovery batches."""

from __future__ import annotations

import time
from collections.abc import Collection
from typing import Mapping


def project_terminal_batch_state(
    state: dict,
    *,
    delivered_talk_statuses: Collection[object],
    talk_failure_statuses: Collection[object],
    cover_pending_status: object,
    exact_closure: Mapping[str, object],
    retry_epoch: int | None,
) -> dict[str, object]:
    picks = [row for row in state.get("picks", []) if isinstance(row, dict)]
    songs = [row for row in state.get("songs", []) if isinstance(row, dict)]
    delivered_talk = [
        row for row in picks if row.get("status") in delivered_talk_statuses
    ]
    delivered_songs = [row for row in songs if row.get("delivered")]
    repaired = [row for row in delivered_talk if row.get("boundary_repairs")]
    blocked_songs = [row for row in songs if row.get("status") == "blocked"]
    failures = [
        row
        for row in picks + songs
        if row.get("status") in talk_failure_statuses
        or row.get("status") in {"candidate_rejected", cover_pending_status}
    ]

    exact_status = str(exact_closure.get("status") or "")
    if exact_status == "NOT_APPLICABLE":
        state.pop("exact_talk_contract_closure", None)
    else:
        state["exact_talk_contract_closure"] = dict(exact_closure)
    if retry_epoch is None:
        state.pop("next_retry_at_epoch", None)
        state.pop("next_retry_at", None)
    else:
        state["next_retry_at_epoch"] = retry_epoch
        state["next_retry_at"] = time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime(retry_epoch)
        )

    if exact_status == "INCOMPLETE":
        status = "recovery_incomplete"
    elif exact_status == "COMPLETE":
        status = "review_ready"
    elif retry_epoch is not None:
        status = (
            "review_ready_retry_wait"
            if delivered_talk or delivered_songs
            else "retry_wait"
        )
    elif delivered_talk or delivered_songs:
        status = "review_ready_with_failures" if failures else "review_ready"
    else:
        status = "no_delivery"
    state["status"] = status
    return {
        "picks": picks,
        "songs": songs,
        "delivered_talk": delivered_talk,
        "delivered_songs": delivered_songs,
        "repaired": repaired,
        "blocked_songs": blocked_songs,
        "failures": failures,
        "exact_closure": dict(exact_closure),
        "retry_epoch": retry_epoch,
    }
