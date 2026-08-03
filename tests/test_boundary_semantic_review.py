import json

import pytest

from src.autoslice.boundary_semantic_review import (
    BoundarySemanticReviewError,
    boundary_search_scope_is_valid,
    build_boundary_search_scope,
    cue_grid_sha256,
    required_source_context_end_ms,
    review_talk_boundary_semantics,
)
from src.autoslice.jingting_chunker import SrtCue


def _cue(index: int, start_ms: int, end_ms: int, text: str) -> SrtCue:
    return SrtCue(index=str(index), start_ms=start_ms, end_ms=end_ms, text=text)


def _scorecard(*, self_contained: int = 4, comedic_payoff: int = 4) -> dict:
    return {
        "status": "VALID",
        "dimensions": {
            "self_contained": self_contained,
            "comedic_payoff": comedic_payoff,
        },
    }


def _extract(value: str) -> dict:
    return json.loads(value)


def _source_window_review(
    *,
    final_start_ms: int = 10_000,
    final_end_ms: int = 20_000,
) -> dict:
    return {
        "schema_version": "talk-boundary-semantic-review.v1",
        "status": "PASS",
        "review_scope": "source_full_window",
        "request_sha256": "sha256:" + "a" * 64,
        "cue_grid_sha256": "sha256:" + "b" * 64,
        "recommended_end_ms": final_end_ms - 400,
        "next_topic_separated": True,
        "next_topic_witness_valid": True,
        "final_endpoint_binding": {
            "schema_version": "talk-boundary-final-endpoint-binding.v1",
            "status": "PASS",
            "final_start_ms": final_start_ms,
            "final_end_ms": final_end_ms,
        },
    }


def test_independent_boundary_review_binds_recommendation_to_cue_grid():
    cues = [
        _cue(1, 0, 9_000, "我们是不太熟的关系"),
        _cue(2, 9_100, 12_000, "就是刚认识暂时不太熟啊"),
        _cue(3, 12_100, 15_000, "谢谢下一条SC"),
    ]
    response = json.dumps(
        {
            "syntax_complete": True,
            "story_closed": True,
            "next_topic_separated": True,
            "recommended_end_cue_index": 2,
            "evidence_cue_indexes": [1, 2, 3],
            "reason_codes": [],
            "summary": "第二条收束，第三条开始新SC。",
        },
        ensure_ascii=False,
    )

    review = review_talk_boundary_semantics(
        cues=cues,
        target_ms=9_000,
        candidate_id="candidate",
        selection_hook="不熟关系",
        selection_scorecard=_scorecard(),
        structured_context="superchat @12100ms: 下一条SC",
        candidate_context="hash-bound context",
        llm_call=lambda _prompt: response,
        extract_json=_extract,
    )

    assert review["status"] == "PASS"
    assert review["recommended_end_ms"] == 12_000
    assert review["reviewer_independence_group"] == (
        "cpa-gpt-5.6-semantic-family"
    )
    assert review["selector_story_witness"]["independence_group"] == (
        "cpa-gpt-5.6-semantic-family"
    )
    assert review["content_anchor_covered"] is True
    assert review["independent_semantic_vote_count"] == 1
    assert review["correlated_reviewer_disclosure"] is True
    assert review["cue_grid_sha256"] == cue_grid_sha256(cues)


def test_boundary_review_cannot_self_certify_a_weak_selector_story_witness():
    response = json.dumps(
        {
            "syntax_complete": True,
            "story_closed": True,
            "next_topic_separated": True,
            "recommended_end_cue_index": 1,
            "evidence_cue_indexes": [1, 2],
            "reason_codes": [],
            "summary": "模型声称完整。",
        },
        ensure_ascii=False,
    )
    review = review_talk_boundary_semantics(
        cues=[
            _cue(1, 0, 9_000, "可能收束"),
            _cue(2, 9_100, 12_000, "下一句"),
        ],
        target_ms=9_000,
        candidate_id="candidate",
        selection_hook="",
        selection_scorecard=_scorecard(self_contained=2),
        structured_context="",
        candidate_context="",
        llm_call=lambda _prompt: response,
        extract_json=_extract,
    )

    assert review["status"] == "BLOCK"
    assert "SELECTOR_STORY_WITNESS_INSUFFICIENT" in review["reason_codes"]


def test_boundary_review_rejects_recommendation_beyond_forward_cap():
    response = json.dumps(
        {
            "syntax_complete": True,
            "story_closed": True,
            "next_topic_separated": True,
            "recommended_end_cue_index": 2,
            "evidence_cue_indexes": [1, 2],
            "reason_codes": [],
            "summary": "过远。",
        },
        ensure_ascii=False,
    )
    review = review_talk_boundary_semantics(
        cues=[
            _cue(1, 0, 9_000, "目标"),
            _cue(2, 39_100, 40_000, "过远结尾"),
        ],
        target_ms=9_000,
        candidate_id="candidate",
        selection_hook="",
        selection_scorecard=_scorecard(),
        structured_context="",
        candidate_context="",
        llm_call=lambda _prompt: response,
        extract_json=_extract,
    )

    assert review["status"] == "BLOCK"
    assert "BOUNDARY_RECOMMENDATION_OUT_OF_SCOPE" in review["reason_codes"]


