import hashlib
import json
from pathlib import Path

import pytest

import src.autoslice.moss_transcription as moss


class _CallLog(list):
    pass


class _Response:
    def __init__(self, payload=None, *, status_code=200, content=None):
        self._status_code = status_code
        self.content = (
            content
            if content is not None
            else json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
        )
        self.closed = False

    def close(self):
        self.closed = True

    def getcode(self):
        return self._status_code

    def read(self):
        return self.content


def _install_opener(monkeypatch, response, calls):
    class _Opener:
        def open(self, request, *, timeout):
            calls.append((request, timeout))
            return response

    def fake_build_opener(*handlers):
        calls.handlers = handlers
        return _Opener()

    calls.handlers = ()
    monkeypatch.setattr(moss.urllib.request, "build_opener", fake_build_opener)


def _configure(monkeypatch):
    monkeypatch.setenv("AUTOSLICE_MOSS_API_KEY", "test-secret-key")
    monkeypatch.delenv("AUTOSLICE_MOSS_API_KEY_FILE", raising=False)


def test_good_response_is_native_srt_with_bounded_request(monkeypatch, tmp_path: Path):
    _configure(monkeypatch)
    audio_path = tmp_path / "talk.mp3"
    audio_path.write_bytes(b"audio-bytes")
    payload = {
        "segments": [
            {"start": 0.125, "end": 1.5, "text": "啊啊 123", "speaker": "S01"},
            {"start": 2, "end": 3.25, "text": "重复重复"},
        ]
    }
    response = _Response(payload)
    calls = _CallLog()
    _install_opener(monkeypatch, response, calls)

    srt, metadata = moss.transcribe_moss(audio_path, duration_ms=3_000)

    assert srt == (
        "1\n00:00:00,125 --> 00:00:01,500\n啊啊 123\n\n"
        "2\n00:00:02,000 --> 00:00:03,250\n重复重复\n"
    )
    assert metadata["provider"] == "moss"
    assert metadata["model"] == moss.MOSS_MODEL
    assert metadata["input_audio_sha256"] == hashlib.sha256(b"audio-bytes").hexdigest()
    assert metadata["response_sha256"] == hashlib.sha256(response.content).hexdigest()
    assert metadata["segment_count"] == 2
    assert metadata["speaker_labels"] == ["S01"]
    assert "test-secret-key" not in repr(metadata)

    assert len(calls) == 1
    request, timeout = calls[0]
    assert request.full_url == moss.MOSS_ENDPOINT
    assert timeout <= 600
    assert request.get_header("Authorization") == "Bearer test-secret-key"
    body = request.data
    assert b'name="model"\r\n\r\nmoss-transcribe-diarize-pro' in body
    assert b'name="diarize"\r\n\r\ntrue' in body
    assert b'name="response_format"\r\n\r\ndiarized_json' in body
    assert b'name="file"' in body
    assert any(isinstance(handler, moss._NoRedirect) for handler in calls.handlers)


def test_bytes_input_matches_saved_moss_pro_response_shape(monkeypatch):
    _configure(monkeypatch)
    # The saved 011-fresh-mandarin-panel MOSS Pro envelopes record the actual
    # response keys as duration/segments/task/text and segment rows as
    # start/end/speaker/text.  This is the provider payload shape used here.
    payload = {
        "duration": 4.0,
        "task": "transcribe",
        "text": "我不是可爱小猪，我不是可爱小猪",
        "segments": [
            {"start": 0.48, "end": 2.33, "speaker": "S01", "text": "我不是可爱小猪"},
            {"start": 2.5, "end": 4.0, "speaker": "S02", "text": "我不是可爱小猪"},
        ],
    }
    calls = _CallLog()
    _install_opener(monkeypatch, _Response(payload), calls)

    srt, metadata = moss.transcribe_moss(b"audio-bytes", duration_ms=4_000)

    assert "我不是可爱小猪" in srt
    assert metadata["speaker_labels"] == ["S01", "S02"]
    assert len(calls) == 1
    assert b'filename="audio.mp3"' in calls[0][0].data


@pytest.mark.parametrize(
    ("segments", "reason"),
    [
        ([{"start": 0, "end": 1, "text": "ok"}, {"start": 0.5, "end": 2, "text": "overlap"}], "MOSS_OVERLAP"),
        ([{"start": 0, "end": 1, "text": ""}], "MOSS_SEGMENT_INVALID"),
        ([{"start": 0, "end": 1, "text": "ok"}, {"start": 2, "end": float("nan"), "text": "bad"}], "MOSS_SEGMENT_INVALID"),
        ([], "MOSS_EMPTY_RESULT"),
    ],
)
def test_malformed_segments_fail_closed(monkeypatch, tmp_path: Path, segments, reason):
    _configure(monkeypatch)
    audio_path = tmp_path / "talk.mp3"
    audio_path.write_bytes(b"audio")
    calls = _CallLog()
    _install_opener(monkeypatch, _Response({"segments": segments}), calls)

    with pytest.raises(moss.MossTranscriptionError) as exc_info:
        moss.transcribe_moss(audio_path)

    assert exc_info.value.reason_code == reason
    assert len(calls) == 1


def test_duration_overrun_and_http_failure_are_sanitized_without_retry(monkeypatch, tmp_path: Path):
    _configure(monkeypatch)
    audio_path = tmp_path / "talk.mp3"
    audio_path.write_bytes(b"audio")
    calls = _CallLog()
    _install_opener(
        monkeypatch,
        _Response({"error": "test-secret-key body"}, status_code=503),
        calls,
    )

    with pytest.raises(moss.MossTranscriptionError) as exc_info:
        moss.transcribe_moss(audio_path, duration_ms=1_000)

    assert exc_info.value.reason_code == "MOSS_HTTP_ERROR"
    assert "test-secret-key" not in str(exc_info.value)
    assert len(calls) == 1

    _install_opener(
        monkeypatch,
        _Response({"segments": [{"start": 0, "end": 1.501, "text": "late"}]}),
        calls,
    )
    with pytest.raises(moss.MossTranscriptionError) as overrun:
        moss.transcribe_moss(audio_path, duration_ms=1_000)
    assert overrun.value.reason_code == "MOSS_DURATION_OUT_OF_BOUNDS"
    assert len(calls) == 2


def test_key_file_must_be_owner_only_regular_and_nonsymlink(monkeypatch, tmp_path: Path):
    monkeypatch.delenv("AUTOSLICE_MOSS_API_KEY", raising=False)
    key_path = tmp_path / "moss.key"
    key_path.write_text("test-secret-key\n", encoding="utf-8")
    key_path.chmod(0o644)
    monkeypatch.setenv("AUTOSLICE_MOSS_API_KEY_FILE", str(key_path))
    audio_path = tmp_path / "talk.mp3"
    audio_path.write_bytes(b"audio")

    with pytest.raises(moss.MossTranscriptionError) as exc_info:
        moss.transcribe_moss(audio_path)

    assert exc_info.value.reason_code == "MOSS_API_KEY_FILE_PERMISSIONS"
    assert "test-secret-key" not in str(exc_info.value)
