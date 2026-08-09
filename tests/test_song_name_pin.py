"""Deterministic talk-lane song-name pinning and its semantic admission gate."""

from __future__ import annotations

from src.autoslice.song_name_pin import (
    fold_for_similarity,
    pin_song_names_in_srt,
    song_name_candidates_prompt_block,
)
from src.autoslice.song_name_semantic_verification import (
    MappingSongLyricsProvider,
    build_song_name_semantic_verification,
)


def _srt(text: str, *, start: str = "00:00:00,000", end: str = "00:00:03,000") -> str:
    return f"1\n{start} --> {end}\n{text}\n"


def _verification(srt, candidates, songs, evidence_text):
    return build_song_name_semantic_verification(
        srt,
        candidates=candidates,
        evidence_lines=[
            {"source": "title_quote", "cue_index": None, "text": evidence_text}
        ],
        local_provider=MappingSongLyricsProvider(songs),
    )


def test_f19_synthetic_case_lyrics_semantics_overrides_franchise_mishearing():
    quote = "请感受穿越屏幕的热烈,再一次爱上我吧"
    srt = _srt("下一首歌是LoveLive!")
    candidates = ["ラブコード", "LoveLive!"]
    verification = _verification(
        srt,
        candidates,
        {
            "ラブコード": ["请感受穿越屏幕的热烈，再一次爱上我吧"],
            "LoveLive!": ["虚构舞台上的大家一起挥手微笑"],
        },
        quote,
    )
    output, audit = pin_song_names_in_srt(
        srt,
        candidates=candidates,
        semantic_verification=verification,
    )

    assert output == _srt("下一首歌是《ラブコード》!")
    assert len(audit["replacements"]) == 1
    repl = audit["replacements"][0]
    assert repl["candidate"] == "ラブコード"
    assert repl["matched_span"] == "LoveLive"
    assert repl["surface_candidate"] == "LoveLive!"
    assert repl["decision_surface"] == "lyrics_semantic_override"
    assert repl["combined_confidence"] > repl["ratio"]
    verdicts = {row["candidate"]: row["verdict"] for row in verification["candidate_results"]}
    assert verdicts == {"ラブコード": "MATCH", "LoveLive!": "DISPUTED"}
    assert audit["output_srt_sha256"] != audit["input_srt_sha256"]


def test_f19_semantic_gate_also_corrects_an_advisory_llm_titled_candidate():
    quote = "请感受穿越屏幕的热烈,再一次爱上我吧"
    srt = _srt("下一首歌是《LoveLive!》")
    candidates = ["ラブコード", "LoveLive!"]
    verification = _verification(
        srt,
        candidates,
        {
            "ラブコード": ["请感受穿越屏幕的热烈，再一次爱上我吧"],
            "LoveLive!": ["虚构舞台上的大家一起挥手微笑"],
        },
        quote,
    )

    output, audit = pin_song_names_in_srt(
        srt,
        candidates=candidates,
        semantic_verification=verification,
    )

    assert output == _srt("下一首歌是《ラブコード》")
    assert audit["replacements"][0]["matched_span"] == "LoveLive!"
    assert audit["replacements"][0]["surface_candidate"] == "LoveLive!"


def test_no_intent_phrase_is_a_no_op():
    """A cue with no song-mention intent phrase must never be touched, even if
    it happens to contain text that fuzzy-matches a candidate title."""

    srt = _srt("今天聊到了爱拉拉爱这个梗")
    verification = _verification(srt, ["虚构星光码"], {"虚构星光码": ["穿越屏幕的热烈光芒"]}, "穿越屏幕的热烈光芒")
    output, audit = pin_song_names_in_srt(
        srt, candidates=["虚构星光码"], semantic_verification=verification
    )

    assert output == srt
    assert audit["replacements"] == []


def test_low_similarity_tail_is_a_no_op():
    """'下一首歌还没想好' must not be forced into any candidate title — this is
    the explicit false-positive risk the task calls out."""

    srt = _srt("下一首歌还没想好呢")
    candidates = ["虚构星光码", "虚构夜航曲"]
    verification = _verification(
        srt, candidates, {"虚构星光码": ["穿越屏幕的热烈光芒"], "虚构夜航曲": ["深夜启航"]}, "穿越屏幕的热烈光芒"
    )
    output, audit = pin_song_names_in_srt(
        srt, candidates=candidates, semantic_verification=verification
    )

    assert output == srt
    assert audit["replacements"] == []


def test_already_titled_span_is_a_no_op():
    srt = _srt("下一首歌是《爱啦啦》")
    verification = _verification(srt, ["爱啦啦"], {"爱啦啦": ["虚构歌词证据足够长"]}, "虚构歌词证据足够长")
    output, audit = pin_song_names_in_srt(
        srt, candidates=["爱啦啦"], semantic_verification=verification
    )

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
    srt = _srt("点歌虚构星光码")
    candidates = ["虚构夜航曲", "虚构星光码"]
    verification = _verification(
        srt,
        candidates,
        {"虚构夜航曲": ["深夜启航去远方"], "虚构星光码": ["穿越屏幕的热烈光芒"]},
        "穿越屏幕的热烈光芒",
    )
    output, audit = pin_song_names_in_srt(
        srt, candidates=candidates, semantic_verification=verification
    )

    assert "《虚构星光码》" in output
    assert len(audit["replacements"]) == 1


def test_missing_semantic_verification_canary_blocks_an_otherwise_strong_pin():
    """Canary: removing/bypassing F19 makes the old high-ratio mutation happen."""

    srt = _srt("点歌虚构星光码")
    output, audit = pin_song_names_in_srt(srt, candidates=["虚构星光码"])

    assert output == srt
    assert audit["semantic_gate_status"] == "BLOCKED_MISSING_VERIFICATION"
    assert audit["replacements"] == []


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
