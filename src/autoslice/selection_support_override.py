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


_safe_document = consume_in_memory
create_or_apply_selection_support_override = consume_in_memory
prepare_selection_support_override = consume_in_memory
historical_preflight = consume_in_memory
