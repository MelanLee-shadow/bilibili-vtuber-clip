"""Final capture rejects unusable extracted audio before calling a provider.

Fault injection replaces only the extraction subprocess result. Positive cases
use real FFmpeg/ffprobe on locally generated PCM; neither lane calls a service.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import wave
from pathlib import Path

import pytest

from src.autoslice import final_subtitle_audio_gate as gate


@pytest.mark.parametrize("payload", [b"", b"not an audio stream", b"ID3\x04\x00\x00\x00\x00\x00\x00"])
def test_unusable_zero_exit_extraction_never_reaches_bcut(tmp_path, monkeypatch, payload):
    media = tmp_path / "synthetic.wav"
    with wave.open(str(media), "wb") as sound:
        sound.setparams((1, 2, 16_000, 0, "NONE", "not compressed"))
        sound.writeframes(b"\0\0" * 16_000)
    subtitle = tmp_path / "synthetic.srt"
    subtitle.write_text("1\n00:00:00,000 --> 00:00:01,000\n这是合成测试的字幕\n")
    digest = "sha256:" + hashlib.sha256(media.read_bytes()).hexdigest()
    record = {
        "artifact_hashes": {"burned_video_sha256": digest},
        "burned_preview": {"status": "BURNED", "path": str(media), "burned_sha256": digest},
    }
    real_run = subprocess.run
    providers = []

    def extraction_fault(argv, **kwargs):
        if argv[0] == "ffmpeg":
            Path(argv[-1]).write_bytes(payload)
            return subprocess.CompletedProcess(argv, 0, "", "")
        return real_run(argv, **kwargs)

    def provider(audio):
        providers.append(audio)
        raise AssertionError("unusable extraction was dispatched to BCUT")

    monkeypatch.setattr(gate.subprocess, "run", extraction_fault)
    adapters = gate.FinalSubtitleAudioGateAdapters(gate._default_extract_audio, provider, lambda _: "")
    with pytest.raises(gate.FinalSubtitleAudioGateError):
        gate.capture_final_subtitle_audio_check(
            record, subtitle, tmp_path / "evidence", "synthetic", adapters=adapters,
        )
    assert providers == []
    assert "subtitle_audio_correspondence" not in record
    assert not list((tmp_path / "evidence").glob("*.subtitle-audio-bcut.raw.json"))


@pytest.mark.skipif(not shutil.which("ffmpeg") or not shutil.which("ffprobe"), reason="media tools absent")
@pytest.mark.parametrize("silent", [False, True])
def test_real_extractor_preserves_decodable_audio_including_silence(tmp_path, silent):
    media = tmp_path / "synthetic.wav"
    samples = b"\0\0\0\0" if silent else b"\x00\x08\x00\xf8"
    with wave.open(str(media), "wb") as sound:
        sound.setparams((1, 2, 16_000, 0, "NONE", "not compressed"))
        sound.writeframes(samples * 8_000 * 3)
    output = tmp_path / "output.mp3"
    before = hashlib.sha256(media.read_bytes()).hexdigest()
    gate._default_extract_audio(media, output)
    decoded = subprocess.run(
        ["ffmpeg", "-nostdin", "-v", "error", "-i", str(output),
         "-f", "s16le", "-ac", "1", "-ar", "16000", "pipe:1"],
        stdin=subprocess.DEVNULL, capture_output=True, timeout=10, check=False,
    )
    assert decoded.returncode == 0 and decoded.stderr == b""
    assert len(decoded.stdout) == 3 * 16_000 * 2
    if silent:
        assert not any(decoded.stdout)
    assert hashlib.sha256(media.read_bytes()).hexdigest() == before
    print(json.dumps({"silent": silent, "source_seconds": 3,
                      "decoded_pcm_bytes": len(decoded.stdout),
                      "decoded_seconds": len(decoded.stdout) / 32_000}))


@pytest.mark.parametrize("defect", [
    "nonzero", "stderr_error", "invalid_json", "not_object", "no_streams", "extra_stream",
    "not_audio", "wrong_codec", "wrong_sample_rate", "wrong_channels", "zero_frames",
    "zero_duration", "nan_duration", "infinite_duration", "missing_duration", "timeout",
])
def test_extracted_audio_probe_fails_closed(tmp_path, monkeypatch, defect):
    audio = tmp_path / "synthetic.mp3"
    audio.write_bytes(b"synthetic probe fixture")
    stream = {"codec_type": "audio", "codec_name": "mp3", "sample_rate": "16000",
              "channels": 1, "duration": "3", "nb_read_frames": "84"}
    changed = {
        "not_audio": ("codec_type", "video"), "wrong_codec": ("codec_name", "aac"),
        "wrong_sample_rate": ("sample_rate", "48000"), "wrong_channels": ("channels", 2),
        "zero_frames": ("nb_read_frames", "0"), "zero_duration": ("duration", "0"),
        "nan_duration": ("duration", "NaN"), "infinite_duration": ("duration", "Infinity"),
    }
    if defect in changed:
        key, value = changed[defect]
        stream[key] = value
    if defect == "missing_duration":
        del stream["duration"]
    document = {"streams": [stream]}
    if defect == "no_streams":
        document["streams"] = []
    elif defect == "extra_stream":
        document["streams"].append(dict(stream))
    output = json.dumps([] if defect == "not_object" else document)
    if defect == "invalid_json":
        output = "not JSON"

    def probe(argv, **kwargs):
        assert argv[0] == "ffprobe" and "-count_frames" in argv
        assert kwargs["stdin"] == subprocess.DEVNULL
        if defect == "timeout":
            raise subprocess.TimeoutExpired(argv, kwargs["timeout"])
        return subprocess.CompletedProcess(
            argv, 1 if defect == "nonzero" else 0, output,
            "synthetic decoder failure" if defect == "stderr_error" else "",
        )

    monkeypatch.setattr(gate.subprocess, "run", probe)
    with pytest.raises(gate.FinalSubtitleAudioGateError, match="extracted audio decode validation failed"):
        gate._validate_extracted_audio_media(audio)


@pytest.mark.skipif(not shutil.which("ffmpeg") or not shutil.which("ffprobe"), reason="media tools absent")
def test_capture_consumer_and_warm_cache_keep_their_existing_contract(tmp_path):
    """Real media I/O with an explicitly synthetic transcript, not speech truth."""
    from scripts.free_asr_client import to_srt
    from tests.subtitle_audio_test_support import SYNTHETIC_ROWS, SYNTHETIC_SRT

    media = tmp_path / "synthetic.wav"
    with wave.open(str(media), "wb") as sound:
        sound.setparams((1, 2, 16_000, 0, "NONE", "not compressed"))
        sound.writeframes((b"\x00\x08\x00\xf8" * 8_000) * 48)
    subtitle = tmp_path / "synthetic.srt"
    subtitle.write_text(SYNTHETIC_SRT, encoding="utf-8")
    before_srt = subtitle.read_bytes()
    digest = "sha256:" + hashlib.sha256(media.read_bytes()).hexdigest()
    record = {
        "artifact_hashes": {"burned_video_sha256": digest},
        "burned_preview": {"status": "BURNED", "path": str(media), "burned_sha256": digest},
    }
    calls = []

    def transcribe(audio):
        assert audio
        calls.append(len(audio))
        return {"fixture_scope": "SYNTHETIC_NOT_A_SPEECH_TRANSCRIPT", "utterances": [
            {"start_time": start, "end_time": end, "transcript": text, "words": []}
            for start, end, text in SYNTHETIC_ROWS
        ]}

    captured = gate.capture_final_subtitle_audio_check(
        record, subtitle, tmp_path, "synthetic",
        adapters=gate.FinalSubtitleAudioGateAdapters(gate._default_extract_audio, transcribe, to_srt),
    )
    assert len(calls) == 1
    assert captured["subtitle_audio_correspondence"]["status"] == "PASS"
    assert captured["subtitle_audio_correspondence"]["cache_reused"] is False
    gate.validate_final_subtitle_audio_check(
        captured, final_srt=subtitle, actual_media=media, package_root=tmp_path,
    )

    def unavailable(*_args):
        pytest.fail("a validated warm capture must not re-extract, re-probe, or call a provider")

    repeated = gate.capture_final_subtitle_audio_check(
        captured, subtitle, tmp_path, "synthetic",
        adapters=gate.FinalSubtitleAudioGateAdapters(unavailable, unavailable, unavailable),
    )
    assert repeated["subtitle_audio_correspondence"]["cache_reused"] is True
    assert subtitle.read_bytes() == before_srt
    assert "sha256:" + hashlib.sha256(media.read_bytes()).hexdigest() == digest
    assert len(calls) == 1
