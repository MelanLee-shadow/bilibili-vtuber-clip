"""Members of the live-arrangement omission shape derived from real receipts.

维护者: "理论上主播是会把歌唱完的，但是偶尔主播是不会唱完，比如说他可能中间会缺
一句缺两句。那么这种情况我希望我们的歌切检查程序也能适应这种情况。" and, on the
ordering failure, "为什么我们会有这样一个metric？是不是我们这个metric设的不太好呢？"

Fixtures under ``fixtures/song_omission`` are derived from the OCI3
receipts of 快乐手帐 (song_130025_580 v1-002 variant-01) and 失眠飞行
(song_133029_1424 variant-02): real LRC/AGY/BCUT clocks, heard pattern,
arrangement/spot/live-performance observations and host speech; lyric text is
replaced by placeholders.  Every case enters at the ordinary ``attempt_song_repair``
input and is read back by the stored-report consumers.  This file imports only
interfaces that already existed before the repair, so it also runs as the
guard on the old generator.
"""
from __future__ import annotations

import array
import copy
import hashlib
import json
import math
import wave
from collections import Counter
from pathlib import Path

import pytest

from src.autoslice.live_source_review import _verify_live_performance_observation
from src.autoslice.recut_materialization import _load_lyric_timeline, _write_lyric_timeline_srt
from src.autoslice.review_evidence import SourceCue
from src.autoslice.song_common import CHANNEL_PROFILE, LrcLine, LrcResult
from src.autoslice.song_completion import _load_source_context_evidence
from src.autoslice.song_repair import attempt_song_repair
from test_song_repair import (
    _omit_live_arrangement_rows,
    _rebind_fake_audio_alignment_run,
    _write_fake_audio_alignment_run,
)

FIXTURES = Path(__file__).parent / "fixtures" / "song_omission"
KUAILE = "kuaile_shouzhang_v01"
SHIMIAN = "shimian_feixing_v02"


