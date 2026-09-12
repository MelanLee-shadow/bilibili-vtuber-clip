"""Bounded tests for the optional native candidate-blind context lane."""

from __future__ import annotations

import hashlib
import json

from src.autoslice import native_context_witness as native
from src.autoslice.acoustic_witness_adjudication import build_witness_request
from src.autoslice.local_asr_target_evidence import (
    SCHEMA as NATIVE_SCHEMA,
    digest as native_digest,
)


def _request(*, offset_ms: int = 10_000, evidence_id: str = "e" * 64) -> dict[str, object]:
    return build_witness_request(
        {
            "evidence_id": evidence_id,
            "cue_indexes": [7],
            "matched_start_ms": 2_500,
            "matched_end_ms": 4_000,
            "context_start_ms": 1_500,
            "context_end_ms": 5_000,
            "source_media_timeline_offset_ms": offset_ms,
            "current_cue": "当前候选，不应跨边界",
            "proposed_cue": "另一个候选，也不应跨边界",
        }
    )


def _receipt(
    *,
    source_sha256: str,
    start_ms: int,
    end_ms: int,
    transcript: str = "原生整 cue 听写",
    status: str = "OBSERVED",
    provider: str = "moss",
) -> dict[str, object]:
    receipt = {
        "schema_version": NATIVE_SCHEMA,
        "provider": provider,
        "model": "moss-transcribe-diarize-pro",
        "status": status,
        "candidate_exposure": "none",
        "authority": "EVIDENCE_ONLY",
        "mutation_authorized": False,
        "source_media_sha256": source_sha256,
        "input_audio_sha256": "b" * 64,
        "response_sha256": "c" * 64,
        "target_start_ms": start_ms,
        "target_end_ms": end_ms,
        "crop_is_exact_target": True,
        "transcript": transcript,
        "native_segments": [],
    }
    receipt["receipt_sha256"] = native_digest(receipt)
    return receipt


def _build(tmp_path, monkeypatch, *, observer, fallback=None):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"stable source bytes")
    calls: list[dict[str, object]] = []

    def factory(**kwargs):
        calls.append(kwargs)
        return observer

    monkeypatch.setattr(native, "build_native_foreign_witness", factory)
    fallback_calls: list[object] = []

    def fallback_fn(request):
        fallback_calls.append(request)
        return fallback if fallback is not None else {"fallback": request}

    verifier = native.build_native_context_verifier(
        fallback=fallback_fn,
        source_media=source,
        output_dir=tmp_path / "out",
        provider="moss",
    )
    return source, verifier, calls, fallback_calls


def test_native_success_uses_only_offset_geometry_and_preserves_receipt(tmp_path, monkeypatch):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"stable source bytes")
    source_sha = hashlib.sha256(source.read_bytes()).hexdigest()
    request = _request(offset_ms=10_000)
    observer_calls: list[dict[str, object]] = []

    def observer(**kwargs):
        observer_calls.append(kwargs)
        return _receipt(
            source_sha256=source_sha,
            start_ms=12_500,
            end_ms=14_000,
        )

    source, verifier, _, fallback_calls = _build(tmp_path, monkeypatch, observer=observer)
    result = verifier(request)

    assert observer_calls == [{"start_ms": 12_500, "end_ms": 14_000}]
    assert not fallback_calls
    assert result["schema_version"] == "subtitle-span-acoustic-witness.v1"
    assert result["witness_protocol"] == "candidate_blind_transcript"
    assert result["request_sha256"] == request["request_sha256"]
    assert result["status"] == "OBSERVED"
    assert result["target_audible"] is True
    assert result["audibility_basis"] == "nonempty_provider_transcript"
    assert result["exact_transcript"] == "原生整 cue 听写"
    assert result["candidate_exposure"] == "none"
    assert result["authority"] == "EVIDENCE_ONLY"
    assert result["mutation_authorized"] is False
    assert result["source_media_sha256"] == source_sha
    assert result["audio_clip_sha256"] == "b" * 64
    assert result["audio_start_ms"] == 12_500
    assert result["audio_end_ms"] == 14_000
    assert result["provider"] == "moss"
    assert result["model"] == "moss-transcribe-diarize-pro"
    assert result["response_sha256"] == "c" * 64
    assert result["native_observation"] == _receipt(
        source_sha256=source_sha,
        start_ms=12_500,
        end_ms=14_000,
    )
    assert not {"heard_pinyin", "language", "speaker", "confidence", "syllable_count"} & result.keys()

    envelope_path = (
        tmp_path / "out" / "native-context" / f"{request['request_sha256']}.json"
    )
    envelope = json.loads(envelope_path.read_text(encoding="utf-8"))
    assert envelope["native_observation"] == result["native_observation"]
    assert envelope["request"] == request


