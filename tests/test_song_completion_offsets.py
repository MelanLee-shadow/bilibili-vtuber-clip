from src.autoslice.song_completion import _validate_audio_observation_rows


def _audio_observation_fixture(*, agy_offset_ms: int = 18_800):
    raw_sha = "a" * 64
    residuals = [18_000, 18_800, 19_500]
    lrc_times = [1_000, 11_000, 21_000]
    vocal_claim = {
        "lyric_vocal_subject": "LIDOUSHA",
        "lidousha_role": "LEAD_SINGER",
        "same_live_vocal_source_as_lidousha": True,
        "other_singer_or_harmony_audible": False,
        "recorded_or_playback_vocal_audible": False,
    }
    lyric_lines = []
    raw_rows = []
    report_alignment = []
    for index, (lrc_time_ms, residual_ms) in enumerate(
        zip(lrc_times, residuals)
    ):
        cue_start_ms = lrc_time_ms + residual_ms
        cue_end_ms = cue_start_ms + 1_500
        text = f"第{index + 1}句"
        lyric_lines.append(
            {
                "lrc_index": index,
                "lrc_time_ms": lrc_time_ms,
                "text": text,
            }
        )
        raw_rows.append(
            {
                "lrc_index": index,
                "lrc_time_ms": lrc_time_ms,
                "text": text,
                "heard": True,
                "live_start_ms": cue_start_ms,
                "live_end_ms": cue_end_ms,
                "confidence": 0.95,
                **vocal_claim,
            }
        )
        report_alignment.append(
            {
                "canonical_lrc_index": index,
                "lrc_time_ms": lrc_time_ms,
                "lrc_text": text,
                "matched_cue_id": f"agy-audio:{raw_sha[:12]}:line-{index}",
                "cue_start_ms": cue_start_ms,
                "cue_end_ms": cue_end_ms,
                "match_ratio": 0.95,
                "evidence_source": "agy_audio_lrc",
                **vocal_claim,
            }
        )
    report = {
        "offset_basis": "asr_anchor",
        "offset_ms": 17_300,
        "asr_offset_ms": 17_300,
        "agy_offset_ms": agy_offset_ms,
        "first_lyric_start_ms": report_alignment[0]["cue_start_ms"],
        "last_lyric_end_ms": report_alignment[-1]["cue_end_ms"],
    }
    return report, lyric_lines, raw_rows, report_alignment, raw_sha


def _validate_fixture(*, agy_offset_ms: int = 18_800) -> list[str]:
    report, lyric_lines, raw_rows, report_alignment, raw_sha = (
        _audio_observation_fixture(agy_offset_ms=agy_offset_ms)
    )
    failures: list[str] = []
    _validate_audio_observation_rows(
        report=report,
        failures=failures,
        is_int=lambda value: isinstance(value, int)
        and not isinstance(value, bool),
        offset_ms=report["offset_ms"],
        lyric_lines=lyric_lines,
        line_count=len(lyric_lines),
        matched_count=len(lyric_lines),
        report_alignment=report_alignment,
        audio_artifacts={"raw_output_sha256": raw_sha},
        raw_rows=raw_rows,
    )
    return failures


def test_asr_anchor_offset_validates_raw_rows_against_original_agy_median():
    # The last raw observation is 2.2s from the accepted ASR offset but only
    # 0.7s from the AGY median.  This is the production false block from
    # both offsets agree within the permitted 1.5s.
    assert _validate_fixture() == []


def test_asr_anchor_offset_rejects_drifted_agy_median_provenance():
    assert "SONG_AUDIO_LRC_OBSERVATION_INVALID" in _validate_fixture(
        agy_offset_ms=18_801
    )
