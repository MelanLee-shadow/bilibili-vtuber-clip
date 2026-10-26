import hashlib
import json
from pathlib import Path

import pytest

from src.autoslice.acoustic_witness_adjudication import build_witness_request
from src.autoslice.acoustic_witness_protocol import BLIND_PINYIN_PROTOCOL, CANDIDATE_BLIND_TRANSCRIPT_PROTOCOL
from src.autoslice.final_review_auditor import (
    adjudicate_context_finding, adjudicate_exact_release_findings,
    build_context_adjudication_request, _rebuild_candidate_after_neither,
)
from src.autoslice.independent_context_adjudication import (
    adapt_secondary_audio_context_observation, adapt_secondary_audio_evidence,
    adapt_range_acoustic_context_observation,
    independent_asr_candidates,
    validate_range_acoustic_observation,
)
import src.autoslice.independent_context_adjudication as independent_module


SOURCE = "1\n00:00:01,000 --> 00:00:04,000\n错误原句\n"
FINDING = {"cue_index": 1, "suspect": "错误", "suggestion": "误认",
           "proposed_full_cue": "误认原句", "repair_class": "phonetic"}


def _srt(*texts):
    return "\n\n".join(
        f"{index}\n00:00:{index * 5:02d},000 --> 00:00:{index * 5 + 4:02d},000\n{text}"
        for index, text in enumerate(texts, start=1)
    ) + "\n"


def _native(target, provider, text):
    return {
        "schema_version": "subtitle-span-acoustic-witness.v1",
        "witness_protocol": CANDIDATE_BLIND_TRANSCRIPT_PROTOCOL,
        "request_sha256": target["request_sha256"], "status": "OBSERVED",
        "target_audible": True, "exact_transcript": text,
        "candidate_exposure": "none", "authority": "EVIDENCE_ONLY",
        "mutation_authorized": False, "provider": provider, "model": provider + "-model",
        "source_media_sha256": "a" * 64,
        "audio_start_ms": target["matched_start_ms"] + target["source_media_timeline_offset_ms"],
        "audio_end_ms": target["matched_end_ms"] + target["source_media_timeline_offset_ms"],
    }


def _secondary(target, provider, text="然后他当时就是在做梦"):
    start = target["matched_start_ms"] + target["source_media_timeline_offset_ms"]
    end = target["matched_end_ms"] + target["source_media_timeline_offset_ms"]
    segment = {
        "start_ms": start - 80,
        "end_ms": end + 80,
        "text": text,
        "speaker": "S00",
    }
    return {
        "schema_version": "secondary-audio-witness-evidence.v1",
        "status": "OBSERVED",
        "authority": "EVIDENCE_ONLY",
        "mutation_authorized": False,
        "candidate_exposure": "none",
        "witness_request_sha256": target["request_sha256"],
        "witness_protocol": BLIND_PINYIN_PROTOCOL,
        "provider": provider,
        "model": f"{provider}-model",
        "source_media_sha256": "a" * 64,
        "source_crop_sha256": "b" * 64,
        "secondary_audio_sha256": "c" * 64,
        "provider_input_audio_sha256": "c" * 64,
        "provider_response_sha256": ("d" if provider == "mai" else "e") * 64,
        "receipt_sha256": ("f" if provider == "mai" else "0") * 64,
        "timeline_binding": {
            "schema_version": "subtitle-audio-timeline-binding.v1",
            "source_media_timeline_offset_ms": target["source_media_timeline_offset_ms"],
            "delivery_local": {
                "target_start_ms": target["matched_start_ms"],
                "target_end_ms": target["matched_end_ms"],
                "context_start_ms": target["context_start_ms"],
                "context_end_ms": target["context_end_ms"],
            },
            "source_media": {
                "target_start_ms": start,
                "target_end_ms": end,
                "crop_start_ms": start - 500,
                "crop_end_ms": end + 500,
            },
        },
        "native_segments": [segment],
        "target_overlap_segments": [dict(segment)],
    }


def _write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _range_artifacts(
    tmp_path,
    source,
    current_request,
    *,
    matched_start_ms=5000,
    matched_end_ms=34000,
    provider_count_mismatch=False,
    prompt_text="candidate-free witness prompt",
):
    """Create a receipt-shaped broad witness with real file bindings."""
    broad = build_witness_request({
        "schema_version": "subtitle-span-acoustic-check-request.v1",
        "kind": "subtitle_span_acoustic_check",
        "evidence_id": "range-test-cues1-6",
        "cue_indexes": [1, 2, 3, 4, 5, 6],
        "matched_start_ms": matched_start_ms,
        "matched_end_ms": matched_end_ms,
        "context_start_ms": matched_start_ms,
        "context_end_ms": matched_end_ms,
        "source_media_timeline_offset_ms": 100,
    })
    source_path = tmp_path / "source.mp4"
    srt_path = tmp_path / "reviewed.srt"
    audio_path = tmp_path / "input.mp4"
    prompt_path = tmp_path / "prompt.md"
    response_path = tmp_path / "response.json"
    source_path.write_bytes(b"range-source")
    srt_path.write_text(source, encoding="utf-8")
    audio_path.write_bytes(b"range-audio")
    prompt_path.write_text(prompt_text, encoding="utf-8")
    raw_response = {
        "schema_version": "subtitle-span-acoustic-witness.v1",
        "status": "OBSERVED",
        "target_audible": True,
        "heard_pinyin": "ran ge zhen",
        "uncertain_positions": [1],
        "syllable_count": 3,
        "confidence": 0.8,
        "reason": "range evidence",
    }
    _write_json(response_path, raw_response)
    source_sha = hashlib.sha256(source_path.read_bytes()).hexdigest()
    srt_sha = hashlib.sha256(srt_path.read_bytes()).hexdigest()
    audio_sha = hashlib.sha256(audio_path.read_bytes()).hexdigest()
    prompt_sha = hashlib.sha256(prompt_path.read_bytes()).hexdigest()
    response_sha = hashlib.sha256(response_path.read_bytes()).hexdigest()
    timeline = {
        "schema_version": "subtitle-audio-timeline-binding.v1",
        "source_media_timeline_offset_ms": 100,
        "delivery_local": {
            "target_start_ms": matched_start_ms,
            "target_end_ms": matched_end_ms,
            "context_start_ms": matched_start_ms,
            "context_end_ms": matched_end_ms,
        },
        "source_media": {
            "target_start_ms": matched_start_ms + 100,
            "target_end_ms": matched_end_ms + 100,
            "crop_start_ms": matched_start_ms,
            "crop_end_ms": matched_end_ms + 200,
        },
    }
    verdict = {
        **raw_response,
        "witness_protocol": BLIND_PINYIN_PROTOCOL,
        "request_sha256": broad["request_sha256"],
        "source_media_sha256": source_sha,
        "audio_clip_sha256": audio_sha,
        "prompt_sha256": prompt_sha,
        "response_sha256": response_sha,
        "provider": "gemini_api",
        "model": "gemini-test",
        "audio_start_ms": matched_start_ms,
        "audio_end_ms": matched_end_ms + 200,
        "timeline_binding": timeline,
    }
    if provider_count_mismatch:
        verdict["self_count_mismatch"] = True
        verdict["syllable_count"] = raw_response["syllable_count"] + 1
    verdict_path = tmp_path / "verdict.json"
    manifest_path = tmp_path / "verdict.manifest.json"
    input_path = tmp_path / "INPUT.json"
    result_path = tmp_path / "RESULT.json"
    _write_json(verdict_path, verdict)
    manifest = {
        "schema_version": "entity-audio-verdict-manifest.v1",
        "request_sha256": broad["request_sha256"],
        "request_payload_sha256": independent_module._digest(broad),
        "source_media": str(source_path),
        "source_media_sha256": source_sha,
        "audio_clip": str(audio_path),
        "audio_clip_sha256": audio_sha,
        "prompt_sha256": prompt_sha,
        "response_sha256": response_sha,
        "timeline_binding": timeline,
        "verdict": verdict,
    }
    _write_json(manifest_path, manifest)
    source_window = {
        "target_start_ms": matched_start_ms + 100,
        "target_end_ms": matched_end_ms + 100,
        "crop_start_ms": matched_start_ms,
        "crop_end_ms": matched_end_ms + 200,
    }
    input_doc = {
        "schema_version": "range-test-input.v1",
        "candidate_id": "candidate-range-test",
        "candidate_blind": True,
        "candidate_text_in_request": False,
        "provider": "gemini_api",
        "model": "gemini-test",
        "source_media_path": str(source_path),
        "source_media_sha256": source_sha,
        "reviewed_srt_path": str(srt_path),
        "reviewed_srt_sha256": srt_sha,
        "source_media_timeline_offset_ms": 100,
        "delivery_window": {
            "cue_indexes": [1, 2, 3, 4, 5, 6],
            "start_ms": matched_start_ms,
            "end_ms": matched_end_ms,
        },
        "source_window": source_window,
        "witness_request": broad,
        "witness_request_sha256": broad["request_sha256"],
    }
    result_doc = {
        "schema_version": "range-test-result.v1",
        "status": "OBSERVED",
        "srt_mutation": False,
        "publication": "NOT_REQUESTED",
        "provider": "gemini_api",
        "model": "gemini-test",
        "request_sha256": broad["request_sha256"],
        "candidate_blind": True,
        "source_media_sha256": source_sha,
        "source_media_timeline_offset_ms": 100,
        "source_window": source_window,
        "verdict_path": str(verdict_path),
        "verdict": verdict,
    }
    _write_json(input_path, input_doc)
    _write_json(result_path, result_doc)
    observation = {
        "input_path": str(input_path),
        "result_path": str(result_path),
        "verdict_path": str(verdict_path),
        "manifest_path": str(manifest_path),
        "response_path": str(response_path),
        "prompt_path": str(prompt_path),
        "source_media_path": str(source_path),
        "reviewed_srt_path": str(srt_path),
        "audio_clip_path": str(audio_path),
    }
    return observation, broad, source_window


def _range_clip_context():
    return {
        "schema_version": "lidousha-clip-context.v1",
        "candidate_id": "candidate-range-test",
        "pieces": [
            {
                "source_media_sha256": hashlib.sha256(b"range-source").hexdigest()
            }
        ],
        "whole_clip_draft_srt_sha256": "unused-in-test",
        "context_sha256": "unused-in-test",
        "mutation_authorized": False,
    }


def _segmented_range_artifacts(tmp_path, *, audio_observations=None):
    source = _srt("第一句话", "当前句子", "第三句话")
    finding = {"cue_index": 2, "suspect": "当前", "suggestion": "目前",
               "proposed_full_cue": "目前句子", "repair_class": "phonetic"}
    clip = _range_clip_context()
    narrow = build_context_adjudication_request(
        source, finding, clip_context=clip, source_media_timeline_offset_ms=100,
    )
    roles = ("segmented_input", "execution", "raw_response", "provider_stdout",
             "prompt", "audio_clip", "source_media", "reviewed_srt")
    paths = {role: tmp_path / role for role in roles}
    for role, value in (("source_media", b"range-source"), ("audio_clip", b"real-crop"),
                        ("prompt", b"Blind dictation, target segments A and B only"),
                        ("reviewed_srt", source.encode())):
        paths[role].write_bytes(value)
    def record(role):
        return {"path": str(paths[role]), "sha256": hashlib.sha256(paths[role].read_bytes()).hexdigest()}
    input_doc = {"candidate_id": clip["candidate_id"], "provider": "agy", "model": "frozen-model",
                 "candidate_blind": True, "candidate_text_in_provider_prompt": False,
                 "srt_mutation": False, "publication": "NOT_REQUESTED",
                 "cue_indexes": [1, 2, 3], "delivery_target_ms": [5000, 19000],
                 "source_media_timeline_offset_ms": 100, "source_target_ms": [5100, 19100],
                 "source_crop_ms": [5000, 19200], "delivery_crop_ms": [4900, 19100],
                 "target_local_ms": [100, 14100], "segments_local_ms": {"A": [100, 7000], "B": [7000, 14100]},
                 **{role: record(role) for role in ("source_media", "audio_clip", "reviewed_srt", "prompt")}}
    raw = {"schema_version": "original-provider-segment-response.v1", "status": "OBSERVED",
           "target_audible": True, "segments": [
               {"segment": "A", "start_ms": 100, "end_ms": 7000, "transcript": "实际上半句"},
               {"segment": "B", "start_ms": 7000, "end_ms": 14100, "transcript": "实际下半句"}],
           "full_transcript": "实际上半句实际下半句"}
    if audio_observations is not None:
        raw["audio_observations"] = audio_observations
    _write_json(paths["raw_response"], raw)
    paths["provider_stdout"].write_text("## Observation saved\nSee the structured response file.\n")
    execution = {"status": "COMPLETED", "returncode": 0, "provider": "agy", "model": "frozen-model",
                 "srt_mutation": False, "publication": "NOT_REQUESTED",
                 "response": record("raw_response"), "stdout": record("provider_stdout")}
    _write_json(paths["segmented_input"], input_doc)
    _write_json(paths["execution"], execution)
    bindings = {key: value for role in roles for key, value in (
        (f"{role}_path", str(paths[role])),
        (f"{role}_sha256", hashlib.sha256(paths[role].read_bytes()).hexdigest()))}
    return source, finding, clip, narrow, {"artifact_bindings": bindings}, paths


def test_saved_segmented_transcript_reaches_normal_cpa_as_context_only(tmp_path):
    source, finding, clip, narrow, observation, paths = _segmented_range_artifacts(tmp_path)
    before = {role: path.read_bytes() for role, path in paths.items()}
    request = build_context_adjudication_request(
        source, {**finding, "_prior_range_acoustic_observations": [observation]},
        clip_context=clip, source_media_timeline_offset_ms=100,
    )
    row, = request["context_only_range_acoustic_observations"]
    assert row["verdict"]["exact_transcript"] == "实际上半句实际下半句"
    assert row["verdict"]["original_provider_response"]["schema_version"] == "original-provider-segment-response.v1"
    assert row["transcript_scope"] == "RANGE_WITHOUT_SINGLE_CUE_TIMESTAMPS"
    assert row["mutation_authorized"] is False
    assert "witness_request" not in row
    assert "heard_pinyin" not in row["verdict"]
    assert build_witness_request(request) == build_witness_request(narrow)
    assert {role: path.read_bytes() for role, path in paths.items()} == before


