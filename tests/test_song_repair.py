import json
from pathlib import Path

from src.autoslice.review_evidence import SourceCue
from src.autoslice.song_repair import (
    LrcLine,
    LrcResult,
    _build_lyric_queries,
    attempt_song_repair,
    parse_lrc_text,
)


def test_build_lyric_queries_prefers_clean_lines_over_longest():
    """Ivan 2026-07-06 《屑屑》: the longest ASR lines are the English/rap parts
    BCUT mangles ("chewe now baby just chewe now") which find nothing on netease;
    a clean CJK-dense line finds the song.  Query selection must issue the clean
    distinctive lines, not just the longest."""
    cues = [
        SourceCue("c1", 1000, 4000, "chewe now baby just chewe now", kind="singing"),   # longest, garbled
        SourceCue("c2", 5000, 8000, "just did the chely now给整片银河系", kind="singing"),  # long, garbled
        SourceCue("c3", 9000, 12000, "谁说圆满的人生才能算圆满", kind="singing"),          # clean distinctive
        SourceCue("c4", 13000, 16000, "简直简直忍不住想要为自己喝彩", kind="singing"),      # clean distinctive
        SourceCue("c5", 17000, 20000, "嗯", kind="singing"),                              # too short
    ]
    queries = _build_lyric_queries(cues, 0, 21_000)
    # the clean distinctive lines are issued as their OWN queries
    assert "谁说圆满的人生才能算圆满" in queries
    assert "简直简直忍不住想要为自己喝彩" in queries
    # ranked ahead of the garbled English line (clean lines score higher)
    assert queries.index("谁说圆满的人生才能算圆满") < queries.index("chewe now baby just chewe now")
    # the sub-6-char cue is never a query
    assert "嗯" not in queries


def _song_cues(start_ms: int = 50_000) -> list[SourceCue]:
    lyrics = [
        "憧憬一生 竹马组你终相守",
        "常叹一生未曾有此从容",
        "江湖难测 侠骨柔情红颜梦",
        "沧桑了谁人的眼眸",
        "还有多少痛 埋藏在心中",
        "只为一人从容 无求孤身闯万重",
    ]
    cues = []
    cursor = start_ms
    for index, text in enumerate(lyrics):
        cues.append(
            SourceCue(
                cue_id=f"cue-{index}",
                source_start_ms=cursor,
                source_end_ms=cursor + 6_000,
                text=text,
                kind="singing",
            )
        )
        cursor += 7_000
    return cues


def _matching_lrc() -> LrcResult:
    lines = [
        "憧憬一生 竹马组你终相守",
        "常叹一生未曾有此从容",
        "江湖难测 侠骨柔情红颜梦",
        "沧桑了谁人的眼眸",
        "还有多少痛 埋藏在心中",
        "只为一人从容 无求孤身闯万重",
    ]
    return LrcResult(
        provider="fake",
        song_title="侠客行",
        artist="测试歌手",
        source_ref="fake://song/1",
        # same 7s line pacing as the performed cues: a real matching LRC keeps
        # roughly the performance tempo, which the global-shift model requires
        lines=tuple(LrcLine(time_ms=index * 7_000, text=text) for index, text in enumerate(lines)),
    )


def test_repair_succeeds_and_emits_hashable_alignment_proof(tmp_path):
    result = attempt_song_repair(
        candidate_id="song-ok",
        cues=_song_cues(),
        anchor_start_ms=60_000,
        anchor_end_ms=80_000,
        source_duration_ms=300_000,
        output_dir=tmp_path,
        lrc_provider=lambda query: _matching_lrc(),
    )

    assert result.repaired is True
    assert result.song_boundary["status"] == "FULL_SONG_READY"
    alignment = result.lyrics_alignment
    assert alignment["status"] == "READY"
    assert alignment["provider"] == "fake"
    report_path = Path(str(alignment["alignment_report_path"]))
    assert report_path.is_file()
    import hashlib

    assert hashlib.sha256(report_path.read_bytes()).hexdigest() == alignment["alignment_report_sha256"]
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["matched_line_ratio"] >= 0.8
    # the burned subtitle timeline consumes this: one global shift, LRC truth
    assert report["offset_ms"] == 50_000
    assert alignment["offset_ms"] == 50_000
    assert [line["lrc_time_ms"] for line in report["lyric_lines"]] == [0, 7_000, 14_000, 21_000, 28_000, 35_000]
    # clip covers the whole performance with pre/post roll
    assert result.song_boundary["clip_start_ms"] <= 50_000
    assert result.song_boundary["clip_end_ms"] >= 91_000


