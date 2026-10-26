from types import SimpleNamespace
from copy import deepcopy

import pytest

from src.autoslice.boundary_semantic_review import cue_grid_sha256
from src.autoslice import producer_source_boundary_review as router


def cue(index, start, end, text):
    return SimpleNamespace(index=str(index), start_ms=start, end_ms=end, text=text)


def projection(cues):
    return {
        "schema_version": "delivery-to-source-timeline-projection.v1",
        "status": "PASS",
        "cue_count": len(cues),
        "projected_source_cue_grid_sha256": cue_grid_sha256(cues),
        "delivery_srt_cue_grid_sha256": "sha256:" + "a" * 64,
    }


def test_source_reviewer_receives_current_correction_without_changing_raw_grid(monkeypatch):
    source = [cue(45, 101100, 103360, "不描绘到是什么感觉了"),
              cue(46, 103500, 105000, "最近声音有点哑")]
    current = [cue(30, 101100, 103360, "不必要的会是什么感觉了")]
    raw_grid = cue_grid_sha256(source)
    seen = {}
    monkeypatch.setattr(router, "authority_from_spec", lambda _: None)
    monkeypatch.setattr(router, "load_boundary_review_authorities", lambda *a, **k: (None, None))
    monkeypatch.setattr(router, "clip_context_prompt_text", lambda _: "历史完整上下文")
    monkeypatch.setattr(router, "_final_review_structured_context", lambda **k: "")
    def unused_transport(_prompt):
        raise AssertionError("The reviewer fixture must not dispatch a provider call")

    monkeypatch.setattr(router, "build_final_review_llm_call", lambda: unused_transport)

    def reviewer(**kwargs):
        seen.update(kwargs)
        return {"status": "BLOCK", "syntax_complete": False}

    monkeypatch.setattr(router, "review_final_boundary_semantics", reviewer)
    _, result, _, _ = router.review_source_boundary(
        spec={}, candidate_id="story", cues=source, boundary_target_ms=103360,
        boundary_search_scope={"recommendation_forward_ms": 30000},
        current_owner_contract=None, authoritative_chat=(), clip_context={},
        available_local_source_context_end_ms=179434,
        current_source_cues=current, current_delivery_projection=projection(current),
    )
    assert seen["cues"] is source
    assert cue_grid_sha256(source) == raw_grid
    assert '"source_cue_index": 45' in seen["candidate_context"]
    assert "不描绘到" in seen["candidate_context"]
    assert "不必要的会" in seen["candidate_context"]
    assert "CURRENT_TEXT_PROPOSAL_NOT_FINAL_CLEAN" in seen["candidate_context"]
    assert result["status"] == "BLOCK"
    assert result["syntax_complete"] is False


def test_changed_current_text_changes_semantic_request_context():
    source = [cue(2, 1000, 2000, "普通名词")]
    a = [cue(1, 1000, 2000, "专名甲")]
    b = [cue(1, 1000, 2000, "专名乙")]
    assert router._current_text_context(source, a, projection(a))[0] != \
        router._current_text_context(source, b, projection(b))[0]


def test_crossing_and_ambiguous_intervals_cannot_supply_current_text():
    source = [cue(1, 1000, 2000, "原文"), cue(2, 1000, 2000, "重叠原文")]
    for current in ([cue(1, 1000, 2000, "不能认领")],
                    [cue(1, 1500, 2500, "跨界不能认领")]):
        context, evidence = router._current_text_context(source, current, projection(current))
        assert context == ""
        assert evidence["exact_interval_changes"] == []
        assert evidence["unmatched_current_cue_indexes"] == ["1"]


def test_stale_projection_fails_before_reviewer():
    current = [cue(1, 1000, 2000, "当前文本")]
    stale = projection(current)
    stale["projected_source_cue_grid_sha256"] = "sha256:" + "b" * 64
    with pytest.raises(ValueError, match="PROJECTION_DRIFT"):
        router._current_text_context(current, current, stale)


def test_unchanged_current_text_keeps_existing_request_context():
    source = [cue(2, 1000, 2000, "相同原文")]
    current = [cue(1, 1000, 2000, "相同原文")]
    context, evidence = router._current_text_context(source, current, projection(current))
    assert context == ""
    assert evidence["exact_interval_changes"] == []


