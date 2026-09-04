"""OSS stub for the private C3 terminal source-fact seam."""
from __future__ import annotations


CANDIDATE_ID = "__oss_private_c3_unavailable__"
DECISION = "C3_PRIVATE_TERMINAL_SOURCE_FACT_UNAVAILABLE"
SCHEMA = ""
REVIEW_SCHEMA = "lidousha-source-fact-review.v1"


class C3TerminalSourceFactPreservationError(ValueError):
    """The private C3 terminal authority is unavailable in OSS."""


def _unavailable(*_args, **_kwargs):
    raise C3TerminalSourceFactPreservationError(
        "C3_PRIVATE_TERMINAL_SOURCE_FACT_UNAVAILABLE"
    )


def validate_review(*_args, **_kwargs):
    return False


load_authority = _unavailable
build_review = _unavailable
