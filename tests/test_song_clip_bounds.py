"""Unit cases of the one song clip start rule (song_clip_bounds, 维护者)."""
from __future__ import annotations

import array
import hashlib
import math
import wave

import pytest

from src.autoslice.review_evidence import SourceCue
from src.autoslice.song_clip_bounds import (
    AUDIO_EARLIER_ONSET,
    AUDIO_NO_EARLIER_ONSET,
    AUDIO_ONSET_UNAVAILABLE,
    INTRO_SPEECH_MAX_LEAD_MS,
    RECOGNITION_TOLERANCE_MS,
    SAME_UTTERANCE_GAP_MS,
    audio_onset_evidence,
    earlier_audio_onset_ms,
    song_clip_start,
)
from src.autoslice.boundary_semantic_review import SEMANTIC_FLOOR_SILENT_GAP_MS

RATE = 16_000
LYRICS = ["lijupexibodojipi", "gagecohijanolacido"]


def _tone(ms, amplitude=6_000):
    return [int(amplitude * math.sin(2 * math.pi * 330 * n / RATE)) for n in range(ms * RATE // 1000)]


def _silence(ms):
    return [0] * (ms * RATE // 1000)


def test_constants_come_from_the_boundary_step_and_admin_interpretation():
    assert SAME_UTTERANCE_GAP_MS == SEMANTIC_FLOOR_SILENT_GAP_MS == 400
    assert (INTRO_SPEECH_MAX_LEAD_MS, RECOGNITION_TOLERANCE_MS) == (15_000, 300)


@pytest.mark.parametrize(("pieces", "expected"), [
    ([("s", 3_000), ("t", 2_000)], 13_000),             # silence then sound into the song
    ([("t", 5_000)], None),                             # continuous sound: nothing proven
    ([("t", 2_000), ("s", 3_000)], None),               # silence up to the estimate
    ([("s", 1_000), ("t", 1_500), ("s", 300), ("t", 2_200)], 11_000),  # a 300ms dip is not a pause
    ([("t", 2_000), ("q", 1_500), ("t", 1_500)], None),  # a quiet bar inside music is not silence
])
def test_earlier_audio_onset_needs_a_proven_silence(pieces, expected):
    samples = []
    for kind, ms in pieces:
        samples += _silence(ms) if kind == "s" else _tone(ms, 300 if kind == "q" else 6_000)
    assert earlier_audio_onset_ms(samples, window_start_ms=10_000, window_end_ms=15_000) == expected


def test_audio_onset_evidence_reads_only_the_hash_bound_audio(tmp_path):
    path = tmp_path / "input.mp3"
    samples = array.array("h", _silence(9_000) + _tone(12_000))
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(RATE)
        handle.writeframes(samples.tobytes())
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    evidence = audio_onset_evidence((path, sha), estimate_ms=20_000)
    assert evidence["status"] == AUDIO_EARLIER_ONSET
    assert evidence["window_ms"] == [5_000, 20_000] and evidence["onset_ms"] == 9_000
    assert audio_onset_evidence((path, "0" * 64), estimate_ms=20_000)["status"] == AUDIO_ONSET_UNAVAILABLE
    assert audio_onset_evidence(None, estimate_ms=20_000)["status"] == AUDIO_ONSET_UNAVAILABLE
    garbage = tmp_path / "garbage.mp3"
    garbage.write_bytes(b"not audio")
    unreadable = audio_onset_evidence((garbage, hashlib.sha256(b"not audio").hexdigest()), estimate_ms=20_000)
    assert unreadable["status"] == AUDIO_ONSET_UNAVAILABLE


def test_start_is_the_music_start_minus_tolerance_without_intro_speech():
    start, evidence = song_clip_start(lrc_zero_ms=50_000, first_lyric_start_ms=58_000, cues=[], lyrics=LYRICS)
    assert start == 50_000 - RECOGNITION_TOLERANCE_MS
    assert evidence["music_start_ms"] == 50_000 and evidence["intro_host_speech"] == []
    # A late first vocal never moves the music start.
    assert song_clip_start(lrc_zero_ms=50_000, first_lyric_start_ms=70_000, cues=[], lyrics=LYRICS)[0] == 49_700
    assert song_clip_start(lrc_zero_ms=100, first_lyric_start_ms=900, cues=[], lyrics=LYRICS)[0] == 0


def test_only_a_proven_earlier_audio_onset_moves_the_music_start():
    earlier = {"status": AUDIO_EARLIER_ONSET, "onset_ms": 44_000}
    assert song_clip_start(lrc_zero_ms=50_000, first_lyric_start_ms=58_000, cues=[], lyrics=LYRICS,
                           audio_onset=earlier)[0] == 43_700
    for status in (AUDIO_NO_EARLIER_ONSET, AUDIO_ONSET_UNAVAILABLE):
        assert song_clip_start(lrc_zero_ms=50_000, first_lyric_start_ms=58_000, cues=[], lyrics=LYRICS,
                               audio_onset={"status": status, "onset_ms": 44_000})[0] == 49_700


def test_intro_speech_extends_to_the_utterance_start_and_is_capped():
    cues = [
        SourceCue("far", 30_000, 31_000, "上一件事说完了"),
        SourceCue("a", 46_000, 47_800, "那我们来唱"),
        SourceCue("b", 48_000, 49_900, "这首歌"),
        SourceCue("over", 51_000, 52_000, "大家跟我一起"),
        SourceCue("lyric", 58_000, 60_000, "lijupexi bodojipi"),
        SourceCue("guest", 49_000, 49_800, "[嘉宾] 好的"),
        SourceCue("music", 49_850, 50_000, "[纯音乐]"),
    ]
    start, evidence = song_clip_start(lrc_zero_ms=50_000, first_lyric_start_ms=58_000, cues=cues, lyrics=LYRICS)
    assert start == 46_000 - RECOGNITION_TOLERANCE_MS
    assert [row[0] for row in evidence["intro_host_speech"]] == ["a", "b", "over"]
    # Speech over the intro alone also counts.
    start, _ = song_clip_start(lrc_zero_ms=50_000, first_lyric_start_ms=58_000, cues=cues[3:], lyrics=LYRICS)
    assert start == 50_000 - RECOGNITION_TOLERANCE_MS
    # Ten cues 300ms apart: one long utterance reaching back 20s.
    chain = [SourceCue(f"c{n}", 48_000 - 2_000 * n, 49_700 - 2_000 * n, "我还在说话") for n in range(10)]
    start, evidence = song_clip_start(lrc_zero_ms=50_000, first_lyric_start_ms=58_000, cues=chain, lyrics=LYRICS)
    assert start == 50_000 - INTRO_SPEECH_MAX_LEAD_MS and evidence["speech_capped"] is True
