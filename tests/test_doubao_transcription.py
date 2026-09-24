import base64
import hashlib
import json
from pathlib import Path

import pytest

import src.autoslice.doubao_transcription as doubao


class _CallLog(list):
    pass


class _Response:
    def __init__(self, payload, *, provider_status="20000000", http_status=200):
        self.content = (
            payload
            if isinstance(payload, bytes)
            else json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
        )
        self._http_status = http_status
        self.headers = {
            "X-Api-Status-Code": provider_status,
            "X-Api-Message": "OK",
            "X-Tt-Logid": "test-log-id",
        }
        self.closed = False

    def getcode(self):
        return self._http_status

    def read(self, amount=-1):
        return self.content if amount < 0 else self.content[:amount]

    def close(self):
        self.closed = True


def _configure(monkeypatch):
    monkeypatch.setenv("AUTOSLICE_DOUBAO_API_KEY", "test-doubao-secret")
    monkeypatch.delenv("AUTOSLICE_DOUBAO_API_KEY_FILE", raising=False)
    monkeypatch.delenv("VOLC_ASR_API_KEY", raising=False)


def _install_opener(monkeypatch, response, calls):
    class _Opener:
        def open(self, request, *, timeout):
            calls.append((request, timeout))
            return response

    def fake_build_opener(*handlers):
        calls.handlers = handlers
        return _Opener()

    calls.handlers = ()
    monkeypatch.setattr(doubao.urllib.request, "build_opener", fake_build_opener)


def _provider_payload():
    return {
        "audio_info": {"duration": 1500},
        "result": {
            "text": "关闭透传。",
            "utterances": [
                {
                    "start_time": 0,
                    "end_time": 1500,
                    "text": "关闭透传。",
                    "words": [
                        {"start_time": 0, "end_time": 400, "text": "关"},
                        {"start_time": 400, "end_time": 800, "text": "闭"},
                        {"start_time": 800, "end_time": 1200, "text": "透"},
                        # The official example contains a zero-duration word.
                        {"start_time": 1500, "end_time": 1500, "text": "传"},
                    ],
                }
            ],
        },
    }


def _lower_headers(request):
    return {key.casefold(): value for key, value in request.header_items()}


def test_flash_uses_direct_base64_without_url_or_context(monkeypatch):
    _configure(monkeypatch)
    response = _Response(_provider_payload())
    calls = _CallLog()
    _install_opener(monkeypatch, response, calls)
    dispatched = []

    evidence = doubao.transcribe_doubao_flash_evidence(
        b"short-mp3-crop",
        duration_ms=1500,
        before_request=lambda: dispatched.append("reserved"),
    )

    assert dispatched == ["reserved"]
    assert evidence["provider"] == "doubao_flash"
    assert evidence["model"] == doubao.DOUBAO_FLASH_MODEL
    assert evidence["resource_id"] == doubao.DOUBAO_FLASH_RESOURCE
    assert evidence["authority"] == "EVIDENCE_ONLY"
    assert evidence["mutation_authorized"] is False
    assert evidence["candidate_exposure"] == "none"
    assert evidence["status"] == "OK"
    assert evidence["native_segments"][0]["words"][-1] == {
        "text": "传",
        "start_ms": 1500,
        "end_ms": 1500,
    }
    assert evidence["input_audio_sha256"] == hashlib.sha256(b"short-mp3-crop").hexdigest()
    assert "test-doubao-secret" not in repr(evidence)

    assert len(calls) == 1
    request, timeout = calls[0]
    assert request.full_url == doubao.DOUBAO_FLASH_ENDPOINT
    assert timeout == doubao.DOUBAO_TIMEOUT_SECONDS
    headers = _lower_headers(request)
    assert headers["x-api-key"] == "test-doubao-secret"
    assert headers["x-api-resource-id"] == doubao.DOUBAO_FLASH_RESOURCE
    assert headers["x-api-sequence"] == "-1"
    body = json.loads(request.data)
    assert body["audio"] == {
        "format": "mp3",
        "data": base64.b64encode(b"short-mp3-crop").decode("ascii"),
    }
    assert "url" not in body["audio"]
    assert body["request"]["enable_ddc"] is False
    assert body["request"]["show_utterances"] is True
    assert "corpus" not in body["request"]
    assert "context" not in request.data.decode("utf-8")
    assert any(isinstance(handler, doubao._NoRedirect) for handler in calls.handlers)
    assert response.closed is True


