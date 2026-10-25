"""BND-03 story-ending comparison (30-boundary 「结尾候选比较」).

公开规则：像厦大这种情况不需要问我。故事更完整干净直接占优.
公开规则：如果按当前的结尾截止，是否是个好故事，如果是再继续延续，是否会更好，
这是需要比较的。如果还吊人胃口，那么继续延续把话题结束是最好的。但是一般只要把核心话题
说完就行。

A missing post-end topic-change witness no longer means "cannot end": a typed
story-ending comparison is a second proof of proposition 4, carried through
the source witness, the final-delivery review, the correlated projection and
the package validators. Reviewer replies below follow the v3 output contract
and are constructed fixtures (no provider call); cue timings of the real
members are copied read-only from OCI3 (text replaced by placeholders).
"""

import json
from pathlib import Path

import pytest

from scripts.produce_slice_package import enforce_minimum_effective_duration
from src.autoslice.boundary_endpoint_binding import bind_final_semantic_endpoint
from src.autoslice.boundary_semantic_projection import (
    project_correlated_source_boundary_pass,
)
from src.autoslice.boundary_semantic_review import (
    STORY_ENDING_PROOF_KIND,
    build_boundary_search_scope,
    boundary_search_scope_is_valid,
    review_talk_boundary_semantics,
    source_ending_proof_kind,
    story_ending_proof_is_valid,
)
from src.autoslice.jingting_chunker import SrtCue
from src.autoslice.producer_boundary_resolution import (
    BoundaryResolutionAdapters,
    _select_initial_boundary,
)
from src.autoslice.producer_boundary_review_stage import (
    review_exact_delivery_boundary_semantics,
)
from src.autoslice.review_package_boundary_validators import (
    semantic_boundary_review_is_valid,
)

FIXTURES = Path(__file__).parent / "fixtures"


def _cue(index, start_ms, end_ms, text=None):
    return SrtCue(index=str(index), start_ms=start_ms, end_ms=end_ms,
                  text=text if text is not None else f"第{index}句")


def _scorecard():
    return {"status": "VALID", "dimensions": {"self_contained": 4, "comedic_payoff": 3}}


def _review(cues, *, target_ms, reply, max_forward_ms=30_000, scope=None,
            candidate_id="bnd03", hook="核心故事", seen=None):
    def call(prompt):
        if seen is not None:
            seen.append(prompt)
        return json.dumps(reply, ensure_ascii=False)

    return review_talk_boundary_semantics(
        cues=cues, target_ms=target_ms, candidate_id=candidate_id,
        selection_hook=hook, selection_scorecard=_scorecard(),
        structured_context="", candidate_context="",
        llm_call=call, extract_json=json.loads, max_forward_ms=max_forward_ms,
        **({"boundary_search_scope": scope} if scope is not None else {}),
    )


def _reply(**fields):
    base = {
        "syntax_complete": True, "story_closed": True, "next_topic_separated": False,
        "content_anchor_covered": True, "same_topic_continues_after_target": False,
        "needs_more_context": False, "reason_codes": [], "summary": "构造的 v3 回包。",
    }
    base.update(fields)
    return base


def _legacy(reply):
    """The same verdict without any v3 comparison field (old output contract)."""
    return {k: v for k, v in reply.items() if k not in {
        "core_topic_complete", "leaves_audience_hanging", "compared_end_cue_indexes",
        "next_question_start_cue_index", "ending_comparison_reason"}}


def _select(review, cues, *, semantic_end_ms, piece_end_ms, cap_ms):
    return _select_initial_boundary(
        spec={"pieces": [{"start_ms": 0, "end_ms": piece_end_ms}],
              "semantic_start_ms": 0, "semantic_end_ms": semantic_end_ms,
              "boundary_semantic_review": review,
              "boundary_search_scope": review.get("boundary_search_scope")},
        durations=[piece_end_ms], padded=Path("/nonexistent/unused.mp4"),
        padded_dur=piece_end_ms, out_root=Path("/nonexistent"),
        transcriber=lambda *_a: "", cues=cues, required_tail_end_ms=None,
        adapters=BoundaryResolutionAdapters(
            accurate_recut_command=lambda **_k: [], run_command=lambda *_a, **_k: None),
        boundary_repair_extend_cap_ms=cap_ms,
    )


# ---------------------------------------------------------------- synthetic

SUMMARY_CUES = [
    _cue(1, 0, 9_000, "亲戚问我考不考清北，我说要去厦大"),
    _cue(2, 9_100, 20_000, "因为易中天老师和辩论队"),
    _cue(3, 20_100, 28_000, "不过我还是觉得，不管什么学校，都靠自己"),
    _cue(4, 28_500, 31_000, "双非，什么是双非啊"),
    _cue(5, 31_100, 36_000, "双非就是非985211"),
]
SUMMARY_REPLY = _reply(
    recommended_end_cue_index=3, evidence_cue_indexes=[1, 2, 3],
    core_topic_complete=True, leaves_audience_hanging=False,
    compared_end_cue_indexes=[2, 3], next_question_start_cue_index=4,
    ending_comparison_reason="2 已是好故事；3 是同话题收束总结，包含后更完整；4 起是新问题。",
)