def test_repair_without_provider_records_skip_and_does_not_repair(tmp_path):
    result = attempt_song_repair(
        candidate_id="song-noprov",
        cues=_song_cues(),
        anchor_start_ms=60_000,
        anchor_end_ms=80_000,
        source_duration_ms=300_000,
        output_dir=tmp_path,
        lrc_provider=None,
    )

    assert result.repaired is False
    steps = {attempt.step: attempt.status for attempt in result.attempts}
    assert steps["lrc_discovery"] == "SKIPPED"
    assert Path(str(result.report_path)).is_file()


def test_repair_fails_closed_when_song_head_is_not_in_source(tmp_path):
    # LRC has two extra opening lines that were never performed in the window:
    # the capture started mid-song, so a complete-song slice is impossible.
    lrc = _matching_lrc()
    head_lines = (
        LrcLine(time_ms=0, text="第一句从未被录到的开场歌词啊啊"),
        LrcLine(time_ms=10_000, text="第二句也没有被录到的桥段哦哦"),
    )
    lrc = LrcResult(
        provider="fake",
        song_title=lrc.song_title,
        artist=lrc.artist,
        source_ref=lrc.source_ref,
        lines=head_lines + lrc.lines,
    )

    result = attempt_song_repair(
        candidate_id="song-headless",
        cues=_song_cues(),
        anchor_start_ms=60_000,
        anchor_end_ms=80_000,
        source_duration_ms=300_000,
        output_dir=tmp_path,
        lrc_provider=lambda query: lrc,
    )

    assert result.repaired is False
    failure = [a for a in result.attempts if a.step == "song_completeness"]
    assert failure and failure[0].status == "FAILED"
    assert "head missing" in failure[0].detail


def test_repair_fails_when_lyrics_do_not_match_performance(tmp_path):
    wrong_lrc = LrcResult(
        provider="fake",
        song_title="完全不同的歌",
        artist=None,
        source_ref="fake://song/2",
        lines=tuple(LrcLine(time_ms=i * 15_000, text=f"毫不相关的歌词内容第{i}句真的完全不一样") for i in range(8)),
    )

    result = attempt_song_repair(
        candidate_id="song-mismatch",
        cues=_song_cues(),
        anchor_start_ms=60_000,
        anchor_end_ms=80_000,
        source_duration_ms=300_000,
        output_dir=tmp_path,
        lrc_provider=lambda query: wrong_lrc,
    )

    assert result.repaired is False
    failure = [a for a in result.attempts if a.step == "lyrics_alignment"]
    assert failure and failure[0].status == "FAILED"


def test_repair_fails_when_lrc_matches_cues_in_non_monotonic_order(tmp_path):
    cues = [
        SourceCue("cue-1", 10_000, 12_000, "前一句歌词", kind="singing"),
        SourceCue("cue-2", 30_000, 32_000, "后一句歌词", kind="singing"),
    ]
    # Search can return a wrong/reordered LRC whose individual lines match well,
    # but the resulting full-song boundary would run backwards. That is not a
    # valid proof and must fail closed instead of emitting FULL_SONG_READY.
    non_monotonic_lrc = LrcResult(
        provider="fake",
        song_title="顺序错的歌",
        artist=None,
        source_ref="fake://song/non-monotonic",
        lines=(
            LrcLine(time_ms=0, text="后一句歌词"),
            LrcLine(time_ms=10_000, text="前一句歌词"),
        ),
    )

    result = attempt_song_repair(
        candidate_id="song-non-monotonic",
        cues=cues,
        anchor_start_ms=0,
        anchor_end_ms=40_000,
        source_duration_ms=40_000,
        output_dir=tmp_path,
        lrc_provider=lambda query: non_monotonic_lrc,
        min_matched_ratio=0.9,
    )

    assert result.repaired is False
    assert result.song_boundary is None
    failure = [a for a in result.attempts if a.step == "global_shift_check"]
    assert failure and failure[0].status == "FAILED"


