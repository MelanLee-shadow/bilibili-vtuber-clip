from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.autoslice import producer_native_witness as routing_module
from src.autoslice import producer_text_pipeline
from src.autoslice.producer_native_witness import (
    bind_native_attempt_recorder,
    finalize_audio_witness_routing,
    finish_audio_witness_routing,
)
from src.autoslice.read_aloud_llm_verifier import build_cpa_read_aloud_verifier


HEX = {
    "parent": "a" * 64,
    "source": "1" * 64,
    "audio": "2" * 64,
    "response": "3" * 64,
    "native": "4" * 64,
}


def _routing(*, local: str = "mai", foreign: str = "moss") -> dict[str, object]:
    return {
        "schema_version": "producer-audio-witness-routing.v1",
        "status": "PLANNED",
        "candidate_id": "dispatch-binding",
        "policy": {
            "text_decision_authority": "CPA",
            "dispatch_decision_policy": "CPA_ONLY_UNTIL_JEV_PRODUCTION_PROMOTION",
            "jev_production_status": "NOT_PROMOTED",
        },
        "stages": {
            "local_entity": {
                "call_gate": "CPA_TEXT_FIRST_NEEDS_AUDIO_TRUE",
                "selected_provider": local,
            },
            "foreign_script": {
                "call_gate": "MIXED_SCRIPT_BLOCK_THEN_CPA_FINAL_ADJUDICATION",
                "selected_provider": foreign,
            },
        },
        "observed_consumption": [],
    }


def _entity_request() -> dict[str, object]:
    return {
        "schema_version": "transcript-entity-verification-request.v1",
        "request_sha256": "sha256:" + HEX["parent"],
        "evidence_id": "entity-li-mo",
        "kind": "transcript_entity",
        "cue_indexes": [1],
        "matched_start_ms": 5_000,
        "matched_end_ms": 9_000,
        "context_start_ms": 3_500,
        "context_end_ms": 10_500,
        "source_media_timeline_offset_ms": 0,
        "matched_audio_text": "琳墨",
        "transcript_canonical": "俪墨",
        "transcript_surface": "琳墨",
        "context_before": "她刚才在点名",
        "context_after": "随后继续回答",
        "candidate_entities": [
            {"canonical": "俪墨", "surfaces": ["俪墨"]},
            {"canonical": "琳墨", "surfaces": ["琳墨"]},
        ],
    }


def _native_audio_callback(provider: str = "mai"):
    def observe(request: dict[str, object]) -> dict[str, object]:
        return {
            "schema_version": "subtitle-span-acoustic-witness.v1",
            "witness_protocol": "candidate_blind_transcript",
            "request_sha256": request["request_sha256"],
            "status": "OBSERVED",
            "target_audible": True,
            "candidate_exposure": "none",
            "authority": "EVIDENCE_ONLY",
            "mutation_authorized": False,
            "exact_transcript": "li mo",
            "source_media_sha256": HEX["source"],
            "audio_clip_sha256": HEX["audio"],
            "audio_start_ms": 3_500,
            "audio_end_ms": 10_500,
            "provider": provider,
            "model": "MAI-Transcribe-2" if provider == "mai" else "MOSS-Audio-Tokenizer",
            "response_sha256": HEX["response"],
            "native_observation": {
                "receipt_sha256": HEX["native"],
                "provider": provider,
                "served_from_cache": False,
            },
        }

    return observe


def _text_first_native_verdict(provider: str = "mai") -> dict[str, object]:
    calls = 0

    def judge(_prompt: str) -> str:
        nonlocal calls
        calls += 1
        return json.dumps(
            {
                "ranking": [
                    {"canonical": "俪墨", "p": 0.72},
                    {"canonical": "琳墨", "p": 0.28},
                ],
                "choice": "俪墨",
                "needs_audio": calls == 1,
                "reason": "先文字消歧不足，局部音频后仍由 CPA 闭集裁决",
            },
            ensure_ascii=False,
        )

    verdict = build_cpa_read_aloud_verifier(
        judge,
        next_verifier=_native_audio_callback(provider),
    )(_entity_request())
    assert verdict is not None
    assert calls == 2
    return verdict


