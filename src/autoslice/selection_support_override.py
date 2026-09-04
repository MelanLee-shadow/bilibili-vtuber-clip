"""OSS stub: private operator selection overrides are never redistributed."""
from __future__ import annotations


class SelectionSupportOverrideError(ValueError):
    """Operator-only selection support is unavailable in an OSS snapshot."""


def selection_support_override_applies(*_args, **_kwargs):
    return False


def terminal_selection_support_blocked(*_args, **_kwargs):
    return False


def consume_in_memory(*_args, **_kwargs):
    raise SelectionSupportOverrideError(
        "SELECTION_SUPPORT_PRIVATE_OVERRIDE_UNAVAILABLE"
    )
