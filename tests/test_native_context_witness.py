"""Bounded tests for the optional native candidate-blind context lane."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from src.autoslice import native_context_witness as native
from src.autoslice import native_foreign_witness as foreign
from src.autoslice import native_audio_budget_receipt as budget_receipts
from src.autoslice.acoustic_witness_adjudication import build_witness_request
from src.autoslice.local_asr_target_evidence import (
    SCHEMA as NATIVE_SCHEMA,
    digest as native_digest,
    exact_target_evidence,
    MODELS as NATIVE_MODELS,
)
from src.autoslice.mai_transcription import MAI_MODEL
from src.autoslice.moss_transcription import MOSS_MODEL
from src.autoslice.supplement_audio_budget import get_budget, start_budget


def _request(
    *,
    offset_ms: int = 10_000,
    evidence_id: str = "e" * 64,
    matched_start_ms: int = 2_500,
    matched_end_ms: int = 4_000,
) -> dict[str, object]:
    return build_witness_request(
        {
            "evidence_id": evidence_id,
            "cue_indexes": [7],
            "matched_start_ms": matched_start_ms,
            "matched_end_ms": matched_end_ms,
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


def _independent_witness(
    *,
    request: dict[str, object],
    source_sha256: str,
    provider: str,
    transcript: str = "独立整 cue 听写",
    status: str = "OBSERVED",
) -> dict[str, object]:
    prepared = native._prepare_request(request)
    receipt = _receipt(
        source_sha256=source_sha256,
        start_ms=prepared.physical_start_ms,
        end_ms=prepared.physical_end_ms,
        transcript=transcript,
        status=status,
        provider=provider,
    )
    receipt["model"] = native.NATIVE_MODELS[provider]
    receipt["receipt_sha256"] = native_digest(
        {key: value for key, value in receipt.items() if key != "receipt_sha256"}
    )
    return native._native_transcript_witness(
        request=request,
        prepared=prepared,
        native_observation=receipt,
        source_sha256=source_sha256,
        provider=provider,
    )


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


def _range_requests(source_sha256: str) -> tuple[dict[str, object], dict[str, object]]:
    srt = (
        "1\n00:00:05,000 --> 00:00:09,000\n第一句\n\n"
        "2\n00:00:10,000 --> 00:00:14,000\n当前句\n\n"
        "3\n00:00:15,000 --> 00:00:19,000\n第三句\n"
    )
    check = {
        "evidence_id": "narrow-range",
        "cue_indexes": [2],
        "matched_start_ms": 10_000,
        "matched_end_ms": 14_000,
        "context_start_ms": 5_000,
        "context_end_ms": 19_000,
        "source_media_timeline_offset_ms": 100,
        "current_cue": "当前句",
        "proposed_cue": "目前句",
        "whole_clip_current_srt": srt,
        "clip_context_binding": {
            "candidate_id": "candidate-range-test",
            "source_media_sha256s": [source_sha256],
        },
    }
    broad = build_witness_request(
        {
            "evidence_id": "broad-range",
            "cue_indexes": [1, 2, 3],
            "matched_start_ms": 5_000,
            "matched_end_ms": 19_000,
            "context_start_ms": 5_000,
            "context_end_ms": 19_000,
            "source_media_timeline_offset_ms": 100,
        }
    )
    return check, broad


def _install_range_writer(
    monkeypatch, source: Path, cache_root: Path, *, moss_text_unlocated: bool = False,
    dispatches: list[tuple[str, int, int]] | None = None,
):
    source_sha256 = hashlib.sha256(source.read_bytes()).hexdigest()

    def factory(*, source_media, output_dir, provider, **_kwargs):
        model = NATIVE_MODELS[provider]

        def observe(*, start_ms, end_ms):
            if dispatches is not None:
                dispatches.append((provider, start_ms, end_ms))
            job = (
                Path(output_dir)
                / "native-exact"
                / provider
                / f"{source_sha256[:20]}-{start_ms}-{end_ms}"
            )
            job.mkdir(parents=True, exist_ok=True)
            audio_path = job / "input.mp3"
            audio_path.write_bytes(f"audio-{provider}".encode())
            audio = audio_path.read_bytes()
            raw = {"text": "整句范围听写"}
            metadata = {
                "provider": provider,
                "model": model,
                "input_audio_sha256": hashlib.sha256(audio).hexdigest(),
                "response_sha256": "c" * 64,
                "raw_response": raw,
                "status": "OK",
                "native_segments": [
                    {"start_ms": 100, "end_ms": 1_200, "text": "整句范围听写"}
                ],
                "native_timeline": {"valid": True},
                "candidate_exposure": "none",
                "contract": "candidate-free-native-secondary-v1",
            }
            if moss_text_unlocated and provider == "moss":
                metadata.update(
                    status="TEXT_UNLOCATED",
                    evidence_scope="exact_cue",
                    native_segments=[],
                    one_track_srt_eligible=False,
                    native_timeline={
                        "one_track_srt_eligible": False,
                        "valid": False,
                    },
                )
            receipt = {
                "binding": {
                    "contract": "candidate-free-native-secondary-v1",
                    "provider": provider,
                    "model": model,
                    "input_audio_sha256": metadata["input_audio_sha256"],
                    "duration_ms": end_ms - start_ms,
                    "candidate_exposure": "none",
                },
                "evidence": metadata,
                "evidence_sha256": native_digest(metadata),
            }
            (job / f"input.audio-evidence.{provider}.json").write_text(
                json.dumps(receipt, ensure_ascii=False), encoding="utf-8"
            )
            return exact_target_evidence(
                metadata,
                audio=audio,
                source_sha256=source_sha256,
                start_ms=start_ms,
                end_ms=end_ms,
                prefer_provider_text=provider == "moss",
            )

        return observe

    monkeypatch.setattr(native, "build_native_foreign_witness", factory)
    return source_sha256


def _install_real_range_budget_writer(
    monkeypatch, dispatches: list[tuple[str, int, int]]
) -> None:
    """Exercise the real native foreign writer with a local provider seam."""

    def extract(_source, target, _start_ms, _end_ms):
        target.write_bytes(b"synthetic exact range audio")

    def observe(audio, **kwargs):
        provider = kwargs["provider"]
        model = NATIVE_MODELS[provider]
        budget = get_budget(kwargs["supplement_source"])
        assert budget is not None
        attempt = budget.consume(
            provider=provider,
            model=model,
            start_ms=kwargs["crop_start_ms"],
            end_ms=kwargs["crop_end_ms"],
            max_attempts_per_window=3,
        )
        persist = kwargs["persist_before_dispatch"]
        assert callable(persist)
        persist()
        dispatches.append((provider, kwargs["crop_start_ms"], kwargs["crop_end_ms"]))
        metadata = {
            "provider": provider,
            "model": model,
            "input_audio_sha256": hashlib.sha256(audio).hexdigest(),
            "response_sha256": "c" * 64,
            "raw_response": {"text": "整句范围听写"},
            "status": "OK",
            "native_segments": [
                {"start_ms": 100, "end_ms": 1_200, "text": "整句范围听写"}
            ],
            "native_timeline": {"valid": True},
            "candidate_exposure": "none",
            "contract": "candidate-free-native-secondary-v1",
        }
        media_path = Path(kwargs["media_path"])
        binding = {
            "contract": "candidate-free-native-secondary-v1",
            "provider": provider,
            "model": model,
            "input_audio_sha256": metadata["input_audio_sha256"],
            "duration_ms": kwargs["duration_ms"],
            "candidate_exposure": "none",
        }
        media_path.with_suffix(f".audio-evidence.{provider}.json").write_text(
            json.dumps(
                {
                    "binding": binding,
                    "evidence": metadata,
                    "evidence_sha256": native_digest(metadata),
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        budget.finish_attempt(attempt, status="OBSERVED")
        persist()
        return metadata

    monkeypatch.setattr(foreign, "_extract_exact_mp3", extract)
    monkeypatch.setattr(foreign, "observe_secondary", observe)


def _write_pre_http_config_envelope(
    *,
    source: Path,
    output: Path,
    request: dict[str, object],
    source_sha256: str,
    provider: str = "mai",
    reason_code: str = "MAI_ENDPOINT_INVALID",
) -> tuple[Path, bytes]:
    prepared = native._prepare_request(request)
    witness = native._independent_uncertain(
        request,
        provider,
        reason_code,
        source_sha256=source_sha256,
        geometry=(prepared.physical_start_ms, prepared.physical_end_ms),
    )
    native._write_envelope(
        output_dir=output / provider,
        prepared=prepared,
        witness=witness,
        native_observation=None,
        source_sha256=source_sha256,
    )
    path = output / provider / "native-context" / f"{prepared.request_sha256}.json"
    return path, path.read_bytes()


def _write_canonical_budget_receipt(
    *, source: Path, receipt_path: Path, source_sha256: str
) -> None:
    start_budget(source)
    budget_receipts.persist_native_audio_budget_receipt(
        source_media=source,
        source_media_sha256=source_sha256,
        receipt_path=receipt_path,
    )


def test_ordinary_cue_recovers_only_unspent_config_slot_and_reuses_success(tmp_path, monkeypatch):
    source=tmp_path/'source.mp4';source.write_bytes(b'ordinary-cue-source')
    sha=hashlib.sha256(source.read_bytes()).hexdigest();request=_request(offset_ms=0)
    output=tmp_path/'out';receipt=output/'native-audio-budget.json';dispatches=[]
    _install_real_range_budget_writer(monkeypatch,dispatches)
    def build(providers):
        return native.build_independent_context_witnesses(fallback=lambda r:{},source_media=source,
            output_dir=output,providers=providers,budget_receipt_path=receipt,
            native_observation_cache_root=tmp_path/'cache')
    build(('moss',)).observe_independent_witnesses(request)
    assert dispatches==[('moss',2500,4000)]
    path,old=_write_pre_http_config_envelope(source=source,output=output,request=request,
        source_sha256=sha,reason_code='MAI_NATIVE_AUDIO_RUNTIME_CONFIG_MISSING')
    monkeypatch.setattr(native,'_validate_recovery_provider_configuration',lambda _:None)
    rows=build(('mai','moss')).observe_independent_witnesses(request)
    assert [r['status'] for r in rows]==['OBSERVED','OBSERVED']
    assert dispatches==[('moss',2500,4000),('mai',2500,4000)]
    assert Path(rows[0]['pre_dispatch_history_path']).read_bytes()==old
    before=receipt.read_bytes();assert path.read_bytes()!=old
    again=build(('mai','moss')).observe_independent_witnesses(request)
    assert all(r['served_from_cache'] for r in again)
    assert len(dispatches)==2 and receipt.read_bytes()==before


@pytest.mark.parametrize('reason',['MAI_HTTP_ERROR','MAI_NATIVE_AUDIO_RUNTIME_CONFIG_MISSING'])
def test_ordinary_cue_does_not_redispatch_unknown_or_unready_failure(tmp_path, monkeypatch, reason):
    source=tmp_path/'source.mp4';source.write_bytes(b'cue-source')
    sha=hashlib.sha256(source.read_bytes()).hexdigest();request=_request(offset_ms=0)
    output=tmp_path/'out';receipt=output/'native-audio-budget.json'
    _write_canonical_budget_receipt(source=source,receipt_path=receipt,source_sha256=sha)
    path,old=_write_pre_http_config_envelope(source=source,output=output,request=request,
        source_sha256=sha,reason_code=reason)
    if reason=='MAI_HTTP_ERROR':
        document=json.loads(old);document['witness']['reason_detail']='HTTP 503; transcript unavailable'
        path.write_text(json.dumps(document));old=path.read_bytes()
    before=receipt.read_bytes()
    monkeypatch.setattr(native,'_validate_recovery_provider_configuration',lambda _:'NATIVE_RANGE_RECOVERY_CONFIG_NOT_READY')
    monkeypatch.setattr(foreign,'observe_secondary',lambda **kw:pytest.fail('no dispatch'))
    wrapper=native.build_independent_context_witnesses(fallback=lambda r:{},source_media=source,
        output_dir=output,providers=('mai',),budget_receipt_path=receipt)
    result=wrapper.observe_independent_witnesses(request)[0]
    assert result['status']=='UNCERTAIN'
    if reason=='MAI_HTTP_ERROR':
        assert result['reason_code']=='MAI_HTTP_ERROR'
        assert result['reason_detail']=='HTTP 503; transcript unavailable'
    assert path.read_bytes()==old and receipt.read_bytes()==before
    if reason=='MAI_HTTP_ERROR':
        document=json.loads(old);document['source_media_sha256']='0'*64
        path.write_text(json.dumps(document));corrupt=path.read_bytes()
        invalid=wrapper.observe_independent_witnesses(request)[0]
        assert invalid['reason_code']=='NATIVE_RANGE_ENVELOPE_INVALID'
        assert path.read_bytes()==corrupt and receipt.read_bytes()==before


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
    from src.autoslice.review_errors import ProviderFailure

    def observer(**_kwargs):
        raise ProviderFailure("MOSS_HTTP_ERROR", "Authorization: super-secret-token", provider="moss")

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


def test_independent_wrapper_is_opt_in_and_preserves_fallback_seams(tmp_path, monkeypatch):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"stable source bytes")
    sentinel = {"schema_version": "chat-entity-verdict.v1", "status": "RESOLVED"}
    fallback_calls: list[object] = []
    build_calls: list[str] = []

    class Fallback:
        def __call__(self, request):
            fallback_calls.append(request)
            return sentinel

        def probe_witness_cache(self, request):
            return {"cached": request}

        def exact_source_transcript(self, request):
            return {"exact": request}

        def probe_exact_source_transcript_cache(self, request):
            return {"exact_cached": request}

    def forbidden_factory(**kwargs):
        build_calls.append(kwargs["provider"])
        raise AssertionError("provider verifiers are opt-in")

    monkeypatch.setattr(native, "build_native_context_verifier", forbidden_factory)
    fallback = Fallback()
    wrapper = native.build_independent_context_witnesses(
        fallback=fallback,
        source_media=source,
        output_dir=tmp_path / "out",
        providers=("mai", "moss"),
    )

    request = {"schema_version": "chat-entity-request.v1"}
    assert wrapper(request) is sentinel
    assert fallback_calls == [request]
    assert build_calls == []
    assert wrapper.probe_witness_cache(request) == {"cached": request}
    assert wrapper.exact_source_transcript(request) == {"exact": request}
    assert wrapper.probe_exact_source_transcript_cache(request) == {"exact_cached": request}


@pytest.mark.parametrize("range_end_ms", [19_000, 36_270])
def test_range_context_reuses_native_writer_and_keeps_original_files_immutable(
    tmp_path, monkeypatch, range_end_ms,
):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"range-source")
    source_sha256 = _install_range_writer(monkeypatch, source, tmp_path / "cache")
    check_request, range_request = _range_requests(source_sha256)
    if range_end_ms != 19_000:
        check_request["whole_clip_current_srt"] = check_request["whole_clip_current_srt"].replace(
            "00:00:19,000", "00:00:36,270"
        )
        check_request["context_end_ms"] = range_end_ms
        range_request["matched_end_ms"] = range_end_ms
        range_request["context_end_ms"] = range_end_ms
        range_request["request_sha256"] = native._canonical_sha256(
            {key: value for key, value in range_request.items() if key != "request_sha256"}
        )
    output = tmp_path / "out"
    wrapper = native.build_independent_context_witnesses(
        fallback=lambda request: {"fallback": request},
        source_media=source,
        output_dir=output,
        providers=("mai",),
        native_observation_cache_root=tmp_path / "cache",
    )

    first = wrapper.observe_range_context(
        check_request=check_request, range_witness_request=range_request
    )
    assert len(first) == 1
    assert first[0]["schema_version"] == "range-acoustic-context-observation.v1"
    assert first[0]["evidence_scope"] == "RANGE_CONTEXT_ONLY"
    assert first[0]["authority"] == "EVIDENCE_ONLY"
    assert first[0]["mutation_authorized"] is False
    assert first[0]["candidate_blind"] is True
    assert first[0]["current_cue_index"] == 2
    assert first[0]["verdict"]["exact_transcript"] == "整句范围听写"
    assert "当前句" not in json.dumps(first[0]["witness_request"], ensure_ascii=False)

    bindings = first[0]["artifact_bindings"]
    original_bytes = {
        role: Path(bindings[f"{role}_path"]).read_bytes()
        for role in ("native_envelope", "provider_receipt")
    }
    second = wrapper.observe_range_context(
        check_request=check_request, range_witness_request=range_request
    )
    assert second[0]["schema_version"] == "range-acoustic-context-observation.v1"
    assert second[0]["served_from_cache"] is True
    assert {
        role: Path(second[0]["artifact_bindings"][f"{role}_path"]).read_bytes()
        for role in ("native_envelope", "provider_receipt")
    } == original_bytes


def test_range_context_rejects_current_drift_and_malformed_candidate_blind_range(
    tmp_path, monkeypatch,
):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"range-source")
    source_sha256 = _install_range_writer(monkeypatch, source, tmp_path / "cache")
    check_request, range_request = _range_requests(source_sha256)
    wrapper = native.build_independent_context_witnesses(
        fallback=lambda request: {"fallback": request},
        source_media=source,
        output_dir=tmp_path / "out",
        providers=("moss",),
        native_observation_cache_root=tmp_path / "cache",
    )

    malformed = dict(range_request)
    malformed["current_cue"] = "候选文本不得进 native request"
    malformed["request_sha256"] = native._canonical_sha256(
        {key: value for key, value in malformed.items() if key != "request_sha256"}
    )
    rejected = wrapper.observe_range_context(
        check_request=check_request, range_witness_request=malformed
    )
    assert rejected[0]["status"] == "UNCERTAIN"
    assert rejected[0]["reason_code"] == "NATIVE_WITNESS_REQUEST_KEYS_INVALID"

    accepted = wrapper.observe_range_context(
        check_request=check_request, range_witness_request=range_request
    )
    assert accepted[0]["status"] == "OBSERVED"
    drifted = dict(check_request)
    drifted["clip_context_binding"] = {
        **check_request["clip_context_binding"],
        "candidate_id": "different-candidate",
    }
    result = wrapper.observe_range_context(
        check_request=drifted, range_witness_request=range_request
    )
    assert result[0]["status"] == "UNCERTAIN"
    assert result[0]["reason_code"] == "NATIVE_RANGE_GEOMETRY_OR_CANDIDATE_DRIFT"


def test_range_context_keeps_moss_unlocated_text_as_range_only_evidence(
    tmp_path, monkeypatch,
):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"range-source")
    source_sha256 = _install_range_writer(
        monkeypatch, source, tmp_path / "cache", moss_text_unlocated=True
    )
    check_request, range_request = _range_requests(source_sha256)
    wrapper = native.build_independent_context_witnesses(
        fallback=lambda request: {"fallback": request},
        source_media=source,
        output_dir=tmp_path / "out",
        providers=("moss",),
        native_observation_cache_root=tmp_path / "cache",
    )
    rows = wrapper.observe_range_context(
        check_request=check_request, range_witness_request=range_request
    )
    assert rows[0]["status"] == "OBSERVED"
    assert rows[0]["evidence_scope"] == "RANGE_CONTEXT_ONLY"
    assert rows[0]["transcript_scope"] == "RANGE_WITHOUT_SINGLE_CUE_TIMESTAMPS"
    assert rows[0]["verdict"]["exact_transcript"] == "整句范围听写"


def test_range_context_separates_current_srt_revisions_without_new_provider_dispatch(
    tmp_path, monkeypatch,
):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"range-source")
    dispatches: list[tuple[str, int, int]] = []
    source_sha256 = _install_range_writer(
        monkeypatch, source, tmp_path / "cache", dispatches=dispatches
    )
    check_request, range_request = _range_requests(source_sha256)
    output = tmp_path / "out"
    wrapper = native.build_independent_context_witnesses(
        fallback=lambda request: {"fallback": request},
        source_media=source,
        output_dir=output,
        providers=("mai",),
        native_observation_cache_root=tmp_path / "cache",
    )

    first = wrapper.observe_range_context(
        check_request=check_request, range_witness_request=range_request
    )[0]
    first_reviewed = Path(first["artifact_bindings"]["reviewed_srt_path"])
    first_input = Path(first["artifact_bindings"]["input_path"])
    first_reviewed_bytes = first_reviewed.read_bytes()

    revised_check = dict(check_request)
    revised_check["whole_clip_current_srt"] = str(
        check_request["whole_clip_current_srt"]
    ).replace("第三句", "第三句（当前修订）")
    second = wrapper.observe_range_context(
        check_request=revised_check, range_witness_request=range_request
    )[0]

    second_reviewed = Path(second["artifact_bindings"]["reviewed_srt_path"])
    second_input = Path(second["artifact_bindings"]["input_path"])
    assert second["status"] == "OBSERVED"
    assert second["served_from_cache"] is True
    assert dispatches == [("mai", 5_100, 19_100)]
    assert second_reviewed != first_reviewed
    assert second_input != first_input
    assert first_reviewed.read_bytes() == first_reviewed_bytes


def test_range_context_preflights_actual_cue_and_source_before_provider(
    tmp_path, monkeypatch,
):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"range-source")
    dispatches: list[tuple[str, int, int]] = []
    source_sha256 = _install_range_writer(
        monkeypatch, source, tmp_path / "cache", dispatches=dispatches
    )
    check_request, range_request = _range_requests(source_sha256)
    wrapper = native.build_independent_context_witnesses(
        fallback=lambda request: {"fallback": request},
        source_media=source,
        output_dir=tmp_path / "out",
        providers=("moss",),
        native_observation_cache_root=tmp_path / "cache",
    )

    bad_cue = dict(check_request)
    bad_cue["current_cue"] = "当前句被篡改"
    cue_result = wrapper.observe_range_context(
        check_request=bad_cue, range_witness_request=range_request
    )[0]
    assert cue_result["status"] == "UNCERTAIN"
    assert cue_result["reason_code"] == "NATIVE_RANGE_CURRENT_CUE_DRIFT"
    assert dispatches == []

    bad_source = dict(check_request)
    bad_source["clip_context_binding"] = {
        **check_request["clip_context_binding"],
        "source_media_sha256s": ["0" * 64],
    }
    source_result = wrapper.observe_range_context(
        check_request=bad_source, range_witness_request=range_request
    )[0]
    assert source_result["status"] == "UNCERTAIN"
    assert source_result["reason_code"] == "NATIVE_RANGE_CURRENT_SOURCE_DRIFT"
    assert dispatches == []


def test_range_context_dispatches_only_missing_provider_and_rejects_invalid_envelope(
    tmp_path, monkeypatch,
):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"range-source")
    dispatches: list[tuple[str, int, int]] = []
    source_sha256 = _install_range_writer(
        monkeypatch, source, tmp_path / "cache", dispatches=dispatches
    )
    check_request, range_request = _range_requests(source_sha256)
    output = tmp_path / "out"
    first_wrapper = native.build_independent_context_witnesses(
        fallback=lambda request: {"fallback": request},
        source_media=source,
        output_dir=output,
        providers=("mai",),
        native_observation_cache_root=tmp_path / "cache",
    )
    first_wrapper.observe_range_context(
        check_request=check_request, range_witness_request=range_request
    )

    second_wrapper = native.build_independent_context_witnesses(
        fallback=lambda request: {"fallback": request},
        source_media=source,
        output_dir=output,
        providers=("mai", "moss"),
        native_observation_cache_root=tmp_path / "cache",
    )
    rows = second_wrapper.observe_range_context(
        check_request=check_request, range_witness_request=range_request
    )
    assert [row["provider"] for row in rows] == ["mai", "moss"]
    assert [row["status"] for row in rows] == ["OBSERVED", "OBSERVED"]
    assert dispatches == [("mai", 5_100, 19_100), ("moss", 5_100, 19_100)]

    moss_envelope = output / "moss" / "native-context" / f"{range_request['request_sha256']}.json"
    moss_envelope.write_text("{}\n", encoding="utf-8")
    cold_wrapper = native.build_independent_context_witnesses(
        fallback=lambda request: {"fallback": request},
        source_media=source,
        output_dir=output,
        providers=("moss",),
        native_observation_cache_root=tmp_path / "cache",
    )
    rejected = cold_wrapper.observe_range_context(
        check_request=check_request, range_witness_request=range_request
    )[0]
    assert rejected["status"] == "UNCERTAIN"
    assert rejected["reason_code"] == "NATIVE_RANGE_ENVELOPE_INVALID"
    assert dispatches == [("mai", 5_100, 19_100), ("moss", 5_100, 19_100)]


@pytest.mark.parametrize("cold_process", [False, True])
def test_range_context_recovers_pre_http_config_failure_once_and_archives_old_bytes(
    tmp_path, monkeypatch, cold_process,
):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"range-source")
    source_sha256 = hashlib.sha256(source.read_bytes()).hexdigest()
    check_request, range_request = _range_requests(source_sha256)
    output = tmp_path / "out"
    receipt_path = output / "native-audio-budget.json"
    _write_canonical_budget_receipt(
        source=source, receipt_path=receipt_path, source_sha256=source_sha256
    )
    envelope_path, old_bytes = _write_pre_http_config_envelope(
        source=source,
        output=output,
        request=range_request,
        source_sha256=source_sha256,
    )
    if cold_process:
        from src.autoslice import supplement_audio_budget
        monkeypatch.delitem(supplement_audio_budget._REGISTRY, str(source.resolve()))
        assert get_budget(source) is None
    dispatches: list[tuple[str, int, int]] = []
    _install_real_range_budget_writer(monkeypatch, dispatches)
    monkeypatch.setattr(native, "_validate_recovery_provider_configuration", lambda _: None)

    wrapper = native.build_independent_context_witnesses(
        fallback=lambda request: {"fallback": request},
        source_media=source,
        output_dir=output,
        providers=("mai",),
        budget_receipt_path=receipt_path,
        native_observation_cache_root=tmp_path / "cache",
    )
    first = wrapper.observe_range_context(
        check_request=check_request, range_witness_request=range_request
    )[0]
    assert first["status"] == "OBSERVED"
    assert first["evidence_scope"] == "RANGE_CONTEXT_ONLY"
    assert dispatches == [("mai", 5_100, 19_100)]
    history_path = Path(first["pre_dispatch_history_path"])
    assert history_path.is_file()
    assert history_path.read_bytes() == old_bytes
    assert hashlib.sha256(history_path.read_bytes()).hexdigest() == first[
        "pre_dispatch_history_sha256"
    ]
    assert envelope_path.read_bytes() != old_bytes
    packet = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert [
        (row["provider"], row["model"], row["start_ms"], row["end_ms"])
        for row in packet["budget"]["attempts"]
    ] == [("mai", NATIVE_MODELS["mai"], 5_100, 19_100)]

    # A cold wrapper sees the newly materialized native observation as the
    # ordinary exact-range cache and does not repeat the provider dispatch.
    cold_wrapper = native.build_independent_context_witnesses(
        fallback=lambda request: {"fallback": request},
        source_media=source,
        output_dir=output,
        providers=("mai",),
        budget_receipt_path=receipt_path,
        native_observation_cache_root=tmp_path / "cache",
    )
    second = cold_wrapper.observe_range_context(
        check_request=check_request, range_witness_request=range_request
    )[0]
    assert second["status"] == "OBSERVED"
    assert second["served_from_cache"] is True
    assert dispatches == [("mai", 5_100, 19_100)]
    assert history_path.read_bytes() == old_bytes
    assert json.loads(receipt_path.read_text(encoding="utf-8"))["budget"]["attempts"] == packet[
        "budget"
    ]["attempts"]


def test_range_context_recovers_config_failure_memoized_in_same_wrapper(
    tmp_path, monkeypatch,
):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"same-wrapper-range-source")
    source_sha256 = hashlib.sha256(source.read_bytes()).hexdigest()
    check_request, range_request = _range_requests(source_sha256)
    output = tmp_path / "out"
    receipt_path = output / "native-audio-budget.json"
    dispatches: list[tuple[str, int, int]] = []
    _install_real_range_budget_writer(monkeypatch, dispatches)
    ready_observer = foreign.observe_secondary

    def missing_config(*_args, **_kwargs):
        from src.autoslice.mai_transcription import MaiTranscriptionError
        raise MaiTranscriptionError("MAI_ENDPOINT_INVALID", "config unavailable")

    monkeypatch.setattr(foreign, "observe_secondary", missing_config)
    wrapper = native.build_independent_context_witnesses(
        fallback=lambda request: {"fallback": request},
        source_media=source, output_dir=output, providers=("mai",),
        budget_receipt_path=receipt_path,
        native_observation_cache_root=tmp_path / "cache",
    )
    first = wrapper.observe_independent_witnesses(range_request)[0]
    assert first["reason_code"] == "MAI_ENDPOINT_INVALID"
    assert get_budget(source).snapshot()["attempts"] == []
    prepared = native._prepare_request(range_request)
    envelope = output / "mai" / "native-context" / f"{prepared.request_sha256}.json"
    old_bytes = envelope.read_bytes()
    monkeypatch.setattr(foreign, "observe_secondary", ready_observer)
    monkeypatch.setattr(native, "_validate_recovery_provider_configuration", lambda _: None)
    recovered = wrapper.observe_range_context(
        check_request=check_request, range_witness_request=range_request,
    )[0]
    assert recovered["status"] == "OBSERVED"
    assert dispatches == [("mai", 5_100, 19_100)]
    assert Path(recovered["pre_dispatch_history_path"]).read_bytes() == old_bytes


def test_range_context_does_not_recover_unknown_config_or_unready_provider(
    tmp_path, monkeypatch,
):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"range-source")
    source_sha256 = hashlib.sha256(source.read_bytes()).hexdigest()
    check_request, range_request = _range_requests(source_sha256)
    output = tmp_path / "out"
    receipt_path = output / "native-audio-budget.json"
    _write_canonical_budget_receipt(
        source=source, receipt_path=receipt_path, source_sha256=source_sha256
    )
    envelope_path, old_bytes = _write_pre_http_config_envelope(
        source=source,
        output=output,
        request=range_request,
        source_sha256=source_sha256,
        reason_code="MAI_HTTP_500",
    )
    dispatches: list[tuple[str, int, int]] = []
    _install_range_writer(
        monkeypatch, source, tmp_path / "cache", dispatches=dispatches
    )
    monkeypatch.setattr(native, "_validate_recovery_provider_configuration", lambda _: None)
    wrapper = native.build_independent_context_witnesses(
        fallback=lambda request: {"fallback": request},
        source_media=source,
        output_dir=output,
        providers=("mai",),
        budget_receipt_path=receipt_path,
        native_observation_cache_root=tmp_path / "cache",
    )
    unknown = wrapper.observe_range_context(
        check_request=check_request, range_witness_request=range_request
    )[0]
    assert unknown["status"] == "UNCERTAIN"
    assert unknown["reason_code"] == "NATIVE_RANGE_ENVELOPE_INVALID"
    assert dispatches == []
    assert envelope_path.read_bytes() == old_bytes
    assert not (envelope_path.parent / "pre-dispatch-history").exists()

    # The same exact old envelope is still immutable when current config is
    # unavailable; fixing this requires a later cold call after the config
    # validator is ready, rather than silently attempting a provider call now.
    valid_path, valid_bytes = _write_pre_http_config_envelope(
        source=source,
        output=output,
        request=range_request,
        source_sha256=source_sha256,
        reason_code="MAI_ENDPOINT_INVALID",
    )
    monkeypatch.setattr(
        native,
        "_validate_recovery_provider_configuration",
        lambda _: "NATIVE_RANGE_RECOVERY_CONFIG_NOT_READY",
    )
    blocked = wrapper.observe_range_context(
        check_request=check_request, range_witness_request=range_request
    )[0]
    assert blocked["status"] == "UNCERTAIN"
    assert blocked["reason_code"] == "NATIVE_RANGE_RECOVERY_CONFIG_NOT_READY"
    assert valid_path.read_bytes() == valid_bytes
    assert dispatches == []


@pytest.mark.parametrize(
    ("mode", "expected_reason"),
    [
        ("missing", "NATIVE_RANGE_RECOVERY_RECEIPT_INVALID"),
        ("malformed", "NATIVE_RANGE_RECOVERY_RECEIPT_INVALID"),
        ("attempt", "NATIVE_RANGE_RECOVERY_ATTEMPT_EXISTS"),
        ("pending", "NATIVE_RANGE_RECOVERY_RECEIPT_INVALID"),
        ("envelope_symlink", "NATIVE_RANGE_ENVELOPE_INVALID"),
        ("envelope_drift", "NATIVE_RANGE_ENVELOPE_INVALID"),
    ],
)
def test_range_context_recovery_requires_current_canonical_budget_continuity(
    tmp_path, monkeypatch, mode, expected_reason,
):
    source = tmp_path / "source.mp4"
    source.write_bytes(f"range-source-{mode}".encode())
    source_sha256 = hashlib.sha256(source.read_bytes()).hexdigest()
    check_request, range_request = _range_requests(source_sha256)
    output = tmp_path / "out"
    receipt_path = output / "native-audio-budget.json"
    if mode != "missing":
        budget = start_budget(source)
        if mode in {"attempt", "pending"}:
            attempt = budget.consume(
                provider="mai",
                model=NATIVE_MODELS["mai"],
                start_ms=5_100,
                end_ms=19_100,
                max_attempts_per_window=3,
            )
            if mode == "attempt":
                budget.finish_attempt(attempt, status="FAILED", reason_code="OLD_FAILURE")
        budget_receipts.persist_native_audio_budget_receipt(
            source_media=source,
            source_media_sha256=source_sha256,
            receipt_path=receipt_path,
        )
        if mode == "malformed":
            receipt_path.write_text("{bad\n", encoding="utf-8")
    envelope_path, _ = _write_pre_http_config_envelope(
        source=source,
        output=output,
        request=range_request,
        source_sha256=source_sha256,
    )
    if mode == "envelope_symlink":
        target = output / "preserved-envelope.json"
        target.write_bytes(envelope_path.read_bytes())
        envelope_path.unlink()
        envelope_path.symlink_to(target)
    elif mode == "envelope_drift":
        drifted = json.loads(envelope_path.read_text(encoding="utf-8"))
        drifted["source_media_sha256"] = "0" * 64
        envelope_path.write_text(json.dumps(drifted), encoding="utf-8")
    dispatches: list[tuple[str, int, int]] = []
    _install_range_writer(
        monkeypatch, source, tmp_path / "cache", dispatches=dispatches
    )
    monkeypatch.setattr(native, "_validate_recovery_provider_configuration", lambda _: None)
    wrapper = native.build_independent_context_witnesses(
        fallback=lambda request: {"fallback": request},
        source_media=source,
        output_dir=output,
        providers=("mai",),
        budget_receipt_path=receipt_path,
        native_observation_cache_root=tmp_path / "cache",
    )
    row = wrapper.observe_range_context(
        check_request=check_request, range_witness_request=range_request
    )[0]
    assert row["status"] == "UNCERTAIN"
    assert row["reason_code"] == expected_reason
    assert dispatches == []

def test_independent_witnesses_share_physical_window_but_rebind_request(tmp_path, monkeypatch):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"stable source bytes")
    source_sha = hashlib.sha256(source.read_bytes()).hexdigest()
    build_calls: list[str] = []
    verify_calls: dict[str, int] = {"mai": 0, "moss": 0}

    def factory(**kwargs):
        provider = kwargs["provider"]
        build_calls.append(provider)

        def verify(request):
            verify_calls[provider] += 1
            return _independent_witness(
                request=request,
                source_sha256=source_sha,
                provider=provider,
                transcript=f"{provider} 独立整 cue 听写",
            )

        return verify

    monkeypatch.setattr(native, "build_native_context_verifier", factory)
    wrapper = native.build_independent_context_witnesses(
        fallback=lambda request: {"fallback": request},
        source_media=source,
        output_dir=tmp_path / "out",
        providers=("mai", "moss"),
    )
    first_request = _request(evidence_id="e" * 64)
    second_request = _request(evidence_id="f" * 64)
    second_request["cue_indexes"] = [8]
    second_request["syllable_count_hint"] = 3
    second_request["request_sha256"] = native._canonical_sha256(
        {key: value for key, value in second_request.items() if key != "request_sha256"}
    )

    first = wrapper.observe_independent_witnesses(first_request)
    second = wrapper.observe_independent_witnesses(second_request)

    assert build_calls == ["mai", "moss"]
    assert verify_calls == {"mai": 1, "moss": 1}
    assert [item["provider"] for item in first] == ["mai", "moss"]
    assert [item["provider"] for item in second] == ["mai", "moss"]
    assert [item["request_sha256"] for item in first] == [first_request["request_sha256"]] * 2
    assert [item["request_sha256"] for item in second] == [second_request["request_sha256"]] * 2
    assert all(item["status"] == "OBSERVED" for item in first + second)
    assert all(item["served_from_cache"] is True for item in second)
    assert all(item["audio_start_ms"] == 12_500 for item in first + second)
    assert all(item["audio_end_ms"] == 14_000 for item in first + second)
    assert all(item["candidate_exposure"] == "none" for item in first + second)
    assert all(item["authority"] == "EVIDENCE_ONLY" for item in first + second)
    assert all(item["mutation_authorized"] is False for item in first + second)
    assert (tmp_path / "out" / "mai" / "native-context").is_dir()
    assert (tmp_path / "out" / "moss" / "native-context").is_dir()

    source.write_bytes(b"source drift")
    drift_request = _request(evidence_id="a" * 64)
    drift = wrapper.observe_independent_witnesses(drift_request)
    assert [item["reason_code"] for item in drift] == ["NATIVE_SOURCE_DRIFT"] * 2
    assert verify_calls == {"mai": 1, "moss": 1}


def test_independent_uncertain_result_is_cached_without_retry(tmp_path, monkeypatch):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"stable source bytes")
    source_sha = hashlib.sha256(source.read_bytes()).hexdigest()
    verify_calls: dict[str, int] = {"mai": 0, "moss": 0}

    def factory(**kwargs):
        provider = kwargs["provider"]

        def verify(request):
            verify_calls[provider] += 1
            return _independent_witness(
                request=request,
                source_sha256=source_sha,
                provider=provider,
                transcript="",
                status="NO_SPEECH_REPORTED",
            )

        return verify

    monkeypatch.setattr(native, "build_native_context_verifier", factory)
    wrapper = native.build_independent_context_witnesses(
        fallback=lambda request: {"fallback": request},
        source_media=source,
        output_dir=tmp_path / "out",
        providers=("mai", "moss"),
    )
    first_request = _request(evidence_id="e" * 64)
    second_request = _request(evidence_id="f" * 64)
    second_request["cue_indexes"] = [8]
    second_request["request_sha256"] = native._canonical_sha256(
        {key: value for key, value in second_request.items() if key != "request_sha256"}
    )

    first = wrapper.observe_independent_witnesses(first_request)
    second = wrapper.observe_independent_witnesses(second_request)

    assert verify_calls == {"mai": 1, "moss": 1}
    assert [item["status"] for item in first] == ["UNCERTAIN", "UNCERTAIN"]
    assert [item["reason_code"] for item in first] == ["NATIVE_NO_SPEECH"] * 2
    assert [item["reason_code"] for item in second] == ["NATIVE_NO_SPEECH"] * 2
    assert [item["request_sha256"] for item in second] == [second_request["request_sha256"]] * 2
    assert all(item["served_from_cache"] is True for item in second)
    assert all(item["provider"] in {"mai", "moss"} for item in first + second)


def _install_real_native_budget_seams(monkeypatch):
    """Keep the native builder and receipt writer real while forbidding I/O."""

    def extract(_source, target, _start_ms, _end_ms):
        target.write_bytes(b"synthetic exact audio")

    def observe(audio, **kwargs):
        provider = kwargs["provider"]
        model = {"mai": MAI_MODEL, "moss": MOSS_MODEL}[provider]
        budget = get_budget(kwargs["supplement_source"])
        assert budget is not None
        attempt = budget.consume(
            provider=provider,
            model=model,
            start_ms=kwargs["crop_start_ms"],
            end_ms=kwargs["crop_end_ms"],
            max_attempts_per_window=3,
        )
        persist = kwargs["persist_before_dispatch"]
        assert callable(persist)
        persist()
        budget.finish_attempt(attempt, status="OBSERVED")
        persist()
        return {
            "provider": provider,
            "model": model,
            "input_audio_sha256": hashlib.sha256(audio).hexdigest(),
            "response_sha256": hashlib.sha256(
                f"{provider}:{kwargs['crop_start_ms']}:{kwargs['crop_end_ms']}".encode()
            ).hexdigest(),
            "native_segments": [
                {
                    "start_ms": 100,
                    "end_ms": min(kwargs["duration_ms"] - 100, 1_200),
                    "text": "合成听写",
                }
            ],
            "native_timeline": {"valid": True},
            "raw_response": {"text": "合成听写"},
        }

    monkeypatch.setattr(foreign, "_extract_exact_mp3", extract)
    monkeypatch.setattr(foreign, "observe_secondary", observe)


def test_direct_and_independent_lanes_extend_one_canonical_receipt(
    tmp_path, monkeypatch,
):
    """A factory's direct and independent observers preserve one receipt history."""

    _install_real_native_budget_seams(monkeypatch)
    source = tmp_path / "source.mp4"
    source.write_bytes(b"stable source bytes")
    output = tmp_path / "out"
    canonical_receipt = output / "native-audio-budget.json"

    direct = native.build_native_context_verifier(
        fallback=lambda request: {"fallback": request},
        source_media=source,
        output_dir=output,
        provider="mai",
        budget_receipt_path=canonical_receipt,
    )
    direct_result = direct(_request(matched_start_ms=2_500, matched_end_ms=4_000))
    assert direct_result["status"] == "OBSERVED"

    independent = native.build_independent_context_witnesses(
        fallback=lambda request: {"fallback": request},
        source_media=source,
        output_dir=output / "independent-context",
        providers=("moss",),
        budget_receipt_path=canonical_receipt,
    )
    independent_request = _request(matched_start_ms=5_000, matched_end_ms=6_500)
    first = independent.observe_independent_witnesses(independent_request)
    assert [item["status"] for item in first] == ["OBSERVED"]

    packet = json.loads(canonical_receipt.read_text(encoding="utf-8"))
    attempts = packet["budget"]["attempts"]
    assert [(row["provider"], row["start_ms"], row["end_ms"]) for row in attempts] == [
        ("mai", 12_500, 14_000),
        ("moss", 15_000, 16_500),
    ]
    first_attempt = dict(attempts[0])

    second = independent.observe_independent_witnesses(independent_request)
    assert second[0]["served_from_cache"] is True
    packet_after_cache = json.loads(canonical_receipt.read_text(encoding="utf-8"))
    assert packet_after_cache["budget"]["attempts"] == [*attempts]
    assert packet_after_cache["budget"]["attempts"][0] == first_attempt
    assert not (output / "independent-context" / "native-audio-budget.json").exists()


