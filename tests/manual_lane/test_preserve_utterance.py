"""Review-c can decline a proposed cut without changing the ordinary subtitle writer."""

from __future__ import annotations

from copy import deepcopy
import json

import pytest
from fakes import FakeRemote, make_ctx

from src.autoslice.manual_lane import stage_impl as si
from src.autoslice.manual_lane import variant_c


PROPOSALS = "【剪映有效分句提案】\n"
BCUT = "【BCUT 全片字幕：唯一可提名的编号】\n"
JY = "【剪映独立 ASR 全片字幕：编号仅供参考，不能提名】\n"
COMMENTS = "【弹幕（剪辑时间线 t_s / text）】\n"


# Independent synthetic inputs. The second uses different IDs, absolute times and three JY pieces.
MEMBERS = [
    (
        [{"id": 17, "start_s": 1.0, "end_s": 3.0, "text": "我把钥匙放在桌子上"},
         {"id": 35, "start_s": 4.0, "end_s": 7.0, "text": "外面下雨我们改天去"},
         {"id": 47, "start_s": 8.0, "end_s": 9.0, "text": "这样也好"}],
        [{"id": 101, "start_s": 1.0, "end_s": 1.7, "text": "我把钥匙"},
         {"id": 102, "start_s": 1.7, "end_s": 3.0, "text": "放在桌子上"},
         {"id": 103, "start_s": 4.0, "end_s": 5.3, "text": "外面下雨"},
         {"id": 104, "start_s": 5.3, "end_s": 7.0, "text": "我们改天去"},
         {"id": 105, "start_s": 8.0, "end_s": 9.0, "text": "这样也好"},
         {"id": 106, "start_s": 12.0, "end_s": 13.5, "text": "小猫睡着了"}],
    ),
    (
        [{"id": 208, "start_s": 11.25, "end_s": 14.25, "text": "她把刚买来的书放进柜子"},
         {"id": 462, "start_s": 16.0, "end_s": 19.0, "text": "天色暗了我们先回家"},
         {"id": 603, "start_s": 20.0, "end_s": 21.0, "text": "明天见面"}],
        [{"id": 701, "start_s": 11.25, "end_s": 12.2, "text": "她把刚买"},
         {"id": 702, "start_s": 12.2, "end_s": 13.0, "text": "来的书"},
         {"id": 703, "start_s": 13.0, "end_s": 14.25, "text": "放进柜子"},
         {"id": 704, "start_s": 16.0, "end_s": 17.4, "text": "天色暗了"},
         {"id": 705, "start_s": 17.4, "end_s": 19.0, "text": "我们先回家"},
         {"id": 706, "start_s": 20.0, "end_s": 21.0, "text": "明天见面"},
         {"id": 707, "start_s": 24.0, "end_s": 25.5, "text": "杯子掉地上"}],
    ),
]


def _detect(tmp_path, monkeypatch, bcut, jianying, response):
    danmaku = [{"t_s": -2.0, "text": "上下文 {不执行}"}]
    original = deepcopy((bcut, jianying, danmaku))
    ctx = make_ctx(tmp_path, remote=FakeRemote(), cpa=None)
    calls, records, process, chunk_inputs = [], [], [], []
    monkeypatch.setattr(si, "_asr_witnesses", lambda _ctx: ({}, {}))
    monkeypatch.setattr(si, "_danmaku_lines", lambda _ctx, _cut_len: danmaku)

    def chunks(_ctx, cues):
        chunk_inputs.append(deepcopy(cues))
        return {}, ""

    def review(_ctx, call_id, prompt, *, purpose):
        calls.append((call_id, purpose, prompt))
        return {"call_id": call_id, "rc": 0,
                "response_text": json.dumps(response, ensure_ascii=False), "file": "fake-review"}

    monkeypatch.setattr(si, "_speaker_chunks", chunks)
    monkeypatch.setattr(si, "_ask_cpa", review)
    det = variant_c._detect(ctx, si, bcut, jianying, 30.0, {}, records, process)
    assert [(call, purpose) for call, purpose, _ in calls] == [("review-c", "review-c")]
    assert len(records) == 1
    assert ctx.adapters.remote.scripts == [] and ctx.adapters.danmaku.calls == []
    assert (bcut, jianying, danmaku) == original
    return det, calls[0][2], process, chunk_inputs[0], danmaku


