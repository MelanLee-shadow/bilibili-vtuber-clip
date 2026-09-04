import pytest

from src.autoslice.operator_correction_policy import plan_operator_correction


@pytest.mark.parametrize("count", [1, 2])
def test_one_or_two_unqualified_reports_require_whole_clip_rerun(count):
    plan = plan_operator_correction(candidate_id="auto_x", issue_count=count)
    assert plan["mode"] == "WHOLE_CLIP_RERUN_AND_REVIEW"
    assert plan["whole_clip_rerun_required"] is True


def test_exactly_three_reports_conservatively_require_whole_clip_review():
    plan = plan_operator_correction(candidate_id="auto_x", issue_count=3)
    assert plan["mode"] == "WHOLE_CLIP_RERUN_AND_REVIEW"
    assert plan["whole_clip_rerun_required"] is True


def test_more_than_three_reports_use_targeted_exhaustive_repairs():
    plan = plan_operator_correction(candidate_id="auto_x", issue_count=4)
    assert plan["mode"] == "TARGETED_REPAIR_PLUS_SYSTEMIC_FIX"
    assert plan["operator_scope"] == "EXHAUSTIVE_CANDIDATE"
    assert plan["targeted_locations_only"] is True
    assert plan["requires_change_coverage_proof"] is True
    assert plan["systemic_pipeline_fix_required"] is True


def test_explicitly_exhaustive_short_report_is_targeted():
    plan = plan_operator_correction(
        candidate_id="auto_x",
        issue_count=1,
        explicitly_exhaustive=True,
    )
    assert plan["mode"] == "TARGETED_REPAIR_PLUS_SYSTEMIC_FIX"


@pytest.mark.parametrize("count", [0, -1])
def test_invalid_issue_count_is_rejected(count):
    with pytest.raises(ValueError):
        plan_operator_correction(candidate_id="auto_x", issue_count=count)
