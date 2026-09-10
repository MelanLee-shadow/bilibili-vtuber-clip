"""OSS stub: private C7b failed-row adoption is not redistributed."""
from __future__ import annotations


class C7bFailedRowAdoptionError(ValueError):
    """The private C7b adoption receipt is unavailable in an OSS snapshot."""


def _unavailable(*_args, **_kwargs):
    raise C7bFailedRowAdoptionError("C7B_PRIVATE_ADOPTION_UNAVAILABLE")


validate_c7b_failed_row_adoption = _unavailable
resolve_c7b_operator_authority = _unavailable


def c7b_emergency_cpa_repair_allowed(*_args, **_kwargs):
    return False


def _delivery_local_incident_repair_valid(*_args, **_kwargs):
    return False
