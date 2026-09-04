"""OSS stub for the private C3 source-fact supersession seam."""
from __future__ import annotations


CANDIDATE_ID = "__oss_private_c3_unavailable__"
RECORDING_DATE = ""
DECISION = "C3_PRIVATE_SOURCE_FACT_SUPERSESSION_UNAVAILABLE"
SCHEMA = ""


class C3SourceFactSupersessionError(ValueError):
    """The private C3 source-fact authority is unavailable in OSS."""


def _unavailable(*_args, **_kwargs):
    raise C3SourceFactSupersessionError(
        "C3_PRIVATE_SOURCE_FACT_SUPERSESSION_UNAVAILABLE"
    )


def validate_c3_source_fact_review_from_validation(*_args, **_kwargs):
    return False


load_authority = _unavailable
build_c3_source_fact_supersession = _unavailable
validate_c3_source_fact_supersession = _unavailable
