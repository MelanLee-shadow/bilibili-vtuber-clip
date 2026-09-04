"""Phase 1 witness/judge fusion: anti-sycophancy and pinyin-gate contracts."""

from __future__ import annotations

import json

import pytest

from src.autoslice.acoustic_witness_adjudication import (
    adjudicate_with_witness,
    build_witness_request,
    judge_word_choice,
    pinyin_compatibility,
    valid_witness_evidence,
)
from src.autoslice.acoustic_pinyin import neutral_syllable_count_hint
from src.autoslice.entity_audio_verifier import (
    _verify_local_audio_request,
    _witness_prompt,
)
from src.autoslice.final_review_auditor import adjudicate_context_finding


CHECK_REQUEST = {
    "schema_version": "subtitle-span-acoustic-check-request.v1",
    "evidence_id": "e" * 64,
    "cue_indexes": [2],
    "matched_start_ms": 10_000,
    "matched_end_ms": 12_000,
    "context_start_ms": 9_000,
    "context_end_ms": 13_000,
    "source_media_timeline_offset_ms": 9_730,
    "current_cue": "还没有歌杂呢",
    "proposed_cue": "还没有歌债呢",
    "suspect": "歌杂",
    "replacement": "歌债",
    "repair_class": "phonetic",
    "context_before": "上一句",
    "context_after": "下一句",
}


def _witness(heard: str, *, audible: bool = True, uncertain=()):
    return {
        "schema_version": "subtitle-span-acoustic-witness.v1",
        "witness_protocol": "blind_pinyin",
        "status": "OBSERVED",
        "target_audible": audible,
        "heard_pinyin": heard,
        "uncertain_positions": list(uncertain),
        "syllable_count": len(heard.split()),
        "confidence": 0.9,
    }


def test_witness_request_strips_every_textual_channel():
    request = build_witness_request(CHECK_REQUEST)
    serialized = json.dumps(request, ensure_ascii=False)
    for leak in ("歌杂", "歌债", "current_cue", "proposed_cue",
                 "candidate_entities", "context_before", "suspect"):
        assert leak not in serialized
    assert request["matched_start_ms"] == 10_000
    assert request["source_media_timeline_offset_ms"] == 9_730
    assert request["witness_protocol"] == "blind_pinyin"
    assert request["syllable_count_hint"] == 6


def test_neutral_syllable_hint_ignores_punctuation_without_biasing_length():
    assert neutral_syllable_count_hint("你好！", "你好吗") is None
    assert neutral_syllable_count_hint("你好！", "你好。") == 2
    assert neutral_syllable_count_hint("你好2", "你好2") is None
    unequal = dict(CHECK_REQUEST, current_cue="你好！", proposed_cue="你好吗")
    assert "syllable_count_hint" not in build_witness_request(unequal)


def test_witness_prompt_never_contains_candidates_or_hanzi_context():
    prompt = _witness_prompt(
        recording_date="2026-07-26",
        target_audio_start_ms=500,
        target_audio_end_ms=2_500,
    )
    assert "候选" not in prompt
    assert "CURRENT" not in prompt
    assert "pinyin" in prompt


def test_verifier_rejects_witness_request_with_candidates():
    poisoned = build_witness_request(CHECK_REQUEST)
    poisoned["candidate_entities"] = [{"canonical": "还没有歌债呢"}]

    class _Verifier:
        output_dir = None

    verdict = _verify_local_audio_request(
        verifier=_Verifier(), request=poisoned
    )
    assert verdict["status"] == "UNCERTAIN"
    assert verdict["reason_code"] == "WITNESS_REQUEST_CARRIES_CANDIDATES"


def test_pinyin_compatibility_prefers_matching_candidate():
    heard = "hai mei you ge zhai ne"
    assert pinyin_compatibility("还没有歌债呢", heard_pinyin=heard) == 1.0
    other = pinyin_compatibility("还没有歌杂呢", heard_pinyin=heard)
    assert other is not None and other < 1.0


def test_pinyin_wildcards_cover_declared_uncertainty():
    heard = "hai mei you ? zhai ne"
    score = pinyin_compatibility(
        "还没有歌债呢", heard_pinyin=heard, uncertain_positions=[3]
    )
    assert score == 1.0