def test_repeated_chorus_lines_align_to_their_own_occurrences(tmp_path):
    # Greedy per-line matching mapped every occurrence of an identical chorus
    # line to the same single cue, which then failed the global-shift check
    # even for a genuinely complete performance (real 《雨天》 capture failure).
    # The monotonic DP alignment must give each occurrence its own cue.
    verse = ["第一段主歌歌词内容在这里", "第二段主歌歌词内容在这里"]
    chorus = ["副歌这一句每次都一模一样", "副歌第二句也每次一模一样"]
    lyric_texts = verse + chorus + ["间奏后的第三段主歌歌词"] + chorus
    cues = []
    cursor = 40_000
    for index, text in enumerate(lyric_texts):
        cues.append(SourceCue(f"cue-{index}", cursor, cursor + 5_000, text, kind="singing"))
        cursor += 6_000
    lrc = LrcResult(
        provider="fake",
        song_title="副歌歌",
        artist=None,
        source_ref="fake://song/chorus",
        lines=tuple(LrcLine(time_ms=index * 6_000, text=text) for index, text in enumerate(lyric_texts)),
    )

    result = attempt_song_repair(
        candidate_id="song-chorus",
        cues=cues,
        anchor_start_ms=50_000,
        anchor_end_ms=70_000,
        source_duration_ms=200_000,
        output_dir=tmp_path,
        lrc_provider=lambda query: lrc,
    )

    assert result.repaired is True
    report = json.loads(Path(str(result.lyrics_alignment["alignment_report_path"])).read_text(encoding="utf-8"))
    matched_cues = [entry["matched_cue_id"] for entry in report["alignment"] if entry["matched_cue_id"]]
    # every line matched, and no cue was reused by the repeated chorus
    assert len(matched_cues) == len(lyric_texts)
    assert len(set(matched_cues)) == len(matched_cues)


def test_repair_fails_when_many_lrc_lines_pile_onto_one_cue(tmp_path):
    # Regression for the 生日歌 false FULL_SONG_READY: four repeated LRC lines
    # spanning 12s+ all greedy-matched the SAME 4s cue, and with a fake tail
    # match the 28s capture was promoted to a "complete" multi-minute song.
    cues = [
        SourceCue("cue-head", 500, 4_500, "祝你生日快乐祝你生日快乐", kind="singing"),
        SourceCue("cue-mid", 15_000, 19_000, "呀呼谢谢大家来看直播哦", kind="singing"),
        SourceCue("cue-tail", 24_000, 28_000, "祝你生日快乐永远快乐", kind="singing"),
    ]
    lines = [LrcLine(time_ms=3_100 + i * 3_900, text="祝你生日快乐") for i in range(4)]
    lines += [
        LrcLine(time_ms=31_960, text="今天你生日"),
        LrcLine(time_ms=33_840, text="送上我祝福"),
        LrcLine(time_ms=90_000, text="祝福你好运常伴"),
        LrcLine(time_ms=150_000, text="祝你生日快乐"),
        LrcLine(time_ms=155_000, text="祝你生日快乐"),
        LrcLine(time_ms=160_000, text="祝你生日快乐"),
    ]
    birthday_lrc = LrcResult(
        provider="fake",
        song_title="生日祝福歌",
        artist=None,
        source_ref="fake://song/birthday",
        lines=tuple(lines),
    )

    result = attempt_song_repair(
        candidate_id="song-birthday-fake-full",
        cues=cues,
        anchor_start_ms=0,
        anchor_end_ms=28_000,
        source_duration_ms=90_000,
        output_dir=tmp_path,
        lrc_provider=lambda query: birthday_lrc,
    )

    assert result.repaired is False
    assert result.song_boundary is None


def test_provider_exception_is_recorded_not_raised(tmp_path):
    def broken_provider(query):
        raise RuntimeError("network down")

    result = attempt_song_repair(
        candidate_id="song-neterr",
        cues=_song_cues(),
        anchor_start_ms=60_000,
        anchor_end_ms=80_000,
        source_duration_ms=300_000,
        output_dir=tmp_path,
        lrc_provider=broken_provider,
    )

    assert result.repaired is False
    failure = [a for a in result.attempts if a.step == "lrc_discovery"]
    assert failure and failure[0].status == "FAILED"
    assert "network down" in failure[0].detail


def test_parse_lrc_text_skips_metadata_and_sorts():
    # trailing credits with mid-head keywords (音乐制作/贝斯演奏/混音、母带) are the
    # real 《屑屑》 lines that got burned over the outro — must be dropped too, not
    # just the exact-prefix 作词/作曲 ones.
    lrc = (
        "[00:01.00]作词 : 某人\n"
        "[03:45.00]音乐制作 : ChiliChill乐团\n"
        "[03:47.00]贝斯演奏 : 李彦希\n"
        "[03:48.00]混音、母带 : ChiliChill乐团\n"
        "[00:12.50]第二句\n[00:02.00]第一句\n[junk]\n"
    )
    lines = parse_lrc_text(lrc)
    assert [line.text for line in lines] == ["第一句", "第二句"]
    assert lines[0].time_ms == 2_000
    assert lines[1].time_ms == 12_500


