"""Fail-closed release contract for the exact final subtitle bytes."""

from __future__ import annotations

from .review_evidence_store import cold_input
from .canonical_json_hash import canonical_sha256
from .validation_memo import memoized_validation

import hashlib
import re
from typing import Mapping

from src.autoslice.acoustic_witness_adjudication import (
    REQUIRE_COMPLETE_UTTERANCE_SUPPORT,
    WITNESS_CONFLICT_UNSUPPORTED_PROPOSED,
    valid_inaudible_drop_repair,
    valid_inaudible_override_repair,
    valid_current_utterance_support,
)
from src.autoslice.acoustic_witness_protocol import BLIND_PINYIN_PROTOCOL
from src.autoslice.exact_final_witness_authority import (
    DOWNGRADE_BRANCH as HISTORY_CONVERGENCE_DOWNGRADE_BRANCH,
    GATE_SCHEMA as _HISTORY_CONVERGENCE_GATE_SCHEMA,
    REQUIRED_REASON as _HISTORY_CONVERGENCE_REQUIRED_REASON,
)
from src.autoslice.exact_final_convergence import (
    exact_final_convergence_memo_identity,
    valid_exact_final_convergence_memo_binding,
)
from src.autoslice.unreadable_span_policy import (
    unreadable_cue_drop_audit_problem,
)
from src.autoslice.exact_final_cpa_history import (
    FINAL_IDENTITY_MISMATCH, CHAIN_TERMINAL_MISMATCH,
    read_successors, ordered_successors,
)

SCHEMA_VERSION = "final-review-audit.v2"
CURRENT_UTTERANCE_KEEP_BRANCH = 'JUDGE_KEEPS_CURRENT'
EXACT_FINAL_CPA_SELF_HEAL_SOFT_REPAIR_PASSES = 5
# Compatibility for historical callers; this number is no longer a cutoff.
EXACT_FINAL_CPA_SELF_HEAL_MAX_REPAIR_PASSES = EXACT_FINAL_CPA_SELF_HEAL_SOFT_REPAIR_PASSES
_SHA256_RX = re.compile(r"^sha256:[0-9a-f]{64}$")


class FinalReviewContractError(ValueError):
    def __init__(self, reason_code: str, **context) -> None:
        self.reason_code = reason_code
        self.context = context
        super().__init__(reason_code + (f": {context}" if context else ""))


@cold_input
@memoized_validation(lambda audit, expected_srt_sha256, allow_pending=False:
    canonical_sha256([audit.get('exact_final_cpa_self_heal'), expected_srt_sha256, allow_pending], allow_nan=True))