def test_core_topic_told_then_summary_selects_summary_end_without_topic_witness():
    """① core topic told, the continuation is a same-topic summary → its end,
    no topic-change witness required."""
    seen = []
    review = _review(SUMMARY_CUES, target_ms=9_000, reply=SUMMARY_REPLY, seen=seen)

    assert review["status"] == "PASS"
    assert review["next_topic_separated"] is False
    assert review["ending_proof_kind"] == STORY_ENDING_PROOF_KIND
    assert review["recommended_end_cue_index"] == 3
    assert review["recommended_end_ms"] == 28_000
    assert review["retry_scope"] == "none" and review["needs_more_context"] is False
    record = review["story_ending_comparison"]
    assert record["status"] == "PASS" and record["reason_codes"] == []
    assert record["request_sha256"] == review["request_sha256"]
    assert record["cue_grid_sha256"] == review["cue_grid_sha256"]
    assert record["compared_end_cue_indexes"] == [2, 3]
    assert record["next_question_start_cue_index"] == 4
    assert story_ending_proof_is_valid(review)
    assert "NEXT_TOPIC_SEPARATED_NOT_PROVEN" not in review["reason_codes"]
    assert json.loads(seen[0].split("绑定请求 JSON：\n", 1)[1].split("\n\n只输出 JSON：", 1)[0])[
        "endpoint_selection_contract_version"] == "talk-boundary-endpoint-selection-contract.v3"
    assert "一般只要把核心话题说完就行" in seen[0]

    initial = _select(review, SUMMARY_CUES, semantic_end_ms=9_000, piece_end_ms=60_000, cap_ms=30_000)
    assert initial.target_rel == 28_000 and initial.snapped_end == 28_000


def test_old_contract_blocks_case_one_guard():
    """⑤ guard: the same verdict under the old output contract stays BLOCK —
    the PASS comes from the new typed proof, not from a loosened gate."""
    review = _review(SUMMARY_CUES, target_ms=9_000, reply=_legacy(SUMMARY_REPLY))

    assert review["status"] == "BLOCK"
    assert "NEXT_TOPIC_SEPARATED_NOT_PROVEN" in review["reason_codes"]
    assert "story_ending_comparison" not in review and "ending_proof_kind" not in review
    with pytest.raises(SystemExit, match="BOUNDARY_SEMANTIC_REVIEW_REQUIRED"):
        _select(review, SUMMARY_CUES, semantic_end_ms=9_000, piece_end_ms=60_000, cap_ms=30_000)


def test_old_responsible_mechanism_blocks_the_same_v3_case_one(monkeypatch):
    """⑤ Disable only the new proof generator, keeping the same input/reply:
    the original topic-witness gate must BLOCK the positive witness."""
    from src.autoslice import boundary_semantic_review as module

    repaired = _review(SUMMARY_CUES, target_ms=9_000, reply=SUMMARY_REPLY)
    assert repaired["status"] == "PASS"
    monkeypatch.setattr(module, "_story_ending_comparison", lambda **_kwargs: (None, []))
    original = _review(SUMMARY_CUES, target_ms=9_000, reply=SUMMARY_REPLY)
    assert original["status"] == "BLOCK"
    assert "NEXT_TOPIC_SEPARATED_NOT_PROVEN" in original["reason_codes"]


QUESTION_CUES = [
    _cue(1, 0, 9_000, "目标故事"),
    _cue(2, 9_100, 18_000, "那你们猜我最后去了哪个学校？"),
    _cue(3, 18_100, 33_000, "我最后去的是……"),
    _cue(4, 33_100, 45_000, "答案揭晓"),
]


@pytest.mark.parametrize("max_forward_ms", [30_000, 60_000])
def test_question_asked_but_not_answered_must_continue(max_forward_ms):
    """② a question asked but not answered is a hanging ending: never PASS."""
    reply = _reply(
        story_closed=False, recommended_end_cue_index=2, evidence_cue_indexes=[1, 2, 3],
        core_topic_complete=False, leaves_audience_hanging=True,
        compared_end_cue_indexes=[1, 2], next_question_start_cue_index=None,
        same_topic_continues_after_target=True, needs_more_context=True,
        ending_comparison_reason="2 抛出问题未回答，吊人胃口，需延续到回答。",
    )
    review = _review(QUESTION_CUES, target_ms=9_000, reply=reply, max_forward_ms=max_forward_ms)

    assert review["status"] == "BLOCK"
    assert "STORY_ENDING_LEAVES_AUDIENCE_HANGING" in review["reason_codes"]
    assert "STORY_ENDING_CORE_TOPIC_NOT_COMPLETE" in review["reason_codes"]
    assert "ending_proof_kind" not in review
    # The existing bounded retry still carries it forward once.
    assert review["retry_scope"] == "same_topic_continues"


def test_hanging_ending_blocks_even_with_a_claimed_topic_change():
    reply = _reply(
        next_topic_separated=True, recommended_end_cue_index=2, evidence_cue_indexes=[1, 2, 3],
        core_topic_complete=True, leaves_audience_hanging=True,
        compared_end_cue_indexes=[1, 2], next_question_start_cue_index=3,
        ending_comparison_reason="自相矛盾：仍吊胃口。",
    )
    review = _review(QUESTION_CUES, target_ms=9_000, reply=reply)

    assert review["status"] == "BLOCK"
    assert "STORY_ENDING_LEAVES_AUDIENCE_HANGING" in review["reason_codes"]


@pytest.mark.parametrize("topic_claim", [False, True])
def test_ending_past_the_next_question_start_is_rejected(topic_claim):
    """③ the next new question is the natural topic change: crossing it is
    rejected on either proof route."""
    reply = _reply(
        next_topic_separated=topic_claim, recommended_end_cue_index=5,
        evidence_cue_indexes=[1, 2, 3, 4, 5],
        core_topic_complete=True, leaves_audience_hanging=False,
        compared_end_cue_indexes=[3, 5], next_question_start_cue_index=4,
        ending_comparison_reason="延续到5（越过了4起的新问题）。",
    )
    review = _review(SUMMARY_CUES, target_ms=9_000, reply=reply)

    assert review["status"] == "BLOCK"
    assert "BOUNDARY_ENDING_CROSSES_NEXT_QUESTION" in review["reason_codes"]
    assert not story_ending_proof_is_valid(review)


