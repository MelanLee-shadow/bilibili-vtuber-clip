import json
from pathlib import Path

import pytest

from src.autoslice.cpa_semantic_review import (
    CPASemanticReviewError,
    CPASemanticReviewResponse,
    apply_cpa_semantic_review,
    load_cpa_semantic_review_response,
)
from src.autoslice.review_evidence import ReviewEvidence


def test_loads_valid_cpa_semantic_review_response(tmp_path):
    path = tmp_path / "candidate.cpa-semantic.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": "cpa-semantic-review-response.v1",
                "candidate_id": "candidate-1",
                "release_ready": True,
                "semantic_complete": True,
                "terminology_ok": True,
                "title_hook_score": 0.86,
                "context_dependency_score": 0.12,
                "reason_codes": [],
                "required_fixes": [],
                "evidence": {"summary": "完整闭环，术语正确"},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    result = load_cpa_semantic_review_response(path, expected_candidate_id="candidate-1")

    assert result.candidate_id == "candidate-1"
    assert result.release_ready is True
    assert result.semantic_complete is True
    assert result.terminology_ok is True
    assert result.reason_codes == ()


def test_cpa_semantic_review_fails_closed_on_invalid_json(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text("not json", encoding="utf-8")

    with pytest.raises(CPASemanticReviewError) as exc:
        load_cpa_semantic_review_response(path, expected_candidate_id="candidate-1")

    assert exc.value.reason_code == "CPA_SEMANTIC_QA_INVALID_JSON"


def test_cpa_semantic_review_fails_closed_on_candidate_mismatch(tmp_path):
    path = tmp_path / "candidate.cpa-semantic.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": "cpa-semantic-review-response.v1",
                "candidate_id": "other",
                "release_ready": True,
                "semantic_complete": True,
                "terminology_ok": True,
                "reason_codes": [],
                "required_fixes": [],
                "evidence": {},
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(CPASemanticReviewError) as exc:
        load_cpa_semantic_review_response(path, expected_candidate_id="candidate-1")

    assert exc.value.reason_code == "CPA_SEMANTIC_QA_CANDIDATE_MISMATCH"


def test_apply_cpa_semantic_review_adds_check_and_blocks_bad_terminology(tmp_path):
    path = tmp_path / "candidate.cpa-semantic.json"
    result = CPASemanticReviewResponse.from_mapping(
        {
            "schema_version": "cpa-semantic-review-response.v1",
            "candidate_id": "candidate-1",
            "release_ready": False,
            "semantic_complete": True,
            "terminology_ok": False,
            "title_hook_score": 0.4,
            "context_dependency_score": 0.2,
            "reason_codes": ["TERMINOLOGY_QA_FAILED"],
            "required_fixes": ["字幕中必须写 kmx，不能写 kimo熊 或天不熊"],
            "evidence": {"terminology_findings": [{"alias": "天不熊", "canonical": "kmx"}]},
        }
    )
    evidence = ReviewEvidence(candidate_id="candidate-1", open_loop_count=0, checks=())

    updated = apply_cpa_semantic_review(evidence, result, response_path=path)

    assert updated.metadata["cpa_semantic_review"]["response_path"] == str(path)
    assert "TERMINOLOGY_QA_FAILED" in updated.evidence_gaps
    assert updated.checks[-1]["code"] == "CPA_SEMANTIC_QA"
    assert updated.checks[-1]["pass"] is False
    assert updated.checks[-1]["reason_code"] == "TERMINOLOGY_QA_FAILED"