def test_rerank_picks_correct_song_among_candidates_ignoring_search_order(tmp_path):
    wrong = LrcResult(
        provider="fake",
        song_title="搜索排名第一但不对的歌",
        artist=None,
        source_ref="fake://song/wrong",
        lines=tuple(LrcLine(time_ms=i * 15_000, text=f"完全不相关的第{i}句歌词内容啊") for i in range(10)),
    )
    right = _matching_lrc()

    result = attempt_song_repair(
        candidate_id="song-rerank",
        cues=_song_cues(),
        anchor_start_ms=60_000,
        anchor_end_ms=80_000,
        source_duration_ms=300_000,
        output_dir=tmp_path,
        lrc_provider=lambda query: [wrong, right],  # wrong one ranked first by "search"
    )

    assert result.repaired is True
    assert result.song_boundary["song_title"] == "侠客行"
    per_candidate = [a for a in result.attempts if a.step == "candidate_alignment"]
    assert len(per_candidate) == 2


def test_llm_song_hint_queries_reach_provider_first(tmp_path):
    queries_seen = []

    def provider(query):
        queries_seen.append(query)
        if query == "侠客行 测试歌手":
            return _matching_lrc()
        return None

    def hint_llm(prompt: str) -> str:
        assert "同音错别字" in prompt
        return '{"guesses": [{"title": "侠客行", "artist": "测试歌手"}]}'

    result = attempt_song_repair(
        candidate_id="song-hint",
        cues=_song_cues(),
        anchor_start_ms=60_000,
        anchor_end_ms=80_000,
        source_duration_ms=300_000,
        output_dir=tmp_path,
        lrc_provider=provider,
        hint_llm_call=hint_llm,
    )

    assert result.repaired is True
    assert queries_seen[0] == "侠客行 测试歌手"
    hint_attempts = [a for a in result.attempts if a.step == "llm_song_hint"]
    assert hint_attempts and hint_attempts[0].status == "SUCCESS"


def test_pinned_lrc_repairs_when_search_finds_nothing(tmp_path):
    # Reproduces the real 《屑屑》 failure: LLM hint guessed wrong songs and text
    # search found nothing that aligned, so the song was never identified and the
    # whole complete-song evidence path was skipped.  A caller-pinned LRC (matched
    # by known-song fingerprint) is added to the ranking pool so alignment can
    # still prove it — deterministic, not dependent on flaky discovery.
    result = attempt_song_repair(
        candidate_id="song-pinned",
        cues=_song_cues(),
        anchor_start_ms=60_000,
        anchor_end_ms=80_000,
        source_duration_ms=300_000,
        output_dir=tmp_path,
        lrc_provider=lambda query: None,  # search finds nothing (the real 屑屑 bug)
        pinned_lrc_results=[_matching_lrc()],
    )

    assert result.repaired is True
    assert result.song_boundary["song_title"] == "侠客行"
    pinned = [a for a in result.attempts if a.step == "pinned_lrc"]
    assert pinned and pinned[0].status == "SUCCESS"


def test_outro_kept_up_to_next_talk_cue(tmp_path):
    # Ivan 2026-07-07: a complete song must keep the instrumental 后奏 (outro)
    # after the last sung line, ending before the post-song talk.  The last sung
    # cue ends at 91_000; a 谢谢大家 talk cue starts 15s later — that 15s gap is
    # the outro and must be retained (the old last_lyric+4s post-roll cut it).
    cues = _song_cues() + [
        SourceCue("talk-thanks", 106_000, 110_000, "谢谢大家的礼物哦", kind="talk"),
    ]
    result = attempt_song_repair(
        candidate_id="song-outro",
        cues=cues,
        anchor_start_ms=60_000,
        anchor_end_ms=80_000,
        source_duration_ms=300_000,
        output_dir=tmp_path,
        lrc_provider=lambda query: _matching_lrc(),
    )

    assert result.repaired is True
    clip_end = result.song_boundary["clip_end_ms"]
    # kept the instrumental outro (old post-roll would have stopped near 95_000)…
    assert clip_end > 100_000
    # …but stopped before the post-song talk at 106_000
    assert clip_end <= 106_000


def test_llm_hint_failure_is_recorded_and_text_queries_still_tried(tmp_path):
    def hint_llm(prompt: str) -> str:
        raise RuntimeError("bridge down")

    result = attempt_song_repair(
        candidate_id="song-hint-down",
        cues=_song_cues(),
        anchor_start_ms=60_000,
        anchor_end_ms=80_000,
        source_duration_ms=300_000,
        output_dir=tmp_path,
        lrc_provider=lambda query: _matching_lrc(),
        hint_llm_call=hint_llm,
    )

    assert result.repaired is True  # text queries still found the song
    hint_attempts = [a for a in result.attempts if a.step == "llm_song_hint"]
    assert hint_attempts and hint_attempts[0].status == "FAILED"
