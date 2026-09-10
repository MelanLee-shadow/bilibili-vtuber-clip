"""OSS compatibility stub: no private original-preservation authorization is distributed."""
from __future__ import annotations

SCHEMA = "original-preserved-final-review.v1"


def build_preserved_review(*_args, **_kwargs):
    raise ValueError("PRIVATE_ORIGINAL_PRESERVATION_UNAVAILABLE")


def validate_preserved_review(*_args, **_kwargs):
    from src.autoslice.final_review_contract import FinalReviewContractError

    raise FinalReviewContractError("PRIVATE_ORIGINAL_PRESERVATION_UNAVAILABLE")
