"""Semantic-authority behavior: LLM-recalled windows are gated semantically
(CPA QA + viewer-context check), while keyword boundary/content heuristics are
demoted to advisory for those candidates."""

import json
from dataclasses import replace

from scripts.run_auto_review_shadow_pipeline import (
    _apply_semantic_authority_evidence,
    _resolve_live_source_boundary,
)
from scripts.run_full_session_selector_cpa_shadow import _viewer_context_expanded_candidate
from src.autoslice.auto_review import DecisionAction
from src.autoslice.boundary_resolver import AnchorCandidate, BoundaryResolution
from src.autoslice.content_evidence import analyze_content_evidence
from src.autoslice.full_session_candidate_selector import FullSessionCandidate
from src.autoslice.review_evidence import SourceCue


def _cue(cue_id: str, start_ms: int, end_ms: int, text: str) -> SourceCue:
    return SourceCue(
        cue_id=cue_id,
        source_start_ms=start_ms,
        source_end_ms=end_ms,
        text=text,
        language="zh",
        kind="speech",
        confidence=1.0,
    )


# Reactive banter with an open loop and no closure keywords: the keyword talk
# resolver DROPs this window (NO_NATURAL_CLOSURE).
_DANMAKU_BANTER_CUES = [
    _cue("u_1", 1_000, 4_000, "为什么你们都在刷这个"),
    _cue("u_2", 5_000, 9_000, "这真的不是融了阿朵吗"),
    _cue("u_3", 10_000, 14_000, "一眼AI 好吧"),
    _cue("u_4", 15_000, 19_000, "它胸口那个毛好可爱"),
]


def _job(**overrides) -> dict:
    job = {
        "candidate_id": "semantictalk_1000_19000",
        "timeline": {"anchor_start_ms": 1_000, "anchor_end_ms": 19_000},
    }
    job.update(overrides)
    return job


def test_keyword_boundary_still_drops_without_semantic_authority(tmp_path):
    resolution = _resolve_live_source_boundary(_job(), _DANMAKU_BANTER_CUES, output_dir=tmp_path)
    assert resolution.action == DecisionAction.DROP
    assert "NO_NATURAL_CLOSURE" in resolution.reason_codes


def test_semantic_authority_demotes_keyword_drop_to_advisory(tmp_path):
    resolution = _resolve_live_source_boundary(
        _job(boundary_authority="semantic"), _DANMAKU_BANTER_CUES, output_dir=tmp_path
    )
    assert resolution.action == DecisionAction.AUTO_RECUT
    assert resolution.resolved_start_ms == 1_000
    assert resolution.resolved_end_ms == 19_000
    assert resolution.next_start_ms == 1_000
    assert resolution.next_end_ms == 19_000
    assert "BOUNDARY_SEMANTIC_AUTHORITY" in resolution.reason_codes
    assert "ADVISORY_NO_NATURAL_CLOSURE" in resolution.reason_codes
    # Bare keyword codes must not leak: they would read as gating verdicts.
    assert "NO_NATURAL_CLOSURE" not in resolution.reason_codes


def _evidence_with_cpa_check(cpa_evidence: dict):
    evidence = analyze_content_evidence(
        candidate_id="semantictalk_1000_19000",
        cues=_DANMAKU_BANTER_CUES,
        title="一眼AI",
    )
    # Keyword scoring rates this reactive-banter window as no-payoff.
    assert evidence.payoff_score < 0.90
    check = {"code": "CPA_SEMANTIC_QA", "pass": True, "severity": "PASS", "evidence": cpa_evidence}
    return replace(evidence, checks=tuple(evidence.checks) + (check,))


def test_semantic_authority_lets_cpa_verdict_supersede_keyword_scores():
    evidence = _evidence_with_cpa_check(
        {
            "semantic_complete": True,
            "title_hook_score": 0.9,
            "context_dependency_score": 0.1,
            "viewer_context": {"viewer_context_ok": True},
        }
    )
    result = _apply_semantic_authority_evidence(evidence, {"boundary_authority": "semantic"})
    assert result.payoff_score >= 0.96
    assert result.standalone_score >= 0.94
    assert result.start_boundary_score >= 0.97
    assert result.end_boundary_score >= 0.98
    assert result.open_loop_count == 0
    assert result.editorial_score >= 82.0
    assert result.metadata["semantic_authority"]["applied"] is True