@pytest.mark.parametrize("start,accepted", [(-50, True), (-200, False)])
def test_complete_range_can_include_available_preclip_audio_only(tmp_path, start, accepted):
    source, _finding, _clip, narrow, observation, paths = _segmented_range_artifacts(tmp_path)
    doc = json.loads(paths["segmented_input"].read_text())
    doc.update(delivery_target_ms=[start, 19000], source_target_ms=[start + 100, 19100],
               source_crop_ms=[start + 100, 19200], delivery_crop_ms=[start, 19100],
               target_local_ms=[0, 19000 - start],
               segments_local_ms={"A": [0, 7000], "B": [7000, 19000 - start]})
    raw = json.loads(paths["raw_response"].read_text())
    raw["segments"][0].update(start_ms=0, end_ms=7000)
    raw["segments"][1].update(start_ms=7000, end_ms=19000 - start)
    bindings = observation["artifact_bindings"]
    bindings["segmented_input_sha256"] = _write_json(paths["segmented_input"], doc)
    bindings["raw_response_sha256"] = _write_json(paths["raw_response"], raw)
    execution = json.loads(paths["execution"].read_text())
    execution["response"]["sha256"] = bindings["raw_response_sha256"]
    bindings["execution_sha256"] = _write_json(paths["execution"], execution)
    observed, reason = validate_range_acoustic_observation(source, narrow, observation)
    if accepted:
        assert reason is None and observed["evidence_scope"] == "RANGE_CONTEXT_ONLY"
        assert observed["range"]["matched_start_ms"] == -50
        assert observed["mutation_authorized"] is False
        assert all(row["coverage"] == "FULL" for row in observed["cue_texts"])
    else:
        assert observed is None and reason == "SEGMENTED_RANGE_GEOMETRY_INVALID"


def test_neither_uses_bound_range_asr_before_last_resort_phonemes(tmp_path):
    source, finding, clip, _narrow, observation, paths = _segmented_range_artifacts(tmp_path)
    request = build_context_adjudication_request(
        source, {**finding, "_prior_range_acoustic_observations": [observation]},
        clip_context=clip, source_media_timeline_offset_ms=100,
    )
    before = {role: path.read_bytes() for role, path in paths.items()}
    prompts = []

    def propose(prompt):
        prompts.append(prompt)
        assert "实际上半句实际下半句" in prompt
        assert "被拒提案" in prompt and "目前句子" in prompt
        assert "不能再输出被拒提案" in prompt
        assert "绑定聊天" in prompt
        assert "最后恢复手段" not in prompt
        return json.dumps({"status": "PROPOSED", "proposed_cue": "实际句子",
                           "reason": "实际整块听写的有界候选；仍待盲听和裁决"})

    rebuilt, audit = _rebuild_candidate_after_neither(
        finding=finding, request=request, witness={}, llm_call=propose,
        asr_attempts={"routes_exhausted": False}, structured_chat="绑定聊天",
    )
    assert len(prompts) == 1
    assert rebuilt["proposed_full_cue"] == "实际句子"
    assert rebuilt["candidate_provenance"]["mutation_authorized"] is False
    assert audit["mutation_authorized"] is False
    assert audit["basis"] == "VALIDATED_RANGE_ASR_THIRD_CANDIDATE"
    assert audit["range_acoustic_context_authority"] == "EVIDENCE_ONLY"
    assert {role: path.read_bytes() for role, path in paths.items()} == before


def test_neither_range_rebuild_rejects_drift_before_cpa(tmp_path):
    source, finding, clip, _narrow, observation, paths = _segmented_range_artifacts(tmp_path)
    request = build_context_adjudication_request(
        source, {**finding, "_prior_range_acoustic_observations": [observation]},
        clip_context=clip, source_media_timeline_offset_ms=100,
    )
    paths["raw_response"].write_text("changed")
    prompts = []
    rebuilt, audit = _rebuild_candidate_after_neither(
        finding=finding, request=request, witness={},
        llm_call=lambda prompt: prompts.append(prompt),
    )
    assert rebuilt is None and prompts == []
    assert audit["reason_code"] == "PHONEME_REBUILD_REQUIRES_EXHAUSTED_ASR_AND_ACTUAL_PHONEMES"


def test_neither_range_rebuild_cannot_recycle_rejected_candidate(tmp_path):
    source, finding, clip, _narrow, observation, _paths = _segmented_range_artifacts(tmp_path)
    request = build_context_adjudication_request(
        source, {**finding, "_prior_range_acoustic_observations": [observation]},
        clip_context=clip, source_media_timeline_offset_ms=100,
    )
    rebuilt, audit = _rebuild_candidate_after_neither(
        finding=finding, request=request, witness={},
        llm_call=lambda prompt: json.dumps({"status": "PROPOSED",
                                           "proposed_cue": "目前句子", "reason": "旧提案"}),
    )
    assert rebuilt is None
    assert audit["range_candidate_proposal"]["status"] == "INVALID"
    assert audit["mutation_authorized"] is False


@pytest.mark.parametrize("accept_proposal", [True, False])
def test_configured_exact_listener_does_not_skip_bound_range_cpa_proposal(
    tmp_path, accept_proposal,
):
    source, finding, clip, _narrow, observation, paths = _segmented_range_artifacts(tmp_path)
    before = {role: path.read_bytes() for role, path in paths.items()}
    prompts = []

    def verify(target):
        return {
            "schema_version": "subtitle-span-acoustic-witness.v1",
            "witness_protocol": BLIND_PINYIN_PROTOCOL,
            "request_sha256": target["request_sha256"], "status": "OBSERVED",
            "target_audible": True, "heard_pinyin": "shi ji ju zi",
            "confidence": .9, "syllable_count": 4, "uncertain_positions": [],
        }

    def extra_listener(_target):
        pytest.fail("bound range proposal must precede another exact listener")

    def judge(prompt):
        prompts.append(prompt)
        if "# 字幕缺失候选重建" in prompt:
            assert "实际上半句实际下半句" in prompt
            return json.dumps({"status": "PROPOSED", "proposed_cue": "实际句子",
                               "reason": "绑定的整句观察"})
        if "实际句子" in prompt:
            return _answer("PROPOSED" if accept_proposal else "NEITHER", supported=True)
        return _answer("NEITHER", supported=True)

    output, audit = adjudicate_context_finding(
        source, {**finding, "_prior_range_acoustic_observations": [observation]},
        entity_verifier=verify, judge_llm_call=judge,
        exact_source_transcript_provider=extra_listener,
        clip_context=clip, source_media_timeline_offset_ms=100,
    )
    assert sum("# 字幕缺失候选重建" in prompt for prompt in prompts) == 1
    assert audit["proposal_rebuild"]["basis"] == "VALIDATED_RANGE_ASR_THIRD_CANDIDATE"
    assert audit["proposal_rebuild"]["mutation_authorized"] is False
    assert audit["repaired"] is accept_proposal
    if accept_proposal:
        assert "实际句子" in output and "当前句子" not in output
        assert audit["mutation_authority"]["status"] == "PASS"
    else:
        assert output == source
        assert audit["policy_branch"] == "PROPOSAL_REBUILD_EXHAUSTED"
    assert {role: path.read_bytes() for role, path in paths.items()} == before


def test_exact_listener_fallback_does_not_repeat_the_same_range_proposal(tmp_path):
    source, finding, clip, _narrow, observation, _paths = _segmented_range_artifacts(tmp_path)
    request = build_context_adjudication_request(
        source, {**finding, "_prior_range_acoustic_observations": [observation]},
        clip_context=clip, source_media_timeline_offset_ms=100,
    )
    prior_audit = {"status": "UNRESOLVED", "basis": "VALIDATED_RANGE_ASR_THIRD_CANDIDATE"}
    rebuilt, audit = _rebuild_candidate_after_neither(
        finding=finding, request=request, witness={},
        llm_call=lambda _prompt: pytest.fail("same range proposal cannot repeat on fallback"),
        range_proposal_audit=prior_audit,
    )
    assert rebuilt is None
    assert audit["range_candidate_proposal"] == prior_audit
    assert audit["reason_code"] == "PHONEME_REBUILD_REQUIRES_EXHAUSTED_ASR_AND_ACTUAL_PHONEMES"


@pytest.mark.parametrize("drift", ["raw", "stdout", "parse_error", "model", "geometry", "prompt_exposure", "current_source"])
def test_saved_segmented_transcript_rejects_original_or_current_drift(tmp_path, drift):
    source, _finding, _clip, narrow, observation, paths = _segmented_range_artifacts(tmp_path)
    bindings = observation["artifact_bindings"]
    if drift == "current_source":
        narrow["clip_context_binding"]["source_media_sha256s"] = ["a" * 64]
    elif drift == "raw":
        raw = json.loads(paths["raw_response"].read_text())
        raw["full_transcript"] = "invented replacement"
        bindings["raw_response_sha256"] = _write_json(paths["raw_response"], raw)
    elif drift == "stdout":
        paths["provider_stdout"].write_text("Different terminal output")
        bindings["provider_stdout_sha256"] = hashlib.sha256(paths["provider_stdout"].read_bytes()).hexdigest()
    elif drift in {"model", "parse_error"}:
        execution = json.loads(paths["execution"].read_text())
        if drift == "model":
            execution["model"] = "new-model"
        else:
            execution["response_parse_error"] = "JSONDecodeError"
        bindings["execution_sha256"] = _write_json(paths["execution"], execution)
    else:
        input_doc = json.loads(paths["segmented_input"].read_text())
        if drift == "geometry":
            input_doc["source_target_ms"][0] += 1
        else:
            paths["prompt"].write_text("Blind dictation: 当前句子")
            digest = hashlib.sha256(paths["prompt"].read_bytes()).hexdigest()
            bindings["prompt_sha256"] = digest
            input_doc["prompt"]["sha256"] = digest
        bindings["segmented_input_sha256"] = _write_json(paths["segmented_input"], input_doc)
    result, reason = validate_range_acoustic_observation(source, narrow, observation)
    assert result is None and reason


