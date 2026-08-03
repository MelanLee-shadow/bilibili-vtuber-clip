"""Deterministic talk-lane song-name pinning (维护者).

Verified delivery bug: ``下一首歌是爱拉拉爱`` shipped instead of
下一首歌是《爱啦啦》 with zero song-name context available to the correction
lanes.  These tests lock the real case plus the required no-op guards.
"""

from __future__ import annotations

from src.autoslice.song_name_pin import (
    MIN_REPLACE_RATIO,
    fold_for_similarity,
    pin_song_names_in_srt,
    song_name_candidates_prompt_block,
)


def _srt(text: str, *, start: str = "00:00:00,000", end: str = "00:00:03,000") -> str:
    return f"1\n{start} --> {end}\n{text}\n"


def test_real_case_pins_the_screen_songlist_title():
    srt = _srt("下一首歌是爱拉拉爱")
    output, audit = pin_song_names_in_srt(srt, candidates=["爱啦啦"])

    assert output == _srt("下一首歌是《爱啦啦》")
    assert len(audit["replacements"]) == 1
    repl = audit["replacements"][0]
    assert repl["candidate"] == "爱啦啦"
    assert repl["matched_span"] == "爱拉拉爱"
    assert repl["ratio"] >= MIN_REPLACE_RATIO
    assert audit["output_srt_sha256"] != audit["input_srt_sha256"]


def test_no_intent_phrase_is_a_no_op():
    """A cue with no song-mention intent phrase must never be touched, even if
    it happens to contain text that fuzzy-matches a candidate title."""

    srt = _srt("今天聊到了爱拉拉爱这个梗")
    output, audit = pin_song_names_in_srt(srt, candidates=["爱啦啦"])

    assert output == srt
    assert audit["replacements"] == []


def test_low_similarity_tail_is_a_no_op():
    """'下一首歌还没想好' must not be forced into any candidate title — this is
    the explicit false-positive risk the task calls out."""

    srt = _srt("下一首歌还没想好呢")
    output, audit = pin_song_names_in_srt(srt, candidates=["爱啦啦", "屑屑", "芽吹くとき"])

    assert output == srt
    assert audit["replacements"] == []


def test_already_titled_span_is_a_no_op():
    srt = _srt("下一首歌是《爱啦啦》")
    output, audit = pin_song_names_in_srt(srt, candidates=["爱啦啦"])

    assert output == srt
    assert audit["replacements"] == []


def test_one_character_candidate_is_excluded():
    srt = _srt("点歌爱")
    output, audit = pin_song_names_in_srt(srt, candidates=["爱"])

    assert output == srt
    assert audit["candidates_considered"] == 0
    assert audit["replacements"] == []


def test_no_candidates_is_a_no_op():
    srt = _srt("下一首歌是爱拉拉爱")
    output, audit = pin_song_names_in_srt(srt, candidates=[])

    assert output == srt
    assert audit["replacements"] == []
    assert audit["output_srt_sha256"] == audit["input_srt_sha256"]


def test_at_most_one_replacement_per_cue_picks_the_best_candidate():
    srt = _srt("点歌爱啦啦")
    output, audit = pin_song_names_in_srt(srt, candidates=["屑屑", "爱啦啦"])

    assert "《爱啦啦》" in output
    assert len(audit["replacements"]) == 1


def test_fold_for_similarity_normalizes_common_asr_confusables():
    assert fold_for_similarity("爱啦啦") == fold_for_similarity("爱拉拉")


def test_prompt_block_wording_and_cap():
    block = song_name_candidates_prompt_block(["爱啦啦", "屑屑"])
    assert "当场歌单/点歌候选歌名" in block
    assert "不得听写生造歌名" in block
    assert "爱啦啦" in block and "屑屑" in block

    capped = song_name_candidates_prompt_block([f"曲{i}" for i in range(30)], max_items=5)
    listed_line = capped.strip().splitlines()[-1]
    assert len([name for name in listed_line.split("、") if name.strip()]) == 5


def test_prompt_block_empty_for_no_candidates():
    assert song_name_candidates_prompt_block([]) == ""