@pytest.mark.parametrize("topic_claim", [False, True])
@pytest.mark.parametrize("next_start_ms", [27_500, 28_000])
def test_next_question_start_is_checked_in_source_time(topic_claim, next_start_ms):
    """Cue order alone is insufficient when ASR cues overlap in time."""
    cues = [
        _cue(4, next_start_ms, cue.end_ms, cue.text) if i == 4 else cue
        for i, cue in enumerate(SUMMARY_CUES, 1)
    ]
    reply = dict(SUMMARY_REPLY, next_topic_separated=topic_claim,
                 evidence_cue_indexes=[1, 2, 3, 4])
    review = _review(cues, target_ms=9_000, reply=reply)
    if next_start_ms < 28_000:
        assert review["status"] == "BLOCK"
        assert "BOUNDARY_ENDING_CROSSES_NEXT_QUESTION" in review["reason_codes"]
    else:
        assert review["status"] == "PASS"


def test_story_ending_does_not_lower_the_sixty_second_floor():
    """④ the story-ending proof decides the semantic endpoint only; the
    ordinary new-talk effective duration must still be strictly over 60 s."""
    review = _review(SUMMARY_CUES, target_ms=9_000, reply=SUMMARY_REPLY)
    initial = _select(review, SUMMARY_CUES, semantic_end_ms=9_000, piece_end_ms=60_000, cap_ms=30_000)
    spec = {"minimum_effective_duration_ms": 60_000}

    assert review["status"] == "PASS"
    with pytest.raises(SystemExit, match="TALK_EFFECTIVE_DURATION_NOT_OVER_60S_AFTER_BOUNDARY"):
        enforce_minimum_effective_duration(spec, final_start_ms=0, final_end_ms=initial.snapped_end + 400)
    with pytest.raises(SystemExit, match="TALK_EFFECTIVE_DURATION_NOT_OVER_60S_AFTER_BOUNDARY"):
        enforce_minimum_effective_duration(spec, final_start_ms=0, final_end_ms=60_000)
    enforce_minimum_effective_duration(spec, final_start_ms=0, final_end_ms=60_001)


def test_story_ending_keeps_the_required_owner_floor():
    scope = build_boundary_search_scope(
        semantic_target_ms=9_000, repair_cap_ms=30_000, required_owner_end_ms=31_000)
    review = _review(SUMMARY_CUES, target_ms=scope["review_target_ms"],
                     reply=SUMMARY_REPLY, scope=scope)
    assert review["status"] == "BLOCK"
    assert "STORY_ENDING_RECOMMENDATION_NOT_ELIGIBLE" in review["reason_codes"]


def test_story_ending_does_not_bypass_source_coverage():
    from src.autoslice.producer_boundary_review_stage import review_final_boundary_semantics

    scope = build_boundary_search_scope(semantic_target_ms=9_000, repair_cap_ms=30_000)

    def must_not_call(_prompt):
        raise AssertionError("Missing source coverage must block before a provider call")

    review = review_final_boundary_semantics(
        cues=SUMMARY_CUES, boundary_target_ms=9_000, candidate_id="bnd03",
        selection_hook="核心故事", selection_scorecard=_scorecard(),
        structured_context="", candidate_context="", boundary_max_forward_ms=30_000,
        llm_call=must_not_call, extract_json=json.loads, boundary_search_scope=scope,
        available_local_source_context_end_ms=scope["required_local_source_context_end_ms"] - 1,
    )
    assert review["status"] == "BLOCK"
    assert "BOUNDARY_SOURCE_WITNESS_RESERVE_INCOMPLETE" in review["reason_codes"]


@pytest.mark.parametrize("change, reason", [
    ({"compared_end_cue_indexes": [2]}, "STORY_ENDING_COMPARISON_INVALID"),
    ({"compared_end_cue_indexes": [3, 99]}, "STORY_ENDING_COMPARISON_INVALID"),
    ({"next_question_start_cue_index": 99}, "STORY_ENDING_NEXT_QUESTION_INVALID"),
    ({"ending_comparison_reason": " "}, "STORY_ENDING_REASON_MISSING"),
    ({"core_topic_complete": "yes"}, "STORY_ENDING_CORE_TOPIC_COMPLETE_INVALID"),
    ({"leaves_audience_hanging": None}, "STORY_ENDING_LEAVES_AUDIENCE_HANGING_INVALID"),
    ({"next_topic_separated": True}, "STORY_ENDING_UNPROVEN_TRANSITION_CLAIM"),
    ({"story_closed": False}, "STORY_ENDING_FOUR_PROPOSITION_BASE_NOT_PROVEN"),
])
def test_malformed_comparison_fails_closed(change, reason):
    reply = dict(SUMMARY_REPLY)
    reply.update(change)
    if change.get("next_topic_separated"):
        reply["evidence_cue_indexes"] = [1, 2, 3]  # claimed transition, no post-end cue cited
    review = _review(SUMMARY_CUES, target_ms=9_000, reply=reply)

    assert review["status"] == "BLOCK"
    assert reason in review["reason_codes"]


@pytest.mark.parametrize("field", [
    "core_topic_complete", "leaves_audience_hanging", "compared_end_cue_indexes",
    "next_question_start_cue_index", "ending_comparison_reason",
])
def test_partial_story_comparison_cannot_pass(field):
    reply = dict(SUMMARY_REPLY)
    reply.pop(field)
    review = _review(SUMMARY_CUES, target_ms=9_000, reply=reply)
    assert review["status"] == "BLOCK"
    assert "STORY_ENDING_FIELDS_MISSING" in review["reason_codes"]