def test_text_first_cpa_native_call_is_bound_to_dispatch_and_route() -> None:
    verdict = _text_first_native_verdict()

    assert verdict["text_first_judge"]["needs_audio"] is True
    witness = verdict["acoustic_witness"]
    assert witness["provider"] == "mai"
    assert witness["authority"] == "EVIDENCE_ONLY"
    assert witness["mutation_authorized"] is False
    assert "exact_transcript" not in witness

    finalized = finalize_audio_witness_routing(
        _routing(),
        {
            "entity_repairs": [
                {
                    "request_sha256": _entity_request()["request_sha256"],
                    "cue_indexes": [1],
                    "verdict": verdict,
                }
            ]
        },
    )

    assert finalized["status"] == "CONSUMED"
    assert finalized["valid_consumption_count"] == 1
    assert finalized["invalid_consumption_count"] == 0
    row = finalized["observed_consumption"][0]
    assert row["stage"] == "local_entity"
    assert row["provider"] == "mai"
    assert row["dispatch_authority"] == "CPA_JUDGE"
    assert row["dispatch_request_binding"] == "WITNESS_REQUEST_SHA256"
    assert row["dispatch_gate_valid"] is True
    assert row["evidence_binding_valid"] is True
    assert row["observed_route_match"] is True
    assert row["consumption_valid"] is True
    assert row["post_audio_decision_authority"] == "CPA_JUDGE"


def test_native_evidence_without_predispatch_decision_is_invalid() -> None:
    verdict = _text_first_native_verdict()
    verdict.pop("text_first_judge")

    finalized = finalize_audio_witness_routing(
        _routing(),
        {
            "entity_repairs": [
                {
                    "request_sha256": _entity_request()["request_sha256"],
                    "cue_indexes": [1],
                    "verdict": verdict,
                }
            ]
        },
    )

    assert finalized["status"] == "INVALID_NATIVE_CONSUMPTION"
    row = finalized["observed_consumption"][0]
    assert row["evidence_binding_valid"] is True
    assert row["dispatch_gate_valid"] is False
    assert row["dispatch_reason_codes"] == ["DISPATCH_DECISION_MISSING"]
    assert row["consumption_valid"] is False


def test_text_first_decision_that_rejected_audio_cannot_authorize_native_call() -> None:
    verdict = _text_first_native_verdict()
    verdict["text_first_judge"]["needs_audio"] = False

    finalized = finalize_audio_witness_routing(
        _routing(),
        {
            "entity_repairs": [
                {
                    "request_sha256": _entity_request()["request_sha256"],
                    "cue_indexes": [1],
                    "verdict": verdict,
                }
            ]
        },
    )

    assert finalized["status"] == "INVALID_NATIVE_CONSUMPTION"
    assert "DISPATCH_DECISION_DID_NOT_REQUEST_AUDIO" in finalized[
        "observed_consumption"
    ][0]["dispatch_reason_codes"]


def test_provider_route_mismatch_is_invalid_even_with_valid_evidence() -> None:
    verdict = _text_first_native_verdict(provider="moss")

    finalized = finalize_audio_witness_routing(
        _routing(local="mai"),
        {
            "entity_repairs": [
                {
                    "request_sha256": _entity_request()["request_sha256"],
                    "cue_indexes": [1],
                    "verdict": verdict,
                }
            ]
        },
    )

    assert finalized["status"] == "INVALID_NATIVE_CONSUMPTION"
    row = finalized["observed_consumption"][0]
    assert row["provider"] == "moss"
    assert row["observed_route_match"] is False
    assert row["consumption_valid"] is False


def test_final_review_nested_native_call_is_collected_once() -> None:
    verdict = _text_first_native_verdict()
    authority = {
        "final_review_audit": {
            "findings": [
                {
                    "request_sha256": _entity_request()["request_sha256"],
                    "cue_indexes": [7],
                    "verdict": verdict,
                }
            ]
        }
    }

    finalized = finalize_audio_witness_routing(_routing(), authority)

    assert finalized["status"] == "CONSUMED"
    assert len(finalized["observed_consumption"]) == 1
    row = finalized["observed_consumption"][0]
    assert row["origin"].startswith("final_review_audit.findings")
    assert row["cue_indexes"] == [7]


