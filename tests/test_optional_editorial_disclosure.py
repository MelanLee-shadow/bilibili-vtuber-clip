"""Current content judgment and optional presentation transport are separate."""

from src.autoslice.discovery_channels import batch_from_rows
import copy
import hashlib
import json

import pytest

from src.autoslice import final_review_auditor as auditor
from src.autoslice.final_review_contract import (
    current_noncritical_editorial_assessment, is_noncritical_disclosed,
)
from src.autoslice.final_review_meaning_policy import POLICY_VERSION
from tests.test_final_review_meaning_policy import equivalent


SRT = "1\n00:00:01,000 --> 00:00:03,000\n谢谢\n"
SHA = "sha256:" + hashlib.sha256(SRT.encode()).hexdigest()


def _current_finding(risk="noncritical_uncertainty"):
    return auditor.audit_final_subtitles(
        SRT, llm_call=lambda _: json.dumps({"findings": [{
            "cue": 1, "kind": "context", "repair_class": "editorial_rendering",
            "suspect": "谢谢", "proposed_full_cue": None,
            "why": "尾音词形不能确定", "content_risk": risk,
            "risk_basis": "主体可靠，尾音未知不足认定关键漏义",
            "meaning_equivalence": equivalent("尾音词形", "说话人表达感谢"),
        }]}, ensure_ascii=False), extract_json=json.loads,
    )[0]


def _unknown_optional_attempt():
    return {"schema_version": "subtitle-editorial-adjudication.v1", "status": "UNCERTAIN",
            "repaired": False, "timing_immutable": True, "reason_code": "LlmCallError",
            "semantic_retry_progress": {"schema_version": "semantic-retry-progress.v1",
                "status": "CALL_OUTCOME_UNKNOWN", "mutation_authorized": False,
                "events": [{"status": "CALL_OUTCOME_UNKNOWN", "provider_calls": None,
                            "request_sha256": "997699dc" + "a" * 56}]}}


def test_completed_current_judgment_does_not_resolve_unknown_without_provider_permission(monkeypatch):
    finding = _current_finding()
    finding["exact_release_adjudication"] = _unknown_optional_attempt()
    before = copy.deepcopy(finding)
    def forbidden(*args, **kwargs):
        raise AssertionError("optional provider question must not be repeated")
    monkeypatch.setattr(auditor.ContextAdjudicationBudget, "adjudicate", forbidden)
    pending, resolved = auditor.adjudicate_exact_release_findings(
        SRT, [finding], entity_verifier=forbidden, judge_llm_call=forbidden,
        provider_calls_allowed=False,
    )
    assert resolved == [] and len(pending) == 1
    assert finding == before
    assert pending[0]["exact_release_adjudication"]["status"] == "SKIPPED_PROVIDER_DISABLED"
    assert finding["exact_release_adjudication"]["semantic_retry_progress"]["status"] == "CALL_OUTCOME_UNKNOWN"


@pytest.mark.parametrize("change", ["missing", "historical", "srt", "cue", "base", "basis",
    "meaning_reversal", "factual_error", "potential_harm", "mandatory", "mutation", "applied"])
def test_stale_or_mandatory_unknown_has_no_optional_disclosure(change):
    finding = _current_finding()
    finding["exact_release_adjudication"] = _unknown_optional_attempt()
    risk = finding["current_content_risk"]
    if change == "missing": finding.pop("current_content_risk")
    elif change == "historical": risk["decision_authority"] = "HISTORICAL_REVIEW"
    elif change == "srt": risk["reviewed_srt_sha256"] = "sha256:" + "b" * 64
    elif change == "cue": risk["cue_index"] = 2
    elif change == "base": risk["base_text_sha256"] = "b" * 64
    elif change == "basis": finding["risk_basis"] = "different current evidence"
    elif change in {"meaning_reversal", "factual_error", "potential_harm"}:
        finding["content_risk"] = risk["content_risk"] = change
    elif change == "mandatory": finding["exact_release_adjudication"]["schema_version"] = "subtitle-span-acoustic-adjudication.v1"
    elif change == "mutation": finding["exact_release_adjudication"]["mutation_authority"] = {"status": "PASS"}
    elif change == "applied": finding["exact_release_adjudication"]["repaired"] = True
    assert not current_noncritical_editorial_assessment(finding, expected_srt_sha256=SHA)
    if change != "srt": assert not is_noncritical_disclosed(finding)


