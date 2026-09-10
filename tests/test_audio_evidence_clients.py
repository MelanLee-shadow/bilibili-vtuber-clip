import hashlib
from io import BytesIO
import json
import urllib.error

import pytest

import src.autoslice.diarized_transcription as diarized
import src.autoslice.mai_transcription as mai
import src.autoslice.moss_transcription as moss
import src.autoslice.subtitle_audio_evidence as secondary
from src.autoslice.supplement_audio_budget import start_budget


class _CallLog(list):
    pass


class _Response:
    def __init__(self, payload=None, *, status_code=200, content=None):
        self.content = content if content is not None else json.dumps(
            payload, ensure_ascii=False, separators=(",", ":")
        ).encode()
        self.status_code = status_code
        self.read_count = 0

    def getcode(self):
        return self.status_code

    def read(self):
        self.read_count += 1
        return self.content

    def close(self):
        pass


def _install_opener(monkeypatch, module, response, calls):
    class _Opener:
        def open(self, request, *, timeout):
            calls.append((request, timeout))
            return response

    def fake_build_opener(*handlers):
        calls.handlers = handlers
        return _Opener()

    calls.handlers = ()
    monkeypatch.setattr(module.urllib.request, "build_opener", fake_build_opener)


def _configure_mai(monkeypatch):
    monkeypatch.setenv("AZURE_ENDPOINT", "https://resource.services.ai.azure.com")
    monkeypatch.setenv("AZURE_API_KEY", "test-secret-key")
    monkeypatch.delenv("AUTOSLICE_MAI_API_KEY_FILE", raising=False)


def _configure_moss(monkeypatch):
    monkeypatch.setenv("AUTOSLICE_MOSS_API_KEY", "test-secret-key")
    monkeypatch.delenv("AUTOSLICE_MOSS_API_KEY_FILE", raising=False)


def _mai_payload(phrases, duration):
    return {
        "durationMilliseconds": duration,
        "combinedPhrases": [{"text": "".join(phrase["text"] for phrase in phrases)}],
        "phrases": phrases,
    }


def test_mai_evidence_keeps_long_overlap_and_speaker_zero(monkeypatch):
    _configure_mai(monkeypatch)
    first = {
        "speaker": 0,
        "offsetMilliseconds": 100,
        "durationMilliseconds": 4_000,
        "text": "甲" * 40,
    }
    second = {
        "speaker": 1,
        "offsetMilliseconds": 200,
        "durationMilliseconds": 300,
        "text": "乙",
    }
    payload = _mai_payload([first, second], duration=4_500)
    response = _Response(payload)
    calls = _CallLog()
    _install_opener(monkeypatch, mai, response, calls)

    metadata = diarized.transcribe_evidence(b"audio", "mai", duration_ms=4_500)

    assert len(calls) == 1
    assert metadata["status"] == "OK"
    assert metadata["speaker_labels"] == ["S00", "S01"]
    assert metadata["native_segments"][0]["speaker"] == "S00"
    assert metadata["native_segments"][0]["text"] == first["text"]
    assert metadata["native_segments"][0]["words"] is None
    assert metadata["native_segments"][0]["words_unavailable_reason"] == "MAI_WORDS_MISSING"
    assert metadata["native_segments"][1]["start_ms"] == 200
    assert metadata["native_segments"][0]["end_ms"] > metadata["native_segments"][1]["start_ms"]
    assert metadata["native_timeline"] == {
        "has_overlap": True,
        "has_out_of_order": False,
        "one_track_srt_eligible": False,
    }
    assert metadata["one_track_srt_eligible"] is False
    assert {item["reason_code"] for item in metadata["diagnostics"]} >= {
        "MAI_NATIVE_OVERLAP",
        "MAI_WORDS_MISSING",
    }
    assert metadata["raw_response"] == payload
    assert "test-secret-key" not in repr(metadata)


def test_mai_wrong_word_evidence_is_retained_but_strict_draft_rejects(monkeypatch):
    _configure_mai(monkeypatch)
    phrase = {
        "speaker": 0,
        "offsetMilliseconds": 0,
        "durationMilliseconds": 100,
        "text": "甲乙",
        "words": [{"text": "甲", "offsetMilliseconds": 0, "durationMilliseconds": 50}],
    }
    payload = _mai_payload([phrase], duration=100)
    response = _Response(payload)
    calls = _CallLog()
    _install_opener(monkeypatch, mai, response, calls)

    metadata = diarized.transcribe_evidence(b"audio", "mai", duration_ms=100)

    assert len(calls) == 1
    assert metadata["native_segments"][0]["words"] is None
    assert metadata["native_segments"][0]["words_unavailable_reason"] == "MAI_WORD_TEXT_MISMATCH"
    assert metadata["diagnostics"] == [{"phrase_index": 1, "reason_code": "MAI_WORD_TEXT_MISMATCH"}]

    strict_response = _Response(payload)
    _install_opener(monkeypatch, mai, strict_response, calls)
    with pytest.raises(diarized.DiarizedTranscriptionError) as exc_info:
        diarized.transcribe_diarized(b"audio", provider="mai", duration_ms=100)
    assert len(calls) == 2
    assert exc_info.value.reason_code == "MAI_WORD_TEXT_MISMATCH"
    assert exc_info.value.metadata["raw_response"] == payload


