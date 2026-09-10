"""A non-exhaustive repair request is not authority to freeze other words."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from src.autoslice.operator_correction_policy import plan_operator_correction
from scripts.materialize_operator_reviewed_subtitle_baseline import (
    OperatorBaselineCompileError,
    compile_operator_baseline,
)

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("count", [1, 2, 3, 4, 59])
def test_explicit_non_exhaustive_overrides_count(count):
    plan = plan_operator_correction(
        candidate_id="scope_example", issue_count=count, only_these_errors=False,
    )
    assert plan["whole_clip_rerun_required"] is True
    assert plan["targeted_locations_only"] is False
    assert plan["operator_scope"] == "EXPLICITLY_NON_EXHAUSTIVE"


def test_contradictory_explicit_scope_is_rejected():
    with pytest.raises(ValueError, match="conflicting"):
        plan_operator_correction(
            candidate_id="scope_example", issue_count=3,
            only_these_errors=False, explicitly_exhaustive=True,
        )


def test_cli_distinguishes_absent_flag_from_explicit_non_exhaustive():
    command = [sys.executable, str(ROOT / "scripts/plan_operator_subtitle_correction.py"),
               "--candidate", "scope_example", "--issue-count", "3"]
    default = subprocess.run(command, capture_output=True, text=True, check=True)
    explicit = subprocess.run(command + ["--no-only-these-errors"],
                              capture_output=True, text=True, check=True)
    assert json.loads(default.stdout)["only_these_errors"] is None
    assert json.loads(default.stdout)["targeted_locations_only"] is True
    assert json.loads(explicit.stdout)["whole_clip_rerun_required"] is True


@pytest.mark.parametrize("plan", [None, {},
    plan_operator_correction(candidate_id="scope_example", issue_count=3),
    plan_operator_correction(candidate_id="scope_example", issue_count=1,
                             only_these_errors=False),
])
def test_compiler_cannot_mint_full_ownership_from_release_permission(tmp_path, plan):
    # Reject before reading or materializing any media, even with an EXHAUSTIVE
    # label fabricated on a decision ledger. Numeric inference is not review.
    with pytest.raises(OperatorBaselineCompileError, match="explicit exhaustive"):
        compile_operator_baseline(
            source_srt=tmp_path / "not-read.srt",
            reviewed_srt=tmp_path / "not-read-either.srt",
            candidate_id="scope_example", authority="repair and upload; find other errors",
            source_recording_basename="recording.mp4", source_recording_sha256="ab" * 32,
            absolute_source_start_ms=0, absolute_source_end_ms=1000,
            decision_ledger={"report_scope": "EXHAUSTIVE"}, correction_plan=plan,
        )
    assert list(tmp_path.iterdir()) == []


def test_missing_scope_argument_is_not_a_legacy_compiler_bypass(tmp_path):
    with pytest.raises(OperatorBaselineCompileError, match="explicit exhaustive"):
        compile_operator_baseline(
            source_srt=tmp_path / "source.srt", reviewed_srt=tmp_path / "reviewed.srt",
            candidate_id="scope_example", authority="publish after repairing",
            source_recording_basename="source.mp4", source_recording_sha256="ab" * 32,
            absolute_source_start_ms=0, absolute_source_end_ms=1000,
            decision_ledger={"report_scope": "EXHAUSTIVE"},
        )


def test_cross_candidate_or_tampered_explicit_plan_cannot_authorize_freezing():
    from src.autoslice.operator_correction_policy import require_explicit_exhaustive_review_plan

    plan = plan_operator_correction(candidate_id="a", issue_count=1,
                                    explicitly_exhaustive=True)
    with pytest.raises(ValueError, match="explicit exhaustive"):
        require_explicit_exhaustive_review_plan(plan, candidate_id="b")
    plan["whole_clip_rerun_required"] = True
    with pytest.raises(ValueError, match="explicit exhaustive"):
        require_explicit_exhaustive_review_plan(plan, candidate_id="a")