def test_legacy_sighted_witness_stays_valid_but_is_not_recomputed():
    request = build_witness_request(CHECK_REQUEST)
    legacy = _witness("hai mei you ge zhai ne")
    legacy.pop("witness_protocol")
    legacy["request_sha256"] = request["request_sha256"]

    assert valid_witness_evidence(
        legacy, request_sha256=request["request_sha256"]
    ) is True
    repaired, branch, audit = adjudicate_with_witness(
        check_request=CHECK_REQUEST,
        witness=legacy,
        llm_call=lambda _prompt: json.dumps({"choice": "PROPOSED"}),
    )

    assert repaired is False
    assert branch == "LEGACY_SIGHTED_WITNESS_NOT_REUSABLE"
    assert audit["witness_protocol"] == "legacy_sighted"
    assert "candidate_pinyin_similarity" not in audit


def test_unknown_witness_protocol_fails_closed_before_judge():
    witness = _witness("hai mei you ge zhai ne")
    witness["witness_protocol"] = "future_untrusted_protocol"
    called = False

    def judge(_prompt):
        nonlocal called
        called = True
        return json.dumps({"choice": "PROPOSED"})

    repaired, branch, _audit = adjudicate_with_witness(
        check_request=CHECK_REQUEST,
        witness=witness,
        llm_call=judge,
    )

    assert repaired is False
    assert branch == "WITNESS_UNAVAILABLE_KEEP_CURRENT"
    assert called is False


def test_blind_protocol_canary_rejects_sycophantic_proposal_mismatch():
    """A sighted echo would confirm PROPOSED; blind audio matches CURRENT."""

    audio_pinyin = "hai mei you ge za ne"

    def compliant_witness(request):
        return (
            "hai mei you ge zhai ne"
            if "proposed_cue" in request
            else audio_pinyin
        )

    proposed_echo = compliant_witness(CHECK_REQUEST)
    assert pinyin_compatibility(
        CHECK_REQUEST["proposed_cue"], heard_pinyin=proposed_echo
    ) == 1.0
    blind_request = build_witness_request(CHECK_REQUEST)
    assert compliant_witness(blind_request) == audio_pinyin

    prompts = []
    repaired, branch, audit = adjudicate_with_witness(
        check_request=CHECK_REQUEST,
        witness=_witness(compliant_witness(blind_request)),
        llm_call=lambda prompt: prompts.append(prompt)
        or json.dumps(
            {
                "choice": "PROPOSED",
                "reason": "semantic proposal looks fluent",
            }
        ),
    )

    assert repaired is False
    assert branch == "WITNESS_CONFLICT_UNSUPPORTED_PROPOSED_KEPT_CURRENT"
    assert audit["witness_protocol"] == "blind_pinyin"
    assert audit["candidate_pinyin_similarity"]["current"] > audit[
        "candidate_pinyin_similarity"
    ]["proposed"]
    assert "代码计算的双候选拼音贴合" in prompts[0]


def test_judge_out_of_set_answer_is_a_refusal():
    verdict = judge_word_choice(
        llm_call=lambda prompt: json.dumps(
            {"choice": "还没有歌坛呢", "reason": "invented"},
            ensure_ascii=False,
        ),
        check_request=CHECK_REQUEST,
        witness=_witness("hai mei you ge zhai ne"),
    )
    assert verdict["choice"] == "UNCERTAIN"
    assert verdict["reason_code"] == "JUDGE_CHOICE_OUT_OF_SET"


def test_judge_call_failure_fails_closed():
    def broken(_prompt):
        raise RuntimeError("cpa down")

    repaired, branch, _audit = adjudicate_with_witness(
        check_request=CHECK_REQUEST,
        witness=_witness("hai mei you ge zhai ne"),
        llm_call=broken,
    )
    assert repaired is False
    assert branch == "JUDGE_UNCERTAIN_KEEP_CURRENT"


def test_judge_call_retries_once_on_provider_transient_error_then_succeeds():
    """维护者 工程优化②：真善美 zsm4 三模型均短暂 400 报废整轮候选
    的事故——同轮内单次 provider-shaped 失败必须能自愈重试，不立刻判死。"""

    attempts = {"n": 0}

    def flaky(_prompt):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise RuntimeError("HTTPError: 400 Bad Request")
        return json.dumps({"choice": "PROPOSED", "reason": "recovered"})

    verdict = judge_word_choice(
        llm_call=flaky,
        check_request=CHECK_REQUEST,
        witness=_witness("hai mei you ge zhai ne"),
    )

    assert attempts["n"] == 2
    assert verdict["status"] == "JUDGED"
    assert verdict["choice"] == "PROPOSED"
    # source_fact_review.py house pattern: "每次重试都进回执披露" — a JUDGED
    # verdict reached only after a provider-transient retry must not look
    # identical to a clean first-try success, especially since it is what
    # gets written into the judge-verdict-cache and replayed verbatim.
    assert verdict["provider_retry_attempted"] is True


