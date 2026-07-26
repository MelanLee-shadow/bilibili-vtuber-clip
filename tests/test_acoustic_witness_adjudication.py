"""Phase 1 witness/judge fusion: anti-sycophancy and pinyin-gate contracts."""

from __future__ import annotations

import json

import pytest

from src.autoslice.acoustic_witness_adjudication import (
    adjudicate_with_witness,
    build_witness_request,
    judge_word_choice,
    pinyin_compatibility,
)
from src.autoslice.entity_audio_verifier import (
    _verify_local_audio_request,
    _witness_prompt,
)


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


def test_witness_prompt_never_contains_candidates_or_hanzi_context():
    prompt = _witness_prompt(
        recording_date="2026-07-26",
        delivery_mode="gemini_api",
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


def test_judged_proposed_needs_pinyin_agreement():
    # judge says PROPOSED but the dictation matches the CURRENT text
    repaired, branch, audit = adjudicate_with_witness(
        check_request=CHECK_REQUEST,
        witness=_witness("hai mei you ge za ne"),
        llm_call=lambda prompt: json.dumps({"choice": "PROPOSED"}),
    )
    assert repaired is False
    assert branch == "JUDGE_CHOICE_PINYIN_INCOMPATIBLE_KEEP_CURRENT"
    compat = audit["pinyin_compatibility"]
    assert compat["current"] > compat["proposed"]


def test_judged_proposed_with_agreeing_pinyin_applies():
    repaired, branch, _audit = adjudicate_with_witness(
        check_request=CHECK_REQUEST,
        witness=_witness("hai mei you ge zhai ne"),
        llm_call=lambda prompt: json.dumps({"choice": "PROPOSED"}),
    )
    assert repaired is True
    assert branch == "WITNESS_JUDGE_APPLY_PROPOSED"


def test_inaudible_target_only_supports_drop_cue():
    dropped, branch, _ = adjudicate_with_witness(
        check_request={**CHECK_REQUEST, "repair_class": "acoustic_drop_cue"},
        witness=_witness("?", audible=False, uncertain=(0,)),
        llm_call=None,
    )
    assert dropped is True and branch == "TARGET_INAUDIBLE_DROP_CUE"

    kept, branch, _ = adjudicate_with_witness(
        check_request=CHECK_REQUEST,
        witness=_witness("?", audible=False, uncertain=(0,)),
        llm_call=None,
    )
    assert kept is False and branch == "TARGET_INAUDIBLE_KEEP_CURRENT"


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

    # witness actually heard the full sentence -> deletion loses its margin
    repaired, branch, _ = adjudicate_with_witness(
        check_request=request,
        witness=_witness("wo cao luan shuo de a"),
        llm_call=lambda prompt: json.dumps({"choice": "PROPOSED"}),
    )
    assert repaired is False
    assert branch == "JUDGE_CHOICE_PINYIN_INCOMPATIBLE_KEEP_CURRENT"


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
    assert "UNCERTAIN" in prompt


@pytest.mark.parametrize("bad_witness", [
    {},
    {"schema_version": "subtitle-span-acoustic-witness.v1", "status": "UNCERTAIN"},
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
