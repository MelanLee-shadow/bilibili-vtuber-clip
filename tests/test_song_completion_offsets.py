import pytest

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
    for index, (lrc_time_ms, residual_ms) in enumerate(zip(lrc_times, residuals)):
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
    report, lyric_lines, raw_rows, report_alignment, raw_sha = _audio_observation_fixture(
        agy_offset_ms=agy_offset_ms
    )
    failures: list[str] = []
    _validate_audio_observation_rows(
        report=report,
        failures=failures,
        is_int=lambda value: isinstance(value, int) and not isinstance(value, bool),
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
    assert "SONG_AUDIO_LRC_OBSERVATION_INVALID" in _validate_fixture(agy_offset_ms=18_801)


@pytest.mark.parametrize("location", ["interior", "first", "before", "end", "missing"])
def test_completion_repeated_spot_uses_bound_later_interval(location):
    report, lyrics, raw, alignment, raw_sha = _audio_observation_fixture()
    lyrics[-1]["text"] = raw[-1]["text"] = alignment[-1]["lrc_text"] = lyrics[0]["text"]
    start = alignment[-1]["cue_start_ms"]
    alignment[-1]["cue_end_ms"] = raw[-1]["live_end_ms"] = start + 7100
    report["last_lyric_end_ms"] = start + 7100
    time_ms = {
        "interior": start + 3300,
        "first": alignment[0]["cue_start_ms"],
        "before": start - 1,
        "end": start + 7100,
        "missing": None,
    }[location]
    report["spot_checks"] = [{"name": "repeated_section", "live_time_ms": time_ms, "result": "OK"}]
    failures = []
    _validate_audio_observation_rows(
        report=report,
        failures=failures,
        is_int=lambda value: type(value) is int,
        offset_ms=report["offset_ms"],
        lyric_lines=lyrics,
        line_count=len(lyrics),
        matched_count=len(lyrics),
        report_alignment=alignment,
        audio_artifacts={"raw_output_sha256": raw_sha},
        raw_rows=raw,
    )
    assert ("SONG_AUDIO_LRC_SPOT_CHECK_INVALID" in failures) is (location != "interior")


@pytest.mark.parametrize(
    "damage",
    [None, "proof", "offset", "median_label", "row", "raw_row", "bad_proof_type", "proof_float"],
)
@pytest.mark.parametrize("asr_anchor", [False, True])
def test_completion_recomputes_feasible_interval_proof_from_bound_raw_rows(damage, asr_anchor):
    from src.autoslice.song_global_shift import solve_global_shift

    report, lyrics, raw, alignment, raw_sha = _audio_observation_fixture()
    residuals = [10000, 10010, 12990]
    for row, entry, line, residual in zip(raw, alignment, lyrics, residuals):
        start = line["lrc_time_ms"] + residual
        row.update(live_start_ms=start, live_end_ms=start + 1500)
        entry.update(cue_start_ms=start, cue_end_ms=start + 1500)
    proof = solve_global_shift(residuals)
    report.update(
        offset_basis="audio_global_shift",
        offset_ms=proof["offset_ms"],
        agy_offset_ms=proof["offset_ms"],
        asr_offset_ms=None,
        audio_global_shift_proof=proof,
        first_lyric_start_ms=alignment[0]["cue_start_ms"],
        last_lyric_end_ms=alignment[-1]["cue_end_ms"],
    )
    if asr_anchor:
        report.update(
            offset_basis="asr_anchor",
            offset_ms=proof["offset_ms"] - 1000,
            asr_offset_ms=proof["offset_ms"] - 1000,
        )
    if damage == "proof":
        proof["feasible_minimum_ms"] += 1
    elif damage == "offset":
        report["offset_ms"] += 1
    elif damage == "median_label":
        report["offset_basis"] = "agy_median"
    elif damage == "row":
        alignment[0]["cue_start_ms"] += 1
    elif damage == "raw_row":
        raw[0]["live_start_ms"] += 1
    elif damage == "bad_proof_type":
        report["audio_global_shift_proof"] = "invalid"
    elif damage == "proof_float":
        proof["offset_ms"] = float(proof["offset_ms"])
    failures = []
    _validate_audio_observation_rows(
        report=report,
        failures=failures,
        is_int=lambda v: type(v) is int,
        offset_ms=report["offset_ms"],
        lyric_lines=lyrics,
        line_count=3,
        matched_count=3,
        report_alignment=alignment,
        audio_artifacts={"raw_output_sha256": raw_sha},
        raw_rows=raw,
    )
    assert ("SONG_AUDIO_LRC_OBSERVATION_INVALID" in failures) is (damage is not None)