def test_semantic_authority_does_not_apply_to_keyword_lanes():
    evidence = _evidence_with_cpa_check(
        {
            "semantic_complete": True,
            "title_hook_score": 0.9,
            "context_dependency_score": 0.1,
            "viewer_context": {"viewer_context_ok": True},
        }
    )
    result = _apply_semantic_authority_evidence(evidence, {})
    assert result is evidence


def test_semantic_authority_keeps_failing_cpa_verdict_binding():
    evidence = _evidence_with_cpa_check(
        {
            "semantic_complete": False,
            "title_hook_score": 0.2,
            "context_dependency_score": 0.8,
            "viewer_context": {"viewer_context_ok": False},
        }
    )
    result = _apply_semantic_authority_evidence(evidence, {"boundary_authority": "semantic"})
    # A failing semantic verdict must not raise any score.
    assert result.payoff_score == evidence.payoff_score
    assert result.standalone_score == evidence.standalone_score
    assert result.start_boundary_score == evidence.start_boundary_score


def _talk_candidate(cues) -> FullSessionCandidate:
    anchor = AnchorCandidate(
        candidate_id="fallbacktalk_10000_19000",
        anchor_start_ms=10_000,
        anchor_end_ms=19_000,
    )
    boundary = BoundaryResolution(
        candidate_id=anchor.candidate_id,
        action=DecisionAction.AUTO_RECUT,
        resolved_start_ms=10_000,
        resolved_end_ms=19_000,
        start_boundary_score=0.9,
        end_boundary_score=0.9,
        reason_codes=(),
        next_start_ms=10_000,
        next_end_ms=19_000,
    )
    window = tuple(cue for cue in cues if cue.source_start_ms >= 10_000)
    return FullSessionCandidate(
        anchor=anchor,
        boundary=boundary,
        cues=window,
        text_preview=" ".join(cue.text for cue in window),
        content_type_hint="talk",
    )


def _write_response(tmp_path, viewer_context: dict):
    response_json = tmp_path / "resp.json"
    response_json.write_text(
        json.dumps({"metadata": {"viewer_context": viewer_context}}, ensure_ascii=False),
        encoding="utf-8",
    )
    return response_json


def test_viewer_context_expansion_builds_expanded_candidate(tmp_path):
    candidate = _talk_candidate(_DANMAKU_BANTER_CUES)
    response_json = _write_response(
        tmp_path, {"viewer_context_ok": False, "expand_before_ms": 9_500, "expand_after_ms": 0}
    )
    expanded = _viewer_context_expanded_candidate(
        candidate, _DANMAKU_BANTER_CUES, response_json, source_duration_ms=19_000
    )
    assert expanded is not None
    assert expanded.boundary.resolved_start_ms == 1_000
    assert expanded.boundary.resolved_end_ms == 19_000
    assert expanded.anchor.candidate_id == "fallbacktalk_1000_19000_ctxexp"
    assert "VIEWER_CONTEXT_EXPANDED" in expanded.boundary.reason_codes
    assert expanded.content_type_hint == "talk"


def test_viewer_context_ok_or_song_or_zero_expansion_returns_none(tmp_path):
    candidate = _talk_candidate(_DANMAKU_BANTER_CUES)
    ok_response = _write_response(tmp_path, {"viewer_context_ok": True, "expand_before_ms": 0, "expand_after_ms": 0})
    assert _viewer_context_expanded_candidate(candidate, _DANMAKU_BANTER_CUES, ok_response, source_duration_ms=19_000) is None

    zero_response = _write_response(tmp_path, {"viewer_context_ok": False, "expand_before_ms": 0, "expand_after_ms": 0})
    assert _viewer_context_expanded_candidate(candidate, _DANMAKU_BANTER_CUES, zero_response, source_duration_ms=19_000) is None

    song_candidate = replace(candidate, content_type_hint="song")
    bad_response = _write_response(tmp_path, {"viewer_context_ok": False, "expand_before_ms": 9_500, "expand_after_ms": 0})
    assert _viewer_context_expanded_candidate(song_candidate, _DANMAKU_BANTER_CUES, bad_response, source_duration_ms=19_000) is None


def test_viewer_context_expansion_noop_when_bounds_unchanged(tmp_path):
    candidate = _talk_candidate(_DANMAKU_BANTER_CUES)
    # 500ms of expansion does not reach the previous cue, so the cue window is
    # identical — the retry would re-review the same text.
    response_json = _write_response(
        tmp_path, {"viewer_context_ok": False, "expand_before_ms": 500, "expand_after_ms": 0}
    )
    assert _viewer_context_expanded_candidate(candidate, _DANMAKU_BANTER_CUES, response_json, source_duration_ms=19_000) is None