def test_boundary_review_sees_long_forward_window_and_next_topic_witness():
    cues = [_cue(1, 0, 9_000, "目标仍未回答完")]
    for index in range(2, 18):
        start = 9_000 + (index - 2) * 3_000
        cues.append(_cue(index, start, start + 2_800, f"同一回答第{index}段"))
    cues.extend(
        [
            _cue(18, 57_000, 58_300, "回答终于完整落地"),
            _cue(19, 58_400, 59_400, "谢谢下一条SC"),
        ]
    )
    seen_prompt = ""

    def review(prompt: str) -> str:
        nonlocal seen_prompt
        seen_prompt = prompt
        return json.dumps(
            {
                "syntax_complete": True,
                "story_closed": True,
                "next_topic_separated": True,
                "recommended_end_cue_index": 18,
                "evidence_cue_indexes": [1, 18, 19],
                "same_topic_continues_after_target": False,
                "needs_more_context": False,
                "reason_codes": ["CODA_COMPLETE"],
                "summary": "第18句闭环，第19句换到下一条SC。",
            },
            ensure_ascii=False,
        )

    result = review_talk_boundary_semantics(
        cues=cues,
        target_ms=9_000,
        candidate_id="long-coda",
        selection_hook="回答SC",
        selection_scorecard=_scorecard(),
        structured_context="",
        candidate_context="",
        llm_call=review,
        extract_json=_extract,
        max_forward_ms=60_000,
    )

    assert result["status"] == "PASS"
    assert result["recommended_end_ms"] == 58_300
    assert result["max_forward_ms"] == 60_000
    assert '"cue_index": 18' in seen_prompt
    assert '"cue_index": 19' in seen_prompt


def test_manual_lower_bound_moves_shared_scope_past_old_origin():
    """The shared cap is measured from an authorized lower bound, not from a
    stale selector endpoint."""

    scope = build_boundary_search_scope(
        semantic_target_ms=202_720,
        manual_lower_bound_ms=230_760,
        required_owner_end_ms=230_760,
        repair_cap_ms=60_000,
        last_piece_start_ms=1_856_000,
    )
    cues = [
        _cue(1, 227_640, 230_760, "人工下界处的句子"),
        _cue(2, 261_850, 263_330, "同一故事终于完整收束"),
        _cue(3, 263_330, 268_480, "明确开始下一话题"),
    ]
    response = json.dumps(
        {
            "syntax_complete": True,
            "story_closed": True,
            "next_topic_separated": True,
            "recommended_end_cue_index": 2,
            "evidence_cue_indexes": [1, 2, 3],
            "same_topic_continues_after_target": False,
            "needs_more_context": False,
            "reason_codes": [],
            "summary": "第二句结束原话题，第三句开始童年衣服话题。",
        },
        ensure_ascii=False,
    )

    review = review_talk_boundary_semantics(
        cues=cues,
        target_ms=230_760,
        candidate_id="generic-late-closure",
        selection_hook="晚到的故事闭环",
        selection_scorecard=_scorecard(),
        structured_context="",
        candidate_context="hash-bound context",
        llm_call=lambda _prompt: response,
        extract_json=_extract,
        max_forward_ms=60_000,
        boundary_search_scope=scope,
    )

    assert boundary_search_scope_is_valid(scope)
    assert scope["semantic_search_origin_ms"] == 230_760
    assert scope["max_recommended_end_ms"] == 290_760
    assert review["status"] == "PASS"
    assert review["recommended_end_ms"] == 263_330
    assert review["boundary_search_scope"] == scope


def test_automatic_semantic_tail_allows_bounded_backward_review():
    scope = build_boundary_search_scope(
        semantic_target_ms=107_000,
        repair_cap_ms=30_000,
        semantic_tail_trim_cap_ms=15_000,
    )

    assert boundary_search_scope_is_valid(scope)
    assert scope["semantic_search_origin_ms"] == 107_000
    assert scope["delivery_lower_bound_ms"] == 92_000
    assert scope["review_target_ms"] == 107_000
    assert scope["recommendation_backward_ms"] == 15_000
    assert scope["recommendation_forward_ms"] == 30_000
    assert scope["minimum_recommended_end_ms"] == 92_000
    assert scope["max_recommended_end_ms"] == 137_000


def test_manual_lower_bound_disables_automatic_tail_trim():
    scope = build_boundary_search_scope(
        semantic_target_ms=107_000,
        manual_lower_bound_ms=107_000,
        repair_cap_ms=30_000,
        semantic_tail_trim_cap_ms=15_000,
    )

    assert boundary_search_scope_is_valid(scope)
    assert scope["delivery_lower_bound_ms"] == 107_000
    assert scope["review_target_ms"] == 107_000
    assert scope["recommendation_backward_ms"] == 0
    assert scope["minimum_recommended_end_ms"] == 107_000


