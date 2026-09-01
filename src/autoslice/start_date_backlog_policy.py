"""Bounded start-date and oldest-unfinished backlog policy for the runner."""

from __future__ import annotations

import datetime as dt
import json
import os
import re
from collections.abc import Callable, Iterable
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
    return isinstance(state, dict) and state.get("status") in START_DATE_TERMINAL_STATUSES


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