def test_judge_call_does_not_retry_semantic_out_of_set_refusal():
    """Retries are for provider-layer failures only; a model that answered
    with an out-of-set choice already responded — retrying would be re-rolling
    a semantic result, which 维护者's ruling forbids."""

    attempts = {"n": 0}

    def out_of_set(_prompt):
        attempts["n"] += 1
        return json.dumps({"choice": "还没有歌坛呢", "reason": "invented"})

    verdict = judge_word_choice(
        llm_call=out_of_set,
        check_request=CHECK_REQUEST,
        witness=_witness("hai mei you ge zhai ne"),
    )

    assert attempts["n"] == 1
    assert verdict["reason_code"] == "JUDGE_CHOICE_OUT_OF_SET"


def test_judge_call_exhausts_retry_and_preserves_full_error_cascade():
    """Double-truncation regression: llm_client's stderr[-400:] plus the old
    flat error[:300] threw away exactly which earlier models failed. Every
    attempt's error must survive in ``error_cascade``, not just the tail."""

    attempts = {"n": 0}

    def always_fails(_prompt):
        attempts["n"] += 1
        raise RuntimeError(f"HTTPError: 400 Bad Request on gpt-5.6-sol attempt {attempts['n']}")

    verdict = judge_word_choice(
        llm_call=always_fails,
        check_request=CHECK_REQUEST,
        witness=_witness("hai mei you ge zhai ne"),
    )

    assert attempts["n"] == 2
    assert verdict["status"] == "JUDGE_UNAVAILABLE"
    assert verdict["reason_code"] == "JUDGE_CALL_FAILED"
    assert verdict["provider_retry_attempted"] is True
    cascade = verdict["error_cascade"]
    assert len(cascade) == 2
    assert "attempt 1" in cascade[0]
    assert "attempt 2" in cascade[1]
    # The old single-string field must still be present (additive schema).
    assert "error" in verdict and isinstance(verdict["error"], str)


def test_judge_can_reject_a_malformed_closed_set_without_mutating_text():
    repaired, branch, audit = adjudicate_with_witness(
        check_request=CHECK_REQUEST,
        witness=_witness("wan quan bu shi zhe liang ju"),
        llm_call=lambda _prompt: json.dumps(
            {
                "ranking": [
                    {"choice": "NEITHER", "p": 0.98},
                    {"choice": "CURRENT", "p": 0.01},
                    {"choice": "PROPOSED", "p": 0.01},
                ],
                "choice": "NEITHER",
                "reason": "both candidates conflict with the witness",
            }
        ),
    )

    assert repaired is False
    assert branch == "JUDGE_REJECTS_CLOSED_SET"
    assert audit["judge"]["status"] == "JUDGED"
    assert audit["judge"]["choice"] == "NEITHER"


def test_judged_proposed_needs_support_when_pinyin_witness_disagrees():
    # 维护者：CPA 仍终裁，但无第三方结构化证据不得背离耳朵。
    repaired, branch, audit = adjudicate_with_witness(
        check_request=CHECK_REQUEST,
        witness=_witness("hai mei you ge za ne"),
        llm_call=lambda prompt: json.dumps({"choice": "PROPOSED"}),
    )
    assert repaired is False
    assert branch == "WITNESS_CONFLICT_UNSUPPORTED_PROPOSED_KEPT_CURRENT"
    assert audit["witness_diagnostic_conflict"] is True
    assert audit["witness_conflict_gate"]["status"] == "BLOCK"
    compat = audit["pinyin_compatibility"]
    assert compat["current"] > compat["proposed"]


def test_equal_low_pinyin_scores_need_structured_support_to_apply():
    request = {
        **CHECK_REQUEST,
        "current_cue": "请问什么打不过这 NPC",
        "proposed_cue": "请问怎么打不过这 NPC",
        "suspect": "什",
        "replacement": "怎",
        "repair_class": "phonetic",
    }

    repaired, branch, audit = adjudicate_with_witness(
        check_request=request,
        witness=_witness("ki mo i sum da bu guo"),
        llm_call=lambda _prompt: json.dumps({"choice": "PROPOSED"}),
    )

    assert repaired is False
    assert branch == "WITNESS_CONFLICT_UNSUPPORTED_PROPOSED_KEPT_CURRENT"
    assert audit["pinyin_compatibility"]["current"] == audit[
        "pinyin_compatibility"
    ]["proposed"]
    assert audit["witness_diagnostic_conflict"] is True


