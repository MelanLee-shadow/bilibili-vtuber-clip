"""OSS compatibility stub: private C10 operator successor is unavailable."""
from __future__ import annotations

SCHEMA_VERSION = "c10-operator-final-review-successor.v1"


class C10OperatorReviewSuccessorError(ValueError):
    def __init__(self, reason_code: str = "C10_PRIVATE_AUTHORITY_UNAVAILABLE") -> None:
        self.reason_code = reason_code
        super().__init__(reason_code)


def _unavailable(*_args, **_kwargs):
    raise C10OperatorReviewSuccessorError()


build_c10_operator_review_successor = _unavailable
build_c10_operator_review_successor_report = _unavailable
validate_c10_operator_review_successor = _unavailable
