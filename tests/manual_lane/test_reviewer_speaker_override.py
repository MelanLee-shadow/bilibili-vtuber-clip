"""Step 4: a cue 维护者 labelled takes 维护者's speaker; any other cue keeps the v8 result (TASK-5 round 2 / QA-02)."""

from __future__ import annotations

from src.autoslice.manual_lane import speaker_v8 as sv


def _dec(speaker):
    return {"start_ms": 0, "end_ms": 0, "speaker": speaker, "conf": 0.9, "units": 1}


def test_a_labelled_cue_takes_reviewers_speaker_and_an_unlabelled_one_keeps_v8():
    decided = [_dec(sv.HOST), _dec(sv.GUEST_LABEL)]
    cues_abs = [(1000, 3000), (5000, 7000)]
    labels = [{"abs_start_ms": 1100, "abs_end_ms": 3000, "speaker": "雾深Girimi", "truth_key": "t1"}]
    out = sv.apply_reviewer_labels(decided, cues_abs, labels)
    assert out[0]["speaker"] == sv.GUEST_LABEL and out[0]["source"] == "维护者" and out[0]["reviewer_truth_key"] == "t1"
    assert out[1]["speaker"] == sv.GUEST_LABEL and out[1]["source"] == "v8"


def test_a_label_that_covers_less_than_half_of_the_cue_does_not_override():
    out = sv.apply_reviewer_labels([_dec(sv.GUEST_LABEL)], [(0, 4000)],
                               [{"abs_start_ms": 3000, "abs_end_ms": 6000, "speaker": "李豆沙"}])
    assert out[0]["speaker"] == sv.GUEST_LABEL and out[0]["source"] == "v8"


def test_reviewers_host_name_maps_to_the_host_label():
    out = sv.apply_reviewer_labels([_dec(sv.GUEST_LABEL)], [(0, 1000)],
                               [{"abs_start_ms": 0, "abs_end_ms": 1000, "speaker": "李豆沙"}])
    assert out[0]["speaker"] == sv.HOST and out[0]["source"] == "维护者"