def test_only_one_complete_ending_among_many_recommendable_cues_can_pass():
    cues = [
        _cue(1, 0, 9_000, "故事刚开始"),
        _cue(2, 9_100, 20_000, "问题尚未回答的中段"),
        _cue(3, 20_100, 28_000, "答案揭晓，故事在此收束"),
        _cue(4, 28_500, 31_000, "下一个新问题"),
    ]
    reply = dict(SUMMARY_REPLY, compared_end_cue_indexes=[3],
                 ending_comparison_reason="1、2核心未完，只有3是完整结尾；4起是新问题，不宜延续。")
    review = _review(cues, target_ms=9_000, reply=reply)
    assert review["status"] == "PASS"
    assert story_ending_proof_is_valid(review)


def test_backward_trim_keeps_its_own_topic_witness_gate():
    scope = build_boundary_search_scope(
        semantic_target_ms=28_000, repair_cap_ms=30_000, semantic_tail_trim_cap_ms=15_000)
    reply = dict(SUMMARY_REPLY, recommended_end_cue_index=2, compared_end_cue_indexes=[2, 3],
                 next_question_start_cue_index=3)
    review = _review(SUMMARY_CUES, target_ms=28_000, reply=reply, scope=scope)

    assert review["status"] == "BLOCK"
    assert "STORY_ENDING_RECOMMENDATION_NOT_ELIGIBLE" in review["reason_codes"]


def test_exact_source_pin_does_not_take_the_story_route():
    scope = build_boundary_search_scope(
        semantic_target_ms=20_000, repair_cap_ms=30_000,
        manual_lower_bound_ms=28_000, boundary_end_mode="exact_source_pin")
    reply = dict(SUMMARY_REPLY, compared_end_cue_indexes=[3])
    review = _review(SUMMARY_CUES, target_ms=28_000, reply=reply, scope=scope, max_forward_ms=0)

    assert review["status"] == "BLOCK"
    assert "STORY_ENDING_EXACT_SOURCE_PIN_NOT_ELIGIBLE" in review["reason_codes"]


def test_first_round_better_summary_takes_the_retry_and_sixty_seconds_accepts():
    """BND-03 first sentence: with the retry still available, a cited
    same-topic wrap-up past the window wins over an in-window good ending; at
    the 60 s cap the good-story ending is accepted instead of BLOCK."""
    cues = [
        _cue(1, 0, 9_000, "目标故事"),
        _cue(2, 9_100, 20_000, "窗内完整收束"),
        _cue(3, 30_500, 37_000, "同话题总结开头"),
        _cue(4, 37_100, 45_000, "同话题总结末尾"),
    ]
    reply = _reply(
        recommended_end_cue_index=2, evidence_cue_indexes=[1, 2, 3],
        core_topic_complete=True, leaves_audience_hanging=False,
        compared_end_cue_indexes=[2, 4], next_question_start_cue_index=None,
        same_topic_continues_after_target=True, needs_more_context=True,
        ending_comparison_reason="2 已是好故事，但 3-4 是同话题收束总结，包含更完整。",
    )
    first = _review(cues, target_ms=9_000, reply=reply, max_forward_ms=30_000)
    assert first["status"] == "BLOCK"
    assert first["retry_scope"] == "same_topic_continues"
    assert "STORY_ENDING_BETTER_CONTINUATION_PENDING_RETRY" in first["reason_codes"]

    better = _review(cues, target_ms=9_000, max_forward_ms=60_000, reply=dict(
        reply, recommended_end_cue_index=4, compared_end_cue_indexes=[2, 4],
        evidence_cue_indexes=[1, 2, 3, 4], same_topic_continues_after_target=False,
        needs_more_context=False))
    assert better["status"] == "PASS" and better["recommended_end_ms"] == 45_000


def test_sixty_second_cap_accepts_good_story_despite_out_of_window_same_topic():
    cues = [
        _cue(1, 0, 9_000, "目标故事"),
        _cue(2, 9_100, 60_000, "窗内完整收束"),
        _cue(3, 62_000, 75_000, "同一大话题继续闲聊"),
    ]
    reply = _reply(
        recommended_end_cue_index=2, evidence_cue_indexes=[1, 2, 3],
        core_topic_complete=True, leaves_audience_hanging=False,
        compared_end_cue_indexes=[1, 2], next_question_start_cue_index=None,
        same_topic_continues_after_target=True, needs_more_context=True,
        ending_comparison_reason="2 处核心话题已说完、不吊胃口；3 仍在聊但窗内放不下。",
    )
    review = _review(cues, target_ms=9_000, reply=reply, max_forward_ms=60_000)

    assert review["status"] == "PASS"
    assert review["ending_proof_kind"] == STORY_ENDING_PROOF_KIND
    assert review["needs_more_context"] is False and review["retry_scope"] == "none"
    assert review["story_ending_comparison"]["same_topic_continues_after_selected"] is True


# ------------------------------------------------- whole-path consumption

