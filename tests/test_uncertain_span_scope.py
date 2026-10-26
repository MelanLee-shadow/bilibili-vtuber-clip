"""Selected word answers and requested audio are independent retry signals.

An explicit CURRENT/PROPOSED answer is selected even when another audio
question remains. NEITHER stays unresolved. This retry classification grants
no mutation or release authority; target localization is checked separately.
No provider is called; these are protocol-shaped strings only.
"""
import json

from src.autoslice.semantic_retry_progress import _uncertain


def _prompt(span=(4, 6), binding_sha="b" * 64):
    block = {"binding": {"binding_sha256": binding_sha, "codepoint_span": list(span)},
             "observations": [], "invalid_observations": [], "request_sha256": "r" * 64}
    return ("## 字幕选字裁决\n\nSPAN_REVIEW_INPUT\n" + json.dumps(block, sort_keys=True)
            + "\nEND_SPAN_REVIEW_INPUT\n")


def _answer(*, questions, target_needs_audio=False, choice="PROPOSED", binding_sha="b" * 64):
    return json.dumps({"choice": choice, "needs_audio": True, "reason": "句尾另有疑点",
                       "span_review": {"binding_sha256": binding_sha, "request_sha256": "r" * 64,
                                       "all_audio_questions_located": True,
                                       "target_needs_audio": target_needs_audio,
                                       "audio_questions": [{"codepoint_span": q, "reason": "x",
                                                            "observation_ids": ["o"]} for q in questions]}})


def test_outside_question_does_not_make_the_target_unresolved():
    assert _uncertain(_answer(questions=[[7, 8]]), _prompt()) is False


def test_unselected_answers_stay_unresolved_and_selected_answers_do_not():
    assert _uncertain(_answer(questions=[[4, 5]], choice="NEITHER"), _prompt()) is True           # inside the target
    assert _uncertain(_answer(questions=[[3, 5]], choice="NEITHER"), _prompt()) is True           # crosses the boundary
    assert _uncertain(_answer(questions=[[7, 8]], target_needs_audio=True), _prompt()) is False
    assert _uncertain(_answer(questions=[[7, 8]], choice="NEITHER"), _prompt()) is True
    assert _uncertain(_answer(questions=[[7, 8]], binding_sha="c" * 64), _prompt()) is False
    assert _uncertain(_answer(questions=[[7, 8]])) is False                      # no bound request
