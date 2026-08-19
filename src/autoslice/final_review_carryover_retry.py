"""Bounded replay policy for exact-final repaired-finding carryover."""

from __future__ import annotations

import re
from collections.abc import Mapping


FINAL_REVIEW_CARRYOVER_RETRY_CAP = 8
_PIPELINE_FINGERPRINT_RX = re.compile(r"sha256:[0-9a-f]{64}\Z")


def unconsumed_final_review_carryover(
    record: Mapping[str, object],
) -> str | None:
    fingerprint = record.get("failure_fingerprint")
    consumed = record.get("final_review_carryover_consumed_fingerprints")
    consumed_fingerprints = (
        [value for value in consumed if isinstance(value, str)]
        if isinstance(consumed, list)
        else []
    )
    if (
        record.get("status") != "failed"
        or record.get("failure_recoverable") is not True
        or record.get("failure_stage") != "final_review_carryover"
        or not isinstance(fingerprint, str)
        or _PIPELINE_FINGERPRINT_RX.fullmatch(fingerprint) is None
        or fingerprint in consumed_fingerprints
        or len(consumed_fingerprints) >= FINAL_REVIEW_CARRYOVER_RETRY_CAP
    ):
        return None
    return fingerprint