def _source_and_delivery(cues, review, *, final_start_ms, delivery_reply, delivery_call_log=None):
    recommended = review["recommended_end_cue_index"]
    closure = [c for c in cues if c.text.strip()][recommended - 1]
    snapped = closure.end_ms
    final_end = snapped + 400
    bound, reasons = bind_final_semantic_endpoint(
        semantic_review=review, cues=cues, closure_cue=closure, snapped_end_ms=snapped,
        final_start_ms=final_start_ms, final_end_ms=final_end)
    assert reasons == []
    delivery_cues = [
        _cue(position, max(0, c.start_ms - final_start_ms), c.end_ms - final_start_ms, c.text)
        for position, c in enumerate(
            [c for c in cues if final_start_ms <= c.end_ms <= snapped], start=1)
    ]

    def call(prompt):
        if delivery_call_log is not None:
            delivery_call_log.append(prompt)
        reply = dict(delivery_reply)
        reply.setdefault("recommended_end_cue_index", len(delivery_cues))
        reply.setdefault("compared_end_cue_indexes", [len(delivery_cues)])
        reply.setdefault("evidence_cue_indexes", [len(delivery_cues)])
        return json.dumps(reply, ensure_ascii=False)

    delivery = review_exact_delivery_boundary_semantics(
        cues=delivery_cues, source_boundary_review=bound,
        source_final_start_ms=final_start_ms, source_final_end_ms=final_end,
        candidate_id=review["candidate_id"], selection_hook="核心故事",
        selection_scorecard=_scorecard(), structured_context="", candidate_context="",
        boundary_max_forward_ms=30_000, llm_call=call, extract_json=json.loads,
    )
    return bound, delivery


DELIVERY_STORY_REPLY = _reply(
    core_topic_complete=True, leaves_audience_hanging=False,
    next_question_start_cue_index=None,
    ending_comparison_reason="最终字幕最后一条完成同话题收束，核心话题已说完，不吊胃口。",
)


def test_story_ending_reaches_final_delivery_and_package_validators():
    """One proof through every consumer: source review → endpoint binding →
    source witness → final-delivery review → package validators."""
    review = _review(SUMMARY_CUES, target_ms=9_000, reply=SUMMARY_REPLY)
    prompts = []
    bound, delivery = _source_and_delivery(
        SUMMARY_CUES, review, final_start_ms=0, delivery_reply=DELIVERY_STORY_REPLY,
        delivery_call_log=prompts)

    witness = delivery["source_separation_witness"]
    assert witness["status"] == "PASS" and witness["reason_codes"] == []
    assert witness["ending_proof_kind"] == STORY_ENDING_PROOF_KIND
    assert source_ending_proof_kind(bound) == STORY_ENDING_PROOF_KIND
    assert "story_complete_ending" in prompts[0]
    assert delivery["status"] == "PASS"
    assert delivery["review_scope"] == "final_delivery"
    assert delivery["ending_proof_kind"] == STORY_ENDING_PROOF_KIND
    assert delivery["next_topic_separated"] is False
    assert delivery["final_endpoint_binding"]["status"] == "PASS"
    assert semantic_boundary_review_is_valid(bound, expected_scope="source_full_window")
    assert semantic_boundary_review_is_valid(delivery, expected_scope="final_delivery")


def test_story_witness_cannot_be_upgraded_into_a_topic_change():
    review = _review(SUMMARY_CUES, target_ms=9_000, reply=SUMMARY_REPLY)
    claim = _legacy(dict(DELIVERY_STORY_REPLY, next_topic_separated=True))
    bound, delivery = _source_and_delivery(SUMMARY_CUES, review, final_start_ms=0,
                                           delivery_reply=claim)
    delivery_cues = [_cue(i, c.start_ms, c.end_ms, c.text) for i, c in enumerate(SUMMARY_CUES[:3], 1)]
    raw = review_talk_boundary_semantics(
        cues=delivery_cues, target_ms=28_000, candidate_id="bnd03", selection_hook="核心故事",
        selection_scorecard=_scorecard(), structured_context="", candidate_context="",
        llm_call=lambda _p: json.dumps(dict(claim, recommended_end_cue_index=3,
                                            evidence_cue_indexes=[3]), ensure_ascii=False),
        extract_json=json.loads, terminal_source_review=bound,
        source_final_start_ms=0, source_final_end_ms=28_400,
    )

    # A story-kind witness never stands in for a post-end transition cue.
    assert raw["status"] == "BLOCK"
    assert "BOUNDARY_NEXT_TOPIC_WITNESS_MISSING" in raw["reason_codes"]
    # The correlated projection may only carry the source's own story proof.
    assert delivery["next_topic_separated"] is False
    assert delivery.get("ending_proof_kind") == STORY_ENDING_PROOF_KIND


def test_correlated_delivery_block_projects_the_story_proof_not_a_topic_change():
    review = _review(SUMMARY_CUES, target_ms=9_000, reply=SUMMARY_REPLY)
    unsure = dict(DELIVERY_STORY_REPLY, story_closed=False, core_topic_complete=True,
                  leaves_audience_hanging=False)
    bound, delivery = _source_and_delivery(SUMMARY_CUES, review, final_start_ms=0,
                                           delivery_reply=unsure)

    assert delivery["status"] == "PASS"
    assert delivery["correlated_source_projection"]["ending_proof_kind"] == STORY_ENDING_PROOF_KIND
    assert delivery["next_topic_separated"] is False
    assert semantic_boundary_review_is_valid(delivery, expected_scope="final_delivery")

    hanging = dict(unsure, leaves_audience_hanging=True)
    _bound, blocked = _source_and_delivery(SUMMARY_CUES, review, final_start_ms=0,
                                           delivery_reply=hanging)
    assert blocked["status"] == "BLOCK"
    assert "correlated_source_projection" not in blocked


@pytest.mark.parametrize("source_has_topic_witness", [False, True])
@pytest.mark.parametrize("negative", [
    {"leaves_audience_hanging": True},
    {"core_topic_complete": False},
    {"next_question_start_cue_index": 3},
])
def test_delivery_negative_ending_verdict_cannot_be_projected_to_pass(
    source_has_topic_witness, negative,
):
    source_reply = dict(SUMMARY_REPLY)
    if source_has_topic_witness:
        source_reply.update(next_topic_separated=True, evidence_cue_indexes=[1, 2, 3, 4])
    source = _review(SUMMARY_CUES, target_ms=9_000, reply=source_reply)
    reply = dict(DELIVERY_STORY_REPLY, next_topic_separated=source_has_topic_witness, **negative)
    _bound, delivery = _source_and_delivery(
        SUMMARY_CUES, source, final_start_ms=0, delivery_reply=reply)
    assert delivery["status"] == "BLOCK"
    assert "correlated_source_projection" not in delivery


