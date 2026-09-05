"""OSS stub: the revoked private C5 start-clamp authority is unavailable."""
from __future__ import annotations


class C5StartClampError(RuntimeError):
    """The private C5 authority is not redistributed in an OSS snapshot."""


C5_ACCEPTANCE_EXPECTATIONS = None
CUE = {}


def finalizer_authority_kwargs(*_args, **_kwargs):
    # The former clamp is revoked; callers receive no runtime override.
    return {}


def _unavailable(*_args, **_kwargs):
    raise C5StartClampError("C5_PRIVATE_AUTHORITY_UNAVAILABLE")


runtime_authority_paths = _unavailable
load_proposal = _unavailable
load_accepted_authority = _unavailable
accepted_delivery_geometry = _unavailable
validate_proposal = _unavailable
validate_accepted_authority = _unavailable
