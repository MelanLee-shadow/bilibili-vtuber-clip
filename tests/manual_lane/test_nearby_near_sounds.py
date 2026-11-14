"""A character one sound apart from what the cues around it keep saying is a hint for the subtitle agent (owner
「你敢吗你敢吗」 was 「你改嘛你改嘛」 in a stretch about changing an ID)."""

from __future__ import annotations

from src.autoslice.manual_lane import playbook_evidence as pe

ID_STRETCH = ["那是kmx要我改的。", "他让我改了我就改了", "那小李还不是kmx说什么就是什么。", "我哪能主动帮kmx改ID呀。",
              "这不都是kmx说什么我就做什么啦。", "kmx说：小李，你帮我改个ID吧。", "小李就说：啊，这样不好吧。",
              "但kmx说：你敢吗？你敢吗？", "然后小李就三推四让。", "既然kmx让小李改，那小李就改吧。", "然后我就改了"]


def _run(texts):
    return pe.nearby_near_sounds({i: [t] for i, t in enumerate(texts)}, list(range(len(texts))))


def test_the_word_the_stretch_keeps_saying_is_offered_for_the_one_sound_apart_character():
    found = _run(ID_STRETCH)
    assert found[7] == [{"written": "敢", "nearby_word": "改", "nearby_cues": [0, 1, 3, 5, 9, 10]}]


def test_no_hint_when_the_sentence_already_writes_the_word_or_the_tone_differs_or_it_is_said_too_rarely():
    with_word = [*ID_STRETCH[:7], "但kmx说：你敢改吗？", *ID_STRETCH[8:]]
    assert 7 not in _run(with_word)                       # 改 is in the sentence itself
    other_tone = [t.replace("改", "该") for t in ID_STRETCH]   # gai1: same letters, another tone
    assert 7 not in _run(other_tone)
    rare = [t if i in (0, 7) else "嗯嗯好的" for i, t in enumerate(ID_STRETCH)]
    assert _run(rare) == {}


def test_every_recogniser_reading_of_a_cue_counts_and_common_characters_never_do():
    readings = {i: [t, t] for i, t in enumerate(ID_STRETCH)}
    readings[7] = ["但kmx说：你敢吗？", "但kmx说：你干嘛？"]   # 干 gan4: not one sound apart from 改 gai3
    found = pe.nearby_near_sounds(readings, list(range(len(ID_STRETCH))))
    assert [row["written"] for row in found[7]] == ["敢"]
    assert not any(row["nearby_word"] in pe.COMMON_CHARS for rows in found.values() for row in rows)
