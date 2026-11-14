"""Literal source facts travel from ordinary synthetic inputs to the adjudication request."""
from __future__ import annotations

import json

import pytest
from fakes import FakeRemote, IdCpa, make_ctx

from src.autoslice.manual_lane import doubt_policy, evidence_attach, stage_impl, step3_adjudicate, step3_subtitles, variant_c
from src.autoslice.manual_lane.runner import StepOutcome
from src.autoslice.manual_lane.step3_doubts import doubt_entry
from src.autoslice.manual_lane.step3_evidence import _evidence_for_cue


@pytest.mark.parametrize(
    "current,jianying,mai,with_moss",
    [
        ("咱们现在继续确认现场安排", "离开房间前请把右边那扇窗户打开", "离开房间前请把右边那扇窗户关紧", False),
        ("先核对设备安全", "离开之前请确认侧门已经完全锁好", "离开之前请确认侧门没有完全锁好", True),
        ("我们正在整理房间", "把走廊旁边的备用纸箱搬进两间房", "把走廊旁边的备用纸箱搬进三间房", True),
        ("先把这些零件按顺序放好", "先把这些零件按顺序放好", "先把这些零件按顺序放好放好", True),
        ("嗯", "嗯", "啊", True),
        ("现在开始讨论这个方案", "现在开始讨论这个方案。", "现在开始讨论这个方案", True),
    ],
    ids=["open-close", "negation", "chinese-number", "repetition", "filler", "punctuation"],
)
def test_ordinary_source_inputs_reach_request_without_borrowed_votes(tmp_path, monkeypatch, current, jianying, mai, with_moss):
    cues = [{"id": 1, "start_s": 0.0, "end_s": 3.0, "text": current}]
    jy = evidence_attach.attach_asr(cues, [{"start_s": 0.0, "end_s": 3.0, "text": jianying}], source="jianying")
    lines = {source: [{"abs_start_s": 0.0, "end_s": 3.0, "text": value}]
             for source, value in (("asr:mai", mai), ("asr:mai@win", mai))}
    if with_moss:
        lines["asr:moss"] = [{"abs_start_s": 0.0, "end_s": 3.0, "text": jianying}]
    witnesses = evidence_attach.own_witness_lines(cues, lines)
    ev = _evidence_for_cue(cues[0], 0, cues, jy, witnesses, lines,
                           [{"t_s": 0.5, "text": "弹幕只是语境"}])
    doubt = doubt_entry(cues[0], {"start_ms": 10000}, ev, differing=["jianying", "asr:mai"])
    cpa = IdCpa()
    ctx = make_ctx(tmp_path, remote=FakeRemote(), cpa=cpa)
    monkeypatch.setattr(step3_adjudicate, "_confusion_lines", lambda *_args: "合成误听线索仅用于测试")
    events = []
    answers, failure = step3_adjudicate._adjudicate_all(ctx, [doubt], "1 0-3 " + current, events, tag="source-facts")
    assert failure is None and len(cpa.prompts) == 1
    prompt = cpa.prompts[0]
    (tmp_path / "adjudication.prompt.txt").write_text(prompt, encoding="utf-8")
    (tmp_path / "source-evidence.json").write_text(json.dumps(ev, ensure_ascii=False, indent=2), encoding="utf-8")
    (tmp_path / "fake-response.json").write_text(json.dumps({"events": events, "answers": answers},
                                                            ensure_ascii=False, indent=2), encoding="utf-8")
    part = prompt.split("输入疑点（含候选读法及其支持来源、各来源读法与弹幕线索）：\n", 1)[1]
    captured = json.loads(part.split("\n\n全片字幕（序号 时间 文字）：\n", 1)[0])[0]
    assert captured["readings"]["jianying"] == jianying
    assert captured["readings"]["asr:mai"] == captured["readings"]["asr:mai@win"] == mai
    assert (captured["readings"].get("asr:moss") == jianying) is with_moss
    assert captured["danmaku"] and captured["abs_start_ms"] == 10000 and captured["abs_end_ms"] == 13000
    by_text = {row["text"].strip(): row for row in captured["candidates"]}
    assert jianying in by_text and mai in by_text
    j_families = {"asr:jianying"} | ({"asr:moss"} if with_moss else set())
    j_families |= {"current"} if current == jianying else set()
    m_families = {"asr:mai"} | ({"current"} if current == mai else set())
    assert set(by_text[jianying]["families"]) == j_families
    assert set(by_text[mai]["families"]) == m_families
    assert by_text[jianying]["supporters"] == len(j_families)
    assert by_text[mai]["supporters"] == len(m_families)
    assert {"asr:mai", "asr:mai@win"} <= set(by_text[mai]["sources"])
    assert "asr:mai" not in by_text[jianying]["sources"]
    assert all("danmaku" not in row.get("families", []) for row in captured["candidates"])
    summary = captured["maintenance"]["candidates"]
    assert set(summary[jianying]["families"]) == j_families
    assert set(summary[mai]["families"]) == m_families
    if not with_moss:
        assert not captured["maintenance"]["maintained"]


