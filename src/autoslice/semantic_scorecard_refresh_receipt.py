"""Lossless lifecycle carry for semantic scorecard refresh receipts."""

from __future__ import annotations

from copy import deepcopy
from typing import Mapping


ROW_RECEIPT_KEY = "semantic_evidence_scorecard_refresh"


def copied_refresh_receipt(source: Mapping[str, object]) -> dict[str, object]:
    """Copy an existing opaque receipt; never synthesize or validate it."""

    if ROW_RECEIPT_KEY not in source:
        return {}
    return {ROW_RECEIPT_KEY: deepcopy(source[ROW_RECEIPT_KEY])}


__all__ = ["ROW_RECEIPT_KEY", "copied_refresh_receipt"]
