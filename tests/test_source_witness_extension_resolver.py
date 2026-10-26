"""The ordinary resolver must consume the same full source grid as its review."""
from types import SimpleNamespace
import pytest

from src.autoslice import producer_boundary_resolution as resolver
from src.autoslice import source_witness_extension as extension


def test_normal_resolver_receives_completed_witness_grid_without_delivery_projection(tmp_path, monkeypatch):
    prefix = [SimpleNamespace(index="1", start_ms=0, end_ms=1000, text="fixture prefix")]
    full = [*prefix, SimpleNamespace(index="2", start_ms=1000, end_ms=2000, text="fixture witness")]
    seen = {}

    def restore(spec, *, cues, chat_authority_audit):
        assert cues == prefix and chat_authority_audit == {"fixture": True}
        return full

    def selector(**kwargs):
        seen.update(kwargs)
        raise SystemExit("EXPLICIT_TEST_STOP_BEFORE_SEMANTIC_VERDICT")

    monkeypatch.setattr(extension, "resolver_source_cues", restore, raising=False)
    monkeypatch.setattr(resolver, "resolve_exact_boundary", lambda **kwargs: None)
    monkeypatch.setattr(resolver, "_select_initial_boundary", selector)
    with pytest.raises(SystemExit, match="EXPLICIT_TEST_STOP"):
        resolver.resolve_producer_boundary(
            spec={"source_witness_extension": {"fixture": True}},
            durations=[1000], padded=tmp_path / "prefix.mp4", padded_dur=1000,
            cid="fixture", out_root=tmp_path, transcriber=lambda **kwargs: None,
            cues=prefix, spans=[], boundary_repair_extend_cap_ms=30000,
            adapters=SimpleNamespace(), chat_authority_audit={"fixture": True},
        )
    assert seen["cues"] == full
    assert seen["padded_dur"] == 1000 and seen["durations"] == [1000]


def _semantic_fixture():
    import json
    from src.autoslice.boundary_semantic_review import build_boundary_search_scope
    cues = [SimpleNamespace(index=str(i), start_ms=(i - 1) * 1000,
                            end_ms=i * 1000, text="EXPLICIT ENGINEERING STUB")
            for i in range(1, 76)]
    scope = build_boundary_search_scope(
        semantic_target_ms=30000, published_recall_anchor_ms=30000,
        boundary_end_mode="published_recall_anchor", repair_cap_ms=30000,
        semantic_tail_trim_cap_ms=15000, last_piece_start_ms=100000)
    seen = {}

    def call(prompt):
        request = json.loads(prompt.split("绑定请求 JSON：\n", 1)[1].split("\n\n只输出 JSON：", 1)[0])
        seen.update(request)
        return json.dumps({
            "syntax_complete": False, "story_closed": False, "next_topic_separated": False,
            "content_anchor_covered": False, "recommended_end_cue_index": None,
            "evidence_cue_indexes": [75], "same_topic_continues_after_target": True,
            "needs_more_context": True, "reason_codes": ["EXPLICIT_ENGINEERING_STUB"],
            "summary": "EXPLICIT ENGINEERING STUB: no semantic acceptance",
        })
    return dict(cues=cues, target_ms=scope["review_target_ms"], candidate_id="fixture",
                selection_hook="", selection_scorecard={}, structured_context="",
                candidate_context="", llm_call=call, extract_json=json.loads,
                max_forward_ms=scope["recommendation_forward_ms"], boundary_search_scope=scope), seen


def test_normal_review_sees_complete_reserve_without_extending_endpoint_eligibility():
    from src.autoslice.boundary_semantic_review import review_talk_boundary_semantics, cue_grid_sha256
    kwargs, seen = _semantic_fixture()
    end = kwargs["boundary_search_scope"]["required_local_source_context_end_ms"]
    review = review_talk_boundary_semantics(**kwargs, source_full_window_end_ms=end)
    assert seen["cues"][-1]["end_ms"] == end == 75000
    assert seen["cues"][-1]["cue_index"] == 75
    assert 75 in seen["next_topic_witness_cue_indexes"]
    assert 75 not in seen["recommendation_cue_indexes"]
    assert review["status"] == "BLOCK" and review["review_scope"] == "source_full_window"
    assert review["cue_grid_sha256"] == cue_grid_sha256(kwargs["cues"])


@pytest.mark.parametrize("bad_end", [True, 74999, 75001])
def test_normal_review_rejects_unbound_reserve_end_before_provider(bad_end):
    from src.autoslice.boundary_semantic_review import review_talk_boundary_semantics, BoundarySemanticReviewError
    kwargs, seen = _semantic_fixture()
    with pytest.raises(BoundarySemanticReviewError, match="BOUNDARY_SOURCE_WITNESS_CONTEXT_INVALID"):
        review_talk_boundary_semantics(**kwargs, source_full_window_end_ms=bad_end)
    assert seen == {}
