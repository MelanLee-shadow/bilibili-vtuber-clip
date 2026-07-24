import json

from src.autoslice.boundary_semantic_review import review_talk_boundary_semantics
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
