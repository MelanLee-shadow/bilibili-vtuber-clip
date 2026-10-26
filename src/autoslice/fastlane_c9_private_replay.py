"""Public original-patch callers have no private C9 source projection."""
from __future__ import annotations


def preserved_c9_source_projection(*_args, **_kwargs):
    return None


def c9_reconciled_boundary_owner_valid(*_args, **_kwargs):
    return False
