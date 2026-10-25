"""Actual saved native range artifacts flow into the ordinary span judge."""

import json
import socket

import pytest

from src.autoslice import native_context_witness as native
from src.autoslice.acoustic_witness_adjudication import build_witness_request
from src.autoslice.final_review_auditor import adjudicate_context_finding
from src.autoslice.local_asr_target_evidence import MODELS, exact_target_evidence, digest
from src.autoslice.span_phoneme_resolution import FIELD, validated_resolution, range_observations
from tests import test_span_phoneme_resolution as fixture


def write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False))


def existing_range(tmp_path, source, source_srt, context, provider, transcript):
    tmp_path.mkdir()
    broad = build_witness_request(
        {
            "evidence_id": "fixture-broad-" + provider,
            "cue_indexes": [1],
            "matched_start_ms": 1000,
            "matched_end_ms": 4000,
            "context_start_ms": 1000,
            "context_end_ms": 4000,
            "source_media_timeline_offset_ms": 0,
        }
    )
    paths = {
        key: tmp_path / key
        for key in (
            "input",
            "native_envelope",
            "provider_receipt",
            "audio_clip",
            "reviewed_srt",
            "raw_response",
        )
    }
    paths["source_media"] = source
    paths["reviewed_srt"].write_text(source_srt)
    audio = b"fake transport audio bytes"
    paths["audio_clip"].write_bytes(audio)
    source_sha = fixture.sha(source.read_bytes())
    raw = {"text": transcript, "segments": [{"start_ms": 0, "end_ms": 3000, "text": transcript}]}
    metadata = {
        "provider": provider,
        "model": MODELS[provider],
        "status": "OK",
        "candidate_exposure": "none",
        "input_audio_sha256": fixture.sha(audio),
        "response_sha256": fixture.sha(json.dumps(raw)),
        "raw_response": raw,
        "native_segments": raw["segments"],
    }
    binding = {k: metadata[k] for k in ("provider", "model", "input_audio_sha256")}
    binding.update(
        contract="candidate-free-native-secondary-v1", duration_ms=3000, candidate_exposure="none"
    )
    write(
        paths["provider_receipt"],
        {"binding": binding, "evidence": metadata, "evidence_sha256": digest(metadata)},
    )
    write(paths["raw_response"], raw)
    observed = exact_target_evidence(
        metadata, audio=audio, source_sha256=source_sha, start_ms=1000, end_ms=4000
    )
    witness = native._native_transcript_witness(
        request=broad,
        prepared=native._prepare_request(broad),
        native_observation=observed,
        source_sha256=source_sha,
        provider=provider,
    )
    write(
        paths["native_envelope"],
        {
            "schema_version": "native-context-witness-envelope.v1",
            "request": broad,
            "request_sha256": broad["request_sha256"],
            "source_media_sha256": source_sha,
            "audio_start_ms": 1000,
            "audio_end_ms": 4000,
            "native_observation": observed,
            "witness": witness,
        },
    )
    write(
        paths["input"],
        {
            "candidate_id": context["candidate_id"],
            "source_media_path": str(source),
            "source_media_sha256": source_sha,
            "reviewed_srt_path": str(paths["reviewed_srt"]),
            "reviewed_srt_sha256": fixture.sha(source_srt),
            "witness_request": broad,
            "witness_request_sha256": broad["request_sha256"],
            "candidate_blind": True,
            "candidate_text_in_request": False,
            "srt_mutation": False,
            "publication": "NOT_REQUESTED",
            "source_media_timeline_offset_ms": 0,
            "delivery_window": {"cue_indexes": [1], "start_ms": 1000, "end_ms": 4000},
        },
    )
    bindings = {}
    for key, path in paths.items():
        bindings[key + "_path"] = str(path)
        bindings[key + "_sha256"] = fixture.sha(path.read_bytes())
    return {"artifact_bindings": bindings}, paths


@pytest.fixture(autouse=True)
def offline(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTOSLICE_BASE", str(tmp_path / "runtime"))

    def forbidden(*a, **kw):
        pytest.fail("No provider/network in span range controls")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)