def test_published_recall_anchor_allows_bounded_tail_trim():
    scope = build_boundary_search_scope(
        semantic_target_ms=107_000,
        published_recall_anchor_ms=107_000,
        boundary_end_mode="published_recall_anchor",
        repair_cap_ms=30_000,
        semantic_tail_trim_cap_ms=15_000,
    )

    assert boundary_search_scope_is_valid(scope)
    assert scope["published_recall_anchor_ms"] == 107_000
    assert scope["manual_lower_bound_ms"] is None
    assert scope["semantic_search_origin_ms"] == 107_000
    assert scope["delivery_lower_bound_ms"] == 92_000
    assert scope["recommendation_backward_ms"] == 15_000
    assert scope["minimum_recommended_end_ms"] == 92_000
    assert scope["max_recommended_end_ms"] == 137_000


def test_boundary_review_can_trim_open_next_topic_after_payoff():
    scope = build_boundary_search_scope(
        semantic_target_ms=107_000,
        repair_cap_ms=30_000,
        semantic_tail_trim_cap_ms=15_000,
    )
    cues = [
        _cue(1, 90_000, 100_000, "原故事的包袱已经完整落地"),
        _cue(2, 100_100, 107_000, "为啥有点下头"),
        _cue(3, 107_100, 109_000, "下一话题的开场"),
    ]
    response = json.dumps(
        {
            "syntax_complete": True,
            "story_closed": True,
            "next_topic_separated": True,
            "content_anchor_covered": True,
            "recommended_end_cue_index": 1,
            "evidence_cue_indexes": [1, 2, 3],
            "same_topic_continues_after_target": False,
            "needs_more_context": False,
            "reason_codes": ["OPEN_NEXT_TOPIC_TAIL_TRIMMED"],
            "summary": "第一句已闭环，第二句是没有回答的新问题。",
        },
        ensure_ascii=False,
    )

    review = review_talk_boundary_semantics(
        cues=cues,
        target_ms=107_000,
        candidate_id="open-tail",
        selection_hook="原故事包袱",
        selection_scorecard=_scorecard(),
        structured_context="",
        candidate_context="hash-bound context",
        llm_call=lambda _prompt: response,
        extract_json=_extract,
        max_forward_ms=30_000,
        boundary_search_scope=scope,
    )

    assert review["status"] == "PASS"
    assert review["recommended_end_ms"] == 100_000
    assert review["content_anchor_covered"] is True


def test_backward_trim_requires_explicit_content_anchor_coverage():
    scope = build_boundary_search_scope(
        semantic_target_ms=107_000,
        repair_cap_ms=30_000,
        semantic_tail_trim_cap_ms=15_000,
    )
    response = json.dumps(
        {
            "syntax_complete": True,
            "story_closed": True,
            "next_topic_separated": True,
            "content_anchor_covered": False,
            "recommended_end_cue_index": 1,
            "evidence_cue_indexes": [1, 2],
            "same_topic_continues_after_target": False,
            "needs_more_context": False,
            "reason_codes": [],
            "summary": "没有证明内容锚点已全部落在切点前。",
        },
        ensure_ascii=False,
    )

    review = review_talk_boundary_semantics(
        cues=[
            _cue(1, 90_000, 100_000, "可能的包袱结尾"),
            _cue(2, 100_100, 107_000, "可能仍有关联的尾句"),
        ],
        target_ms=107_000,
        candidate_id="unproven-anchor",
        selection_hook="完整故事",
        selection_scorecard=_scorecard(),
        structured_context="",
        candidate_context="hash-bound context",
        llm_call=lambda _prompt: response,
        extract_json=_extract,
        max_forward_ms=30_000,
        boundary_search_scope=scope,
    )

    assert review["status"] == "BLOCK"
    assert review["content_anchor_covered"] is False
    assert (
        "CONTENT_ANCHOR_COVERED_NOT_PROVEN"
        in review["reason_codes"]
    )


def test_exact_source_pin_reviews_fresh_closure_before_media_cut():
    pin_ms = 202_720
    scope = build_boundary_search_scope(
        semantic_target_ms=pin_ms,
        manual_lower_bound_ms=pin_ms,
        repair_cap_ms=30_000,
        boundary_end_mode="exact_source_pin",
    )
    cues = [
        _cue(1, 199_400, 202_320, "就是刚认识暂时不太熟啊"),
        _cue(2, 202_320, 204_120, "我行啊"),
        _cue(3, 204_180, 206_420, "谢谢下一条SC"),
    ]
    response = json.dumps(
        {
            "syntax_complete": True,
            "story_closed": True,
            "next_topic_separated": True,
            "recommended_end_cue_index": 1,
            "evidence_cue_indexes": [1, 2, 3],
            "same_topic_continues_after_target": False,
            "needs_more_context": False,
            "reason_codes": [],
            "summary": "第一句闭环，source pin 后进入下一条SC。",
        },
        ensure_ascii=False,
    )

    review = review_talk_boundary_semantics(
        cues=cues,
        target_ms=pin_ms,
        candidate_id="auto_193450_1863_2056",
        selection_hook="火锅关系闭环",
        selection_scorecard=_scorecard(),
        structured_context="官方 cue 912 为下一条SC",
        candidate_context="hash-bound context",
        llm_call=lambda _prompt: response,
        extract_json=_extract,
        max_forward_ms=0,
        boundary_search_scope=scope,
    )

    assert boundary_search_scope_is_valid(scope)
    assert scope["minimum_recommended_end_ms"] == pin_ms - 400
    assert scope["max_recommended_end_ms"] == pin_ms
    assert scope["recommendation_forward_ms"] == 0
    assert (
        required_source_context_end_ms(scope, repair_cap_ms=60_000)
        == pin_ms + 15_000
    )
    assert review["status"] == "PASS"
    assert review["recommended_end_ms"] == pin_ms - 400


