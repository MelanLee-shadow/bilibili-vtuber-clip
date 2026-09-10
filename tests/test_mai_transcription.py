import hashlib
import json
from pathlib import Path

import pytest

import src.autoslice.mai_transcription as mai


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
        self.read_count = 0
        self.closed = False

    def close(self):
        self.closed = True

    def getcode(self):
        return self._status_code

    def read(self):
        self.read_count += 1
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
    monkeypatch.setattr(mai.urllib.request, "build_opener", fake_build_opener)


def _configure(monkeypatch, *, endpoint="https://resource.services.ai.azure.com/api/projects/demo"):
    monkeypatch.setenv("AZURE_ENDPOINT", endpoint)
    monkeypatch.setenv("AZURE_API_KEY", "test-secret-key")
    monkeypatch.delenv("AUTOSLICE_MAI_API_KEY_FILE", raising=False)


def _worded_phrase(text, *, start=0, speaker=0, step=100, duration=None):
    words = []
    for index, char in enumerate(text):
        words.append(
            {
                "text": char,
                "offsetMilliseconds": start + index * step,
                "durationMilliseconds": 50,
            }
        )
    phrase_duration = duration or max(100, len(text) * step)
    return {
        "speaker": speaker,
        "offsetMilliseconds": start,
        "durationMilliseconds": phrase_duration,
        "text": text,
        "words": words,
        "locale": "zh",
        "confidence": 0,
    }


def _payload(*phrases, duration=None):
    end = max(
        (phrase["offsetMilliseconds"] + phrase["durationMilliseconds"] for phrase in phrases),
        default=0,
    )
    return {
        "durationMilliseconds": duration if duration is not None else end,
        "combinedPhrases": [{"text": "".join(phrase["text"] for phrase in phrases)}],
        "phrases": list(phrases),
    }


def test_good_response_uses_native_words_and_fixed_definition(monkeypatch):
    _configure(monkeypatch)
    text = "甲乙丙丁戊己庚辛壬癸子丑寅卯辰巳午未申酉戌亥一二三四五六七八九。再见"
    payload = _payload(
        _worded_phrase(text, start=100, speaker=0, step=100, duration=5_000),
        {"speaker": None, "offsetMilliseconds": 5_500, "durationMilliseconds": 500, "text": "嗯"},
    )
    response = _Response(payload)
    calls = _CallLog()
    _install_opener(monkeypatch, response, calls)

    srt, metadata = mai.transcribe_mai(b"mp3-bytes", duration_ms=6_000)

    assert srt.startswith("1\n00:00:00,100 --> ")
    assert "甲乙丙丁" in srt and "再见" in srt
    assert [line for line in srt.splitlines() if line.isdigit()] == ["1", "2", "3"]
    assert metadata["provider"] == "mai"
    assert metadata["model"] == mai.MAI_MODEL
    assert metadata["input_audio_sha256"] == hashlib.sha256(b"mp3-bytes").hexdigest()
    assert metadata["response_sha256"] == hashlib.sha256(response.content).hexdigest()
    assert metadata["http_status"] == 200
    assert metadata["speaker_labels"] == ["S00"]
    assert metadata["segment_count"] == len(metadata["segments"]) == 3
    assert metadata["native_segments"][0]["speaker"] == "S00"
    assert metadata["native_segments"][1]["speaker"] is None
    assert metadata["native_segments"][0]["words"][0] == {
        "text": "甲",
        "start_ms": 100,
        "end_ms": 150,
    }
    assert "test-secret-key" not in repr(metadata)

    assert len(calls) == 1
    request, timeout = calls[0]
    assert request.full_url == (
        "https://resource.services.ai.azure.com/speechtotext/transcriptions:transcribe"
        "?api-version=2025-10-15"
    )
    assert timeout == mai.MAI_TIMEOUT_SECONDS == 300
    assert next(
        value
        for key, value in request.header_items()
        if key.lower() == "ocp-apim-subscription-key"
    ) == "test-secret-key"
    assert any(isinstance(handler, mai._NoRedirect) for handler in calls.handlers)
    body = request.data
    assert b'name="audio"; filename="audio.mp3"' in body
    assert b'name="definition"' in body
    definition_body = body.split(b'name="definition"\r\n\r\n', 1)[1].split(
        b"\r\n--", 1
    )[0]
    assert json.loads(definition_body) == {
        "enhancedMode": {
            "enabled": True,
            "model": "MAI-Transcribe-2",
            "modelOptions": {"transcribeStyle": "verbatim", "timestamps": "word"},
        },
        "diarization": {"enabled": True},
        "profanityFilterMode": "None",
    }