def _native_range_artifacts(tmp_path, provider, *, word_timing=False, unlocated_status=None,
                            word_offsets=(500, 5100, 10100)):
    from src.autoslice.local_asr_target_evidence import exact_target_evidence, MODELS
    from src.autoslice.native_context_witness import _native_transcript_witness, _prepare_request

    source = _srt("第一句", "当前句", "第三句")
    finding = {"cue_index": 2, "suspect": "当前", "suggestion": "目前",
               "proposed_full_cue": "目前句", "repair_class": "phonetic"}
    clip = _range_clip_context()
    narrow = build_context_adjudication_request(
        source, finding, clip_context=clip, source_media_timeline_offset_ms=100,
    )
    broad = build_witness_request({
        "evidence_id": "saved-native-range", "cue_indexes": [1, 2, 3],
        "matched_start_ms": 5000, "matched_end_ms": 19000,
        "context_start_ms": 5000, "context_end_ms": 19000,
        "source_media_timeline_offset_ms": 100,
    })
    paths = {role: tmp_path / role for role in (
        "input", "native_envelope", "provider_receipt", "audio_clip", "source_media", "reviewed_srt", "raw_response",
    )}
    paths["source_media"].write_bytes(b"range-source")
    paths["reviewed_srt"].write_text(source)
    audio = b"actual-native-range-crop"
    paths["audio_clip"].write_bytes(audio)
    source_sha = hashlib.sha256(b"range-source").hexdigest()
    raw = {"text": "整句范围听写", "segments": []}
    metadata = {
        "provider": provider, "model": MODELS[provider], "status": "OK",
        "candidate_exposure": "none", "input_audio_sha256": hashlib.sha256(audio).hexdigest(),
        "response_sha256": hashlib.sha256(json.dumps(raw).encode()).hexdigest(), "raw_response": raw,
        "native_segments": [{"start_ms": 0, "end_ms": 14000, "text": "整句范围听写"}],
    }
    if unlocated_status:
        metadata.update(
            status=unlocated_status, evidence_scope="exact_cue",
            native_segments=[], one_track_srt_eligible=False,
            native_timeline={"one_track_srt_eligible": False, "valid": False},
        )
    if word_timing:
        from src.autoslice.mai_transcription import _parse_evidence_response
        raw = {"durationMilliseconds": 14000, "combinedPhrases": [{"text": "甲乙丙"}],
               "phrases": [{"offsetMilliseconds": 0, "durationMilliseconds": 14000,
                            "text": "甲乙丙", "words": [
                                {"text": text, "offsetMilliseconds": start, "durationMilliseconds": 100}
                                for text, start in zip("甲乙丙", word_offsets)]}]}
        segments, _, _ = _parse_evidence_response(raw, duration_ms=14000)
        metadata.update(raw_response=raw, native_segments=segments,
                        response_sha256=hashlib.sha256(json.dumps(raw).encode()).hexdigest())
    binding = {key: metadata[key] for key in ("provider", "model", "input_audio_sha256")}
    binding.update(contract="candidate-free-native-secondary-v1", duration_ms=14000, candidate_exposure="none")
    receipt = {"binding": binding, "evidence": metadata,
               "evidence_sha256": independent_module._digest(metadata)}
    _write_json(paths["provider_receipt"], receipt)
    _write_json(paths["raw_response"], raw)
    native = exact_target_evidence(
        metadata, audio=audio, source_sha256=source_sha, start_ms=5100, end_ms=19100,
        prefer_provider_text=bool(unlocated_status),
    )
    prepared = _prepare_request(broad)
    witness = _native_transcript_witness(
        request=broad, prepared=prepared, native_observation=native, source_sha256=source_sha, provider=provider,
    )
    envelope = {"schema_version": "native-context-witness-envelope.v1", "request": broad,
                "request_sha256": broad["request_sha256"], "source_media_sha256": source_sha,
                "audio_start_ms": 5100, "audio_end_ms": 19100,
                "native_observation": native, "witness": witness}
    _write_json(paths["native_envelope"], envelope)
    _write_json(paths["input"], {
        "candidate_id": clip["candidate_id"], "source_media_path": str(paths["source_media"]),
        "source_media_sha256": source_sha, "reviewed_srt_path": str(paths["reviewed_srt"]),
        "reviewed_srt_sha256": hashlib.sha256(source.encode()).hexdigest(),
        "witness_request": broad, "witness_request_sha256": broad["request_sha256"],
        "candidate_blind": True, "candidate_text_in_request": False,
        "srt_mutation": False, "publication": "NOT_REQUESTED", "source_media_timeline_offset_ms": 100,
        "delivery_window": {"cue_indexes": [1, 2, 3], "start_ms": 5000, "end_ms": 19000},
    })
    bindings = {}
    for role, path in paths.items():
        bindings[f"{role}_path"] = str(path)
        bindings[f"{role}_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    return source, finding, clip, narrow, {"artifact_bindings": bindings}, paths


@pytest.mark.parametrize("provider", ["mai", "moss"])
@pytest.mark.parametrize("drift", [None, "srt", "text_only", "geometry", "audio", "raw", "window", "fractional", "fractional_mismatch", "candidate", "authority"])
def test_mapped_source_native_receipt_is_context_and_rejects_drift(tmp_path, monkeypatch, provider, drift):
    from src.autoslice import producer_nested_acoustic

    source, finding, clip, narrow, observation, paths = _native_range_artifacts(tmp_path, provider)
    clip["context_sha256"] = hashlib.sha256(json.dumps(clip, sort_keys=True).encode()).hexdigest()
    outer = tmp_path / "outer_media"
    outer.write_bytes(b"different outer recording")
    observation.update(schema_version="mapped-source-audio-context.v1", authority="EVIDENCE_ONLY",
                       mutation_authorized=False, candidate_id=clip["candidate_id"],
                       clip_context_sha256=clip["context_sha256"],
                       descriptor={"target_interval_ms": [5000, 19000], "target_cue_indexes": [1, 2, 3]})
    observation["artifact_bindings"].update(
        outer_media_path=str(outer), outer_media_sha256=hashlib.sha256(outer.read_bytes()).hexdigest(),
    )
    # Mapping measurement has its own tests and the real native acceptance.
    # Here exercise the native receipt/CPA ingress across different media.
    window = [5101, 19100] if drift == "window" else [5100, 19100]
    if drift == "fractional":
        window = [5099.875, 19099.875]
    elif drift == "fractional_mismatch":
        window = [5099.49, 19099.875]
    monkeypatch.setattr(producer_nested_acoustic, "_descriptor_binding", lambda **kwargs: (
        {"independent_source_media": {"sha256": observation["artifact_bindings"]["source_media_sha256"]},
         "interval_ms": [5000, 19000], "mapping_receipt": {"test_only": True}},
        {"source_window_ms": window},
    ))
    if drift == "srt":
        source = source.replace("当前句", "别的句")
    elif drift == "text_only":
        source = source.replace("第一句", "修好的邻句")
    elif drift == "geometry":
        source = source.replace("00:00:05,000", "00:00:05,001")
    elif drift in {"audio", "raw"}:
        paths[{"audio": "audio_clip", "raw": "raw_response"}[drift]].write_bytes(b"changed artifact")
    elif drift == "candidate":
        observation["candidate_id"] = "other-candidate"
    elif drift == "authority":
        observation["mutation_authorized"] = True
    request = build_context_adjudication_request(
        source, {**finding, "_prior_range_acoustic_observations": [observation]},
        clip_context=clip, source_media_timeline_offset_ms=100,
    ) if drift != "srt" else narrow
    if drift == "srt":
        validated, _reason = validate_range_acoustic_observation(source, request, observation)
        assert validated is None
    elif drift and drift not in {"text_only", "fractional"}:
        assert "context_only_range_acoustic_observations" not in request
    else:
        row = request["context_only_range_acoustic_observations"][0]
        assert row["evidence_scope"] == "INDEPENDENT_SOURCE_CONTEXT_ONLY"
        assert row["same_cue_direct_transcript_authority"] is False
        assert row["speaker_role_authority"] is False
        assert row["source_transcript"] == "整句范围听写"
        assert row["source_window_ms"] == window
        assert row["native_source_window_ms"] == [5100, 19100]
        if drift == "text_only":
            assert row["context_only_text_carryover"] is True
            assert row["artifact_bindings"]["reviewed_srt_sha256"] == observation["artifact_bindings"]["reviewed_srt_sha256"]
            assert row["original_reviewed_srt_sha256"] != row["current_reviewed_srt_sha256"]
        assert "native_word_overlap_evidence" not in row
        assert row["witness_request_sha256"] != build_witness_request(request)["request_sha256"]
        from src.autoslice import final_review_auditor
        from src.autoslice.acoustic_witness_adjudication import judge_word_choice

        def forbidden_proposal_call(_prompt):
            raise AssertionError("watched-source speech must not become target ASR")

        assert final_review_auditor._rebuild_candidate_from_range_observations(
            finding=finding, request=request, llm_call=forbidden_proposal_call,
        ) == (None, {})
        prompts = []
        judge_word_choice(
            llm_call=lambda prompt: prompts.append(prompt) or json.dumps({"choice": "CURRENT", "reason": "context only"}),
            check_request=request, witness={"status": "UNAVAILABLE"},
        )
        assert "另一份独立原视频" in prompts[0]
        assert "不能当成主播目标句的原音" in prompts[0]


@pytest.mark.parametrize("provider", ["mai", "moss"])
def test_saved_native_range_enters_normal_cpa_context_without_narrow_rebinding(tmp_path, provider):
    source, finding, clip, narrow, observation, paths = _native_range_artifacts(tmp_path, provider)
    before = {role: path.read_bytes() for role, path in paths.items()}
    request = build_context_adjudication_request(
        source, {**finding, "_prior_range_acoustic_observations": [observation]},
        clip_context=clip, source_media_timeline_offset_ms=100,
    )
    rows = request["context_only_range_acoustic_observations"]
    assert len(rows) == 1 and rows[0]["provider"] == provider
    assert rows[0]["verdict"]["exact_transcript"] == "整句范围听写"
    assert rows[0]["witness_request"]["cue_indexes"] == [1, 2, 3]
    assert rows[0]["evidence_scope"] == "RANGE_CONTEXT_ONLY"
    assert rows[0]["mutation_authorized"] is False
    assert build_witness_request(request) == build_witness_request(narrow)
    assert request["request_sha256"] != narrow["request_sha256"]
    assert {role: path.read_bytes() for role, path in paths.items()} == before
    persisted = build_context_adjudication_request(
        source, {**finding, "_prior_range_acoustic_observations": rows},
        clip_context=clip, source_media_timeline_offset_ms=100,
    )
    assert persisted == request  # a typed receipt remains replayable from its own file bindings
    assert "native_word_overlap_evidence" not in rows[0]


@pytest.mark.parametrize("provider", ["mai", "moss"])
@pytest.mark.parametrize("side", ["leading", "trailing", "both"])
def test_native_sealed_range_keeps_boundary_silence_explicit(tmp_path, provider, side):
    source, finding, clip, _narrow, observation, paths = _native_range_artifacts(tmp_path, provider)
    if side in {"leading", "both"}:
        source = source.replace("00:00:05,000 --> 00:00:09,000", "00:00:01,000 --> 00:00:04,000")
    if side in {"trailing", "both"}:
        source = source.replace("00:00:15,000 --> 00:00:19,000", "00:00:20,000 --> 00:00:24,000")
    paths["reviewed_srt"].write_text(source)
    inp = json.loads(paths["input"].read_text())
    inp["reviewed_srt_sha256"] = hashlib.sha256(source.encode()).hexdigest()
    _write_json(paths["input"], inp)
    for role in ("input", "reviewed_srt"):
        observation["artifact_bindings"][role + "_sha256"] = hashlib.sha256(paths[role].read_bytes()).hexdigest()
    before = {role: paths[role].read_bytes() for role in ("native_envelope", "provider_receipt", "audio_clip", "raw_response")}
    request = build_context_adjudication_request(
        source, {**finding, "_prior_range_acoustic_observations": [observation]},
        clip_context=clip, source_media_timeline_offset_ms=100,
    )
    row = request["context_only_range_acoustic_observations"][0]
    assert row["cue_texts"][1]["coverage"] == "FULL"
    assert row["evidence_scope"] == "RANGE_CONTEXT_ONLY" and row["mutation_authorized"] is False
    uncovered = [cue for cue in row["cue_texts"] if cue["coverage"] == "NONE"]
    assert len(uncovered) == (2 if side == "both" else 1)
    assert all(cue["covered_start_ms"] is None and cue["covered_end_ms"] is None
               and cue["boundary_gap_ms"] == 1000 for cue in uncovered)
    assert {role: paths[role].read_bytes() for role in before} == before
    assert independent_module._range_cue_rows(source, [1, 2, 3], 5000, 19000) is None
    current = source.replace("第一句", "新的第一句")
    current_request = build_context_adjudication_request(
        current, finding, clip_context=clip, source_media_timeline_offset_ms=100,
    )
    carried, reason = adapt_range_acoustic_context_observation(current, current_request, observation)
    assert carried is not None, reason
    assert carried["current_cue_texts"][1]["coverage"] == "FULL"
    assert carried["original_witness_request"] == row["witness_request"]


@pytest.mark.parametrize("start,end", [(11000, 19000), (5000, 13000), (9500, 13500)])
def test_sealed_range_boundary_gap_cannot_hide_missing_internal_cue(start, end):
    source = _srt("第一句", "当前句", "第三句")
    assert independent_module._range_cue_rows(
        source, [1, 2, 3], start, end, allow_boundary_gaps=True,
    ) is None


@pytest.mark.parametrize("indexes,start,end", [([2, 3], 9500, 19500), ([2], 9500, 14500)])
def test_native_range_allows_complete_cues_with_outer_text_free_margins(indexes, start, end):
    source = _srt("前句", "当前句", "第三句", "后句")
    rows = independent_module._range_cue_rows(source, indexes, start, end, allow_boundary_gaps=True)
    assert rows is not None and all(row["coverage"] == "FULL" for row in rows)
    assert independent_module._range_cue_rows(source, indexes, start, end) is None
    assert independent_module._range_cue_rows(source, indexes, 8500, end, allow_boundary_gaps=True) is None
    # The end cannot absorb any portion of the next undeclared subtitle.
    next_start = 15000 if indexes == [2] else 20000
    assert independent_module._range_cue_rows(source, indexes, start, next_start + 1, allow_boundary_gaps=True) is None


def test_native_range_carryover_allows_peripheral_text_change_without_rebinding(tmp_path):
    source, finding, clip, narrow, observation, paths = _native_range_artifacts(
        tmp_path, "mai",
    )
    old_validated, old_reason = validate_range_acoustic_observation(
        source, narrow, observation,
    )
    assert old_validated is not None, old_reason
    anchored_observation = {
        **observation,
        "current_cue_index": old_validated["current_cue_index"],
        "current_target": {
            "cue_index": old_validated["current_cue_index"],
            "start_ms": narrow["matched_start_ms"],
            "end_ms": narrow["matched_end_ms"],
        },
    }
    current_source = source.replace("第一句", "第一版").replace("第三句", "第三版")
    current_request = build_context_adjudication_request(
        current_source, finding, clip_context=clip, source_media_timeline_offset_ms=100,
    )
    before = {role: path.read_bytes() for role, path in paths.items()}

    carried, carry_reason = adapt_range_acoustic_context_observation(
        current_source, current_request, anchored_observation,
    )

    assert carried is not None, carry_reason
    assert carried["schema_version"] == "range-acoustic-context-carryover.v1"
    assert carried["status"] == "CONTEXT_ONLY"
    assert carried["authority"] == "EVIDENCE_ONLY"
    assert carried["mutation_authorized"] is False
    assert carried["range"] == {
        "cue_indexes": [1, 2, 3],
        "matched_start_ms": 5000,
        "matched_end_ms": 19000,
        "context_start_ms": 5000,
        "context_end_ms": 19000,
        "source_media_timeline_offset_ms": 100,
    }
    # The carryover keeps the original receipt's cue text as evidence; the
    # current SRT revision is bound separately by current_srt_sha256 and the
    # geometry comparison above.
    assert [row["text"] for row in carried["cue_texts"]] == [
        "第一句", "当前句", "第三句",
    ]
    assert [row["text"] for row in carried["current_cue_texts"]] == [
        "第一版", "当前句", "第三版",
    ]
    assert carried["original_range_observation"]["range"] == carried["range"]
    assert {role: path.read_bytes() for role, path in paths.items()} == before


def test_normal_context_builder_replays_native_range_after_text_only_repair(tmp_path):
    source, finding, clip, _narrow, observation, paths = _native_range_artifacts(tmp_path, "mai")
    current = source.replace("第一句", "修复后的第一句")
    before = {role: path.read_bytes() for role, path in paths.items()}
    request = build_context_adjudication_request(
        current, {**finding, "_prior_range_acoustic_observations": [observation]},
        clip_context=clip, source_media_timeline_offset_ms=100,
    )
    carried = request["context_only_range_acoustic_observations"][0]
    # 41:1489-1492: native ASR received no subtitle text, so a text-only
    # repair keeps the strict observation; current text is attached for CPA.
    assert carried["schema_version"] == "range-acoustic-context-observation.v1"
    assert carried["text_revision"] == "TEXT_ONLY_REVISION_AT_IDENTICAL_CUE_GRID"
    assert carried["current_srt_sha256"] == hashlib.sha256(current.encode()).hexdigest()
    assert carried["authority"] == "EVIDENCE_ONLY" and carried["mutation_authorized"] is False
    assert carried["current_cue_texts"][0]["text"] == "修复后的第一句"
    assert carried["cue_texts"][0]["text"] == "第一句"
    from src.autoslice.acoustic_witness_adjudication import _word_choice_prompt
    prompt, *_ = _word_choice_prompt(
        llm_call=lambda _prompt: pytest.fail("prompt construction must not dispatch"),
        check_request=request,
        witness={"status": "UNCERTAIN"},
    )
    assert "current_cue_texts" in prompt and "修复后的第一句" in prompt
    assert "不能把旧字幕当作当前文字或听写真值" in prompt
    assert {role: path.read_bytes() for role, path in paths.items()} == before
    paths["source_media"].write_bytes(b"different source")
    rejected = build_context_adjudication_request(
        current, {**finding, "_prior_range_acoustic_observations": [observation]},
        clip_context=clip, source_media_timeline_offset_ms=100,
    )
    assert "context_only_range_acoustic_observations" not in rejected


@pytest.mark.parametrize("drift", ["target", "range_geometry", "source"])
def test_native_range_carryover_rejects_target_geometry_and_source_drift(tmp_path, drift):
    source, finding, clip, narrow, observation, _paths = _native_range_artifacts(
        tmp_path, "moss",
    )
    old_validated, old_reason = validate_range_acoustic_observation(
        source, narrow, observation,
    )
    assert old_validated is not None, old_reason
    observation = {
        **observation,
        "current_cue_index": old_validated["current_cue_index"],
        "current_target": {
            "cue_index": old_validated["current_cue_index"],
            "start_ms": narrow["matched_start_ms"],
            "end_ms": narrow["matched_end_ms"],
        },
    }
    current_source = source.replace("第一句", "第一版")
    current_finding = finding
    current_request = build_context_adjudication_request(
        current_source, current_finding,
        clip_context=clip, source_media_timeline_offset_ms=100,
    )
    if drift == "target":
        current_finding = {
            **finding, "cue_index": 3, "suspect": "第三", "suggestion": "第四",
            "proposed_full_cue": "第四句",
        }
        current_request = build_context_adjudication_request(
            current_source, current_finding,
            clip_context=clip, source_media_timeline_offset_ms=100,
        )
    elif drift == "range_geometry":
        current_source = current_source.replace(
            "00:00:15,000 --> 00:00:19,000",
            "00:00:15,000 --> 00:00:19,100",
        )
        current_request = build_context_adjudication_request(
            current_source, current_finding,
            clip_context=clip, source_media_timeline_offset_ms=100,
        )
    else:
        current_request["clip_context_binding"] = {
            **current_request["clip_context_binding"],
            "source_media_sha256s": ["f" * 64],
        }

    carried, reason = adapt_range_acoustic_context_observation(
        current_source, current_request, observation,
    )

    assert carried is None
    assert reason is not None
    if drift == "target":
        assert reason == "RANGE_ACOUSTIC_CONTEXT_CURRENT_TARGET_DRIFT"
    elif drift == "range_geometry":
        assert reason == "RANGE_ACOUSTIC_CONTEXT_RANGE_GEOMETRY_DRIFT"
    else:
        assert "CURRENT_SOURCE_DRIFT" in reason


def test_moss_unlocated_full_crop_text_reaches_cpa_as_untimed_range_context(tmp_path):
    source, finding, clip, narrow, observation, paths = _native_range_artifacts(
        tmp_path, "moss", unlocated_status="TEXT_UNLOCATED",
    )
    before = {role: path.read_bytes() for role, path in paths.items()}
    request = build_context_adjudication_request(
        source, {**finding, "_prior_range_acoustic_observations": [observation]},
        clip_context=clip, source_media_timeline_offset_ms=100,
    )
    rows = request["context_only_range_acoustic_observations"]
    assert len(rows) == 1
    assert rows[0]["verdict"]["exact_transcript"] == "整句范围听写"
    assert rows[0]["transcript_scope"] == "RANGE_WITHOUT_SINGLE_CUE_TIMESTAMPS"
    assert rows[0]["mutation_authorized"] is False
    assert "native_word_overlap_evidence" not in rows[0]
    assert build_witness_request(request) == build_witness_request(narrow)
    assert {role: path.read_bytes() for role, path in paths.items()} == before


@pytest.mark.parametrize("provider,status", [("mai", "TEXT_UNLOCATED"), ("moss", "FAILED"), ("moss", "NO_SPEECH")])
def test_range_context_does_not_promote_other_provider_or_failure_status(tmp_path, provider, status):
    source, _finding, _clip, narrow, observation, _paths = _native_range_artifacts(
        tmp_path, provider, unlocated_status=status,
    )
    result, reason = independent_module.validate_native_range_context_observation(
        source, narrow, observation,
    )
    assert result is None
    assert reason == "NATIVE_RANGE_PROVIDER_ARTIFACT_DRIFT"


def test_native_word_timing_reaches_cpa_as_partial_range_evidence(tmp_path):
    source, finding, clip, narrow, observation, paths = _native_range_artifacts(
        tmp_path, "mai", word_timing=True,
    )
    before = {role: path.read_bytes() for role, path in paths.items()}
    request = build_context_adjudication_request(
        source, {**finding, "_prior_range_acoustic_observations": [observation]},
        clip_context=clip, source_media_timeline_offset_ms=100,
    )
    row = request["context_only_range_acoustic_observations"][0]
    timing = row["native_word_overlap_evidence"]
    assert timing["crop_local_to_delivery_offset_ms"] == 5000
    assert timing["coordinate_system"] == "delivery_clip_ms"
    assert timing["mutation_authorized"] is False
    assert timing["table"]["rows"][0]["text"] == "甲乙丙"
    assert timing["table"]["rows"][0]["cue_spans"] == [
        {"n": 1, "text": "甲", "start_ms": 5500, "end_ms": 5600},
        {"n": 2, "text": "乙", "start_ms": 10100, "end_ms": 10200},
        {"n": 3, "text": "丙", "start_ms": 15100, "end_ms": 15200},
    ]
    assert row["transcript_scope"] == "RANGE_WITH_NATIVE_WORD_OVERLAP"
    assert build_witness_request(request) == build_witness_request(narrow)
    assert request["proposed_cue"] == narrow["proposed_cue"]
    assert {role: path.read_bytes() for role, path in paths.items()} == before
    from src.autoslice.acoustic_witness_adjudication import _word_choice_prompt
    prompt, *_ = _word_choice_prompt(
        llm_call=lambda _: "", check_request=request, witness={"status": "UNAVAILABLE"},
    )
    assert "已换算为交付时钟" in prompt
    assert '"start_ms": 10100' in prompt
    assert "只是完整落入 cue 的局部听写观察" in prompt


def test_boundary_word_survives_raw_receipt_validation_and_normal_prompt(tmp_path):
    source, finding, clip, narrow, observation, paths = _native_range_artifacts(
        tmp_path, "mai", word_timing=True, word_offsets=(500, 4950, 10100),
    )
    before = {role: path.read_bytes() for role, path in paths.items()}
    request = build_context_adjudication_request(
        source, {**finding, "_prior_range_acoustic_observations": [observation]},
        clip_context=clip, source_media_timeline_offset_ms=100,
    )
    row = request["context_only_range_acoustic_observations"][0]
    table_row = row["native_word_overlap_evidence"]["table"]["rows"][0]
    assert table_row["unassigned_words"] == [{
        "text": "乙", "start_ms": 9950, "end_ms": 10050, "overlapping_cues": [2],
    }]
    assert all(span["text"] != "乙" for span in table_row["cue_spans"])
    assert row["authority"] == "EVIDENCE_ONLY"
    assert row["mutation_authorized"] is False
    from src.autoslice.acoustic_witness_adjudication import _word_choice_prompt
    from src.autoslice.missing_proposal_bootstrap import _range_audio_context
    prompt, *_ = _word_choice_prompt(
        llm_call=lambda _: "", check_request=request, witness={"status": "UNAVAILABLE"},
    )
    assert '"unassigned_words"' in prompt and '"start_ms": 9950' in prompt
    assert "不能把它硬塞进任一 cue" in prompt
    candidate_prompt, _ = _range_audio_context((row,))
    assert '"unassigned_words"' in candidate_prompt
    assert "尚未决定归属" in candidate_prompt
    assert build_witness_request(request) == build_witness_request(narrow)
    assert {role: path.read_bytes() for role, path in paths.items()} == before


@pytest.mark.parametrize("start,end,overlaps", [(1990, 2010, [1, 2]), (3000, 3010, [])])
def test_unassigned_native_word_keeps_cross_cue_or_outside_timing(start, end, overlaps):
    from src.autoslice.subtitle_audio_evidence import evidence_table
    source = "1\n00:00:01,000 --> 00:00:02,000\n甲\n\n2\n00:00:02,000 --> 00:00:03,000\n乙\n"
    evidence = {"provider": "mai", "native_segments": [{
        "start_ms": 1000, "end_ms": 3100, "text": "甲后乙", "speaker": "S00",
        "words_available": True,
        "words": [{"text": "后", "start_ms": start, "end_ms": end}],
    }]}
    row = evidence_table(source, evidence)["rows"][0]
    assert row["unassigned_words"] == [{
        "text": "后", "start_ms": start, "end_ms": end, "overlapping_cues": overlaps,
    }]
    assert row["cue_spans"] == []
    assert row["speaker_cluster"].endswith(":S00")
    evidence["native_segments"][0].update(words_available=False, words=None)
    assert "unassigned_words" not in evidence_table(source, evidence)["rows"][0]


def test_native_word_timing_cannot_be_invented_by_rehashing_metadata(tmp_path):
    from src.autoslice.local_asr_target_evidence import exact_target_evidence
    from src.autoslice.native_context_witness import _native_transcript_witness, _prepare_request
    source, _finding, _clip, narrow, observation, paths = _native_range_artifacts(
        tmp_path, "mai", word_timing=True,
    )
    receipt = json.loads(paths["provider_receipt"].read_text())
    metadata = receipt["evidence"]
    metadata["native_segments"][0]["words"][1]["start_ms"] += 1
    receipt["evidence_sha256"] = independent_module._digest(metadata)
    envelope = json.loads(paths["native_envelope"].read_text())
    native = exact_target_evidence(
        metadata, audio=paths["audio_clip"].read_bytes(),
        source_sha256=envelope["source_media_sha256"], start_ms=5100, end_ms=19100,
    )
    envelope["native_observation"] = native
    envelope["witness"] = _native_transcript_witness(
        request=envelope["request"], prepared=_prepare_request(envelope["request"]),
        native_observation=native, source_sha256=envelope["source_media_sha256"], provider="mai",
    )
    for role, value in (("provider_receipt", receipt), ("native_envelope", envelope)):
        observation["artifact_bindings"][f"{role}_sha256"] = _write_json(paths[role], value)
    result, reason = validate_range_acoustic_observation(source, narrow, observation)
    assert result is None
    assert reason == "NATIVE_RANGE_WORD_TIMELINE_DRIFT"


def _padded_recording_provenance(tmp_path, media_path):
    original = tmp_path / "recording.mp4"
    original.write_bytes(b"original recording bytes")
    original_sha = hashlib.sha256(original.read_bytes()).hexdigest()
    piece = tmp_path / "piece.mp4"
    piece.write_bytes(media_path.read_bytes())
    media_sha = hashlib.sha256(piece.read_bytes()).hexdigest()
    piece_proof = piece.with_suffix(".provenance.json")
    padded_proof = media_path.with_suffix(".provenance.json")
    _write_json(piece_proof, {
        "source_path": str(original), "source_sha256": original_sha,
        "source_media_binding": "sha256:" + original_sha,
        "start_ms": 438350, "end_ms": 617760,
        "output_path": str(piece), "output_sha256": media_sha,
    })
    _write_json(padded_proof, {
        "inputs": [{"path": str(piece), "sha256": media_sha}],
        "output_path": str(media_path), "output_sha256": media_sha,
    })
    return original_sha, original, piece, piece_proof, padded_proof


def _multi_padded_recording_provenance(tmp_path, media_path):
    """Create a producer-style padded output made from two real pieces."""
    original = tmp_path / "recording.mp4"
    original.write_bytes(b"original recording bytes")
    original_sha = hashlib.sha256(original.read_bytes()).hexdigest()
    pieces = []
    intervals = ((438350, 500000), (500000, 617760))
    media = media_path.read_bytes()
    split = len(media) // 2
    for index, (payload, (start_ms, end_ms)) in enumerate(
        zip((media[:split], media[split:]), intervals, strict=True)
    ):
        piece = tmp_path / f"piece_{index}.mp4"
        piece.write_bytes(payload)
        piece_sha = hashlib.sha256(payload).hexdigest()
        piece_proof = piece.with_suffix(".provenance.json")
        _write_json(piece_proof, {
            "source_path": str(original), "source_sha256": original_sha,
            "source_media_binding": "sha256:" + original_sha,
            "start_ms": start_ms, "end_ms": end_ms,
            "output_path": str(piece), "output_sha256": piece_sha,
        })
        pieces.append((piece, piece_proof, piece_sha, start_ms, end_ms))
    padded_proof = media_path.with_suffix(".provenance.json")
    _write_json(padded_proof, {
        "inputs": [
            {"path": str(piece), "sha256": piece_sha}
            for piece, _proof, piece_sha, _start, _end in pieces
        ],
        "output_path": str(media_path),
        "output_sha256": hashlib.sha256(media).hexdigest(),
    })
    return original_sha, original, pieces, padded_proof


def _source_context_provenance(tmp_path):
    """Build the one-piece source-context envelope used by stage40/41."""
    media = tmp_path / "source-context.mp4"
    source = tmp_path / "recording.flv"
    media.write_bytes(b"source-context-bytes")
    source.write_bytes(b"immutable-parent-recording")
    media_sha = hashlib.sha256(media.read_bytes()).hexdigest()
    source_sha = hashlib.sha256(source.read_bytes()).hexdigest()
    start_ms, end_ms = 1_075_200, 1_226_160
    row = {
        "piece_id": "piece_0",
        "source_path": str(source),
        "source_sha256": "sha256:" + source_sha,
        "source_start_ms": start_ms,
        "source_end_ms": end_ms,
        "start_ms": start_ms,
        "end_ms": end_ms,
        "duration_ms": end_ms - start_ms,
        "materialized_path": str(media),
        "materialized_sha256": "sha256:" + media_sha,
    }
    provenance_path = tmp_path / "padded-context.provenance.json"
    provenance = {
        "schema_version": "lidousha-speaker-recut-provenance.v1",
        "source_path": str(source),
        "source_sha256": "sha256:" + source_sha,
        "source_start_ms": start_ms,
        "source_end_ms": end_ms,
        "padded_path": str(media),
        "padded_sha256": "sha256:" + media_sha,
        "padded_duration_ms": end_ms - start_ms,
        "parent_full_hash_recomputed": False,
        "parent_full_decode_repeated": False,
        "piece_provenance_rows": [row],
    }
    _write_json(provenance_path, provenance)
    request = {
        "clip_context_binding": {
            "source_media_sha256s": ["sha256:" + source_sha],
        },
    }
    return request, media, source, provenance_path, provenance, media_sha, source_sha


def test_source_context_provenance_binds_real_one_piece_recording(tmp_path):
    request, media, source, provenance_path, _provenance, media_sha, source_sha = (
        _source_context_provenance(tmp_path)
    )
    binding = independent_module._range_media_source_binding(
        request, str(media), media_sha,
    )
    assert binding == {
        "role": "PRODUCER_PADDED_MEDIA_FROM_RECORDING",
        "media_sha256": media_sha,
        "recording_sha256": source_sha,
        "recording_start_ms": 1_075_200,
        "recording_end_ms": 1_226_160,
        "padded_provenance_path": str(provenance_path),
        "padded_provenance_sha256": hashlib.sha256(provenance_path.read_bytes()).hexdigest(),
        "piece_count": 1,
        "piece_id": "piece_0",
        "piece_source_interval_ms": [1_075_200, 1_226_160],
    }
    assert source.is_file() and media.is_file()


@pytest.mark.parametrize("drift", ["source", "media", "row_interval", "row_media"])
def test_source_context_provenance_rejects_byte_or_binding_drift(tmp_path, drift):
    request, media, source, provenance_path, provenance, media_sha, _source_sha = (
        _source_context_provenance(tmp_path)
    )
    if drift == "source":
        source.write_bytes(b"changed-parent-recording")
    elif drift == "media":
        media.write_bytes(b"changed-source-context")
    else:
        row = dict(provenance["piece_provenance_rows"][0])
        if drift == "row_interval":
            row["end_ms"] += 1
        else:
            row["materialized_path"] = str(tmp_path / "other.mp4")
        provenance["piece_provenance_rows"] = [row]
        _write_json(provenance_path, provenance)
    assert independent_module._range_media_source_binding(
        request, str(media), media_sha,
    ) is None


def test_source_context_bad_legacy_sidecar_does_not_fallback_to_explicit_envelope(tmp_path):
    request, media, _source, _provenance_path, _provenance, media_sha, _source_sha = (
        _source_context_provenance(tmp_path)
    )
    legacy_path = media.with_suffix(".provenance.json")
    _write_json(legacy_path, {"schema_version": "ordinary-producer-padded.v1"})
    assert independent_module._range_media_source_binding(
        request, str(media), media_sha,
    ) is None


def test_native_range_replays_source_context_provenance_without_provider(tmp_path):
    source, _finding, _clip, narrow, observation, paths = _native_range_artifacts(
        tmp_path, "mai",
    )
    media = paths["source_media"]
    source_path = tmp_path / "recording.flv"
    source_path.write_bytes(b"immutable-parent-recording")
    media_sha = hashlib.sha256(media.read_bytes()).hexdigest()
    source_sha = hashlib.sha256(source_path.read_bytes()).hexdigest()
    start_ms, end_ms = 1_075_200, 1_226_160
    row = {
        "piece_id": "piece_0",
        "source_path": str(source_path),
        "source_sha256": "sha256:" + source_sha,
        "source_start_ms": start_ms,
        "source_end_ms": end_ms,
        "start_ms": start_ms,
        "end_ms": end_ms,
        "duration_ms": end_ms - start_ms,
        "materialized_path": str(media),
        "materialized_sha256": "sha256:" + media_sha,
    }
    _write_json(tmp_path / "padded-context.provenance.json", {
        "schema_version": "lidousha-speaker-recut-provenance.v1",
        "source_path": str(source_path),
        "source_sha256": "sha256:" + source_sha,
        "source_start_ms": start_ms,
        "source_end_ms": end_ms,
        "padded_path": str(media),
        "padded_sha256": "sha256:" + media_sha,
        "padded_duration_ms": end_ms - start_ms,
        "parent_full_hash_recomputed": False,
        "parent_full_decode_repeated": False,
        "piece_provenance_rows": [row],
    })
    narrow["clip_context_binding"] = {
        **narrow["clip_context_binding"],
        "source_media_sha256s": ["sha256:" + source_sha],
    }
    validated, reason = validate_range_acoustic_observation(source, narrow, observation)
    assert validated is not None, reason
    binding = validated["media_source_binding"]
    assert binding["recording_sha256"] == source_sha
    assert binding["piece_source_interval_ms"] == [start_ms, end_ms]
    assert binding["padded_provenance_path"] == str(tmp_path / "padded-context.provenance.json")


@pytest.mark.parametrize("provider", ["mai", "moss"])
def test_native_range_accepts_ordered_multi_piece_recording_chain(tmp_path, provider):
    source, finding, clip, _narrow, observation, paths = _native_range_artifacts(
        tmp_path, provider,
    )
    original_sha, _original, pieces, padded_proof = _multi_padded_recording_provenance(
        tmp_path, paths["source_media"],
    )
    clip["pieces"][0]["source_media_sha256"] = "sha256:" + original_sha
    request = build_context_adjudication_request(
        source, {**finding, "_prior_range_acoustic_observations": [observation]},
        clip_context=clip, source_media_timeline_offset_ms=100,
    )
    row, = request["context_only_range_acoustic_observations"]
    binding = row["media_source_binding"]
    assert binding["role"] == "PRODUCER_PADDED_MEDIA_FROM_RECORDING_CHAIN"
    assert binding["media_sha256"] == hashlib.sha256(paths["source_media"].read_bytes()).hexdigest()
    assert binding["piece_count"] == 2
    assert [piece["recording_sha256"] for piece in binding["pieces"]] == [
        original_sha, original_sha,
    ]
    assert [
        (piece["recording_start_ms"], piece["recording_end_ms"])
        for piece in binding["pieces"]
    ] == [(438350, 500000), (500000, 617760)]
    assert [piece["piece_path"] for piece in binding["pieces"]] == [
        str(piece[0]) for piece in pieces
    ]
    assert binding["padded_provenance_path"] == str(padded_proof)
    assert row["evidence_scope"] == "RANGE_CONTEXT_ONLY"
    assert row["mutation_authorized"] is False


@pytest.mark.parametrize(
    "drift", ["piece", "source", "output", "interval", "order", "duplicate", "symlink"],
)
def test_multi_piece_recording_chain_rejects_provenance_drift(tmp_path, drift):
    _source, _finding, _clip, narrow, _observation, paths = _native_range_artifacts(
        tmp_path, "mai",
    )
    original_sha, original, pieces, _padded_proof = _multi_padded_recording_provenance(
        tmp_path, paths["source_media"],
    )
    media_sha = hashlib.sha256(paths["source_media"].read_bytes()).hexdigest()
    if drift == "piece":
        pieces[1][0].write_bytes(b"changed producer piece")
    elif drift == "source":
        original.write_bytes(b"changed source recording")
    elif drift in {"output", "interval", "order"}:
        piece_proof = json.loads(pieces[1][1].read_text())
        if drift == "output":
            piece_proof["output_sha256"] = "0" * 64
        elif drift == "interval":
            piece_proof["end_ms"] = piece_proof["start_ms"]
        else:
            piece_proof["start_ms"] = 400000
        _write_json(pieces[1][1], piece_proof)
    elif drift == "duplicate":
        padded = json.loads(paths["source_media"].with_suffix(".provenance.json").read_text())
        padded["inputs"][1] = dict(padded["inputs"][0])
        _write_json(paths["source_media"].with_suffix(".provenance.json"), padded)
    else:
        link = tmp_path / "piece_link_target.json"
        link.write_bytes(pieces[1][1].read_bytes())
        pieces[1][1].unlink()
        pieces[1][1].symlink_to(link)
    narrow["clip_context_binding"]["source_media_sha256s"] = ["sha256:" + original_sha]
    assert independent_module._range_media_source_binding(
        narrow, str(paths["source_media"]), media_sha,
    ) is None


@pytest.mark.parametrize("provider", ["mai", "moss"])
def test_native_range_uses_real_padded_chain_keeps_recording_identity(tmp_path, provider):
    source, finding, clip, _narrow, observation, paths = _native_range_artifacts(tmp_path, provider)
    original_sha, *_ = _padded_recording_provenance(tmp_path, paths["source_media"])
    clip["pieces"][0]["source_media_sha256"] = "sha256:" + original_sha
    before = json.dumps(clip, sort_keys=True)
    request = build_context_adjudication_request(
        source, {**finding, "_prior_range_acoustic_observations": [observation]},
        clip_context=clip, source_media_timeline_offset_ms=100,
    )
    row, = request["context_only_range_acoustic_observations"]
    assert row["media_source_binding"]["recording_sha256"] == original_sha
    assert row["media_source_binding"]["role"] == "PRODUCER_PADDED_MEDIA_FROM_RECORDING"
    assert request["clip_context_binding"]["source_media_sha256s"] == ["sha256:" + original_sha]
    assert row["mutation_authorized"] is False
    assert json.dumps(clip, sort_keys=True) == before


def test_range_witness_uses_same_producer_chain_with_original_context(tmp_path):
    source = _srt("第一句", "第二句", "当前句", "第四句", "第五句", "第六句")
    finding = {"cue_index": 3, "suspect": "当前", "suggestion": "目前",
               "proposed_full_cue": "目前句", "repair_class": "phonetic"}
    clip = _range_clip_context()
    request = build_context_adjudication_request(
        source, finding, clip_context=clip, source_media_timeline_offset_ms=100,
    )
    observation, broad, _ = _range_artifacts(tmp_path, source, request)
    media_path = Path(observation["source_media_path"])
    original_sha, *_ = _padded_recording_provenance(tmp_path, media_path)
    clip["pieces"][0]["source_media_sha256"] = "sha256:" + original_sha
    request = build_context_adjudication_request(
        source, {**finding, "_prior_range_acoustic_observations": [observation]},
        clip_context=clip, source_media_timeline_offset_ms=100,
    )
    row, = request["context_only_range_acoustic_observations"]
    assert row["media_source_binding"]["recording_sha256"] == original_sha
    assert row["witness_request"] == broad
    assert request["clip_context_binding"]["source_media_sha256s"] == ["sha256:" + original_sha]
    persisted = build_context_adjudication_request(
        source, {**finding, "_prior_range_acoustic_observations": [row]},
        clip_context=clip, source_media_timeline_offset_ms=100,
    )
    assert persisted == request


def test_exact_review_dispatcher_supplies_saved_range_to_actual_context_builder(tmp_path):
    source, finding, clip, _request, observation, _paths = _native_range_artifacts(tmp_path, "mai")
    prompts = []

    def judge(prompt):
        prompts.append(prompt)
        return json.dumps({"choice": "CURRENT", "needs_audio": False,
                           "current_utterance_supported": True, "reason": "unit seam probe"})

    pending, resolved = adjudicate_exact_release_findings(
        source, [finding], entity_verifier=None, clip_context=clip,
        source_media_timeline_offset_ms=100, judge_llm_call=judge,
        prior_range_acoustic_observations=[observation],
    )
    assert any("RANGE_CONTEXT_ONLY" in prompt and "整句范围听写" in prompt for prompt in prompts)
    row, = pending + resolved
    request = row["exact_release_adjudication"]["request"]
    saved, = request["context_only_range_acoustic_observations"]
    assert saved["provider"] == "mai" and saved["mutation_authorized"] is False
    assert saved["witness_request"]["cue_indexes"] == [1, 2, 3]


@pytest.mark.parametrize("drift", ["original", "piece", "output", "interval", "missing", "symlink"])
def test_range_padded_chain_never_accepts_unbound_or_drifted_recording(tmp_path, drift):
    source, _finding, _clip, narrow, observation, paths = _native_range_artifacts(tmp_path, "mai")
    original_sha, original, piece, piece_proof, padded_proof = _padded_recording_provenance(
        tmp_path, paths["source_media"],
    )
    narrow["clip_context_binding"]["source_media_sha256s"] = ["sha256:" + original_sha]
    if drift == "original":
        original.write_bytes(b"changed recording")
    elif drift == "piece":
        piece.write_bytes(b"changed recut")
    elif drift in {"output", "interval"}:
        doc = json.loads(piece_proof.read_text())
        doc["output_sha256" if drift == "output" else "end_ms"] = "0" * 64 if drift == "output" else 1
        _write_json(piece_proof, doc)
    elif drift == "missing":
        padded_proof.unlink()
    else:
        copy = tmp_path / "copy.json"
        copy.write_bytes(piece_proof.read_bytes())
        piece_proof.unlink()
        piece_proof.symlink_to(copy)
    row, reason = validate_range_acoustic_observation(source, narrow, observation)
    assert row is None and reason == "NATIVE_RANGE_CURRENT_SOURCE_DRIFT"


@pytest.mark.parametrize("drift", ["audio", "provider_text", "raw_file", "candidate", "srt", "narrow_outside"])
def test_native_range_context_rejects_saved_artifact_or_current_binding_drift(tmp_path, drift):
    source, _finding, _clip, narrow, observation, paths = _native_range_artifacts(tmp_path, "moss")
    if drift == "audio":
        paths["audio_clip"].write_bytes(b"different-audio")
    elif drift == "provider_text":
        receipt = json.loads(paths["provider_receipt"].read_text())
        receipt["evidence"]["raw_response"]["text"] = "different-native-text"
        observation["artifact_bindings"]["provider_receipt_sha256"] = _write_json(paths["provider_receipt"], receipt)
    elif drift == "candidate":
        narrow["clip_context_binding"]["candidate_id"] = "other-candidate"
    elif drift == "raw_file":
        observation["artifact_bindings"]["raw_response_sha256"] = _write_json(
            paths["raw_response"], {"text": "different-raw-response"},
        )
    elif drift == "srt":
        # A cue-grid change is drift; a text-only revision is not (41:1489-1492).
        source = source + "\n99\n00:09:00,000 --> 00:09:01,000\n新增句\n"
        narrow["whole_clip_current_srt"] = source
    else:
        narrow["matched_end_ms"] = 20000
    validated, reason = validate_range_acoustic_observation(source, narrow, observation)
    assert validated is None and reason.startswith("NATIVE_RANGE_")


def _secondary_artifacts(tmp_path, request, rows):
    """Build the same externally-bound file set required by the native seam."""
    target = build_witness_request(request)
    source_path = tmp_path / "source.mp4"
    source_path.write_bytes(b"source-media-for-secondary-evidence")
    source_sha = hashlib.sha256(source_path.read_bytes()).hexdigest()
    physical_start = target["matched_start_ms"] + target["source_media_timeline_offset_ms"]
    physical_end = target["matched_end_ms"] + target["source_media_timeline_offset_ms"]
    request_path = tmp_path / "witness-request.json"
    _write_json(
        request_path,
        {
            "witness_request": target,
            "source_media_sha256": source_sha,
            "expected_source_target_ms": [physical_start, physical_end],
        },
    )
    provider_bindings = {}
    for index, row in enumerate(rows):
        provider = row["provider"]
        crop_start = row["timeline_binding"]["source_media"]["crop_start_ms"]
        crop_path = tmp_path / f"{provider}-crop.bin"
        audio_path = tmp_path / f"{provider}-audio.bin"
        crop_path.write_bytes(f"crop-{provider}".encode())
        audio_path.write_bytes(f"audio-{provider}".encode())
        crop_sha = hashlib.sha256(crop_path.read_bytes()).hexdigest()
        audio_sha = hashlib.sha256(audio_path.read_bytes()).hexdigest()
        row["source_media_sha256"] = source_sha
        row["source_crop_sha256"] = crop_sha
        row["secondary_audio_sha256"] = audio_sha
        row["provider_input_audio_sha256"] = audio_sha
        row.setdefault("served_from_cache", True)
        raw_segments = []
        for segment in row["native_segments"]:
            relative = dict(segment)
            relative["start_ms"] -= crop_start
            relative["end_ms"] -= crop_start
            raw_segments.append(relative)
        raw_response = {"text": row["target_overlap_segments"][0]["text"], "provider": provider}
        response_sha = independent_module._digest(raw_response)
        row["provider_response_sha256"] = response_sha
        response_evidence = {
            "contract": "candidate-free-native-secondary-v1",
            "provider": provider,
            "model": row["model"],
            "input_audio_sha256": audio_sha,
            "response_sha256": response_sha,
            "raw_response": raw_response,
            "native_segments": raw_segments,
            "candidate_exposure": "none",
            "source_crop_sha256": crop_sha,
            "secondary_audio_sha256": audio_sha,
            "provider_input_audio_sha256": audio_sha,
        }
        response_path = tmp_path / f"{provider}-response.json"
        response_file_sha = _write_json(
            response_path,
            {
                "binding": {
                    "contract": "candidate-free-native-secondary-v1",
                    "provider": provider,
                    "model": row["model"],
                    "input_audio_sha256": audio_sha,
                },
                "evidence": response_evidence,
                "evidence_sha256": independent_module._digest(response_evidence),
            },
        )
        receipt = {key: row[key] for key in independent_module._RECEIPT_FIELDS}
        receipt_path = tmp_path / f"{provider}-secondary-evidence.json"
        receipt_sha = _write_json(receipt_path, receipt)
        row["receipt_sha256"] = receipt_sha
        provider_bindings[provider] = {
            "receipt_path": str(receipt_path),
            "receipt_sha256": receipt_sha,
            "crop_path": str(crop_path),
            "audio_path": str(audio_path),
            "response_path": str(response_path),
            "response_file_sha256": response_file_sha,
        }
    return {
        "providers": provider_bindings,
        "request_path": str(request_path),
        "source_media_path": str(source_path),
    }


def _secondary_payload(tmp_path, request, rows):
    bindings = _secondary_artifacts(tmp_path, request, rows)
    return {
        "providers": {row["provider"]: row for row in rows},
        "artifact_bindings": bindings,
    }, bindings


def _refresh_receipt_binding(bindings, provider, row):
    provider_binding = bindings["providers"][provider]
    receipt_path = Path(provider_binding["receipt_path"])
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt.update({key: row[key] for key in independent_module._RECEIPT_FIELDS})
    receipt_sha = _write_json(receipt_path, receipt)
    provider_binding["receipt_sha256"] = receipt_sha
    row["receipt_sha256"] = receipt_sha


def _verifier(text):
    calls = []

    def verify(target):
        return {
            "schema_version": "subtitle-span-acoustic-witness.v1",
            "witness_protocol": BLIND_PINYIN_PROTOCOL,
            "request_sha256": target["request_sha256"], "status": "OBSERVED",
            "target_audible": True, "heard_pinyin": "zhen zheng yuan ju",
            "confidence": .9, "syllable_count": 4, "uncertain_positions": [],
        }

    def observe(target):
        calls.append(dict(target))
        return [_native(target, "mai", text), _native(target, "moss", text)]

    verify.observe_independent_witnesses = observe
    return verify, calls


def _answer(choice, *, candidate_id="", supported=False, prompt=None):
    answer = {"choice": choice, "candidate_id": candidate_id,
                       "ranking": [{"choice": choice, "p": .98},
                                   *[{"choice": c, "p": .01} for c in ("CURRENT", "PROPOSED", "NEITHER") if c != choice]],
                       "needs_audio": not supported, "reason": "实际原音候选与整片语境",
                       "current_utterance_supported": supported,
                       "current_utterance_support_reason": "目标原句得到支持" if supported else "原句仍无法成立"}
    if prompt is not None and "\nSPAN_REVIEW_INPUT\n" in prompt:
        data=json.loads(prompt.split("\nSPAN_REVIEW_INPUT\n",1)[1].split("\nEND_SPAN_REVIEW_INPUT\n",1)[0])
        # These existing minimal provider fixtures have no sealed acoustic
        # acquisitions. Respond to the current schema without fabricating any
        # alignment; compilation stays explicitly blocked, and only their
        # independently tested legacy full-utterance contract is exercised.
        answer['span_review']={'binding_sha256':data['binding']['binding_sha256'],
            'request_sha256':data['request_sha256'],
            'context_basis':'Synthetic legacy full-utterance contract; no sealed span acoustic authority.',
            'all_audio_questions_located':True,'target_needs_audio':not supported,
            'alignments':[],'audio_questions':[]}
    return json.dumps(answer)


def test_independent_observation_does_not_close_remaining_explicit_audio_request():
    verify, calls = _verifier("真正原句")

    def judge(prompt):
        if CANDIDATE_BLIND_TRANSCRIPT_PROTOCOL in prompt:
            return _answer("PROPOSED", candidate_id="SOURCE_1" if '"SOURCE_1"' in prompt else "PROPOSAL", prompt=prompt)
        return _answer("CURRENT", prompt=prompt)

    output, audit = adjudicate_context_finding(
        SOURCE, FINDING, entity_verifier=verify, judge_llm_call=judge,
        require_complete_utterance_support=True,
    )
    assert output == SOURCE
    assert len(calls) == 1
    assert audit["repaired"] is False
    assert audit["policy_branch"] == "JUDGE_REQUESTS_MORE_AUDIO"
    assert audit["independent_asr"]["candidate_judge"]["judge"]["needs_audio_requested"] is True


def test_relative_current_triggers_independent_asr_and_typed_rebound_correction():
    verify, calls = _verifier("真正原句")
    prompts = []

    def judge(prompt):
        prompts.append(prompt)
        assert "# 字幕坏闭集重建" not in prompt
        if CANDIDATE_BLIND_TRANSCRIPT_PROTOCOL in prompt:
            assert '"provider": "mai"' in prompt and '"provider": "moss"' in prompt
            return _answer("PROPOSED", candidate_id="SOURCE_1" if '"SOURCE_1"' in prompt else "PROPOSAL", supported=True, prompt=prompt)
        return _answer("CURRENT", prompt=prompt)

    output, audit = adjudicate_context_finding(
        SOURCE, FINDING, entity_verifier=verify, judge_llm_call=judge,
        require_complete_utterance_support=True,
    )
    assert "真正原句" in output and "错误原句" not in output
    assert len(calls) == 1  # one combined full-cue request, each provider once
    assert len(prompts) == 4  # text first, primary witness, ASR selection, new bound closed set
    assert audit["mutation_authority"]["status"] == "PASS"
    assert audit["request"]["proposed_cue"] == "真正原句"
    assert audit["rebuilt_finding"]["proposed_full_cue"] == "真正原句"
    assert audit["independent_asr"]["routes_attempted"] == 2
    assert hashlib.sha256("错误原句".encode()).hexdigest() == audit["request"]["base_text_sha256"]


def test_native_current_closure_requires_explicit_whole_utterance_support():
    verify, calls = _verifier("错误原句")

    def judge(prompt):
        return _answer("CURRENT", supported=CANDIDATE_BLIND_TRANSCRIPT_PROTOCOL in prompt, prompt=prompt)

    unresolved, resolved = adjudicate_exact_release_findings(
        SOURCE, [FINDING], entity_verifier=verify, judge_llm_call=judge,
    )
    assert unresolved == [] and len(resolved) == 1 and len(calls) == 1
    audit = resolved[0]["exact_release_adjudication"]
    assert audit["witness_judge"]["judge"]["current_utterance_supported"] is True
    assert audit["verdict"]["witness_protocol"] == CANDIDATE_BLIND_TRANSCRIPT_PROTOCOL


@pytest.mark.parametrize("change_rebound_choice", [False, True])
def test_cached_native_selection_rebinds_primary_and_refuses_second_mismatch(monkeypatch, change_rebound_choice):
    import src.autoslice.final_review_auditor as module
    from src.autoslice.independent_context_adjudication import bind_request
    original_builder = module.build_context_adjudication_request

    def builder(*args, **kwargs):
        request = original_builder(*args, **kwargs)
        return bind_request(request, proposed_candidates=[
            {"text": "第一原句", "source": {"provider": "mai"}},
            {"text": "另一个原句", "source": {"provider": "moss"}},
        ])

    monkeypatch.setattr(module, "build_context_adjudication_request", builder)
    finding = {**FINDING, "suggestion": "第一", "proposed_full_cue": "第一原句"}
    native_prompts = []
    audio_calls = []

    def verify(target):
        audio_calls.append(target)
        return _native(target, "mai", "第一原句")

    def judge(prompt):
        if CANDIDATE_BLIND_TRANSCRIPT_PROTOCOL not in prompt:
            return _answer("CURRENT", prompt=prompt)
        native_prompts.append(prompt)
        return _answer("PROPOSED", candidate_id=(
            "SOURCE_2" if len(native_prompts) == 1
            else "SOURCE_1" if change_rebound_choice else "PROPOSAL"
        ), supported=True, prompt=prompt)

    output, audit = adjudicate_context_finding(
        SOURCE, finding, entity_verifier=verify, judge_llm_call=judge,
        require_complete_utterance_support=True,
    )
    assert len(audio_calls) == 1
    assert len(native_prompts) == 2
    assert audit["request"]["proposed_cue"] == "另一个原句"
    if change_rebound_choice:
        assert output == SOURCE
        assert audit["repaired"] is False
        assert audit["policy_branch"] == "SELECTED_TRANSCRIPT_CANDIDATE_REQUIRES_REBOUND"
    else:
        assert "另一个原句" in output and "第一原句" not in output
        assert audit["repaired"] is True
        assert audit["witness_judge"]["judge"]["selected_candidate_text"] == "另一个原句"


def test_source_window_drift_never_becomes_asr_candidate():
    verify, _ = _verifier("真正原句")
    request = build_context_adjudication_request(SOURCE, FINDING)

    def observe(target):
        row = _native(target, "mai", "真正原句")
        row["audio_start_ms"] += 1
        return [row]

    verify.observe_independent_witnesses = observe
    _, witness, audit = independent_asr_candidates(request, verify)
    assert witness is None and audit["routes_exhausted"] is False


def test_missing_native_configuration_is_not_asr_exhaustion():
    request = build_context_adjudication_request(SOURCE, FINDING)

    class Verify:
        def observe_independent_witnesses(self, target):
            rows = []
            for provider, reason in [("mai", "MAI_ENDPOINT_INVALID"), ("moss", "MOSS_API_KEY_MISSING")]:
                row = _native(target, provider, "")
                row.update(status="UNCERTAIN", reason_code=reason)
                rows.append(row)
            return rows

    unchanged, witness, audit = independent_asr_candidates(request, Verify())
    assert unchanged == request and witness is None
    assert audit["routes_attempted"] == 2 and audit["routes_exhausted"] is False


def test_bound_valid_no_speech_observation_is_completed_not_a_retry_error():
    request = build_context_adjudication_request(SOURCE, FINDING)

    class Verify:
        def observe_independent_witnesses(self, target):
            row = _native(target, "mai", "")
            native = {
                "status": "NO_SPEECH_REPORTED", "authority": "EVIDENCE_ONLY",
                "mutation_authorized": False, "candidate_exposure": "none",
                "provider": row["provider"], "model": row["model"],
                "source_media_sha256": row["source_media_sha256"],
                "target_start_ms": row["audio_start_ms"], "target_end_ms": row["audio_end_ms"],
                "crop_is_exact_target": True, "input_audio_sha256": "b" * 64,
                "response_sha256": "c" * 64,
            }
            native["receipt_sha256"] = independent_module._digest(native)
            row.update(status="UNCERTAIN", native_observation=native, reason_code="NATIVE_NO_SPEECH")
            return [row]

    _, witness, audit = independent_asr_candidates(request, Verify())
    assert witness is None and audit["routes_exhausted"] is True


def test_independent_sources_cannot_be_silently_combined():
    verify, _ = _verifier("真正原句")
    request = build_context_adjudication_request(SOURCE, FINDING)

    def observe(target):
        first = _native(target, "mai", "真正原句")
        second = _native(target, "moss", "另一个源的原句")
        second["source_media_sha256"] = "b" * 64
        return [first, second]

    verify.observe_independent_witnesses = observe
    unchanged, witness, audit = independent_asr_candidates(request, verify)
    assert unchanged == request and witness is None
    assert audit["routes_exhausted"] is False
    assert audit["reason_code"] == "INDEPENDENT_ASR_SOURCE_CONFLICT"


def test_phoneme_proposal_cannot_precede_actual_asr_routes():
    calls = []
    _, audit = _rebuild_candidate_after_neither(
        finding=FINDING, request={"current_cue": "错误原句", "proposed_cue": "误认原句"},
        witness={"status": "OBSERVED", "target_audible": True, "heard_pinyin": "zhen zheng yuan ju"},
        llm_call=lambda prompt: calls.append(prompt),
    )
    assert calls == []
    assert audit["reason_code"] == "PHONEME_REBUILD_REQUIRES_EXHAUSTED_ASR_AND_ACTUAL_PHONEMES"


def test_secondary_padded_receipts_become_non_exact_cpa_candidates(tmp_path):
    request = build_context_adjudication_request(SOURCE, FINDING)
    target = build_witness_request(request)
    rows = [_secondary(target, "mai"), _secondary(target, "moss", "猫，然后他当时就是在做梦")]
    payload, _ = _secondary_payload(tmp_path, request, rows)

    adapted = adapt_secondary_audio_evidence(request, payload)
    assert [row["provider"] for row in adapted] == ["mai", "moss"]
    assert all(row["crop_is_exact_target"] is False for row in adapted)
    assert all(row["evidence_scope"] == "SECONDARY_AUDIO_TARGET_OVERLAP" for row in adapted)
    assert all("native_observation" not in row for row in adapted)
    assert adapted[1]["transcript_scope"] == "TARGET_OVERLAP_SEGMENT"
    assert adapted[1]["secondary_audio_evidence"]["target_overlap_segments"][0]["text"].startswith("猫")
    assert Path(adapted[0]["artifact_provenance"]["source_media_path"]).is_file()

    class Verify:
        def observe_secondary_audio_evidence(self, witness_request):
            assert witness_request["request_sha256"] == target["request_sha256"]
            return payload

    augmented, witness, audit = independent_asr_candidates(request, Verify())
    assert [item["text"] for item in augmented["proposed_candidates"]] == [
        "然后他当时就是在做梦",
        "猫，然后他当时就是在做梦",
    ]
    assert witness is not None and witness["crop_is_exact_target"] is False
    assert audit["secondary_audio_adapter"] == "secondary-audio-witness-evidence.v1"
    assert audit["candidate_count"] == 2
    assert audit["routes_exhausted"] is True


def test_secondary_external_source_drift_is_not_hidden_by_other_provider(tmp_path):
    request = build_context_adjudication_request(SOURCE, FINDING)
    target = build_witness_request(request)
    rows = [_secondary(target, "mai"), _secondary(target, "moss")]
    payload, bindings = _secondary_payload(tmp_path, request, rows)
    Path(bindings["source_media_path"]).write_bytes(b"source-media-was-replaced")

    class Verify:
        def observe_secondary_audio_evidence(self, _target):
            return payload

    unchanged, witness, audit = independent_asr_candidates(request, Verify())
    assert unchanged == request and witness is None
    assert audit["reason_code"] == "SECONDARY_AUDIO_SOURCE_FILE_DRIFT"
    assert audit["routes_exhausted"] is False


def test_secondary_missing_artifact_is_rejected(tmp_path):
    request = build_context_adjudication_request(SOURCE, FINDING)
    target = build_witness_request(request)
    row = _secondary(target, "mai")
    payload, bindings = _secondary_payload(tmp_path, request, [row])
    Path(bindings["providers"]["mai"]["audio_path"]).unlink()

    assert adapt_secondary_audio_evidence(request, payload) == []

    class Verify:
        def observe_secondary_audio_evidence(self, _target):
            return payload

    unchanged, witness, audit = independent_asr_candidates(request, Verify())
    assert unchanged == request and witness is None
    assert audit["reason_code"] == "SECONDARY_AUDIO_MEDIA_ARTIFACT_DRIFT"


def test_secondary_target_outside_bound_crop_is_rejected(tmp_path):
    request = build_context_adjudication_request(SOURCE, FINDING)
    target = build_witness_request(request)
    row = _secondary(target, "mai")
    payload, bindings = _secondary_payload(tmp_path, request, [row])
    row["timeline_binding"]["source_media"]["crop_end_ms"] = (
        target["matched_end_ms"] + target["source_media_timeline_offset_ms"] - 1
    )
    _refresh_receipt_binding(bindings, "mai", row)

    assert adapt_secondary_audio_evidence(request, payload) == []

    class Verify:
        def observe_secondary_audio_evidence(self, _target):
            return payload

    unchanged, witness, audit = independent_asr_candidates(request, Verify())
    assert unchanged == request and witness is None
    assert audit["reason_code"] == "SECONDARY_AUDIO_TARGET_OUTSIDE_CROP"


def test_secondary_same_receipt_sha_cannot_hide_segment_mutation(tmp_path):
    request = build_context_adjudication_request(SOURCE, FINDING)
    target = build_witness_request(request)
    row = _secondary(target, "mai")
    payload, _ = _secondary_payload(tmp_path, request, [row])
    row["native_segments"][0]["text"] = "篡改后的segment"

    assert adapt_secondary_audio_evidence(request, payload) == []

    class Verify:
        def observe_secondary_audio_evidence(self, _target):
            return payload

    unchanged, witness, audit = independent_asr_candidates(request, Verify())
    assert unchanged == request and witness is None
    assert audit["reason_code"] == "SECONDARY_AUDIO_RECEIPT_CONTENT_DRIFT"


def test_secondary_response_segments_change_is_rejected_even_with_new_file_hash(tmp_path):
    request = build_context_adjudication_request(SOURCE, FINDING)
    target = build_witness_request(request)
    row = _secondary(target, "mai")
    payload, bindings = _secondary_payload(tmp_path, request, [row])
    response_path = Path(bindings["providers"]["mai"]["response_path"])
    response_doc = json.loads(response_path.read_text(encoding="utf-8"))
    response_doc["evidence"]["native_segments"][0]["text"] = "provider drift"
    response_doc["evidence_sha256"] = independent_module._digest(response_doc["evidence"])
    bindings["providers"]["mai"]["response_file_sha256"] = _write_json(response_path, response_doc)

    assert adapt_secondary_audio_evidence(request, payload) == []

    class Verify:
        def observe_secondary_audio_evidence(self, _target):
            return payload

    unchanged, witness, audit = independent_asr_candidates(request, Verify())
    assert unchanged == request and witness is None
    assert audit["reason_code"] == "SECONDARY_AUDIO_NATIVE_RESPONSE_DRIFT"


def _secondary_context_setup(tmp_path, *, current_source_sha=None):
    source = SOURCE
    source_sha = hashlib.sha256(b"source-media-for-secondary-evidence").hexdigest()
    clip_context = {
        "schema_version": "lidousha-clip-context.v1",
        "candidate_id": "candidate-secondary-context",
        "pieces": [{"source_media_sha256": current_source_sha or source_sha}],
        "whole_clip_draft_srt_sha256": "draft-srt-sha",
        "context_sha256": "context-sha",
        "mutation_authorized": False,
    }
    finding = {**FINDING, "_prior_secondary_audio_observations": []}
    current_request = build_context_adjudication_request(
        source, finding, clip_context=clip_context,
    )
    current_target = build_witness_request(current_request)
    source_target = dict(current_target)
    source_target["evidence_id"] = "persisted-secondary-context-request"
    source_target.pop("request_sha256", None)
    source_target = build_witness_request(source_target)
    row = _secondary(source_target, "mai")
    payload, bindings = _secondary_payload(tmp_path, source_target, [row])
    observation = {
        "request": source_target,
        "evidence": payload,
    }
    return current_request, clip_context, source_target, observation, payload, bindings


def test_secondary_receipt_can_be_prior_context_without_rebinding_narrow_request(tmp_path):
    current_request, clip_context, source_target, observation, payload, bindings = (
        _secondary_context_setup(tmp_path)
    )

    assert adapt_secondary_audio_evidence(
        current_request, payload, artifact_bindings=bindings
    ) == []
    rows = adapt_secondary_audio_context_observation(current_request, observation)
    assert len(rows) == 1
    row = rows[0]
    assert row["schema_version"] == "secondary-audio-context-observation.v1"
    assert row["current_request_sha256"] == build_witness_request(current_request)[
        "request_sha256"
    ]
    assert row["source_request_sha256"] == source_target["request_sha256"]
    assert row["current_request_sha256"] != row["source_request_sha256"]
    assert row["current_candidate_id"] == "candidate-secondary-context"
    assert row["context_only"] is True
    assert row["authority"] == "EVIDENCE_ONLY"
    assert row["mutation_authorized"] is False
    assert row["crop_is_exact_target"] is False

    finding = {
        **FINDING,
        "_prior_secondary_audio_observations": [observation],
    }
    with_context = build_context_adjudication_request(
        SOURCE,
        finding,
        clip_context=clip_context,
    )
    assert build_witness_request(with_context) == build_witness_request(current_request)
    prior = with_context["prior_acoustic_observations"]
    assert len(prior) == 1
    assert prior[0]["schema_version"] == "secondary-audio-context-observation.v1"
    assert prior[0]["verdict"]["transcript_scope"] == "TARGET_OVERLAP_SEGMENT"
    from src.autoslice.acoustic_witness_adjudication import _word_choice_prompt

    prompt, *_ = _word_choice_prompt(
        llm_call=lambda _prompt: "",
        check_request=with_context,
        witness={
            "schema_version": "subtitle-span-acoustic-witness.v1",
            "request_sha256": build_witness_request(with_context)["request_sha256"],
            "status": "UNCERTAIN",
            "target_audible": True,
        },
    )
    assert "secondary-audio-context-observation.v1" in prompt
    assert "然后他当时就是在做梦" in prompt


def test_secondary_context_rejects_current_source_drift(tmp_path):
    current_request, _clip_context, _source_target, observation, _payload, _bindings = (
        _secondary_context_setup(tmp_path, current_source_sha="b" * 64)
    )
    assert adapt_secondary_audio_context_observation(current_request, observation) == []


def test_secondary_context_rejects_geometry_drift(tmp_path):
    current_request, _clip_context, source_target, _observation, _payload, _bindings = (
        _secondary_context_setup(tmp_path)
    )
    drifted = dict(source_target)
    drifted["matched_end_ms"] += 1
    drifted.pop("request_sha256", None)
    drifted = build_witness_request(drifted)
    row = _secondary(drifted, "mai")
    drift_dir = tmp_path / "drift"
    drift_dir.mkdir()
    payload, bindings = _secondary_payload(drift_dir, drifted, [row])
    observation = {"request": drifted, "evidence": payload, "artifact_bindings": bindings}
    assert adapt_secondary_audio_context_observation(current_request, observation) == []


def test_secondary_context_rejects_native_response_drift(tmp_path):
    current_request, _clip_context, source_target, _observation, _payload, bindings = (
        _secondary_context_setup(tmp_path)
    )
    response_path = Path(bindings["providers"]["mai"]["response_path"])
    response_doc = json.loads(response_path.read_text(encoding="utf-8"))
    response_doc["evidence"]["native_segments"][0]["text"] = "provider drift"
    response_doc["evidence_sha256"] = independent_module._digest(response_doc["evidence"])
    bindings["providers"]["mai"]["response_file_sha256"] = _write_json(
        response_path, response_doc
    )
    row = _secondary(source_target, "mai")
    observation = {
        "request": source_target,
        "evidence": {"providers": {"mai": row}},
        "artifact_bindings": bindings,
    }
    assert adapt_secondary_audio_context_observation(current_request, observation) == []


def test_public_adjudicator_passes_secondary_context_to_ordinary_judge(tmp_path):
    current_request, clip_context, _source_target, observation, _payload, _bindings = (
        _secondary_context_setup(tmp_path)
    )
    prompts = []

    def judge(prompt):
        prompts.append(prompt)
        return json.dumps(
            {
                "choice": "CURRENT",
                "needs_audio": False,
                "current_utterance_supported": True,
                "reason": "offline ordinary seam probe",
            },
            ensure_ascii=False,
        )

    _output, audit = adjudicate_context_finding(
        SOURCE,
        FINDING,
        entity_verifier=None,
        clip_context=clip_context,
        judge_llm_call=judge,
        prior_secondary_audio_observations=[observation],
    )

    request = audit["request"]
    assert len(request["prior_acoustic_observations"]) == 1
    assert request["prior_acoustic_observations"][0]["context_only"] is True
    assert request["prior_acoustic_observations"][0]["mutation_authorized"] is False
    assert "secondary-audio-context-observation.v1" in prompts[0]
    request_without_sha = dict(request)
    request_without_sha.pop("request_sha256")
    assert hashlib.sha256(
        json.dumps(
            request_without_sha,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest() == request["request_sha256"]


def test_range_witness_is_context_only_and_preserves_single_cue_witness(tmp_path):
    from src.autoslice.acoustic_witness_adjudication import _word_choice_prompt
    from src.autoslice.final_review_auditor import build_context_adjudication_request

    source = _srt("第一句", "第二句", "当前句", "第四句", "第五句", "第六句")
    finding = {
        "cue_index": 3,
        "suspect": "当前",
        "suggestion": "目前",
        "proposed_full_cue": "目前句",
        "repair_class": "phonetic",
    }
    narrow = build_context_adjudication_request(
        source,
        finding,
        clip_context=_range_clip_context(),
        source_media_timeline_offset_ms=100,
    )
    observation, broad, source_window = _range_artifacts(tmp_path, source, narrow)
    with_range = build_context_adjudication_request(
        source,
        {**finding, "_prior_range_acoustic_observations": [observation]},
        clip_context=_range_clip_context(),
        source_media_timeline_offset_ms=100,
    )

    assert build_witness_request(with_range) == build_witness_request(narrow)
    rows = with_range["context_only_range_acoustic_observations"]
    assert len(rows) == 1
    assert rows[0]["authority"] == "EVIDENCE_ONLY"
    assert rows[0]["mutation_authorized"] is False
    assert rows[0]["witness_request"] == broad
    assert rows[0]["source_window"] == source_window
    assert [row["cue_index"] for row in rows[0]["cue_texts"]] == [1, 2, 3, 4, 5, 6]

    witness = {
        "schema_version": "subtitle-span-acoustic-witness.v1",
        "witness_protocol": BLIND_PINYIN_PROTOCOL,
        "request_sha256": build_witness_request(with_range)["request_sha256"],
        "status": "OBSERVED",
        "target_audible": True,
        "heard_pinyin": "dang qian ju",
        "uncertain_positions": [],
        "syllable_count": 3,
        "confidence": 0.9,
    }
    prompt, *_ = _word_choice_prompt(
        llm_call=lambda _prompt: "",
        check_request=with_range,
        witness=witness,
    )
    assert "更宽范围的音频观察（按类型使用，仅上下文证据）" in prompt
    assert "candidate_blind=true" in prompt
    assert "CONTEXT_ASSISTED_AUDIO" in prompt
    assert "inferred 不是听到的字" in prompt
    assert "ran ge zhen" in prompt
    assert "不能直接证明当前 cue 的任何一个字" in prompt


def test_range_context_blind_text_revision_keeps_strict_observation_with_current_texts(tmp_path):
    """41:1489-1492: a subtitle-text-free blind range survives a text-only revision.

    Its prompt carried no subtitle text, so in-window text changes are not
    context drift at the identical cue grid. CPA readers get the current
    texts as ``current_cue_texts``; the original record stays labelled.
    """
    source = _srt("第一句", "第二句", "当前句", "第四句", "第五句", "第六句")
    finding = {
        "cue_index": 3,
        "suspect": "当前",
        "suggestion": "目前",
        "proposed_full_cue": "目前句",
        "repair_class": "phonetic",
    }
    old_request = build_context_adjudication_request(
        source, finding, clip_context=_range_clip_context(), source_media_timeline_offset_ms=100,
    )
    observation, broad, _source_window = _range_artifacts(tmp_path, source, old_request)
    current_source = source.replace("第二句", "第二版").replace("当前句", "当前新句")
    current_request = build_context_adjudication_request(
        current_source, {**finding, "proposed_full_cue": "目前新句"},
        clip_context=_range_clip_context(), source_media_timeline_offset_ms=100,
    )
    strict, reason = validate_range_acoustic_observation(current_source, current_request, observation)
    assert strict is not None, reason
    assert strict["status"] == "OBSERVED"
    assert strict["mutation_authorized"] is False
    assert strict["authority"] == "EVIDENCE_ONLY"
    assert strict["text_revision"] == "TEXT_ONLY_REVISION_AT_IDENTICAL_CUE_GRID"
    assert [row["text"] for row in strict["current_cue_texts"]] == [
        "第一句", "第二版", "当前新句", "第四句", "第五句", "第六句",
    ]
    assert [row["text"] for row in strict["cue_texts"]] == [
        "第一句", "第二句", "当前句", "第四句", "第五句", "第六句",
    ]
    assert strict["current_srt_sha256"] == hashlib.sha256(current_source.encode()).hexdigest()
    assert strict["witness_request"] == broad
    # A grid change is still rejected.
    retimed = current_source.replace("00:00:10,000 --> 00:00:14,000", "00:00:10,000 --> 00:00:14,100")
    retimed_request = build_context_adjudication_request(
        retimed, {**finding, "proposed_full_cue": "目前新句"},
        clip_context=_range_clip_context(), source_media_timeline_offset_ms=100,
    )
    rejected, code = validate_range_acoustic_observation(retimed, retimed_request, observation)
    assert rejected is None and code == "RANGE_ACOUSTIC_SRT_FILE_DRIFT"


def test_range_context_carryover_allows_text_change_but_keeps_authority_bound(tmp_path):
    # The provider prompt here exposed window subtitle text: an in-window
    # revision keeps the strict rejection and only the carryover may reuse it.
    source = _srt("第一句话", "第二句话", "当前句话", "第四句话", "第五句话", "第六句话")
    old_finding = {
        "cue_index": 3,
        "suspect": "当前",
        "suggestion": "目前",
        "proposed_full_cue": "目前句话",
        "repair_class": "phonetic",
    }
    old_request = build_context_adjudication_request(
        source,
        old_finding,
        clip_context=_range_clip_context(),
        source_media_timeline_offset_ms=100,
    )
    observation, broad, _source_window = _range_artifacts(
        tmp_path, source, old_request, prompt_text="请听写：第二句话 当前句话",
    )
    old_validated, old_reason = validate_range_acoustic_observation(
        source, old_request, observation
    )
    assert old_validated is not None, old_reason
    # This is the immutable target anchor produced with the old range
    # observation.  The artifact paths remain the raw receipt paths.
    anchored_observation = {
        **observation,
        "current_cue_index": old_validated["current_cue_index"],
        "current_target": {
            "cue_index": old_validated["current_cue_index"],
            "start_ms": old_request["matched_start_ms"],
            "end_ms": old_request["matched_end_ms"],
        },
    }

    current_source = source.replace("第二句话", "第二版话").replace("当前句话", "当前新句话")
    current_finding = {
        **old_finding,
        "proposed_full_cue": "目前新句话",
    }
    current_request = build_context_adjudication_request(
        current_source,
        current_finding,
        clip_context=_range_clip_context(),
        source_media_timeline_offset_ms=100,
    )
    strict, strict_reason = validate_range_acoustic_observation(
        current_source, current_request, anchored_observation
    )
    assert strict is None
    assert strict_reason == "RANGE_ACOUSTIC_SRT_FILE_DRIFT"

    carried, carry_reason = adapt_range_acoustic_context_observation(
        current_source, current_request, anchored_observation
    )
    assert carried is not None, carry_reason
    assert carried["schema_version"] == "range-acoustic-context-carryover.v1"
    assert carried["status"] == "CONTEXT_ONLY"
    assert carried["authority"] == "EVIDENCE_ONLY"
    assert carried["context_only"] is True
    assert carried["mutation_authorized"] is False
    assert carried["original_witness_request"] == broad
    assert carried["original_reviewed_srt_sha256"] == hashlib.sha256(
        source.encode("utf-8")
    ).hexdigest()
    assert carried["current_srt_sha256"] == hashlib.sha256(
        current_source.encode("utf-8")
    ).hexdigest()
    assert carried["current_narrow_witness_request"] == build_witness_request(
        current_request
    )
    assert carried["original_range_observation"]["status"] == "OBSERVED"
    assert carried["evidence_scope"] == "RANGE_CONTEXT_ONLY_SRT_CARRYOVER"
    assert [row["text"] for row in carried["cue_texts"]] == [
        "第一句话", "第二句话", "当前句话", "第四句话", "第五句话", "第六句话",
    ]
    assert [row["text"] for row in carried["current_cue_texts"]] == [
        "第一句话", "第二版话", "当前新句话", "第四句话", "第五句话", "第六句话",
    ]
    stale_request = dict(current_request)
    stale_request["whole_clip_current_srt"] = source
    stale, stale_reason = adapt_range_acoustic_context_observation(
        current_source, stale_request, anchored_observation
    )
    assert stale is None
    assert stale_reason == "RANGE_ACOUSTIC_CONTEXT_SRT_REQUEST_DRIFT"

    request = build_context_adjudication_request(
        current_source,
        {**current_finding, "_prior_range_acoustic_observations": [anchored_observation]},
        clip_context=_range_clip_context(),
        source_media_timeline_offset_ms=100,
    )
    assert request["context_only_range_acoustic_observations"] == [carried]
    assert build_witness_request(request) == build_witness_request(current_request)


@pytest.mark.parametrize(
    "drift", ["target", "range_geometry", "source", "candidate", "prompt", "response"]
)
def test_range_context_carryover_rejects_current_binding_drift(tmp_path, drift):
    source = _srt("第一句", "第二句", "当前句", "第四句", "第五句", "第六句")
    finding = {
        "cue_index": 3,
        "suspect": "当前",
        "suggestion": "目前",
        "proposed_full_cue": "目前句",
        "repair_class": "phonetic",
    }
    request = build_context_adjudication_request(
        source,
        finding,
        clip_context=_range_clip_context(),
        source_media_timeline_offset_ms=100,
    )
    observation, _broad, _source_window = _range_artifacts(tmp_path, source, request)
    old_validated, old_reason = validate_range_acoustic_observation(
        source, request, observation
    )
    assert old_validated is not None, old_reason
    observation = {
        **observation,
        "current_cue_index": old_validated["current_cue_index"],
        "current_target": {
            "cue_index": old_validated["current_cue_index"],
            "start_ms": request["matched_start_ms"],
            "end_ms": request["matched_end_ms"],
        },
    }

    current_source = source.replace("第一句", "第一版")
    current_finding = finding
    current_context = _range_clip_context()
    if drift == "target":
        current_finding = {
            **finding,
            "cue_index": 4,
            "suspect": "第四",
            "suggestion": "第肆",
            "proposed_full_cue": "第肆句",
        }
    elif drift == "range_geometry":
        current_source = current_source.replace(
            "00:00:10,000 --> 00:00:14,000",
            "00:00:10,000 --> 00:00:14,100",
        )
    elif drift == "source":
        current_request = dict(request)
        current_request["source_media_sha256"] = "f" * 64
    elif drift == "prompt":
        Path(observation["prompt_path"]).write_text(
            "drifted provider prompt", encoding="utf-8"
        )
    elif drift == "response":
        response_path = Path(observation["response_path"])
        response_doc = json.loads(response_path.read_text(encoding="utf-8"))
        response_doc["heard_pinyin"] = "drifted provider response"
        _write_json(response_path, response_doc)
    else:
        current_context = {**_range_clip_context(), "candidate_id": "other-candidate"}

    if drift == "source":
        current_request = dict(current_request)
        current_request["whole_clip_current_srt"] = current_source
    else:
        current_request = build_context_adjudication_request(
            current_source,
            current_finding,
            clip_context=current_context,
            source_media_timeline_offset_ms=100,
        )
    carried, reason = adapt_range_acoustic_context_observation(
        current_source, current_request, observation
    )
    assert carried is None
    assert reason is not None
    if drift == "target":
        assert reason == "RANGE_ACOUSTIC_CONTEXT_CURRENT_TARGET_DRIFT"
    elif drift == "range_geometry":
        assert reason == "RANGE_ACOUSTIC_CONTEXT_RANGE_GEOMETRY_DRIFT"
    elif drift == "source":
        assert reason.endswith("RANGE_ACOUSTIC_CURRENT_SOURCE_DRIFT")
    elif drift == "candidate":
        assert reason.endswith("RANGE_ACOUSTIC_CURRENT_CANDIDATE_DRIFT")
    else:
        assert "ORIGINAL_RANGE_ACOUSTIC_PROVIDER_ARTIFACT_DRIFT" in reason


def test_range_witness_allows_partial_first_cue_but_keeps_target_full(tmp_path):
    source = _srt("第一句", "第二句", "当前句", "第四句", "第五句", "第六句")
    finding = {
        "cue_index": 3,
        "suspect": "当前",
        "suggestion": "目前",
        "proposed_full_cue": "目前句",
        "repair_class": "phonetic",
    }
    narrow = build_context_adjudication_request(
        source,
        finding,
        clip_context=_range_clip_context(),
        source_media_timeline_offset_ms=100,
    )
    observation, _broad, _source_window = _range_artifacts(
        tmp_path,
        source,
        narrow,
        matched_start_ms=5500,
    )
    with_range = build_context_adjudication_request(
        source,
        {**finding, "_prior_range_acoustic_observations": [observation]},
        clip_context=_range_clip_context(),
        source_media_timeline_offset_ms=100,
    )
    rows = with_range["context_only_range_acoustic_observations"][0]["cue_texts"]
    assert rows[0]["coverage"] == "PARTIAL"
    assert rows[0]["covered_start_ms"] == 5500
    assert rows[0]["covered_end_ms"] == 9000
    assert rows[2]["coverage"] == "FULL"
    assert rows[2]["covered_start_ms"] == 15000
    assert rows[2]["covered_end_ms"] == 19000
    assert rows[-1]["coverage"] == "FULL"


def test_range_witness_accepts_explicit_provider_count_mismatch(tmp_path):
    source = _srt("第一句", "第二句", "当前句", "第四句", "第五句", "第六句")
    finding = {
        "cue_index": 3,
        "suspect": "当前",
        "suggestion": "目前",
        "proposed_full_cue": "目前句",
        "repair_class": "phonetic",
    }
    narrow = build_context_adjudication_request(
        source,
        finding,
        clip_context=_range_clip_context(),
        source_media_timeline_offset_ms=100,
    )
    observation, _broad, _source_window = _range_artifacts(
        tmp_path,
        source,
        narrow,
        provider_count_mismatch=True,
    )
    with_range = build_context_adjudication_request(
        source,
        {**finding, "_prior_range_acoustic_observations": [observation]},
        clip_context=_range_clip_context(),
        source_media_timeline_offset_ms=100,
    )
    assert len(with_range["context_only_range_acoustic_observations"]) == 1


def test_range_witness_requires_current_candidate_source_binding(tmp_path):
    source = _srt("第一句", "第二句", "当前句", "第四句", "第五句", "第六句")
    finding = {
        "cue_index": 3,
        "suspect": "当前",
        "suggestion": "目前",
        "proposed_full_cue": "目前句",
        "repair_class": "phonetic",
    }
    narrow = build_context_adjudication_request(
        source,
        finding,
        clip_context=_range_clip_context(),
        source_media_timeline_offset_ms=100,
    )
    observation, _broad, _source_window = _range_artifacts(tmp_path, source, narrow)
    narrow.pop("clip_context_binding")
    validated, reason = validate_range_acoustic_observation(
        source, narrow, observation
    )
    assert validated is None
    assert reason == "RANGE_ACOUSTIC_CURRENT_SOURCE_IDENTITY_MISSING"


@pytest.mark.parametrize(
    "drift",
    [
        "source",
        "srt",
        "offset",
        "response",
        "audio",
        "result_mutation",
        "publication",
        "pretend_narrow",
    ],
)
def test_range_witness_binding_drift_is_rejected(tmp_path, drift):
    from src.autoslice.final_review_auditor import build_context_adjudication_request

    source = _srt("第一句", "第二句", "当前句", "第四句", "第五句", "第六句")
    finding = {
        "cue_index": 3,
        "suspect": "当前",
        "suggestion": "目前",
        "proposed_full_cue": "目前句",
        "repair_class": "phonetic",
    }
    narrow = build_context_adjudication_request(
        source,
        finding,
        clip_context=_range_clip_context(),
        source_media_timeline_offset_ms=100,
    )
    observation, broad, _ = _range_artifacts(tmp_path, source, narrow)
    if drift == "source":
        Path(observation["source_media_path"]).write_bytes(b"replaced-source")
    elif drift == "srt":
        Path(observation["reviewed_srt_path"]).write_text(
            source.replace("当前句", "漂移句"), encoding="utf-8"
        )
    elif drift == "offset":
        input_path = Path(observation["input_path"])
        input_doc = json.loads(input_path.read_text(encoding="utf-8"))
        input_doc["source_media_timeline_offset_ms"] = 101
        _write_json(input_path, input_doc)
    elif drift == "response":
        response_path = Path(observation["response_path"])
        response_doc = json.loads(response_path.read_text(encoding="utf-8"))
        response_doc["heard_pinyin"] = "drifted response"
        _write_json(response_path, response_doc)
    elif drift == "audio":
        Path(observation["audio_clip_path"]).write_bytes(b"drifted audio")
    elif drift in {"result_mutation", "publication"}:
        result_path = Path(observation["result_path"])
        result_doc = json.loads(result_path.read_text(encoding="utf-8"))
        result_doc["srt_mutation"] = drift == "result_mutation"
        result_doc["publication"] = (
            "PUBLISHED" if drift == "publication" else "NOT_REQUESTED"
        )
        _write_json(result_path, result_doc)
    else:
        input_path = Path(observation["input_path"])
        input_doc = json.loads(input_path.read_text(encoding="utf-8"))
        input_doc["witness_request"] = build_witness_request(narrow)
        input_doc["witness_request_sha256"] = input_doc["witness_request"]["request_sha256"]
        _write_json(input_path, input_doc)

    validated, reason = validate_range_acoustic_observation(source, narrow, observation)
    assert validated is None
    assert isinstance(reason, str) and reason.startswith("RANGE_ACOUSTIC_")

    request = build_context_adjudication_request(
        source,
        {**finding, "_prior_range_acoustic_observations": [observation]},
        clip_context=_range_clip_context(),
        source_media_timeline_offset_ms=100,
    )
    assert "context_only_range_acoustic_observations" not in request
    assert build_witness_request(request) == build_witness_request(narrow)


def _sound_row(**changes):
    return {"kind":"environment_sound", "status":"OBSERVED", "start_ms":8000,
            "end_ms":8300,"description":"A brief hard impact after the phrase; object not established", **changes}


@pytest.mark.parametrize("narrow_status,narrow_audible", [("OBSERVED", True), ("OBSERVED", False), ("UNCERTAIN", False)])
def test_saved_sound_observation_reaches_candidate_and_editorial_consumers(tmp_path, monkeypatch, narrow_status, narrow_audible):
    monkeypatch.setenv('AUTOSLICE_BASE',str(tmp_path))
    source, finding, clip, narrow, observation, paths = _segmented_range_artifacts(
        tmp_path, audio_observations=[_sound_row()])
    before = {role:path.read_bytes() for role,path in paths.items()}
    finding = {**finding,'_prior_range_acoustic_observations':[json.loads(json.dumps(observation))]}
    request = build_context_adjudication_request(source,finding,clip_context=clip,source_media_timeline_offset_ms=100)
    assert 'not transcript words' in request['context_audio_observation_policy']
    row, = request['context_only_range_acoustic_observations']
    assert row['verdict']['audio_observations']==[_sound_row()]
    assert row['verdict']['exact_transcript']=='实际上半句实际下半句'
    assert build_witness_request(request)==build_witness_request(narrow)
    def propose(prompt):
        assert 'brief hard impact' in prompt and 'not speech-unit boundaries' in prompt
        return json.dumps({'status':'PROPOSED','proposed_cue':'实际句子','reason':'独立转写支持，环境声仅为语境'})
    rebuilt, audit = _rebuild_candidate_after_neither(finding=finding,request=request,witness={},llm_call=propose)
    assert rebuilt['proposed_full_cue']=='实际句子' and audit['mutation_authorized'] is False
    from src.autoslice.producer_package_finalization import _apply_exact_final_cpa_repairs
    from src.autoslice.editorial_subtitle import validate_repair
    def witness(req):
        return {'schema_version':'subtitle-span-acoustic-witness.v1','status':narrow_status,
                'request_sha256':req['request_sha256'],'target_audible':narrow_audible,'heard_pinyin':'mu qian ju zi' if narrow_audible else '',
                'source_media_sha256':'a'*64,'audio_clip_sha256':'b'*64}
    def judge(prompt):
        assert 'brief hard impact' in prompt and 'not transcript words' in prompt
        return json.dumps({'status':'RESOLVED','mode':'faithful_paraphrase','edit_start':0,'edit_end':4,
                           'replacement':'实际句子','reliable_fragments':[{'text':'实际句子','basis':'独立转写中的可靠含义'}],
                           'evidence_refs':['audio'],'meaning_basis':'整窗转写支持；环境声不决定字形',
                           'literal_uncertainty':'原稿误识','no_new_facts':True})
    pending, _ = adjudicate_exact_release_findings(source,[{**finding,'repair_class':'editorial_rendering',
            'proposed_full_cue':None}],entity_verifier=witness,judge_llm_call=judge,clip_context=clip,
            source_media_timeline_offset_ms=100)
    output, repairs = _apply_exact_final_cpa_repairs(source,{'findings':pending},clip_context=clip)
    assert '实际句子' in output and len(repairs)==1
    assert repairs[0]['editorial_authority']['request']['audio']['target_audible'] is narrow_audible
    validate_repair(json.loads(json.dumps(repairs[0])))
    assert {role:path.read_bytes() for role,path in paths.items()}==before
    # Changing only the response bytes invalidates both context acquisition and mutation replay.
    paths['raw_response'].write_text('{}')
    request2=build_context_adjudication_request(source,finding,clip_context=clip,source_media_timeline_offset_ms=100)
    assert 'context_only_range_acoustic_observations' not in request2
    with pytest.raises(ValueError, match='EDITORIAL_SOUND_CONTEXT_DRIFT'):
        validate_repair(repairs[0])


@pytest.mark.parametrize('change',[{'start_ms':-1},{'end_ms':99999},{'start_ms':True},
    {'kind':'exact_words'},{'status':'CONFIRMED_TRUTH'},{'description':''},{'canonical_entity':'Someone'}])
def test_sound_observation_bad_shape_or_crop_does_not_enter_context(tmp_path,change):
    source,finding,clip,narrow,observation,_ = _segmented_range_artifacts(tmp_path,audio_observations=[_sound_row(**change)])
    validated,reason=validate_range_acoustic_observation(source,narrow,observation)
    assert validated is None and reason=='SEGMENTED_RANGE_AUDIO_OBSERVATIONS_INVALID'


def test_uncertain_sound_note_cannot_replace_missing_narrow_audio(tmp_path):
    source, finding, clip, _, observation, _ = _segmented_range_artifacts(
        tmp_path,audio_observations=[_sound_row(status='UNCERTAIN')])
    finding={**finding,'repair_class':'editorial_rendering','proposed_full_cue':None,
             '_prior_range_acoustic_observations':[observation]}
    def unavailable(req):
        return {'schema_version':'subtitle-span-acoustic-witness.v1','request_sha256':req['request_sha256'],
                'status':'UNCERTAIN','target_audible':False,'heard_pinyin':''}
    pending,_=adjudicate_exact_release_findings(source,[finding],entity_verifier=unavailable,
        judge_llm_call=lambda _:pytest.fail('uncertain sound is not positive audio evidence'),
        clip_context=clip,source_media_timeline_offset_ms=100)
    assert pending[0]['exact_release_adjudication']['reason_code']=='EDITORIAL_AUDIO_UNAVAILABLE'
