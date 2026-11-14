"""TASK-17 / U159: two timed readings that repeat each other at their boundary are joined by one function
(overlap_join), for step 3's readings and for scoring alike.  A real repeat (said three times) is never collapsed; one
speech written by two windows is counted once; without whole-clip evidence on both sides nothing is merged."""

from __future__ import annotations

from src.autoslice.manual_lane import evidence_attach as ea
from src.autoslice.manual_lane import overlap_join as oj
from src.autoslice.manual_lane import score as sc
from src.autoslice.manual_lane import stage_impl as si

# 1229_1356 ~78-84 s: BCUT heard 「没有钱了没有钱了没有钱啊」, said three times
THRICE = {"bcut": [{"start_s": 78.72, "end_s": 84.02, "text": "没有钱了没有钱了没有钱啊"}]}


def test_a_repeat_said_three_times_is_not_joined_into_two():
    items = [{"id": "a", "start_s": 78.72, "end_s": 80.70, "text": "没有钱了"},
             {"id": "b", "start_s": 80.70, "end_s": 84.02, "text": "没有钱了没有钱啊"}]
    out, log = oj.join_items(items, THRICE)
    assert log[0]["candidates"] == [4]                         # the longest-overlap rule would have merged 没有钱了
    assert "".join(i["text"] for i in out).count("没有钱") == 3 and log[0]["k"] == 0


def test_one_speech_written_by_two_windows_is_joined_into_one():
    items = [{"id": "a", "start_s": 0.0, "end_s": 2.4, "text": "其实她的爽点是一种"},      # this window ran on
             {"id": "b", "start_s": 2.0, "end_s": 4.0, "text": "是一种爽文感的爽点"}]
    once = {"jianying": [{"start_s": 0.0, "end_s": 4.0, "text": "其实她的爽点是一种爽文感的爽点"}]}
    out, log = oj.join_items(items, once)
    assert "".join(i["text"] for i in out) == "其实她的爽点是一种爽文感的爽点" and log[0]["k"] == 3


def test_no_whole_clip_reading_on_both_sides_means_no_join():
    items = [{"id": "a", "start_s": 0.0, "end_s": 2.0, "text": "其实她的爽点是一种"},
             {"id": "b", "start_s": 2.0, "end_s": 4.0, "text": "是一种爽文感的爽点"}]
    one_side = {"bcut": [{"start_s": 0.0, "end_s": 1.9, "text": "其实她的爽点是一种"}]}
    for sources in ({}, one_side):
        out, log = oj.join_items(items, sources)
        assert log[0]["k"] == 0 and out[0]["text"] == "其实她的爽点是一种"


def test_scoring_joins_truth_with_the_pipeline_function(monkeypatch):
    calls = []

    def spy(items, sources, *, norm):
        calls.append((len(items), dict(sources)))
        return [dict(i) for i in items], []

    monkeypatch.setattr(oj, "join_items", spy)
    truth = [{"id": "t0", "abs_start_ms": 10_000, "abs_end_ms": 12_000, "text": "没有钱了"}]
    sc.dedup_truth(truth, THRICE, offset_ms=0)
    assert calls == [(1, THRICE)]


def test_step_3_keeps_a_real_repeat_in_a_listening_reading_and_trims_a_window_that_ran_back():
    def reading(cue2_bcut, whole_text):
        bcut = [{"id": 1, "start_s": 78.72, "end_s": 80.70, "text": "没有钱了"},
                {"id": 2, "start_s": 80.70, "end_s": 84.02, "text": cue2_bcut}]
        jy = ea.attach_asr(bcut, [], source="jianying")
        asr = {"asr:mai": [{"abs_start_s": 78.72, "end_s": 84.02, "text": whole_text}]}
        att = ea.own_witness_lines(bcut, asr)
        lines = {"audio:38m@P0": [{"abs_start_s": 80.5, "text": "没有钱了没有钱啊", "owners": [2]}], **asr}
        return si._evidence_for_cue(bcut[1], 1, bcut, jy, att, lines, [])["readings"]["audio:38m@P0"]

    # said three times (BCUT and MAI agree): the listener's words stay
    assert reading("没有钱了没有钱啊", "没有钱了没有钱了没有钱啊") == "没有钱了没有钱啊"
    # said twice: the listening window ran back into cue 1, and the repeat goes
    assert reading("没有钱啊", "没有钱了没有钱啊") == "没有钱啊"