def test_boundary_review_rejects_tampered_search_scope_binding():
    scope = build_boundary_search_scope(
        semantic_target_ms=9_000,
        repair_cap_ms=30_000,
    )
    scope["max_recommended_end_ms"] = 90_000

    with pytest.raises(
        BoundarySemanticReviewError,
        match="BOUNDARY_SEMANTIC_SEARCH_SCOPE_INVALID",
    ):
        review_talk_boundary_semantics(
            cues=[_cue(1, 0, 9_000, "目标")],
            target_ms=9_000,
            candidate_id="tampered",
            selection_hook="",
            selection_scorecard=_scorecard(),
            structured_context="",
            candidate_context="",
            llm_call=lambda _prompt: "{}",
            extract_json=_extract,
            max_forward_ms=30_000,
            boundary_search_scope=scope,
        )


def test_boundary_review_requests_one_long_context_retry_fail_closed():
    cues = [
        _cue(1, 0, 9_000, "目标仍未回答完"),
        _cue(2, 9_100, 20_000, "同一回答继续"),
        _cue(3, 20_100, 31_000, "仍然没有闭环"),
    ]
    response = json.dumps(
        {
            "syntax_complete": True,
            "story_closed": False,
            "next_topic_separated": False,
            "recommended_end_cue_index": None,
            "evidence_cue_indexes": [1, 2, 3],
            "same_topic_continues_after_target": True,
            "needs_more_context": True,
            "reason_codes": ["SAME_TOPIC_FOLLOWUP"],
            "summary": "现有上下文内同一回答仍在继续。",
        },
        ensure_ascii=False,
    )

    result = review_talk_boundary_semantics(
        cues=cues,
        target_ms=9_000,
        candidate_id="needs-context",
        selection_hook="回答SC",
        selection_scorecard=_scorecard(),
        structured_context="",
        candidate_context="",
        llm_call=lambda _prompt: response,
        extract_json=_extract,
        max_forward_ms=30_000,
    )

    assert result["status"] == "BLOCK"
    assert result["needs_more_context"] is True
    assert result["same_topic_continues_after_target"] is True
    assert result["retry_scope"] == "same_topic_continues"
    assert "BOUNDARY_CONTEXT_EXHAUSTED" in result["reason_codes"]
    assert "BOUNDARY_RECOMMENDATION_MISSING" in result["reason_codes"]
    assert "BOUNDARY_RECOMMENDATION_OUT_OF_SCOPE" not in result["reason_codes"]


def test_boundary_review_cannot_request_context_without_post_target_evidence():
    response = json.dumps(
        {
            "syntax_complete": False,
            "story_closed": False,
            "next_topic_separated": False,
            "recommended_end_cue_index": None,
            "evidence_cue_indexes": [1],
            "same_topic_continues_after_target": True,
            "needs_more_context": True,
            "reason_codes": ["SAME_TOPIC_FOLLOWUP"],
            "summary": "没有后续证据。",
        },
        ensure_ascii=False,
    )
    result = review_talk_boundary_semantics(
        cues=[_cue(1, 0, 9_000, "目标")],
        target_ms=9_000,
        candidate_id="no-post-evidence",
        selection_hook="",
        selection_scorecard=_scorecard(),
        structured_context="",
        candidate_context="",
        llm_call=lambda _prompt: response,
        extract_json=_extract,
    )

    assert result["needs_more_context"] is False
    assert result["retry_scope"] == "none"


def test_boundary_review_rejects_evidence_cue_not_shown_to_reviewer():
    cues = [
        _cue(index, (index - 1) * 2_000, index * 2_000, f"第{index}句")
        for index in range(1, 21)
    ]
    response = json.dumps(
        {
            "syntax_complete": True,
            "story_closed": True,
            "next_topic_separated": True,
            "recommended_end_cue_index": 2,
            "evidence_cue_indexes": [1, 2, 20],
            "reason_codes": [],
            "summary": "伪造了请求外证据。",
        },
        ensure_ascii=False,
    )

    result = review_talk_boundary_semantics(
        cues=cues,
        target_ms=2_000,
        candidate_id="unseen-evidence",
        selection_hook="",
        selection_scorecard=_scorecard(),
        structured_context="",
        candidate_context="",
        llm_call=lambda _prompt: response,
        extract_json=_extract,
        max_forward_ms=10_000,
    )

    assert result["status"] == "BLOCK"
    assert "BOUNDARY_EVIDENCE_CUES_INVALID" in result["reason_codes"]


