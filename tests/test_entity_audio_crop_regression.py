"""Regression for successful FFmpeg exits that leave unusable witness media."""
import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from src.autoslice import entity_audio_verifier as module


def _probe(duration=2.4, *, silent=False):
    # Silence is valid evidence for an inaudible cue; never test amplitude here.
    return {"streams": [
        {"codec_type": "video", "width": 320, "height": 240,
         "duration": str(duration), "nb_read_frames": "24"},
        {"codec_type": "audio", "sample_rate": "48000", "channels": 2,
         "duration": str(duration), "nb_read_frames": "113"},
    ], "format": {"duration": str(duration)}}


def _fake_run(tmp_path, monkeypatch, payload):
    commands = []
    def run(command, **kwargs):
        commands.append((command, kwargs))
        if command[0] == "ffmpeg":
            Path(command[-1]).write_bytes(b"synthetic container bytes")
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        assert command[0] == "ffprobe"
        return SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")
    monkeypatch.setattr(module.subprocess, "run", run)
    return commands


def test_zero_stream_file_is_not_a_successful_audio_crop(tmp_path, monkeypatch):
    _fake_run(tmp_path, monkeypatch, {"streams": [], "format": {"duration": "0"}})
    ok, detail = module._crop_black_frame_audio(
        source_media=tmp_path / "source.mp4", audio_path=tmp_path / "input.mp4",
        start_ms=54100, end_ms=56500,
    )
    assert not ok, "rc=0 and file existence must not send an empty witness to a provider"
    assert detail


def test_crop_ffmpeg_never_consumes_callers_stdin(tmp_path, monkeypatch):
    commands = _fake_run(tmp_path, monkeypatch, _probe())
    ok, _ = module._crop_black_frame_audio(
        source_media=tmp_path / "source.mp4", audio_path=tmp_path / "input.mp4",
        start_ms=54100, end_ms=56500,
    )
    assert ok
    ffmpeg, kwargs = commands[0]
    assert "-nostdin" in ffmpeg
    assert kwargs["stdin"] is subprocess.DEVNULL
    assert ffmpeg.index("-ss") < ffmpeg.index("-i")


@pytest.mark.parametrize("payload", [
    {"streams": [{"codec_type": "video", "duration": "2.4", "nb_read_frames": "24"}]},
    {"streams": [], "format": {"duration": "2.4"}},
    _probe(0), _probe(0.3), _probe(float("nan")),
    {"streams": [dict(row, nb_read_frames="0") for row in _probe()["streams"]]},
])
def test_bad_or_truncated_media_fails_before_audio_provider(tmp_path, monkeypatch, payload):
    _fake_run(tmp_path, monkeypatch, payload)
    ok, detail = module._crop_black_frame_audio(
        source_media=tmp_path / "source.mp4", audio_path=tmp_path / "input.mp4",
        start_ms=54100, end_ms=56500,
    )
    assert not ok
    assert detail


def test_real_silent_short_clip_remains_usable(tmp_path):
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("real FFmpeg/ffprobe unavailable")
    source = tmp_path / "silent.mp4"
    subprocess.run([
        "ffmpeg", "-nostdin", "-v", "error", "-f", "lavfi", "-i",
        "color=c=blue:s=320x240:r=10:d=4", "-f", "lavfi", "-i",
        "anullsrc=r=48000:cl=stereo", "-t", "4", "-c:v", "libx264",
        "-c:a", "aac", "-shortest", str(source),
    ], check=True, stdin=subprocess.DEVNULL, capture_output=True, timeout=30)
    ok, detail = module._crop_black_frame_audio(
        source_media=source, audio_path=tmp_path / "input.mp4",
        start_ms=500, end_ms=2900,
    )
    assert ok, detail


def test_invalid_attachment_never_reaches_the_provider(tmp_path, monkeypatch):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"bound source bytes")
    _fake_run(tmp_path, monkeypatch, {"streams": []})
    def forbidden_provider(**_kwargs):
        raise AssertionError("an empty attachment reached an audio provider")
    monkeypatch.setattr(module, "_observe_entity_audio", forbidden_provider)
    verify = module.build_local_audio_entity_verifier(
        source_media=source, output_dir=tmp_path / "out",
        recording_date="2026-08-15", source_duration_ms=10000, agy_bin="agy-test",
    )
    request = {
        "schema_version": "chat-entity-verification-request.v1",
        "request_sha256": "a" * 64, "matched_start_ms": 2000,
        "matched_end_ms": 4000,
        "candidate_entities": [{"canonical": "first"}, {"canonical": "second"}],
    }
    result = verify(request)
    assert result["status"] == "UNCERTAIN"
    assert result["reason_code"] == "ENTITY_AUDIO_CROP_FAILED"


@pytest.mark.parametrize("failure", [OSError("probe missing"), subprocess.TimeoutExpired("ffprobe", 30)])
def test_probe_unavailable_is_an_existing_crop_failure(tmp_path, monkeypatch, failure):
    def run(command, **kwargs):
        if command[0] == "ffmpeg":
            Path(command[-1]).write_bytes(b"nonempty")
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        raise failure
    monkeypatch.setattr(module.subprocess, "run", run)
    ok, detail = module._crop_black_frame_audio(
        source_media=tmp_path / "source.mp4", audio_path=tmp_path / "input.mp4",
        start_ms=500, end_ms=2900,
    )
    assert not ok
    assert detail