def test_mai_no_speech_is_evidence_status_but_strict_draft_stays_empty(monkeypatch):
    _configure_mai(monkeypatch)
    payload = {
        "durationMilliseconds": 5_000,
        "combinedPhrases": [{"text": ""}],
        "phrases": [],
    }
    response = _Response(payload)
    calls = _CallLog()
    _install_opener(monkeypatch, mai, response, calls)

    metadata = diarized.transcribe_evidence(b"audio", "mai", duration_ms=5_000)

    assert len(calls) == 1
    assert metadata["status"] == "NO_SPEECH"
    assert metadata["native_segments"] == []
    assert metadata["raw_response"] == payload

    strict_response = _Response(payload)
    _install_opener(monkeypatch, mai, strict_response, calls)
    with pytest.raises(diarized.DiarizedTranscriptionError) as exc_info:
        diarized.transcribe_diarized(b"audio", provider="mai", duration_ms=5_000)
    assert len(calls) == 2
    assert exc_info.value.reason_code == "MAI_EMPTY_RESULT"
    assert exc_info.value.metadata["raw_response"] == payload


@pytest.mark.parametrize(
    "segments",
    [
        [
            {"start": 0, "end": 2, "text": "甲"},
            {"start": 1, "end": 3, "text": "乙"},
        ],
        [
            {"start": 2, "end": 3, "text": "乙"},
            {"start": 1, "end": 2, "text": "甲"},
        ],
    ],
)
def test_moss_evidence_keeps_valid_overlap_or_out_of_order_rows(monkeypatch, segments):
    _configure_moss(monkeypatch)
    payload = {"duration": 3.0, "segments": segments}
    response = _Response(payload)
    calls = _CallLog()
    _install_opener(monkeypatch, moss, response, calls)

    metadata = diarized.transcribe_evidence(b"audio", "moss", duration_ms=3_000)

    assert len(calls) == 1
    assert metadata["status"] == "OK"
    assert metadata["native_segments"] == [
        {"start_ms": int(row["start"] * 1000), "end_ms": int(row["end"] * 1000), "text": row["text"], "speaker": None}
        for row in segments
    ]
    expected_overlap = (
        segments[1]["start"] < segments[0]["end"]
        and segments[1]["end"] > segments[0]["start"]
    )
    expected_out_of_order = segments[1]["start"] < segments[0]["start"]
    assert metadata["native_timeline"] == {
        "has_overlap": expected_overlap,
        "has_out_of_order": expected_out_of_order,
        "one_track_srt_eligible": False,
    }
    assert metadata["one_track_srt_eligible"] is False
    assert {item["reason_code"] for item in metadata["diagnostics"]} == {
        code
        for code, present in (
            ("MOSS_NATIVE_OVERLAP", expected_overlap),
            ("MOSS_NATIVE_OUT_OF_ORDER", expected_out_of_order),
        )
        if present
    }
    assert metadata["raw_response"] == payload

    strict_response = _Response(payload)
    _install_opener(monkeypatch, moss, strict_response, calls)
    with pytest.raises(diarized.DiarizedTranscriptionError) as exc_info:
        diarized.transcribe_diarized(b"audio", provider="moss", duration_ms=3_000)
    assert len(calls) == 2
    assert exc_info.value.metadata["raw_response"] == payload
    assert exc_info.value.reason_code in {"MOSS_OVERLAP", "MOSS_SEGMENTS_OUT_OF_ORDER"}


def test_moss_empty_segments_with_text_are_unlocated_evidence(monkeypatch):
    _configure_moss(monkeypatch)
    payload = {"duration": 3.0, "text": "未定位的话", "segments": []}
    response = _Response(payload)
    calls = _CallLog()
    _install_opener(monkeypatch, moss, response, calls)

    metadata = diarized.transcribe_evidence(b"audio", "moss", duration_ms=3_000)

    assert len(calls) == 1
    assert metadata["status"] == "TEXT_UNLOCATED"
    assert metadata["native_segments"] == []
    assert metadata["diagnostics"] == [{"reason_code": "MOSS_TEXT_UNLOCATED"}]
    assert metadata["one_track_srt_eligible"] is False
    assert metadata["raw_response"] == payload


def test_moss_non_json_response_retains_sanitized_metadata(monkeypatch):
    _configure_moss(monkeypatch)
    response = _Response(content=b"not-json")
    calls = _CallLog()
    _install_opener(monkeypatch, moss, response, calls)

    with pytest.raises(diarized.DiarizedTranscriptionError) as exc_info:
        diarized.transcribe_evidence(b"audio", "moss")

    assert len(calls) == 1
    assert exc_info.value.reason_code == "MOSS_RESPONSE_NOT_JSON"
    assert exc_info.value.metadata["http_status"] == 200
    assert "raw_response" not in exc_info.value.metadata
    assert "test-secret-key" not in repr(exc_info.value.metadata)


