"""Bounded start-date and oldest-unfinished backlog policy for the runner."""

from __future__ import annotations

import datetime as dt
import json
import os
import re
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path


START_DATE_ENV = "AUTOSLICE_START_DATE"
# An explicitly bounded soak may walk a small oldest-first backlog.  The
# ordinary Free path keeps its existing latest-three selection when unset.
START_DATE_BACKLOG_LIMIT = 3
START_DATE_TERMINAL_STATUSES = frozenset(
    {
        "review_ready",
        "review_ready_with_failures",
        "no_delivery",
        "ready_unpublished",
        "ready_unpublished_with_failures",
        "published",
        "published_with_failures",
    }
)


def _parse_start_date(raw: str | None = None) -> str | None:
    """Parse the optional lower date bound before any provider work."""

    value = os.environ.get(START_DATE_ENV) if raw is None else str(raw)
    if value in (None, ""):
        return None
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value) is None:
        raise ValueError(f"{START_DATE_ENV} must be an ISO date YYYY-MM-DD")
    try:
        parsed = dt.date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{START_DATE_ENV} must be an ISO date YYYY-MM-DD") from exc
    if parsed.isoformat() != value:
        raise ValueError(f"{START_DATE_ENV} must be an ISO date YYYY-MM-DD")
    return value


def start_date_is_terminal(date: str, state_path: Callable[[str], Path]) -> bool:
    """Return whether a bounded-soak date reached an honest batch end."""

    try:
        state = json.loads(state_path(date).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if not isinstance(state, dict) or state.get("status") not in START_DATE_TERMINAL_STATUSES:
        return False
    # A failed pick may still carry the exact one-shot screenshot-route
    # producer rerun owned by delivery recovery.  Keep that work visible to
    # oldest-first maintenance instead of treating the date as complete.
    picks = state.get("picks")
    if isinstance(picks, list):
        from src.autoslice.talk_recovery_record_policy import (
            cover_route_retry_is_eligible,
        )

        for record in picks:
            if not isinstance(record, Mapping):
                continue
            try:
                queued_route_retry = cover_route_retry_is_eligible(record)
            except (OverflowError, TypeError, ValueError):
                # A malformed marker cannot prove queued work.  The normal
                # delivery path remains responsible for rejecting it.
                queued_route_retry = False
            if queued_route_retry:
                return False
    # A terminal projection must not hide work that was deliberately left in
    # either lane for a later deploy-safe tick.
    return not state.get("pending_talk") and not state.get("pending_song")


def select_start_date_backlog(
    names: Iterable[str],
    start_date: str,
    state_path: Callable[[str], Path],
) -> list[str]:
    """Select the bounded oldest unfinished dates at or after ``start_date``."""

    eligible = [name for name in names if name >= start_date]
    unfinished = {
        date for date in sorted(eligible) if not start_date_is_terminal(date, state_path)
    }
    return sorted(unfinished)[:START_DATE_BACKLOG_LIMIT]
