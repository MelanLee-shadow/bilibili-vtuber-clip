import hashlib
import json
from copy import deepcopy
from pathlib import Path

from src.autoslice.final_review_auditor import build_context_adjudication_request
from src.autoslice.missing_proposal_bootstrap import bootstrap_missing_proposal
from src.autoslice.screen_read_witness import (
    validate_range_screen_context_observation,
    validate_screen_context_observation,
)


SRT = "1\n00:00:10,000 --> 00:00:12,000\n又不是你妈妈点个赞嘛\n"


def fixture(tmp_path):
    def artifact(name, data):
        path = tmp_path / name
        path.write_bytes(data)
        return {"path": str(path), "sha256": hashlib.sha256(data).hexdigest()}

    image = artifact("frame.png", b"immutable source image fixture")
    evidence = artifact("source-result.json", b"source derivation fixture")
    receipt = {"schema_version": "cpa-frame-witness.v1", "status": "OBSERVED",
               "image_sha256": image["sha256"], "answer": '{"texts":["SC：点 捏麻"]}'}
    result = artifact("vision-result.json", json.dumps({
        "status": "COMPLETED_EVIDENCE_ONLY", "receipt": receipt,
        "same_cue_direct_transcript_authority": False, "subtitle_mutation": False,
    }).encode())
    observation = {
        "schema_version": "screen-context-observation.v1", "authority": "EVIDENCE_ONLY",
        "mutation_authorized": False, "same_cue_direct_transcript_authority": False,
        "candidate_id": "clip", "cue_index": 1,
        "clip_context_sha256": "sha256:" + "a" * 64,
        "original_srt": artifact("original.srt", SRT.encode()),
        "related_source_media": artifact("source.mp4", b"immutable media fixture"),
        "source_media_timeline_offset_ms": 1000, "target_source_interval_ms": [11000, 13000],
        "earlier_source_interval_ms": [0, 10000],
        "observations": [{"related_source_frame_ms": 5000,
                          "frame_relation": "EXTERNAL_SOURCE_FRAME_CONTEXT_CANDIDATE",
                          "image": image, "receipt_result": result,
                          "source_evidence_artifacts": [evidence]}],
    }
    context = {"candidate_id": "clip", "context_sha256": observation["clip_context_sha256"]}
    finding = {"cue_index": 1, "suspect": "点个赞嘛", "suggestion": "点歌捏麻",
               "repair_class": "phonetic", "_prior_screen_context_observations": [observation]}
    return observation, context, finding


def test_normal_request_carries_real_earlier_context_without_text_authority(tmp_path):
    observation, context, finding = fixture(tmp_path)
    request = build_context_adjudication_request(SRT, finding, clip_context=context,
                                               source_media_timeline_offset_ms=1000)
    row = request["context_only_screen_observations"][0]
    assert row["observations"][0]["texts"] == ["SC：点 捏麻"]
    assert row["authority"] == "EVIDENCE_ONLY"
    assert row["same_cue_direct_transcript_authority"] is False
    assert row["mutation_authorized"] is False
    assert request["candidate_provenance"]["kind"] == "screen_context_evidence"
    assert request["orthography_authority"]["status"] != "PASS"
    assert observation["original_srt"]["sha256"] == row["original_srt_sha256"]


def test_changed_image_candidate_or_clock_does_not_enter_cpa_context(tmp_path):
    observation, context, finding = fixture(tmp_path)
    request = build_context_adjudication_request(SRT, finding, clip_context=context,
                                               source_media_timeline_offset_ms=1000)
    for change in [
        {"candidate_id": "other"}, {"target_source_interval_ms": [10000, 12000]},
        {"earlier_source_interval_ms": [10000, 14000]}, {"mutation_authorized": True},
        {"same_cue_direct_transcript_authority": True},
    ]:
        bad = {**observation, **change}
        row, reason = validate_screen_context_observation(SRT, request, bad)
        assert row is None and reason == "SCREEN_CONTEXT_BINDING_INVALID"
    bad = deepcopy(observation)
    bad["observations"][0]["image"]["sha256"] = "b" * 64
    assert validate_screen_context_observation(SRT, request, bad)[0] is None


