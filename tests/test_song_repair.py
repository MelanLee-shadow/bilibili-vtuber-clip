import json
import hashlib
from pathlib import Path

import pytest

import src.autoslice.song_repair as song_repair
from src.autoslice.review_evidence import SourceCue
from src.autoslice.song_repair import (
    AudioLrcAlignmentRun,
    LrcLine,
    LrcResult,
    _choose_audio_lrc_candidate,
    _build_lyric_queries,
    attempt_song_repair,
    build_composite_lrc_provider,
    build_lrclib_lrc_provider,
    fetch_lrclib_lrc,
    live_performance_failure_reason_codes,
    parse_lrc_text,
)


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        ("ORIGINAL_OR_BACKGROUND_PLAYBACK", ("SONG_BACKGROUND_PLAYBACK_ONLY", "SONG_NOT_LIDOUSHA_SINGING")),
        ("STREAMER_TALKING_OVER_MUSIC", ("SONG_BACKGROUND_PLAYBACK_ONLY", "SONG_NOT_LIDOUSHA_SINGING")),
        ("OTHER_SINGER", ("SONG_NOT_LIDOUSHA_SINGING",)),
        ("AMBIGUOUS", ("SONG_LIVE_PERFORMANCE_UNPROVEN",)),
    ],
)
def test_live_performance_failure_reason_codes_are_specific(mode, expected):
    assert live_performance_failure_reason_codes({"mode": mode}) == expected


