"""TASK-30 guard (COACH-ADVICE e05 §2 / §4; lane_rules listening.change_needs_listening): a change of the current
text that CPA decided on a level that does not listen (window ASR, text only) while no Gemini listening of the doubt
exists is not settled there; it goes to the next listening level, where CPA decides again with the listening in front
of it.  CPA's last answer stands (U107); a KEEP, or a change only in punctuation / fillers, still settles without
listening (the asr_first saving, TASK-11)."""

from __future__ import annotations

import json

from test_task11_witnesses_cover_lone import _ladder_ctx

from src.autoslice.manual_lane import lane_rules
from src.autoslice.manual_lane import stage_impl as si
from src.autoslice.manual_lane import suspicion


def _row(state, text, confidence="高"):
    return [{"id": "d001", "state": state, "final_text": text, "confidence": confidence, "affects_meaning": True,
             "basis": "窗口 MAI 与 MOSS 一致"}]


def _doubt(ctx):
    return next(d for d in json.loads((ctx.clip_dir / "subs/doubts.json").read_text(encoding="utf-8"))
                if d["cue_id"] == 1)


def test_on_the_production_ladder_a_change_from_the_window_asr_alone_is_listened_to_before_it_stands(
        tmp_path, monkeypatch):
    adopt = _row("ADOPT", "句子甲")
    ctx, calls, cpa = _ladder_ctx(tmp_path, monkeypatch, "asr_first",
                                  {"adjudicate-c-L1-1": adopt, "adjudicate-c-L2-1": adopt, "adjudicate-c-L3-1": adopt})
    out = si.step3_subtitles(ctx)
    assert out.ok, (out.reason, out.note)
    assert "s3-asrwin-C-L1" in calls
    assert "s3-listen-C-L2" in calls                       # the window-ASR change was sent to Gemini 3.8
    assert "adjudicate-c-L2-1" in cpa.calls                 # and CPA decided it again with that listening
    doubt = _doubt(ctx)
    assert doubt["final_text"] == "句子甲" and doubt["settled_at_level"] != "L1"   # CPA's own answer stands (U107)
    assert "句子甲" in (ctx.clip_dir / "subs/final.srt").read_text(encoding="utf-8")


def test_text_first_a_change_from_the_text_evidence_alone_is_listened_to(tmp_path, monkeypatch):
    adopt = _row("FIT_CONTEXT", "句子甲")
    ctx, calls, cpa = _ladder_ctx(tmp_path, monkeypatch, "text_first",
                                  {"adjudicate-c-L0-1": adopt, "adjudicate-c-L1-1": adopt, "adjudicate-c-L2-1": adopt})
    out = si.step3_subtitles(ctx)
    assert out.ok, (out.reason, out.note)
    assert "s3-listen-C-L1" in calls and "adjudicate-c-L1-1" in cpa.calls
    assert _doubt(ctx)["final_text"] == "句子甲"


def test_a_change_only_in_punctuation_or_fillers_still_settles_on_the_window_asr(tmp_path, monkeypatch):
    ctx, calls, _cpa = _ladder_ctx(tmp_path, monkeypatch, "asr_first",
                                   {"adjudicate-c-L1-1": _row("ADOPT", "嗯，句子1。")})
    out = si.step3_subtitles(ctx)
    assert out.ok, (out.reason, out.note)
    assert not any(c.startswith("s3-listen-") for c in calls)
    assert _doubt(ctx)["settled_at_level"] == "L1"


def test_with_the_switch_off_a_window_asr_change_settles_at_l1(tmp_path, monkeypatch):
    rules = json.loads(lane_rules.RULES_FILE.read_text(encoding="utf-8"))
    rules["listening"]["change_needs_listening"] = False
    path = tmp_path / "rules.json"
    path.write_text(json.dumps(rules, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setenv("MANUAL_LANE_RULES", str(path))
    ctx, calls, _cpa = _ladder_ctx(tmp_path, monkeypatch, "asr_first", {"adjudicate-c-L1-1": _row("ADOPT", "句子甲")})
    out = si.step3_subtitles(ctx)
    assert out.ok, (out.reason, out.note)
    assert not any(c.startswith("s3-listen-") for c in calls)


def test_unheard_change_is_a_content_change_without_any_gemini_reading():
    change = {"state": "ADOPT", "final_text": "沙豆李在藏尸体"}
    asr_only = {"bcut": "沙豆李在藏尸体", "jianying": "Shadow Lee在藏尸体", "asr:moss@win": "Shadow Lee在藏尸体"}
    assert suspicion.unheard_change(change, "Shadow Lee在藏尸体", asr_only)
    assert not suspicion.unheard_change(change, "Shadow Lee在藏尸体", dict(asr_only, **{"audio:38m@P0": "沙豆李在藏"}))
    assert not suspicion.unheard_change({"state": "KEEP", "final_text": "现稿"}, "Shadow Lee在藏尸体", asr_only)
    assert not suspicion.unheard_change({"state": "FIT_CONTEXT", "final_text": "她说了"}, "他说的", asr_only)
