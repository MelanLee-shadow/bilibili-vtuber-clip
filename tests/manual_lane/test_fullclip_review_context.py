"""The ordinary review-c call receives the complete, independent clip evidence."""

from __future__ import annotations

from copy import deepcopy
import json

import pytest
from fakes import FakeRemote, make_ctx

from src.autoslice.manual_lane import stage_impl as si
from src.autoslice.manual_lane import variant_c


BCUT_HEADER = "【BCUT 全片字幕：唯一可提名的编号】\n"
JY_HEADER = "【剪映独立 ASR 全片字幕：编号仅供参考，不能提名】\n"
DANMAKU_HEADER = "【弹幕（剪辑时间线 t_s / text）】\n"
CLUE = "玻璃月台的故事"
REVIEW_REASON = "两个来源提供可核查线索"


def _evidence():
    bcut = [
        {"id": 7, "start_s": 0.0, "end_s": 1.5, "text": "今天说说那件事 {strict_rules}"},
        {"id": 29, "start_s": 2.0, "end_s": 4.0, "text": "后来那位朋友来了"},
    ]
    jianying = [
        {"id": 701, "start_s": 0.0, "end_s": 1.5, "text": bcut[0]["text"]},
        {"id": 702, "start_s": 2.0, "end_s": 4.0, "text": bcut[1]["text"]},
        {"id": 901, "start_s": 6.0, "end_s": 7.0, "text": f"提到{CLUE}"},
    ]
    danmaku = [
        {"t_s": -3.25, "text": "前面的普通评论"},
        {"t_s": 7.25, "text": f"{CLUE}？ {{pronoun_rule}}\n另一行，含引号\""},
    ]
    return bcut, jianying, danmaku


def _detect(tmp_path, monkeypatch, bcut, jianying, danmaku):
    ctx = make_ctx(tmp_path, remote=FakeRemote(), cpa=None)
    calls = []
    records = []
    monkeypatch.setattr(si, "_asr_witnesses", lambda _ctx: ({}, {}))
    monkeypatch.setattr(si, "_danmaku_lines", lambda _ctx, _cut_len: danmaku)
    monkeypatch.setattr(si, "_speaker_chunks", lambda _ctx, _cues: ({}, ""))

    def fake_judge(_ctx, call_id, prompt, *, purpose):
        calls.append((call_id, purpose, prompt))
        jy = prompt.partition(JY_HEADER)[2].partition(DANMAKU_HEADER)[0]
        _, separator, raw_comments = prompt.partition(DANMAKU_HEADER)
        comments = json.loads(raw_comments) if separator else []
        found = CLUE in jy and any(CLUE in row["text"] for row in comments)
        rows = ([{"cue_id": 29, "reason": REVIEW_REASON},
                 {"cue_id": 901, "reason": "独立编号不能成为目标"}] if found else [])
        return {"call_id": call_id, "rc": 0,
                "response_text": json.dumps({"suspicious": rows}, ensure_ascii=False), "file": "fake"}

    monkeypatch.setattr(si, "_ask_cpa", fake_judge)
    det = variant_c._detect(ctx, si, bcut, jianying, 10.0, {}, records, [])
    assert isinstance(det, variant_c._Detection)
    assert [(call, purpose) for call, purpose, _ in calls] == [("review-c", "review-c")]
    assert len(records) == 1 and records[0]["call_id"] == "review-c"
    assert ctx.adapters.remote.scripts == [] and ctx.adapters.danmaku.calls == []
    return det, calls[0][2]


def _sections(prompt):
    bcut = prompt.split(BCUT_HEADER, 1)[1].split(JY_HEADER, 1)[0].rstrip("\n")
    jy = prompt.split(JY_HEADER, 1)[1].split(DANMAKU_HEADER, 1)[0].rstrip("\n")
    comments = json.loads(prompt.split(DANMAKU_HEADER, 1)[1])
    return bcut, jy, comments


def test_detect_gives_review_all_independent_evidence_and_merges_only_bcut_ids(tmp_path, monkeypatch):
    bcut, jianying, danmaku = _evidence()
    original = deepcopy((bcut, jianying, danmaku))
    det, prompt = _detect(tmp_path, monkeypatch, bcut, jianying, danmaku)

    assert "CPA 审读：" + REVIEW_REASON in det.reasons.get(29, [])
    assert 901 not in det.reasons and set(det.reasons) <= {7, 29}
    assert _sections(prompt) == (si._context_lines(bcut), si._context_lines(jianying), danmaku)
    assert CLUE not in si._context_lines(bcut)
    assert (bcut, jianying, danmaku) == original


@pytest.mark.parametrize("with_jianying,with_danmaku", [(False, False), (True, False), (False, True)])
def test_detect_keeps_missing_auxiliary_inputs_empty(tmp_path, monkeypatch, with_jianying, with_danmaku):
    bcut, jianying, danmaku = _evidence()
    jianying = jianying if with_jianying else []
    danmaku = danmaku if with_danmaku else []
    det, prompt = _detect(tmp_path, monkeypatch, bcut, jianying, danmaku)

    assert all(not reason.startswith("CPA 审读：") for reasons in det.reasons.values() for reason in reasons)
    assert _sections(prompt) == (si._context_lines(bcut), si._context_lines(jianying), danmaku)