def test_normal_composition_preserves_gemini_range_and_source_budget(tmp_path):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source for normal composed gemini range")
    output = tmp_path / "out"
    receipt = output / "native-audio-budget.json"
    calls = []

    def fallback(request):
        return {"fallback": request}

    def gemini_range(**kwargs):
        calls.append(kwargs)
        return [{"actual_range_observation": True}]

    fallback.observe_gemini_range_context = gemini_range
    direct = native.build_native_context_verifier(
        fallback=fallback, source_media=source, output_dir=output,
        provider="mai", budget_receipt_path=receipt,
    )
    ordinary = native.build_independent_context_witnesses(
        fallback=direct, source_media=source, output_dir=output / "independent",
        providers=("mai", "moss"), budget_receipt_path=receipt,
    )
    check, broad = _range_requests(hashlib.sha256(source.read_bytes()).hexdigest())
    # Each configured native provider is also its own actual-model input (SUB-20).
    assert ordinary.observe_range_context.available_providers == ("native", "mai", "moss", "gemini")
    assert ordinary.observe_range_context(
        check_request=check, range_witness_request=broad, provider="gemini",
    ) == [{"actual_range_observation": True}]
    assert calls == [{"check_request": check, "range_witness_request": broad,
                      "budget_receipt_path": receipt}]
    from src.autoslice.read_aloud_llm_verifier import build_cpa_read_aloud_verifier

    cpa = build_cpa_read_aloud_verifier(lambda _: "not dispatched", next_verifier=ordinary)
    assert cpa.observe_range_context is ordinary.observe_range_context
    assert cpa.observe_range_context.available_providers == ("native", "mai", "moss", "gemini")