def _foreign_authority(*, include_detector: bool = True, end_ms: int = 8_000):
    detector = {
        "cue_index": 3,
        "start_ms": 5_000,
        "end_ms": 8_000,
        "text": "English phrase",
    }
    audit: dict[str, object] = {
        "audio_witness_rows": [
            {
                "cue_index": 3,
                "start_ms": 5_000,
                "end_ms": end_ms,
                "provider": "moss",
                "model": "MOSS-Audio-Tokenizer",
                "audio_sha256": HEX["audio"],
                "response_sha256": HEX["response"],
                "source_media_sha256": HEX["source"],
                "served_from_cache": True,
                "witness_native_evidence": {
                    "schema_version": "native-audio-observation.v1",
                    "status": "OBSERVED",
                    "provider": "moss",
                    "model": "MOSS-Audio-Tokenizer",
                    "source_media_sha256": HEX["source"],
                    "input_audio_sha256": HEX["audio"],
                    "response_sha256": HEX["response"],
                    "target_start_ms": 5_000,
                    "target_end_ms": end_ms,
                    "candidate_exposure": "none",
                    "authority": "EVIDENCE_ONLY",
                    "mutation_authorized": False,
                    "served_from_cache": True,
                    "receipt_sha256": HEX["native"],
                },
            }
        ],
        "cpa_adjudication_rows": [
            {
                "cue_index": 3,
                "resolved": True,
                "decision_authority": "CPA_JUDGE",
                "choice": "CURRENT",
            }
        ],
    }
    if include_detector:
        audit["mixed_cjk_latin_cues"] = [detector]
    return {"foreign_script_consistency_audit": audit}


def test_foreign_native_call_binds_to_deterministic_detector_and_cache_reuse() -> None:
    finalized = finalize_audio_witness_routing(_routing(), _foreign_authority())

    assert finalized["status"] == "CONSUMED"
    row = finalized["observed_consumption"][0]
    assert row["stage"] == "foreign_script"
    assert row["dispatch_authority"] == "DETERMINISTIC_MIXED_SCRIPT_GATE"
    assert row["dispatch_request_binding"] == "CUE_INDEX_AND_GEOMETRY"
    assert row["transport_observation_kind"] == "CACHE_REUSE"
    assert row["post_audio_decision_authority"] == "CPA_JUDGE"
    assert row["consumption_valid"] is True


def test_foreign_native_call_without_matching_detector_is_invalid() -> None:
    for authority in (
        _foreign_authority(include_detector=False),
        _foreign_authority(end_ms=8_100),
    ):
        finalized = finalize_audio_witness_routing(_routing(), authority)
        assert finalized["status"] == "INVALID_NATIVE_CONSUMPTION"
        row = finalized["observed_consumption"][0]
        assert row["dispatch_gate_valid"] is False
        assert row["consumption_valid"] is False


def test_configuration_without_native_evidence_is_not_consumption() -> None:
    finalized = finalize_audio_witness_routing(_routing(), {})

    assert finalized["status"] == "NO_NATIVE_CALL_CONSUMED"
    assert finalized["observed_consumption"] == []
    assert finalized["valid_consumption_count"] == 0
    assert finalized["invalid_consumption_count"] == 0


