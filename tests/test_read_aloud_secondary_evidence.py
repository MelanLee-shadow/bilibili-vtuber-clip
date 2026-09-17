from __future__ import annotations

import json

from src.autoslice import read_aloud_llm_verifier as module


def _request() -> dict[str, object]:
    return {
        "schema_version": "chat-entity-verification-request.v1",
        "request_sha256": "sha256:" + "a" * 64,
        "kind": "danmaku",
        "exact_text": "要是能变成她",
        "matched_audio_text": "蘸酱油",
        "context_before": "刚才有人问",
        "context_after": "她继续回答",
        "cue_indexes": [1],
        "matched_start_ms": 5_000,
        "matched_end_ms": 9_000,
        "context_start_ms": 3_500,
        "context_end_ms": 10_500,
        "source_media_timeline_offset_ms": 0,
        "candidate_entities": [
            {"canonical": "要是能变成她"},
            {"canonical": "蘸酱油"},
        ],
    }


def _audio(*, secondary_text: str, events: list[str]):
    def primary(request):
        events.append("AUDIO")
        return {
            "schema_version": "subtitle-span-acoustic-witness.v1",
            "request_sha256": request["request_sha256"],
            "status": "OBSERVED",
            "target_audible": True,
            "heard_pinyin": "yao shi neng bian cheng ta",
            "uncertain_positions": [],
            "syllable_count": 6,
            "confidence": 0.97,
            "source_media_sha256": "1" * 64,
            "audio_clip_sha256": "2" * 64,
            "prompt_sha256": "3" * 64,
            "response_sha256": "4" * 64,
        }

    def secondary(request):
        events.append("SECONDARY")
        assert "candidate_entities" not in request
        return {
            "schema_version": "secondary-audio-witness-evidence.v1",
            "status": "OBSERVED",
            "authority": "EVIDENCE_ONLY",
            "mutation_authorized": False,
            "candidate_exposure": "none",
            "provider": "moss",
            "model": "moss-transcribe-diarize-pro",
            "provider_response_sha256": "5" * 64,
            "receipt_sha256": "6" * 64,
            "target_overlap_segments": [
                {"start_ms": 5_000, "end_ms": 9_000, "text": secondary_text}
            ],
        }

    primary.secondary_audio_evidence = secondary
    return primary


def _judge(calls: list[str]):
    def judge(_prompt: str) -> str:
        calls.append("CPA")
        return json.dumps(
            {
                "ranking": [
                    {"canonical": "要是能变成她", "p": 0.9},
                    {"canonical": "蘸酱油", "p": 0.1},
                ],
                "choice": "要是能变成她",
                "needs_audio": len(calls) == 1,
                "reason": "闭集裁决",
            },
            ensure_ascii=False,
        )

    return judge


def test_zero_pinyin_secondary_conflict_blocks_before_final_cpa() -> None:
    events: list[str] = []
    calls: list[str] = []
    verify = module.build_cpa_read_aloud_verifier(
        _judge(calls),
        next_verifier=_audio(secondary_text="してベンツを", events=events),
    )

    verdict = verify(_request())

    assert calls == ["CPA"]
    assert events == ["AUDIO", "SECONDARY"]
    assert verdict["status"] == "UNCERTAIN"
    assert verdict["reason_code"] == "ACOUSTIC_WITNESS_PROVIDER_CONFLICT"
    assert verdict["decision_authority"] == "NONE"
    assert verdict["acoustic_conflict"]["pinyin_compatibility"] == 0.0
    assert "canonical_entity" not in verdict


def test_nonconflicting_secondary_remains_diagnostic_only() -> None:
    events: list[str] = []
    calls: list[str] = []
    verify = module.build_cpa_read_aloud_verifier(
        _judge(calls),
        next_verifier=_audio(secondary_text="要是能变成她", events=events),
    )

    verdict = verify(_request())

    assert calls == ["CPA", "CPA"]
    assert events == ["AUDIO", "SECONDARY"]
    assert verdict["status"] == "RESOLVED"
    assert verdict["canonical_entity"] == "要是能变成她"
    assert verdict["decision_authority"] == "CPA_JUDGE"
    assert "secondary_audio_evidence" not in verdict