def test_moss_response_key_echo_is_rejected_without_metadata_leak(monkeypatch):
    _configure_moss(monkeypatch)
    response = _Response(content=b'{"echo":"test-secret-key"}')
    calls = _CallLog()
    _install_opener(monkeypatch, moss, response, calls)

    with pytest.raises(diarized.DiarizedTranscriptionError) as exc_info:
        diarized.transcribe_evidence(b"audio", "moss")

    assert len(calls) == 1
    assert exc_info.value.reason_code == "MOSS_CREDENTIAL_ECHO"
    assert "test-secret-key" not in str(exc_info.value)
    assert exc_info.value.metadata is None


@pytest.mark.parametrize(
    "module,configure,status",
    [(mai, _configure_mai, 401), (moss, _configure_moss, 503)],
)
def test_http_error_keeps_only_safe_status_metadata(monkeypatch, module, configure, status):
    configure(monkeypatch)
    calls = []

    class _Opener:
        def open(self, request, *, timeout):
            calls.append((request, timeout))
            raise urllib.error.HTTPError(
                request.full_url,
                status,
                "provider secret body",
                {"X-Provider-Secret": "test-secret-key"},
                BytesIO(b"provider secret body test-secret-key"),
            )

    monkeypatch.setattr(module.urllib.request, "build_opener", lambda *_: _Opener())
    with pytest.raises(diarized.DiarizedTranscriptionError) as exc_info:
        diarized.transcribe_evidence(b"audio", "mai" if module is mai else "moss")

    assert len(calls) == 1
    assert exc_info.value.reason_code in {"MAI_HTTP_ERROR", "MOSS_HTTP_ERROR"}
    assert exc_info.value.metadata == {"http_status": status}
    assert "test-secret-key" not in repr(exc_info.value.metadata)
    assert "provider secret body" not in str(exc_info.value)


def test_budget_callback_waits_for_mai_local_validation(monkeypatch, tmp_path):
    _configure_mai(monkeypatch)
    monkeypatch.delenv("AZURE_API_KEY", raising=False)
    budget = start_budget(tmp_path / "missing-key.mp3")
    callback_calls = []

    def charge():
        callback_calls.append(True)
        budget.consume("mai", mai.MAI_MODEL, 0, 1_000)

    with pytest.raises(diarized.DiarizedTranscriptionError) as exc_info:
        diarized.transcribe_evidence(
            b"audio", "mai", duration_ms=1_000, before_request=charge
        )

    assert exc_info.value.reason_code == "MAI_API_KEY_MISSING"
    assert callback_calls == []
    assert budget.snapshot()["attempt_count"] == 0


def test_budget_callback_charges_timeout_after_moss_dispatch(monkeypatch, tmp_path):
    _configure_moss(monkeypatch)
    budget = start_budget(tmp_path / "timeout.mp3")
    callback_calls = []

    def charge():
        callback_calls.append(True)
        budget.consume("moss", moss.MOSS_MODEL, 0, 1_000)

    class _Opener:
        def open(self, request, *, timeout):
            raise TimeoutError("offline timeout")

    monkeypatch.setattr(moss.urllib.request, "build_opener", lambda *_: _Opener())
    with pytest.raises(diarized.DiarizedTranscriptionError) as exc_info:
        diarized.transcribe_evidence(
            b"audio", "moss", duration_ms=1_000, before_request=charge
        )

    assert exc_info.value.reason_code == "MOSS_TIMEOUT"
    assert callback_calls == [True]
    assert budget.snapshot()["attempt_count"] == 1
    assert budget.snapshot()["total_audio_ms"] == 1_000


def test_secondary_cache_hit_does_not_consume_budget(monkeypatch, tmp_path):
    calls = []

    def fake_transcribe(audio, provider, duration_ms=None):
        calls.append((audio, provider, duration_ms))
        model = mai.MAI_MODEL if provider == "mai" else moss.MOSS_MODEL
        return {
            "provider": provider,
            "model": model,
            "input_audio_sha256": hashlib.sha256(audio).hexdigest(),
            "response_sha256": "r" * 64,
            "native_segments": [],
        }

    monkeypatch.setattr(diarized, "transcribe_evidence", fake_transcribe)
    media = tmp_path / "cached.mp4"
    media.write_bytes(b"media")
    first = secondary.observe_secondary(
        b"audio", media_path=media, provider="mai", duration_ms=1_000
    )
    budget = start_budget(media)
    cached = secondary.observe_secondary(
        b"audio",
        media_path=media,
        provider="mai",
        duration_ms=1_000,
        supplement_source=media,
        crop_start_ms=0,
        crop_end_ms=1_000,
    )

    assert first["served_from_cache"] is False
    assert cached["served_from_cache"] is True
    assert calls == [(b"audio", "mai", 1_000)]
    assert budget.snapshot()["attempt_count"] == 0
