"""Synthetic timing controls for independent post-song speech witnesses."""

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
from tests.test_song_repair import _japanese_lrc, _write_fake_audio_alignment_run


# free 生产报告实测：每条带 talk 毫秒的 lyrics-alignment-report 与它那一场
# `*_full_source.srt` 的最近 ASR cue（→ 全集，14 条）。
# 13 条正确断言最远 6100ms，唯一已知错的一条 11940ms。
PRODUCTION_TALK_CLAIMS = [
    ("synthetic timing case", 259_400, (258_840, 261_480), 0, True),
    ("synthetic timing case", 229_000, (229_300, 230_680), 300, True),
    ("synthetic timing case", 157_000, (157_220, 159_020), 220, True),
    ("synthetic timing case", 171_000, (172_080, 173_720), 1_080, True),
    ("synthetic timing case", 328_000, (327_870, 330_190), 0, True),
    ("synthetic timing case", 280_000, (280_380, 282_820), 380, True),
    ("synthetic timing case", 301_000, (301_320, 305_440), 320, True),
    ("synthetic timing case", 288_000, (287_960, 289_640), 0, True),
    ("synthetic timing case", 742_000, (748_100, 750_020), 6_100, True),
    ("synthetic timing case", 748_000, (748_100, 750_020), 100, True),
    ("synthetic timing case", 272_500, (270_430, 272_710), 0, True),
    ("synthetic timing case", 426_800, (419_510, 421_470), 5_330, True),
    ("synthetic timing case", 426_800, (419_510, 421_470), 5_330, True),
    # 唯一已知指错的真例：258000ms 落在 [242040, 269940] 的转写空洞正中。
    ("synthetic timing case", 258_000, (269_940, 275_840), 11_940, False),
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






# --- (c) 交叉校验现在能查出「两个一起错」 ------------------------------------




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






def test_new_decoupled_form_fails_closed_without_any_transcript():
    with pytest.raises(PostSongTalkWitnessError, match="no fresh-ASR witness available"):
        witness_post_song_talk_start(
            post_song_talk_start_ms=485_500, asr_cues=(), require_witness=True
        )


def test_legacy_form_without_a_transcript_keeps_its_named_fallback():



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