def _validate_exact_final_cpa_self_heal(
    audit: Mapping[str, object],
    *,
    expected_srt_sha256: str | None,
    allow_pending: bool = False,
) -> None:
    receipt = audit.get("exact_final_cpa_self_heal")
    if receipt is None:
        return
    if (
        not isinstance(receipt, Mapping)
        or receipt.get("schema_version")
        != "exact-final-cpa-self-heal-audit.v1"
        or receipt.get("status") not in (
            {"PASS", "REVIEW_PENDING"} if allow_pending else {"PASS"}
        )
    ):
        raise FinalReviewContractError(
            "EXACT_FINAL_CPA_SELF_HEAL_AUDIT_INVALID"
        )
    # A resumed private run may retain actual applied repairs before the next
    # complete review. Validate that chain against the active bytes without
    # giving it a release verdict. Public acceptance still requires PASS.
    final_sha256 = str(
        expected_srt_sha256
        if allow_pending and receipt.get("status") == "REVIEW_PENDING"
        else receipt.get("final_srt_sha256") or ""
    )
    if (
        _SHA256_RX.fullmatch(final_sha256) is None
        or (
            expected_srt_sha256 is not None
            and final_sha256 != expected_srt_sha256
        )
    ):
        raise FinalReviewContractError(
            FINAL_IDENTITY_MISMATCH, expected=expected_srt_sha256, actual=final_sha256,
        )
    passes = receipt.get("passes")
    if (
        not isinstance(passes, list)
        or not passes
    ):
        raise FinalReviewContractError(
            "EXACT_FINAL_CPA_SELF_HEAL_AUDIT_INVALID"
        )
    from src.autoslice.span_phoneme_resolution import validate_persisted_judge_scopes
    try:
        validate_persisted_judge_scopes(receipt)
    except (ValueError, TypeError, KeyError, AttributeError) as exc:
        raise FinalReviewContractError("FINAL_REVIEW_SPAN_COMPILATION_INVALID") from exc
    previous_output: str | None = None
    try:
        successors = read_successors(receipt)
    except ValueError as exc:
        raise FinalReviewContractError(str(exc)) from exc
    seen_hashes: set[str] = set()
    seen_memo_ids: set[str] = set()
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
            if isinstance(repair, Mapping):
                from src.autoslice.span_finding_obligations import validate_span_repair
                try:
                    validate_span_repair(repair)
                except (ValueError, KeyError, TypeError) as exc:
                    raise FinalReviewContractError("SPAN_SELF_HEAL_REPAIR_INVALID") from exc
            if isinstance(repair, Mapping) and (repair.get("repair_class") == "editorial_rendering"
                                                 or repair.get("editorial_authority") is not None):
                from src.autoslice.editorial_subtitle import validate_repair
                try:
                    validate_repair(repair)
                except (ValueError, KeyError, TypeError) as exc:
                    raise FinalReviewContractError("EDITORIAL_SELF_HEAL_INVALID") from exc
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
            from src.autoslice.editorial_subtitle import valid_display_omission_repair
            typed_drop = bool(
                isinstance(repair, Mapping)
                and (valid_inaudible_drop_repair(repair) or valid_display_omission_repair(repair))
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
            judge = repair.get("judge") if isinstance(repair, Mapping) else None
            if (
                isinstance(judge, Mapping)
                and judge.get("decision_contract")
                == "current-proposed-candidate-id-neither.v1"
                and judge.get("choice") == "PROPOSED"
                and judge.get("selected_candidate_text") != after
            ):
                raise FinalReviewContractError(
                    "EXACT_FINAL_CPA_SELECTED_TRANSCRIPT_MISMATCH"
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
        memo_ids_this_pass: set[str] = set()
        for repair in repairs:
            memo_keys = [
                key
                for key in (
                    "exact_final_cpa_cycle_memo",
                    "exact_final_cpa_convergence_memo",
                )
                if repair.get(key) is not None
            ]
            if len(memo_keys) > 1:
                raise FinalReviewContractError(
                    "EXACT_FINAL_CPA_SELF_HEAL_CYCLE_RETURN_INVALID"
                )
            if not memo_keys:
                continue
            memo = repair[memo_keys[0]]
            memo_id = (
                exact_final_convergence_memo_identity(memo)
                if isinstance(memo, Mapping)
                else None
            )
            if memo_id is None:
                raise FinalReviewContractError(
                    "EXACT_FINAL_CPA_SELF_HEAL_CYCLE_RETURN_BINDING_INVALID"
                )
            if memo_id in seen_memo_ids or memo_id in memo_ids_this_pass:
                raise FinalReviewContractError(
                    "EXACT_FINAL_CPA_SELF_HEAL_CYCLE_RETURN_MEMO_REPLAY"
                )
            if not valid_exact_final_convergence_memo_binding(
                memo,
                input_srt_sha256=input_sha256,
                before_text=repair["before"],
                after_text=repair["after"],
                before_sha256=repair["before_sha256"],
                after_sha256=repair["after_sha256"],
                matched_start_ms=repair.get("matched_start_ms"),
                matched_end_ms=repair.get("matched_end_ms"),
            ):
                raise FinalReviewContractError(
                    "EXACT_FINAL_CPA_SELF_HEAL_CYCLE_RETURN_BINDING_INVALID"
                )
            memo_ids_this_pass.add(memo_id)
        if output_sha256 in seen_hashes and len(memo_ids_this_pass) != len(repairs):
            raise FinalReviewContractError(
                "EXACT_FINAL_CPA_SELF_HEAL_CYCLE_RETURN_UNAUTHORIZED"
            )
        seen_memo_ids.update(memo_ids_this_pass)
        seen_hashes.add(input_sha256)
        seen_hashes.add(output_sha256)
        previous_output = output_sha256
        # Each independently replayed transform must consume the actual prior
        # bytes. Source extension and visual filtering may occur in either
        # order; a missing or ambiguous link must never become a release PASS.
        try:
            ordered = ordered_successors(successors.get(expected_index, []), previous_output)
        except ValueError as exc:
            raise FinalReviewContractError(str(exc), pass_index=expected_index,
                actual=previous_output,
                transform_types=[r["schema_version"] for r in successors.get(expected_index, [])]) from exc
        for successor in ordered:
            previous_output = successor["output_srt_sha256"]
            seen_hashes.add(previous_output)
    if previous_output != final_sha256:
        raise FinalReviewContractError(
            CHAIN_TERMINAL_MISMATCH, expected=final_sha256, actual=previous_output,
            pass_index=len(passes),
            transform_types=[r["schema_version"] for r in successors.get(len(passes), [])],
        )


def _validate_review_integrity(audit, *, expected_srt_sha256):
    """Common byte/discovery/mutation requirements; does not decide scope."""
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
    from src.autoslice.span_phoneme_resolution import validate_persisted_judge_scopes
    try:
        validate_persisted_judge_scopes(audit)
    except (ValueError, TypeError, KeyError, AttributeError) as exc:
        raise FinalReviewContractError("FINAL_REVIEW_SPAN_COMPILATION_INVALID") from exc
    if unconsumed_correction_carryover_count(audit):
        raise FinalReviewContractError(
            "FINAL_REVIEW_CARRYOVER_UNCONSUMED"
        )


def _validate_review_geometry(audit, *, expected_srt_sha256):
    """Common exact boundary/clock/self-heal checks, never relaxed by scope."""
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
    _validate_exact_final_cpa_self_heal(
        audit,
        expected_srt_sha256=expected_srt_sha256,
    )





    unreadable_problem = unreadable_cue_drop_audit_problem(
        audit.get("unreadable_cue_drops"),
        expected_srt_sha256=expected_srt_sha256,
    )
    if unreadable_problem is not None:
        raise FinalReviewContractError(unreadable_problem)


@cold_input
def validate_final_review_release(
    audit: object,
    *,
    expected_srt_sha256: str | None = None,
) -> dict[str, object]:
    """Validate a positive receipt; every other state is a release block."""
    from src.autoslice.final_review_meaning_policy import POLICY_VERSION
    if not isinstance(audit, Mapping) or audit.get("meaning_policy") != POLICY_VERSION:
        raise FinalReviewContractError("FINAL_REVIEW_MEANING_POLICY_REVIEW_REQUIRED")

    if audit.get("final_review_mode") == "per_finding" or "finding_closure" in audit:
        from src.autoslice.finding_closure import ClosureContractError
        from src.autoslice.finding_closure_ports import RetainedObject
        from src.autoslice.finding_closure_shell import validate_published_closure
        try:
            validate_published_closure(RetainedObject.from_value(dict(audit)),
                                      expected_srt_sha256=expected_srt_sha256)
        except ClosureContractError as exc:
            raise FinalReviewContractError(str(exc).split(":", 1)[0]) from exc

    if isinstance(audit, Mapping) and audit.get("schema_version") == "b2-caption-final-review-successor.v1":
        from src.autoslice.b2_caption_formal_successor import (
            B2CaptionFormalSuccessorError,
            validate_final_review_successor,
        )
        try:
            return validate_final_review_successor(audit, expected_srt_sha256=expected_srt_sha256)
        except B2CaptionFormalSuccessorError as exc:
            raise FinalReviewContractError(exc.reason_code) from exc

    if (
        isinstance(audit, Mapping)
        and audit.get("schema_version")
        == "e353-content-final-review-successor.v1"
    ):
        from src.autoslice.e353_content_formal_successor import (
            E353ContentFormalSuccessorError,
            validate_final_review_successor,
        )

        try:
            return validate_final_review_successor(
                audit, expected_srt_sha256=expected_srt_sha256
            )
        except E353ContentFormalSuccessorError as exc:
            raise FinalReviewContractError(exc.reason_code) from exc

    if isinstance(audit, Mapping) and audit.get("schema_version") == "c10-operator-final-review-successor.v1":
        from src.autoslice.c10_operator_review_successor import (
            C10OperatorReviewSuccessorError,
            validate_c10_operator_review_successor,
        )

        try:
            return validate_c10_operator_review_successor(
                audit, expected_srt_sha256=expected_srt_sha256
            )
        except C10OperatorReviewSuccessorError as exc:
            raise FinalReviewContractError(exc.reason_code) from exc
    if isinstance(audit, Mapping) and audit.get("schema_version") == "original-preserved-final-review.v1":
        from src.autoslice.operator_preserved_final_review import validate_preserved_review
        return validate_preserved_review(audit, expected_srt_sha256=expected_srt_sha256)
    _validate_review_integrity(audit, expected_srt_sha256=expected_srt_sha256)
    from src.autoslice.span_finding_obligations import validate_release_obligations
    from .finding_lifecycle import REVIEW_PENDING, release_reason_codes
    pending_obligations = False
    try:
        validate_release_obligations(audit)
    except (ValueError, KeyError, TypeError) as exc:
        if str(exc) != REVIEW_PENDING:
            raise FinalReviewContractError(str(exc)) from exc
        # An unresolved obligation still blocks release, but it must not hide
        # a more specific engineering failure or required edit on that finding.
        pending_obligations = True
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
        try:
            codes = release_reason_codes(findings, terminal=bool(audit.get('terminal_funnel')))
        except ValueError as exc:
            raise FinalReviewContractError(str(exc)) from exc
        raise FinalReviewContractError(codes[0] if codes else "FINAL_REVIEW_FINDINGS_CONTRACT_INVALID")
    if pending_obligations:
        raise FinalReviewContractError(REVIEW_PENDING)




    disclosed = audit.get("unresolved_findings_disclosed")
    if disclosed is not None:
        if not isinstance(disclosed, list):
            raise FinalReviewContractError(
                "FINAL_REVIEW_FINDINGS_CONTRACT_INVALID"
            )
        for row in disclosed:



            if not is_release_resolved_keep(row):
                raise FinalReviewContractError(
                    "FINAL_REVIEW_FINDINGS_CONTRACT_INVALID"
                )
            if isinstance(row, Mapping) and row.get("current_content_risk") is not None:
                assessment = row["current_content_risk"]
                if (not isinstance(assessment, Mapping)
                        or assessment.get("reviewed_srt_sha256") != audit.get("reviewed_srt_sha256")):
                    raise FinalReviewContractError("FINAL_REVIEW_CURRENT_RISK_DRIFT")
            if isinstance(row, Mapping) and row.get("candidate_content_risk") is not None:
                from src.autoslice.final_review_candidate_risk import candidate_risk_assessment_valid
                if not candidate_risk_assessment_valid(row, srt_sha256=audit.get("reviewed_srt_sha256")):
                    raise FinalReviewContractError("FINAL_REVIEW_CANDIDATE_RISK_DRIFT")
    _validate_review_geometry(audit, expected_srt_sha256=expected_srt_sha256)
    if audit.get("release_gate") != "PASS" or audit.get("status") != "CLEAN":
        raise FinalReviewContractError("FINAL_REVIEW_RELEASE_GATE_BLOCKED")
    return dict(audit)






















_DECIDED_KEEP_CURRENT_BRANCHES = frozenset(
    {
        # judge 明确选 CURRENT。
        CURRENT_UTTERANCE_KEEP_BRANCH,





        WITNESS_CONFLICT_UNSUPPORTED_PROPOSED,
        # 以下两个是 0a97deb(7/27) 写表当时的引擎名字，d71e856(7/28) 已把
        # 产出点删除——src 中再无任何代码吐出它们。保留仅为兼容那之前落盘
        # 的历史回执重放；新回执不会再出现。
        "JUDGE_CHOICE_PINYIN_INCOMPATIBLE_KEEP_CURRENT",
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


def decided_history_convergence_disclosure(
    adjudication: Mapping[str, object],
    *,
    timing_immutable: bool,
) -> bool:
    """史收敛降级里「耳朵真听见了」的那一半，也是一次已完成的决定。

    ``HISTORY_CONVERGENCE_DOWNGRADED_TO_DISCLOSURE_ONLY`` 的分支名直译就是
    「已降级为：只披露」，审片员同时把 ``repair_class`` 改成 ``disclosure_only``
    ——它明确说了「别改、只披露」。但降级同时把 adjudication 的 ``status`` 写成
    ``UNCERTAIN``（这里的 uncertain 指的是「对那次改写没把握」，不是「没观测
    到」），于是它撞死在 ``decided_keep_current_adjudication`` 的
    ``status == "OBSERVED"`` 上：既不许改、又不许披露，整条候选永久悬停
    （auto_214238_835_960 的唯一阻断项即此）。

    放行判据只有一条实质内容：**声学机器必须真的跑完并交出一次观测**。
    降级发生在「CPA 收敛判官选了 PROPOSED，但代码级见证门 BLOCK」时，而门
    BLOCK 有两种截然不同的成因：

    * ``verdict.status == "OBSERVED"``：耳朵听见了、判官判了、门用证据否掉
      了这次改写、一个字节没动 —— 证据在手做出的「保留原文」，与白名单里
      ``WITNESS_CONFLICT_UNSUPPORTED_PROPOSED_KEPT_CURRENT``（门本身就是决定）
      同类，可随包披露发出去，发后可修。
    * ``verdict`` 非 OBSERVED（如 ``WITNESS_IMPLAUSIBLE_SYLLABLE_RATE`` 让证词
      落到 UNCERTAIN）：耳朵没能给出观测 —— 这是机器没能决定，按
      ``is_keep_current_disclosed`` 的既定界线继续拦死，不许借降级出口逃逸。

    其余字段全部按 ``downgrade_convergence_finding`` 落盘的 typed 形状逐项核
    对（门 schema/BLOCK/reason_code、mutation basis、decision/witness
    authority），任何一处对不上都视为伪造的降级壳子，不予放行。
    """

    gate = adjudication.get("history_convergence_acoustic_witness")
    mutation = adjudication.get("mutation_authority")
    witness = adjudication.get("verdict")
    return bool(
        adjudication.get("policy_branch")
        == HISTORY_CONVERGENCE_DOWNGRADE_BRANCH
        and adjudication.get("status") == "UNCERTAIN"
        and adjudication.get("repaired") is False
        and adjudication.get("reason_code")
        == _HISTORY_CONVERGENCE_REQUIRED_REASON
        and adjudication.get("decision_authority") == "CPA_PROPOSAL_ONLY"
        and adjudication.get("witness_authority")
        == "ACOUSTIC_WITNESS_REQUIRED"
        and timing_immutable
        and isinstance(gate, Mapping)
        and gate.get("schema_version") == _HISTORY_CONVERGENCE_GATE_SCHEMA
        and gate.get("status") == "BLOCK"
        and gate.get("reason_code") == _HISTORY_CONVERGENCE_REQUIRED_REASON
        and isinstance(mutation, Mapping)
        and mutation.get("schema_version")
        == "subtitle-correction-mutation-authority.v1"
        and mutation.get("status") == "NOT_APPLIED"
        and mutation.get("basis") == _HISTORY_CONVERGENCE_REQUIRED_REASON
        and isinstance(witness, Mapping)
        and witness.get("schema_version")
        == "subtitle-span-acoustic-witness.v1"
        and witness.get("witness_protocol") == BLIND_PINYIN_PROTOCOL
        and witness.get("status") == "OBSERVED"
    )


def current_noncritical_editorial_assessment(
    finding: object, *, expected_srt_sha256: str | None = None,
) -> bool:
    """A completed current content judgment makes presentation optional.

    This never resolves the transport status of an earlier optional attempt.
    Other adjudication lanes and any applied mutation keep their own gates.
    """
    if not isinstance(finding, Mapping) or finding.get("repair_class") != "editorial_rendering":
        return False
    from src.autoslice.final_review_meaning_policy import current_meaning_equivalent
    if not current_meaning_equivalent(finding, expected_srt_sha256=expected_srt_sha256):
        return False
    assessment = finding.get("current_content_risk")
    digest = assessment.get("reviewed_srt_sha256")
    if (assessment.get("schema_version") != "subtitle-current-content-risk.v2"
            or assessment.get("status") != "COMPLETE"
            or assessment.get("decision_authority") != "CURRENT_FINAL_REVIEW"
            or _SHA256_RX.fullmatch(str(digest or "")) is None
            or (expected_srt_sha256 is not None and digest != expected_srt_sha256)
            or type(finding.get("cue_index")) is not int
            or finding["cue_index"] < 1
            or assessment.get("cue_index") != finding["cue_index"]
            or re.fullmatch(r"[0-9a-f]{64}", str(finding.get("base_text_sha256") or "")) is None
            or assessment.get("base_text_sha256") != finding["base_text_sha256"]
            or assessment.get("content_risk") != "noncritical_uncertainty"
            or finding.get("content_risk") != "noncritical_uncertainty"
            or not isinstance(finding.get("risk_basis"), str) or not finding["risk_basis"].strip()
            or assessment.get("risk_basis") != finding["risk_basis"]):
        return False
    adjudication = finding.get("exact_release_adjudication")
    if adjudication is None:
        adjudication = finding.get("context_audio_adjudication")
    return adjudication is None or (isinstance(adjudication, Mapping)
        and adjudication.get("schema_version") == "subtitle-editorial-adjudication.v1"
        and adjudication.get("status") == "UNCERTAIN"
        and adjudication.get("repaired") is False
        and adjudication.get("timing_immutable") is True
        and not adjudication.get("mutation_authority"))


def pending_noncritical_repair(finding: object) -> bool:
    """Lack of a candidate or plan is never evidence of meaning equivalence."""
    from src.autoslice.final_review_meaning_policy import current_meaning_equivalent
    return bool(isinstance(finding, Mapping)
        and finding.get("content_risk") == "noncritical_uncertainty"
        and not current_meaning_equivalent(finding))


def undecided_witness_nomination(finding: object) -> bool:
    """An independent-witness wording nomination that no exact adjudication decided.

    维护者 10-16: where the audio is clear the subtitle must be the spoken words.
    A deterministic nomination from the independent dictation witness therefore
    cannot be waved through as "non-critical / meaning-equivalent": audio plus
    the CPA judge must pick CURRENT or the witness wording first. This
    witness-specific discovery check does not exempt other proposal kinds:
    final release still requires every doubt to be resolved under
    ``is_audio_decided_keep_current`` and the ordinary repair gates.
    """
    if not isinstance(finding, Mapping):
        return False
    provenance = finding.get("candidate_provenance")
    if not (isinstance(provenance, Mapping)
            and provenance.get("kind") == "subtitle_audio_witness_disagreement"):
        return False
    adjudication = finding.get("exact_release_adjudication")
    if adjudication is None:
        adjudication = finding.get("context_audio_adjudication")
    return not isinstance(adjudication, Mapping) or not adjudication


def is_noncritical_disclosed(finding: object) -> bool:
    """Classify legacy noncritical disclosures without resolving transport loss.

    This compatibility predicate is not release permission. Current final
    release consumers use ``is_release_resolved_keep`` instead. It does
    not authorize a mutation or clear an ambiguous historical request.
    """
    if not isinstance(finding, Mapping):
        return False
    if undecided_witness_nomination(finding):
        return False
    from src.autoslice.final_review_meaning_policy import current_meaning_equivalent
    if not current_meaning_equivalent(finding):
        return False
    if (finding.get("content_risk") != "noncritical_uncertainty"
            or not isinstance(finding.get("risk_basis"), str)
            or not finding["risk_basis"].strip()):
        return False
    from src.autoslice.final_review_candidate_risk import (
        needs_candidate_content_review, candidate_risk_assessment_valid,
    )
    if (finding.get("candidate_content_risk_required") is True
            and needs_candidate_content_review(finding) and not candidate_risk_assessment_valid(finding)):
        return False
    adjudication = finding.get("exact_release_adjudication")
    if adjudication is None:
        adjudication = finding.get("context_audio_adjudication")
    if adjudication is None:
        adjudication = {}
    if not isinstance(adjudication, Mapping) or adjudication.get("repaired") is True:
        return False
    mutation = adjudication.get("mutation_authority")
    if isinstance(mutation, Mapping) and mutation.get("status") != "NOT_APPLIED":
        return False
    if current_noncritical_editorial_assessment(finding):
        return True
    if pending_noncritical_repair(finding):
        return False
    if adjudication.get("status") in {"ERROR", "OUTCOME_UNKNOWN", "STARTED", "SKIPPED_PROVIDER_DISABLED"}:
        return False
    progress = adjudication.get("semantic_retry_progress")
    if isinstance(progress, Mapping):
        if "UNKNOWN" in str(progress.get("status") or ""):
            return False
        for event in progress.get("events", []):
            if isinstance(event, Mapping) and any("UNKNOWN" in str(event.get(key) or "")
                                                 for key in ("status", "reason_code", "event", "outcome")):
                return False
    reason = str(adjudication.get("reason_code") or "")
    if (adjudication.get('schema_version') == 'subtitle-editorial-adjudication.v1'
            and adjudication.get('status') == 'UNCERTAIN'
            and adjudication.get('timing_immutable') is True
            and reason in {'EDITORIAL_AUDIO_UNAVAILABLE', 'EDITORIAL_FACT_OR_NOOP_INVALID'}):
        # A failed optional edit grants no mutation. A current, explicit
        # noncritical ruling can disclose the unchanged text; it is not an
        # editorial PASS or an assertion that the missing audio was consumed.
        return True
    if reason == "RetryNeedsPlan":
        # Requiring a useful plan for *another* provider call is not a content
        # error in the unchanged current bytes. A current noncritical ruling
        # can disclose this deliberate no-dispatch decision without resetting
        # history or claiming any old request was never sent.
        events = progress.get("events") if isinstance(progress, Mapping) else None
        return bool(
            isinstance(progress, Mapping)
            and progress.get("schema_version") == "semantic-retry-progress.v1"
            and progress.get("status") == "CONTINUATION_VALUE_PLAN_REQUIRED"
            and progress.get("mutation_authorized") is False
            and isinstance(events, list) and events
            and all(isinstance(event, Mapping)
                    and event.get("status") == "CONTINUATION_VALUE_PLAN_REQUIRED"
                    and type(event.get("provider_calls")) is int and event["provider_calls"] == 0
                    for event in events)
        )
    if any(token in reason.upper() for token in (
        "TIMEOUT", "CONNECTION", "OUTCOME_UNKNOWN", "PROVIDER_UNAVAILABLE",
        "RETRYNEEDSPLAN", "UNAVAILABLE", "INVALID", "DRIFT", "CACHEMISS",
    )):
        return False
    if not adjudication:
        return True
    timing_immutable = (adjudication.get("timing_immutable") is True
                        or finding.get("timing_immutable") is True)
    return (decided_keep_current_adjudication(adjudication, timing_immutable=timing_immutable)
            or decided_history_convergence_disclosure(adjudication, timing_immutable=timing_immutable))


def is_keep_current_disclosed(finding: object) -> bool:
    """Whether a row already sitting in a disclosed list is a legitimate disclosure.

    Consumers that re-check an existing ``unresolved_findings_disclosed`` row
    (continuation input, carryover consumption, postburn evidence, operator
    preserved review) ask this. It is the legacy noncritical classification
    plus the release union ``is_release_resolved_keep``: a completed audio
    KEEP_CURRENT ruling, or SUB-18's exhausted-audio meaning-equivalent keep.
    Infra incompleteness (witness/judge/pinyin backend unavailable, stale
    base, invalid response, budget skip) stays a blocker.

    It is not the exact-final bucket: deciding whether a *new* row may enter
    the disclosed list uses ``is_release_resolved_keep`` alone (维护者
    a bare noncritical / meaning-equivalence label is no ruling).
    """

    return is_noncritical_disclosed(finding) or is_release_resolved_keep(finding)


SUB18_KEEP_BRANCH = "SUB18_CONTEXT_MEANING_EQUIVALENT_KEEP"
SUB18_KEEP_SCHEMA = "sub18-context-meaning-equivalent-keep.v1"
SUB18_KEEP_RULE = ('公开规则：如果有哪些真的听不清，但是你发现语义完全符合，语境完全符合，不会有任何的问题，那么那个小错是可以发的。(GOAL-03/SUB-18)')


@cold_input
def is_sub18_context_meaning_equivalent_keep(finding: object) -> bool:
    """SUB-18 second terminal form: a kept small error that cannot change meaning.

    Only after the audio material is exhausted (the sealed no-gain proof and
    its material-exhaustion record), only for ``noncritical_uncertainty``
    (``meaning_reversal`` never), only with complete typed
    ``subtitle-current-content-risk.v2`` meaning-equivalence evidence bound to
    the reviewed subtitle, the context basis it cites, and the
    UNRESOLVED_BY_AUDIO disclosure. Every field is re-checked here; nothing is
    inferred from a branch name. ``is_audio_decided_keep_current`` keeps its
    own meaning; release consumers ask for the union below.
    无增益证明必须是本句的证明（anchor 的当前句与整片字幕哈希须与本行一致），他句的证明不能挪用。
    """

    def _bare_sha(value):
        return value.removeprefix("sha256:") if isinstance(value, str) else None

    if not isinstance(finding, Mapping) or finding.get("content_risk") != "noncritical_uncertainty":
        return False
    from src.autoslice.final_review_meaning_policy import current_meaning_equivalent
    from src.autoslice import targeted_relisten as relisten
    if not current_meaning_equivalent(finding):
        return False
    adjudication = finding.get("exact_release_adjudication")
    if not isinstance(adjudication, Mapping):
        adjudication = finding.get("context_audio_adjudication")
    if not isinstance(adjudication, Mapping):
        return False
    if adjudication.get('terminal_review'):
        from src.autoslice.semantic_terminal_adjudication import terminal_keep_matches
        try:
            if not terminal_keep_matches(finding, adjudication):
                return False
        except (ValueError, TypeError, KeyError, AttributeError):
            return False
    mutation = adjudication.get("mutation_authority")
    receipt = adjudication.get("sub18_context_meaning_equivalent_keep")
    risk = finding.get("current_content_risk") or {}
    if not (adjudication.get("schema_version") == "subtitle-span-adjudication.v1"
            and adjudication.get("policy_branch") == SUB18_KEEP_BRANCH
            and adjudication.get("repaired") is False
            and adjudication.get("timing_immutable") is True
            and isinstance(mutation, Mapping) and mutation.get("status") == "NOT_APPLIED"
            and mutation.get("basis") == SUB18_KEEP_BRANCH
            and isinstance(receipt, Mapping)):
        return False
    proof = receipt.get("no_gain_proof")
    if (isinstance(proof, Mapping) and proof.get('proof_kind') in {relisten.LOOP_STOP_KIND, 'TASK_STOP'}
            and not adjudication.get('terminal_review')):
        return False  # A loop-stop proof cannot fall back to an unjudged legacy keep.
    consistency = receipt.get("context_consistency")
    try:
        return bool(
            receipt.get("schema_version") == SUB18_KEEP_SCHEMA
            and receipt.get("rule") == SUB18_KEEP_RULE
            and receipt.get("audio_resolution") == "UNRESOLVED_BY_AUDIO"
            and receipt.get("literal_transcription") is False
            and receipt.get("mutation_authorized") is False
            and receipt.get("content_risk") == "noncritical_uncertainty"
            and receipt.get("cue_index") == finding.get("cue_index")
            and receipt.get("base_text_sha256") == finding.get("base_text_sha256")
            and receipt.get("reviewed_srt_sha256") == risk.get("reviewed_srt_sha256")
            and receipt.get("meaning_equivalence") == risk.get("meaning_equivalence")
            and isinstance(consistency, Mapping)
            and consistency.get("status") == "CONTEXT_FULLY_CONSISTENT"
            and consistency.get("context_basis") == (risk.get("meaning_equivalence") or {}).get("context_basis")
            and consistency.get("evidence_cue_ids") == (risk.get("meaning_equivalence") or {}).get("evidence_cue_ids")
            and isinstance(consistency.get("no_gain_context_refs"), list) and consistency["no_gain_context_refs"]
            and isinstance(proof, Mapping)
            and proof.get("schema_version") == relisten.NO_GAIN_SCHEMA
            and proof.get("mutation_authorized") is False
            and proof.get("audio_resolution") == "UNRESOLVED_BY_AUDIO"
            and proof.get("proof_sha256") == relisten.digest(
                {k: v for k, v in proof.items() if k != "proof_sha256"})
            and isinstance(proof.get("anchor"), Mapping)
            and proof["anchor"].get("current_cue_sha256") == finding.get("base_text_sha256")
            and _bare_sha(proof["anchor"].get("current_srt_sha256"))
            and _bare_sha(risk.get("reviewed_srt_sha256"))
            and _bare_sha(proof["anchor"].get("current_srt_sha256"))
                == _bare_sha(risk.get("reviewed_srt_sha256"))
            and receipt.get("no_gain_proof_sha256") == proof.get("proof_sha256")
            and sorted(consistency["no_gain_context_refs"]) == sorted(proof.get("context_sources") or {})
            and proof.get("material_exhaustion") is not None
            and (relisten.validate_current_no_gain(proof,
                    adjudication['terminal_review']['request']['check_request']) == proof
                if proof.get('proof_kind') == 'TASK_STOP'
                else relisten._valid_exhaustion(proof["material_exhaustion"], proof["projection"]))
            and receipt.get("material_exhaustion") == proof["material_exhaustion"])
    except (KeyError, TypeError, ValueError, AttributeError):
        return False


@cold_input
def is_release_resolved_keep(finding: object) -> bool:
    """Union for release consumers: an audio KEEP_CURRENT ruling, or SUB-18's
    exhausted-audio meaning-equivalent keep (admin ruling)."""

    if isinstance(finding, Mapping):
        audit = finding.get('exact_release_adjudication') or finding.get('context_audio_adjudication') or {}
        if isinstance(audit, Mapping) and audit.get('terminal_review'):
            from src.autoslice.semantic_terminal_adjudication import terminal_keep_matches, KEEP_ROUTES
            try:
                if not terminal_keep_matches(finding, audit):
                    return False
                from src.autoslice.llm_client import extract_json_object
                from .answer_contract import decode_answer
                review = audit['terminal_review']
                route = decode_answer(review['request'], extract_json_object(review['raw_completion']),
                    prompt=review['prompt'])['keep_route']
                return (is_audio_decided_keep_current(finding) if KEEP_ROUTES[route]['branch'] == CURRENT_UTTERANCE_KEEP_BRANCH
                    else is_sub18_context_meaning_equivalent_keep(finding))
            except (ValueError, TypeError, KeyError, AttributeError, OSError):
                return False
    return is_audio_decided_keep_current(finding) or is_sub18_context_meaning_equivalent_keep(finding)


@cold_input
def is_audio_decided_keep_current(finding: object) -> bool:
    """Only a completed listening ruling to keep the original text resolves a row.

    No ``is_noncritical_disclosed`` shortcut: a bare ``noncritical_uncertainty``
    / meaning-equivalence disclosure is an unresolved doubt, not a ruling
    (维护者). Narrow by design (the audio judge kept CURRENT); release
    consumers ask the union ``is_release_resolved_keep``.
    """

    if not isinstance(finding, Mapping):
        return False
    from src.autoslice.final_review_meaning_policy import current_meaning_equivalent
    from src.autoslice.final_review_candidate_risk import needs_candidate_content_review, candidate_risk_assessment_valid
    if (finding.get("content_risk") == "noncritical_uncertainty"
            and finding.get("candidate_content_risk_required") is True
            and needs_candidate_content_review(finding)
            and not candidate_risk_assessment_valid(finding)):
        return False
    adjudication = finding.get("exact_release_adjudication")
    if not isinstance(adjudication, Mapping):
        adjudication = finding.get("context_audio_adjudication")
    if not isinstance(adjudication, Mapping):
        return False
    timing_immutable = (
        adjudication.get("timing_immutable") is True
        or finding.get("timing_immutable") is True
    )
    if not current_meaning_equivalent(finding):
        # 41:48-56 : the only retained original that counts as
        # resolved is an audio ruling of KEEP_CURRENT. A complete exact
        # listening ruling therefore resolves the row without a separate
        # meaning-equivalence assessment, but only the strict form: the judge
        # chose CURRENT on OBSERVED audio under the complete-utterance support
        # contract. Uncertain keeps, legacy request-less receipts and other
        # keep branches stay unresolved here.
        return complete_audio_keep_current_ruling(
            adjudication, timing_immutable=timing_immutable)
    return decided_keep_current_adjudication(
        adjudication,
        timing_immutable=timing_immutable,
    ) or decided_history_convergence_disclosure(
        adjudication,
        timing_immutable=timing_immutable,
    )


@cold_input
def complete_audio_keep_current_ruling(
    adjudication: Mapping[str, object],
    *,
    timing_immutable: bool,
) -> bool:
    """A completed listening ruling that the original subtitle is what was said.

    Requires the decided CURRENT base (OBSERVED, NOT_APPLIED, timeline
    untouched), the explicit ``JUDGE_KEEPS_CURRENT`` branch, a request that
    opted into the complete-utterance support contract and a valid support
    receipt for that exact request.
    """

    if adjudication.get('terminal_review'):
        # The new B object reuses this contract, but its actual final answer
        # and real observed support must still match in a cold consumer.
        from src.autoslice.semantic_terminal_adjudication import terminal_keep_matches
        # The row binding is checked by is_release_resolved_keep; validate the
        # complete request/answer here too for direct audio-contract callers.
        request = adjudication['terminal_review']['request']
        row = {'base_text_sha256': hashlib.sha256(request['current_cue'].encode()).hexdigest()}
        try:
            if not terminal_keep_matches(row, adjudication):
                return False
        except (ValueError, TypeError, KeyError, OSError):
            return False
    request = adjudication.get("request")
    return bool(
        adjudication.get("policy_branch") == CURRENT_UTTERANCE_KEEP_BRANCH
        and isinstance(request, Mapping)
        and request.get(REQUIRE_COMPLETE_UTTERANCE_SUPPORT) is True
        and decided_keep_current_adjudication(
            adjudication, timing_immutable=timing_immutable)
    )


def decided_keep_current_adjudication(
    adjudication: Mapping[str, object],
    *,
    timing_immutable: bool,
) -> bool:
    """Shared terminal predicate for a fully decided CURRENT outcome."""

    mutation = adjudication.get("mutation_authority")
    base = bool(
        adjudication.get("status") == "OBSERVED"
        and adjudication.get("policy_branch") in _DECIDED_KEEP_CURRENT_BRANCHES
        and adjudication.get("repaired") is False
        and timing_immutable
        and isinstance(mutation, Mapping)
        and mutation.get("status") == "NOT_APPLIED"
    )
    if not base:
        return False
    request = adjudication.get("request")
    if not isinstance(request, Mapping):
        # Historical ordinary/context receipts predate the request binding;
        # retain their validity unless they explicitly opt into the new exact
        # final support contract.
        return True
    # The stronger proof is scoped to the acoustic relative-CURRENT branch.
    # Other exact branches (for example witness-conflict or target-inaudible
    # KEEP_CURRENT) already carry their own typed decision contract.
    if (
        request.get(REQUIRE_COMPLETE_UTTERANCE_SUPPORT) is not True
        or adjudication.get("policy_branch") != CURRENT_UTTERANCE_KEEP_BRANCH
    ):
        # Historical ordinary/context adjudications retain their old contract.
        return True
    witness_judge = adjudication.get("witness_judge")
    judge = (
        witness_judge.get("judge")
        if isinstance(witness_judge, Mapping)
        else None
    )
    return bool(
        isinstance(witness_judge, Mapping)
        and isinstance(judge, Mapping)
        and valid_current_utterance_support(
            check_request=request,
            judge=judge,
            witness_judge=witness_judge,
        )
    )
