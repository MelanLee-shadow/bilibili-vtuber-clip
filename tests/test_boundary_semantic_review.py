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
