"""OSS stub for the private C3 replay-stage geometry hook."""
from __future__ import annotations


class C3PrivateStageAuthorityError(ValueError):
    """The private C3 stage authority is unavailable in OSS."""


C3_PRIVATE_STAGE_REASON = "C3_PRIVATE_STAGE_AUTHORITY_UNAVAILABLE"


def resolve_c3_private_stage_geometry(*_args, **_kwargs):
    # Public candidates retain the generic source interval; no private
    # geometry or candidate-specific fallback is exposed here.
    return None
