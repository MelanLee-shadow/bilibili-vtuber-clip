"""维护者: no unresolved subtitle doubt may ship."""

import copy
import json
from pathlib import Path

from src.autoslice.unresolved_findings_gate import (
    UNRESOLVED_FINDINGS_CODE,
    unresolved_disclosed_findings,
    unresolved_findings_detail,
    unresolved_findings_problem,
)
from tests.test_final_review_contract import _with_current_equivalence

FIXTURE = Path(__file__).parent / "fixtures" / "unresolved_findings" / "guimi_postburn_record_excerpt.json"


def _guimi():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _keep_current_row(cue_index=5):
    return _with_current_equivalence({
        "cue_index": cue_index,
        "suspect": "原文",
        "exact_release_adjudication": {
            "status": "OBSERVED",
            "policy_branch": "JUDGE_KEEPS_CURRENT",
            "repaired": False,
            "timing_immutable": True,
            "mutation_authority": {"status": "NOT_APPLIED"},
        },
    })


def test_real_guimi_record_excerpt_is_blocked_with_cue_and_suspect():
    record = _guimi()
    rows = unresolved_disclosed_findings(record)
    assert len(rows) == 11
    assert {row["content_risk"] for row in rows} == {"noncritical_uncertainty"}
    detail = unresolved_findings_detail(rows)
    assert "cue 14: '能发挥'" in detail
    # A proposal that was never applied is called out, not treated as resolved.
    assert "你，你不觉得我跟别，别人说你坏话很不道德吗" in detail
    problem = unresolved_findings_problem(record)
    assert problem and problem.startswith(UNRESOLVED_FINDINGS_CODE)


def test_only_listening_confirmed_keep_current_passes():
    record = {"postburn_subtitle_audio_review": {"unresolved_findings_disclosed": [_keep_current_row()]}}
    assert unresolved_disclosed_findings(record) == []
    assert unresolved_findings_problem(record) is None


def test_keep_current_does_not_launder_noncritical_neighbours():
    record = copy.deepcopy(_guimi())
    record["postburn_subtitle_audio_review"]["unresolved_findings_disclosed"].append(_keep_current_row(99))
    rows = unresolved_disclosed_findings(record)
    assert len(rows) == 11 and 99 not in {row["cue_index"] for row in rows}


def test_empty_or_absent_disclosure_passes():
    assert unresolved_disclosed_findings({}) == []
    assert unresolved_disclosed_findings({"postburn_subtitle_audio_review": {"unresolved_findings_disclosed": []}}) == []
    assert unresolved_disclosed_findings(None) == []


def test_other_final_review_structures_and_malformed_lists_are_covered():
    row = {"cue_index": 3, "suspect": "x", "content_risk": "noncritical_uncertainty"}
    for key in ("exact_final_review", "final_review_audit", "final_review"):
        assert len(unresolved_disclosed_findings({key: {"unresolved_findings_disclosed": [row]}})) == 1
    assert len(unresolved_disclosed_findings({"final_review_audit": {"unresolved_findings_disclosed": "oops"}})) == 1


def test_bare_noncritical_with_complete_assessment_still_blocks():
    row = _with_current_equivalence({"cue_index": 4, "suspect": "y"})
    record = {"postburn_subtitle_audio_review": {"unresolved_findings_disclosed": [row]}}
    assert len(unresolved_disclosed_findings(record)) == 1