def test_direct_and_independent_share_raw_cache_but_keep_provider_envelopes_separate(
    tmp_path, monkeypatch,
):
    """One exact source/window dispatch is reused across both normal lanes."""

    from src.autoslice import diarized_transcription

    source = tmp_path / "source.mp4"
    source.write_bytes(b"stable source bytes")
    output = tmp_path / "out"
    canonical_receipt = output / "native-audio-budget.json"
    dispatches: list[tuple[str, str]] = []

    def extract(_source, target, _start_ms, _end_ms):
        target.write_bytes(b"synthetic exact audio")

    def transcribe_evidence(audio, provider, duration_ms=None, before_request=None):
        if before_request is not None:
            before_request()
        model = {"mai": MAI_MODEL, "moss": MOSS_MODEL}[provider]
        dispatches.append((provider, model))
        audio_bytes = audio if isinstance(audio, bytes) else Path(audio).read_bytes()
        return {
            "provider": provider,
            "model": model,
            "input_audio_sha256": hashlib.sha256(audio_bytes).hexdigest(),
            "response_sha256": hashlib.sha256(
                f"{provider}:{duration_ms}".encode()
            ).hexdigest(),
            "native_segments": [
                {
                    "start_ms": 100,
                    "end_ms": min(duration_ms - 100, 1_200),
                    "text": "合成听写",
                }
            ],
            "native_timeline": {"valid": True},
            "raw_response": {"text": "合成听写"},
        }

    monkeypatch.setattr(foreign, "_extract_exact_mp3", extract)
    monkeypatch.setattr(diarized_transcription, "transcribe_evidence", transcribe_evidence)

    direct_request = _request(evidence_id="e" * 64)
    direct = native.build_native_context_verifier(
        fallback=lambda request: {"fallback": request},
        source_media=source,
        output_dir=output,
        provider="mai",
        budget_receipt_path=canonical_receipt,
        native_observation_cache_root=output,
    )
    direct_result = direct(direct_request)
    assert direct_result["status"] == "OBSERVED"

    independent_request = _request(evidence_id="f" * 64)
    independent = native.build_independent_context_witnesses(
        fallback=lambda request: {"fallback": request},
        source_media=source,
        output_dir=output / "independent-context",
        providers=("mai", "moss"),
        budget_receipt_path=canonical_receipt,
        native_observation_cache_root=output,
    )
    independent_results = independent.observe_independent_witnesses(independent_request)

    assert [result["provider"] for result in independent_results] == ["mai", "moss"]
    assert all(result["status"] == "OBSERVED" for result in independent_results)
    # The direct MAI call and independent MAI call share the exact raw sidecar;
    # MOSS remains a distinct provider/model observation.
    assert dispatches == [("mai", MAI_MODEL), ("moss", MOSS_MODEL)]
    assert (output / "native-exact" / "mai").is_dir()
    assert (output / "native-exact" / "moss").is_dir()

    direct_envelope = output / "native-context" / f"{direct_request['request_sha256']}.json"
    independent_envelope = (
        output
        / "independent-context"
        / "mai"
        / "native-context"
        / f"{independent_request['request_sha256']}.json"
    )
    moss_envelope = (
        output
        / "independent-context"
        / "moss"
        / "native-context"
        / f"{independent_request['request_sha256']}.json"
    )
    assert direct_envelope.is_file()
    assert independent_envelope.is_file()
    assert moss_envelope.is_file()
    assert json.loads(direct_envelope.read_text(encoding="utf-8"))["request"]["evidence_id"] == "e" * 64
    assert json.loads(independent_envelope.read_text(encoding="utf-8"))["request"]["evidence_id"] == "f" * 64
    assert json.loads(moss_envelope.read_text(encoding="utf-8"))["witness"]["provider"] == "moss"

    packet = json.loads(canonical_receipt.read_text(encoding="utf-8"))
    assert [row["provider"] for row in packet["budget"]["attempts"]] == ["mai", "moss"]


