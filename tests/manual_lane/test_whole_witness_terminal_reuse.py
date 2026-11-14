"""Whole-witness responses traverse the real provider parser and on-disk reuse boundary."""

import hashlib
import importlib.util
import json
from pathlib import Path
import urllib.request

import pytest

from src.autoslice import mai_transcription, moss_transcription


_PATH = Path(__file__).resolve().parents[2] / "assets/manual_lane/remote/witness_asr.py"
_SPEC = importlib.util.spec_from_file_location("whole_witness_reuse_test", _PATH)
assert _SPEC and _SPEC.loader
witness = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(witness)


def mai_payload(*, bad_words=False):
    words = [
        {"text": "a", "offsetMilliseconds": 0, "durationMilliseconds": 300},
        {"text": "b", "offsetMilliseconds": 200 if bad_words else 300, "durationMilliseconds": 200},
    ]
    return {"durationMilliseconds": 1000, "phrases": [
        {"text": "ab", "offsetMilliseconds": 0, "durationMilliseconds": 500, "words": words}
    ]}


def moss_payload(*, invalid=False, empty=False):
    segments = [] if empty else [{"text": "" if invalid else "ab", "start": 0, "end": 0.5}]
    return {"text": "", "segments": segments, "usage": {"seconds": 1}}


@pytest.fixture
def transport(monkeypatch):
    # The real request and parser run, but no network or real credential can be used.
    env = {"AZURE_ENDPOINT": "https://example.cognitiveservices.azure.com",
           "AZURE_API_KEY": "fixture-key", "AUTOSLICE_MOSS_API_KEY": "fixture-key"}
    monkeypatch.setattr(mai_transcription, "_runtime_environment", lambda environment=None: env)
    monkeypatch.setattr(moss_transcription, "_runtime_environment", lambda environment=None: env)
    calls = []
    reply = {"status": 200, "payload": {}}

    class Response:
        def getcode(self):
            return reply["status"]

        def read(self):
            if "raw" in reply:
                return reply["raw"]
            return json.dumps(reply["payload"]).encode()

        def close(self):
            pass

    class Opener:
        def open(self, request, timeout):
            calls.append(request.full_url)
            if reply.get("timeout"):
                raise TimeoutError("fixture timeout")
            return Response()

    monkeypatch.setattr(urllib.request, "build_opener", lambda *args: Opener())
    return reply, calls


def invoke(name, audio, outdir):
    digest = hashlib.sha256(audio.read_bytes()).hexdigest()
    return witness.record(name, witness.provider_call(name), audio, outdir, 1000, digest)


@pytest.mark.parametrize(
    ("name", "payload", "reason", "billed"),
    [
        ("mai", {"durationMilliseconds": 1000, "phrases": [{"text": "", "offsetMilliseconds": 0,
                                                             "durationMilliseconds": 500}]},
         "MAI_PHRASE_INVALID", {"duration_ms": 1000}),
        ("moss", moss_payload(invalid=True), "MOSS_SEGMENT_INVALID", {"usage": {"seconds": 1}}),
        ("moss", moss_payload(empty=True), "MOSS_NO_SPEECH", {"usage": {"seconds": 1}}),
    ],
)
def test_http_200_unavailable_is_persisted_and_reused(tmp_path, transport, name, payload, reason, billed):
    reply, calls = transport
    reply["payload"] = payload
    audio = tmp_path / "audio.mp3"
    audio.write_bytes(b"fixture audio")
    first = invoke(name, audio, tmp_path)
    saved = (tmp_path / f"{name}.json").read_bytes()
    again = invoke(name, audio, tmp_path)
    assert len(calls) == 1
    assert first == again == json.loads(saved)
    assert (tmp_path / f"{name}.json").read_bytes() == saved
    assert first["status"] == "UNAVAILABLE" and first["reason_code"] == reason
    assert first["http_status"] == 200 and first["billed"] == billed
    assert not first.get("segments")


@pytest.mark.parametrize(("status", "payload", "raw", "reason"), [
    (201, moss_payload(empty=True), None, "MOSS_NO_SPEECH"),
    (204, None, b"", "MOSS_RESPONSE_NOT_JSON"),
    (299, moss_payload(empty=True), None, "MOSS_NO_SPEECH"),
])
def test_other_completed_2xx_members_are_reused(tmp_path, transport, status, payload, raw, reason):
    reply, calls = transport
    reply.update(status=status, payload=payload)
    if raw is not None:
        reply["raw"] = raw
    audio = tmp_path / "audio.mp3"
    audio.write_bytes(b"fixture audio")
    first = invoke("moss", audio, tmp_path)
    assert first["status"] == "UNAVAILABLE" and first["reason_code"] == reason
    assert first["http_status"] == status and not first.get("segments")
    assert invoke("moss", audio, tmp_path) == first
    assert len(calls) == 1


