"""联唱场景下 `post_song_talk_start_ms` 指错的回归覆盖。

真例：`song_210131_1210` /《心型病毒 (Live)》。同一段音频、同一候选，
gemini-3.6-flash 跑了八次：五次给出真说话（484000/484300/484500/484500/485500ms，
新鲜 ASR 把「欢迎回来」钉在 486040ms），三次给出歌间间隙
（245000/245200/258000ms）。**八次全部通过旧交叉校验**——因为旧校验比的是同一
次回答里被 prompt 要求写成相等的两个字段。落到生产报告里的是 258000ms，那里
没有任何说话：ASR 在 [242040, 269940] 整段空白，269940ms 起是这场联唱的下一首歌。

更糟的是旧契约让真话违法：`post_song_transition_ms` 同时被当成 clip_end，受
`MAX_PROVEN_LIVE_INSTRUMENTAL_GAP_MS`(120s) 的 outro 帽约束，所以「歌 243.5s 结束、
说话 485.5s 才开始」这句真话必被拒；分变体重试于是一直采样到一个短的错值为止。

因此本轮两件事：契约把「歌结束」和「第一次说话」解耦（联唱真相变得可表达），
交叉校验换成真正独立的第三个信号——新鲜（非 AGY）全片 ASR。
"""

import json
from pathlib import Path

import pytest

import src.autoslice.host_vocal_proof as host_vocal_proof
import src.autoslice.song_repair as song_repair
from src.autoslice.post_song_talk_witness import (
    POST_SONG_TALK_WITNESS_WINDOW_MS,
    PostSongTalkWitnessError,
    validate_post_song_transition_pair,
    witness_post_song_talk_start,
)
from src.autoslice.review_evidence import SourceCue
from src.autoslice.song_performance import derive_live_arrangement_completeness
from tests.test_song_repair import _japanese_lrc, _write_fake_audio_alignment_run

FIXTURE = (
    Path(__file__).resolve().parent
    / "lidousha"
    / "fixtures"
    / "song_210131_1210_gemini_failover_alignment_20260808.json"
)

# free 生产报告实测：每条带 talk 毫秒的 lyrics-alignment-report 与它那一场
# `*_full_source.srt` 的最近 ASR cue（→ 全集，14 条）。
# 13 条正确断言最远 6100ms，唯一已知错的一条 11940ms。
PRODUCTION_TALK_CLAIMS = [
    ("2026-07-10 园游会", 259_400, (258_840, 261_480), 0, True),
    ("2026-07-10 怎么办", 229_000, (229_300, 230_680), 300, True),
    ("2026-07-10 想和你迎着台风去看海", 157_000, (157_220, 159_020), 220, True),
    ("2026-07-10 宝贝", 171_000, (172_080, 173_720), 1_080, True),
    ("2026-07-11 小幸运", 328_000, (327_870, 330_190), 0, True),
    ("2026-07-11 暗恋是一个人的事", 280_000, (280_380, 282_820), 380, True),
    ("2026-07-11 群青", 301_000, (301_320, 305_440), 320, True),
    ("2026-07-15 梦一场", 288_000, (287_960, 289_640), 0, True),
    ("2026-07-18 暖暖 attempt-0_3q3xvp", 742_000, (748_100, 750_020), 6_100, True),
    ("2026-07-18 暖暖 attempt-blpt632u", 748_000, (748_100, 750_020), 100, True),
    ("2026-07-24 宝贝", 272_500, (270_430, 272_710), 0, True),
    ("2026-07-25 海海海 attempt-2qypk6g8", 426_800, (419_510, 421_470), 5_330, True),
    ("2026-07-25 海海海 attempt-f0rzkx9i", 426_800, (419_510, 421_470), 5_330, True),
    # 唯一已知指错的真例：258000ms 落在 [242040, 269940] 的转写空洞正中。
    ("2026-08-08 心型病毒 (Live)", 258_000, (269_940, 275_840), 11_940, False),
]

# 心型病毒那一场 `song_210131_1210_full_source.srt` 的真实片段（只取时间轴，
# 文本不入库）：歌在 242040ms 收尾，269940ms 起是联唱的下一首，真说话 486040ms。
XINXING_BINGDU_CUES = (
    (231_790, 236_150),
    (237_680, 240_000),
    (240_000, 242_040),
    (269_940, 275_840),
    (275_840, 281_980),
    (304_210, 308_690),
    (486_040, 488_200),
    (490_760, 491_520),
)


def _cues(intervals):
    return [
        SourceCue(
            cue_id=f"asr-{index}",
            source_start_ms=start_ms,
            source_end_ms=end_ms,
            text="placeholder",
        )
        for index, (start_ms, end_ms) in enumerate(intervals)
    ]


def _fixture_payload():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _derive(payload):
    return derive_live_arrangement_completeness(
        observations=payload["observations"],
        live_arrangement=payload["live_arrangement"],
        post_song_talk_start_ms=payload["post_song_talk_start_ms"],
        source_duration_ms=payload["record"]["source_duration_ms"],
        spot_checks=payload["spot_checks"],
        live_performance=payload["live_performance"],
    )


