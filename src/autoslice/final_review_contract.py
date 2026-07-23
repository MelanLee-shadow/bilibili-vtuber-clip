"""Fail-closed release contract for the exact final subtitle bytes."""

from __future__ import annotations

import re
from typing import Mapping


SCHEMA_VERSION = "final-review-audit.v2"
_SHA256_RX = re.compile(r"^sha256:[0-9a-f]{64}$")


class FinalReviewContractError(ValueError):
    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__(reason_code)


def validate_final_review_release(
    audit: object,
    *,
    expected_srt_sha256: str | None = None,
) -> dict[str, object]:
    """Validate a positive receipt; every other state is a release block."""

    if not isinstance(audit, Mapping):
        raise FinalReviewContractError("FINAL_REVIEW_AUDIT_MISSING_OR_INVALID")
    if audit.get("schema_version") != SCHEMA_VERSION:
        raise FinalReviewContractError("FINAL_REVIEW_AUDIT_SCHEMA_INVALID")
    reviewed = str(audit.get("reviewed_srt_sha256") or "")
    if _SHA256_RX.fullmatch(reviewed) is None:
        raise FinalReviewContractError("FINAL_REVIEW_SRT_BINDING_INVALID")
    if expected_srt_sha256 is not None and reviewed != expected_srt_sha256:
        raise FinalReviewContractError("FINAL_REVIEW_SRT_BINDING_MISMATCH")
    discovery = audit.get("discovery")
    if (
        not isinstance(discovery, Mapping)
        or discovery.get("status") != "COMPLETE"
    ):
        raise FinalReviewContractError("FINAL_REVIEW_DISCOVERY_INCOMPLETE")
    findings = audit.get("findings")
    if not isinstance(findings, list):
        raise FinalReviewContractError("FINAL_REVIEW_FINDINGS_CONTRACT_INVALID")
    validated_count = audit.get("validated_finding_count")
    if (
        isinstance(validated_count, bool)
        or not isinstance(validated_count, int)
        or validated_count != len(findings)
    ):
        raise FinalReviewContractError("FINAL_REVIEW_FINDINGS_CONTRACT_INVALID")
    if audit.get("release_gate") != "PASS" or audit.get("status") != "CLEAN":
        raise FinalReviewContractError("FINAL_REVIEW_RELEASE_GATE_BLOCKED")
    if findings:
        raise FinalReviewContractError("FINAL_REVIEW_UNRESOLVED_FINDINGS")
    boundary = audit.get("boundary_semantic_review")
    if not isinstance(boundary, Mapping) or boundary.get("status") != "PASS":
        raise FinalReviewContractError("FINAL_REVIEW_BOUNDARY_SEMANTIC_BLOCKED")
    return dict(audit)