def test_hash_mismatch_and_extra_candidate_fields_are_refused(tmp_path, monkeypatch):
    calls = []

    def observer(**kwargs):
        calls.append(kwargs)
        raise AssertionError("invalid requests must not invoke native observer")

    _, verifier, _, fallback_calls = _build(tmp_path, monkeypatch, observer=observer)
    request = _request()
    bad_hash = dict(request)
    bad_hash["request_sha256"] = "f" * 64
    assert verifier(bad_hash)["status"] == "UNCERTAIN"
    assert verifier(bad_hash)["reason_code"] == "NATIVE_WITNESS_REQUEST_HASH_MISMATCH"

    with_extra = dict(request)
    with_extra["current_cue"] = "candidate text must be rejected"
    with_extra["request_sha256"] = native._canonical_sha256(
        {key: value for key, value in with_extra.items() if key != "request_sha256"}
    )
    result = verifier(with_extra)
    assert result["status"] == "UNCERTAIN"
    assert result["reason_code"] == "NATIVE_WITNESS_REQUEST_KEYS_INVALID"
    assert not calls
    assert not fallback_calls


def test_no_speech_is_uncertain_and_never_delete_authority(tmp_path, monkeypatch):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"stable source bytes")
    source_sha = hashlib.sha256(source.read_bytes()).hexdigest()
    request = _request()

    def observer(**_kwargs):
        return _receipt(
            source_sha256=source_sha,
            start_ms=12_500,
            end_ms=14_000,
            transcript="",
            status="NO_SPEECH_REPORTED",
        )

    _, verifier, _, fallback_calls = _build(tmp_path, monkeypatch, observer=observer)
    result = verifier(request)
    assert result["status"] == "UNCERTAIN"
    assert result["mutation_authorized"] is False
    assert "target_audible" not in result
    assert "target_audible" not in json.dumps(result, ensure_ascii=False)
    assert result["native_observation"]["status"] == "NO_SPEECH_REPORTED"
    assert not fallback_calls


def test_native_error_does_not_fall_back_to_agy_and_reason_is_sanitized(tmp_path, monkeypatch):
    class NativeError(RuntimeError):
        reason_code = "MOSS_HTTP_ERROR"

    def observer(**_kwargs):
        raise NativeError("Authorization: super-secret-token")

    _, verifier, _, fallback_calls = _build(tmp_path, monkeypatch, observer=observer)
    result = verifier(_request())
    assert result["status"] == "UNCERTAIN"
    assert result["reason_code"] == "MOSS_HTTP_ERROR"
    assert "super-secret-token" not in json.dumps(result)
    assert "Authorization=<redacted>" in result["reason_detail"]
    assert not fallback_calls


def test_native_receipt_binding_rejects_tamper_and_authority_metadata(tmp_path, monkeypatch):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"stable source bytes")
    source_sha = hashlib.sha256(source.read_bytes()).hexdigest()
    request = _request()
    receipts = []

    tampered = _receipt(source_sha256=source_sha, start_ms=12_500, end_ms=14_000)
    tampered["receipt_sha256"] = "d" * 64
    receipts.append(tampered)
    authority_tampered = _receipt(source_sha256=source_sha, start_ms=12_500, end_ms=14_000)
    authority_tampered["authority"] = "SUBTITLE_AUTHORITY"
    authority_tampered["receipt_sha256"] = native_digest(
        {key: value for key, value in authority_tampered.items() if key != "receipt_sha256"}
    )
    receipts.append(authority_tampered)

    observer_calls = []

    def observer(**_kwargs):
        observer_calls.append(True)
        return receipts.pop(0)

    _, verifier, _, fallback_calls = _build(tmp_path, monkeypatch, observer=observer)
    first = verifier(request)
    second = verifier(_request(evidence_id="f" * 64))
    assert first["reason_code"] == "NATIVE_OBSERVATION_RECEIPT_HASH_INVALID"
    assert second["reason_code"] == "NATIVE_OBSERVATION_AUTHORITY_INVALID"
    assert len(observer_calls) == 2
    assert not fallback_calls


