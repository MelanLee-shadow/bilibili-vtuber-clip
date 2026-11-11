"""CPA editorial nominations and decisions reach the ordinary subtitle writer."""
from __future__ import annotations

import json

import pytest
from fakes import srt
from test_task32_drop_cue import _Cpa, _ctx, _doubt, _dropped_record, _final_srt

from src.autoslice.manual_lane import doubt_policy as dp
from src.autoslice.manual_lane import stage_impl as si


class EditorialCpa(_Cpa):
    """Synthetic adapter: nominate an editorial choice, then supply a scripted decision."""
    def ask(self, *, call_id, prompt_text, model, effort, remote_dir):
        if call_id == "review-c":
            self.calls.append(call_id)
            self.prompts.append(prompt_text)
            text = json.dumps({"suspicious": [{"cue_id": 2, "reason": "这句与当前故事无关，需编辑取舍"}]},
                              ensure_ascii=False)
            return {"call_id": call_id, "model": model, "effort": effort, "rc": 0, "elapsed_s": 0.1,
                    "response_text": text, "stderr_tail": ""}
        return super().ask(call_id=call_id, prompt_text=prompt_text, model=model, effort=effort,
                           remote_dir=remote_dir)


@pytest.mark.parametrize("spoken,state", [("稍后我们聊别的事情", "ADOPT"), ("谢谢谢谢", "FIT_CONTEXT"), ("好", "ADOPT")])
def test_editorial_nomination_and_cpa_drop_reach_the_normal_writer(tmp_path, monkeypatch, spoken, state):
    text = srt(3, step=2.0).replace("句子2", spoken)
    answer = {"state": state, "final_text": spoken, "drop": True, "confidence": "高",
              "affects_meaning": False, "reason": "与当前故事无关，字幕不呈现"}
    ctx = _ctx(tmp_path, monkeypatch, text, answer)
    ctx.adapters.cpa = EditorialCpa(answer)
    outcome = si.step3_subtitles(ctx)
    assert outcome.ok, (outcome.reason, outcome.note)
    review = ctx.adapters.cpa.prompts[ctx.adapters.cpa.calls.index("review-c")]
    assert "字幕是否需要呈现" in review and "例子包括但不限于" in review
    assert "不要标：语气词本身、笑声、清楚通顺的句子" not in review
    doubt = _doubt(ctx, "d002")
    assert any("CPA 审读：这句与当前故事无关" in reason for reason in doubt["reasons"])
    assert all("BCUT 与剪映不一致" not in reason for reason in doubt["reasons"])
    assert doubt["state"] == state and doubt["dropped"] is True and doubt["final_text"] == ""
    assert doubt["drop_said"] == spoken and not doubt["drop_refused_written"] and not doubt["drop_routed"]
    assert spoken not in _final_srt(ctx)
    record = _dropped_record(ctx)
    assert len(record) == 1 and record[0]["text"] == spoken and record[0]["said"] == spoken
    assert record[0]["reason"] == answer["reason"]
    assert any("CPA 判定不出字幕" in line and spoken in line for line in outcome.process)


def test_adjudication_prompt_describes_editorial_choice_without_lexical_drop_veto():
    prompt = si._render("subtitle_adjudicate.md")
    assert "字幕留删由你结合整片故事" in prompt
    assert "故事铺垫、交流、口误和有意义的重复" in prompt
    assert "不要为了消除疑点、缩短字幕或提高分数而删掉不确定的内容" in prompt
    assert "不剪掉媒体区间" in prompt
    assert "drop` 只用于这三种情况" not in prompt and "写 drop 会在下游被拒" not in prompt


def test_low_confidence_meaning_affecting_drop_remains_held():
    row = {"state": "ADOPT", "final_text": "可能有意义的句子", "drop": True,
           "confidence": "低", "affects_meaning": True}
    decided = dp.decide(row, "可能有意义的句子", maintained=False)
    assert decided["dropped"] is True and decided["affects_meaning"] is True
    assert dp.is_hold(decided)


def test_low_confidence_drop_without_meaning_judgment_remains_held():
    row = {"state": "ADOPT", "final_text": "尚未判断是否重要的内容", "drop": True,
           "confidence": "低"}
    assert dp.is_hold(dp.decide(row, "尚未判断是否重要的内容", maintained=False))


def test_low_confidence_drop_remains_visible_to_the_ordinary_hold(tmp_path, monkeypatch):
    spoken = "不能确定是否有关的重要信息"
    text = srt(3, step=2.0).replace("句子2", spoken)
    answer = {"state": "ADOPT", "final_text": spoken, "drop": True, "confidence": "低",
              "affects_meaning": True, "reason": "暂时考虑省略，但意思尚不确定"}
    ctx = _ctx(tmp_path, monkeypatch, text, answer)
    ctx.adapters.cpa = EditorialCpa(answer)
    outcome = si.step3_subtitles(ctx)
    assert outcome.ok, (outcome.reason, outcome.note)  # the existing hold is enforced at package seal
    doubt = _doubt(ctx, "d002")
    assert doubt["dropped"] is True and doubt["confidence"] == "低" and doubt["affects_meaning"] is True
    assert dp.is_hold(doubt)
    assert any("定不下且影响意思 1 句" in line and "d002" in line for line in outcome.process)


def test_non_drop_keeps_text_and_illegal_state_remains_failure(tmp_path, monkeypatch):
    spoken = "保持原句的完整意思"
    text = srt(3, step=2.0).replace("句子2", spoken)
    keep = {"state": "KEEP", "final_text": spoken, "drop": False, "confidence": "高", "affects_meaning": False}
    ctx = _ctx(tmp_path / "keep", monkeypatch, text, keep)
    ctx.adapters.cpa = EditorialCpa(keep)
    assert si.step3_subtitles(ctx).ok
    assert spoken in _final_srt(ctx) and not _doubt(ctx, "d002")["dropped"]
    illegal = {"state": "INVALID", "final_text": spoken, "drop": True, "confidence": "高", "affects_meaning": True}
    bad = _ctx(tmp_path / "bad", monkeypatch, text, illegal)
    bad.adapters.cpa = EditorialCpa(illegal)
    outcome = si.step3_subtitles(bad)
    assert not outcome.ok and outcome.reason == "DOUBT_UNRESOLVED"
    assert not _doubt(bad, "d002")["dropped"]


def test_editorial_review_cannot_override_reviewer_protected_anchor(tmp_path, monkeypatch):
    spoken = "用户已经确认的句子"
    text = srt(3, step=2.0).replace("句子2", spoken)
    answer = {"state": "ADOPT", "final_text": spoken, "drop": True, "confidence": "高", "affects_meaning": False}
    ctx = _ctx(tmp_path, monkeypatch, text, answer)
    ctx.adapters.cpa = EditorialCpa(answer)
    label = {"cid": "clip", "doubt_id": "d002", "abs_start_ms": 12000, "abs_end_ms": 14000,
             "current": spoken, "text": "保护的准确句子", "choice": "custom", "note": ""}
    ctx.state.data["reviewer_labels"] = {"export_sha256": "f" * 64, "labels": [label]}
    outcome = si.step3_subtitles(ctx)
    assert outcome.ok, (outcome.reason, outcome.note)
    assert "保护的准确句子" in _final_srt(ctx)
    assert not any('"d002"' in prompt for call, prompt in zip(ctx.adapters.cpa.calls, ctx.adapters.cpa.prompts)
                   if call.startswith("adjudicate-"))
    assert not _dropped_record(ctx)
