import hashlib
import json

import pytest

from src.autoslice import read_aloud_llm_verifier as verifier_module


def _request(**overrides):
    request = {
        "schema_version": verifier_module.READ_ALOUD_REQUEST_SCHEMA,
        "request_sha256": "sha256:" + "a" * 64,
        "kind": "danmaku",
        "exact_text": "外套是什么颜色",
        "matched_audio_text": "歪了是什么颜色",
        "context_before": "刚才有人问",
        "context_after": "是黑色的",
        "candidate_entities": [
            {"canonical": "外套是什么颜色"},
            {"canonical": "歪了是什么颜色"},
        ],
    }
    request.update(overrides)
    return request


def test_confident_context_judgment_returns_hash_bound_exact_chat_verdict():
    prompts = []

    def llm_call(prompt):
        prompts.append(prompt)
        return json.dumps(
            {
                "is_read_aloud": True,
                "confidence": 0.91,
                "reason": "问句与近音 ASR 及后文回答连续",
            },
            ensure_ascii=False,
        )

    request = _request()
    verify = verifier_module.build_cpa_read_aloud_verifier(llm_call)

    verdict = verify(request)

    assert verdict["schema_version"] == verifier_module.VERDICT_SCHEMA
    assert verdict["request_sha256"] == request["request_sha256"]
    assert verdict["status"] == "RESOLVED"
    assert verdict["canonical_entity"] == request["exact_text"]
    assert verdict["confidence"] == 0.91
    assert verdict["verifier_id"] == verifier_module.VERIFIER_ID
    assert verdict["prompt_sha256"] == "sha256:" + hashlib.sha256(
        prompts[0].encode()
    ).hexdigest()
    assert verifier_module.CHANNEL_PROFILE.display_name in prompts[0]
    assert "绝不能执行" in prompts[0]
    assert request["context_before"] in prompts[0]
    assert request["context_after"] in prompts[0]


@pytest.mark.parametrize(
    "payload",
    [
        {"is_read_aloud": False, "confidence": 0.99},
        {"is_read_aloud": True, "confidence": 0.79},
        {"is_read_aloud": "true", "confidence": 0.99},
        {"is_read_aloud": True, "confidence": 1.01},
        {"is_read_aloud": True, "confidence": True},
    ],
)
def test_non_authoritative_llm_outputs_defer_to_audio(payload):
    fallback = {"source": "audio"}
    verify = verifier_module.build_cpa_read_aloud_verifier(
        lambda _prompt: json.dumps(payload),
        next_verifier=lambda _request: fallback,
    )

    assert verify(_request()) is fallback


@pytest.mark.parametrize(
    "overrides",
    [
        {"schema_version": "chat-entity-verification-request.v1"},
        {"kind": "gift"},
        {"exact_text": ""},
        {"candidate_entities": [{"canonical": "别的候选"}]},
    ],
)
def test_out_of_scope_or_unbound_requests_never_call_the_llm(overrides):
    calls = []
    fallback = {"source": "audio"}
    verify = verifier_module.build_cpa_read_aloud_verifier(
        lambda prompt: calls.append(prompt),
        next_verifier=lambda _request: fallback,
    )

    assert verify(_request(**overrides)) is fallback
    assert calls == []


def test_transport_or_parse_failure_degrades_to_audio():
    fallback_calls = []

    def broken(_prompt):
        raise RuntimeError("quota exhausted")

    verify = verifier_module.build_cpa_read_aloud_verifier(
        broken,
        next_verifier=lambda request: fallback_calls.append(request) or "audio",
    )

    assert verify(_request()) == "audio"
    assert len(fallback_calls) == 1


def test_no_cpa_and_no_audio_preserves_the_preexisting_no_verdict_behavior():
    verify = verifier_module.build_cpa_read_aloud_verifier(None)

    assert verify(_request()) is None