def test_text_successor_uses_current_hash_only_when_source_grid_is_unchanged(tmp_path):
    observation, context, finding = fixture(tmp_path)
    changed = SRT.replace("点个赞嘛", "点歌捏麻")
    request = build_context_adjudication_request(changed,
        {**finding, "suspect": "点歌捏麻", "suggestion": "点个赞嘛"},
        clip_context=context, source_media_timeline_offset_ms=1000)
    row, _ = validate_screen_context_observation(changed, request, observation)
    assert row["current_srt_sha256"] == hashlib.sha256(changed.encode()).hexdigest()
    assert row["original_srt_sha256"] != row["current_srt_sha256"]
    drift = changed.replace("00:00:10,000", "00:00:10,250")
    assert validate_screen_context_observation(drift, request, observation)[0] is None


def test_bootstrap_prompt_and_cache_identity_include_separate_screen_context(tmp_path):
    observation, context, finding = fixture(tmp_path)
    request = build_context_adjudication_request(SRT, finding, clip_context=context,
                                               source_media_timeline_offset_ms=1000)
    prompts = []
    def llm(prompt):
        prompts.append(prompt)
        return '{"status":"UNRESOLVED","proposed_cue":"","reason":"evidence-only"}'
    args = dict(srt_text=SRT, finding={**finding, "suggestion": ""}, structured_chat="",
                llm_call=llm, derive_single_span_edit=lambda *a, **k: None)
    _, plain = bootstrap_missing_proposal(**args)
    _, with_screen = bootstrap_missing_proposal(**args,
        screen_context_observations=tuple(request["context_only_screen_observations"]))
    assert plain["prompt_sha256"] != with_screen["prompt_sha256"]
    assert "非结构化聊天记录" in prompts[-1] and "SC：点 捏麻" in prompts[-1]
    assert with_screen["screen_context_authority"] == "EVIDENCE_ONLY"


def _range_screen_fixture(tmp_path):
    current_srt = "1\n00:00:10,000 --> 00:00:12,000\n当前句\n"
    original_srt = "1\n00:00:10,000 --> 00:00:12,000\n旧句\n"

    def artifact(name, data):
        path = tmp_path / name
        path.write_bytes(data)
        return {"path": str(path), "sha256": hashlib.sha256(data).hexdigest()}

    original = artifact("original.srt", original_srt.encode("utf-8"))
    source = artifact("range-source.mp4", b"validated range source")
    image = artifact("frame-14000.jpg", b"bound extracted frame bytes")
    evidence = artifact("raw-screen-receipt.txt", b"raw OCR receipt evidence")
    raw_receipt = {
        "schema_version": "cpa-frame-witness.v1",
        "status": "OBSERVED",
        "frame_ms": 14000,
        "frame_sha256": image["sha256"],
        "media_path": source["path"],
        "answer": "OCR literal free text",
    }
    raw_receipt_artifact = artifact(
        "raw-frame-vision.receipt.json",
        json.dumps(raw_receipt, ensure_ascii=False, sort_keys=True).encode("utf-8"),
    )
    raw_result = artifact("raw-RESULT.json", b"immutable raw result bytes")
    receipt_result = artifact(
        "receipt-result.json",
        json.dumps({
            "status": "COMPLETED_EVIDENCE_ONLY",
            "same_cue_direct_transcript_authority": False,
            "subtitle_mutation": False,
            "raw_frame_sha256": image["sha256"],
            "raw_receipt_sha256": raw_receipt_artifact["sha256"],
            "raw_result_sha256": raw_result["sha256"],
            "receipt": raw_receipt,
        }, ensure_ascii=False, sort_keys=True).encode("utf-8"),
    )
    request = {
        "whole_clip_current_srt": current_srt,
        "cue_indexes": [1],
        "matched_start_ms": 10000,
        "matched_end_ms": 12000,
        "source_media_timeline_offset_ms": 1000,
        "clip_context_binding": {
            "candidate_id": "clip",
            "context_sha256": "sha256:" + "a" * 64,
            "source_media_sha256s": ["sha256:" + source["sha256"]],
        },
    }
    range_observation = {
        "schema_version": "range-acoustic-context-observation.v1",
        "status": "OBSERVED",
        "authority": "EVIDENCE_ONLY",
        "mutation_authorized": False,
        "candidate_id": "clip",
        "current_cue_index": 1,
        "witness_request": {
            "cue_indexes": [1],
            "context_start_ms": 7000,
            "context_end_ms": 13000,
            "source_media_timeline_offset_ms": 1000,
        },
        "verdict": {
            "audio_start_ms": 9000,
            "audio_end_ms": 15000,
            "source_media_sha256": source["sha256"],
        },
        "artifact_bindings": {
            "source_media_path": source["path"],
            "source_media_sha256": source["sha256"],
            "reviewed_srt_sha256": hashlib.sha256(current_srt.encode("utf-8")).hexdigest(),
        },
        "source_window": {
            "target_start_ms": 11000,
            "target_end_ms": 13000,
            "crop_start_ms": 9000,
            "crop_end_ms": 15000,
        },
        "cue_texts": [{"cue_index": 1, "start_ms": 10000, "end_ms": 12000}],
    }
    observation = {
        "schema_version": "range-screen-context-observation.v1",
        "authority": "EVIDENCE_ONLY",
        "mutation_authorized": False,
        "same_cue_direct_transcript_authority": False,
        "candidate_id": "clip",
        "clip_context_sha256": "sha256:" + "a" * 64,
        "cue_index": 1,
        "original_srt": original,
        "related_source_media": source,
        "source_media_timeline_offset_ms": 1000,
        "target_source_interval_ms": [11000, 13000],
        "range_source_interval_ms": [9000, 15000],
        "observations": [{
            "related_source_frame_ms": 14000,
            "frame_relation": "AFTER_TARGET_WITHIN_RANGE_CONTEXT",
            "image": image,
            "receipt_result": receipt_result,
            "source_evidence_artifacts": [evidence, raw_receipt_artifact, raw_result],
        }],
    }
    return current_srt, request, range_observation, observation