def test_independent_native_budget_receipt_is_shared_across_restart_and_order(
    tmp_path, monkeypatch,
):
    """MAI/MOSS restore one source ledger even when the next run reverses order."""

    _install_real_native_budget_seams(monkeypatch)
    source = tmp_path / "source.mp4"
    source.write_bytes(b"stable source bytes")
    output = tmp_path / "independent-context"
    first_request = _request(evidence_id="1" * 64)

    first_wrapper = native.build_independent_context_witnesses(
        fallback=lambda request: {"fallback": request},
        source_media=source,
        output_dir=output,
        providers=("mai", "moss"),
    )
    first = first_wrapper.observe_independent_witnesses(first_request)
    assert [item["provider"] for item in first] == ["mai", "moss"]
    assert all(item["status"] == "OBSERVED" for item in first)

    receipt_path = output / "native-audio-budget.json"
    first_packet = json.loads(receipt_path.read_text(encoding="utf-8"))
    first_attempts = first_packet["budget"]["attempts"]
    assert [row["provider"] for row in first_attempts] == ["mai", "moss"]
    assert not (output / "mai" / "native-audio-budget.json").exists()
    assert not (output / "moss" / "native-audio-budget.json").exists()

    # A new process starts with an empty in-memory ledger and a different provider order.
    start_budget(source)
    second_wrapper = native.build_independent_context_witnesses(
        fallback=lambda request: {"fallback": request},
        source_media=source,
        output_dir=output,
        providers=("moss", "mai"),
    )
    second = second_wrapper.observe_independent_witnesses(
        _request(
            evidence_id="2" * 64,
            matched_start_ms=5_000,
            matched_end_ms=6_500,
        )
    )
    assert [item["provider"] for item in second] == ["moss", "mai"]
    assert all(item["status"] == "OBSERVED" for item in second)

    final_packet = json.loads(receipt_path.read_text(encoding="utf-8"))
    final_attempts = final_packet["budget"]["attempts"]
    assert final_attempts[:2] == first_attempts
    assert [row["provider"] for row in final_attempts] == ["mai", "moss", "moss", "mai"]
    assert [row["start_ms"] for row in final_attempts] == [
        first_attempts[0]["start_ms"],
        first_attempts[1]["start_ms"],
        15_000,
        15_000,
    ]
    assert (output / "mai" / "native-context").is_dir()
    assert (output / "moss" / "native-context").is_dir()