def test_stale_whole_srt_reaches_mandatory_adjudicator(monkeypatch):
    finding = _current_finding()
    finding["exact_release_adjudication"] = _unknown_optional_attempt()
    calls = []
    def blocked(self, source, row):
        calls.append(source)
        return lambda: {"status": "ERROR", "repaired": False}
    monkeypatch.setattr(auditor.ContextAdjudicationBudget, "prepare", blocked)
    pending, _ = auditor.adjudicate_exact_release_findings(
        SRT.replace("03,000", "04,000"), [finding], entity_verifier=None,
    )
    assert len(calls) == 1 and not is_noncritical_disclosed(pending[0])


def test_history_label_cannot_create_a_current_content_assessment():
    historical = {"cue": 1, "kind": "context", "repair_class": "editorial_rendering",
        "suspect": "谢谢", "proposed_full_cue": None, "why": "old doubt",
        "content_risk": "noncritical_uncertainty", "risk_basis": "old opinion"}
    with pytest.raises(auditor.FinalReviewAuditError, match="FINAL_REVIEW_PRIORITY_RISK_INCOMPLETE"):
        auditor.audit_final_subtitles(SRT, llm_call=lambda _: '{"findings": []}',
            extract_json=json.loads, discovery_batch=batch_from_rows([historical]))


def test_release_checks_current_risk_hash_and_keeps_boundary_mandatory():
    from src.autoslice.final_review_contract import FinalReviewContractError, validate_final_review_release
    finding = _current_finding()
    finding["exact_release_adjudication"] = {
        "status": "OBSERVED", "repaired": False, "timing_immutable": True,
        "policy_branch": "JUDGE_KEEPS_CURRENT", "mutation_authority": {"status": "NOT_APPLIED"},
    }
    grid = "sha256:" + "d" * 64
    boundary = {"status": "PASS", "review_scope": "final_delivery",
        "request_sha256": "sha256:" + "e" * 64, "cue_grid_sha256": grid,
        "final_endpoint_binding": {"schema_version": "talk-boundary-final-endpoint-binding.v1",
            "status": "PASS", "recommended_end_cue_index": 1, "recommended_end_ms": 3000,
            "final_closure_cue_index": 1, "final_snapped_end_ms": 3000, "reason_codes": [],
            "semantic_cue_grid_sha256": grid, "final_cue_grid_sha256": grid},
        "source_separation_witness": {"schema_version": "talk-boundary-source-separation-witness.v1",
            "status": "PASS", "source_review_sha256": "sha256:" + "f" * 64,
            "source_request_sha256": "sha256:" + "0" * 64,
            "source_cue_grid_sha256": "sha256:" + "1" * 64,
            "source_final_start_ms": 1000, "source_final_end_ms": 3000, "reason_codes": []}}
    audit = {"schema_version": "final-review-audit.v2", "meaning_policy": POLICY_VERSION, "reviewed_srt_sha256": SHA,
        "status": "CLEAN", "release_gate": "PASS", "discovery": {"status": "COMPLETE"},
        "correction_mutation_authority": {"schema_version": "subtitle-correction-mutation-audit.v1", "status": "PASS"},
        "findings": [], "validated_finding_count": 0, "unresolved_findings_disclosed": [finding],
        "boundary_semantic_review": boundary}
    before = copy.deepcopy(audit)
    assert validate_final_review_release(audit, expected_srt_sha256=SHA)["release_gate"] == "PASS"
    assert audit == before
    finding["current_content_risk"]["reviewed_srt_sha256"] = "sha256:" + "b" * 64
    with pytest.raises(FinalReviewContractError, match="FINAL_REVIEW_CURRENT_RISK_DRIFT"):
        validate_final_review_release(audit, expected_srt_sha256=SHA)
    finding["current_content_risk"]["reviewed_srt_sha256"] = SHA
    boundary["status"] = "UNAVAILABLE"
    with pytest.raises(FinalReviewContractError, match="FINAL_REVIEW_BOUNDARY_SEMANTIC_BLOCKED"):
        validate_final_review_release(audit, expected_srt_sha256=SHA)