def test_weak_read_aloud_context_has_typed_cpa_dispatch_before_native_call() -> None:
    request = {
        "schema_version": "chat-read-aloud-verification-request.v1",
        "request_sha256": "sha256:" + HEX["parent"],
        "evidence_id": "weak-read-aloud",
        "kind": "danmaku",
        "exact_text": "外套是什么颜色",
        "matched_audio_text": "歪了是什么颜色",
        "context_before": "刚才有人问",
        "context_after": "是黑色的",
        "cue_indexes": [2],
        "matched_start_ms": 0,
        "matched_end_ms": 2_000,
        "context_start_ms": 0,
        "context_end_ms": 3_500,
        "source_media_timeline_offset_ms": 0,
        "candidate_entities": [
            {"canonical": "外套是什么颜色"},
            {"canonical": "歪了是什么颜色"},
        ],
    }
    calls = 0

    def judge(_prompt: str) -> str:
        nonlocal calls
        calls += 1
        if calls == 1:
            return json.dumps(
                {"is_read_aloud": True, "confidence": 0.51},
                ensure_ascii=False,
            )
        return json.dumps(
            {
                "ranking": [
                    {"canonical": "外套是什么颜色", "p": 0.8},
                    {"canonical": "歪了是什么颜色", "p": 0.2},
                ],
                "choice": "外套是什么颜色",
                "reason": "候选盲局部转写与问答语境共同支持",
            },
            ensure_ascii=False,
        )

    def native(witness_request: dict[str, object]) -> dict[str, object]:
        return {
            "schema_version": "subtitle-span-acoustic-witness.v1",
            "witness_protocol": "candidate_blind_transcript",
            "request_sha256": witness_request["request_sha256"],
            "status": "OBSERVED",
            "target_audible": True,
            "candidate_exposure": "none",
            "authority": "EVIDENCE_ONLY",
            "mutation_authorized": False,
            "exact_transcript": "wai tao shi shen me yan se",
            "source_media_sha256": HEX["source"],
            "audio_clip_sha256": HEX["audio"],
            "audio_start_ms": 0,
            "audio_end_ms": 3_500,
            "provider": "mai",
            "model": "MAI-Transcribe-2",
            "response_sha256": HEX["response"],
            "served_from_cache": True,
            "native_observation": {
                "receipt_sha256": HEX["native"],
                "served_from_cache": True,
            },
        }

    verdict = build_cpa_read_aloud_verifier(judge, next_verifier=native)(request)
    assert verdict is not None
    assert calls == 2
    assert verdict["audio_dispatch_decision"]["decision_authority"] == (
        "CPA_CONTEXT_POLICY"
    )
    assert verdict["audio_dispatch_decision"]["needs_audio"] is True
    assert verdict["acoustic_witness"]["audio_start_ms"] == 0
    assert verdict["acoustic_witness"]["served_from_cache"] is True

    finalized = finalize_audio_witness_routing(
        _routing(),
        {
            "read_aloud_arbitrations": [
                {
                    "request_sha256": request["request_sha256"],
                    "cue_indexes": [2],
                    "verdict": verdict,
                }
            ]
        },
    )
    assert finalized["status"] == "CONSUMED"
    row = finalized["observed_consumption"][0]
    assert row["dispatch_authority"] == "CPA_CONTEXT_POLICY"
    assert row["dispatch_request_binding"] == "PARENT_REQUEST_SHA256"
    assert row["transport_observation_kind"] == "CACHE_REUSE"
    assert row["audio_start_ms"] == 0
    assert row["consumption_valid"] is True