def test_tampered_story_record_is_rejected_everywhere():
    review = _review(SUMMARY_CUES, target_ms=9_000, reply=SUMMARY_REPLY)
    bound, _ = _source_and_delivery(SUMMARY_CUES, review, final_start_ms=0,
                                    delivery_reply=DELIVERY_STORY_REPLY)
    for mutate in (
        lambda r: r["story_ending_comparison"].update(leaves_audience_hanging=True),
        lambda r: r["story_ending_comparison"].update(selected_end_cue_index=2),
        lambda r: r["story_ending_comparison"].update(request_sha256="sha256:" + "0" * 64),
        lambda r: r["story_ending_comparison"].update(next_question_start_cue_index=3),
        lambda r: r["story_ending_comparison"].update(next_question_start_ms=27_500),
        lambda r: r["story_ending_comparison"].update(selected_end_ms=27_999),
        lambda r: r["story_ending_comparison"].update(reason_codes=123),
        lambda r: r.pop("ending_proof_kind"),
    ):
        tampered = json.loads(json.dumps(bound))
        mutate(tampered)
        assert not story_ending_proof_is_valid(tampered)
        assert not semantic_boundary_review_is_valid(tampered, expected_scope="source_full_window")
        assert source_ending_proof_kind(tampered) is None


def _srt(cues):
    def stamp(ms):
        return f"{ms // 3_600_000:02d}:{ms // 60_000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}"
    return "\n".join(f"{i}\n{stamp(c.start_ms)} --> {stamp(c.end_ms)}\n{c.text}\n"
                     for i, c in enumerate(cues, start=1))


def test_package_boundary_contract_accepts_story_proof_and_rejects_kind_drift(tmp_path):
    from src.autoslice.review_package_boundary_contract import audit_boundary_contract

    review = _review(SUMMARY_CUES, target_ms=9_000, reply=SUMMARY_REPLY)
    bound, delivery = _source_and_delivery(SUMMARY_CUES, review, final_start_ms=0,
                                           delivery_reply=DELIVERY_STORY_REPLY)
    subtitle = tmp_path / "bnd03.srt"
    subtitle.write_text(_srt(SUMMARY_CUES[:3]), encoding="utf-8")

    def codes(final_review, **audit_changes):
        issues = []
        audit = {
            "boundary_authority": "correlated_semantic_review_plus_deterministic_guards",
            "boundary_semantic_review": bound,
            "final_delivery_boundary_semantic_review": final_review,
            "final_start_ms": 0, "snapped_sentence_end_ms": 28_000, "final_end_ms": 28_400,
            "boundary_selection_lower_bound_ms": 28_000,
            "delivery_coverage_lower_bound_ms": 28_000,
            "delivery_coverage_verification": {"status": "PASS", "failure": None},
            "tail_pad_coverage_bridge": {
                "status": "NOT_NEEDED", "delivery_lower_bound_ms": 28_000,
                "closure_lower_bound_ms": 28_000, "maximum_tail_pad_ms": 400,
            },
            "frozen_required_boundary_owner_count": 0,
            "frozen_required_boundary_owners": [],
            "required_boundary_owner_verification": {"status": "PASS", "failures": []},
        }
        audit.update(audit_changes)
        audit_boundary_contract(
            issue_adder=lambda rows, code, **fields: rows.append({"code": code, **fields}),
            issues=issues, stem="bnd03", record_path=tmp_path / "bnd03.record.json",
            subtitle_path=subtitle, exact_final_review={"boundary_semantic_review": final_review},
            record={"boundary_audit": audit},
            story_contract={"boundary_semantic_review": final_review},
            required=True, is_song=False,
        )
        return {issue["code"] for issue in issues}

    clean = codes(delivery)
    assert clean == set()

    drifted = json.loads(json.dumps(delivery))
    drifted["source_separation_witness"].pop("ending_proof_kind")
    drift = codes(drifted)
    assert "BOUNDARY_SOURCE_SEPARATION_WITNESS_INVALID" in drift
    assert "BOUNDARY_SEMANTIC_REVIEW_NOT_PASS" in drift


    for field in (
        "source_request_sha256", "source_cue_grid_sha256", "source_review_sha256",
        "source_story_ending_comparison_sha256", "source_final_start_ms", "source_final_end_ms",
    ):
        drifted = json.loads(json.dumps(delivery))
        value = drifted["source_separation_witness"][field]
        drifted["source_separation_witness"][field] = (
            value + 1 if isinstance(value, int) else "sha256:" + "0" * 64)
        assert "BOUNDARY_SOURCE_SEPARATION_WITNESS_INVALID" in codes(drifted)

    assert "BOUNDARY_DELIVERY_COVERAGE_INVALID" in codes(
        delivery, delivery_coverage_verification={"status": "BLOCK", "failure": "missing_source"})


def test_topic_witness_source_keeps_the_legacy_witness_shape():
    reply = _reply(next_topic_separated=True, recommended_end_cue_index=3,
                   evidence_cue_indexes=[1, 2, 3, 4],
                   core_topic_complete=True, leaves_audience_hanging=False,
                   compared_end_cue_indexes=[2, 3], next_question_start_cue_index=4,
                   ending_comparison_reason="3 收束，4 起新问题。")
    review = _review(SUMMARY_CUES, target_ms=9_000, reply=reply)
    _bound, delivery = _source_and_delivery(
        SUMMARY_CUES, review, final_start_ms=0,
        delivery_reply=_legacy(dict(DELIVERY_STORY_REPLY, next_topic_separated=True)))

    assert review["status"] == "PASS" and "ending_proof_kind" not in review
    assert "ending_proof_kind" not in delivery["source_separation_witness"]
    assert delivery["status"] == "PASS" and delivery["next_topic_separated"] is True


