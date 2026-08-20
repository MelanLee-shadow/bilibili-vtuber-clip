"""Fail-closed validation for zero-mutation human-truth review skips."""

from __future__ import annotations

from typing import Mapping


_SKIPPED_STAGES = frozenset(
    {
        "producer_text_pipeline._run_final_review",
        "chat_repair.apply_audio_entity_verification",
        "microcue_acoustic_discovery.discover_microcue_findings",
        "restatement_recall.merge_restatement_priority_findings",
    }
)
_PRESERVED_STAGES = frozenset(
    {
        "producer_text_pipeline._run_exact_final_release_review",
        "boundary_semantic_review.review_final_boundary_semantics",
        "producer_package_finalization._verify_final_authority",
        "producer_package_finalization._build_and_burn_record",
    }
)


def _clean_sha256(value: object) -> str:
    text = str(value or "").strip().removeprefix("sha256:")
    if len(text) == 64 and all(char in "0123456789abcdef" for char in text):
        return text
    return ""


def _common_receipt_valid(ownership: Mapping[str, object]) -> bool:
    skipped = ownership.get("skipped_stages")
    if (
        not isinstance(skipped, list)
        or len(skipped) != len(_SKIPPED_STAGES)
        or any(not isinstance(row, Mapping) for row in skipped)
    ):
        return False
    if {str(row.get("stage") or "") for row in skipped} != _SKIPPED_STAGES:
        return False
    if any(
        not str(row.get("kind") or "").strip()
        or not str(row.get("reason_code") or "").strip()
        for row in skipped
    ):
        return False
    preserved = ownership.get("preserved_stages")
    if not isinstance(preserved, list) or not _PRESERVED_STAGES.issubset(
        {str(row) for row in preserved}
    ):
        return False
    conditions = ownership.get("effective_conditions")
    return bool(
        isinstance(conditions, list)
        and len(conditions) >= 4
        and all(str(row).strip() for row in conditions)
    )


def _common_coverage(
    ownership: Mapping[str, object],
) -> tuple[Mapping[str, object], Mapping[str, object], int] | None:
    coverage = ownership.get("coverage")
    if not isinstance(coverage, Mapping):
        return None
    cue_count = coverage.get("cue_count")
    if isinstance(cue_count, bool) or not isinstance(cue_count, int) or cue_count <= 0:
        return None
    proof = coverage.get("proof")
    if not isinstance(proof, Mapping):
        return None
    start_ms = proof.get("absolute_source_start_ms")
    end_ms = proof.get("absolute_source_end_ms")
    authority = proof.get("authority")
    operator_authority = proof.get("operator_authority")
    typed_operator_authority = (
        isinstance(operator_authority, Mapping)
        and set(operator_authority) == {"kind", "evidence_ref"}
        and operator_authority.get("kind") == "IVAN_OPERATOR"
        and isinstance(operator_authority.get("evidence_ref"), str)
        and operator_authority["evidence_ref"].strip()
    )
    if not (
        _clean_sha256(proof.get("baseline_sha256"))
        and _clean_sha256(proof.get("source_sha256"))
        and (str(authority or "").strip() or typed_operator_authority)
        and str(proof.get("source_recording_basename") or "").strip()
        and isinstance(start_ms, int)
        and not isinstance(start_ms, bool)
        and isinstance(end_ms, int)
        and not isinstance(end_ms, bool)
        and 0 <= start_ms < end_ms
    ):
        return None
    return coverage, proof, cue_count