def test_ordinary_text_pipeline_writes_bound_consumption_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    padded = tmp_path / "source-window.mp4"
    padded.write_bytes(b"bound source window")
    out_root = tmp_path / "out"
    out_root.mkdir()
    cid = "ordinary-routing-consumer"
    srt_text = (
        "1\n00:00:00,000 --> 00:00:01,000\n开场\n\n"
        "2\n00:00:01,000 --> 00:00:02,000\n发展\n\n"
        "3\n00:00:02,000 --> 00:00:03,000\n收束\n"
    )
    verdict = _text_first_native_verdict()
    authority_audit = {
        "status": "PASS",
        "entity_repairs": [
            {
                "request_sha256": _entity_request()["request_sha256"],
                "cue_indexes": [1],
                "verdict": verdict,
            }
        ],
    }
    spec = {
        "candidate_id": cid,
        "date": "2026-09-25",
        "selection_hook": "ordinary routing consumer",
        "pieces": [],
        "human_truth_mode": "withheld",
    }

    monkeypatch.setattr(
        routing_module,
        "_provider_configuration",
        lambda provider, _environ: {
            "configured": provider == "mai",
            "reason_code": "CONFIGURED" if provider == "mai" else "MISSING",
        },
    )
    monkeypatch.setattr(
        producer_text_pipeline,
        "_collect_timeline_chat",
        lambda _spec, _durations: ([], []),
    )
    monkeypatch.setattr(
        producer_text_pipeline,
        "build_env_screen_read_probe",
        lambda _padded: None,
    )
    monkeypatch.setattr(
        producer_text_pipeline,
        "_transcribe_draft",
        lambda **_kwargs: SimpleNamespace(
            srt_text=srt_text,
            support_srts=[],
            code_switch_audit={},
            term_boundary_moves=[],
            session_topic_absorption_audits=[],
            song_name_candidates=[],
            session_topic_authorities=(),
            source_language_witness_srt=srt_text,
            vad_span_consumption={"status": "PASS"},
            spans=[(0, 3_000)],
            transcriber=lambda *_args, **_kwargs: srt_text,
        ),
    )
    monkeypatch.setattr(
        producer_text_pipeline,
        "apply_source_subtitle_truth",
        lambda text, **_kwargs: (text, {"status": "PASS", "windows": []}),
    )
    monkeypatch.setattr(
        producer_text_pipeline,
        "build_source_truth_preview_receipt",
        lambda **_kwargs: {"status": "PASS", "protected_cue_indexes": []},
    )
    monkeypatch.setattr(
        producer_text_pipeline,
        "_build_entity_verification_context",
        lambda **_kwargs: SimpleNamespace(
            referent_groups=[],
            verify_confusable_entity=lambda _request: None,
            topic_resolution_audit={"status": "PASS"},
        ),
    )
    monkeypatch.setattr(
        producer_text_pipeline,
        "build_clip_context",
        lambda **_kwargs: {"schema_version": "clip-context.v1", "candidate_id": cid},
    )
    monkeypatch.setattr(producer_text_pipeline, "write_clip_context", lambda *_a, **_k: None)
    monkeypatch.setattr(
        producer_text_pipeline,
        "ledger_required_owner_contracts",
        lambda **_kwargs: [],
    )
    monkeypatch.setattr(
        producer_text_pipeline,
        "_redelivery_baseline_boundary_owner",
        lambda _spec, _durations: [],
    )
    monkeypatch.setattr(
        producer_text_pipeline,
        "resolve_truth_full_ownership",
        lambda _spec: {"status": "PASS", "owner": "test"},
    )
    monkeypatch.setattr(
        producer_text_pipeline,
        "_apply_entity_authority",
        lambda **_kwargs: SimpleNamespace(
            srt_text=srt_text,
            chat_authority_audit=authority_audit,
            transcript_entity_audit={"status": "PASS"},
            handled_entity_cues=set(),
        ),
    )
    monkeypatch.setattr(
        producer_text_pipeline,
        "build_structured_chat_binding_audit",
        lambda *_args, **_kwargs: {"status": "PASS"},
    )
    monkeypatch.setattr(
        producer_text_pipeline,
        "_pinned_replay_reviewed_text_ownership",
        lambda _spec: None,
    )
    monkeypatch.setattr(
        producer_text_pipeline,
        "skipped_final_review_audit",
        lambda *_args, **_kwargs: {"status": "SKIPPED", "infra_unresolved": []},
    )
    chat_authority_path = out_root / f"{cid}.chat-authority.json"
    monkeypatch.setattr(
        producer_text_pipeline,
        "_finalize_text_evidence",
        lambda **_kwargs: SimpleNamespace(
            srt_text=srt_text,
            cues=["cue-1", "cue-2", "cue-3"],
            chat_authority_path=chat_authority_path,
        ),
    )
    monkeypatch.setattr(
        producer_text_pipeline,
        "freeze_required_boundary_owner_contract",
        lambda **_kwargs: (3_000, {"status": "PASS"}),
    )
    monkeypatch.setattr(
        producer_text_pipeline,
        "review_source_boundary",
        lambda **_kwargs: (
            {"status": "PASS"},
            {"status": "PASS", "review_scope": "source_full_window"},
            None,
            None,
        ),
    )
    monkeypatch.setattr(
        producer_text_pipeline,
        "persist_review_audit",
        lambda *_args, **_kwargs: None,
    )
    adapters = SimpleNamespace(
        profile_asset_file=lambda name: tmp_path / f"{name}.json"
    )

    result = producer_text_pipeline.run_text_pipeline(
        spec=spec,
        durations=[3_000],
        padded=padded,
        padded_dur=3_000,
        host="localhost",
        text_override_path=None,
        cid=cid,
        out_root=out_root,
        substrate="aggregate_asr",
        correct="cpa",
        screen_text=False,
        adapters=adapters,
    )

    receipt_path = out_root / f"{cid}.audio-witness-routing.json"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert result.srt_text == srt_text
    assert receipt["status"] == "CONSUMED"
    assert receipt["valid_consumption_count"] == 1
    assert receipt["invalid_consumption_count"] == 0
    assert receipt["observed_consumption"][0]["consumption_valid"] is True
    assert receipt["observed_consumption"][0]["provider"] == "mai"
    assert receipt["policy"]["jev_production_status"] == "NOT_PROMOTED"