def test_independent_native_budget_refuses_legacy_provider_receipt_without_canonical(
    tmp_path, monkeypatch,
):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"stable source bytes")
    output = tmp_path / "independent-context"
    legacy = output / "moss" / "native-audio-budget.json"
    legacy.parent.mkdir(parents=True)
    legacy.write_text("{}\n", encoding="utf-8")
    build_calls: list[str] = []

    def factory(**kwargs):
        build_calls.append(kwargs["provider"])
        return lambda request: {"unexpected": request}

    monkeypatch.setattr(native, "build_native_context_verifier", factory)
    wrapper = native.build_independent_context_witnesses(
        fallback=lambda request: {"fallback": request},
        source_media=source,
        output_dir=output,
        providers=("mai", "moss"),
    )
    results = wrapper.observe_independent_witnesses(_request())

    assert build_calls == []
    assert [item["status"] for item in results] == ["UNCERTAIN", "UNCERTAIN"]
    assert [item["reason_code"] for item in results] == [
        native.LEGACY_NATIVE_BUDGET_RECEIPT_MIGRATION_REQUIRED,
        native.LEGACY_NATIVE_BUDGET_RECEIPT_MIGRATION_REQUIRED,
    ]
    assert not (output / "native-audio-budget.json").exists()


def test_explicit_canonical_receipt_refuses_existing_output_receipt(
    tmp_path, monkeypatch,
):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"stable source bytes")
    output = tmp_path / "independent-context"
    output.mkdir()
    legacy = output / "native-audio-budget.json"
    canonical = tmp_path / "canonical" / "native-audio-budget.json"
    canonical.parent.mkdir()
    budget = start_budget(source)
    for provider, model in (("mai", MAI_MODEL), ("mai", MAI_MODEL), ("moss", MOSS_MODEL)):
        attempt = budget.consume(
            provider,
            model,
            60_580,
            64_660,
            max_attempts_per_window=3,
        )
        budget.finish_attempt(attempt, status="OBSERVED")
    source_sha = hashlib.sha256(source.read_bytes()).hexdigest()
    budget_receipts.persist_native_audio_budget_receipt(
        source_media=source,
        source_media_sha256=source_sha,
        receipt_path=legacy,
    )
    budget_receipts.persist_native_audio_budget_receipt(
        source_media=source,
        source_media_sha256=source_sha,
        receipt_path=canonical,
    )
    legacy_bytes = legacy.read_bytes()
    canonical_bytes = canonical.read_bytes()
    build_calls: list[str] = []

    def factory(**kwargs):
        build_calls.append(kwargs["provider"])
        raise AssertionError("legacy receipt conflict must stop before provider construction")

    monkeypatch.setattr(native, "build_native_context_verifier", factory)
    wrapper = native.build_independent_context_witnesses(
        fallback=lambda request: {"fallback": request},
        source_media=source,
        output_dir=output,
        providers=("mai", "moss"),
        budget_receipt_path=canonical,
    )
    results = wrapper.observe_independent_witnesses(_request())

    assert build_calls == []
    assert [item["status"] for item in results] == ["UNCERTAIN", "UNCERTAIN"]
    assert [item["reason_code"] for item in results] == [
        native.LEGACY_NATIVE_BUDGET_RECEIPT_MIGRATION_REQUIRED,
        native.LEGACY_NATIVE_BUDGET_RECEIPT_MIGRATION_REQUIRED,
    ]
    assert legacy.read_bytes() == legacy_bytes
    assert canonical.read_bytes() == canonical_bytes