def _fixture(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def _write_tone_wav(path, *, duration_ms, sounding_ms, rate=8_000):
    """Synthetic observation audio: a tone inside ``sounding_ms`` intervals."""
    samples = array.array("h", bytes(2 * duration_ms * rate // 1000))
    for start, end in sounding_ms:
        for n in range(start * rate // 1000, min(len(samples), end * rate // 1000)):
            samples[n] = int(8_000 * math.sin(2 * math.pi * 440 * n / rate))
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(samples.tobytes())


def _repair(tmp_path, name, *, omit=(), drop_rows=(), lrc_shift_ms=0, cue_start_ms=None,
            extra_cues=(), speech_text=None, arrangement=None, transition_ms=None, sounding_ms=None,
            keep_cue_ids=()):
    fixture = _fixture(name)
    rows = [copy.deepcopy(r) for r in fixture["observations"] if r["lrc_index"] not in set(drop_rows)]
    dropped_cues = {c for i in [*omit, *drop_rows] for c in fixture["row_anchor_cue_ids"].get(str(i), [])}
    dropped_cues -= set(keep_cue_ids)
    # Row indices in ``omit`` name the original receipt rows.
    original = [r["lrc_index"] for r in rows]
    for index, row in enumerate(rows):
        row["lrc_index"] = index
        row["lrc_time_ms"] += lrc_shift_ms
    cues = []
    for c in fixture["asr_cues"]:
        if c["cue_id"] in dropped_cues:
            continue
        start = c["source_start_ms"]
        if cue_start_ms and c["cue_id"] in cue_start_ms:
            start = cue_start_ms[c["cue_id"]]
        text = c["text"]
        if speech_text is not None and not any(c["cue_id"] in v for v in fixture["row_anchor_cue_ids"].values()) \
                and start < fixture["post_song_talk_start_ms"]:
            text = speech_text
        cues.append(SourceCue(c["cue_id"], start, c["source_end_ms"], text, kind=c["kind"], speaker=c["speaker"]))
    cues = sorted([*cues, *extra_cues], key=lambda c: (c.source_start_ms, c.cue_id))
    lrc = LrcResult("fixture", name, None, f"fixture://{name}",
                    tuple(LrcLine(r["lrc_time_ms"], r["text"]) for r in rows))
    candidate = f"derived-{name}"
    run = _write_fake_audio_alignment_run(tmp_path, lrc, candidate_id=candidate,
                                          source_duration_ms=fixture["source_duration_ms"])
    payload = json.loads(json.dumps(run.payload))
    payload["observations"] = rows
    for key in ("spot_checks", "live_performance", "live_arrangement", "post_song_talk_start_ms"):
        payload[key] = copy.deepcopy(fixture[key])
    payload["live_arrangement"].update(arrangement or {})
    if transition_ms is not None:
        payload["live_arrangement"]["post_song_transition_ms"] = transition_ms
        payload["post_song_talk_start_ms"] = transition_ms
    _omit_live_arrangement_rows(payload, tuple(original.index(i) for i in omit))
    if sounding_ms is not None:
        # Replace the bound observation audio (same path, rebound hash).
        audio = tmp_path / "input.mp3"
        _write_tone_wav(audio, duration_ms=max(end for _start, end in sounding_ms) + 5_000,
                        sounding_ms=sounding_ms)
        manifest = json.loads(Path(run.manifest_path).read_text(encoding="utf-8"))
        manifest["observation_input"]["audio_sha256"] = hashlib.sha256(audio.read_bytes()).hexdigest()
        Path(run.manifest_path).write_text(json.dumps(manifest, ensure_ascii=False, sort_keys=True) + "\n",
                                           encoding="utf-8")
    run = _rebind_fake_audio_alignment_run(run, payload)
    anchored = [c for c in cues if c.text and any(c.cue_id in v for v in fixture["row_anchor_cue_ids"].values())]
    result = attempt_song_repair(
        candidate_id=candidate, cues=cues, anchor_start_ms=anchored[0].source_start_ms,
        anchor_end_ms=anchored[-1].source_end_ms, source_duration_ms=fixture["source_duration_ms"],
        output_dir=tmp_path / "repair", lrc_provider=lambda _query: lrc,
        source_media_path=Path(run.source_path), audio_lrc_aligner=lambda *_args: run, asr_anchor_cues=cues,
    )
    return result, rows, cues


def _consumed(tmp_path, result):
    """Stored-report consumers: live-source proof, completion record, final SRT."""
    assert _verify_live_performance_observation(result.lyrics_alignment, output_dir=tmp_path / "repair") is None
    stored = json.loads(Path(result.lyrics_alignment["alignment_report_path"]).read_text(encoding="utf-8"))
    failures: list[str] = []
    _load_source_context_evidence({"candidate_id": stored["candidate_id"],
                                   "source_context_job": {"song_boundary": dict(result.song_boundary),
                                                          "lyrics_alignment": dict(result.lyrics_alignment)}},
                                  failures)
    assert failures == []
    loaded = _load_lyric_timeline({"lyrics_alignment": result.lyrics_alignment}, output_dir=tmp_path / "repair")
    assert loaded is not None
    timeline, offset = loaded
    srt = tmp_path / "final.srt"
    _write_lyric_timeline_srt(timeline, offset, result.song_boundary["clip_start_ms"],
                              result.song_boundary["clip_end_ms"], srt)
    report = json.loads(Path(result.lyrics_alignment["alignment_report_path"]).read_text(encoding="utf-8"))
    return report, srt.read_text(encoding="utf-8")


def _unique_text(rows, index):
    counts = Counter(r["text"] for r in rows)
    assert counts[rows[index]["text"]] == 1
    return rows[index]["text"]


def _kinds(report):
    return [r["kind"] for r in report["arrangement_completeness"]["omitted_ranges"]]


def test_witness_kuaile_first_cue_before_lrc_zero_passes_ordinary_entry(tmp_path):
    result, _rows, cues = _repair(tmp_path, KUAILE)
    assert result.repaired, result.attempts
    boundary = result.song_boundary
    assert boundary["completion_basis"] == "FULL_STUDIO_SEQUENCE"
    first_cue = next(c for c in cues if c.cue_id == "u_000045")
    assert boundary["nominal_lrc_zero_ms"] == 120_190 > first_cue.source_start_ms == 120_000
    # Music start = min(LRC zero, first lyric onset) = 120000; no intro speech;
    # 300ms recognition tolerance.  The old start (= LRC zero) broke ordering.
    assert boundary["clip_start_ms"] == 119_700
    evidence = boundary["clip_start_evidence"]
    assert evidence["music_start_ms"] == 120_000 and evidence["intro_host_speech"] == []
    assert evidence["audio_onset"]["status"] == "UNAVAILABLE"
    _consumed(tmp_path, result)


def test_single_nonrepeated_line_hole_is_complete_and_unsubtitled(tmp_path):
    result, rows, _cues = _repair(tmp_path, KUAILE, omit=(16,))
    assert result.repaired, result.attempts
    report, srt = _consumed(tmp_path, result)
    assert _kinds(report) == ["BOUNDED_SMALL_SKIP"]
    assert result.song_boundary["completion_basis"] == "COMPLETE_LIVE_ARRANGEMENT"
    assert _unique_text(rows, 16) not in srt
    assert f"[{CHANNEL_PROFILE.host_speaker_label}]" not in srt


def test_two_separate_single_line_holes_are_complete(tmp_path):
    result, rows, _cues = _repair(tmp_path, KUAILE, omit=(9, 40))
    assert result.repaired, result.attempts
    report, srt = _consumed(tmp_path, result)
    assert _kinds(report) == ["BOUNDED_SMALL_SKIP", "BOUNDED_SMALL_SKIP"]
    assert _unique_text(rows, 9) not in srt and _unique_text(rows, 40) not in srt


def test_two_line_hole_with_source_host_speech_is_complete_and_subtitled(tmp_path):
    fixture = _fixture(KUAILE)
    cue_by_id = {c["cue_id"]: c for c in fixture["asr_cues"]}
    left_end = cue_by_id[fixture["row_anchor_cue_ids"]["17"][-1]]["source_end_ms"]
    right_start = cue_by_id[fixture["row_anchor_cue_ids"]["20"][0]]["source_start_ms"]
    # Real host speech from the 失眠飞行 interlude, placed inside this gap.
    spoken = [c["text"] for c in _fixture(SHIMIAN)["asr_cues"] if 292_000 <= c["source_start_ms"] < 296_000]
    slot = (right_start - left_end - 400) // len(spoken)
    extra = [SourceCue(f"moved-speech-{n}", left_end + 200 + slot * n, left_end + 100 + slot * (n + 1), text)
             for n, text in enumerate(spoken)]
    result, rows, _cues = _repair(tmp_path, KUAILE, omit=(18, 19), extra_cues=extra)
    assert result.repaired, result.attempts
    report, srt = _consumed(tmp_path, result)
    assert result.song_boundary["completion_basis"] == "COMPLETE_LIVE_WITH_SPOKEN_INTERLUDE"
    assert _kinds(report) == ["BOUNDED_SPOKEN_INTERLUDE"]
    assert _unique_text(rows, 18) not in srt and _unique_text(rows, 19) not in srt
    assert len(extra) == 2
    for cue in extra:
        assert f"[{CHANNEL_PROFILE.host_speaker_label}] {cue.text}" in srt


def test_shimian_spoken_interlude_from_ordinary_entry(tmp_path):
    result, rows, _cues = _repair(tmp_path, SHIMIAN)
    assert result.repaired, result.attempts
    report, srt = _consumed(tmp_path, result)
    assert result.song_boundary["completion_basis"] == "COMPLETE_LIVE_WITH_SPOKEN_INTERLUDE"
    assert _kinds(report) == ["BOUNDED_SPOKEN_INTERLUDE"]
    assert srt.count(f"[{CHANNEL_PROFILE.host_speaker_label}]") == 7
    assert all(r["text"] not in srt for r in rows if not r["heard"])


def test_other_song_first_cue_before_lrc_zero_starts_from_evidence(tmp_path):
    # 失眠飞行 with an LRC variant lacking the bridge rows and starting 0.2s
    # before the first vocal; BCUT's first cue begins before that LRC zero.
    result, _rows, _cues = _repair(
        tmp_path, SHIMIAN, drop_rows=tuple(range(41, 47)), lrc_shift_ms=-2_400,
        cue_start_ms={"asr_anchor_000047": 128_000},
        arrangement={"classification": "FULL_STUDIO_SEQUENCE"},
    )
    assert result.repaired, result.attempts
    boundary = result.song_boundary
    assert boundary["completion_basis"] == "FULL_STUDIO_SEQUENCE"
    assert boundary["first_lyric_start_ms"] == 128_000 < boundary["nominal_lrc_zero_ms"]
    assert boundary["clip_start_ms"] <= 128_000
    _consumed(tmp_path, result)





def test_intro_host_speech_moves_start_to_utterance_start(tmp_path):
    # Real pre-song host speech, placed so the utterance ends 300ms before the
    # music start (120000); its first cue is one proven-silent gap earlier.
    extra = [SourceCue("intro-0", 114_000, 115_400, "谢谢你"),
             SourceCue("intro-1", 116_300, 117_700, "现在只需要做一团熊猫就可以了"),
             SourceCue("intro-2", 118_000, 119_700, "这首歌")]
    result, _rows, _cues = _repair(tmp_path, KUAILE, extra_cues=extra)
    assert result.repaired, result.attempts
    boundary = result.song_boundary
    assert boundary["clip_start_ms"] == 116_300 - 300
    assert [row[0] for row in boundary["clip_start_evidence"]["intro_host_speech"]] == ["intro-1", "intro-2"]
    _consumed(tmp_path, result)


def test_pure_accompaniment_intro_starts_at_music_start(tmp_path):
    result, _rows, _cues = _repair(tmp_path, SHIMIAN, drop_rows=tuple(range(41, 47)),
                                   arrangement={"classification": "FULL_STUDIO_SEQUENCE"})
    assert result.repaired, result.attempts
    boundary = result.song_boundary
    assert boundary["nominal_lrc_zero_ms"] == 125_740 < boundary["first_lyric_start_ms"]
    assert boundary["clip_start_evidence"]["music_start_ms"] == 125_740
    assert boundary["clip_start_ms"] == 125_740 - 300
    _consumed(tmp_path, result)


def test_late_singing_keeps_the_music_start(tmp_path):
    # The backing track runs 12.6s before her first line (studio timeline).
    result, _rows, _cues = _repair(tmp_path, SHIMIAN, drop_rows=tuple(range(41, 47)), lrc_shift_ms=10_000,
                                   arrangement={"classification": "FULL_STUDIO_SEQUENCE"})
    assert result.repaired, result.attempts
    boundary = result.song_boundary
    assert boundary["nominal_lrc_zero_ms"] == 115_740
    assert boundary["clip_start_ms"] == 115_740 - 300
    assert boundary["first_lyric_start_ms"] - boundary["clip_start_ms"] > 12_000
    _consumed(tmp_path, result)


def test_music_played_early_starts_at_the_audio_onset(tmp_path):
    # Bound observation audio: silence, then sound from 112.0s into the song.
    result, _rows, _cues = _repair(tmp_path, KUAILE, sounding_ms=[(112_000, 130_000)])
    assert result.repaired, result.attempts
    boundary = result.song_boundary
    assert boundary["clip_start_evidence"]["audio_onset"]["status"] == "EARLIER_ONSET"
    assert boundary["clip_start_evidence"]["music_start_ms"] == 112_000
    assert boundary["clip_start_ms"] == 112_000 - 300
    _consumed(tmp_path, result)


def test_proven_silence_before_the_song_keeps_the_estimate(tmp_path):
    # The witness's real shape: talk, digital silence 115.6-120.4s, then the song.
    result, _rows, _cues = _repair(tmp_path, KUAILE, sounding_ms=[(104_000, 115_600), (120_400, 130_000)])
    assert result.repaired, result.attempts
    boundary = result.song_boundary
    assert boundary["clip_start_evidence"]["audio_onset"]["status"] == "NO_EARLIER_ONSET"
    assert boundary["clip_start_ms"] == 119_700


def test_early_end_clip_ends_at_the_actual_stop(tmp_path):
    # She stops after the second-to-last chorus (rows 52-55 repeat rows 26-29).
    stop_ms = 337_000
    extra = [SourceCue("stop-talk", stop_ms, stop_ms + 1_500, "谢谢大家")]
    result, _rows, _cues = _repair(tmp_path, KUAILE, omit=(52, 53, 54, 55), extra_cues=extra,
                                   transition_ms=stop_ms)
    assert result.repaired, result.attempts
    report, _srt = _consumed(tmp_path, result)
    assert _kinds(report) == ["TRAILING_REPEATED_SECTION"]
    assert result.song_boundary["last_lyric_end_ms"] <= stop_ms == result.song_boundary["clip_end_ms"]


def test_speech_inside_a_repeated_section_hole_is_subtitled_by_the_same_rule(tmp_path):
    # She skips the repeated chorus rows 26-29 (sung again as rows 52-55) and
    # talks in that gap: the hole stays a repeated section, her words show.
    extra = [SourceCue("chorus-talk", 228_000, 229_600, "谢谢小朋友们的打 call")]
    result, rows, _cues = _repair(tmp_path, KUAILE, omit=(26, 27, 28, 29), extra_cues=extra)
    assert result.repaired, result.attempts
    report, srt = _consumed(tmp_path, result)
    assert _kinds(report) == ["BOUNDED_REPEATED_SECTION"]
    assert result.song_boundary["completion_basis"] == "COMPLETE_LIVE_ARRANGEMENT"
    assert f"[{CHANNEL_PROFILE.host_speaker_label}] 谢谢小朋友们的打 call" in srt


def test_trailing_hole_that_bcut_sings_is_not_selectable(tmp_path):
    # The observation calls the final chorus unheard, yet BCUT sings row 54
    # before the claimed transition: her words were dropped, not skipped.
    stop_ms = 351_000
    extra = [SourceCue("stop-talk", stop_ms, stop_ms + 1_500, "谢谢大家")]
    result, _rows, _cues = _repair(tmp_path, KUAILE, omit=(52, 53, 54, 55), extra_cues=extra,
                                   transition_ms=stop_ms, keep_cue_ids=("u_000108",))
    assert not result.repaired
    assert any("is in an omitted block but source ASR cue u_000108 sings it" in item.detail
               for item in result.attempts if item.step == "agy_audio_lrc_alignment"), result.attempts


# 行政 (SUB-26 "可以多、不能漏她的话"): a hole row must be really
# unsung. 失眠飞行 v01's observation calls lyric rows 18-19 unheard, while BCUT
# sings them (asr_anchor_000066/67, which v02's rows 18-19 anchor on).
def _shimian_variant_runs(tmp_path, numbers):
    media = tmp_path / "current-full-window.mp4"
    cues = [SourceCue(c["cue_id"], c["source_start_ms"], c["source_end_ms"], c["text"], kind=c["kind"],
                      speaker=c["speaker"]) for c in _fixture("shimian_feixing_v01")["asr_cues"]]
    runs, lrcs = {}, []
    for number in numbers:
        fixture = _fixture(f"shimian_feixing_v0{number}")
        rows = copy.deepcopy(fixture["observations"])
        lrc = LrcResult("fixture", "失眠飞行", None, f"fixture://shimian/v0{number}",
                        tuple(LrcLine(r["lrc_time_ms"], r["text"]) for r in rows))
        (tmp_path / f"v0{number}").mkdir()
        run = _write_fake_audio_alignment_run(tmp_path / f"v0{number}", lrc, candidate_id="derived-shimian",
                                              source_duration_ms=fixture["source_duration_ms"],
                                              source_media_path=media)
        payload = json.loads(json.dumps(run.payload))
        payload["observations"] = rows
        for key in ("spot_checks", "live_performance", "live_arrangement", "post_song_talk_start_ms"):
            payload[key] = copy.deepcopy(fixture[key])
        runs[lrc.source_ref] = _rebind_fake_audio_alignment_run(run, payload)
        lrcs.append(lrc)
    anchored = [c for c in cues if c.text.isascii()]
    return attempt_song_repair(
        candidate_id="derived-shimian", cues=cues, anchor_start_ms=anchored[0].source_start_ms,
        anchor_end_ms=anchored[-1].source_end_ms, source_duration_ms=499_600, output_dir=tmp_path / "repair",
        lrc_provider=lambda _query: lrcs, extra_queries=("失眠飞行",), source_media_path=media,
        audio_lrc_aligner=lambda _media, lrc, *_args: runs[lrc.source_ref], asr_anchor_cues=cues,
    )


def _sung_hole_rejections(result):
    return [item.detail for item in result.attempts if item.step == "agy_audio_lrc_alignment"
            and "is in an omitted block but source ASR cue" in item.detail]


def test_variant_whose_hole_rows_asr_sings_is_not_selectable(tmp_path):
    result = _shimian_variant_runs(tmp_path, (1,))
    assert not result.repaired
    rejection = _sung_hole_rejections(result)
    assert rejection and "canonical LRC line 22 " in rejection[0] and "asr_anchor_000066" in rejection[0]


def test_production_variant_order_selects_the_variant_that_heard_every_sung_row(tmp_path):
    result = _shimian_variant_runs(tmp_path, (1, 2, 3))
    assert result.repaired, result.attempts
    assert result.lyrics_alignment["external_lrc"] == "fixture://shimian/v02"
    assert result.song_boundary["completion_basis"] == "COMPLETE_LIVE_WITH_SPOKEN_INTERLUDE"
    variants = [item.detail for item in result.attempts if item.step == "agy_audio_lrc_variant"]
    assert "fixture://shimian/v01" in variants[0]  # tried first, as in production
    assert _sung_hole_rejections(result)
    report, srt = _consumed(tmp_path, result)
    # Both rows v01 dropped are sung lines with subtitles in the selected v02.
    sung_text = _fixture("shimian_feixing_v01")["observations"][22]["text"]
    assert srt.count(sung_text) >= 2


# Controls stay incomplete with the old and the repaired generator alike; a
# needle is asserted only where both generators name the same reason.
def _rejected(result, needle=None):
    assert not result.repaired
    assert result.reason_codes == ("SONG_AUDIO_LRC_PROOF_UNRESOLVED",)
    details = [item.detail for item in result.attempts if item.step == "agy_audio_lrc_alignment"]
    assert details
    if needle is not None:
        assert any(needle in detail for detail in details), details


def test_control_three_line_nonrepeated_silent_hole_stays_incomplete(tmp_path):
    result, _rows, _cues = _repair(tmp_path, KUAILE, omit=(20, 21, 22))
    _rejected(result, "canonical LRC line 20 was not affirmatively heard")


def test_control_four_small_holes_stay_incomplete(tmp_path):
    result, _rows, _cues = _repair(tmp_path, KUAILE, omit=(5, 16, 40, 44))
    _rejected(result)


@pytest.mark.parametrize("omit", [(), (16,)])
@pytest.mark.parametrize("flag", ["observed_live_song_opening", "observed_live_song_ending"])
def test_control_unobserved_opening_or_ending_stays_incomplete(tmp_path, flag, omit):
    result, _rows, _cues = _repair(tmp_path, KUAILE, omit=omit, arrangement={flag: False})
    _rejected(result, None if omit else "opening or actual live ending was not observed")


def test_control_chorus_only_fragment_stays_incomplete(tmp_path):
    rows = _fixture(KUAILE)["observations"]
    counts = Counter(r["text"] for r in rows)
    verses = tuple(r["lrc_index"] for r in rows if counts[r["text"]] == 1)
    result, _rows, _cues = _repair(tmp_path, KUAILE, omit=verses)
    assert not result.repaired


def test_control_accompaniment_claimed_as_spoken_interlude_stays_incomplete(tmp_path):
    result, _rows, _cues = _repair(tmp_path, SHIMIAN, speech_text="[纯音乐]",
                                   arrangement={"classification": "COMPLETE_LIVE_WITH_SPOKEN_INTERLUDE"})
    # The same receipt with its real host speech is a member
    # (test_shimian_spoken_interlude_from_ordinary_entry); only the missing
    # speech evidence changes the verdict.
    _rejected(result)