def test_boundary_review_requires_evidence_after_recommended_endpoint():
    response = json.dumps(
        {
            "syntax_complete": True,
            "story_closed": True,
            "next_topic_separated": True,
            "recommended_end_cue_index": 2,
            "evidence_cue_indexes": [1, 2],
            "reason_codes": [],
            "summary": "没有引用任何终点后的换题证据。",
        },
        ensure_ascii=False,
    )

    result = review_talk_boundary_semantics(
        cues=[
            _cue(1, 0, 4_000, "铺垫"),
            _cue(2, 4_100, 8_000, "回答结束"),
            _cue(3, 8_100, 10_000, "下一话题"),
        ],
        target_ms=4_000,
        candidate_id="missing-next-topic-witness",
        selection_hook="",
        selection_scorecard=_scorecard(),
        structured_context="",
        candidate_context="",
        llm_call=lambda _prompt: response,
        extract_json=_extract,
    )

    assert result["status"] == "BLOCK"
    assert "BOUNDARY_NEXT_TOPIC_WITNESS_MISSING" in result["reason_codes"]


def test_boundary_review_keeps_full_hash_bound_candidate_context():
    candidate_context = "前" * 12_000 + "尾部长期回调事实" * 600
    seen_prompt = ""

    def review(prompt: str) -> str:
        nonlocal seen_prompt
        seen_prompt = prompt
        return json.dumps(
            {
                "syntax_complete": True,
                "story_closed": True,
                "next_topic_separated": True,
                "recommended_end_cue_index": 1,
                "evidence_cue_indexes": [1, 2],
                "reason_codes": [],
                "summary": "目标闭环，第二句换题。",
            },
            ensure_ascii=False,
        )

    result = review_talk_boundary_semantics(
        cues=[
            _cue(1, 0, 5_000, "目标闭环"),
            _cue(2, 5_100, 7_000, "下一话题"),
        ],
        target_ms=5_000,
        candidate_id="full-context",
        selection_hook="",
        selection_scorecard=_scorecard(),
        structured_context="",
        candidate_context=candidate_context,
        llm_call=review,
        extract_json=_extract,
    )

    assert result["status"] == "PASS"
    assert candidate_context in seen_prompt
    assert "尾部长期回调事实" in seen_prompt


def test_final_delivery_terminal_cue_accepts_bound_source_separation_witness():
    cues = [
        _cue(1, 0, 4_000, "前句"),
        _cue(2, 4_100, 9_600, "最终闭合句"),
    ]
    seen_request: dict = {}

    def review(prompt: str) -> str:
        request = json.loads(
            prompt.split("绑定请求 JSON：\n", 1)[1].split(
                "\n\n只输出 JSON：", 1
            )[0]
        )
        seen_request.update(request)
        return json.dumps(
            {
                "syntax_complete": True,
                "story_closed": True,
                "next_topic_separated": True,
                "recommended_end_cue_index": 2,
                "evidence_cue_indexes": [1, 2],
                "reason_codes": [],
                "summary": "交付末句闭环，换题由 source 窗口证明。",
            },
            ensure_ascii=False,
        )

    result = review_talk_boundary_semantics(
        cues=cues,
        target_ms=9_600,
        candidate_id="final-delivery",
        selection_hook="完整包袱",
        selection_scorecard=_scorecard(),
        structured_context="",
        candidate_context="",
        llm_call=review,
        extract_json=_extract,
        terminal_source_review=_source_window_review(),
        source_final_start_ms=10_000,
        source_final_end_ms=20_000,
    )

    assert result["status"] == "PASS"
    assert result["review_scope"] == "final_delivery"
    assert result["next_topic_witness_valid"] is True
    assert result["source_separation_witness"]["status"] == "PASS"
    assert seen_request["terminal_source_separation_witness"] == result[
        "source_separation_witness"
    ]


def test_final_delivery_terminal_cue_rejects_interval_mismatched_source_witness():
    response = json.dumps(
        {
            "syntax_complete": True,
            "story_closed": True,
            "next_topic_separated": True,
            "recommended_end_cue_index": 2,
            "evidence_cue_indexes": [1, 2],
            "reason_codes": [],
            "summary": "模型不能把错区间的 source 证据当作换题证据。",
        },
        ensure_ascii=False,
    )

    result = review_talk_boundary_semantics(
        cues=[
            _cue(1, 0, 4_000, "前句"),
            _cue(2, 4_100, 9_600, "最终闭合句"),
        ],
        target_ms=9_600,
        candidate_id="final-delivery",
        selection_hook="完整包袱",
        selection_scorecard=_scorecard(),
        structured_context="",
        candidate_context="",
        llm_call=lambda _prompt: response,
        extract_json=_extract,
        terminal_source_review=_source_window_review(),
        source_final_start_ms=10_000,
        source_final_end_ms=20_001,
    )

    assert result["status"] == "BLOCK"
    assert result["next_topic_witness_valid"] is False
    assert "BOUNDARY_NEXT_TOPIC_WITNESS_MISSING" in result["reason_codes"]
    witness = result["source_separation_witness"]
    assert witness["status"] == "BLOCK"
    assert witness["reason_codes"] == ["SOURCE_DELIVERY_INTERVAL_MISMATCH"]