def test_one_ms_word_rounding_is_preserved(monkeypatch):
    _configure(monkeypatch)
    phrase = {
        "speaker": 0,
        "offsetMilliseconds": 0,
        "durationMilliseconds": 100,
        "text": "甲乙",
        "words": [
            {"text": "甲", "offsetMilliseconds": 0, "durationMilliseconds": 50},
            {"text": "乙", "offsetMilliseconds": 50, "durationMilliseconds": 51},
        ],
    }
    response = _Response(_payload(phrase, duration=101))
    calls = _CallLog()
    _install_opener(monkeypatch, response, calls)

    srt, metadata = mai.transcribe_mai(b"audio", duration_ms=101)

    assert "00:00:00,000 --> 00:00:00,101" in srt
    assert metadata["native_segments"][0]["words"][-1]["end_ms"] == 101


def test_invalid_word_order_retains_native_response(monkeypatch):
    _configure(monkeypatch)
    phrase = _worded_phrase("甲乙")
    phrase["words"][1]["offsetMilliseconds"] = 25
    payload = _payload(phrase)
    response = _Response(payload)
    calls = _CallLog()
    _install_opener(monkeypatch, response, calls)
    with pytest.raises(mai.MaiTranscriptionError) as caught:
        mai.transcribe_mai(b"audio")
    assert caught.value.reason_code == "MAI_WORDS_OUT_OF_ORDER"
    assert caught.value.metadata["raw_response"] == payload
    assert caught.value.metadata["http_status"] == 200
    assert "segments" not in caught.value.metadata


def test_combined_text_mismatch_is_rejected(monkeypatch):
    _configure(monkeypatch)
    phrase = {"speaker": 0, "offsetMilliseconds": 0, "durationMilliseconds": 100, "text": "甲"}
    payload = _payload(phrase, duration=100)
    payload["combinedPhrases"] = [{"text": "乙"}]
    response = _Response(payload)
    calls = _CallLog()
    _install_opener(monkeypatch, response, calls)

    with pytest.raises(mai.MaiTranscriptionError) as exc_info:
        mai.transcribe_mai(b"audio")

    assert exc_info.value.reason_code == "MAI_COMBINED_TEXT_MISMATCH"


def test_one_ms_word_overrun_cannot_create_overlapping_srt(monkeypatch):
    _configure(monkeypatch)
    first = {
        "speaker": 0,
        "offsetMilliseconds": 0,
        "durationMilliseconds": 100,
        "text": "甲",
        "words": [{"text": "甲", "offsetMilliseconds": 0, "durationMilliseconds": 101}],
    }
    second = {"speaker": 0, "offsetMilliseconds": 100, "durationMilliseconds": 100, "text": "乙"}
    payload = _payload(first, second, duration=200)
    response = _Response(payload)
    calls = _CallLog()
    _install_opener(monkeypatch, response, calls)

    with pytest.raises(mai.MaiTranscriptionError) as exc_info:
        mai.transcribe_mai(b"audio")

    assert exc_info.value.reason_code == "MAI_OVERLAP"
    assert exc_info.value.metadata["segments"][0]["end_ms"] == 101


@pytest.mark.parametrize(
    ("phrase", "reason"),
    [
        ({"speaker": 0, "offsetMilliseconds": 0, "durationMilliseconds": 1, "text": ""}, "MAI_PHRASE_INVALID"),
        (
            {
                "speaker": 0,
                "offsetMilliseconds": 0,
                "durationMilliseconds": 100,
                "text": "甲乙",
                "words": [{"text": "甲", "offsetMilliseconds": 0, "durationMilliseconds": 50}],
            },
            "MAI_WORD_TEXT_MISMATCH",
        ),
        (
            {"speaker": 0, "offsetMilliseconds": -1, "durationMilliseconds": 1, "text": "甲"},
            "MAI_TIME_INVALID",
        ),
        (
            {
                "speaker": 0,
                "offsetMilliseconds": 0,
                "durationMilliseconds": 1_000,
                "text": "甲" * 33,
            },
            "MAI_WORDS_MISSING",
        ),
    ],
)
def test_malformed_response_fails_closed(monkeypatch, phrase, reason):
    _configure(monkeypatch)
    response = _Response(_payload(phrase, duration=2_000))
    calls = _CallLog()
    _install_opener(monkeypatch, response, calls)

    with pytest.raises(mai.MaiTranscriptionError) as exc_info:
        mai.transcribe_mai(b"audio")

    assert exc_info.value.reason_code == reason
    assert len(calls) == 1


