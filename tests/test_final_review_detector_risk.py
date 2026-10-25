import json

import pytest

from src.autoslice.final_review_auditor import FinalReviewAuditError, audit_final_subtitles
from src.autoslice.final_review_contract import is_noncritical_disclosed


def _srt(text, *, named_gift=False, factual_conflict=False):
    result = "1\n00:00:00,000 --> 00:00:01,180\n" + text + "\n"
    if named_gift:
        context = ("刚才的灯牌是小红送的，小明还没送" if factual_conflict else
                   "小明，你名字也写作晓明对吧，就是你刚送的灯牌")
        result += "\n2\n00:00:01,200 --> 00:00:03,000\n" + context + "\n"
    return result


def _gift_equivalence():
    return {
        "status": "EQUIVALENT", "current_meaning": "感谢小明送来的灯牌。",
        "alternative_meaning": "名字写作晓明时仍是感谢同一人送来的灯牌。",
        "shared_meaning": "感谢刚送灯牌的同一名观众。", "uncertain_detail": "观众名字明前一字的写法。",
        "context_basis": "第2句将小明和晓明明确绑定为同一观众，且明确此人刚送灯牌。",
        "evidence_cue_ids": [1, 2],
    }


def _risk_fields(risk):
    return {
        "content_risk": risk,
        "risk_basis": "邻句明确灯牌来自小红且小明还没送，当前句感谢小明改变了送礼者。" if risk == "factual_error" else
            "邻句确认小明和晓明是同一送灯牌者，字形疑点不改变整句所指和谢礼含义。",
        "meaning_equivalence": _gift_equivalence() if risk == "noncritical_uncertainty" else None,
    }


@pytest.mark.parametrize("risk", ["noncritical_uncertainty", "factual_error"])
def test_omitted_gift_detector_gets_current_risk_without_edit_authority(risk):
    calls = []

    def reviewer(prompt):
        calls.append(prompt)
        if len(calls) == 1:
            return '{"findings":[]}'
        assert "谢谢小明的灯牌" in prompt
        assert "整句含义未知也不能改写为已确认谢礼" in prompt
        return json.dumps({"findings": [{"cue": 1, **_risk_fields(risk)}]})

    text = _srt("谢谢小明的灯牌", named_gift=True, factual_conflict=risk == "factual_error")
    row, = audit_final_subtitles(text, llm_call=reviewer, extract_json=json.loads)
    assert len(calls) == 2
    assert row["content_risk"] == risk
    assert row["current_content_risk"]["decision_authority"] == "CURRENT_FINAL_REVIEW"
    assert row["proposed_full_cue"] is None
    assert "exact_release_adjudication" not in row
    assert is_noncritical_disclosed(row) is (risk == "noncritical_uncertainty")
    assert text == _srt("谢谢小明的灯牌", named_gift=True, factual_conflict=risk == "factual_error")


@pytest.mark.parametrize("risk", ["noncritical_uncertainty", "factual_error"])
def test_already_classified_gift_keeps_current_judgment_without_another_call(risk):
    calls = []

    def reviewer(prompt):
        calls.append(prompt)
        return json.dumps({"findings": [{"cue": 1, "kind": "entity",
                            "repair_class": "editorial_rendering", "suspect": "小明",
                            **_risk_fields(risk),
                            "proposed_full_cue": None, "why": "当前名字疑点"}]})

    rows = audit_final_subtitles(_srt("谢谢小明的灯牌", named_gift=True, factual_conflict=risk == "factual_error"), llm_call=reviewer,
                                extract_json=json.loads)
    assert len(calls) == 1
    assert all(row["content_risk"] == risk for row in rows)


@pytest.mark.parametrize("text", ["谢谢你的礼物", "谢谢礼物", "谢谢大家"])
def test_generic_thanks_does_not_create_a_name_question_or_extra_risk_call(text):
    calls = []
    assert audit_final_subtitles(_srt(text), llm_call=lambda p: calls.append(p) or
                                 '{"findings":[]}', extract_json=json.loads) == []
    assert len(calls) == 1


def test_missing_detector_risk_decision_stays_blocked():
    with pytest.raises(FinalReviewAuditError, match="FINAL_REVIEW_PRIORITY_RISK_INCOMPLETE"):
        audit_final_subtitles(_srt("谢谢小明的灯牌"), llm_call=lambda _: '{"findings":[]}',
                              extract_json=json.loads)