def test_exact_source_pin_accepts_bounded_pin_crossing_closure_cue():
    """V14 auto_193450_1863_2056: the fresh closure cue ends 210ms after the
    official pin, so no cue ends inside [pin-400, pin] at all.  The crossing
    cue is the closure witness; the media end stays exactly the pin."""

    pin_ms = 202_720
    scope = build_boundary_search_scope(
        semantic_target_ms=pin_ms,
        manual_lower_bound_ms=pin_ms,
        repair_cap_ms=30_000,
        boundary_end_mode="exact_source_pin",
    )
    cues = [
        _cue(1, 199_610, 201_370, "就是刚认识"),
        _cue(2, 201_370, pin_ms + 210, "暂时不太熟"),
        _cue(3, 203_650, 205_170, "嗯谢谢"),
        _cue(4, 205_170, 206_490, "下一条SC"),
    ]
    response = json.dumps(
        {
            "syntax_complete": True,
            "story_closed": True,
            "next_topic_separated": True,
            "recommended_end_cue_index": 2,
            "evidence_cue_indexes": [1, 2, 3, 4],
            "same_topic_continues_after_target": False,
            "needs_more_context": False,
            "reason_codes": [],
            "summary": "第二句闭合关系定义，pin 后进入致谢与下一条SC。",
        },
        ensure_ascii=False,
    )

    review = review_talk_boundary_semantics(
        cues=cues,
        target_ms=pin_ms,
        candidate_id="auto_193450_1863_2056",
        selection_hook="火锅关系闭环",
        selection_scorecard=_scorecard(),
        structured_context="官方 cue 912 为下一条SC",
        candidate_context="hash-bound context",
        llm_call=lambda _prompt: response,
        extract_json=_extract,
        max_forward_ms=0,
        boundary_search_scope=scope,
    )

    assert review["status"] == "PASS"
    assert review["recommended_end_cue_index"] == 2
    assert review["recommended_end_ms"] == pin_ms
    assert review["recommendation_relaxations"] == [
        {
            "kind": "pin_crossing_closure_cue",
            "cue_index": 2,
            "cue_end_ms": pin_ms + 210,
            "pin_ms": pin_ms,
            "overrun_ms": 210,
            "tolerance_ms": 600,
        }
    ]


def test_exact_source_pin_rejects_crossing_cue_beyond_tolerance():
    pin_ms = 202_720
    scope = build_boundary_search_scope(
        semantic_target_ms=pin_ms,
        manual_lower_bound_ms=pin_ms,
        repair_cap_ms=30_000,
        boundary_end_mode="exact_source_pin",
    )
    cues = [
        _cue(1, 199_610, 201_370, "就是刚认识"),
        _cue(2, 201_370, pin_ms + 700, "暂时不太熟还在往下说"),
        _cue(3, 203_650 + 700, 206_490, "下一条SC"),
    ]
    response = json.dumps(
        {
            "syntax_complete": True,
            "story_closed": True,
            "next_topic_separated": True,
            "recommended_end_cue_index": 2,
            "evidence_cue_indexes": [1, 2, 3],
            "same_topic_continues_after_target": False,
            "needs_more_context": False,
            "reason_codes": [],
            "summary": "越界过多的cue不可选。",
        },
        ensure_ascii=False,
    )

    review = review_talk_boundary_semantics(
        cues=cues,
        target_ms=pin_ms,
        candidate_id="auto_193450_1863_2056",
        selection_hook="火锅关系闭环",
        selection_scorecard=_scorecard(),
        structured_context="",
        candidate_context="hash-bound context",
        llm_call=lambda _prompt: response,
        extract_json=_extract,
        max_forward_ms=0,
        boundary_search_scope=scope,
    )

    assert review["status"] == "BLOCK"
    assert "BOUNDARY_RECOMMENDATION_OUT_OF_SCOPE" in review["reason_codes"]
    assert review["recommendation_relaxations"] == []


