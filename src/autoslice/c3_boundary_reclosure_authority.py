"""OSS stub for the private C3 boundary reclosure authority."""
from __future__ import annotations


class C3BoundaryReclosureError(ValueError):
    """The private C3 boundary authority is unavailable in OSS."""


def _unavailable(*_args, **_kwargs):
    raise C3BoundaryReclosureError(
        "C3_PRIVATE_BOUNDARY_AUTHORITY_UNAVAILABLE"
    )


def validate_derived_boundary_receipt(*_args, **_kwargs):
    # Generic callers must fail closed without a private receipt.
    return False


load_c3_boundary_authority = _unavailable
validate_authority_document = _unavailable
derive_current_boundary_review = _unavailable