def test_judge_verdict_cache_round_trip(tmp_path, monkeypatch):
    """维护者 自修复成本令：同一问题（同 prompt_sha）绝不发第二次
    judge 请求。JUDGED 终态入缓存并回放；非 JUDGED 不入；无 AUTOSLICE_BASE
    时完全旁路。"""

    monkeypatch.setenv("AUTOSLICE_BASE", str(tmp_path))
    calls = {"n": 0}

    def counting_llm(prompt):
        calls["n"] += 1
        return json.dumps({"choice": "PROPOSED", "reason": "r"})

    kwargs = dict(
        check_request=CHECK_REQUEST,
        witness=_witness("hai mei you ge zhai ne"),
    )
    first = judge_word_choice(llm_call=counting_llm, **kwargs)
    assert first["status"] == "JUDGED" and calls["n"] == 1
    second = judge_word_choice(llm_call=counting_llm, **kwargs)
    assert second["status"] == "JUDGED" and calls["n"] == 1
    assert second.get("served_from_cache") is True
    assert second["choice"] == first["choice"]

    # 失败结果不得污染缓存：换语境（新 prompt_sha）+ 失败 llm → 不入缓存
    def failing_llm(prompt):
        calls["n"] += 1
        raise RuntimeError("provider down")

    bad = judge_word_choice(
        llm_call=failing_llm,
        check_request={**CHECK_REQUEST, "context_before": "换个语境"},
        witness=_witness("hai mei you ge zhai ne"),
    )
    assert bad["status"] == "JUDGE_UNAVAILABLE" and calls["n"] == 2
    retry = judge_word_choice(
        llm_call=counting_llm,
        check_request={**CHECK_REQUEST, "context_before": "换个语境"},
        witness=_witness("hai mei you ge zhai ne"),
    )
    assert retry["status"] == "JUDGED" and calls["n"] == 3

    # 无缓存根 → 旁路（每次都调用）
    monkeypatch.delenv("AUTOSLICE_BASE")
    judge_word_choice(llm_call=counting_llm, **kwargs)
    judge_word_choice(llm_call=counting_llm, **kwargs)
    assert calls["n"] == 5


def test_self_inconsistent_witness_still_needs_third_party_support():
    """刘若莎案：听写自称 14 音节却写出对不上
    的拼音串（self_count_mismatch）。8/8 裁定补足边界：证人并非最终票，
    但 CPA 背离它仍需第三方结构化证据，不能只凭语义重投一次。"""

    witness = {**_witness("hai mei you ge za ne"), "self_count_mismatch": True}
    repaired, branch, _ = adjudicate_with_witness(
        check_request=CHECK_REQUEST,
        witness=witness,
        llm_call=lambda prompt: json.dumps({"choice": "PROPOSED"}),
    )
    assert repaired is False
    assert branch == "WITNESS_CONFLICT_UNSUPPORTED_PROPOSED_KEPT_CURRENT"

    # 删除类亦不得靠无结构化证据的语义裁决背离耳朵。
    request = {
        **CHECK_REQUEST,
        "current_cue": "我草，乱说的啊",
        "proposed_cue": "乱说的啊",
        "suspect": "我草，",
        "replacement": "",
        "repair_class": "acoustic_delete",
    }
    kept, branch, _ = adjudicate_with_witness(
        check_request=request,
        witness={**_witness("wo cao luan shuo de a"),
                 "self_count_mismatch": True},
        llm_call=lambda prompt: json.dumps({"choice": "PROPOSED"}),
    )
    assert kept is False
    assert branch == "WITNESS_CONFLICT_UNSUPPORTED_PROPOSED_KEPT_CURRENT"


def test_judged_proposed_with_agreeing_pinyin_applies():
    repaired, branch, _audit = adjudicate_with_witness(
        check_request=CHECK_REQUEST,
        witness=_witness("hai mei you ge zhai ne"),
        llm_call=lambda prompt: json.dumps({"choice": "PROPOSED"}),
    )
    assert repaired is True
    assert branch == "WITNESS_JUDGE_APPLY_PROPOSED"