def test_finish_persists_invalid_receipt_then_blocks_delivery(tmp_path: Path) -> None:
    verdict = _text_first_native_verdict()
    verdict.pop("text_first_judge")
    authority = {
        "entity_repairs": [
            {
                "request_sha256": _entity_request()["request_sha256"],
                "cue_indexes": [1],
                "verdict": verdict,
            }
        ]
    }
    path = tmp_path / "invalid.audio-witness-routing.json"

    with pytest.raises(
        SystemExit,
        match="INVALID_NATIVE_AUDIO_WITNESS_CONSUMPTION",
    ):
        finish_audio_witness_routing(_routing(), path, authority)

    persisted = json.loads(path.read_text(encoding="utf-8"))
    assert persisted["status"] == "INVALID_NATIVE_CONSUMPTION"
    assert persisted["invalid_consumption_count"] == 1
    assert authority["audio_witness_routing"]["status"] == (
        "INVALID_NATIVE_CONSUMPTION"
    )


def test_finish_without_native_call_is_a_normal_nonblocking_result(tmp_path: Path) -> None:
    authority: dict[str, object] = {}
    path = tmp_path / "no-call.audio-witness-routing.json"

    finalized = finish_audio_witness_routing(_routing(), path, authority)

    assert finalized["status"] == "NO_NATIVE_CALL_CONSUMED"
    assert json.loads(path.read_text(encoding="utf-8"))["status"] == (
        "NO_NATIVE_CALL_CONSUMED"
    )
    assert authority["audio_witness_routing"]["status"] == (
        "NO_NATIVE_CALL_CONSUMED"
    )


@pytest.mark.parametrize(
    ("field", "reason"),
    [
        ("model", "NATIVE_MODEL_IDENTITY_MISSING"),
        ("served_from_cache", "NATIVE_CACHE_STATE_MISSING"),
    ],
)
def test_native_evidence_requires_model_and_cache_state(field: str, reason: str) -> None:
    verdict = _text_first_native_verdict()
    verdict["acoustic_witness"].pop(field)

    finalized = finalize_audio_witness_routing(
        _routing(),
        {
            "entity_repairs": [
                {
                    "request_sha256": _entity_request()["request_sha256"],
                    "cue_indexes": [1],
                    "verdict": verdict,
                }
            ]
        },
    )

    assert finalized["status"] == "INVALID_NATIVE_CONSUMPTION"
    row = finalized["observed_consumption"][0]
    assert reason in row["evidence_reason_codes"]
    assert row["post_audio_gate_valid"] is True
    assert row["consumption_valid"] is False


def test_local_native_evidence_without_post_audio_cpa_final_is_invalid() -> None:
    verdict = _text_first_native_verdict()
    verdict.pop("decision_authority")

    finalized = finalize_audio_witness_routing(
        _routing(),
        {
            "entity_repairs": [
                {
                    "request_sha256": _entity_request()["request_sha256"],
                    "cue_indexes": [1],
                    "verdict": verdict,
                }
            ]
        },
    )

    assert finalized["status"] == "INVALID_NATIVE_CONSUMPTION"
    row = finalized["observed_consumption"][0]
    assert row["dispatch_gate_valid"] is True
    assert row["evidence_binding_valid"] is True
    assert row["post_audio_gate_valid"] is False
    assert "POST_AUDIO_CPA_AUTHORITY_MISSING" in row["post_audio_reason_codes"]


def test_foreign_native_evidence_without_resolved_cpa_final_is_invalid() -> None:
    authority = _foreign_authority()
    authority["foreign_script_consistency_audit"]["cpa_adjudication_rows"][0][
        "resolved"
    ] = False

    finalized = finalize_audio_witness_routing(_routing(), authority)

    assert finalized["status"] == "INVALID_NATIVE_CONSUMPTION"
    row = finalized["observed_consumption"][0]
    assert row["dispatch_gate_valid"] is True
    assert row["evidence_binding_valid"] is True
    assert row["post_audio_gate_valid"] is False
    assert "POST_AUDIO_CPA_NOT_RESOLVED" in row["post_audio_reason_codes"]


