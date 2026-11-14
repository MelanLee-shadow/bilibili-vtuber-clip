"""evidence_attach helpers (TASK-6): ranking of danmaku and the ownership invariant.  Synthetic texts."""

from __future__ import annotations

from test_evidence_attach import BCUT, JIANYING

from src.autoslice.manual_lane import evidence_attach as ea


def test_danmaku_are_ranked_by_relevance_not_by_time_and_cut_by_count():
    cue = {"id": 1, "start_s": 100.0, "end_s": 102.0, "text": "今天吃火锅"}
    rows = [{"t_s": 101.0 + i * 0.1, "text": f"哈{i}"} for i in range(20)] + [{"t_s": 30.0, "text": "今天吃火锅吗"}]
    ranked = ea.danmaku_for_cue(cue, rows, ["今天吃火锅"])
    assert ranked[0]["text"] == "今天吃火锅吗" and len(ranked) == ea.DANMAKU_PER_CUE


def test_every_segment_has_an_owner_and_a_segment_over_two_cues_reaches_both_with_its_own_slice():
    cues = [dict(id=i, start_s=a, end_s=b, text=t) for i, (a, b, t) in enumerate(BCUT, 1)]
    segs = [{"start_s": 0.2, "end_s": 3.9, "text": "跨两句，的一段"}] + ea.segments_from_srt_cues(
        [dict(start_s=a, end_s=b, text=t) for a, b, t in JIANYING])
    att = ea.attach_asr(cues, segs, source="jianying")
    assert att.stats()["unhandled"] == 0 and att.stats()["attached"] == len(segs)
    # spans both cues: both receive it, each its own slice (TASK-17), cut at the engine's break nearest the boundary
    assert att.slices[0] == {1: "跨两句", 2: "的一段"}
    assert "跨两句" in att.text(1) and "的一段" not in att.text(1) and "的一段" in att.text(2)