def test_semantic_floor_silent_gap_closure_cue_is_recommendable():
    """V14 auto_193450_1475_1543: the published old end sits 400ms of dead
    air after the story-closing cue; the closure cue must stay recommendable
    while the delivery floor itself is untouched."""

    scope = build_boundary_search_scope(
        semantic_target_ms=77_310,
        manual_lower_bound_ms=77_780,
        repair_cap_ms=30_000,
    )
    cues = [
        _cue(1, 72_800, 77_380, "再弹再再一弹一弹"),
        _cue(2, 78_280, 79_720, "谢谢刚刚"),
        _cue(3, 81_680, 83_680, "谢谢舰长欢迎上船"),
    ]
    response = json.dumps(
        {
            "syntax_complete": True,
            "story_closed": True,
            "next_topic_separated": True,
            "recommended_end_cue_index": 1,
            "evidence_cue_indexes": [1, 2, 3],
            "same_topic_continues_after_target": False,
            "needs_more_context": False,
            "reason_codes": [],
            "summary": "第一句落地包袱，其后为谢礼段新话题。",
        },
        ensure_ascii=False,
    )

    review = review_talk_boundary_semantics(
        cues=cues,
        target_ms=77_780,
        candidate_id="auto_193450_1475_1543",
        selection_hook="脑瓜崩镜像左右",
        selection_scorecard=_scorecard(),
        structured_context="",
        candidate_context="hash-bound context",
        llm_call=lambda _prompt: response,
        extract_json=_extract,
        max_forward_ms=30_000,
        boundary_search_scope=scope,
    )

    assert scope["delivery_lower_bound_ms"] == 77_780
    assert review["status"] == "PASS"
    assert review["recommended_end_cue_index"] == 1
    assert review["recommended_end_ms"] == 77_380
    assert review["recommendation_relaxations"] == [
        {
            "kind": "silent_gap_closure_cue",
            "cue_index": 1,
            "cue_end_ms": 77_380,
            "floor_ms": 77_780,
            "gap_ms": 400,
            "tolerance_ms": 400,
        }
    ]


def test_semantic_floor_gap_with_speech_keeps_closure_cue_ineligible():
    scope = build_boundary_search_scope(
        semantic_target_ms=77_310,
        manual_lower_bound_ms=77_780,
        repair_cap_ms=30_000,
    )
    cues = [
        _cue(1, 72_800, 77_380, "再弹再再一弹一弹"),
        _cue(2, 77_500, 77_760, "补一句"),
        _cue(3, 78_280, 79_720, "谢谢刚刚"),
    ]
    response = json.dumps(
        {
            "syntax_complete": True,
            "story_closed": True,
            "next_topic_separated": True,
            "recommended_end_cue_index": 1,
            "evidence_cue_indexes": [1, 2, 3],
            "same_topic_continues_after_target": False,
            "needs_more_context": False,
            "reason_codes": [],
            "summary": "间隙里还有话，不能提前收。",
        },
        ensure_ascii=False,
    )

    review = review_talk_boundary_semantics(
        cues=cues,
        target_ms=77_780,
        candidate_id="auto_193450_1475_1543",
        selection_hook="脑瓜崩镜像左右",
        selection_scorecard=_scorecard(),
        structured_context="",
        candidate_context="hash-bound context",
        llm_call=lambda _prompt: response,
        extract_json=_extract,
        max_forward_ms=30_000,
        boundary_search_scope=scope,
    )

    assert review["status"] == "BLOCK"
    assert "BOUNDARY_RECOMMENDATION_OUT_OF_SCOPE" in review["reason_codes"]
    # The absorbable closure is the LAST cue before the floor (20ms of air),
    # never an earlier cue whose recommendation would drop the speech at
    # 77_500..77_760.
    assert [
        relaxation["cue_index"]
        for relaxation in review["recommendation_relaxations"]
    ] == [2]


def test_baseline_tail_cap_bounds_recommendation_ceiling():
    """1573 r13 鼠标话题案：redelivery 包的 lower_bound 语义延伸不得越过
    已发布 baseline 覆盖终点——尾部恒等锚（头部锚的对偶）。"""
    from src.autoslice.boundary_semantic_review import build_boundary_search_scope

    capped = build_boundary_search_scope(
        semantic_target_ms=100_000,
        repair_cap_ms=60_000,
        manual_lower_bound_ms=100_000,
        baseline_tail_cap_ms=100_060,
    )
    assert capped["max_recommended_end_ms"] == 100_060
    uncapped = build_boundary_search_scope(
        semantic_target_ms=100_000,
        repair_cap_ms=60_000,
        manual_lower_bound_ms=100_000,
    )
    assert uncapped["max_recommended_end_ms"] == 160_000


def test_structured_payoff_hypothesis_yields_to_baseline_tail_cap():
    """1573 案补全：payoff 检测假设是唯一越过 baseline 终点
    的锚时让位（钳制+披露，评审在已发布终点裁收尾）；复核锚（语义/手动/
    owner）越界仍硬拦 BOUNDARY_REQUIRED_OWNER_EXCLUDED。"""

    from src.autoslice.boundary_semantic_review import (
        boundary_search_scope_is_valid,
        build_boundary_search_scope,
    )

    clamped = build_boundary_search_scope(
        semantic_target_ms=109_820,
        repair_cap_ms=30_000,
        manual_lower_bound_ms=109_820,
        structured_payoff_ms=116_830,
        required_owner_end_ms=105_140,
        baseline_tail_cap_ms=109_820,
    )
    assert clamped["status"] == "PASS"
    assert clamped["reason_codes"] == []
    assert clamped["structured_payoff_clamped_from_ms"] == 116_830
    assert clamped["structured_payoff_ms"] == 116_830  # 原值留档
    assert clamped["semantic_search_origin_ms"] == 109_820
    assert clamped["delivery_lower_bound_ms"] == 109_820
    assert clamped["max_recommended_end_ms"] == 109_820
    # 重建定点：新 scope 以自身字段重建必须逐字节自洽
    assert boundary_search_scope_is_valid(clamped)

    # 复核锚（manual）越界不是假设问题——仍硬拦
    hard_blocked = build_boundary_search_scope(
        semantic_target_ms=109_820,
        repair_cap_ms=30_000,
        manual_lower_bound_ms=116_830,
        structured_payoff_ms=116_830,
        required_owner_end_ms=105_140,
        baseline_tail_cap_ms=109_820,
    )
    assert "BOUNDARY_REQUIRED_OWNER_EXCLUDED" in hard_blocked["reason_codes"]
    assert hard_blocked["structured_payoff_clamped_from_ms"] is None


