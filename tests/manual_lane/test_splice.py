"""A chosen reading is never two candidates joined.  The example is 692's 「他开始推谁是真的是真的杀呀」: 是真的 appears
twice, and no single candidate carries the repeat.  Splice: ask once more with the feedback; still spliced: take the
single candidate with the most independent sources and record why."""

from __future__ import annotations

import json

from fakes import FakeCpa, make_ctx

from src.autoslice.manual_lane import doubt_policy as dp
from src.autoslice.manual_lane import stage_impl as si

SPLICED = "他开始推谁是真的是真的杀呀"
TWO = ["所以他们开始推谁是真的", "是真的杀呀"]


def test_a_repeat_that_no_candidate_carries_is_a_splice():
    assert dp.repeated_runs(SPLICED) == ["是真的"]
    assert dp.is_concatenation(SPLICED, TWO) is True


def test_a_repeat_that_a_single_candidate_carries_is_not_a_splice():
    assert dp.is_concatenation(SPLICED, [SPLICED, "是真的杀呀"]) is False


def test_text_without_a_repeat_is_not_a_splice():
    assert dp.is_concatenation("所以他们开始推谁是真的", TWO) is False


def _doubt():
    return {"id": "d010", "cue_id": 10, "current": "他开始推谁是真的杀呀",
            "candidates": [
                {"text": "他开始推谁是真的杀呀", "source": "current", "sources": ["current"], "families": [],
                 "supporters": 0},
                {"text": "所以他们开始推谁是真的", "source": "jianying", "sources": ["jianying"],
                 "families": ["asr:jianying"], "supporters": 1},
                {"text": "是真的杀呀", "source": "audio:38m@P0", "sources": ["audio:38m@P0", "asr:mai"],
                 "families": ["asr:mai", "gemini"], "supporters": 2}],
            "maintenance": {"maintained": False, "reading": ""}}


def _row(text, state="FIT_CONTEXT"):
    return {"id": "d010", "state": state, "final_text": text, "affects_meaning": True, "basis": "拟合",
            "against_maintained": "", "reason_key": "同音"}


def test_a_splice_is_asked_again_with_the_feedback_and_the_clean_answer_is_taken(tmp_path):
    clean = json.dumps([{"id": "d010", "state": "ADOPT", "final_text": "是真的杀呀", "affects_meaning": True,
                         "basis": "单一候选"}], ensure_ascii=False)
    ctx = make_ctx(tmp_path, remote=None, cpa=FakeCpa([clean]))
    row, calls, note = si.resolve_concatenation(ctx, _doubt(), _row(SPLICED), call_prefix="concat-d010",
                                                context="1 00:00:00 x")
    assert len(calls) == 1 and ctx.adapters.cpa.calls == ["concat-d010"]
    assert "不要拼接" in ctx.adapters.cpa.prompts[0] and "是真的" in ctx.adapters.cpa.prompts[0]
    assert row["final_text"] == "是真的杀呀" and "重问一次" in note


def test_a_splice_that_persists_takes_the_candidate_with_the_most_independent_sources(tmp_path):
    again = json.dumps([{"id": "d010", "state": "FIT_CONTEXT", "final_text": SPLICED, "affects_meaning": True,
                         "basis": "拟合"}], ensure_ascii=False)
    ctx = make_ctx(tmp_path, remote=None, cpa=FakeCpa([again]))
    row, calls, note = si.resolve_concatenation(ctx, _doubt(), _row(SPLICED), call_prefix="concat-d010",
                                                context="1 00:00:00 x")
    assert row["state"] == "ADOPT" and row["final_text"] == "是真的杀呀"       # two families beat one
    assert "独立来源最多" in row["basis"] and "拼接未解决" in note
    assert len(calls) == 1


def test_a_clean_choice_is_left_alone_and_costs_no_extra_call(tmp_path):
    ctx = make_ctx(tmp_path, remote=None, cpa=FakeCpa([]))
    row, calls, note = si.resolve_concatenation(ctx, _doubt(), _row("是真的杀呀", state="ADOPT"),
                                                call_prefix="concat-d010", context="x")
    assert row["final_text"] == "是真的杀呀" and calls == [] and note == ""