def _written(bcut, det, *, changed_text=None, other=False):
    doubts = [{"cue_id": cue["id"], "state": "KEEP", "final_text": cue["text"]} for cue in bcut]
    if changed_text is not None:
        doubts[0].update(state="ADOPT", final_text=changed_text)
    if other:
        doubts[1].update(other_speaker_text="确实如此", other_span_s=[bcut[1]["start_s"] + 1, bcut[1]["end_s"]])
    return si._final_cues(bcut, doubts, lines=det.lines)[0]


def _span(cues, cue):
    return [row for row in cues if cue["start_s"] <= row["start_s"] < cue["end_s"]]


@pytest.mark.parametrize("bcut,jianying", MEMBERS, ids=["synthetic-witness", "unseen-member"])
def test_review_veto_reaches_the_ordinary_final_writer(tmp_path, monkeypatch, bcut, jianying):
    target, natural, unplanned = bcut
    veto = {"suspicious": [], "keep_together": [{"cue_id": target["id"], "reason": "这里的词语相连"},
                                                  {"cue_id": unplanned["id"], "reason": "没有提案"},
                                                  {"cue_id": 999999, "reason": "不是本片句子"}]}
    det, prompt, process, chunks, comments = _detect(tmp_path, monkeypatch, bcut, jianying, veto)
    assert isinstance(det, variant_c._Detection)
    final = _written(bcut, det)
    # The first assertions are the old-generator guard: same upstream inputs and real final writer.
    target_lines = _span(final, target)
    assert [(c["start_s"], c["end_s"], c["text"]) for c in target_lines] == [
        (target["start_s"], target["end_s"], target["text"])]
    natural_lines = _span(final, natural)
    assert len(natural_lines) == 2
    assert "".join(c["text"] for c in natural_lines) == natural["text"]
    assert (natural_lines[0]["start_s"], natural_lines[-1]["end_s"]) == (natural["start_s"], natural["end_s"])
    assert [c["text"] for c in final if c["text"] == jianying[-1]["text"]] == [jianying[-1]["text"]]
    assert any(c["id"] == 100000 for c in chunks)
    assert any(f"{target['id']}" in line and "这里的词语相连" in line for line in process)
    assert not any("没有提案" in line or "不是本片句子" in line for line in process)
    proposals = json.loads(prompt.split(PROPOSALS, 1)[1].split(BCUT, 1)[0])
    assert {row["cue_id"] for row in proposals} == {target["id"], natural["id"]}
    proposal = next(row for row in proposals if row["cue_id"] == target["id"])
    assert (proposal["source_text"], proposal["start_s"], proposal["end_s"]) == (
        target["text"], target["start_s"], target["end_s"])
    assert "".join(piece["text"] for piece in proposal["parts"]) == target["text"]
    assert (proposal["parts"][0]["start_s"], proposal["parts"][-1]["end_s"]) == (
        target["start_s"], target["end_s"])
    assert prompt.split(BCUT, 1)[1].split(JY, 1)[0].rstrip("\n") == si._context_lines(bcut)
    assert prompt.split(JY, 1)[1].split(COMMENTS, 1)[0].rstrip("\n") == si._context_lines(jianying)
    assert json.loads(prompt.split(COMMENTS, 1)[1]) == comments
    control, _, _, control_chunks, _ = _detect(tmp_path / "control", monkeypatch, bcut, jianying,
                                                {"suspicious": []})
    assert isinstance(control, variant_c._Detection)
    assert det.reasons == control.reasons and chunks == control_chunks
    assert _span(_written(bcut, det, other=True), natural) == _span(_written(bcut, control, other=True), natural)
    # Later text adjudication remains the writer's authority; veto does not substitute source text.
    revised = target["text"].replace("钥匙", "纸条").replace("柜子", "抽屉")
    final_revised = _span(_written(bcut, det, changed_text=revised), target)
    assert [(c["start_s"], c["end_s"], c["text"]) for c in final_revised] == [
        (target["start_s"], target["end_s"], revised)]


