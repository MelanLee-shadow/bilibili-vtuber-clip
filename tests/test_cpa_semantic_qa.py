import json
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

from src.autoslice.cpa_semantic_qa import (
    COMPLETE_SONG_SEMANTIC_WAIVED_REASONS,
    CpaSemanticQaRequest,
    CpaSemanticQaResponse,
    CpaSemanticSourceRef,
    CpaTerminologyContext,
    apply_cpa_semantic_qa_to_review_evidence,
    build_mock_cpa_response,
    evaluate_cpa_semantic_response_artifact,
    validate_cpa_semantic_request_payload,
    write_cpa_semantic_request_artifact,
    write_cpa_semantic_response_artifact,
)
from src.autoslice.review_evidence import ReviewEvidence


ROOT = Path(__file__).resolve().parents[1]


def base_evidence(candidate_id: str = "clip-cpa") -> ReviewEvidence:
    return ReviewEvidence(
        candidate_id=candidate_id,
        foreground_song_overlap_seconds=0.0,
        song_complete=True,
        lyrics_alignment_ready=True,
        start_boundary_score=0.97,
        end_boundary_score=0.98,
        standalone_score=0.94,
        payoff_score=0.95,
        open_loop_count=0,
        editorial_score=88.0,
        duplicate_similarity=0.10,
        subtitle_alignment_p95_ms=120.0,
        actual_cut_error_ms=20.0,
    )


def complete_song_evidence(candidate_id: str = "clip-cpa") -> ReviewEvidence:
    return replace(
        base_evidence(candidate_id),
        foreground_song_overlap_seconds=188.0,
        song_complete=True,
        lyrics_alignment_ready=True,
        standalone_score=0.94,
        payoff_score=0.95,
    )


def base_request(tmp_path: Path, **overrides) -> CpaSemanticQaRequest:
    data = {
        "candidate_id": "clip-cpa",
        "room_id": "22966160",
        "source": CpaSemanticSourceRef(
            video_path=str(tmp_path / "source.mp4"),
            srt_path=str(tmp_path / "source.srt"),
            start_ms=1000,
            end_ms=6000,
            source_cues_path=str(tmp_path / "source-cues.json"),
            review_evidence_path=str(tmp_path / "review-evidence.json"),
        ),
        "candidate_text": "天不熊被骗到了，大家都笑了。",
        "normalized_text": "kmx被骗到了，大家都笑了。",
        "response_path": str(tmp_path / "cpa.response.json"),
        "terminology": CpaTerminologyContext(
            applied_terms=("kmx",),
            evidence_paths=("lidousha/2026-06-19/example.manual-edited.zh.srt",),
        ),
    }
    data.update(overrides)
    return CpaSemanticQaRequest(**data)


def test_request_artifact_is_parseable_and_fail_closed_fields_present(tmp_path):
    request = base_request(tmp_path)
    request_path = tmp_path / "cpa.request.json"
    request = write_cpa_semantic_request_artifact(request, request_path)

    payload = json.loads(request_path.read_text(encoding="utf-8"))
    assert validate_cpa_semantic_request_payload(payload) == ()
    assert payload["artifact_paths"]["request_path"] == str(request_path)
    assert payload["artifact_paths"]["response_path"] == str(tmp_path / "cpa.response.json")
    assert payload["checks_requested"]


def test_missing_response_fails_closed_and_is_written_into_review_evidence(tmp_path):
    request = write_cpa_semantic_request_artifact(base_request(tmp_path), tmp_path / "cpa.request.json")

    evaluation = evaluate_cpa_semantic_response_artifact(request, Path(request.response_path))
    enriched = apply_cpa_semantic_qa_to_review_evidence(base_evidence(), evaluation)

    assert evaluation.passed is False
    assert evaluation.reason_codes == ("CPA_SEMANTIC_QA_MISSING",)
    assert enriched.checks[-1]["code"] == "CPA_SEMANTIC_QA"
    assert enriched.checks[-1]["severity"] == "BLOCK"
    assert "CPA_SEMANTIC_QA_MISSING" in enriched.evidence_gaps
    assert enriched.metadata["cpa_semantic_qa"]["response_path"] == request.response_path


def test_candidate_mismatch_response_fails_closed(tmp_path):
    request = write_cpa_semantic_request_artifact(base_request(tmp_path), tmp_path / "cpa.request.json")
    bad = CpaSemanticQaResponse(
        candidate_id="other-candidate",
        request_sha256=request.canonical_sha256(),
        release_ready=True,
        semantic_complete=True,
        terminology_ok=True,
        title_hook_score=0.8,
        context_dependency_score=0.2,
        unsafe_upload_risk_score=0.1,
        reason_codes=(),
        required_fixes=(),
        summary="mismatch",
        request_path=request.request_path,
        response_path=request.response_path,
    )
    write_cpa_semantic_response_artifact(bad, Path(request.response_path))

    evaluation = evaluate_cpa_semantic_response_artifact(request, Path(request.response_path))

    assert evaluation.passed is False
    assert "CPA_SEMANTIC_QA_CANDIDATE_MISMATCH" in evaluation.reason_codes