def test_runtime_attempt_recorder_is_text_free_and_preserves_verifier_seams() -> None:
    routing = _routing()

    def probe(_request):
        return {"status": "MISS"}

    verifier = _native_audio_callback()
    verifier.probe_witness_cache = probe
    wrapped = bind_native_attempt_recorder(
        verifier,
        routing,
        stage="local_entity",
    )
    request = {
        "request_sha256": "sha256:" + "b" * 64,
        "cue_indexes": [1],
    }

    evidence = wrapped(request)

    assert evidence["exact_transcript"] == "li mo"
    assert wrapped.probe_witness_cache is probe
    attempts = routing["runtime_native_attempts"]
    assert len(attempts) == 1
    receipt = attempts[0]["acoustic_witness"]
    assert receipt["provider"] == "mai"
    assert receipt["request_sha256"] == request["request_sha256"]
    assert receipt["served_from_cache"] is False
    assert "exact_transcript" not in receipt
    assert "text" not in receipt


def test_orphan_runtime_native_attempt_is_invalid_and_cannot_disappear() -> None:
    routing = _routing()
    wrapped = bind_native_attempt_recorder(
        _native_audio_callback(),
        routing,
        stage="local_entity",
    )
    wrapped(
        {
            "request_sha256": "sha256:" + "b" * 64,
            "cue_indexes": [1],
        }
    )

    finalized = finalize_audio_witness_routing(routing, {})

    assert finalized["status"] == "INVALID_NATIVE_CONSUMPTION"
    assert len(finalized["observed_consumption"]) == 1
    row = finalized["observed_consumption"][0]
    assert row["origin"] == "audio_witness_routing.runtime_native_attempts"
    assert row["evidence_binding_valid"] is True
    assert row["dispatch_gate_valid"] is False
    assert row["post_audio_gate_valid"] is False
    assert row["consumption_valid"] is False


def test_bound_chat_audit_deduplicates_matching_runtime_attempt() -> None:
    verdict = _text_first_native_verdict()
    routing = _routing()
    wrapped = bind_native_attempt_recorder(
        _native_audio_callback(),
        routing,
        stage="local_entity",
    )
    wrapped(
        {
            "request_sha256": verdict["acoustic_witness"]["request_sha256"],
            "cue_indexes": [1],
        }
    )
    authority = {
        "entity_repairs": [
            {
                "request_sha256": _entity_request()["request_sha256"],
                "cue_indexes": [1],
                "verdict": verdict,
            }
        ]
    }

    finalized = finalize_audio_witness_routing(routing, authority)

    assert finalized["status"] == "CONSUMED"
    assert len(finalized["runtime_native_attempts"]) == 1
    assert len(finalized["observed_consumption"]) == 1
    row = finalized["observed_consumption"][0]
    assert row["origin"].startswith("entity_repairs")
    assert row["consumption_valid"] is True


def test_uncertain_native_result_is_recorded_without_claiming_provider_call() -> None:
    routing = _routing()

    def uncertain(_request):
        return {
            "schema_version": "subtitle-span-acoustic-witness.v1",
            "status": "UNCERTAIN",
            "reason_code": "MAI_PROVIDER_TIMEOUT",
            "source_media_sha256": HEX["source"],
            "audio_start_ms": 0,
            "audio_end_ms": 3_000,
            "candidate_exposure": "none",
            "authority": "EVIDENCE_ONLY",
            "mutation_authorized": False,
        }

    wrapped = bind_native_attempt_recorder(
        uncertain,
        routing,
        stage="local_entity",
    )
    wrapped(
        {
            "request_sha256": "sha256:" + "b" * 64,
            "cue_indexes": [1],
        }
    )

    attempt = routing["runtime_native_attempts"][0]["acoustic_witness"]
    assert attempt["provider"] == "mai"
    assert attempt["provider_call_observed"] is False
    assert attempt["reason_code"] == "MAI_PROVIDER_TIMEOUT"
    assert attempt["model"] is None
    assert attempt["provider_response_sha256"] is None

    finalized = finalize_audio_witness_routing(routing, {})
    assert finalized["status"] == "INVALID_NATIVE_CONSUMPTION"
    row = finalized["observed_consumption"][0]
    assert row["provider_call_observed"] is False
    assert "NATIVE_PROVIDER_CALL_NOT_OBSERVED" in row["evidence_reason_codes"]
    assert row["consumption_valid"] is False
