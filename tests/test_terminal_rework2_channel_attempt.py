"""D: the Gemini range producer and cold reader share channel semantics."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.autoslice import independent_context_adjudication as independent
from src.autoslice import targeted_relisten as target_material
from src.autoslice import gemini_range_context as gemini
from tests.test_gemini_range_context import _check_request, _range_request, _verifier


def _install_gemini_stub(monkeypatch, source: Path, output: Path, raw_factory):
    """Use the real range owner with a deterministic transport boundary."""

    monkeypatch.setattr(
        gemini,
        "_extract_exact_mp3",
        lambda source_path, output_path, start_ms, end_ms: output_path.write_bytes(
            f"mp3:{start_ms}:{end_ms}".encode()
        ),
    )
    monkeypatch.setattr(
        gemini,
        "_crop_black_frame_audio",
        lambda **kwargs: (kwargs["audio_path"].write_bytes(b"mp4") or True, ""),
    )

    def observe(**kwargs):
        kwargs["on_provider_dispatch"]("gemini_api", "gemini-test")
        job_dir = kwargs["job_dir"]
        prompt_path = job_dir / "prompt.gemini-api.md"
        prompt_path.write_text(gemini.range_transcript_prompt(kwargs["request"]), encoding="utf-8")
        (job_dir / "input.gemini-api.mp3").write_bytes(b"provider-mp3")
        duration = (
            kwargs["request"]["target_audio_end_ms"]
            - kwargs["request"]["target_audio_start_ms"]
        )
        raw = raw_factory(duration)
        response_path = job_dir / "verdict.gemini-api.raw.json"
        if raw is None:
            response_path.write_bytes(b"")
        else:
            response_path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
        return SimpleNamespace(
            observed=raw,
            provider="gemini_api",
            model="gemini-test",
            prompt_path=prompt_path,
            response_path=response_path,
            provider_failures=([] if raw is not None else [{"category": "TRANSPORT_UNKNOWN"}]),
        )

    monkeypatch.setattr(gemini, "_observe_entity_audio", observe)


def _raw_uncertain_empty(duration):
    return {
        "status": "UNCERTAIN",
        "target_audible": False,
        "full_transcript": "",
        "segments": [{
            "segment": "complete_block",
            "start_ms": 0,
            "end_ms": duration,
            "transcript": "",
        }],
    }


def _raw_observed_text(duration):
    return {
        "status": "OBSERVED",
        "target_audible": True,
        "full_transcript": "heard block",
        "segments": [{
            "segment": "complete_block",
            "start_ms": 0,
            "end_ms": duration,
            "transcript": "heard block",
        }],
    }


def _run_range(tmp_path, monkeypatch, raw_factory):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source-media")
    output = tmp_path / "out"
    output.mkdir()
    srt = "1\n00:00:01,000 --> 00:00:03,000\nspoken words\n\n"
    check = _check_request(source, srt)
    broad = _range_request(check)
    verifier = _verifier(source, output)
    _install_gemini_stub(monkeypatch, source, output, raw_factory)
    receipt = output / "native-audio-budget.json"
    result = gemini.observe_gemini_range_context(
        verifier=verifier,
        check_request=check,
        range_witness_request=broad,
        budget_receipt_path=receipt,
    )
    return source, output, srt, check, result, receipt


def test_uncertain_empty_response_roundtrips_and_completes_wide_channel(tmp_path, monkeypatch):
    source, output, srt, check, result, receipt = _run_range(
        tmp_path, monkeypatch, _raw_uncertain_empty
    )
    row = result[0]
    assert row["status"] == "UNCERTAIN"
    assert row["verdict"]["exact_transcript"] == ""
    original = row["verdict"]["original_provider_response"]
    assert original["status"] == "UNCERTAIN"
    assert original["full_transcript"] == ""
    assert original["segments"][0]["transcript"] == ""
    assert "audio_observations" not in row["verdict"]

    validated, reason = independent.validate_range_acoustic_observation(srt, check, row)
    assert validated is not None, reason
    assert validated["status"] == "UNCERTAIN"
    assert validated["verdict"]["exact_transcript"] == ""

    material_check = dict(check)
    material_check["context_only_range_acoustic_observations"] = [row]
    projection = target_material.evidence_projection(material_check)
    exhausted = target_material.target_material_exhaustion(material_check, projection)
    assert exhausted["completed_channels"]["WIDE_CANDIDATE_BLIND_AUDIO"]
    assert exhausted["missing_channels"] == []
    assert exhausted["exhausted"] is True

    packet = json.loads(receipt.read_text(encoding="utf-8"))
    assert packet["budget"]["attempt_count"] == 1
    assert packet["budget"]["attempts"][0]["status"] == "OBSERVED"


def test_observed_text_response_still_roundtrips_through_shared_protocol(tmp_path, monkeypatch):
    _source, _output, srt, check, result, _receipt = _run_range(
        tmp_path, monkeypatch, _raw_observed_text
    )
    row = result[0]
    assert row["status"] == "OBSERVED"
    validated, reason = independent.validate_range_acoustic_observation(srt, check, row)
    assert validated is not None, reason
    assert validated["verdict"]["exact_transcript"] == "heard block"


def test_observed_empty_response_is_rejected_and_not_a_completed_channel(tmp_path, monkeypatch):
    _source, _output, srt, check, result, receipt = _run_range(
        tmp_path,
        monkeypatch,
        lambda duration: {
            **_raw_uncertain_empty(duration),
            "status": "OBSERVED",
            "target_audible": True,
        },
    )
    row = result[0]
    assert row["status"] == "UNCERTAIN"
    assert row["reason_code"] == "GEMINI_RANGE_RESPONSE_EVIDENCE_EMPTY"
    validated, reason = independent.validate_range_acoustic_observation(srt, check, row)
    assert validated is None
    assert reason in {
        "SEGMENTED_RANGE_RESPONSE_EVIDENCE_EMPTY",
        "SEGMENTED_RANGE_TRANSPORT_OR_AUTHORITY_INVALID",
    }
    material_check = dict(check)
    material_check["context_only_range_acoustic_observations"] = [row]
    exhausted = target_material.target_material_exhaustion(material_check)
    assert exhausted["completed_channels"]["WIDE_CANDIDATE_BLIND_AUDIO"] == []
    assert exhausted["exhausted"] is False
    packet = json.loads(receipt.read_text(encoding="utf-8"))
    assert packet["budget"]["attempts"][0]["status"] == "RESPONSE_REJECTED"


@pytest.mark.parametrize('missing', ['full_transcript', 'segment_transcript'])
def test_observed_incomplete_text_keeps_existing_producer_rejection(tmp_path, monkeypatch, missing):
    def response(duration):
        raw = _raw_observed_text(duration)
        if missing == 'full_transcript':
            raw['full_transcript'] = ''
        else:
            raw['segments'][0]['transcript'] = ''
        return raw
    _source, _output, srt, check, result, receipt = _run_range(tmp_path, monkeypatch, response)
    assert result[0]['reason_code'] == 'GEMINI_RANGE_RESPONSE_EVIDENCE_EMPTY'
    validated, _reason = independent.validate_range_acoustic_observation(srt, check, result[0])
    assert validated is None
    assert json.loads(receipt.read_text())['budget']['attempts'][0]['status'] == 'RESPONSE_REJECTED'


@pytest.mark.parametrize('variant', ['inaudible_text', 'half_text'])
def test_uncertain_attempt_cannot_promote_inconsistent_lexical_evidence(tmp_path, monkeypatch, variant):
    def response(duration):
        raw = {**_raw_observed_text(duration), 'status': 'UNCERTAIN'}
        if variant == 'inaudible_text':
            raw['target_audible'] = False
        else:
            raw['segments'][0]['transcript'] = ''
        return raw
    _source, _output, srt, check, result, receipt = _run_range(tmp_path, monkeypatch, response)
    assert result[0]['reason_code'] == 'GEMINI_RANGE_RESPONSE_EVIDENCE_EMPTY'
    validated, _reason = independent.validate_range_acoustic_observation(srt, check, result[0])
    assert validated is None
    assert json.loads(receipt.read_text())['budget']['attempts'][0]['status'] == 'RESPONSE_REJECTED'


def test_unknown_transport_does_not_complete_channel_or_get_promoted(tmp_path, monkeypatch):
    _source, _output, srt, check, result, receipt = _run_range(
        tmp_path, monkeypatch, lambda _duration: None
    )
    row = result[0]
    assert row["status"] == "UNCERTAIN"
    assert row["reason_code"] in {
        "GEMINI_RANGE_RESPONSE_MISSING",
        "GEMINI_RANGE_PROVIDER_FAILED",
    }
    validated, _reason = independent.validate_range_acoustic_observation(srt, check, row)
    assert validated is None
    material_check = dict(check)
    material_check["context_only_range_acoustic_observations"] = [row]
    exhausted = target_material.target_material_exhaustion(material_check)
    assert exhausted["completed_channels"]["WIDE_CANDIDATE_BLIND_AUDIO"] == []
    assert exhausted["exhausted"] is False
    packet = json.loads(receipt.read_text(encoding="utf-8"))
    assert packet["budget"]["attempts"][0]["status"] == "FAILED"


def test_cold_reader_rejects_tampered_raw_after_valid_uncertain_producer(tmp_path, monkeypatch):
    _source, _output, srt, check, result, _receipt = _run_range(
        tmp_path, monkeypatch, _raw_uncertain_empty
    )
    row = result[0]
    raw_path = Path(row["artifact_bindings"]["raw_response_path"])
    raw_path.write_text(
        json.dumps({**_raw_uncertain_empty(2_000), "full_transcript": "tampered"}),
        encoding="utf-8",
    )
    validated, reason = independent.validate_range_acoustic_observation(srt, check, row)
    assert validated is None
    assert reason == "SEGMENTED_RANGE_ARTIFACT_BINDING_INVALID"