def test_overlap_rejected_with_native_evidence_for_fallback(monkeypatch):
    _configure(monkeypatch)
    first = {"speaker": 0, "offsetMilliseconds": 0, "durationMilliseconds": 1_000, "text": "甲"}
    second = {"speaker": 1, "offsetMilliseconds": 900, "durationMilliseconds": 500, "text": "乙"}
    payload = _payload(first, second, duration=1_400)
    response = _Response(payload)
    calls = _CallLog()
    _install_opener(monkeypatch, response, calls)

    with pytest.raises(mai.MaiTranscriptionError) as exc_info:
        mai.transcribe_mai(b"audio")

    error = exc_info.value
    assert error.reason_code == "MAI_OVERLAP"
    assert error.metadata["raw_response"] == payload
    assert error.metadata["native_segments"] == [
        {"start_ms": 0, "end_ms": 1_000, "text": "甲", "speaker": "S00", "words": []},
        {"start_ms": 900, "end_ms": 1_400, "text": "乙", "speaker": "S01", "words": []},
    ]
    assert error.metadata["speaker_labels"] == ["S00", "S01"]
    assert "test-secret-key" not in repr(error)


def test_http_failure_echo_and_redirect_are_sanitized_without_retry(monkeypatch):
    _configure(monkeypatch)
    calls = _CallLog()
    response = _Response({"error": "test-secret-key"}, status_code=503)
    _install_opener(monkeypatch, response, calls)

    with pytest.raises(mai.MaiTranscriptionError) as failure:
        mai.transcribe_mai(b"audio")
    assert failure.value.reason_code == "MAI_HTTP_ERROR"
    assert response.read_count == 0
    assert len(calls) == 1
    assert "test-secret-key" not in str(failure.value)

    response = _Response(status_code=200, content=b'{"error":"test-secret-key"}')
    _install_opener(monkeypatch, response, calls)
    with pytest.raises(mai.MaiTranscriptionError) as echo:
        mai.transcribe_mai(b"audio")
    assert echo.value.reason_code == "MAI_CREDENTIAL_ECHO"
    assert len(calls) == 2

    response = _Response(status_code=302, content=b"redirect body")
    _install_opener(monkeypatch, response, calls)
    with pytest.raises(mai.MaiTranscriptionError) as redirect:
        mai.transcribe_mai(b"audio")
    assert redirect.value.reason_code == "MAI_REDIRECT"
    assert response.read_count == 0
    assert len(calls) == 3


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://resource.services.ai.azure.com",
        "https://user:secret@resource.services.ai.azure.com",
        "https://resource.services.ai.azure.com:443",
        "https://resource.services.ai.azure.com?x=1",
        "https://resource.services.ai.azure.com#frag",
        "https://resource.example.invalid",
        "https://resource.services.ai.azure.com/not-a-project",
    ],
)
def test_endpoint_must_be_known_https_origin(monkeypatch, endpoint):
    _configure(monkeypatch, endpoint=endpoint)
    calls = _CallLog()
    _install_opener(monkeypatch, _Response({}), calls)

    with pytest.raises(mai.MaiTranscriptionError) as exc_info:
        mai.transcribe_mai(b"audio")

    assert exc_info.value.reason_code == "MAI_ENDPOINT_INVALID"
    assert not calls


def test_owner_only_key_file_and_input_extensions(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("AZURE_ENDPOINT", "https://resource.services.ai.azure.com")
    monkeypatch.delenv("AZURE_API_KEY", raising=False)
    key_path = tmp_path / "mai.key"
    key_path.write_text("test-secret-key\n", encoding="utf-8")
    key_path.chmod(0o644)
    monkeypatch.setenv("AUTOSLICE_MAI_API_KEY_FILE", str(key_path))
    audio_path = tmp_path / "talk.wav"
    audio_path.write_bytes(b"audio")
    calls = _CallLog()
    _install_opener(monkeypatch, _Response({}), calls)

    with pytest.raises(mai.MaiTranscriptionError) as exc_info:
        mai.transcribe_mai(audio_path)
    assert exc_info.value.reason_code == "MAI_API_KEY_FILE_PERMISSIONS"
    assert "test-secret-key" not in str(exc_info.value)
    assert not calls

    monkeypatch.delenv("AUTOSLICE_MAI_API_KEY_FILE", raising=False)
    monkeypatch.setenv("AZURE_API_KEY", "test-secret-key")
    with pytest.raises(mai.MaiTranscriptionError) as unsupported:
        mai.transcribe_mai(tmp_path / "talk.ogg")
    assert unsupported.value.reason_code == "MAI_AUDIO_INVALID"

    with pytest.raises(mai.MaiTranscriptionError) as empty:
        mai.transcribe_mai(b"")
    assert empty.value.reason_code == "MAI_AUDIO_INVALID"