def _as_decoupled_medley(payload, *, transition_ms=258_000, talk_ms=485_500):
    payload["live_arrangement"]["post_song_transition_kind"] = "INSTRUMENTAL_OUTRO_END"
    payload["live_arrangement"]["post_song_transition_ms"] = transition_ms
    payload["post_song_talk_start_ms"] = talk_ms
    for spot in payload["spot_checks"]:
        if spot["name"] == "longest_instrumental_gap":
            spot["live_time_ms"] = 250_000
    return payload


# --- (a) 联唱：歌间间隙不再能冒充歌后说话 ------------------------------------


def test_medley_inter_song_gap_claimed_as_host_talk_is_falsified_by_fresh_asr():
    with pytest.raises(PostSongTalkWitnessError) as excinfo:
        witness_post_song_talk_start(
            post_song_talk_start_ms=258_000,
            asr_cues=_cues(XINXING_BINGDU_CUES),
            require_witness=True,
        )

    message = str(excinfo.value)
    assert "258000ms" in message
    assert "[242040, 269940]" in message
    assert "gap between two songs" in message


def test_the_same_medley_run_true_talk_millisecond_is_corroborated():
    # 同一场、同一份 ASR：真说话 486040ms，模型给的 485500ms 差 540ms。
    record = witness_post_song_talk_start(
        post_song_talk_start_ms=485_500,
        asr_cues=_cues(XINXING_BINGDU_CUES),
        require_witness=True,
    )

    assert record is not None
    assert record["nearest_transcribed_audio_distance_ms"] == 540
    assert record["window_ms"] == POST_SONG_TALK_WITNESS_WINDOW_MS


# --- (b) 单曲场景行为不变 ----------------------------------------------------


@pytest.mark.parametrize(
    ("label", "talk_ms", "nearest_cue", "expected_distance_ms", "expected_pass"),
    PRODUCTION_TALK_CLAIMS,
    ids=[row[0] for row in PRODUCTION_TALK_CLAIMS],
)
def test_every_production_talk_claim_lands_on_the_expected_side(
    label, talk_ms, nearest_cue, expected_distance_ms, expected_pass
):
    cues = _cues([nearest_cue])
    if expected_pass:
        record = witness_post_song_talk_start(
            post_song_talk_start_ms=talk_ms, asr_cues=cues, require_witness=True
        )
        assert record is not None
        assert record["nearest_transcribed_audio_distance_ms"] == expected_distance_ms
    else:
        with pytest.raises(PostSongTalkWitnessError):
            witness_post_song_talk_start(
                post_song_talk_start_ms=talk_ms, asr_cues=cues, require_witness=True
            )


def test_legacy_pair_shapes_are_unchanged():
    validate_post_song_transition_pair(
        transition_kind="HOST_TALK",
        transition_ms=1_000,
        post_song_talk_start_ms=1_000,
        source_duration_ms=10_000,
    )
    validate_post_song_transition_pair(
        transition_kind="INSTRUMENTAL_OUTRO_END",
        transition_ms=1_000,
        post_song_talk_start_ms=None,
        source_duration_ms=10_000,
    )
    with pytest.raises(ValueError, match="host-talk transition is not bound"):
        validate_post_song_transition_pair(
            transition_kind="HOST_TALK",
            transition_ms=1_000,
            post_song_talk_start_ms=1_200,
            source_duration_ms=10_000,
        )
    with pytest.raises(ValueError, match="host-talk transition is not bound"):
        validate_post_song_transition_pair(
            transition_kind="HOST_TALK",
            transition_ms=1_000,
            post_song_talk_start_ms=None,
            source_duration_ms=10_000,
        )
    with pytest.raises(ValueError, match="no proven post-song transition"):
        validate_post_song_transition_pair(
            transition_kind="NONE_OR_UNKNOWN",
            transition_ms=1_000,
            post_song_talk_start_ms=None,
            source_duration_ms=10_000,
        )


def test_stored_inventory_keeps_its_exact_derived_arrangement_dict():
    # 已落库的 report 会被 song_completion / live_source_review 重新 derive 后逐字
    # 比对。HOST_TALK 老件的派生字典必须一个键都不变，否则全部存量证据当场失效。
    derived = _derive(_host_talk_fixture())

    assert sorted(derived) == [
        "canonical_line_count",
        "classification",
        "first_heard_lrc_index",
        "heard_line_count",
        "heard_line_ratio",
        "last_heard_lrc_index",
        "max_interline_gap_ms",
        "observed_live_song_ending",
        "observed_live_song_opening",
        "omitted_ranges",
        "performed_duration_ms",
        "post_song_transition_kind",
        "post_song_transition_ms",
    ]
    assert derived["post_song_transition_kind"] == "HOST_TALK"
    assert derived["post_song_transition_ms"] == 258_000