def test_inaudible_target_uses_explicit_current_proposed_drop_contract():
    prompts: list[str] = []

    def choose_drop(prompt: str) -> str:
        prompts.append(prompt)
        return json.dumps({"choice": "DROP"})

    dropped, branch, _ = adjudicate_with_witness(
        check_request={**CHECK_REQUEST, "repair_class": "acoustic_drop_cue"},
        witness=_witness("?", audible=False, uncertain=(0,)),
        llm_call=choose_drop,
    )
    assert (
        dropped is True
        and branch == "CPA_JUDGE_APPLY_INAUDIBLE_DROP_CUE"
    )
    assert "CURRENT、PROPOSED、DROP 三项" in prompts[0]
    assert "<DROP_CUE: EMPTY SUBTITLE>" in prompts[0]
    assert '"CURRENT"或"PROPOSED"或"DROP"' in prompts[0]

    applied, branch, audit = adjudicate_with_witness(
        check_request=CHECK_REQUEST,
        witness=_witness("?", audible=False, uncertain=(0,)),
        llm_call=lambda _prompt: json.dumps({"choice": "PROPOSED"}),
    )
    assert applied is True
    assert branch == "CPA_EXPLICIT_OVERRIDE_INAUDIBLE_WITNESS"
    assert audit["inaudible_witness_override"]["status"] == "PASS"
    assert audit["inaudible_witness_override"]["target_audible"] is False

    rejected, branch, _ = adjudicate_with_witness(
        check_request={**CHECK_REQUEST, "repair_class": "acoustic_drop_cue"},
        witness=_witness("?", audible=False, uncertain=(0,)),
        llm_call=lambda _prompt: json.dumps({"choice": "CURRENT"}),
    )
    assert rejected is False and branch == "JUDGE_KEEPS_CURRENT"


def test_inaudible_nonempty_proposed_is_an_explicit_cpa_override():
    """CPA may override AGY evidence, but the override is typed and non-empty."""

    deleted, branch, _ = adjudicate_with_witness(
        check_request={**CHECK_REQUEST, "repair_class": "acoustic_delete"},
        witness=_witness("?", audible=False, uncertain=(0,)),
        llm_call=lambda _prompt: json.dumps({"choice": "PROPOSED"}),
    )
    assert deleted is True
    assert branch == "CPA_EXPLICIT_OVERRIDE_INAUDIBLE_WITNESS"


def test_inaudible_empty_proposed_cannot_impersonate_drop():
    request = {
        **CHECK_REQUEST,
        "repair_class": "acoustic_drop_cue",
        "proposed_cue": "",
        "replacement": "",
    }

    applied, branch, _audit = adjudicate_with_witness(
        check_request=request,
        witness=_witness("", audible=False),
        llm_call=lambda _prompt: json.dumps({"choice": "PROPOSED"}),
    )

    assert applied is False
    assert branch == "INAUDIBLE_EMPTY_PROPOSED_REQUIRES_EXPLICIT_DROP"


def test_inaudible_neither_is_out_of_set_and_fails_closed():
    verdict = judge_word_choice(
        llm_call=lambda _prompt: json.dumps({"choice": "NEITHER"}),
        check_request=CHECK_REQUEST,
        witness=_witness("", audible=False),
    )

    assert verdict["status"] == "JUDGE_OUT_OF_SET"
    assert verdict["choice"] == "UNCERTAIN"
    assert verdict["choice_set"] == ["CURRENT", "DROP", "PROPOSED"]