def test_exact_pin_payoff_hypothesis_yields_to_the_published_endpoint():
    """r13 钳制的 pin 模式对偶：pin 是已验证公开
    媒体的字节终点，比 baseline 尾锚更强；同一个越界 payoff 检测假设在弱模式
    下让位、在最强模式下反而硬拦是设计缺口。semantic/manual/owner 全部落在
    pin 内、唯独 payoff 越界 → 钳制到 pin 并披露；语义目标或必需 owner 真越过
    pin 才是真冲突，照旧硬拦。"""

    from src.autoslice.boundary_semantic_review import (
        boundary_search_scope_is_valid,
        build_boundary_search_scope,
    )

    # jyl-r5 实测数字：pin 113570，payoff 检测到 124320（已发布终点之后的
    # 下一段结构化朗读被误判为本事件收尾），owner 111000。
    clamped = build_boundary_search_scope(
        semantic_target_ms=113_010,
        repair_cap_ms=30_000,
        manual_lower_bound_ms=113_570,
        structured_payoff_ms=124_320,
        required_owner_end_ms=111_000,
        baseline_tail_cap_ms=113_570,
        boundary_end_mode="exact_source_pin",
    )
    assert clamped["status"] == "PASS"
    assert clamped["reason_codes"] == []
    assert clamped["structured_payoff_clamped_from_ms"] == 124_320
    assert clamped["structured_payoff_ms"] == 124_320  # 原值留档
    assert clamped["semantic_search_origin_ms"] == 113_570
    assert clamped["delivery_lower_bound_ms"] == 113_570
    assert clamped["max_recommended_end_ms"] == 113_570
    assert boundary_search_scope_is_valid(clamped)

    # 必需 owner 真越过 pin → 不是假设问题，仍硬拦
    hard_blocked = build_boundary_search_scope(
        semantic_target_ms=113_010,
        repair_cap_ms=30_000,
        manual_lower_bound_ms=113_570,
        structured_payoff_ms=124_320,
        required_owner_end_ms=118_000,
        baseline_tail_cap_ms=113_570,
        boundary_end_mode="exact_source_pin",
    )
    assert (
        "BOUNDARY_EXACT_SOURCE_PIN_PAYOFF_CONFLICT"
        in hard_blocked["reason_codes"]
    )
    assert hard_blocked["structured_payoff_clamped_from_ms"] is None


def test_search_scope_identity_replay_requires_same_tail_cap():
    """V15 regression: the resolution-side replay must pass the same
    baseline_tail_cap_ms the freeze side stored, or the three-way dict
    identity check rejects every redelivery candidate."""

    kwargs = dict(
        semantic_target_ms=90_000,
        manual_lower_bound_ms=None,
        structured_payoff_ms=None,
        required_owner_end_ms=88_000,
        repair_cap_ms=30_000,
        last_piece_start_ms=0,
        prior_piece_duration_ms=0,
        boundary_end_mode="semantic_lower_bound",
    )
    frozen = build_boundary_search_scope(
        **kwargs, baseline_tail_cap_ms=92_000
    )
    replay_with_cap = build_boundary_search_scope(
        **kwargs, baseline_tail_cap_ms=92_000
    )
    replay_without_cap = build_boundary_search_scope(**kwargs)

    assert dict(frozen) == dict(replay_with_cap)
    assert dict(frozen) != dict(replay_without_cap)


def test_exact_pin_mode_ignores_baseline_tail_cap():
    """1863 r18: the pin is the published media byte end (media axis); the
    baseline coverage end sits one tail pad earlier on the subtitle axis.
    Clamping the pin by the cap made every pinned redelivery BLOCK."""

    pin = 202_720
    scope = build_boundary_search_scope(
        semantic_target_ms=pin,
        manual_lower_bound_ms=pin,
        repair_cap_ms=30_000,
        boundary_end_mode="exact_source_pin",
        baseline_tail_cap_ms=pin - 400,
    )
    assert scope["status"] == "PASS"
    assert scope["max_recommended_end_ms"] == pin
    # the cap still participates in the frozen identity
    assert scope["baseline_tail_cap_ms"] == pin - 400

    lower = build_boundary_search_scope(
        semantic_target_ms=90_000,
        repair_cap_ms=30_000,
        boundary_end_mode="semantic_lower_bound",
        baseline_tail_cap_ms=95_000,
    )
    assert lower["max_recommended_end_ms"] == 95_000