def _host_talk_fixture():
    payload = _fixture_payload()
    payload["live_arrangement"]["post_song_transition_ms"] = 258_000
    payload["post_song_talk_start_ms"] = 258_000
    for spot in payload["spot_checks"]:
        if spot["name"] == "longest_instrumental_gap":
            spot["live_time_ms"] = 250_000
    return payload


# --- (c) 交叉校验现在能查出「两个一起错」 ------------------------------------


def test_coincident_pair_certifies_itself_but_no_longer_survives_the_witness():
    payload = _host_talk_fixture()

    # 旧的「交叉校验」：两个同源字段相等——错值照样过。
    validate_post_song_transition_pair(
        transition_kind="HOST_TALK",
        transition_ms=258_000,
        post_song_talk_start_ms=258_000,
        source_duration_ms=payload["record"]["source_duration_ms"],
    )
    assert _derive(payload)["post_song_transition_ms"] == 258_000

    # 独立第三信号：同一毫秒在真实转写里根本没有声音。
    with pytest.raises(PostSongTalkWitnessError):
        witness_post_song_talk_start(
            post_song_talk_start_ms=258_000,
            asr_cues=_cues(XINXING_BINGDU_CUES),
            require_witness=True,
        )


def test_selection_path_rejects_an_uncorroborated_talk_anchor(tmp_path):
    # 接线覆盖：单元判据过了不算，必须证明它真的挂在选片路径上。
    lrc = _japanese_lrc()
    run = _write_fake_audio_alignment_run(
        tmp_path, lrc, candidate_id="witness-wiring", offset_ms=10_000
    )
    talk_ms = run.payload["post_song_talk_start_ms"]
    far_cues = _cues([(talk_ms + 30_000, talk_ms + 33_000)])

    with pytest.raises(PostSongTalkWitnessError):
        song_repair._validated_audio_lrc_selection(
            run=run,
            lrc=lrc,
            candidate_id="witness-wiring",
            source_media_path=Path(run.source_path),
            source_duration_ms=100_000,
            min_matched_ratio=0.55,
            asr_anchor_cues=far_cues,
        )

    near_cues = _cues([(talk_ms + 400, talk_ms + 3_000)])
    selected = song_repair._validated_audio_lrc_selection(
        run=run,
        lrc=lrc,
        candidate_id="witness-wiring",
        source_media_path=Path(run.source_path),
        source_duration_ms=100_000,
        min_matched_ratio=0.55,
        asr_anchor_cues=near_cues,
    )
    assert selected[7] == talk_ms


# --- 解耦契约：联唱真相变得可表达，且仍然有序 --------------------------------


def test_medley_truth_is_expressible_only_in_the_decoupled_form():
    # 真话的旧写法（HOST_TALK 拉到 485500）依然违法：它会把下一首整首吞进 clip。
    with pytest.raises(ValueError, match="outside the actual ending boundary"):
        _derive(_fixture_payload())

    derived = _derive(_as_decoupled_medley(_fixture_payload()))

    assert derived["post_song_transition_kind"] == "INSTRUMENTAL_OUTRO_END"
    # clip_end 仍然贴着这首歌自己的 outro，不跨到下一首。
    assert derived["post_song_transition_ms"] == 258_000
    assert witness_post_song_talk_start(
        post_song_talk_start_ms=485_500,
        asr_cues=_cues(XINXING_BINGDU_CUES),
        require_witness=True,
    ) is not None


@pytest.mark.parametrize("talk_ms", [257_999, 602_051])
def test_decoupled_talk_must_sit_between_the_song_end_and_the_source_end(talk_ms):
    with pytest.raises(ValueError, match="instrumental-outro transition is invalid"):
        _derive(_as_decoupled_medley(_fixture_payload(), talk_ms=talk_ms))


def test_new_decoupled_form_fails_closed_without_any_transcript():
    with pytest.raises(PostSongTalkWitnessError, match="no fresh-ASR witness available"):
        witness_post_song_talk_start(
            post_song_talk_start_ms=485_500, asr_cues=(), require_witness=True
        )


def test_legacy_form_without_a_transcript_keeps_its_named_fallback():
    # `_validated_audio_lrc_selection` 有一条明写的「没有 ASR cue 时退 agy_median」
    # 契约（tests/test_song_repair.py 同名用例）。把「有没有转写」变成整条歌 lane
    # 的新交付门是 维护者 的政策裁量，不在本次修复范围内——老形状原样保留。
    assert (
        witness_post_song_talk_start(
            post_song_talk_start_ms=258_000, asr_cues=(), require_witness=False
        )
        is None
    )


def test_absent_talk_millisecond_is_not_a_claim_to_witness():
    assert (
        witness_post_song_talk_start(
            post_song_talk_start_ms=None, asr_cues=(), require_witness=True
        )
        is None
    )


def test_witness_window_is_the_prover_first_anchor_window():
    # 没有新阈值：用的就是 host_vocal_proof 在这个毫秒上切的第一个锚点窗。
    assert POST_SONG_TALK_WITNESS_WINDOW_MS == host_vocal_proof.SESSION_HOST_ANCHOR_WINDOW_MS