def test_range_screen_context_accepts_late_frame_and_text_only_successor(tmp_path):
    srt, request, range_observation, observation = _range_screen_fixture(tmp_path)
    before = json.dumps(observation, ensure_ascii=False, sort_keys=True)
    result, reason = validate_range_screen_context_observation(
        srt, request, range_observation, observation,
    )
    assert reason == "RANGE_SCREEN_CONTEXT_VALIDATED_EVIDENCE_ONLY"
    assert result is not None
    assert result["scope"] == "RANGE_SOURCE_SCREEN_CONTEXT_ONLY"
    assert result["authority"] == "EVIDENCE_ONLY"
    assert result["mutation_authorized"] is False
    assert result["same_cue_direct_transcript_authority"] is False
    assert result["current_srt_sha256"] == hashlib.sha256(srt.encode()).hexdigest()
    assert result["original_srt_sha256"] != result["current_srt_sha256"]
    checked = result["observations"][0]
    assert checked["related_source_frame_ms"] == 14000
    assert checked["frame_relation"] == "WHEN_CONTAINED_IN_RANGE"
    assert checked["texts"] == ["OCR literal free text"]
    assert checked["receipt"]["answer"] == "OCR literal free text"
    assert result["original_observation_sha256"] == hashlib.sha256(
        json.dumps(observation, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    assert json.dumps(observation, ensure_ascii=False, sort_keys=True) == before

    native_like_range = deepcopy(range_observation)
    native_like_range.pop("source_window")
    native_result, native_reason = validate_range_screen_context_observation(
        srt, request, native_like_range, observation,
    )
    assert native_reason == "RANGE_SCREEN_CONTEXT_VALIDATED_EVIDENCE_ONLY"
    assert native_result is not None
    assert native_result["range_source_interval_ms"] == [9000, 15000]


def test_range_screen_replays_carryover_and_rejects_a_relabelled_receipt(tmp_path, monkeypatch):
    from src.autoslice import independent_context_adjudication as context

    srt, request, original_range, observation = _range_screen_fixture(tmp_path)
    original_range["artifact_bindings"]["reviewed_srt_sha256"] = observation["original_srt"]["sha256"]
    carried = {
        **deepcopy(original_range), "schema_version": "range-acoustic-context-carryover.v1",
        "status": "CONTEXT_ONLY", "context_only": True,
        "current_srt_sha256": hashlib.sha256(srt.encode()).hexdigest(),
        "original_reviewed_srt_sha256": observation["original_srt"]["sha256"],
        "original_range_observation": original_range,
    }
    calls = []

    def replay(current, narrow, original):
        calls.append((current, narrow, original))
        assert original is original_range
        return deepcopy(carried), None

    monkeypatch.setattr(context, "adapt_range_acoustic_context_observation", replay)
    before = json.dumps(observation, sort_keys=True)
    accepted, reason = validate_range_screen_context_observation(srt, request, carried, observation)
    assert reason == "RANGE_SCREEN_CONTEXT_VALIDATED_EVIDENCE_ONLY"
    assert accepted["current_srt_sha256"] == hashlib.sha256(srt.encode()).hexdigest()
    assert accepted["mutation_authorized"] is False
    assert len(calls) == 1
    forged = deepcopy(carried)
    forged["verdict"]["audio_end_ms"] += 100
    # Preserve the original reference used by the replay stand-in.
    forged["original_range_observation"] = original_range
    rejected, reason = validate_range_screen_context_observation(srt, request, forged, observation)
    assert rejected is None and reason == "RANGE_SCREEN_CONTEXT_CARRYOVER_BINDING_INVALID"
    monkeypatch.setattr(context, "adapt_range_acoustic_context_observation", lambda *_a: (None, "SOURCE_DRIFT"))
    rejected, reason = validate_range_screen_context_observation(srt, request, carried, observation)
    assert rejected is None and reason == "RANGE_SCREEN_CONTEXT_CARRYOVER_BINDING_INVALID"
    assert json.dumps(observation, sort_keys=True) == before


def test_range_screen_context_rejects_source_frame_candidate_span_grid_and_receipt_drift(tmp_path):
    srt, request, range_observation, observation = _range_screen_fixture(tmp_path)
    cases = []

    bad_source = deepcopy(observation)
    bad_source["related_source_media"]["sha256"] = "f" * 64
    cases.append((request, range_observation, bad_source, "RANGE_SCREEN_CONTEXT_SOURCE_MEDIA_HASH_DRIFT"))

    bad_frame = deepcopy(observation)
    bad_frame["observations"][0]["related_source_frame_ms"] = 15000
    cases.append((request, range_observation, bad_frame, "RANGE_SCREEN_CONTEXT_FRAME_OUTSIDE_RANGE"))

    bad_earlier = deepcopy(observation)
    bad_earlier["observations"][0]["related_source_frame_ms"] = 10000
    cases.append((request, range_observation, bad_earlier, "RANGE_SCREEN_CONTEXT_EARLIER_ROLE"))

    bad_candidate = deepcopy(observation)
    bad_candidate["candidate_id"] = "other"
    cases.append((request, range_observation, bad_candidate, "RANGE_SCREEN_CONTEXT_CANDIDATE_DRIFT"))

    bad_span = deepcopy(observation)
    bad_span["range_source_interval_ms"] = [9001, 15000]
    cases.append((request, range_observation, bad_span, "RANGE_SCREEN_CONTEXT_RANGE_GEOMETRY_DRIFT"))

    bad_grid_request = {**request, "matched_start_ms": 10001}
    cases.append((bad_grid_request, range_observation, observation,
                  "RANGE_SCREEN_CONTEXT_CURRENT_CUE_GRID_DRIFT"))

    bad_receipt = deepcopy(observation)
    bad_receipt["observations"][0]["receipt_result"]["sha256"] = "0" * 64
    cases.append((request, range_observation, bad_receipt,
                  "RANGE_SCREEN_CONTEXT_RECEIPT_RESULT_HASH_DRIFT"))

    forged_wrapper = deepcopy(observation)
    wrapper_path = tmp_path / "forged-receipt-result.json"
    wrapper = json.loads(Path(observation["observations"][0]["receipt_result"]["path"]).read_text())
    wrapper["receipt"]["answer"] = '{"texts":["fabricated"]}'
    wrapper_bytes = json.dumps(wrapper, ensure_ascii=False, sort_keys=True).encode("utf-8")
    wrapper_path.write_bytes(wrapper_bytes)
    forged_wrapper["observations"][0]["receipt_result"] = {
        "path": str(wrapper_path),
        "sha256": hashlib.sha256(wrapper_bytes).hexdigest(),
    }
    cases.append((request, range_observation, forged_wrapper,
                  "RANGE_SCREEN_CONTEXT_RAW_RECEIPT_DRIFT"))

    bad_other_cue = deepcopy(observation)
    bad_other_cue["cue_index"] = 2
    cases.append((request, range_observation, bad_other_cue,
                  "RANGE_SCREEN_CONTEXT_OTHER_CUE"))

    for current_request, audio_range, candidate, expected in cases:
        result, reason = validate_range_screen_context_observation(
            srt, current_request, audio_range, candidate,
        )
        assert result is None and reason == expected


def test_range_screen_context_rejects_earlier_relation_even_inside_range(tmp_path):
    srt, request, range_observation, observation = _range_screen_fixture(tmp_path)
    earlier = deepcopy(observation)
    earlier["observations"][0]["frame_relation"] = "SAME_MEDIA_EARLIER_FRAME"
    result, reason = validate_range_screen_context_observation(
        srt, request, range_observation, earlier,
    )
    assert result is None and reason == "RANGE_SCREEN_CONTEXT_EARLIER_ROLE"
