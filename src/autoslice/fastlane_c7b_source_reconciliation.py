"""OSS stub: private C7b source reconciliation is not redistributed."""
from __future__ import annotations


class C7bSourceReconciliationError(ValueError):
    """The candidate-bound C7b source exception is unavailable."""


def _unavailable(*_args, **_kwargs):
    raise C7bSourceReconciliationError("C7B_PRIVATE_AUTHORITY_UNAVAILABLE")


resolve_c7b_source_reconciliation = _unavailable
resolve_c7b_delivery_start_clamp = _unavailable
resolve_c7b_delivery_end_clamp = _unavailable