@pytest.mark.parametrize(("name", "payload"), [("mai", mai_payload()), ("moss", moss_payload())])
def test_success_reuses_same_audio_but_changed_audio_calls_again(tmp_path, transport, name, payload):
    reply, calls = transport
    reply["payload"] = payload
    audio = tmp_path / "audio.mp3"
    audio.write_bytes(b"first audio")
    first = invoke(name, audio, tmp_path)
    assert first["status"] == "OK" and first["segments"] and first["http_status"] == 200
    assert invoke(name, audio, tmp_path) == first
    assert len(calls) == 1
    audio.write_bytes(b"different audio")
    changed = invoke(name, audio, tmp_path)
    assert len(calls) == 2 and changed["input_sha256"] != first["input_sha256"]


def test_changed_audio_does_not_reuse_unavailable_result(tmp_path, transport):
    reply, calls = transport
    reply["payload"] = moss_payload(invalid=True)
    audio = tmp_path / "audio.mp3"
    audio.write_bytes(b"first audio")
    first = invoke("moss", audio, tmp_path)
    audio.write_bytes(b"different audio")
    changed = invoke("moss", audio, tmp_path)
    assert first["reason_code"] == changed["reason_code"] == "MOSS_SEGMENT_INVALID"
    assert len(calls) == 2 and changed["input_sha256"] != first["input_sha256"]


def test_mai_reply_with_unusable_word_times_still_gives_its_phrases_from_the_one_call(tmp_path, transport):
    # measured one training clip in twelve lost MAI altogether because two word times overlapped
    reply, calls = transport
    reply["payload"] = mai_payload(bad_words=True)
    audio = tmp_path / "audio.mp3"
    audio.write_bytes(b"fixture audio")
    first = invoke("mai", audio, tmp_path)
    assert first["status"] == "OK" and [s["text"] for s in first["segments"]] == ["ab"]
    assert first["segments"][0]["start_ms"] == 0 and first["segments"][0]["end_ms"] == 500
    assert {"reason_code": "MAI_WORDS_OUT_OF_ORDER"} in first["diagnostics"] and first["billed"] == {"duration_ms": 1000}
    assert invoke("mai", audio, tmp_path) == first and len(calls) == 1


@pytest.mark.parametrize("failure", ["timeout", 429, 524])
def test_transport_and_non_200_responses_are_retried(tmp_path, transport, failure):
    reply, calls = transport
    if failure == "timeout":
        reply["timeout"] = True
    else:
        reply["status"] = failure
    audio = tmp_path / "audio.mp3"
    audio.write_bytes(b"fixture audio")
    first = invoke("mai", audio, tmp_path)
    assert first["status"] == "UNAVAILABLE"
    assert first["http_status"] == (None if failure == "timeout" else failure)
    invoke("mai", audio, tmp_path)
    assert len(calls) == 2


@pytest.mark.parametrize("prior", [
    {"status": "UNAVAILABLE"},
    {"status": "UNAVAILABLE", "http_status": 199},
    {"status": "UNAVAILABLE", "http_status": 300},
    {"status": "UNAVAILABLE", "http_status": True},
    {"status": "UNAVAILABLE", "http_status": "200"},
    {"status": "UNKNOWN", "http_status": 200},
])
def test_unknown_prior_records_are_not_promoted_to_terminal(tmp_path, transport, prior):
    reply, calls = transport
    reply["payload"] = moss_payload()
    audio = tmp_path / "audio.mp3"
    audio.write_bytes(b"fixture audio")
    prior["input_sha256"] = hashlib.sha256(audio.read_bytes()).hexdigest()
    (tmp_path / "moss.json").write_text(json.dumps(prior), encoding="utf-8")
    assert invoke("moss", audio, tmp_path)["status"] == "OK"
    assert len(calls) == 1


def test_provider_namespaces_are_independent(tmp_path, transport):
    reply, calls = transport
    audio = tmp_path / "audio.mp3"
    audio.write_bytes(b"fixture audio")
    reply["payload"] = {"durationMilliseconds": 1000, "phrases": "none"}
    mai = invoke("mai", audio, tmp_path)
    reply["payload"] = moss_payload()
    moss = invoke("moss", audio, tmp_path)
    assert len(calls) == 2
    assert mai["status"] == "UNAVAILABLE" and moss["status"] == "OK"
    assert invoke("mai", audio, tmp_path) == mai
    assert invoke("moss", audio, tmp_path) == moss
    assert len(calls) == 2
