import hashlib
import json

import pytest

from src.autoslice import read_aloud_llm_verifier as verifier_module
from src.autoslice.chat_authority import (
    ChatEvidence,
    apply_authoritative_chat_evidence,
)
from src.autoslice.jingting_chunker import parse_srt_cues

# Real 2026-07-11 case: ASR is structurally deaf (外套→歪了), only the danmaku recovers it.
_DANMU = "小豆的外套是可以脱的吗？"
_GARBLE = "小豆歪了可以脱吗"


def _srt(*texts: str) -> str:
    blocks = [
        f"{i}\n00:00:{i * 5:02d},000 --> 00:00:{i * 5 + 4:02d},000\n{t}"
        for i, t in enumerate(texts, start=1)
    ]
    return "\n\n".join(blocks) + "\n"


def _audio_stub(canonical: str):
    """Stand-in for the AGY audio fallback verifier."""

    def verify(request):
        return {
            "schema_version": verifier_module.VERDICT_SCHEMA,
            "request_sha256": request["request_sha256"],
            "status": "RESOLVED",
            "canonical_entity": canonical,
            "confidence": 0.97,
        }

    return verify


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


# --------------------------------------------------------------------------- #
# integration: CPA drives the real restoration through chat_authority (no audio)
# --------------------------------------------------------------------------- #
def test_cpa_read_aloud_restores_garbled_cue_end_to_end_without_audio():
    source = _srt(_GARBLE, "可以呀")
    verify = verifier_module.build_cpa_read_aloud_verifier(
        lambda _p: json.dumps(
            {"is_read_aloud": True, "confidence": 0.98, "reason": "问句弹幕紧邻+外套→歪了谐音"},
            ensure_ascii=False,
        )
    )

    output, audit = apply_authoritative_chat_evidence(
        source,
        [ChatEvidence("danmaku", 0, _DANMU)],
        support_srt_texts=[source],
        entity_verifier=verify,
    )

    texts = [cue.text for cue in parse_srt_cues(output)]
    assert texts[0] == _DANMU, texts  # garble replaced by the danmaku she read, no audio used
    row = audit["read_aloud_arbitrations"][0]
    assert row["outcome"] == "authority_confirmed_by_audio"
    assert row["verdict"]["reason_code"] == "READ_ALOUD_CONFIRMED_BY_CONTEXT"


def test_cpa_uncertain_falls_back_to_audio_that_keeps_asr():
    source = _srt(_GARBLE)
    verify = verifier_module.build_cpa_read_aloud_verifier(
        lambda _p: json.dumps({"is_read_aloud": False, "confidence": 0.3}),
        next_verifier=_audio_stub(_GARBLE),  # audio insists she said the ASR span
    )

    output, audit = apply_authoritative_chat_evidence(
        source,
        [ChatEvidence("danmaku", 0, _DANMU)],
        entity_verifier=verify,
    )

    assert parse_srt_cues(output)[0].text == _GARBLE
    assert audit["read_aloud_arbitrations"][0]["outcome"] == "acoustic_span_confirmed_by_audio"