def timing_fixture():
    source = [cue(681, 1810853, 1811813, "什么呀")]
    current = [cue(95, 1810853, 1811853, "怎么样")]
    projected = projection(current)
    projected.update(source_final_start_ms=1531330, source_final_end_ms=1812213)
    resolution = {
        "final_start": 1531330, "final_end": 1812213,
        "timing_qa": {
            "schema_version": "subtitle-timing-qa.v3",
            "actions": [{
                "cue_id": "fresh_0131", "action": "retime", "text": "什么呀",
                "before": {"start_ms": 1810853, "end_ms": 1811813},
                "after": {"start_ms": 1810853, "end_ms": 1811853},
            }],
        },
        "sanitized_cues": [{"cue_id": "fresh_0131", "text": "什么呀",
                            "source_start_ms": 1810853, "source_end_ms": 1811853}],
    }
    return source, current, projected, resolution


def test_retained_timing_maps_complete_current_text_as_proposal_without_replacing_source():
    source, current, projected, resolution = timing_fixture()
    original_grid = cue_grid_sha256(source)
    router.attach_current_timing_qa_projection(
        source, current, projected, resolution=resolution,
        resolution_sha256="sha256:" + "a" * 64,
    )
    context, evidence = router._current_text_context(source, current, projected)
    row = evidence["timing_qa_interval_changes"][0]
    assert row["source_cue_index"] == "681"
    assert row["current_cue_index"] == "95"
    assert row["timing_cue_id"] == "fresh_0131"
    assert row["source_end_ms"] == 1811813
    assert row["current_source_end_ms"] == 1811853
    assert row["source_text"] == "什么呀"
    assert row["current_text"] == "怎么样"
    assert evidence["unmatched_current_cue_indexes"] == []
    assert "CURRENT_TEXT_PROPOSAL_NOT_FINAL_CLEAN" in context
    assert cue_grid_sha256(source) == original_grid
    assert "release_gate" not in evidence
    # Timing linkage alone must not waive the ordinary changed-tail guard.
    from src.autoslice.producer_boundary_resolution import _current_delivery_closure_binding
    with pytest.raises(SystemExit, match="CLOSURE_CROSSING"):
        _current_delivery_closure_binding(
            closure_cue=source[0], snapped_end_ms=source[0].end_ms,
            current_delivery_cues=current, current_delivery_projection=projected,
            media_end_ms=1812213,
        )


@pytest.mark.parametrize("drift", ["schema", "window", "before", "final_text", "truncated_text", "id", "current_ambiguous"])
def test_retained_timing_join_rejects_unbound_or_changed_evidence(drift):
    source, current, projected, resolution = timing_fixture()
    if drift == "schema":
        resolution["timing_qa"]["schema_version"] = "unknown"
    elif drift == "window":
        resolution["final_end"] += 1
    elif drift == "before":
        resolution["timing_qa"]["actions"][0]["before"]["end_ms"] += 1
    elif drift == "final_text":
        resolution["sanitized_cues"][0]["text"] = "另一句话"
    elif drift == "truncated_text":
        resolution["timing_qa"]["actions"][0]["text"] = "另一句话"
    elif drift == "id":
        resolution["timing_qa"]["actions"].append(deepcopy(resolution["timing_qa"]["actions"][0]))
    else:
        current.append(cue(96, 1810853, 1811853, "重复认领"))
    with pytest.raises(ValueError, match="SOURCE_REVIEW_CURRENT_TIMING"):
        router.attach_current_timing_qa_projection(
            source, current, projected, resolution=resolution,
            resolution_sha256="sha256:" + "a" * 64,
        )


def test_timing_overlap_or_old_current_grid_does_not_supply_a_proposal():
    source, current, projected, resolution = timing_fixture()
    current[0].end_ms += 1  # Similar geometry is insufficient.
    projected["projected_source_cue_grid_sha256"] = cue_grid_sha256(current)
    router.attach_current_timing_qa_projection(
        source, current, projected, resolution=resolution,
        resolution_sha256="sha256:" + "a" * 64,
    )
    context, evidence = router._current_text_context(source, current, projected)
    assert context == ""
    assert evidence["timing_qa_interval_changes"] == []
    assert evidence["unmatched_current_cue_indexes"] == ["95"]
    current[0].text = "后来改的新文字"
    projected["projected_source_cue_grid_sha256"] = cue_grid_sha256(current)
    with pytest.raises(ValueError, match="TIMING_PROJECTION_DRIFT"):
        router._current_text_context(source, current, projected)