def test_non_witness_request_uses_fallback_unchanged(tmp_path, monkeypatch):
    sentinel = {"schema_version": "chat-entity-verdict.v1", "status": "RESOLVED"}
    _, verifier, _, fallback_calls = _build(
        tmp_path,
        monkeypatch,
        observer=lambda **_: (_ for _ in ()).throw(AssertionError("must not call native")),
        fallback=sentinel,
    )
    request = {"schema_version": "chat-entity-request.v1", "candidate_entities": []}
    assert verifier(request) is sentinel
    assert fallback_calls == [request]


def test_fallback_exact_source_seams_are_preserved(tmp_path, monkeypatch):
    class Fallback:
        def __call__(self, request):
            return {"fallback": request}

        def exact_source_transcript(self, request):
            return {"exact": request}

        def probe_exact_source_transcript_cache(self, request):
            return {"exact_cached": request}

    fallback = Fallback()
    source = tmp_path / "source.mp4"
    source.write_bytes(b"stable source bytes")
    monkeypatch.setattr(native, "build_native_foreign_witness", lambda **_: lambda **__: {})
    verifier = native.build_native_context_verifier(
        fallback=fallback,
        source_media=source,
        output_dir=tmp_path / "out",
        provider="moss",
    )
    request = {"schema_version": "exact-source-transcript-request.v1"}
    assert verifier.exact_source_transcript(request) == {"exact": request}
    assert verifier.probe_exact_source_transcript_cache(request) == {"exact_cached": request}


def test_probe_is_provider_free_and_does_not_reuse_after_source_drift(tmp_path, monkeypatch):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"stable source bytes")
    source_sha = hashlib.sha256(source.read_bytes()).hexdigest()
    request = _request()
    calls = []

    def observer(**kwargs):
        calls.append(kwargs)
        return _receipt(source_sha256=source_sha, start_ms=12_500, end_ms=14_000)

    _, verifier, _, _ = _build(tmp_path, monkeypatch, observer=observer)
    first = verifier(request)
    assert first["status"] == "OBSERVED"
    assert len(calls) == 1

    def provider_forbidden(**_kwargs):
        raise AssertionError("probe must not call provider")

    monkeypatch.setattr(native, "build_native_foreign_witness", lambda **_: provider_forbidden)
    cached = verifier.probe_witness_cache(request)
    assert cached["status"] == "OBSERVED"
    assert cached["served_from_cache"] is True
    assert len(calls) == 1

    source.write_bytes(b"source drift")
    assert verifier.probe_witness_cache(request) is None
    assert verifier(request)["status"] == "UNCERTAIN"
    assert len(calls) == 1


def test_probe_cold_instance_does_not_read_disk_cache(tmp_path, monkeypatch):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"stable source bytes")
    source_sha = hashlib.sha256(source.read_bytes()).hexdigest()
    request = _request()

    def observer(**_kwargs):
        return _receipt(source_sha256=source_sha, start_ms=12_500, end_ms=14_000)

    _, verifier, _, _ = _build(tmp_path, monkeypatch, observer=observer)
    verifier(request)
    calls = []

    def second_factory(**_kwargs):
        calls.append(True)
        return lambda **_: (_ for _ in ()).throw(AssertionError("cold probe must not observe"))

    monkeypatch.setattr(native, "build_native_foreign_witness", second_factory)
    cold_verifier = native.build_native_context_verifier(
        fallback=lambda request: {"fallback": request},
        source_media=source,
        output_dir=tmp_path / "out",
        provider="moss",
    )
    assert cold_verifier.probe_witness_cache(request) is None
    assert calls == [True]