# ------------------------------------------------- 厦大 frozen-input replay

def _xiamen(name):
    return json.loads((FIXTURES / "xiamen_boundary_reviews.json").read_text(encoding="utf-8"))[
        "cases"][name]


def _cues_of(case):
    return [_cue(i, s, e) for i, s, e in case["cues"]]


XIAMEN_HOOK = "亲戚问长大要考清华还是北大，小李却认准厦大：小时候挑大学，看的是易中天和辩论队。"


def test_xiamen_v2_first_round_good_ending_still_takes_the_retry_for_the_summary():
    """Diagnosis grid (69eb9ae v2, 30 s): 59 is already a good story, but the
    60–67 same-topic summary is better included, so the one bounded retry
    runs (BND-03 first sentence) instead of BLOCK-without-retry."""
    case = _xiamen("v2")
    reply = _reply(
        recommended_end_cue_index=59, evidence_cue_indexes=[48, 49, 50, 51, 59, 60, 61, 62],
        core_topic_complete=True, leaves_audience_hanging=False,
        compared_end_cue_indexes=[51, 59], next_question_start_cue_index=None,
        same_topic_continues_after_target=True, needs_more_context=True,
        ending_comparison_reason="51 已讲完考清北与认准厦大的核心故事，59 处不吊胃口；"
                                 "60 起是同话题的择校总结，包含后更完整，需延续。",
    )
    review = _review(_cues_of(case), target_ms=case["target_ms"], reply=reply,
                     candidate_id="semantictalk_1438010_1548390", hook=XIAMEN_HOOK)

    assert review["status"] == "BLOCK"
    assert review["retry_scope"] == "same_topic_continues"
    assert "STORY_ENDING_BETTER_CONTINUATION_PENDING_RETRY" in review["reason_codes"]


def test_xiamen_v2_grid_at_sixty_seconds_selects_summary_end_cue_67():
    case = _xiamen("v2")
    reply = _reply(
        recommended_end_cue_index=67, evidence_cue_indexes=[48, 49, 50, 51, 60, 61, 67, 68, 71],
        core_topic_complete=True, leaves_audience_hanging=False,
        compared_end_cue_indexes=[59, 67], next_question_start_cue_index=68,
        ending_comparison_reason="59 已是好故事；60–67「不管在什么样的学校都靠自己」是同话题收束总结，"
                                 "选到 67 更完整；68 起「双非是什么」是新问题，不越过。",
    )
    cues = _cues_of(case)
    review = _review(cues, target_ms=case["target_ms"], reply=reply, max_forward_ms=60_000,
                     candidate_id="semantictalk_1438010_1548390", hook=XIAMEN_HOOK)

    assert review["status"] == "PASS"
    assert review["ending_proof_kind"] == STORY_ENDING_PROOF_KIND
    assert review["recommended_end_cue_index"] == 67
    assert review["recommended_end_ms"] == 161_660
    initial = _select(review, cues, semantic_end_ms=case["target_ms"],
                      piece_end_ms=case["cues"][-1][2] + 1_000, cap_ms=60_000)
    assert initial.snapped_end == 161_660


def test_xiamen_real_sixty_second_attempt_old_reply_blocks_new_reply_ends_at_summary():
    """The real 3d77a79 second attempt (cap 60 s): its old-contract reply (cue
    74, 双非 treated as same topic) stays BLOCK; a v3 reply on the same
    frozen grid ends at the summary cue 66 before the new question 67."""
    case = _xiamen("retry60_3d77a79")
    scope = case["boundary_search_scope"]
    assert boundary_search_scope_is_valid(scope)
    cues = _cues_of(case)
    old = _review(cues, target_ms=case["target_ms"], reply=case["reply"], scope=scope,
                  max_forward_ms=60_000, candidate_id="semantictalk_1438010_1548390")
    assert old["status"] == "BLOCK" and old["recommended_end_cue_index"] == 74

    reply = _reply(
        recommended_end_cue_index=66, evidence_cue_indexes=[48, 49, 50, 51, 59, 60, 66, 67, 70],
        core_topic_complete=True, leaves_audience_hanging=False,
        compared_end_cue_indexes=[59, 66, 74], next_question_start_cue_index=67,
        ending_comparison_reason="66「自己决定一切」收住同话题总结；67 起观众新问题「双非」，"
                                 "74 越过了它。",
    )
    new = _review(cues, target_ms=case["target_ms"], reply=reply, scope=scope,
                  max_forward_ms=60_000, candidate_id="semantictalk_1438010_1548390",
                  hook=XIAMEN_HOOK)
    assert new["status"] == "PASS"
    assert new["recommended_end_ms"] == 161_660

    crossing = _review(cues, target_ms=case["target_ms"], scope=scope, max_forward_ms=60_000,
                       candidate_id="semantictalk_1438010_1548390",
                       reply=dict(reply, recommended_end_cue_index=74,
                                  evidence_cue_indexes=[48, 66, 67, 74]))
    assert crossing["status"] == "BLOCK"
    assert "BOUNDARY_ENDING_CROSSES_NEXT_QUESTION" in crossing["reason_codes"]


# ------------------------------------------------- unseen class members

def _member(name):
    case = json.loads((FIXTURES / "story_ending_class_members.json").read_text(encoding="utf-8"))[
        "cases"][name]
    scope = case["boundary_search_scope"]
    assert boundary_search_scope_is_valid(scope)
    return case, scope, _cues_of(case)


