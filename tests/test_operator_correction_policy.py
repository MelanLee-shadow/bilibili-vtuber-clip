import pytest

from src.autoslice.operator_correction_policy import plan_operator_correction


@pytest.mark.parametrize("count", [1, 2])
def test_one_or_two_unqualified_reports_require_whole_clip_rerun(count):
    plan = plan_operator_correction(candidate_id="auto_x", issue_count=count)
    assert plan["mode"] == "WHOLE_CLIP_RERUN_AND_REVIEW"
    assert plan["whole_clip_rerun_required"] is True


@pytest.mark.parametrize("count", [3, 4])
def test_three_or_more_reports_use_targeted_exhaustive_repairs(count):
    plan = plan_operator_correction(candidate_id="auto_x", issue_count=count)
    assert plan["mode"] == "TARGETED_REPAIR_PLUS_SYSTEMIC_FIX"
    assert plan["operator_scope"] == "EXHAUSTIVE_CANDIDATE"
    assert plan["targeted_locations_only"] is True
    assert plan["requires_change_coverage_proof"] is True
    assert plan["systemic_pipeline_fix_required"] is True


@pytest.mark.parametrize("count", [1, 2, 3])
@pytest.mark.parametrize("scope_flag", ["explicitly_exhaustive", "only_these_errors"])
def test_explicitly_exhaustive_short_report_is_targeted(count, scope_flag):
    plan = plan_operator_correction(
        candidate_id="auto_x",
        issue_count=count,
        **{scope_flag: True},
    )
    assert plan["mode"] == "TARGETED_REPAIR_PLUS_SYSTEMIC_FIX"
    assert plan["whole_clip_rerun_required"] is False
    assert plan["requires_change_coverage_proof"] is True


@pytest.mark.parametrize("count", [0, -1])
def test_invalid_issue_count_is_rejected(count):
    with pytest.raises(ValueError):
        plan_operator_correction(candidate_id="auto_x", issue_count=count)
