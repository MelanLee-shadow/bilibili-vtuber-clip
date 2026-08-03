from src.autoslice.auto_review import DecisionAction, review_candidate
from src.autoslice.render_qa import RenderRequest, RenderedTimelineMetadata, evaluate_render_pts
from tests.test_auto_review import base_candidate


def test_render_pts_qa_computes_max_start_or_end_cut_error_when_metadata_available():
    qa = evaluate_render_pts(
        RenderRequest(candidate_id="clip-pts", requested_start_ms=10_250, requested_end_ms=42_750),
        RenderedTimelineMetadata(actual_start_ms=10_180, actual_end_ms=42_870),
    )

    assert qa.metadata_available
    assert qa.actual_cut_error_ms == 120
    assert qa.reason_codes == ("ACTUAL_CUT_ERROR_HIGH",)
    assert qa.to_manifest_check() == {
        "code": "ACTUAL_CUT_ERROR_HIGH",
        "pass": False,
        "severity": "AUTO_RECUT",
        "evidence": {
            "requested_start_ms": 10_250,
            "requested_end_ms": 42_750,
            "actual_start_ms": 10_180,
            "actual_end_ms": 42_870,
            "start_error_ms": 70,
            "end_error_ms": 120,
            "actual_cut_error_ms": 120,
            "threshold_ms": 100,
        },
    }


def test_render_pts_qa_passes_at_100ms_cut_error_threshold():
    qa = evaluate_render_pts(
        RenderRequest(candidate_id="clip-threshold", requested_start_ms=1_000, requested_end_ms=5_000),
        RenderedTimelineMetadata(actual_start_ms=900, actual_end_ms=5_080),
    )

    assert qa.metadata_available
    assert qa.passed
    assert qa.actual_cut_error_ms == 100
    assert qa.reason_codes == ()


def test_auto_review_recuts_high_actual_cut_error_and_never_auto_uploads_it():
    decision = review_candidate(
        base_candidate(candidate_id="keyframe-shifted", actual_cut_error_ms=101, recut_attempt=0)
    )

    assert decision.action == DecisionAction.AUTO_RECUT
    assert decision.reason_codes == ("ACTUAL_CUT_ERROR_HIGH",)


def test_auto_review_blocks_high_actual_cut_error_after_recut_budget_exhausted():
    decision = review_candidate(
        base_candidate(
            candidate_id="still-keyframe-shifted",
            actual_cut_error_ms=101,
            recut_attempt=2,
            max_recut_attempts=2,
        )
    )

    assert decision.action == DecisionAction.BLOCK
    assert decision.reason_codes == ("ACTUAL_CUT_ERROR_HIGH", "RECUT_BUDGET_EXHAUSTED")


def test_auto_review_recuts_soft_subtitle_p95_alignment_error():
    decision = review_candidate(
        base_candidate(candidate_id="subtitle-soft-offset", subtitle_alignment_p95_ms=351, recut_attempt=0)
    )

    assert decision.action == DecisionAction.AUTO_RECUT
    assert decision.reason_codes == ("SUBTITLE_ALIGNMENT_RETRY",)


def test_auto_review_blocks_bad_subtitle_p95_alignment_error():
    decision = review_candidate(
        base_candidate(candidate_id="subtitle-bad-offset", subtitle_alignment_p95_ms=801)
    )

    assert decision.action == DecisionAction.BLOCK
    assert decision.reason_codes == ("SUBTITLE_ALIGNMENT_BAD",)
