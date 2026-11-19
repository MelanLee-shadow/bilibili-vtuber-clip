"""Step 3: a read-aloud stretch is written as the chat record has it (read_aloud_copy)."""

import json

from src.autoslice.manual_lane import read_aloud_copy as rac
from src.autoslice.manual_lane import subtitle_track as st

SC = "本来是想对小李说薯ki的，但是感觉这样像在陪主包唱南町歌，就不想了（"


def cue(text, start=140.0, end=144.0):
    return {"start_s": start, "end_s": end, "text": text, "speaker": None}


def test_the_sc_she_reads_is_written_as_the_record_not_as_the_picture_read_it():
    # 1086800_trim 11-18: the picture tool read 町 as 酮 and the agent took it
    # both recognisers heard 唱难听歌 = chàng nán tīng gē, the sound of 南町 and not of 南酮
    cues = [cue("像在陪主包唱南酮歌，\n就不想了（ 咋了？")]
    found = rac.copies(cues, [{"t_s": 120.0, "text": SC, "type": "sc"}], [["像在陪主包唱难听歌", "就不想了"]])
    rac.apply(cues, found)
    assert cues[0]["text"] == "像在陪主包唱南町歌，\n就不想了（ 咋了？"
    assert [(c["written"], c["record_has"]) for c in found] == [("酮", "町")]


def test_without_hearing_the_record_s_sound_a_different_character_is_not_copied():
    cues = [cue("像在陪主包唱南酮歌，\n就不想了（")]
    assert rac.copies(cues, [{"t_s": 120.0, "text": SC, "type": "sc"}]) == []
    assert rac.copies(cues, [{"t_s": 120.0, "text": SC, "type": "sc"}], [["像在陪主包唱南酮歌"]]) == []


def test_she_reads_a_comment_back_in_her_own_words_and_that_stays():
    # 11-18 sweep over 34 lane clips: she changes the pronoun, a word or makes a joke on it while reading
    cases = [("其实你的ID也是小凑吗", "其实我的ID也是小凑吗（", "其实你的ID也是小凑吗"),
             ("冷知识：主播喜欢椰子鸡。", "冷知识，主包喜欢椰子鸡", "冷知识主播喜欢椰子鸡"),
             ("差点看成你吃土的祥子真可爱", "差点看成你吃土的样子真可爱", "差点看成你吃土的祥子真可爱")]
    for written, record, heard in cases:
        assert rac.copies([cue(written, 30.0, 33.0)], [{"t_s": 20.0, "text": record}], [[heard]]) == [], written


def test_her_own_reply_in_other_words_is_left_alone():
    cues = [cue("唱难听歌，你不知道", 144.3, 146.6), cue("好听的也要一起，难听的也要一起", 147.0, 150.0)]
    assert rac.copies(cues, [{"t_s": 120.0, "text": SC, "type": "sc"}]) == []


def test_a_comment_read_with_its_first_character_rewritten_is_copied_too():
    # 11-16: review rewrote the chat's 冲b币换电池 as 充
    cues = [cue("充b币换电池", 30.0, 32.0)]
    found = rac.copies(cues, [{"t_s": 20.0, "text": "冲b币换电池"}])
    assert [(c["written"], c["record_has"]) for c in found] == [("充", "冲")]


def test_a_word_of_her_own_inside_the_reading_does_not_hide_it():
    # 1677890_trim 11-18: she read the SC with 是 added; the trim's agent had no chat and wrote 猪屎
    sc = "那个切片本来想起标题是异食癖，想想攻击性太高了去群里征集，他们提的黑白小猪的猪食"
    cues = [cue("去群里征集，\n他们提的是黑白小猪的猪屎。", 5.5, 9.4)]
    found = rac.copies(cues, [{"t_s": -170.0, "text": sc, "type": "sc"}], [["他们提的是黑白小猪的猪食"]])
    assert [(c["written"], c["record_has"]) for c in found] == [("屎", "食")]


def test_only_records_she_can_be_reading_count():
    later = {"t_s": 33.0, "text": "冲b币换电池"}            # sent after the sentence
    too_early = {"t_s": 30.0 - 91, "text": "冲b币换电池"}     # a comment beyond its 90 s
    gift = {"t_s": 20.0, "text": "冲b币换电池", "type": "gift"}
    assert rac.copies([cue("充b币换电池", 30.0, 32.0)], [later, too_early, gift]) == []


def test_a_short_coincidence_with_a_long_record_does_not_count():
    long_record = "我觉得那个很好，但是今天先不说了，我们下次再聊吧"
    assert rac.copies([cue("我觉得这个很好", 30.0, 32.0)], [{"t_s": 20.0, "text": long_record}]) == []


def test_two_records_that_disagree_leave_the_character_as_written():
    rows = [{"t_s": 20.0, "text": "冲b币换电池"}, {"t_s": 21.0, "text": "宠b币换电池"}]
    assert rac.copies([cue("充b币换电池", 30.0, 32.0)], rows) == []


def test_settle_rewrites_the_decided_subtitles_and_records_it(tmp_path):
    (tmp_path / "subs").mkdir()
    (tmp_path / "danmaku").mkdir()
    (tmp_path / "subs/final.srt").write_text(st.format_blocks([cue("像在陪主包唱南酮歌，\n就不想了（")], labels=True),
                                             encoding="utf-8")
    (tmp_path / "danmaku/lines.json").write_text(json.dumps([{"t_s": 120.0, "text": SC, "type": "sc"}]),
                                                 encoding="utf-8")
    (tmp_path / "asr").mkdir()
    (tmp_path / "asr/bcut.srt").write_text(st.format_blocks([cue("像在陪主包唱难听歌", 139.0, 143.0)], labels=False),
                                           encoding="utf-8")
    process: list[str] = []
    rac.settle(tmp_path, process)
    assert "南町歌" in (tmp_path / "subs/final.srt").read_text(encoding="utf-8")
    assert "「酮」→「町」" in process[0]
    assert json.loads((tmp_path / "subs/read_aloud_copy.json").read_text(encoding="utf-8"))[0]["record_has"] == "町"
