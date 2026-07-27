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
    correction_mutations = audit.get("correction_mutation_authority")
    if (
        not isinstance(correction_mutations, Mapping)
        or correction_mutations.get("schema_version")
        != "subtitle-correction-mutation-audit.v1"
        or correction_mutations.get("status") != "PASS"
    ):
        raise FinalReviewContractError(
            "FINAL_REVIEW_CORRECTION_MUTATION_AUTHORITY_INVALID"
        )
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
    if findings:
        raise FinalReviewContractError("FINAL_REVIEW_UNRESOLVED_FINDINGS")
    # Ivan 2026-07-26（无人值守裁定）：`unresolved_findings_disclosed` 只允许
    # 完整走完闭集裁决且策略分支为 KEEP_CURRENT 的条目——它们随包披露、不
    # 阻断交付；混入任何非该形态的条目仍视为合同违规。
    disclosed = audit.get("unresolved_findings_disclosed")
    if disclosed is not None:
        if not isinstance(disclosed, list):
            raise FinalReviewContractError(
                "FINAL_REVIEW_FINDINGS_CONTRACT_INVALID"
            )
        for row in disclosed:
            if not is_keep_current_disclosed(row):
                raise FinalReviewContractError(
                    "FINAL_REVIEW_FINDINGS_CONTRACT_INVALID"
                )
    boundary = audit.get("boundary_semantic_review")
    if not isinstance(boundary, Mapping) or boundary.get("status") != "PASS":
        raise FinalReviewContractError("FINAL_REVIEW_BOUNDARY_SEMANTIC_BLOCKED")
    if _SHA256_RX.fullmatch(
        str(boundary.get("request_sha256") or "")
    ) is None:
        raise FinalReviewContractError(
            "FINAL_REVIEW_BOUNDARY_REQUEST_BINDING_INVALID"
        )
    endpoint = boundary.get("final_endpoint_binding")
    if (
        not isinstance(endpoint, Mapping)
        or endpoint.get("schema_version")
        != "talk-boundary-final-endpoint-binding.v1"
        or endpoint.get("status") != "PASS"
    ):
        raise FinalReviewContractError(
            "FINAL_REVIEW_BOUNDARY_ENDPOINT_BINDING_INVALID"
        )
    integer_fields = (
        "recommended_end_cue_index",
        "recommended_end_ms",
        "final_closure_cue_index",
        "final_snapped_end_ms",
    )
    if any(
        isinstance(endpoint.get(field), bool)
        or not isinstance(endpoint.get(field), int)
        for field in integer_fields
    ):
        raise FinalReviewContractError(
            "FINAL_REVIEW_BOUNDARY_ENDPOINT_BINDING_INVALID"
        )
    if (
        endpoint["recommended_end_cue_index"]
        != endpoint["final_closure_cue_index"]
        or endpoint["recommended_end_ms"]
        != endpoint["final_snapped_end_ms"]
        or endpoint.get("reason_codes") != []
    ):
        raise FinalReviewContractError(
            "FINAL_REVIEW_BOUNDARY_ENDPOINT_BINDING_MISMATCH"
        )
    reviewed_grid = str(boundary.get("cue_grid_sha256") or "")
    semantic_grid = str(
        endpoint.get("semantic_cue_grid_sha256") or ""
    )
    final_grid = str(endpoint.get("final_cue_grid_sha256") or "")
    if any(
        _SHA256_RX.fullmatch(value) is None
        for value in (reviewed_grid, semantic_grid, final_grid)
    ):
        raise FinalReviewContractError(
            "FINAL_REVIEW_BOUNDARY_CUE_GRID_BINDING_INVALID"
        )
    if not reviewed_grid == semantic_grid == final_grid:
        raise FinalReviewContractError(
            "FINAL_REVIEW_BOUNDARY_CUE_GRID_BINDING_MISMATCH"
        )
    witness = boundary.get("source_separation_witness")
    if (
        boundary.get("review_scope") != "final_delivery"
        or not isinstance(witness, Mapping)
        or witness.get("schema_version")
        != "talk-boundary-source-separation-witness.v1"
        or witness.get("status") != "PASS"
        or _SHA256_RX.fullmatch(
            str(witness.get("source_review_sha256") or "")
        )
        is None
        or _SHA256_RX.fullmatch(
            str(witness.get("source_request_sha256") or "")
        )
        is None
        or _SHA256_RX.fullmatch(
            str(witness.get("source_cue_grid_sha256") or "")
        )
        is None
        or isinstance(witness.get("source_final_start_ms"), bool)
        or not isinstance(witness.get("source_final_start_ms"), int)
        or isinstance(witness.get("source_final_end_ms"), bool)
        or not isinstance(witness.get("source_final_end_ms"), int)
        or witness["source_final_end_ms"]
        <= witness["source_final_start_ms"]
        or witness.get("reason_codes") != []
    ):
        raise FinalReviewContractError(
            "FINAL_REVIEW_BOUNDARY_SOURCE_WITNESS_INVALID"
        )
    if audit.get("release_gate") != "PASS" or audit.get("status") != "CLEAN":
        raise FinalReviewContractError("FINAL_REVIEW_RELEASE_GATE_BLOCKED")
    return dict(audit)


def is_keep_current_disclosed(finding: object) -> bool:
    """A completed keep-current adjudication ships with disclosure.

    Requires the full closed-set chain to have OBSERVED with the judge's
    explicit UNCERTAIN → KEEP_CURRENT branch, no mutation applied and the
    timeline untouched. Anything structural stays a blocker.
    """

    if not isinstance(finding, Mapping):
        return False
    adjudication = finding.get("exact_release_adjudication")
    if not isinstance(adjudication, Mapping):
        return False
    mutation = adjudication.get("mutation_authority")
    return bool(
        adjudication.get("status") == "OBSERVED"
        and adjudication.get("policy_branch") == "JUDGE_UNCERTAIN_KEEP_CURRENT"
        and adjudication.get("repaired") is False
        and adjudication.get("timing_immutable") is True
        and isinstance(mutation, Mapping)
        and mutation.get("status") == "NOT_APPLIED"
    )