def test_declared_proper_name_spelling_is_evidence_for_cpa_not_an_override():
    """Official spelling reaches CPA, but cannot overrule its closed-set vote."""

    srt = (
        "1\n00:00:01,000 --> 00:00:03,000\n"
        "那就差林墨没吃了\n"
    )
    finding = {
        "cue_index": 1,
        "kind": "entity",
        "suspect": "林",
        "suggestion": "礼",
        "span_start_codepoint": 3,
        "span_end_codepoint": 4,
        "proposed_full_cue": "那就差礼墨没吃了",
        "repair_class": "source_backed_entity",
        "candidate_provenance": {
            "kind": "official_roster",
            "surface": "礼墨",
        },
    }

    def witness(request):
        return {
            "schema_version": "subtitle-span-acoustic-witness.v1",
            "request_sha256": request["request_sha256"],
            "status": "OBSERVED",
            "target_audible": True,
            "heard_pinyin": "na jiu cha ling mo mei chi le",
            "uncertain_positions": [],
            "syllable_count": 8,
            "confidence": 0.95,
        }

    prompts = []

    def judge_current(prompt):
        prompts.append(prompt)
        return json.dumps({"choice": "CURRENT"})

    kept, audit = adjudicate_context_finding(
        srt,
        finding,
        entity_verifier=witness,
        judge_llm_call=judge_current,
    )

    assert "林墨" in kept
    assert audit["repaired"] is False
    assert audit["policy_branch"] == "JUDGE_KEEPS_CURRENT"
    assert '"kind": "official_roster"' in prompts[0]
    assert '"surface": "礼墨"' in prompts[0]
    assert audit["decision_authority"] == "CPA_JUDGE"
    assert audit["witness_authority"] == "EVIDENCE_ONLY"

    repaired, audit = adjudicate_context_finding(
        srt,
        finding,
        entity_verifier=witness,
        judge_llm_call=lambda _prompt: json.dumps({"choice": "PROPOSED"}),
    )
    assert "礼墨" in repaired
    assert "林墨" not in repaired
    assert audit["repaired"] is True
    assert (
        audit["policy_branch"]
        == "CPA_JUDGE_WITH_TEXT_AUTHORITY_APPLY_PROPOSED"
    )
    assert audit["mutation_authority"] == {
        "schema_version": "subtitle-correction-mutation-authority.v1",
        "status": "PASS",
        "basis": "CPA_JUDGED_WITH_TEXTUAL_ORTHOGRAPHY_EVIDENCE",
    }


def test_acoustic_delete_demands_clear_pinyin_win():
    request = {
        **CHECK_REQUEST,
        "current_cue": "我草，乱说的啊",
        "proposed_cue": "乱说的啊",
        "suspect": "我草，",
        "replacement": "",
        "repair_class": "acoustic_delete",
    }
    repaired, branch, _ = adjudicate_with_witness(
        check_request=request,
        witness=_witness("luan shuo de a"),
        llm_call=lambda prompt: json.dumps({"choice": "PROPOSED"}),
    )
    assert repaired is True and branch == "WITNESS_JUDGE_APPLY_PROPOSED"

    # A conflicting witness plus no independent support keeps CURRENT.
    repaired, branch, _ = adjudicate_with_witness(
        check_request=request,
        witness=_witness("wo cao luan shuo de a"),
        llm_call=lambda prompt: json.dumps({"choice": "PROPOSED"}),
    )
    assert repaired is False
    assert branch == "WITNESS_CONFLICT_UNSUPPORTED_PROPOSED_KEPT_CURRENT"


def test_judge_prompt_carries_witness_and_closed_set():
    prompts = []

    def capture(prompt):
        prompts.append(prompt)
        return json.dumps({"choice": "UNCERTAIN"})

    judge_word_choice(
        llm_call=capture,
        check_request=CHECK_REQUEST,
        witness=_witness("hai mei you ge zhai ne"),
        structured_chat_context="- 观众A: 歌债+1",
    )
    prompt = prompts[0]
    assert "hai mei you ge zhai ne" in prompt
    assert "还没有歌杂呢" in prompt and "还没有歌债呢" in prompt
    assert "歌债+1" in prompt
    assert "NEITHER" in prompt


@pytest.mark.parametrize("bad_witness", [
    {},
    {
        "schema_version": "subtitle-span-acoustic-witness.v1",
        "status": "OBSERVED",
        "target_audible": "yes",
    },
])
def test_invalid_witness_always_keeps_current(bad_witness):
    repaired, branch, _ = adjudicate_with_witness(
        check_request=CHECK_REQUEST,
        witness=bad_witness,
        llm_call=lambda prompt: json.dumps({"choice": "PROPOSED"}),
    )
    assert repaired is False
    assert branch == "WITNESS_UNAVAILABLE_KEEP_CURRENT"


def test_cpa_decides_closed_set_when_agy_witness_is_unavailable():
    witness = {
        "schema_version": "subtitle-span-acoustic-witness.v1",
        "status": "UNCERTAIN",
        "reason_code": "ENTITY_AUDIO_PROVIDER_FAILED",
        "detail": "AGY_QUOTA_EXHAUSTED",
    }
    prompts = []
    repaired, branch, audit = adjudicate_with_witness(
        check_request=CHECK_REQUEST,
        witness=witness,
        llm_call=lambda prompt: (
            prompts.append(prompt)
            or json.dumps({"choice": "PROPOSED", "reason": "context wins"})
        ),
    )
    assert repaired is True
    assert branch == "CPA_JUDGE_APPLY_PROPOSED_WITHOUT_AUDIO_WITNESS"
    assert audit["witness_status"] == "UNCERTAIN"
    assert "AGY_QUOTA_EXHAUSTED" in prompts[0]
    assert "这不剥夺你的" in prompts[0]