def exercise(tmp_path, monkeypatch, *, conflicting=False):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"synthetic source, not played")
    monkeypatch.setattr(fixture, "SOURCE_SHA", fixture.sha(source.read_bytes()))
    text = fixture.srt("你想，甲0呀")
    from src.autoslice.clip_context import _canonical_json_sha256

    context = {
        "schema_version": "lidousha-clip-context.v1",
        "candidate_id": "range-span-test",
        "recording_date": "2026-10-16",
        "mutation_authorized": False,
        "whole_clip_draft_srt": text,
        "whole_clip_draft_srt_sha256": "sha256:" + fixture.sha(text),
        "retrieval_budget": {"whole_clip_transcript_truncated": False},
        "pieces": [
            {"source_media_sha256": "sha256:" + fixture.SOURCE_SHA, "start_ms": 0, "end_ms": 4000}
        ],
    }
    context["context_sha256"] = _canonical_json_sha256(context)
    rows = []
    paths = []
    for provider in ("mai", "moss"):
        row, refs = existing_range(
            tmp_path / provider,
            source,
            text,
            context,
            provider,
            "你想，甲蓝呀" if provider == "moss" and conflicting else "你想，甲零呀",
        )
        rows.append(row)
        paths.append(refs)
    verifier = fixture.Observers()
    verifier.span_termination_ordinary_routes += ("range:mai", "range:moss")
    finding = {
        "cue_index": 1,
        "suspect": "甲0",
        "suggestion": "甲零",
        "repair_class": "phonetic",
        "base_text_sha256": fixture.sha("你想，甲0呀"),
        "_prior_range_acoustic_observations": rows,
    }
    output, audit = adjudicate_context_finding(
        text,
        finding,
        clip_context=context,
        entity_verifier=verifier,
        judge_llm_call=fixture.model_for(),
        require_complete_utterance_support=True,
    )
    return output, audit, paths


def test_completed_wide_routes_replay_bound_bytes_through_normal_adjudicator(tmp_path, monkeypatch):
    output, audit, paths = exercise(tmp_path, monkeypatch)
    assert output == fixture.srt("你想，甲零呀"), audit.get("witness_judge", {}).get(
        "span_termination_blocked_reason"
    )
    proof = validated_resolution(audit[FIELD])
    assert len(proof["route_state"]["ordinary"]) == 5
    assert len(proof["comparisons"]) == 5
    assert all(row["status"] == "COMPLETED" for row in proof["route_state"]["ordinary"])
    restored = json.loads(json.dumps(proof))
    paths[1]["audio_clip"].write_bytes(b"changed audio after original receipt")
    with pytest.raises(ValueError):
        validated_resolution(restored)


def test_real_range_phoneme_conflict_is_not_outvoted_by_exact_observations(tmp_path, monkeypatch):
    output, audit, _ = exercise(tmp_path, monkeypatch, conflicting=True)
    assert output == fixture.srt("你想，甲0呀") and not audit["repaired"]
    assert (
        audit["witness_judge"]["span_termination_blocked_reason"]
        == "SPAN_TARGET_PHONEMES_INCOMPATIBLE"
    )


def test_unlocated_multicue_range_is_not_relabelled_an_exact_target(tmp_path):
    from tests.test_independent_context_adjudication import _native_range_artifacts
    from src.autoslice.final_review_auditor import build_context_adjudication_request

    source, finding, context, _, row, paths = _native_range_artifacts(tmp_path, "moss")
    request = build_context_adjudication_request(
        source,
        {**finding, "_prior_range_acoustic_observations": [row]},
        clip_context=context,
        source_media_timeline_offset_ms=100,
    )
    observations, receipts, invalid = range_observations(request)
    assert observations == [] and invalid == [] and len(receipts) == 1
    assert receipts[0]["target_projection"] == "CONTEXT_ONLY_NO_TARGET_TIMING"
    assert receipts[0]["status"] == "COMPLETED"
    assert receipts[0]["raw_range"]["transcript_scope"] == "RANGE_WITHOUT_SINGLE_CUE_TIMESTAMPS"
