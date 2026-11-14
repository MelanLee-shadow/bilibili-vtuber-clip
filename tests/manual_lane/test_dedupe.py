"""A reading belongs to its own sentence (neighbours' words trimmed).  The example is the 692 repeat: 「到底谁是真的」
appeared at the end of one sentence and the start of the next.  Since U159 the trim is overlap_join.trim_reading: the
repeat goes only when the whole-clip readings say it was said once.  The final assembly reports a repeat at a join and
keeps the decided words (TASK-9)."""

from __future__ import annotations

from src.autoslice.manual_lane import normalize as nz
from src.autoslice.manual_lane import overlap_join as oj
from src.autoslice.manual_lane import stage_impl as si

PREV_CUE = {"start_s": 0.0, "end_s": 5.0, "text": "假CP里面开始揪到底谁是真的，"}
CUE = {"start_s": 5.0, "end_s": 7.0, "text": "到底谁是真的"}
NEXT_CUE = {"start_s": 7.0, "end_s": 9.0, "text": "到底谁是真的，她说"}
# a whole-clip engine heard the words once over the two sentences
ONCE = {"asr:mai": [{"start_s": 0.0, "end_s": 7.0, "text": "假CP里面开始揪到底谁是真的所以呢"}]}


def test_a_reading_loses_the_previous_sentences_tail_at_its_start():
    # the window for the next sentence ran back into 到底谁是真的 and went on to 所以呢
    assert oj.trim_reading("到底谁是真的所以呢", CUE, PREV_CUE, None, ONCE)[0] == "所以呢"


def test_a_reading_loses_the_next_sentences_head_at_its_end():
    once = {"asr:mai": [{"start_s": 5.0, "end_s": 9.0, "text": "这一句完了到底谁是真的她说"}]}
    assert oj.trim_reading("这一句完了到底谁是真的", CUE, None, NEXT_CUE, once)[0] == "这一句完了"


def test_overlaps_shorter_than_two_characters_are_kept():
    prev = dict(PREV_CUE, text="开始揪到底谁是真的")
    assert oj.trim_reading("的是", CUE, prev, None, ONCE)[0] == "的是"   # only one character would overlap: kept
    assert oj.trim_reading("好的", CUE, dict(PREV_CUE, text="一句话的"), None, ONCE)[0] == "好的"


def test_without_a_whole_clip_reading_on_both_sides_nothing_is_trimmed():
    # U159: 没有跨越证据就不合并
    assert oj.trim_reading("到底谁是真的所以呢", CUE, PREV_CUE, None, {})[0] == "到底谁是真的所以呢"
    only_before = {"asr:mai": [{"start_s": 0.0, "end_s": 4.9, "text": "假CP里面开始揪到底谁是真的"}]}
    assert oj.trim_reading("到底谁是真的所以呢", CUE, PREV_CUE, None, only_before)[0] == "到底谁是真的所以呢"


def test_a_reading_trimmed_against_the_neighbours_in_the_step_3_assembly():
    lines = {"audio:38m@P0": [{"abs_start_s": 5.2, "text": "到底谁是真的所以呢"}]}
    out = si._trimmed_readings(CUE, lines, PREV_CUE, None, ONCE)
    assert out == {"audio:38m@P0": "所以呢"}


def test_bcut_stays_current_but_jianying_uses_the_same_evidence_based_neighbour_join():
    lines = {"bcut": [{"abs_start_s": 5.2, "text": "到底谁是真的"}],
             "jianying": [{"abs_start_s": 5.2, "text": "到底谁是真的"}]}
    assert si._trimmed_readings(CUE, lines, PREV_CUE, None, ONCE) == {"bcut": "到底谁是真的"}
    assert si._trimmed_readings(CUE, lines, PREV_CUE, None, {}) == {
        "bcut": "到底谁是真的", "jianying": "到底谁是真的"}  # U159: missing evidence keeps both


def test_word_level_timing_keeps_only_the_words_inside_the_cue_when_it_is_there():
    words = [{"text": "到底", "abs_start_s": 4.0, "abs_end_s": 4.4},
             {"text": "谁是真的", "abs_start_s": 5.1, "abs_end_s": 6.0},
             {"text": "所以", "abs_start_s": 7.8, "abs_end_s": 8.2}]  # centre 8.0 s: outside 5.0-7.0 + 0.5
    assert nz.words_in_cue(words, 5.0, 7.0) == "谁是真的"


def test_a_repeat_at_the_join_is_logged_but_the_decided_words_are_kept():
    # TASK-9: the assembly never removes words the adjudicator, a reviewer fix or an import wrote; the repeat at a
    # join is reported (and is a doubt reason in step 3, normalize.join_repeat), CPA decides it
    cues = [{"start_s": 0.0, "end_s": 5.0, "text": PREV_CUE["text"]},
            {"start_s": 5.0, "end_s": 7.0, "text": "到底谁是真的所以呢"}]
    out, log = nz.normalize_cues(cues)
    text = "".join(c["text"].replace("\n", "") for c in out)
    assert text.count("到底谁是真的") == 2 and "所以呢" in text
    assert log["adjacent_repeats"] == [{"cue_id": None, "kind": "tail"}]


def test_a_restart_after_a_false_start_keeps_its_first_words():
    # 15_229 (TASK-8): 「我们最最…」 then the restart 「我们最近由于…」: the old head rule cut 我们最 off the restart
    cues = [{"start_s": 20.93, "end_s": 21.89, "text": "我们最最…"},
            {"start_s": 21.89, "end_s": 27.31, "text": "我们最近由于这个小圆剧场版回天的出现"}]
    out, _ = nz.normalize_cues(cues)
    assert "".join(c["text"].replace("\n", "") for c in out[1:]).startswith("我们最近由于")


def test_a_join_repeat_is_a_doubt_reason_and_a_paused_repeat_is_not():
    prev = {"start_s": 0.0, "end_s": 1.0, "text": "你能想象"}
    assert nz.join_repeat(prev, {"start_s": 1.1, "end_s": 2.0, "text": "你能想象这个"})
    assert nz.join_repeat(prev, {"start_s": 2.0, "end_s": 3.0, "text": "你能想象这个"}) == ""   # a real pause
    assert nz.join_repeat(None, {"start_s": 0.0, "end_s": 1.0, "text": "你能想象"}) == ""
