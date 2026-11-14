"""Ordinary C detection carries acquired full-clip sources to the final consumer."""

from __future__ import annotations

import json

import pytest
from fakes import FakeRemote, IdCpa, make_ctx

from src.autoslice.manual_lane import stage_impl as si
from src.autoslice.manual_lane import lane_lines, step3_adjudicate, variant_c
from src.autoslice.manual_lane.runner import StepOutcome


@pytest.mark.parametrize("target,far", [(1, 13), (2, 21)])
def test_detect_to_adjudicator_carries_full_primary_sources(tmp_path, monkeypatch, target, far):
    bcut = [{"id": target, "start_s": 0.0, "end_s": 2.0, "text": "本句原稿"},
            {"id": far, "start_s": 40.0, "end_s": 42.0, "text": "远处原稿"}]
    jy = [{"id": 901, "start_s": 0.0, "end_s": 2.0, "text": "本句剪映"},
          {"id": 997, "start_s": 40.0, "end_s": 42.0, "text": f"远处剪映{far}"}]
    comments = [{"t_s": 39.5, "text": f"远处弹幕{far}"}]
    whole = {"asr:mai": [{"abs_start_s": 40.1, "end_s": 41.7, "text": f"远处整片识别{far}",
                           "speaker": "A"}]}
    heard = {"audio:38m@P0": [{"abs_start_s": 40.4, "text": f"远处已听{far}", "owners": [far]}]}
    ctx = make_ctx(tmp_path, remote=FakeRemote(), cpa=IdCpa())
    prompts = {}

    monkeypatch.setattr(si, "_danmaku_lines", lambda *_: comments)
    monkeypatch.setattr(si, "_asr_witnesses", lambda *_: (whole, {"mai": {"status": "OK", "segments": 1},
                                                                  "moss": {"status": "UNAVAILABLE", "reason_code": "OFF"}}))
    monkeypatch.setattr(si, "_speaker_chunks", lambda *_: ({}, ""))
    def review(_ctx, call_id, prompt, **_kwargs):
        prompts[call_id] = prompt
        return {"call_id": call_id, "rc": 0, "response_text": json.dumps({"suspicious": []}), "file": "fake"}
    monkeypatch.setattr(si, "_ask_cpa", review)
    monkeypatch.setattr(si, "_attach_work_lookups", lambda *_: [])
    def adjudicate(_ctx, call_id, prompt, **_kwargs):
        prompts[call_id] = prompt
        doubts = json.loads(prompt.split("输入疑点（含候选读法及其支持来源、各来源读法与弹幕线索）：\n", 1)[1]
                            .split("\n\n全片字幕（序号 时间 文字）：\n", 1)[0])
        return {"call_id": call_id, "rc": 0, "response_text": json.dumps([
            {"id": d["id"], "state": "KEEP", "final_text": d["current"], "confidence": "高",
             "affects_meaning": False} for d in doubts], ensure_ascii=False), "file": "fake"}
    monkeypatch.setattr(step3_adjudicate, "_ask_cpa", adjudicate)

    det = variant_c._detect(ctx, si, bcut, jy, 50.0, {}, [], [])
    assert not isinstance(det, StepOutcome)
    esc = variant_c._Escalation(pending={target}, lines_by_source={**whole, **heard})
    assert variant_c._settle_level(ctx, si, det, esc, "L1", "test", bcut, ctx.state.data["clip"], [],
                                   batch={target}) is None
    final_prompt = prompts["adjudicate-c-L1-1"]
    doubts = json.loads(final_prompt.split("输入疑点（含候选读法及其支持来源、各来源读法与弹幕线索）：\n", 1)[1]
                        .split("\n\n全片字幕（序号 时间 文字）：\n", 1)[0])
    assert len(doubts) == 1 and doubts[0]["cue_id"] == target
    assert not any("远处" in text for text in doubts[0]["readings"].values())
    final_context = final_prompt.split("\n\n全片字幕（序号 时间 文字）：\n", 1)[1]
    assert f"997 " in final_context and f"远处剪映{far}" in final_context
    assert json.dumps(comments, ensure_ascii=False, separators=(",", ":")) in final_context
    assert f"远处整片识别{far}" in final_context and f"远处已听{far}" in final_context
    assert f'"owners":[{far}]' in final_context
    # Initial review remains the exact pre-change composition, independently of the final consumer.
    expected = (si._render("subtitle_review.md", participants=si._participant_line(ctx),
                           confusions=si._confusion_lines(ctx)).rstrip()
                + "\n\n【剪映有效分句提案】\n"
                + json.dumps(lane_lines.effective_split_proposals(bcut, det.lines), ensure_ascii=False,
                             separators=(",", ":"))
                + "\n\n【BCUT 全片字幕：唯一可提名的编号】\n" + si._context_lines(bcut)
                + "\n\n【剪映独立 ASR 全片字幕：编号仅供参考，不能提名】\n" + si._context_lines(jy)
                + "\n\n【弹幕（剪辑时间线 t_s / text）】\n"
                + json.dumps(comments, ensure_ascii=False, separators=(",", ":")))
    assert prompts["review-c"] == expected


def test_primary_snapshot_preserves_colliding_sources_and_never_invents_missing_observations():
    whole = {"asr:mai": [{"abs_start_s": 1.0, "end_s": 2.0, "text": "整片读法", "speaker": "A",
                           "owners": [1], "parts": {1: "整片读法"}}]}
    acquired = {"asr:mai": [{"abs_start_s": 3.0, "text": "不同观察", "owners": [2]}],
                "asr:moss@win": [], "audio:38m@P0": []}
    context = variant_c._primary_witness_context(whole, acquired)
    assert "整片读法" in context and "不同观察" in context
    assert context.count('"asr:mai"') == 2
    assert '"asr:moss@win":[]' in context and '"audio:38m@P0":[]' in context
    assert "没有结果，不代表成功听到" in context
    acquired["asr:mai"][0]["text"] = "之后改变"
    assert "之后改变" not in context