def test_omission_empty_and_unrelated_veto_keep_existing_split_and_other_speaker_path(tmp_path, monkeypatch):
    bcut, jianying = MEMBERS[0]
    outputs = []
    for i, response in enumerate((
        {"suspicious": []},
        {"suspicious": [], "keep_together": []},
        {"suspicious": [], "keep_together": [{"cue_id": bcut[2]["id"], "reason": "无提案"},
                                                {"cue_id": 101, "reason": "剪映编号"}]},
    )):
        det, _, _, _, _ = _detect(tmp_path / str(i), monkeypatch, bcut, jianying, response)
        assert isinstance(det, variant_c._Detection)
        outputs.append((_written(bcut, det), _written(bcut, det, other=True), det.reasons))
    assert outputs[0] == outputs[1] == outputs[2]
    assert len(_span(outputs[0][0], bcut[0])) == 2
    assert [c["text"] for c in _span(outputs[0][1], bcut[1])] == [bcut[1]["text"], "确实如此"]


def test_only_effective_proposals_are_shown_or_cancelled(tmp_path, monkeypatch):
    bcut = [{"id": 73, "start_s": 1.0, "end_s": 3.0, "text": "天气好"},
            {"id": 84, "start_s": 5.0, "end_s": 7.0, "text": "大家准备现在出发"}]
    jianying = [{"id": 1, "start_s": 1.0, "end_s": 2.0, "text": "天气"},
                {"id": 2, "start_s": 2.0, "end_s": 3.0, "text": "很好"},
                {"id": 3, "start_s": 5.0, "end_s": 6.0, "text": "大家准备"},
                {"id": 4, "start_s": 6.0, "end_s": 7.0, "text": "现在出发"}]
    det, prompt, process, _, _ = _detect(tmp_path, monkeypatch, bcut, jianying,
        {"suspicious": [], "keep_together": [{"cue_id": 73, "reason": "不是有效提案"}]})
    assert isinstance(det, variant_c._Detection)
    proposals = json.loads(prompt.split(PROPOSALS, 1)[1].split(BCUT, 1)[0])
    assert [row["cue_id"] for row in proposals] == [84]
    assert set(det.lines.splits) == {73, 84}
    assert not any("不是有效提案" in line for line in process)
    final = _written(bcut, det)
    assert [c["text"] for c in _span(final, bcut[0])] == [bcut[0]["text"]]
    assert len(_span(final, bcut[1])) == 2


def test_numeric_string_veto_does_not_change_suspicious_review(tmp_path, monkeypatch):
    bcut, jianying = MEMBERS[0]
    det, _, _, _, _ = _detect(tmp_path, monkeypatch, bcut, jianying,
        {"suspicious": [{"cue_id": bcut[1]["id"], "reason": "需要另听"}],
         "keep_together": [{"cue_id": f" {bcut[0]['id']} ", "reason": "成分相连"}]})
    assert isinstance(det, variant_c._Detection)
    assert "CPA 审读：需要另听" in det.reasons[bcut[1]["id"]]
    assert not any(reason.startswith("CPA 审读：") for reason in det.reasons.get(bcut[0]["id"], []))
    assert len(_span(_written(bcut, det), bcut[0])) == 1


@pytest.mark.parametrize("bad", [None, {}, "bad", [None], [{}], [{"cue_id": True, "reason": "理由"}],
                                 [{"cue_id": "abc", "reason": "理由"}], [{"cue_id": 1.5, "reason": "理由"}],
                                 [{"cue_id": 17}],
                                 [{"cue_id": 17, "reason": 3}], [{"cue_id": 17, "reason": "  \n "}]])
def test_malformed_optional_field_uses_existing_review_failure(tmp_path, monkeypatch, bad):
    bcut, jianying = MEMBERS[0]
    det, _, _, _, _ = _detect(tmp_path, monkeypatch, bcut, jianying,
                               {"suspicious": [], "keep_together": bad})
    assert not isinstance(det, variant_c._Detection)
    assert not det.ok and det.reason == "CPA_RESPONSE_UNPARSEABLE"
