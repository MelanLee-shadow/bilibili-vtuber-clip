"""Fail-closed release contract for the exact final subtitle bytes."""

from __future__ import annotations

import hashlib
import re
from typing import Mapping

from src.autoslice.acoustic_witness_adjudication import (
    valid_inaudible_drop_repair,
    valid_inaudible_override_repair,
)

SCHEMA_VERSION = "final-review-audit.v2"
EXACT_FINAL_CPA_SELF_HEAL_MAX_REPAIR_PASSES = 5
_SHA256_RX = re.compile(r"^sha256:[0-9a-f]{64}$")


class FinalReviewContractError(ValueError):
    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__(reason_code)


def _validate_exact_final_cpa_self_heal(
    audit: Mapping[str, object],
    *,
    expected_srt_sha256: str | None,
) -> None:
    receipt = audit.get("exact_final_cpa_self_heal")
    if receipt is None:
        return
    if (
        not isinstance(receipt, Mapping)
        or receipt.get("schema_version")
        != "exact-final-cpa-self-heal-audit.v1"
        or receipt.get("status") != "PASS"
    ):
        raise FinalReviewContractError(
            "EXACT_FINAL_CPA_SELF_HEAL_AUDIT_INVALID"
        )
    final_sha256 = str(receipt.get("final_srt_sha256") or "")
    if (
        _SHA256_RX.fullmatch(final_sha256) is None
        or (
            expected_srt_sha256 is not None
            and final_sha256 != expected_srt_sha256
        )
    ):
        raise FinalReviewContractError(
            "EXACT_FINAL_CPA_SELF_HEAL_FINAL_HASH_MISMATCH"
        )
    passes = receipt.get("passes")
    if (
        not isinstance(passes, list)
        or not 1 <= len(passes)
        <= EXACT_FINAL_CPA_SELF_HEAL_MAX_REPAIR_PASSES
    ):
        raise FinalReviewContractError(
            "EXACT_FINAL_CPA_SELF_HEAL_AUDIT_INVALID"
        )
    previous_output: str | None = None
    for expected_index, pass_receipt in enumerate(passes, start=1):
        if (
            not isinstance(pass_receipt, Mapping)
            or pass_receipt.get("schema_version")
            != "exact-final-cpa-self-heal-pass.v1"
            or pass_receipt.get("pass_index") != expected_index
        ):
            raise FinalReviewContractError(
                "EXACT_FINAL_CPA_SELF_HEAL_AUDIT_INVALID"
            )
        input_sha256 = str(
            pass_receipt.get("input_srt_sha256") or ""
        )
        output_sha256 = str(
            pass_receipt.get("output_srt_sha256") or ""
        )
        repairs = pass_receipt.get("repairs")
        if (
            _SHA256_RX.fullmatch(input_sha256) is None
            or _SHA256_RX.fullmatch(output_sha256) is None
            or input_sha256 == output_sha256
            or (
                previous_output is not None
                and input_sha256 != previous_output
            )
            or not isinstance(repairs, list)
            or not repairs
        ):
            raise FinalReviewContractError(
                "EXACT_FINAL_CPA_SELF_HEAL_AUDIT_INVALID"
            )
        for repair in repairs:
            mutation = (
                repair.get("mutation_authority")
                if isinstance(repair, Mapping)
                else None
            )
            cue_index = (
                repair.get("cue_index")
                if isinstance(repair, Mapping)
                else None
            )
            before = (
                repair.get("before")
                if isinstance(repair, Mapping)
                else None
            )
            after = (
                repair.get("after")
                if isinstance(repair, Mapping)
                else None
            )
            typed_drop = bool(
                isinstance(repair, Mapping)
                and valid_inaudible_drop_repair(repair)
            )
            witness = (
                repair.get("acoustic_witness")
                if isinstance(repair, Mapping)
                else None
            )
            inaudible_nonempty = bool(
                after
                and isinstance(witness, Mapping)
                and witness.get("status") == "OBSERVED"
                and witness.get("target_audible") is False
            )
            typed_inaudible_override = bool(
                isinstance(repair, Mapping)
                and valid_inaudible_override_repair(repair)
            )
            if (
                not isinstance(repair, Mapping)
                or repair.get("schema_version")
                != "exact-final-cpa-self-heal.v1"
                or isinstance(cue_index, bool)
                or not isinstance(cue_index, int)
                or cue_index < 1
                or not isinstance(before, str)
                or not before
                or not isinstance(after, str)
                or (not after and not typed_drop)
                or (
                    inaudible_nonempty
                    and not typed_inaudible_override
                )
                or _SHA256_RX.fullmatch(
                    str(repair.get("before_sha256") or "")
                )
                is None
                or _SHA256_RX.fullmatch(
                    str(repair.get("after_sha256") or "")
                )
                is None
                or repair.get("before_sha256")
                == repair.get("after_sha256")
                or repair.get("before_sha256")
                != "sha256:"
                + hashlib.sha256(before.encode("utf-8")).hexdigest()
                or repair.get("after_sha256")
                != "sha256:"
                + hashlib.sha256(after.encode("utf-8")).hexdigest()
                or _SHA256_RX.fullmatch(
                    str(repair.get("finding_sha256") or "")
                )
                is None
                or _SHA256_RX.fullmatch(
                    str(repair.get("request_sha256") or "")
                )
                is None
                or repair.get("decision_authority") != "CPA_JUDGE"
                or repair.get("timing_immutable") is not True
                or not isinstance(mutation, Mapping)
                or mutation.get("schema_version")
                != "subtitle-correction-mutation-authority.v1"
                or mutation.get("status") != "PASS"
            ):
                raise FinalReviewContractError(
                    "EXACT_FINAL_CPA_SELF_HEAL_REPAIR_INVALID"
                )
        previous_output = output_sha256
    if previous_output != final_sha256:
        raise FinalReviewContractError(
            "EXACT_FINAL_CPA_SELF_HEAL_FINAL_HASH_MISMATCH"
        )


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
    if unconsumed_correction_carryover_count(audit):
        raise FinalReviewContractError(
            "FINAL_REVIEW_CARRYOVER_UNCONSUMED"
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
    # 维护者（无人值守裁定）：`unresolved_findings_disclosed` 只允许
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
    _validate_exact_final_cpa_self_heal(
        audit,
        expected_srt_sha256=expected_srt_sha256,
    )
    return dict(audit)


_DECIDED_KEEP_CURRENT_BRANCHES = frozenset(
    {
        # judge 明确选 CURRENT（维护者：decided keep 是已完成的
        # 机器决定，发出去检查有问题再修，而不是一直不发）
        "JUDGE_KEEPS_CURRENT",
        # judge 选了 PROPOSED 但代码级拼音门否决——门本身就是决定
        "JUDGE_CHOICE_PINYIN_INCOMPATIBLE_KEEP_CURRENT",
        # CPA 看完「目标不可闻」证据仍选了一个非删除替换；证据门保留。
        "TARGET_INAUDIBLE_KEEP_CURRENT",
    }
)


def unconsumed_correction_carryover_count(audit: object) -> int:
    """Count replayed carryover rows not closed by a fresh CPA decision.

    Exact-final discovery is intentionally independent and non-deterministic.
    Its clean scan cannot erase a prior hash-remapped carryover merely because
    the correction pass disclosed the row without adjudicating it.
    """

    if not isinstance(audit, Mapping):
        return 0
    correction_pass = audit.get("correction_pass")
    findings = (
        correction_pass.get("findings")
        if isinstance(correction_pass, Mapping)
        else None
    )
    if not isinstance(findings, list):
        return 0
    count = 0
    for finding in findings:
        remap = (
            finding.get("carryover_replay_remap")
            if isinstance(finding, Mapping)
            else None
        )
        consumed = correction_carryover_consumed(finding)
        if (
            isinstance(remap, Mapping)
            and remap.get("schema_version")
            == "final-review-carryover-remap.v1"
            and remap.get("status") == "PASS"
            and not consumed
        ):
            count += 1
    return count


def correction_carryover_consumed(finding: object) -> bool:
    """Whether a replayed correction row reached a new terminal decision."""

    if not isinstance(finding, Mapping):
        return False
    adjudication = finding.get("context_audio_adjudication")
    replay = finding.get("carryover_consumption")
    return bool(
        (
            isinstance(adjudication, Mapping)
            and adjudication.get("repaired") is True
        )
        or is_keep_current_disclosed(finding)
        or (
            isinstance(replay, Mapping)
            and replay.get("schema_version")
            == "exact-final-carryover-consumption.v1"
            and replay.get("status") == "CONSUMED_BY_EXACT_FINAL_CPA"
            and _SHA256_RX.fullmatch(
                str(replay.get("before_sha256") or "")
            )
            is not None
            and _SHA256_RX.fullmatch(
                str(replay.get("after_sha256") or "")
            )
            is not None
        )
    )


def is_keep_current_disclosed(finding: object) -> bool:
    """A completed keep-current adjudication ships with disclosure.

    Requires the full closed-set chain to have OBSERVED with a *decided*
    KEEP_CURRENT branch, no mutation applied and the timeline untouched.
    Infra incompleteness (witness/judge/pinyin backend unavailable, stale
    base, invalid response, budget skip) stays a blocker: those branches are
    machinery failing to decide, not a decision.
    """

    if not isinstance(finding, Mapping):
        return False
    adjudication = finding.get("exact_release_adjudication")
    if not isinstance(adjudication, Mapping):
        adjudication = finding.get("context_audio_adjudication")
    if not isinstance(adjudication, Mapping):
        return False
    mutation = adjudication.get("mutation_authority")
    return bool(
        adjudication.get("status") == "OBSERVED"
        and adjudication.get("policy_branch") in _DECIDED_KEEP_CURRENT_BRANCHES
        and adjudication.get("repaired") is False
        # Auditor receipts historically bind timing immutability on the
        # finding envelope; newer synthetic/unit receipts may carry the same
        # bit inside the adjudication.  Both are the same fail-closed fact.
        and (
            adjudication.get("timing_immutable") is True
            or finding.get("timing_immutable") is True
        )
        and isinstance(mutation, Mapping)
        and mutation.get("status") == "NOT_APPLIED"
    )