def test_exact_trim_only_and_schemes_keep_real_sources_without_double_counting(monkeypatch):
    readings = {"bcut": "开 门", "jianying": " 开 门 ", "asr:mai": "开 门", "asr:mai@win": "开 门",
                "asr:moss": "开门", "audio:38m@P0": "开 门", "audio:argon@P1": "开 门",
                "danmaku": "开门", "first_pass": "开门"}
    for scheme, families in (("u59", {"current", "asr:jianying", "asr:mai", "gemini"}),
                             ("no_asr", {"current", "gemini"}),
                             ("no_current", {"asr:jianying", "asr:mai", "gemini"})):
        monkeypatch.setenv("MANUAL_LANE_VOTE_SCHEME", scheme)
        rows = {r["text"].strip(): r for r in doubt_policy.candidates(readings, "开 门")}
        assert set(rows) == {"开 门", "开门"}
        assert set(rows["开 门"]["families"]) == families
        assert rows["开 门"]["supporters"] == len(families)
        assert set(rows["开门"]["families"]) == ({"asr:moss"} if scheme != "no_asr" else set())
        assert {"jianying", "asr:mai", "asr:mai@win", "audio:38m@P0", "audio:argon@P1"} <= set(rows["开 门"]["sources"])
        assert all(not ({"danmaku", "first_pass"} & set(row["sources"])) for row in rows.values())
    monkeypatch.setenv("MANUAL_LANE_VOTE_SCHEME", "list_only")
    assert all("families" not in r and "supporters" not in r for r in doubt_policy.candidates(readings, "开 门"))
    assert "supporters 是" not in doubt_policy.vote_prompt_fields()["supporters_doc"]
    assert len(doubt_policy.clusters({"jianying": "Alpha", "asr:mai": "alpha"}, "背景")) == 3


@pytest.mark.parametrize("mode", ["A", "B", "full"])
def test_sort_cues_uses_voice_not_candidate_presence_and_preserves_a_auto_keep(mode):
    cues = [{"id": 1, "start_s": 0.0, "end_s": 1.0, "text": "嗯"},
            {"id": 2, "start_s": 1.0, "end_s": 2.0, "text": "哈哈哈哈"},
            {"id": 3, "start_s": 2.0, "end_s": 3.0, "text": ""},
            {"id": 4, "start_s": 3.0, "end_s": 4.0, "text": "嗯"},
            {"id": 5, "start_s": 4.0, "end_s": 5.0, "text": ""}]
    sources = {1: "啊", 2: "（笑）", 3: "", 4: "嗯", 5: "啊"}
    def evidence(_index, cue):
        return {"readings": {"bcut": cue["text"], "jianying": sources[cue["id"]]}, "danmaku": []}
    result = step3_subtitles._sort_cues(cues, {"start_ms": 0}, {}, {}, mode, evidence)
    assert result["no_vote"] == [2, 3]
    doubts = {d["cue_id"] for d in result["doubts"]}
    assert {1, 5} <= doubts
    if mode == "A":
        assert 4 not in doubts and any(d["cue_id"] == 4 and d["source"] == "NO_WITNESS_DISAGREEMENT"
                                       for d in result["auto_keep"])
    else:
        assert 4 in doubts
    assert not any(d.get("source") == "FILLER_KEPT" for d in result["auto_keep"])