def test_relative_explicit_canonical_matches_relative_output_receipt(
    tmp_path, monkeypatch,
):
    monkeypatch.chdir(tmp_path)
    _install_real_native_budget_seams(monkeypatch)
    source = Path("source.mp4")
    source.write_bytes(b"stable source bytes")
    output = Path("relative-output")
    canonical = output / "native-audio-budget.json"
    budget = start_budget(source)
    source_sha = hashlib.sha256(source.read_bytes()).hexdigest()
    budget_receipts.persist_native_audio_budget_receipt(
        source_media=source,
        source_media_sha256=source_sha,
        receipt_path=canonical,
    )

    wrapper = native.build_independent_context_witnesses(
        fallback=lambda request: {"fallback": request},
        source_media=source,
        output_dir=output,
        providers=("mai",),
        budget_receipt_path=canonical,
    )
    result = wrapper.observe_independent_witnesses(_request())

    assert [item["status"] for item in result] == ["OBSERVED"]
    packet = json.loads(canonical.read_text(encoding="utf-8"))
    assert len(packet["budget"]["attempts"]) == 1


def test_cached_independent_recovery_reads_exact_receipts_without_provider(tmp_path, monkeypatch):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"stable source bytes")
    source_sha = hashlib.sha256(source.read_bytes()).hexdigest()
    request = _request()
    output = tmp_path / "out"

    def factory(**kwargs):
        return lambda target: _independent_witness(
            request=target, source_sha256=source_sha,
            provider=kwargs["provider"], transcript="完整现稿原话",
        )

    monkeypatch.setattr(native, "build_native_context_verifier", factory)
    normal = native.build_independent_context_witnesses(
        fallback=lambda request: {}, source_media=source,
        output_dir=output, providers=("mai", "moss"),
    )
    assert len(normal.observe_independent_witnesses(request)) == 2
    paths = [output / name / "native-context" / f"{request['request_sha256']}.json"
             for name in ("mai", "moss")]
    originals = [path.read_bytes() for path in paths]

    def forbidden(**kwargs):
        raise AssertionError("cache recovery must never construct a native provider")

    monkeypatch.setattr(native, "build_native_context_verifier", forbidden)
    cold = native.build_independent_context_witnesses(
        fallback=lambda request: {}, source_media=source,
        output_dir=output, providers=("mai", "moss"),
    )
    normal_rows = cold.observe_independent_witnesses(request)
    assert len(normal_rows) == 2
    assert all(row["status"] == "OBSERVED" for row in normal_rows)
    assert all(row["served_from_cache"] is True for row in normal_rows)
    assert [path.read_bytes() for path in paths] == originals

    rows = cold.observe_cached_independent_witnesses(request)
    assert len(rows) == 2
    assert all(row["exact_transcript"] == "完整现稿原话" for row in rows)
    assert all(row["served_from_cache"] is True for row in rows)
    assert [path.read_bytes() for path in paths] == originals
    assert cold.observe_cached_independent_witnesses(_request(offset_ms=10_001)) == []

    for path in paths:
        doc = json.loads(path.read_text())
        doc["native_observation"]["transcript"] = "unauthorized alteration"
        path.write_text(json.dumps(doc))
    tampered_rows = cold.observe_independent_witnesses(request)
    assert [row["status"] for row in tampered_rows] == ["UNCERTAIN", "UNCERTAIN"]
    assert all(row.get("served_from_cache") is not True for row in tampered_rows)
    assert cold.observe_cached_independent_witnesses(request) == []
    for path, original in zip(paths, originals):
        path.write_bytes(original)
    source.write_bytes(b"source drift")
    drift_rows = cold.observe_independent_witnesses(request)
    assert [row["reason_code"] for row in drift_rows] == [
        "NATIVE_SOURCE_DRIFT",
        "NATIVE_SOURCE_DRIFT",
    ]
    assert cold.observe_cached_independent_witnesses(request) == []


