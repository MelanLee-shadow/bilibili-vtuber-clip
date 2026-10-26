from __future__ import annotations

import hashlib
import json
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.autoslice.acoustic_witness_adjudication import build_witness_request
from src.autoslice.entity_audio_verifier import _AgyQuotaCircuitBreaker, _sha256, _source_stat_binding
from src.autoslice import gemini_range_context as gemini


def _check_request(source: Path, srt: str, *, candidate_id: str = "candidate-1") -> dict:
    source_sha = _sha256(source)
    return {
        "schema_version": "context-adjudication-check-request.v1",
        "evidence_id": "evidence-1",
        "cue_indexes": [1],
        "matched_start_ms": 1_000,
        "matched_end_ms": 3_000,
        "context_start_ms": 1_000,
        "context_end_ms": 3_000,
        "source_media_timeline_offset_ms": 100,
        "current_cue": "spoken words",
        "proposed_cue": "different words",
        "whole_clip_current_srt": srt,
        "clip_context_binding": {
            "candidate_id": candidate_id,
            "source_media_sha256s": [source_sha],
        },
    }


def _range_request(check_request: dict, *, start: int = 1_000, end: int = 3_000) -> dict:
    request = build_witness_request(check_request)
    request.pop("syllable_count_hint", None)
    request.pop("request_sha256", None)
    request.update(
        cue_indexes=[1],
        matched_start_ms=start,
        matched_end_ms=end,
        context_start_ms=start,
        context_end_ms=end,
    )
    request["request_sha256"] = hashlib.sha256(
        json.dumps(request, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return request


def _verifier(source: Path, output: Path):
    return SimpleNamespace(
        source_media=source,
        output_dir=output,
        recording_date="2026-10-13",
        source_duration_ms=10_000,
        source_sha256=_sha256(source),
        source_stat_binding=_source_stat_binding(source),
        binary="agy",
        model="test-agy-model",
        timeout="1m",
        timely_context="",
        agy_quota_circuit=_AgyQuotaCircuitBreaker(),
        web_unavailable=threading.Event(),
    )


def test_m29_cached_acquisition_accepts_relocated_source(tmp_path, monkeypatch):
    import shutil

    test_range_observer_uses_exact_block_cache_and_keeps_provider_blind(tmp_path, monkeypatch, None)
    source, output = tmp_path / "source.mp4", tmp_path / "out"
    moved = tmp_path / "copy.mp4"
    shutil.copy2(source, moved)
    verifier = _verifier(moved, output)
    srt = "1\n00:00:01,000 --> 00:00:03,000\nspoken words\n\n"
    check = _check_request(moved, srt)
    geometry = gemini._validate_source_and_geometry(
        verifier=verifier, check_request=check, range_witness_request=_range_request(check))[0]
    prompt = gemini.range_transcript_prompt(gemini._blind_request(geometry))
    prompt_sha = hashlib.sha256(prompt.encode()).hexdigest()
    path = next(gemini._acquisition_root(verifier, geometry, prompt_sha).glob("*/acquisition.json"))
    assert gemini._cache_metadata_valid(path, verifier=verifier, geometry=geometry,
                                       prompt=prompt, prompt_sha=prompt_sha) is not None
    moved.write_bytes(b"changed-media")
    verifier = _verifier(moved, output)
    assert gemini._cache_metadata_valid(path, verifier=verifier, geometry=geometry,
                                       prompt=prompt, prompt_sha=prompt_sha) is None


def test_range_prompt_is_geometry_only_and_declares_nominal_complete_block():
    request = {
        "schema_version": gemini.RANGE_REQUEST_SCHEMA,
        "target_audio_start_ms": 0,
        "target_audio_end_ms": 2_000,
        "timeline_binding": {
            "schema_version": "gemini-semantic-range-timeline-binding.v1",
            "source_media_timeline_offset_ms": 100,
        },
    }
    prompt = gemini.range_transcript_prompt(request)
    assert "complete_block" in prompt
    assert "token timing" in prompt
    assert "0|2000" not in prompt
    with pytest.raises(ValueError):
        gemini.range_transcript_prompt({**request, "timeline_binding": {"current_cue": "leak"}})
    with pytest.raises(ValueError):
        gemini.range_transcript_prompt({**request, "candidate_id": "leak"})


@pytest.mark.parametrize("sound_kind", [None, "environment_sound", "nonsemantic_vocalization"])
def test_range_observer_uses_exact_block_cache_and_keeps_provider_blind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sound_kind
):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source-media")
    output = tmp_path / "out"
    output.mkdir()
    srt = "1\n00:00:01,000 --> 00:00:03,000\nspoken words\n\n"
    check = _check_request(source, srt)
    broad = _range_request(check)
    verifier = _verifier(source, output)
    calls: list[dict] = []
    counts = {"extract": 0, "crop": 0}

    def extract(source_path: Path, output_path: Path, start_ms: int, end_ms: int):
        counts["extract"] += 1
        output_path.write_bytes(f"mp3:{start_ms}:{end_ms}".encode())

    def crop(**kwargs):
        counts["crop"] += 1
        kwargs["audio_path"].write_bytes(b"black-frame-mp4")
        return True, ""

    def observe(**kwargs):
        calls.append(kwargs)
        kwargs["on_provider_dispatch"]("agy", "test-agy-model")
        prompt_path = kwargs["job_dir"] / "prompt.md"
        prompt_path.write_text(gemini.range_transcript_prompt(kwargs["request"]), encoding="utf-8")
        duration = kwargs["request"]["target_audio_end_ms"] - kwargs["request"]["target_audio_start_ms"]
        raw = {
            "status": "OBSERVED",
            "target_audible": True,
            "full_transcript": "heard complete block",
            "segments": [
                {
                    "segment": "complete_block",
                    "start_ms": 0,
                    "end_ms": duration,
                    "transcript": "heard complete block",
                }
            ],
        }
        if sound_kind:
            raw["audio_observations"] = [{"kind": sound_kind, "status": "OBSERVED",
                "start_ms": 200, "end_ms": 500, "description": "brief impact" if sound_kind == "environment_sound" else "nonlexical throat clear"}]
            if sound_kind == "nonsemantic_vocalization":
                raw["full_transcript"] = ""
                raw["segments"][0]["transcript"] = ""
        response_path = kwargs["job_dir"] / "verdict.raw.json"
        response_path.write_text(json.dumps(raw), encoding="utf-8")
        return SimpleNamespace(
            observed=raw,
            provider="agy",
            model="test-agy-model",
            prompt_path=prompt_path,
            response_path=response_path,
            provider_failures=[],
        )

    monkeypatch.setattr(gemini, "_extract_exact_mp3", extract)
    monkeypatch.setattr(gemini, "_crop_black_frame_audio", crop)
    monkeypatch.setattr(gemini, "_observe_entity_audio", observe)
    receipt = output / "native-audio-budget.json"

    first = gemini.observe_gemini_range_context(
        verifier=verifier, check_request=check, range_witness_request=broad,
        budget_receipt_path=receipt,
    )
    assert first[0]["status"] == "OBSERVED"
    from src.autoslice.producer_text_pipeline import _bound_source_evidence_context
    context = {"candidate_id": "candidate-1", "pieces": [
        {"source_media_sha256": "sha256:" + _sha256(source)}]}
    text, discovery = _bound_source_evidence_context(srt, context, 100,
        range_audio=[first[0], first[0]], earlier_screen=(), range_screen=())
    assert discovery["range_count"] == 1 and discovery["provider_calls"] == 0
    assert discovery["mutation_authorized"] is False
    emitted, = json.loads(text.split("\n")[-1])["range_audio"]
    assert emitted["exact_transcript"] == first[0]["verdict"]["exact_transcript"]
    if sound_kind:
        assert emitted["audio_observations"] == first[0]["verdict"]["audio_observations"]
        assert emitted["audio_observation_time_domain"] == "audio_crop_local_ms"
    if sound_kind:
        assert first[0]["verdict"]["audio_observations"][0]["kind"] == sound_kind
        assert first[0]["verdict"]["audio_observation_time_domain"] == "audio_crop_local_ms"
        assert "not transcript words" in first[0]["verdict"]["audio_observation_policy"]
        if sound_kind == "nonsemantic_vocalization":
            assert first[0]["verdict"]["exact_transcript"] == ""
    assert len(calls) == 1
    assert calls[0]["candidates"] == []
    assert "spoken words" not in gemini.range_transcript_prompt(calls[0]["request"])
    packet = json.loads(receipt.read_text(encoding="utf-8"))
    assert packet["budget"]["attempt_count"] == 1

    second = gemini.observe_gemini_range_context(
        verifier=verifier, check_request=check, range_witness_request=broad,
        budget_receipt_path=receipt,
    )
    assert second[0]["status"] == "OBSERVED"
    assert second[0]["verdict"] == first[0]["verdict"]
    assert second[0]["served_from_cache"] is True
    _, reopened = _bound_source_evidence_context(srt, context, 100,
        range_audio=second, earlier_screen=(), range_screen=())
    assert reopened['range_count'] == 1 and reopened['provider_calls'] == 0
    assert len(calls) == 1
    assert counts == {"extract": 1, "crop": 1}
    packet = json.loads(receipt.read_text(encoding="utf-8"))
    assert packet["budget"]["attempt_count"] == 1
    assert packet["budget"]["cache_hit_count"] == 1

    # A current SRT text change gets a fresh validation artifact while the
    # immutable provider acquisition remains reusable.
    changed_srt = "1\n00:00:01,000 --> 00:00:03,000\nchanged words\n\n"
    changed = dict(check)
    changed["whole_clip_current_srt"] = changed_srt
    changed["current_cue"] = "changed words"
    third = gemini.observe_gemini_range_context(
        verifier=verifier, check_request=changed, range_witness_request=broad,
        budget_receipt_path=receipt,
    )
    assert third[0]["status"] == "OBSERVED"
    assert len(calls) == 1
    assert third[0]["cue_texts"][0]["text"] == "changed words"

    # A physical-window change is a cache miss and therefore a new bounded
    # provider dispatch.
    wider = _range_request(check, start=1_000, end=3_000)
    wider["matched_start_ms"] = 900
    wider["context_start_ms"] = 900
    wider["request_sha256"] = hashlib.sha256(
        json.dumps({k: v for k, v in wider.items() if k != "request_sha256"}, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    fourth = gemini.observe_gemini_range_context(
        verifier=verifier, check_request=check, range_witness_request=wider,
        budget_receipt_path=receipt,
    )
    assert fourth[0]["status"] == "OBSERVED"
    _, distinct = _bound_source_evidence_context(srt, context, 100,
        range_audio=[first[0], fourth[0]], earlier_screen=(), range_screen=())
    assert distinct['range_count'] == 2
    _, drifted = _bound_source_evidence_context(srt, context, 101,
        range_audio=first, earlier_screen=(), range_screen=())
    assert drifted['range_count'] == 0
    assert len(calls) == 2
    assert counts["extract"] == 2


@pytest.mark.parametrize("tamper", ["audio", "provider_identity"])
def test_range_observer_rejects_tampered_cache_and_budget_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tamper: str
):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source-media")
    output = tmp_path / "out"
    output.mkdir()
    srt = "1\n00:00:01,000 --> 00:00:03,000\nspoken words\n\n"
    check = _check_request(source, srt)
    broad = _range_request(check)
    verifier = _verifier(source, output)
    calls = {"observe": 0}

    monkeypatch.setattr(
        gemini,
        "_extract_exact_mp3",
        lambda source_path, output_path, start_ms, end_ms: output_path.write_bytes(b"mp3"),
    )
    monkeypatch.setattr(
        gemini,
        "_crop_black_frame_audio",
        lambda **kwargs: (kwargs["audio_path"].write_bytes(b"mp4") or True, ""),
    )

    def observe(**kwargs):
        calls["observe"] += 1
        kwargs["on_provider_dispatch"]("agy", "test-agy-model")
        prompt_path = kwargs["job_dir"] / "prompt.md"
        prompt_path.write_text(gemini.range_transcript_prompt(kwargs["request"]), encoding="utf-8")
        raw = {
            "status": "OBSERVED", "target_audible": True,
            "full_transcript": "heard",
            "segments": [{"segment": "complete_block", "start_ms": 0, "end_ms": 2_000, "transcript": "heard"}],
        }
        response_path = kwargs["job_dir"] / "response.json"
        response_path.write_text(json.dumps(raw), encoding="utf-8")
        return SimpleNamespace(observed=raw, provider="agy", model="test-agy-model",
                               prompt_path=prompt_path, response_path=response_path,
                               provider_failures=[])

    monkeypatch.setattr(gemini, "_observe_entity_audio", observe)
    receipt = output / "native-audio-budget.json"
    first = gemini.observe_gemini_range_context(
        verifier=verifier, check_request=check, range_witness_request=broad,
        budget_receipt_path=receipt,
    )
    assert first[0]["status"] == "OBSERVED"
    raw_bindings = first[0]["artifact_bindings"]
    if tamper == "audio":
        Path(raw_bindings["audio_clip_path"]).write_bytes(b"tampered")
    else:
        metadata_path = next(output.glob("gemini-range-acquisition/**/*/acquisition.json"))
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        metadata["provider"] = "corrupted_identity"
        metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    second = gemini.observe_gemini_range_context(
        verifier=verifier, check_request=check, range_witness_request=broad,
        budget_receipt_path=receipt,
    )
    assert second[0]["status"] == "UNCERTAIN"
    assert second[0]["reason_code"] == "GEMINI_RANGE_CACHE_INVALID"
    assert calls["observe"] == 1

    # A receipt continuity failure is terminal before cache or provider work.
    receipt.write_text("{}", encoding="utf-8")
    third = gemini.observe_gemini_range_context(
        verifier=verifier, check_request=check, range_witness_request=broad,
        budget_receipt_path=receipt,
    )
    assert third[0]["status"] == "UNCERTAIN"
    assert third[0]["reason_code"] == "LOCAL_AUDIO_BUDGET_RECEIPT_PERSIST_FAILED"
    assert calls["observe"] == 1


def test_invalid_provider_shape_is_terminal_response_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source-media")
    output = tmp_path / "out"
    output.mkdir()
    srt = "1\n00:00:01,000 --> 00:00:03,000\nspoken words\n\n"
    check = _check_request(source, srt)
    verifier = _verifier(source, output)
    monkeypatch.setattr(
        gemini,
        "_extract_exact_mp3",
        lambda source_path, output_path, start_ms, end_ms: output_path.write_bytes(b"mp3"),
    )
    monkeypatch.setattr(
        gemini,
        "_crop_black_frame_audio",
        lambda **kwargs: (kwargs["audio_path"].write_bytes(b"mp4") or True, ""),
    )

    def observe(**kwargs):
        kwargs["on_provider_dispatch"]("gemini_api", "gemini-test")
        prompt_path = kwargs["job_dir"] / "prompt.gemini-api.md"
        prompt_path.write_text(gemini.range_transcript_prompt(kwargs["request"]), encoding="utf-8")
        (kwargs["job_dir"] / "input.gemini-api.mp3").write_bytes(b"provider-mp3")
        raw = {"status": "OBSERVED", "target_audible": True, "full_transcript": "bad", "segments": []}
        response_path = kwargs["job_dir"] / "verdict.gemini-api.raw.json"
        response_path.write_text(json.dumps(raw), encoding="utf-8")
        return SimpleNamespace(
            observed=raw,
            provider="gemini_api",
            model="gemini-test",
            prompt_path=prompt_path,
            response_path=response_path,
            provider_failures=[],
        )

    monkeypatch.setattr(gemini, "_observe_entity_audio", observe)
    receipt = output / "native-audio-budget.json"
    result = gemini.observe_gemini_range_context(
        verifier=verifier, check_request=check, range_witness_request=_range_request(check),
        budget_receipt_path=receipt,
    )
    assert result[0]["status"] == "UNCERTAIN"
    packet = json.loads(receipt.read_text(encoding="utf-8"))
    assert packet["budget"]["attempts"][0]["status"] == "RESPONSE_REJECTED"


def test_valid_uncertain_complete_block_is_semantic_cache_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source-media")
    output = tmp_path / "out"
    output.mkdir()
    srt = "1\n00:00:01,000 --> 00:00:03,000\nspoken words\n\n"
    check = _check_request(source, srt)
    broad = _range_request(check)
    verifier = _verifier(source, output)
    calls: list[dict] = []

    monkeypatch.setattr(
        gemini,
        "_extract_exact_mp3",
        lambda source_path, output_path, start_ms, end_ms: output_path.write_bytes(b"mp3"),
    )
    monkeypatch.setattr(
        gemini,
        "_crop_black_frame_audio",
        lambda **kwargs: (kwargs["audio_path"].write_bytes(b"mp4") or True, ""),
    )

    def observe(**kwargs):
        calls.append(kwargs)
        kwargs["on_provider_dispatch"]("gemini_api", "gemini-3.6-flash")
        job_dir = kwargs["job_dir"]
        (job_dir / "input.gemini-api.mp3").write_bytes(b"provider-mp3")
        prompt_path = job_dir / "prompt.gemini-api.md"
        prompt_path.write_text(gemini.range_transcript_prompt(kwargs["request"]), encoding="utf-8")
        duration = kwargs["request"]["target_audio_end_ms"]
        raw = {
            "status": "UNCERTAIN",
            "target_audible": True,
            "full_transcript": "heard but ambiguous complete block",
            "segments": [{
                "segment": "complete_block",
                "start_ms": 0,
                "end_ms": duration,
                "transcript": "heard but ambiguous complete block",
            }],
        }
        # Exercise the real transport-source preservation path: the provider
        # returned fenced JSON, so the validator gets a normalized derivative
        # while metadata keeps this exact source file and hash.
        response_path = job_dir / "verdict.gemini-api.raw.json"
        response_path.write_text("```json\n" + json.dumps(raw) + "\n```\n", encoding="utf-8")
        return SimpleNamespace(
            observed=raw,
            provider="gemini_api",
            model="gemini-3.6-flash",
            prompt_path=prompt_path,
            response_path=response_path,
            provider_failures=[],
            accepted_key_tier="free",
            accepted_key_ordinal=1,
            configured_key_count=2,
            paid_policy_stamp=None,
        )

    monkeypatch.setattr(gemini, "_observe_entity_audio", observe)
    receipt = output / "native-audio-budget.json"
    first = gemini.observe_gemini_range_context(
        verifier=verifier, check_request=check, range_witness_request=broad,
        budget_receipt_path=receipt,
    )
    assert first[0]["status"] == "UNCERTAIN"
    assert first[0]["evidence_scope"] == "RANGE_CONTEXT_ONLY"
    assert first[0]["verdict"]["exact_transcript"] == "heard but ambiguous complete block"
    assert first[0]["verdict"]["original_provider_response"]["status"] == "UNCERTAIN"
    assert len(calls) == 1

    raw_path = Path(first[0]["artifact_bindings"]["raw_response_path"])
    acquisition = json.loads((raw_path.parent / "acquisition.json").read_text(encoding="utf-8"))
    assert acquisition["status"] == "UNCERTAIN"
    assert acquisition["raw_status"] == "UNCERTAIN"
    assert acquisition["response_source"]["path"].endswith("verdict.gemini-api.raw.json")
    assert acquisition["paths"]["actual_provider_audio"]["path"].endswith("input.gemini-api.mp3")
    execution = json.loads(Path(acquisition["paths"]["execution"]["path"]).read_text())
    assert execution["provider_route"]["configured_key_count"] == 2
    assert execution["provider_route"]["accepted_key_tier"] == "free"
    assert execution["provider_route"]["accepted_key_ordinal"] == 1

    second = gemini.observe_gemini_range_context(
        verifier=verifier, check_request=check, range_witness_request=broad,
        budget_receipt_path=receipt,
    )
    assert second[0]["status"] == "UNCERTAIN"
    assert second[0]["served_from_cache"] is True
    assert len(calls) == 1
    packet = json.loads(receipt.read_text(encoding="utf-8"))
    assert packet["budget"]["attempt_count"] == 1
    assert packet["budget"]["cache_hit_count"] == 1


def test_transport_response_drift_is_terminal_and_never_authority_or_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source-media")
    output = tmp_path / "out"
    output.mkdir()
    srt = "1\n00:00:01,000 --> 00:00:03,000\nspoken words\n\n"
    check = _check_request(source, srt)
    broad = _range_request(check)
    verifier = _verifier(source, output)
    calls = {"observe": 0}
    monkeypatch.setattr(
        gemini,
        "_extract_exact_mp3",
        lambda source_path, output_path, start_ms, end_ms: output_path.write_bytes(b"mp3"),
    )
    monkeypatch.setattr(
        gemini,
        "_crop_black_frame_audio",
        lambda **kwargs: (kwargs["audio_path"].write_bytes(b"mp4") or True, ""),
    )

    def observe(**kwargs):
        calls["observe"] += 1
        kwargs["on_provider_dispatch"]("agy", "test-agy-model")
        job_dir = kwargs["job_dir"]
        prompt_path = job_dir / "prompt.md"
        prompt_path.write_text(gemini.range_transcript_prompt(kwargs["request"]), encoding="utf-8")
        duration = kwargs["request"]["target_audio_end_ms"]
        observed = {
            "status": "OBSERVED", "target_audible": True,
            "full_transcript": "provider parsed text",
            "segments": [{"segment": "complete_block", "start_ms": 0,
                          "end_ms": duration, "transcript": "provider parsed text"}],
        }
        response_path = job_dir / "response.json"
        response_path.write_text(json.dumps({**observed, "full_transcript": "different bytes"}), encoding="utf-8")
        return SimpleNamespace(observed=observed, provider="agy", model="test-agy-model",
                               prompt_path=prompt_path, response_path=response_path,
                               provider_failures=[])

    monkeypatch.setattr(gemini, "_observe_entity_audio", observe)
    receipt = output / "native-audio-budget.json"
    first = gemini.observe_gemini_range_context(
        verifier=verifier, check_request=check, range_witness_request=broad,
        budget_receipt_path=receipt,
    )
    assert first[0]["status"] == "UNCERTAIN"
    assert first[0]["reason_code"] == "GEMINI_RANGE_RESPONSE_SOURCE_DRIFT"
    assert calls["observe"] == 1
    second = gemini.observe_gemini_range_context(
        verifier=verifier, check_request=check, range_witness_request=broad,
        budget_receipt_path=receipt,
    )
    assert second[0]["status"] == "UNCERTAIN"
    assert calls["observe"] == 1
    packet = json.loads(receipt.read_text(encoding="utf-8"))
    assert packet["budget"]["attempt_count"] == 1
    assert packet["budget"]["attempts"][0]["status"] == "RESPONSE_REJECTED"


def test_negative_delivery_preclip_uses_valid_physical_source_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source-media")
    output = tmp_path / "out"
    output.mkdir()
    srt = "1\n00:00:01,000 --> 00:00:03,000\nspoken words\n\n"
    check = _check_request(source, srt)
    broad = _range_request(check, start=-50, end=3_000)
    verifier = _verifier(source, output)
    calls: list[dict] = []
    monkeypatch.setattr(
        gemini,
        "_extract_exact_mp3",
        lambda source_path, output_path, start_ms, end_ms: output_path.write_bytes(b"mp3"),
    )
    monkeypatch.setattr(
        gemini,
        "_crop_black_frame_audio",
        lambda **kwargs: (kwargs["audio_path"].write_bytes(b"mp4") or True, ""),
    )

    def observe(**kwargs):
        calls.append(kwargs)
        kwargs["on_provider_dispatch"]("agy", "test-agy-model")
        prompt_path = kwargs["job_dir"] / "prompt.md"
        prompt_path.write_text(gemini.range_transcript_prompt(kwargs["request"]), encoding="utf-8")
        duration = kwargs["request"]["target_audio_end_ms"]
        raw = {
            "status": "OBSERVED", "target_audible": True,
            "full_transcript": "heard", "segments": [{
                "segment": "complete_block", "start_ms": 0,
                "end_ms": duration, "transcript": "heard",
            }],
        }
        response_path = kwargs["job_dir"] / "response.json"
        response_path.write_text(json.dumps(raw), encoding="utf-8")
        return SimpleNamespace(observed=raw, provider="agy", model="test-agy-model",
                               prompt_path=prompt_path, response_path=response_path,
                               provider_failures=[])

    monkeypatch.setattr(gemini, "_observe_entity_audio", observe)
    result = gemini.observe_gemini_range_context(
        verifier=verifier, check_request=check, range_witness_request=broad,
        budget_receipt_path=output / "native-audio-budget.json",
    )
    assert result[0]["status"] == "OBSERVED"
    assert result[0]["range"]["matched_start_ms"] == -50
    assert result[0]["source_window"]["crop_start_ms"] == 50
    assert calls[0]["request"]["target_audio_end_ms"] == 3_050


def test_negative_delivery_preclip_rejects_negative_physical_window_before_io(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source-media")
    output = tmp_path / "out"
    output.mkdir()
    srt = "1\n00:00:01,000 --> 00:00:03,000\nspoken words\n\n"
    check = _check_request(source, srt)
    broad = _range_request(check, start=-150, end=3_000)
    verifier = _verifier(source, output)
    counts = {"extract": 0, "observe": 0}

    def extract(*args, **kwargs):
        counts["extract"] += 1

    def observe(**kwargs):
        counts["observe"] += 1

    monkeypatch.setattr(gemini, "_extract_exact_mp3", extract)
    monkeypatch.setattr(gemini, "_observe_entity_audio", observe)
    result = gemini.observe_gemini_range_context(
        verifier=verifier, check_request=check, range_witness_request=broad,
        budget_receipt_path=output / "native-audio-budget.json",
    )
    assert result[0]["status"] == "UNCERTAIN"
    assert "PHYSICAL_GEOMETRY" in result[0]["reason_code"]
    assert counts == {"extract": 0, "observe": 0}


def test_dispatch_persistence_failure_stops_before_provider_body_or_key_failover(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source-media")
    output = tmp_path / "out"
    output.mkdir()
    srt = "1\n00:00:01,000 --> 00:00:03,000\nspoken words\n\n"
    check = _check_request(source, srt)
    broad = _range_request(check)
    verifier = _verifier(source, output)
    monkeypatch.setattr(
        gemini,
        "_extract_exact_mp3",
        lambda source_path, output_path, start_ms, end_ms: output_path.write_bytes(b"mp3"),
    )
    monkeypatch.setattr(
        gemini,
        "_crop_black_frame_audio",
        lambda **kwargs: (kwargs["audio_path"].write_bytes(b"mp4") or True, ""),
    )
    real_persist = gemini._persist_budget_receipt
    persist_calls = {"count": 0}
    callback_calls = {"count": 0}
    provider_body_calls = {"count": 0}

    def fail_after_initial(*args, **kwargs):
        persist_calls["count"] += 1
        if persist_calls["count"] >= 2:
            raise OSError("receipt write failed")
        return real_persist(*args, **kwargs)

    monkeypatch.setattr(gemini, "_persist_budget_receipt", fail_after_initial)

    def observe(**kwargs):
        callback_calls["count"] += 1
        kwargs["on_provider_dispatch"]("gemini_api", "gemini-3.6-flash")
        provider_body_calls["count"] += 1
        raise AssertionError("provider body must not run after reservation persistence failure")

    monkeypatch.setattr(gemini, "_observe_entity_audio", observe)
    result = gemini.observe_gemini_range_context(
        verifier=verifier, check_request=check, range_witness_request=broad,
        budget_receipt_path=output / "native-audio-budget.json",
    )
    assert result[0]["status"] == "UNCERTAIN"
    assert result[0]["reason_code"] == "GEMINI_RANGE_BUDGET_PERSIST_FAILED"
    assert callback_calls["count"] == 1
    assert provider_body_calls["count"] == 0
    packet = json.loads((output / "native-audio-budget.json").read_text(encoding="utf-8"))
    assert packet["budget"]["attempt_count"] == 0