def test_non_release_ready_response_derives_reason_codes_and_required_fixes(tmp_path):
    request = write_cpa_semantic_request_artifact(
        base_request(tmp_path, normalized_text="然后她又提这个事情"),
        tmp_path / "cpa.request.json",
    )
    response = build_mock_cpa_response(request)
    write_cpa_semantic_response_artifact(response, Path(request.response_path))

    evaluation = evaluate_cpa_semantic_response_artifact(request, Path(request.response_path))
    enriched = apply_cpa_semantic_qa_to_review_evidence(base_evidence(), evaluation)

    assert evaluation.passed is False
    assert "CPA_SEMANTIC_INCOMPLETE" in evaluation.reason_codes
    assert "CONTEXT_DEPENDENCY_HIGH" in evaluation.reason_codes
    assert "CPA_RELEASE_NOT_READY" in evaluation.reason_codes
    assert enriched.checks[-1]["pass"] is False


def test_complete_song_waives_boring_and_context_cpa_blocks(tmp_path):
    request = write_cpa_semantic_request_artifact(base_request(tmp_path), tmp_path / "cpa.request.json")
    response = CpaSemanticQaResponse(
        candidate_id=request.candidate_id,
        request_sha256=request.canonical_sha256(),
        release_ready=False,
        semantic_complete=False,
        terminology_ok=True,
        title_hook_score=0.25,
        context_dependency_score=0.82,
        unsafe_upload_risk_score=0.05,
        reason_codes=("NOT_INTERESTING",),
        required_fixes=("不要因为完整歌不是聊天段子而要求补包袱",),
        summary="LLM mistakenly judged a complete song as boring/context dependent",
        request_path=request.request_path,
        response_path=request.response_path,
    )
    write_cpa_semantic_response_artifact(response, Path(request.response_path))

    evaluation = evaluate_cpa_semantic_response_artifact(request, Path(request.response_path))
    enriched = apply_cpa_semantic_qa_to_review_evidence(complete_song_evidence(), evaluation)

    assert evaluation.passed is False
    assert "NOT_INTERESTING" in evaluation.reason_codes
    assert "CONTEXT_DEPENDENCY_HIGH" in evaluation.reason_codes
    assert enriched.checks[-1]["pass"] is True
    assert enriched.checks[-1]["severity"] == "PASS"
    assert enriched.checks[-1]["reason_codes"] == []
    assert "NOT_INTERESTING" not in enriched.evidence_gaps
    assert "CONTEXT_DEPENDENCY_HIGH" not in enriched.evidence_gaps
    qa_metadata = enriched.metadata["cpa_semantic_qa"]
    assert isinstance(qa_metadata, dict)
    policy = qa_metadata["complete_song_policy"]
    assert isinstance(policy, dict)
    assert policy["applied"] is True
    assert "NOT_INTERESTING" in policy["waived_reason_codes"]
    assert policy["rules"]["complete_song_semantic_waived_reasons"] == sorted(COMPLETE_SONG_SEMANTIC_WAIVED_REASONS)


def test_complete_song_policy_does_not_waive_terminology_or_unsafe_risk(tmp_path):
    request = write_cpa_semantic_request_artifact(base_request(tmp_path), tmp_path / "cpa.request.json")
    response = CpaSemanticQaResponse(
        candidate_id=request.candidate_id,
        request_sha256=request.canonical_sha256(),
        release_ready=False,
        semantic_complete=True,
        terminology_ok=False,
        title_hook_score=0.25,
        context_dependency_score=0.82,
        unsafe_upload_risk_score=0.80,
        reason_codes=("NOT_INTERESTING", "TERMINOLOGY_QA_FAILED", "UNSAFE_UPLOAD_RISK"),
        required_fixes=("修正术语并排除上传风险",),
        summary="Complete song still has real blockers",
        request_path=request.request_path,
        response_path=request.response_path,
    )
    write_cpa_semantic_response_artifact(response, Path(request.response_path))

    evaluation = evaluate_cpa_semantic_response_artifact(request, Path(request.response_path))
    enriched = apply_cpa_semantic_qa_to_review_evidence(complete_song_evidence(), evaluation)

    assert enriched.checks[-1]["pass"] is False
    reason_codes = enriched.checks[-1]["reason_codes"]
    assert isinstance(reason_codes, list)
    assert "NOT_INTERESTING" not in reason_codes
    assert "CONTEXT_DEPENDENCY_HIGH" not in reason_codes
    assert "TERMINOLOGY_QA_FAILED" in reason_codes
    assert "UNSAFE_UPLOAD_RISK" in reason_codes
    assert "CPA_UNSAFE_UPLOAD_RISK_HIGH" in reason_codes


def test_cli_mock_runner_writes_response_and_passes_review_evidence(tmp_path):
    request_path = tmp_path / "cpa.request.json"
    request = write_cpa_semantic_request_artifact(base_request(tmp_path), request_path)
    response_path = Path(request.response_path)

    completed = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "run_cpa_semantic_qa.py"), "--request", str(request_path), "--response", str(response_path)],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert response_path.is_file()
    evaluation = evaluate_cpa_semantic_response_artifact(request, response_path)
    enriched = apply_cpa_semantic_qa_to_review_evidence(base_evidence(), evaluation)

    assert evaluation.passed is True
    assert evaluation.reason_codes == ()
    assert enriched.checks[-1]["severity"] == "PASS"
    assert enriched.metadata["cpa_semantic_qa"]["response_evidence"]["summary"]
