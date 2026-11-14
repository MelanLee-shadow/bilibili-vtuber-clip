"""Classify recorded focused-review failures before resuming the original request; read the targets of a focused request."""
from __future__ import annotations

import json
from typing import TYPE_CHECKING

from src.autoslice.provider_failure_class import QUOTA, SERVICE, classify_provider_failure
from .adapters import CPA_TEXT_TRIES

if TYPE_CHECKING:
    from .state import CpaEvent


def may_resume_focused(event: CpaEvent) -> bool:
    attempts = event.get("attempts")
    if type(attempts) is not int or attempts < 1:
        return False
    budget = event.get("budget_attempts")
    if budget is not None and (type(budget) is not int or budget < 0 or budget >= CPA_TEXT_TRIES):
        return False
    # Legacy state omitted the budget and HTTP log.  The ordinary client must inspect its durable journal;
    # guessing from the total here would block a mixture of quota refusals and unknown outcomes.
    reason = str(event.get("reason") or "")
    return ((budget is None and reason == "CPA_TEXT_RETRY_EXHAUSTED")
            or reason.startswith("CPA_QUOTA_REFUSED") or classify_provider_failure(reason) in (SERVICE, QUOTA)
            or reason == "EMPTY_ANSWER" or reason.startswith("NO_TERMINAL_EVENT"))


def valid_held_targets(value: object) -> list[str] | None:
    if (not isinstance(value, list) or not value or not all(isinstance(x, str) and x for x in value)
            or len(set(value)) != len(value)):
        return None
    return value


def held_targets_from_prompt(prompt: str) -> list[str] | None:
    marker = "## 本次只处置的疑点 ID\n"
    if marker not in prompt:
        return None
    try:
        value = json.loads(prompt.split(marker, 1)[1].splitlines()[0])
    except (ValueError, IndexError):
        return None
    return valid_held_targets(value)
