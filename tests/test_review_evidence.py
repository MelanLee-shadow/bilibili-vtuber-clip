import json

from src.autoslice.auto_review import DecisionAction, JingtingProvenance, review_candidate
from src.autoslice.review_evidence import ReviewEvidence, SourceCue, to_candidate_review


def good_provenance():
    return JingtingProvenance(
        manifest_present=True,
        provider="agy",
        agy_rc=0,
        model="Gemini 3.6 Flash (Low)",
        provider_fallback_used=False,
    )


def complete_evidence(**overrides):
    data = {
        "candidate_id": "clip-ok",
        "foreground_song_overlap_seconds": 0.0,
        "song_complete": True,
        "lyrics_alignment_ready": True,
        "start_boundary_score": 0.97,
        "end_boundary_score": 0.98,
        "standalone_score": 0.94,
        "payoff_score": 0.95,
        "open_loop_count": 0,
        "editorial_score": 88.0,
        "duplicate_similarity": 0.10,
        "subtitle_alignment_p95_ms": 120.0,
        "actual_cut_error_ms": 20.0,
        "evidence_gaps": (),
        "checks": (),
    }
    data.update(overrides)
    return ReviewEvidence(**data)


def test_review_required_marker_wires_release_ready_and_findings():
    evidence = complete_evidence()

    candidate = to_candidate_review(
        evidence,
        good_provenance(),
        review_required={"release_ready": False, "findings": ["LEXICON_LEAK", "TIMING_DRIFT"]},
    )
    decision = review_candidate(candidate)

    assert candidate.release_ready is False
    assert candidate.review_required_findings == ("LEXICON_LEAK", "TIMING_DRIFT")
    assert decision.action == DecisionAction.BLOCK
    assert "JINGTING_REVIEW_REQUIRED" in decision.reason_codes


def test_review_required_marker_present_but_malformed_fails_closed():
    evidence = complete_evidence()

    candidate = to_candidate_review(evidence, good_provenance(), review_required={})
    decision = review_candidate(candidate)

    assert candidate.release_ready is False
    assert decision.action == DecisionAction.BLOCK
    assert "JINGTING_REVIEW_REQUIRED" in decision.reason_codes


def test_no_review_required_marker_keeps_release_ready_true():
    evidence = complete_evidence()

    candidate = to_candidate_review(evidence, good_provenance(), review_required=None)

    assert candidate.release_ready is True
    assert candidate.review_required_findings == ()


def test_missing_required_evidence_fails_closed_without_jingting_review_label():
    evidence = complete_evidence(start_boundary_score=None, evidence_gaps=("START_BOUNDARY_MISSING",))

    candidate = to_candidate_review(evidence, good_provenance())
    decision = review_candidate(candidate)

    assert candidate.start_boundary_score is None
    assert candidate.release_ready is True
    assert candidate.review_required_findings == ()
    assert decision.action == DecisionAction.BLOCK
    assert "START_BOUNDARY_MISSING" in decision.reason_codes
    assert "JINGTING_REVIEW_REQUIRED" not in decision.reason_codes


def test_complete_evidence_converts_non_missing_candidate_fields():
    evidence = complete_evidence()

    candidate = to_candidate_review(evidence, good_provenance())
    decision = review_candidate(candidate)

    assert candidate.jingting_done is True
    assert candidate.release_ready is True
    assert candidate.review_required_findings == ()
    assert candidate.foreground_song_overlap_seconds == 0.0
    assert candidate.start_boundary_score == 0.97
    assert candidate.actual_cut_error_ms == 20.0
    assert decision.action == DecisionAction.AUTO_UPLOAD


def test_review_evidence_manifest_is_json_serializable():
    evidence = complete_evidence(
        source_cues=(
            SourceCue(
                cue_id="u_000001",
                source_start_ms=3814200,
                source_end_ms=3817650,
                text="我是小皇帝",
                language="zh",
                kind="speech",
                speaker="host",
                confidence=0.91,
            ),
        ),
        checks=({"code": "SOURCE_CUE_TIMING", "pass": True},),
    )

    manifest = evidence.to_manifest()
    encoded = json.dumps(manifest, ensure_ascii=False, sort_keys=True)

    assert "source_start_ms" in encoded
    assert manifest["source_cues"][0]["source_start_ms"] == 3814200
    assert manifest["source_cues"][0]["source_end_ms"] == 3817650


def test_source_cue_uses_source_absolute_ms_not_clip_relative_as_identity():
    cue = SourceCue(
        cue_id="u_abs",
        source_start_ms=97_000,
        source_end_ms=101_200,
        text="绝对时间线上的字幕",
    )

    assert cue.to_manifest()["source_start_ms"] == 97_000
    assert "start_ms" not in cue.to_manifest()