def test_witness_self_count_mismatch_is_disclosed_not_fatal(tmp_path):
    """A model's off-by-one self-count must never invalidate a clean
    dictation (four supporting witnesses on 1209_1410 were all
    killed by the strict equality). Zero/absent counts stay invalid."""

    from src.autoslice import entity_audio_verifier as eav

    audio = tmp_path / "span.wav"
    audio.write_bytes(b"RIFFfake")
    prompt = tmp_path / "prompt.json"
    prompt.write_text("{}", encoding="utf-8")
    response = tmp_path / "response.json"
    response.write_text("{}", encoding="utf-8")

    def observed(count):
        return {
            "schema_version": eav.WITNESS_SCHEMA,
            "status": "OBSERVED",
            "target_audible": True,
            "heard_pinyin": "hai mei you ge zhai ne",
            "uncertain_positions": [],
            "syllable_count": count,
            "confidence": 0.9,
        }

    outcome = eav._EntityProviderOutcome(
        observed=observed(6),
        provider="gemini_api",
        model="gemini-3.6-flash",
        prompt_path=prompt,
        response_path=response,
        accepted_key_tier=None,
        paid_policy_stamp=None,
        provider_failures=[],
    )

    kwargs = dict(
        request={"kind": "subtitle_span_acoustic_witness"},
        request_sha="sha256:0" * 1,
        outcome=outcome,
        source_sha256="sha256:deadbeef",
        audio_path=audio,
        start_ms=0,
        end_ms=2000,
        timeline_binding={},
    )
    off_by_one = eav._subtitle_acoustic_witness_verdict(
        observed=observed(5), **kwargs
    )
    assert off_by_one["status"] == "OBSERVED"
    assert off_by_one["self_count_mismatch"] is True
    assert off_by_one["syllable_count"] == 6

    exact = eav._subtitle_acoustic_witness_verdict(
        observed=observed(6), **kwargs
    )
    assert exact["status"] == "OBSERVED"
    assert exact["self_count_mismatch"] is False

    zero = eav._subtitle_acoustic_witness_verdict(
        observed=observed(0), **kwargs
    )
    assert zero.get("status") != "OBSERVED"
    assert "WITNESS_REPORT_INVALID" in str(zero)

    # 静音证词（1160 咳咳案）：空听写 + target_audible=False + count 0
    # 是删除提案的有效观察——OBSERVED 而非 INVALID。
    silence = eav._subtitle_acoustic_witness_verdict(
        observed={
            "schema_version": eav.WITNESS_SCHEMA,
            "status": "OBSERVED",
            "target_audible": False,
            "heard_pinyin": "",
            "uncertain_positions": [],
            "syllable_count": 0,
            "confidence": 0.9,
        },
        **kwargs,
    )
    assert silence["status"] == "OBSERVED"
    assert silence["target_audible"] is False
    assert silence["heard_pinyin"] == ""
    assert silence["syllable_count"] == 0

    # 有声却空听写仍是报告缺陷
    audible_empty = eav._subtitle_acoustic_witness_verdict(
        observed={
            "schema_version": eav.WITNESS_SCHEMA,
            "status": "OBSERVED",
            "target_audible": True,
            "heard_pinyin": "",
            "uncertain_positions": [],
            "syllable_count": 0,
            "confidence": 0.9,
        },
        **kwargs,
    )
    assert audible_empty.get("status") != "OBSERVED"


def test_witness_audio_crop_is_target_bound_not_context(tmp_path):
    from src.autoslice import entity_audio_verifier as eav

    request = {
        "matched_start_ms": 10_000,
        "matched_end_ms": 11_120,
        "context_start_ms": 4_000,
        "context_end_ms": 18_000,
    }
    span = eav._prepare_audio_span(
        request=request,
        context_mode=True,
        source_media_timeline_offset_ms=0,
        source_duration_ms=3_600_000,
        witness_mode=True,
    )
    assert span is not None
    # target±400ms 再量化到 100ms 网格（起点下取、终点上取）：漂移轮的
    # 裁剪字节稳定 → 声学缓存命中。仍是 target-bound，不是 context-bound。
    assert span.crop_start_ms == 9_600
    assert span.crop_end_ms == 11_600
    # 网格吸附：目标平移 <100ms 落进同一裁剪窗（缓存键稳定的核心保证）
    drifted = eav._prepare_audio_span(
        request={**request, "matched_start_ms": 10_040, "matched_end_ms": 11_160},
        context_mode=True,
        source_media_timeline_offset_ms=0,
        source_duration_ms=3_600_000,
        witness_mode=True,
    )
    assert (drifted.crop_start_ms, drifted.crop_end_ms) == (9_600, 11_600)
    wide = eav._prepare_audio_span(
        request=request,
        context_mode=True,
        source_media_timeline_offset_ms=0,
        source_duration_ms=3_600_000,
    )
    assert wide.crop_start_ms == 4_000 and wide.crop_end_ms == 18_000


