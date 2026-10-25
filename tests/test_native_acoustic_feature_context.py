"""Contract tests for the tone-only context adapter."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from src.autoslice.acoustic_witness_adjudication import (
    BLIND_PINYIN_PROTOCOL,
    build_witness_request,
    judge_word_choice,
)
from src.autoslice.final_review_auditor import build_context_adjudication_request
from src.autoslice.independent_context_adjudication import validate_range_acoustic_observation
from src.autoslice.native_acoustic_feature_context import TONE_CONTEXT_SCHEMA


SOURCE = "1\n00:01:11,740 --> 00:01:14,740\n哈哈哈哈哈呵魔女之夜\n"
MODEL = "gemini-3.8-flash-high"
OFFSET = 10_490
DELIVERY = [71_740, 74_740]
SOURCE_TARGET = [82_230, 85_230]
AUDIO_WINDOW = [79_030, 92_400]
ATTACHMENT = [5_520, 6_200]
CANDIDATE = "semantictalk_712160_967140"


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_json(path: Path, value: object) -> str:
    path.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    return _sha(path)


def _observation() -> dict[str, object]:
    return {
        "status": "OBSERVED",
        "target_interval_seconds": [5.52, 6.2],
        "heard_speech_interval_seconds": [5.52, 6.15],
        "speech_syllable_count": 4,
        "syllables": [
            {
                "index": 1,
                "interval_seconds": [5.52, 5.63],
                "tone": "2",
                "pitch_contour": "RISING",
                "acoustic_note": "Voiced upward glide.",
            },
            {
                "index": 2,
                "interval_seconds": [5.67, 5.79],
                "tone": "3",
                "pitch_contour": "DIPPING",
                "acoustic_note": "Low dipping contour.",
            },
            {
                "index": 3,
                "interval_seconds": [5.87, 5.99],
                "tone": "1",
                "pitch_contour": "LEVEL",
                "acoustic_note": "Stable level contour.",
            },
            {
                "index": 4,
                "interval_seconds": [6.03, 6.15],
                "tone": "4",
                "pitch_contour": "FALLING",
                "acoustic_note": "Falling contour into laughter.",
            },
        ],
        "third_syllable_assessable": True,
        "evidence_basis": "Direct audible perception of the supplied attachment.",
        "uncertainty": "Minor boundary uncertainty after the fourth syllable.",
    }


def _stdout(observation: dict[str, object], audio_path: str) -> str:
    init = {
        "event": "init",
        "init": {"model": MODEL},
    }
    tool_active = {
        "event": "step_update",
        "step_update": {
            "step_index": 2,
            "state": "ACTIVE",
            "step_type": "tool",
            "tool_name": "view_file",
            "tool_info": {"parameters": {"AbsolutePath": audio_path}},
        },
    }
    tool_done = {
        "event": "step_update",
        "step_update": {
            "step_index": 2,
            "state": "DONE",
            "step_type": "tool",
            "tool_name": "view_file",
            "tool_info": {
                "name": "view_file",
                "parameters": {"AbsolutePath": audio_path},
            },
        },
    }
    result = {
        "event": "result",
        "result": {"status": "SUCCESS", "structured_output": observation},
    }
    return "\n".join(
        json.dumps(value, ensure_ascii=False) for value in (init, tool_active, tool_done, result)
    ) + "\n"


def _packet(tmp_path: Path) -> tuple[dict[str, object], dict[str, object], dict[str, object]]:
    source_media = tmp_path / "padded.mp4"
    audio = tmp_path / "input.mp3"
    source_media.write_bytes(b"source media bytes")
    audio.write_bytes(b"audio bytes")
    request_path = tmp_path / "REQUEST.json"
    observation_path = tmp_path / "OBSERVATION.json"
    execution_path = tmp_path / "EXECUTION.json"
    stdout_path = tmp_path / "stdout.ndjson"
    request = {
        "schema": {"type": "object"},
        "audio_path": str(audio),
        "audio_sha256": _sha(audio),
        "candidate_exposure": "none; no words, pinyin, subtitle, or alternatives supplied",
        "explicit_remote_file_writes": False,
        "model": MODEL,
        "mutations_authorized": False,
        "release_authorized": False,
        "prompt": "Bounded acoustic tone perception only; do not transcribe words.",
        "target_attachment_ms": ATTACHMENT,
        "transport": "existing OCI3 AGY native CLI; stdout only",
    }
    observed = _observation()
    stdout_path.write_text(_stdout(observed, str(audio)), encoding="utf-8")
    request_sha = _write_json(request_path, request)
    observation_sha = _write_json(observation_path, observed)
    execution = {
        "authority": "EVIDENCE_ONLY",
        "model_requested": MODEL,
        "provider_cli_dispatches": 1,
        "request_path": str(request_path),
        "request_sha256": request_sha,
        "returncode": 0,
        "stdout_path": str(stdout_path),
        "stdout_sha256": _sha(stdout_path),
    }
    execution_sha = _write_json(execution_path, execution)
    artifacts = {
        "request_path": str(request_path),
        "request_sha256": request_sha,
        "observation_path": str(observation_path),
        "observation_sha256": observation_sha,
        "execution_path": str(execution_path),
        "execution_sha256": execution_sha,
        "stdout_path": str(stdout_path),
        "stdout_sha256": _sha(stdout_path),
        "audio_path": str(audio),
        "audio_sha256": _sha(audio),
        "source_media_path": str(source_media),
        "source_media_sha256": _sha(source_media),
    }
    source_binding = {
        "source_media_sha256": _sha(source_media),
        "audio_sha256": _sha(audio),
        "source_media_timeline_offset_ms": OFFSET,
        "candidate_id": CANDIDATE,
        "cue_index": 1,
        "base_text_sha256": hashlib.sha256("哈哈哈哈哈呵魔女之夜".encode()).hexdigest(),
        "current_srt_sha256": hashlib.sha256(SOURCE.encode()).hexdigest(),
        "delivery_target_ms": DELIVERY,
        "source_target_ms": SOURCE_TARGET,
        "audio_window_ms": AUDIO_WINDOW,
        "target_attachment_ms": ATTACHMENT,
        "model": MODEL,
        "scope": "PARTIAL_CONTEXT",
    }
    envelope = {
        "schema_version": TONE_CONTEXT_SCHEMA,
        "authority": "EVIDENCE_ONLY",
        "mutation_authorized": False,
        "release_authorized": False,
        "candidate_exposure": "none",
        "artifact_bindings": artifacts,
        "source_binding": source_binding,
    }
    clip = {
        "schema_version": "lidousha-clip-context.v1",
        "candidate_id": CANDIDATE,
        "pieces": [{"source_media_sha256": _sha(source_media)}],
        "context_sha256": "context",
        "whole_clip_draft_srt_sha256": hashlib.sha256(SOURCE.encode()).hexdigest(),
        "mutation_authorized": False,
    }
    return envelope, clip, {"request": request, "observation": observed, "paths": {
        "request": request_path, "observation": observation_path,
        "execution": execution_path, "stdout": stdout_path,
        "audio": audio, "source_media": source_media,
    }}


def _current_request(envelope, clip):
    finding = {
        "cue_index": 1,
        "suspect": "之夜",
        "suggestion": "职业",
        "proposed_full_cue": "哈哈哈哈哈呵魔女职业",
        "repair_class": "phonetic",
    }
    return build_context_adjudication_request(
        SOURCE,
        {**finding, "_prior_range_acoustic_observations": [envelope]},
        clip_context=clip,
        source_media_timeline_offset_ms=OFFSET,
    )


def test_tone_packet_enters_cpa_as_partial_context_without_transcript(tmp_path):
    envelope, clip, _ = _packet(tmp_path)
    request = _current_request(envelope, clip)
    rows = request["context_only_range_acoustic_observations"]
    assert len(rows) == 1
    row = rows[0]
    assert row["evidence_scope"] == "PARTIAL_CONTEXT"
    assert row["transcript_scope"] == "TONE_FEATURES_ONLY_NO_TRANSCRIPT"
    assert row["tone_feature_window"]["delivery_ms"] == [74060, 74740]
    assert row["verdict"]["tone_sequence"] == ["2", "3", "1", "4"]
    assert "heard_pinyin" not in row["verdict"]
    assert "exact_transcript" not in row["verdict"]
    assert row["mutation_authorized"] is False
    assert row["release_authorized"] is False

    prompts: list[str] = []
    narrow = build_witness_request(request)
    witness = {
        "schema_version": "subtitle-span-acoustic-witness.v1",
        "witness_protocol": BLIND_PINYIN_PROTOCOL,
        "request_sha256": narrow["request_sha256"],
        "status": "OBSERVED",
        "target_audible": True,
        "heard_pinyin": "mo nv zhi ye",
        "uncertain_positions": [],
        "candidate_exposure": "none",
        "authority": "EVIDENCE_ONLY",
        "mutation_authorized": False,
    }
    judge_word_choice(
        llm_call=lambda prompt: prompts.append(prompt) or json.dumps(
            {"status": "JUDGED", "choice": "CURRENT", "reason": "context remains evidence"}
        ),
        check_request=request,
        witness=witness,
    )
    assert prompts and "PARTIAL_CONTEXT" in prompts[0]
    assert "TONE_FEATURES_ONLY_NO_TRANSCRIPT" in prompts[0]
    assert "tone_sequence" in prompts[0]


def test_tone_packet_rejects_mutation_authority(tmp_path):
    envelope, clip, _ = _packet(tmp_path)
    envelope["mutation_authorized"] = True
    request = _current_request(envelope, clip)
    validated, reason = validate_range_acoustic_observation(SOURCE, request, envelope)
    assert validated is None
    assert reason == "TONE_CONTEXT_MUTATION_AUTHORITY"


def test_m13_tone_context_accepts_rebased_physical_target(tmp_path):
    envelope, clip, _ = _packet(tmp_path)
    request = _current_request(envelope, clip)
    request.update(matched_start_ms=SOURCE_TARGET[0], matched_end_ms=SOURCE_TARGET[1],
                   source_media_timeline_offset_ms=0)
    checked, reason = validate_range_acoustic_observation(SOURCE, request, envelope)
    assert checked is not None, reason
    request["matched_end_ms"] += 1
    checked, reason = validate_range_acoustic_observation(SOURCE, request, envelope)
    assert checked is None and reason == "TONE_CONTEXT_CURRENT_TARGET_DRIFT"


@pytest.mark.parametrize("drift", [None, "parent", "piece", "provenance", "missing"])
def test_tone_packet_replays_recording_to_padded_provenance(tmp_path, drift):
    from tests.test_independent_context_adjudication import _padded_recording_provenance

    envelope, clip, files = _packet(tmp_path)
    original_sha, original, piece, _piece_proof, padded_proof = _padded_recording_provenance(
        tmp_path, files["paths"]["source_media"],
    )
    clip["pieces"][0]["source_media_sha256"] = "sha256:" + original_sha
    before = {name: path.read_bytes() for name, path in files["paths"].items()}
    if drift == "parent":
        original.write_bytes(b"changed parent")
    elif drift == "piece":
        piece.write_bytes(b"changed recut")
    elif drift == "provenance":
        proof = json.loads(padded_proof.read_text())
        proof["output_sha256"] = "f" * 64
        _write_json(padded_proof, proof)
    elif drift == "missing":
        padded_proof.unlink()
    request = _current_request(envelope, clip)
    checked, reason = validate_range_acoustic_observation(SOURCE, request, envelope)
    if drift:
        assert checked is None and reason == "TONE_CONTEXT_CURRENT_SOURCE_DRIFT"
        assert "context_only_range_acoustic_observations" not in request
    else:
        assert checked is not None, reason
        binding = checked["media_source_binding"]
        assert binding["recording_sha256"] == original_sha
        assert binding["media_sha256"] == envelope["source_binding"]["source_media_sha256"]
        assert binding["padded_provenance_sha256"] == _sha(padded_proof)
        assert request["clip_context_binding"]["source_media_sha256s"] == ["sha256:" + original_sha]
        assert request["context_only_range_acoustic_observations"][0]["verdict"]["tone_sequence"] == ["2", "3", "1", "4"]
    assert {name: path.read_bytes() for name, path in files["paths"].items()} == before


@pytest.mark.parametrize("drift", ["source", "offset", "srt", "artifact", "model", "stdout", "exposure"])
def test_tone_packet_rejects_binding_or_transport_drift(tmp_path, drift):
    envelope, clip, files = _packet(tmp_path)
    if drift == "source":
        envelope["source_binding"]["source_media_sha256"] = "f" * 64
    elif drift == "offset":
        envelope["source_binding"]["source_media_timeline_offset_ms"] = OFFSET + 1
    elif drift == "srt":
        envelope["source_binding"]["current_srt_sha256"] = "f" * 64
    elif drift == "artifact":
        envelope["artifact_bindings"]["observation_sha256"] = "f" * 64
    elif drift == "model":
        envelope["source_binding"]["model"] = "other-model"
    elif drift == "stdout":
        files["paths"]["stdout"].write_text(
            files["paths"]["stdout"].read_text().replace('"status": "SUCCESS"', '"status": "FAILED"'),
            encoding="utf-8",
        )
        envelope["artifact_bindings"]["stdout_sha256"] = _sha(files["paths"]["stdout"])
        execution = json.loads(files["paths"]["execution"].read_text())
        execution["stdout_sha256"] = _sha(files["paths"]["stdout"])
        _write_json(files["paths"]["execution"], execution)
        envelope["artifact_bindings"]["execution_sha256"] = _sha(files["paths"]["execution"])
    elif drift == "exposure":
        request = dict(
            files["paths"]["request"].read_text()
            and json.loads(files["paths"]["request"].read_text())
        )
        request["candidate_exposure"] = "candidate shown"
        request_sha = _write_json(files["paths"]["request"], request)
        envelope["artifact_bindings"]["request_sha256"] = request_sha
        execution = json.loads(files["paths"]["execution"].read_text())
        execution["request_sha256"] = request_sha
        _write_json(files["paths"]["execution"], execution)
        envelope["artifact_bindings"]["execution_sha256"] = _sha(files["paths"]["execution"])
    request = build_context_adjudication_request(
        SOURCE,
        {
            "cue_index": 1,
            "suspect": "之夜",
            "suggestion": "职业",
            "proposed_full_cue": "哈哈哈哈哈呵魔女职业",
            "repair_class": "phonetic",
        },
        clip_context=clip,
        source_media_timeline_offset_ms=OFFSET,
    )
    validated, reason = validate_range_acoustic_observation(SOURCE, request, envelope)
    assert validated is None
    assert reason and reason.startswith("TONE_CONTEXT_")