def _rescope(scope, cap_ms):
    return build_boundary_search_scope(
        semantic_target_ms=scope["semantic_target_ms"], repair_cap_ms=cap_ms,
        manual_lower_bound_ms=scope["manual_lower_bound_ms"],
        structured_payoff_ms=scope["structured_payoff_ms"],
        required_owner_end_ms=scope["required_owner_end_ms"],
        last_piece_start_ms=scope["last_piece_start_ms"],
        prior_piece_duration_ms=scope["prior_piece_duration_ms"],
        witness_reserve_ms=scope["witness_reserve_ms"],
        boundary_end_mode=scope["boundary_end_mode"],
        published_recall_anchor_ms=scope.get("published_recall_anchor_ms"),
        semantic_tail_trim_cap_ms=scope["semantic_tail_trim_cap_ms"],
    )


@pytest.mark.parametrize("name, recommended, compared", [
    # End of stream: 83 is the last cue ever spoken; no witness can exist.
    ("semantictalk_712160_967140", 83, [83]),
    # The grid ends on the closing remark of the 外群主 digression.
    ("talk_20260925_461260_599910_conservative", 65, [64, 65]),
])
def test_unseen_witnessless_story_endings_pass_and_reach_delivery(name, recommended, compared):
    case, scope, cues = _member(name)
    old = _review(cues, target_ms=case["target_ms"], reply=case["reply"], scope=scope,
                  max_forward_ms=case["max_forward_ms"], candidate_id=name)
    assert old["status"] == "BLOCK"

    reply = _reply(
        recommended_end_cue_index=recommended,
        evidence_cue_indexes=list(range(recommended - 3, recommended + 1)),
        core_topic_complete=True, leaves_audience_hanging=False,
        compared_end_cue_indexes=compared, next_question_start_cue_index=None,
        ending_comparison_reason="核心话题在此说完，之后没有可见延续，不吊胃口。",
    )
    new = _review(cues, target_ms=case["target_ms"], reply=reply, scope=scope,
                  max_forward_ms=case["max_forward_ms"], candidate_id=name)
    assert new["status"] == "PASS" and new["ending_proof_kind"] == STORY_ENDING_PROOF_KIND

    start = case["cues"][recommended - 40][1]
    bound, delivery = _source_and_delivery(cues, new, final_start_ms=start,
                                           delivery_reply=DELIVERY_STORY_REPLY)
    assert delivery["status"] == "PASS"
    assert semantic_boundary_review_is_valid(bound, expected_scope="source_full_window")
    assert semantic_boundary_review_is_valid(delivery, expected_scope="final_delivery")


def test_unseen_closing_quip_past_window_retries_then_ends_before_new_topic():
    """auto_222823: 97 is a good story, the 98–99 closing quip of the same
    bit crosses the 30 s window → retry; at 60 s it ends at 99, before the
    new 尼古喵喵 remark (100)."""
    case, scope, cues = _member("auto_222823_1076_1325")
    first = _review(cues, target_ms=case["target_ms"], scope=scope, max_forward_ms=30_000,
                    candidate_id="auto_222823_1076_1325", reply=_reply(
                        recommended_end_cue_index=97, evidence_cue_indexes=[91, 92, 97, 98],
                        core_topic_complete=True, leaves_audience_hanging=False,
                        compared_end_cue_indexes=[92, 97], next_question_start_cue_index=None,
                        same_topic_continues_after_target=True, needs_more_context=True,
                        ending_comparison_reason="97 已是好故事，98–99 是同一梗的收尾回应，包含更完整。"))
    assert first["status"] == "BLOCK" and first["retry_scope"] == "same_topic_continues"

    wide = _rescope(scope, 60_000)
    second = _review(cues, target_ms=wide["review_target_ms"], scope=wide, max_forward_ms=60_000,
                     candidate_id="auto_222823_1076_1325", reply=_reply(
                         recommended_end_cue_index=99, evidence_cue_indexes=[91, 97, 98, 99, 100],
                         core_topic_complete=True, leaves_audience_hanging=False,
                         compared_end_cue_indexes=[97, 99], next_question_start_cue_index=100,
                         ending_comparison_reason="99 收住大果/寄礼物的梗；100 起是新话题。"))
    assert second["status"] == "PASS" and second["recommended_end_ms"] == 292_740


@pytest.mark.parametrize("name", ["auto_213023_57_333", "auto_133040_1735_1804"])
def test_unseen_hanging_members_stay_blocked(name):
    """auto_213023: the age question (114) is answered only after the window;
    auto_133040: the promised failure payoff never lands in the window."""
    case, scope, cues = _member(name)
    recommended = case["reply"]["recommended_end_cue_index"] or case["cues"][-1][0]
    eligible = [i for i, _s, e in case["cues"]
                if scope["minimum_recommended_end_ms"] <= e <= scope["max_recommended_end_ms"]]
    recommended = recommended if recommended in eligible else eligible[-1]
    review = _review(cues, target_ms=case["target_ms"], scope=scope,
                     max_forward_ms=case["max_forward_ms"], candidate_id=name, reply=_reply(
                         story_closed=False, recommended_end_cue_index=recommended,
                         evidence_cue_indexes=[recommended],
                         core_topic_complete=False, leaves_audience_hanging=True,
                         compared_end_cue_indexes=[recommended], next_question_start_cue_index=None,
                         ending_comparison_reason="承诺的回答/包袱尚未落地，吊人胃口。"))
    assert review["status"] == "BLOCK"
    assert "STORY_ENDING_LEAVES_AUDIENCE_HANGING" in review["reason_codes"]