def test_default_c_request_and_reference_modes_share_source_facts_without_forcing_majority(tmp_path, monkeypatch):
    """Capture ordinary C detection/settlement and the actual A/B/full adjudication requests offline."""
    monkeypatch.delenv("MANUAL_LANE_ADJUDICATE_MODE", raising=False)
    assert step3_adjudicate.adjudicate_mode() == "C"
    bcut = [{"id": 1, "start_s": 0.0, "end_s": 3.0, "text": "出门前先确认房间"}]
    jy = [{"id": 9, "start_s": 0.0, "end_s": 3.0, "text": "离开房间前请把右边那扇窗户打开"}]
    whole = {"asr:mai": [{"abs_start_s": 0.0, "end_s": 3.0, "text": "离开房间前请把右边那扇窗户关紧"}]}
    comments = [{"t_s": 0.5, "text": "合成弹幕线索"}]
    ctx = make_ctx(tmp_path, remote=FakeRemote(), cpa=IdCpa())
    prompts = {}
    monkeypatch.setattr(stage_impl, "_danmaku_lines", lambda *_: comments)
    monkeypatch.setattr(stage_impl, "_asr_witnesses", lambda *_: (whole, {"mai": {"status": "OK", "segments": 1}}))
    monkeypatch.setattr(stage_impl, "_speaker_chunks", lambda *_: ({}, ""))
    monkeypatch.setattr(stage_impl, "_attach_work_lookups", lambda *_: [])
    monkeypatch.setattr(step3_subtitles, "_attach_work_lookups", lambda *_: [])
    monkeypatch.setattr(stage_impl, "_confusion_lines", lambda *_args: "合成误听线索仅用于测试")
    monkeypatch.setattr(step3_adjudicate, "_confusion_lines", lambda *_args: "合成误听线索仅用于测试")
    def review(_ctx, call_id, prompt, **_kwargs):
        prompts[call_id] = prompt
        return {"call_id": call_id, "rc": 0, "response_text": '{"suspicious": []}', "file": "fake"}
    def adjudicate(_ctx, call_id, prompt, **_kwargs):
        prompts[call_id] = prompt
        part = prompt.split("输入疑点（含候选读法及其支持来源、各来源读法与弹幕线索）：\n", 1)[1]
        doubts = json.loads(part.split("\n\n全片字幕（序号 时间 文字）：\n", 1)[0])
        response = [{"id": d["id"], "state": "KEEP", "final_text": d["current"], "confidence": "高",
                     "affects_meaning": False} for d in doubts]
        return {"call_id": call_id, "rc": 0, "response_text": json.dumps(response, ensure_ascii=False), "file": "fake"}
    monkeypatch.setattr(stage_impl, "_ask_cpa", review)
    monkeypatch.setattr(step3_adjudicate, "_ask_cpa", adjudicate)
    det = variant_c._detect(ctx, stage_impl, bcut, jy, 3.0, {}, [], [])
    assert not isinstance(det, StepOutcome)
    esc = variant_c._Escalation(pending={1}, lines_by_source=whole)
    assert variant_c._settle_level(ctx, stage_impl, det, esc, "L1", "fake", bcut,
                                   ctx.state.data["clip"], [], batch={1}) is None
    captured = {"C": prompts["adjudicate-c-L1-1"]}
    doubt = esc.latest[1]["doubt"]
    for mode in ("A", "B", "full"):
        _, _, failure = step3_subtitles._adjudicate_doubts(ctx, bcut, [doubt], mode, [], {})
        assert failure is None
        captured[mode] = prompts["adjudicate-p1-1"]
    for mode, prompt in captured.items():
        (tmp_path / f"{mode}-request.prompt.txt").write_text(prompt, encoding="utf-8")
    for mode, prompt in captured.items():
        part = prompt.split("输入疑点（含候选读法及其支持来源、各来源读法与弹幕线索）：\n", 1)[1]
        row = json.loads(part.split("\n\n全片字幕（序号 时间 文字）：\n", 1)[0])[0]
        by_text = {c["text"]: c for c in row["candidates"]}
        assert row["readings"]["bcut"] == bcut[0]["text"]
        assert row["readings"]["jianying"] == jy[0]["text"]
        assert row["readings"]["asr:mai"] == whole["asr:mai"][0]["text"]
        assert by_text[jy[0]["text"]]["families"] == ["asr:jianying"]
        assert by_text[whole["asr:mai"][0]["text"]]["families"] == ["asr:mai"]
        assert not row["maintenance"]["maintained"]
        assert "不必按票数选择" in prompt and "单来源新增规则" in prompt
        assert "依据可以是来源读法、音节、时间归属或上下文" in prompt
        assert "只判断仍未确定的部分或省略" in prompt and '"against_maintained"' in prompt
        assert "选字规则（顺序即优先级）" not in prompt
        assert "没有两家一致的读法时" not in prompt
        assert "不选两家一致读法时的上下文冲突理由" not in prompt
        assert "哪一句、哪里读不通或矛盾" not in prompt
        assert (step3_adjudicate.STRICT_RULES in prompt) is (mode == "B")
        if mode == "C":
            assert "【BCUT 全片字幕" in prompt and "【剪映独立 ASR 全片字幕" in prompt
            assert "【已取得的全片主证据" in prompt and "合成弹幕线索" in prompt