def _write_fake_audio_alignment_run(tmp_path: Path, lrc: LrcResult, *, candidate_id: str = "jp-audio") -> AudioLrcAlignmentRun:
    source = tmp_path / "current-full-window.mp4"
    source.write_bytes(b"current-audio-bound-media")
    source_sha = hashlib.sha256(source.read_bytes()).hexdigest()
    lrc_path = tmp_path / "source.lrc"
    lrc_path.write_text(
        "".join(
            f"[{line.time_ms // 60000:02d}:{(line.time_ms % 60000) // 1000:02d}.{line.time_ms % 1000:03d}]{line.text}\n"
            for line in lrc.lines
        ),
        encoding="utf-8",
    )
    lrc_sha = hashlib.sha256(lrc_path.read_bytes()).hexdigest()
    observations = []
    for index, line in enumerate(lrc.lines):
        start = line.time_ms + 10_000
        observations.append(
            {
                "lrc_index": index,
                "lrc_time_ms": line.time_ms,
                "text": line.text,
                "heard": True,
                "live_start_ms": start,
                "live_end_ms": start + 3_000,
                "confidence": 0.98,
            }
        )
    first_live_ms = observations[0]["live_start_ms"]
    last_live_ms = observations[-1]["live_end_ms"]
    live_span_ms = last_live_ms - first_live_ms
    payload = {
        "schema_version": "agy-audio-lrc-observation.v2",
        "record": {
            "attempt_id": "attempt-test",
            "candidate_id": candidate_id,
            "source_sha256": source_sha,
            "lrc_sha256": lrc_sha,
            "source_duration_ms": 100_000,
        },
        "observations": observations,
        "spot_checks": [
            {"name": "first_line", "live_time_ms": 10_000, "result": "OK", "notes": "heard"},
            {"name": "chorus", "live_time_ms": 24_000, "result": "OK", "notes": "heard"},
            {
                "name": "repeated_section",
                "live_time_ms": observations[8]["live_start_ms"],
                "result": "OK",
                "notes": "heard later recurrence",
            },
            {"name": "longest_instrumental_gap", "live_time_ms": 52_000, "result": "OK", "notes": "heard"},
            {"name": "tail", "live_time_ms": observations[-1]["live_start_ms"], "result": "OK", "notes": "heard"},
        ],
        "live_performance": {
            "mode": "LIVE_STREAMER_SINGING",
            "confidence": 0.96,
            "continuous_singing": True,
            "background_recording_likelihood": 0.03,
            "evidence": [
                {"time_ms": first_live_ms + live_span_ms // 6, "observation": "live vocal at head"},
                {"time_ms": first_live_ms + live_span_ms // 2, "observation": "live vocal at middle"},
                {"time_ms": first_live_ms + live_span_ms * 5 // 6, "observation": "live vocal at tail"},
            ],
            "notes": "continuous live streamer vocal",
        },
        "post_song_talk_start_ms": observations[-1]["live_end_ms"] + 2_000,
    }
    prompt = tmp_path / "prompt.md"
    prompt.write_text("strict test prompt\n", encoding="utf-8")
    output = tmp_path / "alignment.json"
    output.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    manifest = tmp_path / "run.manifest.json"
    prompt_sha = hashlib.sha256(prompt.read_bytes()).hexdigest()
    output_sha = hashlib.sha256(output.read_bytes()).hexdigest()
    manifest.write_text(
        json.dumps(
            {
                "schema_version": "agy-audio-lrc-run.v1",
                "candidate_id": candidate_id,
                "provider": "agy",
                "model": "Gemini 3.5 Flash (High)",
                "agy_rc": 0,
                "provider_fallback_used": False,
                "sandbox": True,
                "artifacts": {
                    "source_path": str(source),
                    "source_sha256": source_sha,
                    "source_duration_ms": 100_000,
                    "lrc_path": str(lrc_path),
                    "lrc_sha256": lrc_sha,
                    "prompt_path": str(prompt),
                    "prompt_sha256": prompt_sha,
                    "output_path": str(output),
                    "output_sha256": output_sha,
                },
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )

    def sha(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    return AudioLrcAlignmentRun(
        payload=payload,
        provider="agy",
        model="Gemini 3.5 Flash (High)",
        rc=0,
        provider_fallback_used=False,
        source_path=str(source),
        source_sha256=source_sha,
        source_duration_ms=100_000,
        lrc_path=str(lrc_path),
        lrc_sha256=lrc_sha,
        prompt_path=str(prompt),
        prompt_sha256=prompt_sha,
        output_path=str(output),
        output_sha256=output_sha,
        manifest_path=str(manifest),
        manifest_sha256=sha(manifest),
    )


def _japanese_lrc() -> LrcResult:
    texts = [
        "めいっぱい背伸びしてきたけど",
        "君の前じゃ上手くできない",
        "炭酸が抜けるより早いスピードで",
        "変わっていく気持ち",
        "まだ自分でも気づいてない",
        "最初に望んだ未来とは少し違うけれど",
        "最後はなにもいらないただそばにいて",
        "季節が進むことをためらわないでね",
        "最初に望んだ未来とは少し違うけれど",
        "伝えなくちゃ最後は",
    ]
    return LrcResult(
        provider="lrclib",
        song_title="芽吹くとき",
        artist="yonige",
        source_ref="https://lrclib.net/api/get/33542202",
        lines=tuple(LrcLine(index * 7_000, text) for index, text in enumerate(texts)),
    )


def test_audio_identity_collapses_same_song_provider_variants_and_prefers_lrclib():
    canonical = _japanese_lrc()
    netease_variant = LrcResult(
        provider="netease",
        song_title=canonical.song_title,
        artist=canonical.artist,
        source_ref="netease://song/3363527827",
        lines=tuple(LrcLine(line.time_ms + 10, line.text) for line in canonical.lines),
    )
    unrelated = LrcResult(
        provider="lrclib",
        song_title="だからね",
        artist="別の歌手",
        source_ref="https://lrclib.net/api/get/1",
        lines=tuple(LrcLine(line.time_ms, f"別の歌詞{index}") for index, line in enumerate(canonical.lines)),
    )
    translated_title_alias = LrcResult(
        provider="lrclib",
        song_title="芽吹くとき - Blooming With you",
        artist=canonical.artist,
        source_ref="https://lrclib.net/api/get/33851710",
        lines=canonical.lines,
    )
    chosen = _choose_audio_lrc_candidate(
        [
            (0.28, netease_variant, []),
            (0.28, canonical, []),
            (0.28, translated_title_alias, []),
            (0.09, unrelated, []),
        ],
        pinned_lrc_results=(),
        min_recall_ratio=0.20,
        min_margin=0.08,
    )
    assert chosen.source_ref == "https://lrclib.net/api/get/33542202"


def test_sparse_japanese_asr_escalates_current_audio_and_mints_bound_proof(tmp_path):
    lrc = _japanese_lrc()
    run = _write_fake_audio_alignment_run(tmp_path, lrc)
    cues = [
        SourceCue("jp-0", 10_000, 13_000, lrc.lines[0].text, kind="singing"),
        SourceCue("jp-1", 17_000, 20_000, lrc.lines[1].text, kind="singing"),
    ]
    calls = []

    def aligner(media, chosen_lrc, candidate_id, output_dir):
        calls.append((media, chosen_lrc, candidate_id, output_dir))
        return run

    result = attempt_song_repair(
        candidate_id="jp-audio",
        cues=cues,
        anchor_start_ms=10_000,
        anchor_end_ms=20_000,
        source_duration_ms=100_000,
        output_dir=tmp_path / "repair",
        lrc_provider=lambda _query: lrc,
        source_media_path=Path(run.source_path),
        audio_lrc_aligner=aligner,
    )

    assert len(calls) == 1
    assert result.repaired is True
    assert result.song_boundary["status"] == "FULL_SONG_READY"
    assert result.song_boundary["nominal_lrc_zero_ms"] == 10_000
    assert result.lyrics_alignment["model"] == "lrclib-agy-audio-lrc-global-shift-v1"
    report = json.loads(Path(result.lyrics_alignment["alignment_report_path"]).read_text(encoding="utf-8"))
    assert report["evidence_source"] == "agy_audio_lrc"
    assert report["matched_line_count"] == report["line_count"] == 10
    assert all(row["evidence_source"] == "agy_audio_lrc" for row in report["alignment"])
    assert report["spot_checks"] == run.payload["spot_checks"]
    assert report["live_performance"] == run.payload["live_performance"]
    assert report["post_song_talk_start_ms"] == run.payload["post_song_talk_start_ms"]


@pytest.mark.parametrize(
    "mutation",
    ["unheard", "drift", "wrong_text", "bad_tail_spot", "bad_repeated_spot", "background_playback"],
)
def test_audio_lrc_alignment_mutations_fail_closed(tmp_path, mutation):
    lrc = _japanese_lrc()
    run = _write_fake_audio_alignment_run(tmp_path, lrc)
    payload = json.loads(json.dumps(run.payload))
    if mutation == "unheard":
        payload["observations"][3].update(heard=False, live_start_ms=None, live_end_ms=None)
    elif mutation == "drift":
        payload["observations"][-1]["live_start_ms"] += 8_000
        payload["observations"][-1]["live_end_ms"] += 8_000
        payload["spot_checks"][-1]["live_time_ms"] += 8_000
        payload["post_song_talk_start_ms"] += 8_000
    elif mutation == "wrong_text":
        payload["observations"][2]["text"] = "別の歌詞"
    elif mutation == "bad_tail_spot":
        payload["spot_checks"][-1]["live_time_ms"] = 20_000
    elif mutation == "bad_repeated_spot":
        payload["spot_checks"][2]["live_time_ms"] = payload["observations"][5]["live_start_ms"]
    else:
        payload["live_performance"].update(
            mode="ORIGINAL_OR_BACKGROUND_PLAYBACK",
            confidence=0.98,
            continuous_singing=False,
            background_recording_likelihood=0.99,
        )
    output = Path(run.output_path)
    output.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    run = AudioLrcAlignmentRun(
        **{**run.__dict__, "payload": payload, "output_sha256": hashlib.sha256(output.read_bytes()).hexdigest()}
    )
    cues = [
        SourceCue("jp-0", 10_000, 13_000, lrc.lines[0].text, kind="singing"),
        SourceCue("jp-1", 17_000, 20_000, lrc.lines[1].text, kind="singing"),
    ]
    result = attempt_song_repair(
        candidate_id="jp-audio",
        cues=cues,
        anchor_start_ms=10_000,
        anchor_end_ms=20_000,
        source_duration_ms=100_000,
        output_dir=tmp_path / "repair",
        lrc_provider=lambda _query: lrc,
        source_media_path=Path(run.source_path),
        audio_lrc_aligner=lambda *_args: run,
    )
    assert result.repaired is False
    assert any(item.step == "agy_audio_lrc_alignment" and item.status == "FAILED" for item in result.attempts)


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


def test_build_lyric_queries_accepts_clean_japanese_kana_lines():
    cues = [
        SourceCue("jp", 1_000, 4_000, "ただそばにいてほしいの", kind="singing"),
        SourceCue("noise", 5_000, 8_000, "just stay by my sha la la", kind="singing"),
    ]

    queries = _build_lyric_queries(cues, 0, 9_000)

    assert queries[0] == "ただそばにいてほしいの"


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
    assert report["nominal_lrc_zero_ms"] == 50_000
    assert alignment["offset_ms"] == 50_000
    assert alignment["nominal_lrc_zero_ms"] == 50_000
    assert result.song_boundary["nominal_lrc_zero_ms"] == 50_000
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


def test_pinned_lrc_repairs_without_any_search_provider(tmp_path):
    result = attempt_song_repair(
        candidate_id="song-pinned-only",
        cues=_song_cues(),
        anchor_start_ms=60_000,
        anchor_end_ms=80_000,
        source_duration_ms=300_000,
        output_dir=tmp_path,
        lrc_provider=None,
        pinned_lrc_results=[_matching_lrc()],
    )

    assert result.repaired is True
    assert result.lyrics_alignment["provider"] == "fake"
    assert not [a for a in result.attempts if a.step == "lrc_discovery" and a.status == "SKIPPED"]


def test_song_boundary_keeps_lrc_instrumental_intro(tmp_path):
    cues = _song_cues(start_ms=50_000)
    base_lrc = _matching_lrc()
    # Move every LRC timestamp 7.7s later while keeping the live cues fixed,
    # modelling a track with a 7.7-second instrumental intro.
    lrc = LrcResult(
        provider=base_lrc.provider,
        song_title=base_lrc.song_title,
        artist=base_lrc.artist,
        source_ref=base_lrc.source_ref,
        lines=tuple(LrcLine(line.time_ms + 7_700, line.text) for line in base_lrc.lines),
    )
    result = attempt_song_repair(
        candidate_id="song-with-intro",
        cues=cues,
        anchor_start_ms=50_000,
        anchor_end_ms=90_000,
        source_duration_ms=150_000,
        output_dir=tmp_path,
        lrc_provider=None,
        pinned_lrc_results=[lrc],
    )

    assert result.repaired is True
    # global offset = 50_000 - 7_700 = 42_300; the 1.5s safety lead starts
    # before LRC zero, retaining the whole instrumental intro.
    assert result.lyrics_alignment["offset_ms"] == 42_300
    assert result.song_boundary["clip_start_ms"] == 40_800
    assert result.song_boundary["first_lyric_start_ms"] == 50_000


def test_repair_fails_closed_when_nominal_lrc_zero_precedes_source(tmp_path):
    cues = _song_cues(start_ms=5_000)
    base_lrc = _matching_lrc()
    # The source starts after the external track's instrumental intro.  All
    # lyric lines still align perfectly with one global shift, but that shift
    # places LRC time zero at -5s, outside the available source.  Clamping the
    # recut to source 0 would silently certify a headless song as complete.
    lrc = LrcResult(
        provider=base_lrc.provider,
        song_title=base_lrc.song_title,
        artist=base_lrc.artist,
        source_ref=base_lrc.source_ref,
        lines=tuple(LrcLine(line.time_ms + 10_000, line.text) for line in base_lrc.lines),
    )

    result = attempt_song_repair(
        candidate_id="song-lrc-zero-before-source",
        cues=cues,
        anchor_start_ms=5_000,
        anchor_end_ms=30_000,
        source_duration_ms=100_000,
        output_dir=tmp_path,
        lrc_provider=None,
        pinned_lrc_results=[lrc],
    )

    assert result.repaired is False
    assert result.song_boundary is None
    assert result.lyrics_alignment is None
    failures = [attempt for attempt in result.attempts if attempt.step == "song_boundary"]
    assert failures and failures[-1].status == "FAILED"
    assert "nominal LRC zero -5000ms is outside source" in failures[-1].detail


def test_repair_fails_closed_when_nominal_lrc_zero_follows_source(tmp_path):
    cues = _song_cues(start_ms=50_000)
    base_lrc = _matching_lrc()
    # A malformed/pinned timeline can imply LRC zero after the source has
    # already ended.  Text/order matching alone must not turn that into proof.
    lrc = LrcResult(
        provider=base_lrc.provider,
        song_title=base_lrc.song_title,
        artist=base_lrc.artist,
        source_ref=base_lrc.source_ref,
        lines=tuple(LrcLine(line.time_ms - 60_000, line.text) for line in base_lrc.lines),
    )

    result = attempt_song_repair(
        candidate_id="song-lrc-zero-after-source",
        cues=cues,
        anchor_start_ms=50_000,
        anchor_end_ms=80_000,
        source_duration_ms=100_000,
        output_dir=tmp_path,
        lrc_provider=None,
        pinned_lrc_results=[lrc],
    )

    assert result.repaired is False
    assert result.song_boundary is None
    failures = [attempt for attempt in result.attempts if attempt.step == "song_boundary"]
    assert failures and failures[-1].status == "FAILED"
    assert "nominal LRC zero 110000ms is outside source" in failures[-1].detail


def _lrclib_synced_lines(count: int = 8) -> str:
    return "\n".join(f"[00:{index:02d}.00]第{index}句ただそばにいて" for index in range(count))


@pytest.mark.parametrize(
    "song_ref",
    ["33542202", "lrclib://track/33542202", "https://lrclib.net/api/get/33542202"],
)
def test_fetch_lrclib_lrc_accepts_numeric_and_canonical_ref(monkeypatch, song_ref):
    urls = []

    def fake_http_json(url, *, timeout_seconds):
        urls.append((url, timeout_seconds))
        return {
            "id": 33542202,
            "trackName": "芽吹くとき",
            "artistName": "yonige",
            "syncedLyrics": _lrclib_synced_lines(),
        }

    monkeypatch.setattr(song_repair, "_http_json", fake_http_json)

    result = fetch_lrclib_lrc(song_ref, timeout_seconds=2.5)

    assert result is not None
    assert result.provider == "lrclib"
    assert result.source_ref == "https://lrclib.net/api/get/33542202"
    assert result.song_title == "芽吹くとき"
    assert result.artist == "yonige"
    assert len(result.lines) == 8
    assert urls == [("https://lrclib.net/api/get/33542202", 2.5)]


@pytest.mark.parametrize(
    "payload",
    [
        {"id": 33542202, "trackName": "plain only", "plainLyrics": "lyrics", "syncedLyrics": None},
        {"id": 33542202, "trackName": "too short", "syncedLyrics": _lrclib_synced_lines(7)},
    ],
)
def test_fetch_lrclib_lrc_rejects_missing_or_short_synced_lyrics(monkeypatch, payload):
    monkeypatch.setattr(song_repair, "_http_json", lambda url, *, timeout_seconds: payload)

    assert fetch_lrclib_lrc("33542202") is None


def test_build_lrclib_provider_searches_and_filters_unusable_results(monkeypatch):
    captured = []

    def fake_http_json_value(url, *, timeout_seconds):
        captured.append((url, timeout_seconds))
        return [
            {
                "id": 1,
                "trackName": "plain only",
                "artistName": "nobody",
                "plainLyrics": "untimed",
                "syncedLyrics": None,
            },
            {
                "id": 2,
                "trackName": "too short",
                "artistName": "nobody",
                "syncedLyrics": _lrclib_synced_lines(7),
            },
            {
                "id": 33542202,
                "trackName": "芽吹くとき",
                "artistName": "yonige",
                "syncedLyrics": _lrclib_synced_lines(9),
            },
        ]

    monkeypatch.setattr(song_repair, "_http_json_value", fake_http_json_value)

    results = build_lrclib_lrc_provider(timeout_seconds=3.0)("芽吹くとき yonige")

    assert [(result.provider, result.source_ref) for result in results] == [
        ("lrclib", "https://lrclib.net/api/get/33542202")
    ]
    assert results[0].song_title == "芽吹くとき"
    assert captured[0][1] == 3.0
    assert "q=%E8%8A%BD%E5%90%B9%E3%81%8F%E3%81%A8%E3%81%8D+yonige" in captured[0][0]


def test_composite_provider_preserves_provenance_dedupes_and_survives_failure():
    lrclib = LrcResult(
        provider="lrclib",
        song_title="芽吹くとき",
        artist="yonige",
        source_ref="https://lrclib.net/api/get/33542202",
        lines=tuple(LrcLine(index * 1_000, f"歌词{index}") for index in range(8)),
    )
    netease = LrcResult(
        provider="netease",
        song_title="芽吹くとき",
        artist="yonige",
        source_ref="netease://song/3363527827",
        lines=lrclib.lines,
    )

    def broken(_query):
        raise RuntimeError("temporary outage")

    provider = build_composite_lrc_provider(
        broken,
        lambda _query: [lrclib, lrclib],
        lambda _query: netease,
    )

    results = provider("芽吹くとき")

    assert [(result.provider, result.source_ref) for result in results] == [
        ("lrclib", "https://lrclib.net/api/get/33542202"),
        ("netease", "netease://song/3363527827"),
    ]


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