def test_flash_malformed_timing_and_provider_error_fail_closed(monkeypatch):
    _configure(monkeypatch)
    malformed = _provider_payload()
    malformed["result"]["utterances"][0]["end_time"] = 2501
    calls = _CallLog()
    _install_opener(monkeypatch, _Response(malformed), calls)

    with pytest.raises(doubao.DoubaoTranscriptionError) as invalid:
        doubao.transcribe_doubao_flash_evidence(b"audio", duration_ms=1500)
    assert invalid.value.reason_code == "DOUBAO_DURATION_OUT_OF_BOUNDS"
    assert len(calls) == 1

    calls = _CallLog()
    _install_opener(monkeypatch, _Response(b"", provider_status="55000031"), calls)
    with pytest.raises(doubao.DoubaoTranscriptionError) as rejected:
        doubao.transcribe_doubao_flash_evidence(b"audio", duration_ms=1500)
    assert rejected.value.reason_code == "DOUBAO_PROVIDER_ERROR"
    assert rejected.value.metadata["provider_status_code"] == 55000031
    assert "test-doubao-secret" not in str(rejected.value)
    assert len(calls) == 1


def test_flash_rejects_too_short_audio_before_network(monkeypatch):
    _configure(monkeypatch)
    monkeypatch.setattr(
        doubao,
        "_request_once",
        lambda *args, **kwargs: pytest.fail("network must not be called"),
    )

    with pytest.raises(doubao.DoubaoTranscriptionError) as exc_info:
        doubao.transcribe_doubao_flash_evidence(b"audio", duration_ms=999)
    assert exc_info.value.reason_code == "DOUBAO_AUDIO_TOO_SHORT"


def test_api_key_file_must_be_owner_only_regular(monkeypatch, tmp_path: Path):
    monkeypatch.delenv("AUTOSLICE_DOUBAO_API_KEY", raising=False)
    monkeypatch.delenv("VOLC_ASR_API_KEY", raising=False)
    key_path = tmp_path / "doubao.key"
    key_path.write_text("secret\n", encoding="utf-8")
    key_path.chmod(0o644)
    monkeypatch.setenv("AUTOSLICE_DOUBAO_API_KEY_FILE", str(key_path))

    with pytest.raises(doubao.DoubaoTranscriptionError) as exc_info:
        doubao.transcribe_doubao_flash_evidence(b"audio", duration_ms=1500)
    assert exc_info.value.reason_code == "DOUBAO_API_KEY_FILE_PERMISSIONS"
    assert "secret" not in str(exc_info.value)


def test_api_key_file_symlink_is_rejected_without_network(monkeypatch, tmp_path: Path):
    monkeypatch.delenv("AUTOSLICE_DOUBAO_API_KEY", raising=False)
    monkeypatch.delenv("VOLC_ASR_API_KEY", raising=False)
    target = tmp_path / "real.key"
    target.write_text("secret\n", encoding="utf-8")
    target.chmod(0o600)
    link = tmp_path / "doubao.key"
    link.symlink_to(target)
    monkeypatch.setenv("AUTOSLICE_DOUBAO_API_KEY_FILE", str(link))
    monkeypatch.setattr(
        doubao,
        "_request_once",
        lambda *args, **kwargs: pytest.fail("network must not be called"),
    )

    with pytest.raises(doubao.DoubaoTranscriptionError) as exc_info:
        doubao.transcribe_doubao_flash_evidence(b"audio", duration_ms=1500)
    assert exc_info.value.reason_code == "DOUBAO_API_KEY_FILE_INVALID"


def test_audio_path_symlink_is_rejected_before_network(monkeypatch, tmp_path: Path):
    _configure(monkeypatch)
    target = tmp_path / "real.mp3"
    target.write_bytes(b"audio")
    link = tmp_path / "link.mp3"
    link.symlink_to(target)
    monkeypatch.setattr(
        doubao,
        "_request_once",
        lambda *args, **kwargs: pytest.fail("network must not be called"),
    )

    with pytest.raises(doubao.DoubaoTranscriptionError) as exc_info:
        doubao.transcribe_doubao_flash_evidence(link, duration_ms=1500)
    assert exc_info.value.reason_code == "DOUBAO_AUDIO_INVALID"