def test_independent_normal_cache_dispatches_only_missing_provider(tmp_path, monkeypatch):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"stable source bytes")
    source_sha = hashlib.sha256(source.read_bytes()).hexdigest()
    request = _request()
    output = tmp_path / "out"
    build_calls: list[str] = []

    def factory(**kwargs):
        provider = kwargs["provider"]
        build_calls.append(provider)
        return lambda target: _independent_witness(
            request=target,
            source_sha256=source_sha,
            provider=provider,
            transcript=f"{provider} 独立整 cue 听写",
        )

    monkeypatch.setattr(native, "build_native_context_verifier", factory)
    seeded = native.build_independent_context_witnesses(
        fallback=lambda request: {},
        source_media=source,
        output_dir=output,
        providers=("mai", "moss"),
    )
    assert [row["status"] for row in seeded.observe_independent_witnesses(request)] == [
        "OBSERVED",
        "OBSERVED",
    ]
    paths = [
        output / provider / "native-context" / f"{request['request_sha256']}.json"
        for provider in ("mai", "moss")
    ]
    mai_bytes = paths[0].read_bytes()
    paths[1].unlink()
    build_calls.clear()

    resumed = native.build_independent_context_witnesses(
        fallback=lambda request: {},
        source_media=source,
        output_dir=output,
        providers=("mai", "moss"),
    )
    rows = resumed.observe_independent_witnesses(request)

    assert build_calls == ["moss"]
    assert [row["status"] for row in rows] == ["OBSERVED", "OBSERVED"]
    assert rows[0]["served_from_cache"] is True
    assert rows[1].get("served_from_cache") is not True
    assert paths[0].read_bytes() == mai_bytes
    assert paths[1].is_file()
