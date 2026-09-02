"""OSS stub: the private C1 authority is not redistributed."""
from __future__ import annotations


class C1TechnicalReceiptError(ValueError):
    """The private C1 authority is unavailable in an OSS snapshot."""


TITLE = ""


def _unavailable(*_args, **_kwargs):
    raise C1TechnicalReceiptError("C1_PRIVATE_AUTHORITY_UNAVAILABLE")


public_metadata_projection = _unavailable
projection_authority = _unavailable
validate_projection_authority = _unavailable
validate_authorized_projection_manifest = _unavailable
validate_completed = _unavailable
template = _unavailable
load_formal_authority = _unavailable