def test_flash_contract_is_not_mislabeled_as_seed_or_version_2():
    assert doubao.DOUBAO_FLASH_RESOURCE == "volc.bigasr.auc_turbo"
    assert "seed" not in doubao.DOUBAO_FLASH_MODEL.casefold()
    assert "2.0" not in doubao.DOUBAO_FLASH_MODEL
    assert not hasattr(doubao, "transcribe_doubao_seed_url_evidence")



def test_response_body_and_headers_are_bounded_and_cannot_echo_key(monkeypatch):
    _configure(monkeypatch)
    monkeypatch.setattr(doubao, "MAX_RESPONSE_BYTES", 8)
    oversized = _Response(b"123456789")
    calls = _CallLog()
    _install_opener(monkeypatch, oversized, calls)
    with pytest.raises(doubao.DoubaoTranscriptionError) as too_large:
        doubao.transcribe_doubao_flash_evidence(b"audio", duration_ms=1500)
    assert too_large.value.reason_code == "DOUBAO_RESPONSE_INVALID"

    echoed = _Response(_provider_payload())
    echoed.headers["X-Tt-Logid"] = "prefix-test-doubao-secret-suffix"
    calls = _CallLog()
    _install_opener(monkeypatch, echoed, calls)
    monkeypatch.setattr(doubao, "MAX_RESPONSE_BYTES", 20_000_000)
    with pytest.raises(doubao.DoubaoTranscriptionError) as echo:
        doubao.transcribe_doubao_flash_evidence(b"audio", duration_ms=1500)
    assert echo.value.reason_code == "DOUBAO_CREDENTIAL_ECHO"
    assert "test-doubao-secret" not in str(echo.value)

    invalid = _Response(_provider_payload())
    invalid.headers["X-Api-Message"] = "bad\nheader"
    calls = _CallLog()
    _install_opener(monkeypatch, invalid, calls)
    with pytest.raises(doubao.DoubaoTranscriptionError) as malformed:
        doubao.transcribe_doubao_flash_evidence(b"audio", duration_ms=1500)
    assert malformed.value.reason_code == "DOUBAO_RESPONSE_HEADER_INVALID"



@pytest.mark.parametrize("extension", ["mp3", "wav", "ogg"])
def test_expected_hash_binds_request_bytes_without_changing_format(tmp_path, monkeypatch, extension):
    _configure(monkeypatch)
    audio = tmp_path / ("crop." + extension)
    original = b"frozen synthetic crop"
    audio.write_bytes(original)
    calls = _CallLog()
    _install_opener(monkeypatch, _Response(_provider_payload()), calls)

    def after_read():
        # The path may change after the verified bytes have been put in memory.
        audio.write_bytes(b"later unrelated data")

    evidence = doubao.transcribe_doubao_flash_evidence(
        audio, duration_ms=1500, before_request=after_read,
        expected_audio_sha256=hashlib.sha256(original).hexdigest(),
    )
    assert len(calls) == 1
    payload = json.loads(calls[0][0].data)
    assert payload["audio"]["format"] == extension
    assert base64.b64decode(payload["audio"]["data"]) == original
    assert evidence["input_audio_sha256"] == hashlib.sha256(original).hexdigest()


@pytest.mark.parametrize("expected", ["0" * 64, "bad", True])
def test_bad_or_mismatched_audio_binding_precedes_credentials_and_dispatch(monkeypatch, expected):
    attempts = []
    monkeypatch.setattr(doubao, "_read_api_key", lambda *_: pytest.fail("must precede credential access"))
    monkeypatch.setattr(doubao, "_request_once", lambda *args: pytest.fail("must precede HTTP"))
    with pytest.raises(doubao.DoubaoTranscriptionError) as error:
        doubao.transcribe_doubao_flash_evidence(
            b"synthetic crop", duration_ms=1500,
            before_request=lambda: attempts.append(True), expected_audio_sha256=expected,
        )
    assert error.value.reason_code in {"DOUBAO_AUDIO_INVALID", "DOUBAO_AUDIO_CHANGED"}
    assert attempts == []