def _operator_text_receipt_valid(
    coverage: Mapping[str, object],
    proof: Mapping[str, object],
    cue_count: int,
) -> bool:
    if set(coverage) != {
        "cue_count",
        "reviewed_text_cue_count",
        "changed_text_cue_count",
        "operator_exact_text_cue_count",
        "unchanged_freeze_cue_count",
        "text_ownership",
        "speaker_ownership",
        "proof",
    } or set(proof) != {
        "baseline_sha256",
        "pipeline_srt_sha256",
        "decision_ledger_sha256",
        "diagnostic_diff_sha256",
        "operator_authority",
        "truth_lanes",
        "source_recording_basename",
        "source_sha256",
        "absolute_source_start_ms",
        "absolute_source_end_ms",
    }:
        return False
    reviewed_text = coverage.get("reviewed_text_cue_count")
    changed = coverage.get("changed_text_cue_count")
    exact = coverage.get("operator_exact_text_cue_count")
    frozen = coverage.get("unchanged_freeze_cue_count")
    authority = proof.get("operator_authority")
    truth_lanes = proof.get("truth_lanes")
    lanes_valid = (
        isinstance(truth_lanes, Mapping)
        and set(truth_lanes)
        == {
            "schema_version",
            "release_truth",
            "pipeline_diagnostic",
            "decision_ledger",
            "diff_receipt",
        }
        and truth_lanes.get("schema_version")
        == "operator-reviewed-subtitle-truth-lanes.v1"
        and isinstance(truth_lanes.get("release_truth"), Mapping)
        and _clean_sha256(truth_lanes["release_truth"].get("srt_sha256"))
        == _clean_sha256(proof.get("baseline_sha256"))
    )
    if lanes_valid:
        for lane, proof_key in (
            ("pipeline_diagnostic", "pipeline_srt_sha256"),
            ("decision_ledger", "decision_ledger_sha256"),
            ("diff_receipt", "diagnostic_diff_sha256"),
        ):
            value = truth_lanes[lane]
            if (
                not isinstance(value, Mapping)
                or set(value) != {"path", "sha256"}
                or not str(value.get("path") or "").strip()
                or _clean_sha256(value.get("sha256"))
                != _clean_sha256(proof.get(proof_key))
            ):
                lanes_valid = False
                break
    return bool(
        isinstance(reviewed_text, int)
        and not isinstance(reviewed_text, bool)
        and reviewed_text == cue_count
        and isinstance(changed, int)
        and not isinstance(changed, bool)
        and isinstance(exact, int)
        and not isinstance(exact, bool)
        and 1 <= exact <= cue_count
        and 0 <= changed <= exact
        and isinstance(frozen, int)
        and not isinstance(frozen, bool)
        and frozen >= 0
        and exact + frozen == cue_count
        and coverage.get("text_ownership")
        == "EXACT_INTERVAL_REPLAY_OF_OPERATOR_REVIEWED_BASELINE"
        and coverage.get("speaker_ownership") == "NOT_CLAIMED_TEXT_ONLY"
        and _clean_sha256(proof.get("pipeline_srt_sha256"))
        and _clean_sha256(proof.get("decision_ledger_sha256"))
        and _clean_sha256(proof.get("diagnostic_diff_sha256"))
        and isinstance(authority, Mapping)
        and set(authority) == {"kind", "evidence_ref"}
        and authority.get("kind") == "IVAN_OPERATOR"
        and isinstance(authority.get("evidence_ref"), str)
        and authority["evidence_ref"].strip()
        and lanes_valid
    )


def _arbitration_valid(
    proof: Mapping[str, object], *, machine_count: object, truth_input_sha256: str
) -> bool:
    arbitration_sha256 = _clean_sha256(proof.get("arbitration_receipt_sha256"))
    disposition = proof.get("arbitration_disposition")
    return bool(
        (machine_count and arbitration_sha256 and disposition is None)
        or (
            machine_count == 0
            and (
                arbitration_sha256
                or (
                    isinstance(disposition, Mapping)
                    and disposition.get("schema_version")
                    == "reviewed-speaker-arbitration-disposition.v1"
                    and disposition.get("status")
                    == "NOT_REQUIRED_FULLY_LABELLED"
                    and _clean_sha256(disposition.get("truth_input_sha256"))
                    == truth_input_sha256
                    and _clean_sha256(
                        disposition.get("automatic_labelled_srt_sha256")
                    )
                )
            )
        )
    )


