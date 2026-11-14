"""The gates that remain: 维护者's rules and common-sense checks.  Nothing else stops a clip."""

from __future__ import annotations

from src.autoslice.manual_lane import gates


def _cues(n: int = 30, step: float = 10.0):
    return [{"id": i + 1, "start_s": i * step, "end_s": i * step + step, "text": f"句{i + 1}"} for i in range(n)]


# ----- boundary sanity (two checks on CPA's cut) ---------------------------------------------
def test_boundary_inside_segment_and_reasonable_length_passes():
    assert gates.boundary_sanity(_cues(), start_cue=3, end_cue=8).ok        # 60 s


def test_boundary_outside_the_segment_stops():
    assert gates.boundary_sanity(_cues(), start_cue=3, end_cue=99).reason_code == "BOUNDARY_OUTSIDE_SEGMENT"
    assert gates.boundary_sanity(_cues(), start_cue=8, end_cue=3).reason_code == "BOUNDARY_OUTSIDE_SEGMENT"


def test_boundary_length_outside_30s_to_10min_stops():
    assert gates.boundary_sanity(_cues(), start_cue=3, end_cue=4).reason_code == "BOUNDARY_LENGTH_UNREASONABLE"
    long_cues = _cues(n=80, step=10.0)
    assert gates.boundary_sanity(long_cues, start_cue=1, end_cue=75).reason_code == "BOUNDARY_LENGTH_UNREASONABLE"



def test_dedupe_only_a_complete_duplicate_blocks():
    assert gates.dedupe_gate({"decision": "FULL_DUPLICATE"}).reason_code == "DEDUPE_FULL_DUPLICATE"
    assert gates.dedupe_gate({"decision": "HAS_EXTRA"}).ok
    assert gates.dedupe_gate({"decision": "UNKNOWN"}).ok


def test_doubt_gate_blocks_unresolved_unless_released_and_passes_resolved_states():
    rows = [{"id": "d1", "state": "UNRESOLVED", "affects_meaning": True, "reason_key": "数字"}]
    assert gates.doubt_gate(rows, released_reasons=set()).reason_code == "DOUBT_UNRESOLVED"
    assert gates.doubt_gate(rows, released_reasons={"数字"}).ok
    # TASK-5: an UNRESOLVED stops whether or not it affects meaning (CPA must always choose a best guess)
    assert gates.doubt_gate([dict(rows[0], affects_meaning=False)], released_reasons=set()).reason_code == "DOUBT_UNRESOLVED"
    assert gates.doubt_gate([dict(rows[0], state="FIT_CONTEXT")], released_reasons=set()).ok
    assert gates.doubt_gate([{"id": "d1", "state": None}], released_reasons=set()).reason_code == "DOUBT_STATE_MISSING"


def test_reviewer_label_gate_anchors_on_source_time_and_current_text():
    # the clip starts at 100 s of the source; cue 2 spans 10-20 s of the cut, so 110-120 s of the source
    cues = _cues(3, step=10.0)
    ok = [{"abs_start_ms": 110000, "abs_end_ms": 120000, "current": "句2", "doubt_id": "d002"}]
    verdict = gates.reviewer_label_gate(ok, cues, source_offset_s=100.0)
    assert verdict.ok and list(verdict.evidence["matched"]) == [2]
    assert gates.reviewer_label_gate([dict(ok[0], current="别的")], cues, source_offset_s=100.0).reason_code == \
        "REVIEWER_LABEL_STALE"
    assert gates.reviewer_label_gate([dict(ok[0], abs_start_ms=110700)], cues, source_offset_s=100.0).reason_code == \
        "REVIEWER_LABEL_STALE"


def test_a_small_cut_move_keeps_the_label_on_the_same_speech():
    # the cut moves 0.3 s earlier and ASR shifts the sentence by the same amount: the source-time anchor still holds
    moved = _cues(3, step=10.0)
    for cue in moved:
        cue["start_s"] += 0.3
        cue["end_s"] += 0.3
    ok = [{"abs_start_ms": 110000, "abs_end_ms": 120000, "current": "句2", "doubt_id": "d002"}]
    assert gates.reviewer_label_gate(ok, moved, source_offset_s=99.7).ok


def test_the_label_is_placed_on_the_cue_that_holds_its_speech_not_on_its_old_number():
    cues = _cues(3, step=10.0)
    row = {"abs_start_ms": 120000, "abs_end_ms": 130000, "current": "句3", "doubt_id": "d003"}  # cue 3 = 20-30 s of the cut
    assert list(gates.match_label_anchors([row], cues, source_offset_s=100.0)) == [3]


def test_speaker_mode_only_uniform_or_recorded_colour():
    assert gates.speaker_mode_gate({"speaker_mode": "uniform_host"}).ok
    assert gates.speaker_mode_gate({"speaker_mode": "lds_guest", "enablement_evidence": None}).reason_code == \
        "SPEAKER_MODE_NO_ENABLEMENT_EVIDENCE"
    assert gates.speaker_mode_gate({"speaker_mode": "lds_guest", "enablement_evidence": "x"}).ok
    assert gates.speaker_mode_gate({"speaker_mode": "weird"}).reason_code == "SPEAKER_MODE_UNKNOWN"


def test_intro_must_be_prepended_fail_closed():
    assert gates.intro_gate({"status": "PREPENDED", "intro_id": "Z1", "intro_offset_ms": 0}).ok
    assert gates.intro_gate({"status": "SKIPPED"}).reason_code == "INTRO_NOT_PREPENDED"
    assert gates.intro_gate({"status": "PREPENDED"}).reason_code == "INTRO_ID_MISSING"
    # a channel whose intro manifest is switched off: no intro, offset 0 (prepend_intro.py NO_INTRO)
    assert gates.intro_gate({"status": "NO_INTRO", "intro_id": None, "intro_offset_ms": 0}).ok
    assert not gates.intro_gate({"status": "NO_INTRO", "intro_id": None, "intro_offset_ms": 900}).ok


def test_title_banned_words_follow_reviewer_and_nothing_else():
    assert gates.title_banned("【李豆沙】这一段直接把人笑哭") == ["直接"]
    assert gates.title_banned("【李豆沙】当场破防") == ["当场"]
    assert gates.title_banned("【李豆沙】等了三秒才回") == ["秒+汉字"]
    assert gates.title_banned("【李豆沙】半分钟后才回神") == []
    # a work's name is not the editor's wording; the same words outside the 《》 still count
    assert gates.title_banned("【李豆沙】由菜Yuna问《秒速5厘米》是纯爱吗？") == []
    assert gates.title_banned("【李豆沙】《秒速5厘米》看完秒懂纯爱") == ["秒+汉字"]
    assert gates.title_banned("【李豆沙】《当场破防》这歌直接把她唱哭") == ["直接"]
    # the old quote-verbatim and length rules are gone: a plain title with no quotes is allowed
    assert gates.title_banned("【李豆沙】一个很长很长的标题没有任何引号也没有冒号也完全没有问题的例子") == []


def test_cut_gate_checks_only_the_length_the_tool_was_asked_for():
    assert gates.cut_gate(probe={"duration": 60.02}, expected_seconds=60.0).ok
    assert gates.cut_gate(probe={"duration": 50.0}, expected_seconds=60.0).reason_code == "CUT_DURATION_MISMATCH"
