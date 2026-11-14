"""The deliberate-refusal exception of the local input/evidence contracts, as an import-light leaf.

review_errors re-exports it; modules that only need to raise it import it from here instead of
through the review taxonomy, whose lazy imports reach the whole review stack.
"""
from __future__ import annotations


class ContractRejection(ValueError):
    """A deliberate refusal by a local input/evidence contract."""

    def __init__(self, reason_code: str, detail: str = "", *,
                 material_recovery: object = None) -> None:
        super().__init__(detail or reason_code)
        self.reason_code = reason_code
        self.detail = detail
        self.material_recovery = material_recovery