def _speaker_truth_receipt_valid(
    coverage: Mapping[str, object],
    proof: Mapping[str, object],
    cue_count: int,
) -> bool:
    if set(coverage) != {
        "cue_count",
        "reviewed_override_count",
        "machine_speaker_cue_count",
        "machine_speaker_cues",
        "text_ownership",
        "speaker_ownership",
        "proof",
    } or set(proof) != {
        "truth_input",
        "baseline_sha256",
        "arbitration_receipt_sha256",
        "arbitration_disposition",
        "authority",
        "source_recording_basename",
        "source_sha256",
        "absolute_source_start_ms",
        "absolute_source_end_ms",
    }:
        return False
    reviewed = coverage.get("reviewed_override_count")
    machine_count = coverage.get("machine_speaker_cue_count")
    machine_cues = coverage.get("machine_speaker_cues")
    machine_cues_valid = bool(
        isinstance(machine_cues, list)
        and all(
            isinstance(value, int)
            and not isinstance(value, bool)
            and 1 <= value <= cue_count
            for value in machine_cues
        )
    )
    truth_input = proof.get("truth_input")
    truth_input_sha256 = (
        _clean_sha256(truth_input.get("sha256"))
        if isinstance(truth_input, Mapping)
        else ""
    )
    return bool(
        isinstance(reviewed, int)
        and not isinstance(reviewed, bool)
        and reviewed >= 0
        and isinstance(machine_count, int)
        and not isinstance(machine_count, bool)
        and machine_count >= 0
        and reviewed + machine_count == cue_count
        and machine_cues_valid
        and len(machine_cues) == machine_count
        and len(set(machine_cues)) == machine_count
        and coverage.get("text_ownership")
        == "EXACT_INTERVAL_REPLAY_OF_TRUTH_COMPILED_BASELINE"
        and coverage.get("speaker_ownership")
        == "REVIEWED_OVERRIDES_PLUS_POSITIVE_VOICE_ARBITRATED_MACHINE_CUES"
        and isinstance(truth_input, Mapping)
        and str(truth_input.get("path") or "").strip()
        and truth_input_sha256
        and _arbitration_valid(
            proof,
            machine_count=machine_count,
            truth_input_sha256=truth_input_sha256,
        )
    )


def valid_truth_ownership_zero_mutation_skip(ownership: object) -> bool:
    """Revalidate a typed F20 receipt before exact release accepts the skip."""

    if not isinstance(ownership, Mapping) or set(ownership) != {
        "schema_version",
        "status",
        "coverage",
        "skipped_stages",
        "preserved_stages",
        "effective_conditions",
    }:
        return False
    schema = ownership.get("schema_version")
    expected_status = {
        "truth_full_ownership.v1": "TRUTH_FULL_OWNERSHIP",
        "operator_text_full_ownership.v1": "OPERATOR_TEXT_FULL_OWNERSHIP",
    }.get(str(schema))
    if not expected_status or ownership.get("status") != expected_status:
        return False
    if not _common_receipt_valid(ownership):
        return False
    common = _common_coverage(ownership)
    if common is None:
        return False
    coverage, proof, cue_count = common
    if schema == "operator_text_full_ownership.v1":
        return _operator_text_receipt_valid(coverage, proof, cue_count)
    return _speaker_truth_receipt_valid(coverage, proof, cue_count)


def audit_zero_mutation_correction_skip(
    correction_audit: Mapping[str, object],
) -> dict[str, object] | None:
    """Return a typed mutation receipt for a known skip, else ``None``."""

    status = str(correction_audit.get("status") or "")
    findings = correction_audit.get("findings")
    applied_count = correction_audit.get("applied_count")
    if status == "SKIPPED_PINNED_REPLAY":
        ownership = correction_audit.get("pinned_replay_ownership")
        if (
            isinstance(ownership, Mapping)
            and ownership.get("exact_interval_replay") is True
            and bool(str(ownership.get("baseline_sha256") or "").strip())
            and bool(
                str(ownership.get("publication_authority_sha256") or "").strip()
            )
            and (findings is None or findings == [])
        ):
            return {
                "schema_version": "subtitle-correction-mutation-audit.v1",
                "status": "PASS",
                "applied_count": 0,
                "validated_mutation_count": 0,
                "failures": [],
                "pinned_replay_skip": dict(ownership),
            }
        return {
            "schema_version": "subtitle-correction-mutation-audit.v1",
            "status": "BLOCK",
            "applied_count": applied_count,
            "validated_mutation_count": 0,
            "failures": [{"reason_code": "PINNED_REPLAY_SKIP_UNDISCLOSED"}],
        }
    if status != "SKIPPED_TRUTH_FULL_OWNERSHIP":
        return None
    ownership = correction_audit.get("truth_full_ownership")
    if (
        findings == []
        and applied_count == 0
        and valid_truth_ownership_zero_mutation_skip(ownership)
    ):
        return {
            "schema_version": "subtitle-correction-mutation-audit.v1",
            "status": "PASS",
            "applied_count": 0,
            "validated_mutation_count": 0,
            "failures": [],
            "truth_full_ownership_skip": dict(ownership),
        }
    return {
        "schema_version": "subtitle-correction-mutation-audit.v1",
        "status": "BLOCK",
        "applied_count": applied_count,
        "validated_mutation_count": 0,
        "failures": [{"reason_code": "TRUTH_FULL_OWNERSHIP_SKIP_UNDISCLOSED"}],
    }