def test_witness_implausible_syllable_rate_is_retriable(tmp_path):
    from src.autoslice import entity_audio_verifier as eav

    audio = tmp_path / "span.wav"
    audio.write_bytes(b"RIFFfake")
    prompt = tmp_path / "p.json"
    prompt.write_text("{}", encoding="utf-8")
    response = tmp_path / "r.json"
    response.write_text("{}", encoding="utf-8")

    class Outcome:
        model = "gemini-3.6-flash"
        prompt_path = prompt
        response_path = response
        provider = "gemini_api"
        accepted_key_tier = None

    fourteen = "tang lin yun de hua jiu shi zhe yang zi shuo de ba la"
    verdict = eav._subtitle_acoustic_witness_verdict(
        request={
            "kind": "subtitle_span_acoustic_witness",
            "matched_start_ms": 10_000,
            "matched_end_ms": 11_120,
        },
        request_sha="sha",
        observed={
            "schema_version": eav.WITNESS_SCHEMA,
            "status": "OBSERVED",
            "target_audible": True,
            "heard_pinyin": fourteen,
            "uncertain_positions": [],
            "syllable_count": 14,
            "confidence": 0.9,
        },
        outcome=Outcome(),
        source_sha256="sha256:x",
        audio_path=audio,
        start_ms=0,
        end_ms=1920,
        timeline_binding={},
    )
    assert verdict.get("status") != "OBSERVED"
    assert "WITNESS_IMPLAUSIBLE_SYLLABLE_RATE" in str(verdict)


def test_judge_ranking_top_choice_wins_and_uncertain_is_not_terminal():
    """维护者：按概率排序必须选最高；模型自报 UNCERTAIN 但给出
    有效排序时以排序第一为裁决；无排序的拒答仍是可重试的 OUT_OF_SET。"""

    import json as _json

    from src.autoslice.acoustic_witness_adjudication import judge_word_choice

    def call_with(payload):
        def llm_call(prompt):
            return _json.dumps(payload, ensure_ascii=False)
        return judge_word_choice(
            llm_call=llm_call,
            check_request=dict(CHECK_REQUEST),
            witness={
                "heard_pinyin": "xiao li ni zen me bei dian le",
                "syllable_count": 8,
                "uncertain_positions": [],
                "confidence": 0.9,
            },
            context_before="前句",
            context_after="后句",
            structured_chat_context="弹幕原文：小李你怎么被点了",
        )

    ranked = call_with({
        "ranking": [
            {"choice": "PROPOSED", "p": 0.85},
            {"choice": "CURRENT", "p": 0.15},
        ],
        "choice": "UNCERTAIN",
        "reason": "弹幕原文与自称先验都指向小李",
    })
    assert ranked["status"] == "JUDGED"
    assert ranked["choice"] == "PROPOSED"
    assert ranked["ranking"][0]["p"] == 0.85

    disagree = call_with({
        "ranking": [
            {"choice": "CURRENT", "p": 0.7},
            {"choice": "PROPOSED", "p": 0.3},
        ],
        "choice": "PROPOSED",
        "reason": "自相矛盾时排序第一为准",
    })
    assert disagree["choice"] == "CURRENT"

    neither = call_with({
        "ranking": [
            {"choice": "NEITHER", "p": 0.98},
            {"choice": "CURRENT", "p": 0.01},
            {"choice": "PROPOSED", "p": 0.01},
        ],
        "choice": "CURRENT",
        "reason": "听写拼音与两个整句都不匹配",
    })
    assert neither["status"] == "JUDGED"
    assert neither["choice"] == "NEITHER"

    refusal = call_with({"choice": "UNCERTAIN", "reason": "拒绝排序"})
    assert refusal["status"] == "JUDGE_OUT_OF_SET"
    assert refusal["choice"] == "UNCERTAIN"
