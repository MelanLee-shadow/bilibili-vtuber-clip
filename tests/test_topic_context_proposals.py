"""Proposal recall and its phonetic/context controls, using unseen terms."""
import json

import pytest

from src.autoslice.closed_set_proposal_rebuild import (
    proposal_clip_context, proposal_phonemes, rebuild_candidate_after_neither,
)
from src.autoslice.final_review_auditor import _derive_single_span_edit, build_context_adjudication_request
from src.autoslice.missing_proposal_bootstrap import (
    bootstrap_missing_proposal, collect_proposal_audio, prepare_missing_proposal_candidate,
    converge_missing_proposal,
)


def srt(*texts):
    return "\n".join(f"{i}\n00:00:{i * 2:02},000 --> 00:00:{i * 2 + 1:02},000\n{text}\n"
                     for i, text in enumerate(texts, 1))


def payload(proposed, support):
    return json.dumps({
        "status": "PROPOSED", "proposed_cue": proposed, "reason": "同音且符合本片讨论范围",
        "proposal_basis": "topic_homophone", "topic_support": support,
        "candidate_failures": "既有词面不合主题", "evidence_basis": "拼音及主题",
        "residual_uncertainty": "仅提案，仍须正常裁决",
    }, ensure_ascii=False)


@pytest.mark.parametrize("lane", ["bootstrap", "third", "convergence"])
@pytest.mark.parametrize("proposed,support,reason", [
    ("我们分析隐含波动率", ["期权价格和波动率"], None),
    ("我们分析历史波动率", ["期权价格和波动率"], "TOPIC_PROPOSAL_PHONEME_MISMATCH"),
    ("我们分析隐含波动率", [], "TOPIC_PROPOSAL_SUPPORT_MISSING"),
    ("我们分析隐含波动率", ["未提供的金融知识"], "TOPIC_PROPOSAL_SUPPORT_MISSING"),
])
def test_topic_candidate_requires_sound_and_real_context(lane, proposed, support, reason):
    current = "我们分析音含波动率"
    source = srt(current, "先喝口水", "稍等", "回来了", "接着讨论", "期权价格和波动率")
    finding = {"cue_index": 1, "suspect": "音含", "repair_class": "phonetic"}
    prompts = []

    def propose(prompt):
        prompts.append(prompt)
        result = json.loads(payload(proposed, support))
        if lane == "convergence":
            result.update(decision="REPLACE_WITH_EXACT_TEXT", replacement_text=proposed)
        return json.dumps(result, ensure_ascii=False)

    if lane == "bootstrap":
        rebuilt, audit = bootstrap_missing_proposal(
            srt_text=source, finding=finding, structured_chat="", llm_call=propose,
            derive_single_span_edit=_derive_single_span_edit,
        )
    elif lane == "convergence":
        rebuilt, audit = converge_missing_proposal(
            srt_text=source, finding=finding, structured_chat="", llm_call=propose,
            derive_single_span_edit=_derive_single_span_edit, proposal_audit={},
        )
    else:
        rebuilt, audit = rebuild_candidate_after_neither(
            finding=finding,
            request={"current_cue": current, "proposed_cue": "我们分析因含波动率",
                     "whole_clip_current_srt": source},
            witness={"status": "UNCERTAIN", "reason_code": "PROVIDER_UNAVAILABLE"},
            asr_attempts={"routes_exhausted": True, "observations": [{"status": "UNCERTAIN"}]},
            llm_call=propose, derive_single_span_edit=_derive_single_span_edit,
        )
    assert "期权价格和波动率" in prompts[0]  # Beyond the previous three-cue context.
    assert audit["phonetic_evidence"]["evidence_level"] == "CURRENT_TEXT_PINYIN_WEAK"
    assert audit["mutation_authorized"] is False
    if reason:
        assert rebuilt is None and audit["reason_code"] == reason
    else:
        assert rebuilt["proposed_full_cue"] == proposed


@pytest.mark.parametrize("current,proposed", [("先看组玲", "先看组0"), ("然后组衣", "然后组1")])
def test_single_digit_labels_have_phonetic_readings(current, proposed):
    rebuilt, audit = bootstrap_missing_proposal(
        srt_text=srt(current, "实验分组"),
        finding={"cue_index": 1, "suspect": current, "repair_class": "phonetic"},
        structured_chat="", llm_call=lambda _: payload(proposed, ["实验分组"]),
        derive_single_span_edit=_derive_single_span_edit,
    )
    assert rebuilt["proposed_full_cue"] == proposed
    assert audit["mutation_authorized"] is False


@pytest.mark.parametrize("proposed,accepted", [("检查蛋白质", True), ("检查蛋白酶", False)])
def test_one_spurious_tail_syllable_cannot_invent_a_different_sound(proposed, accepted):
    current = "检查蛋白支流"
    rebuilt, audit = bootstrap_missing_proposal(
        srt_text=srt(current, "讨论蛋白质结构"),
        finding={"cue_index": 1, "suspect": "支流", "repair_class": "phonetic"},
        structured_chat="", llm_call=lambda _: payload(proposed, ["讨论蛋白质结构"]),
        derive_single_span_edit=_derive_single_span_edit,
    )
    assert bool(rebuilt) is accepted
    if not accepted:
        assert audit["reason_code"] == "TOPIC_PROPOSAL_PHONEME_MISMATCH"


def test_current_pinyin_is_not_an_audio_witness_or_silence_override():
    weak = proposal_phonemes("语音", {"status": "UNCERTAIN"})
    assert weak["text_pinyin"] == "yu yin" and weak["audio_observed"] is False
    assert "heard_pinyin" not in weak and "target_audible" not in weak
    assert proposal_phonemes("语音", {"status": "OBSERVED", "target_audible": False})["evidence_level"] == "NO_SPEECH_OBSERVED"


@pytest.mark.parametrize("observed", [True, False])
def test_requested_audio_is_consumed_before_candidate_convergence(observed):
    from src.autoslice.final_review_auditor import _candidate_blind_missing_proposal_scaffold

    current = "我们分析音含波动率"
    source = srt(current, "期权价格和波动率")
    finding = {"cue_index": 1, "suspect": "音含", "suggestion": None, "repair_class": "phonetic"}
    request = build_context_adjudication_request(source, _candidate_blind_missing_proposal_scaffold(source, finding))
    calls = []
    audio_calls = []

    def witness(blind):
        audio_calls.append(blind)
        return {"schema_version": "subtitle-span-acoustic-witness.v1", "request_sha256": blind["request_sha256"],
                "status": "OBSERVED" if observed else "UNCERTAIN", "target_audible": True,
                "heard_pinyin": "wo men fen xi yin han bo dong lv" if observed else "",
                "provider": "gemini_api"}

    def cpa(prompt):
        calls.append(prompt)
        if len(calls) == 1:
            return json.dumps({"status": "UNRESOLVED", "needs_audio": True})
        if observed:
            assert '"evidence_level": "AUDIO_OBSERVED"' in prompt
            return payload("我们分析隐含波动率", ["期权价格和波动率"])
        assert "# 字幕缺失候选最终收敛" in prompt  # No repeated proposal on the same weak evidence.
        return json.dumps({"decision": "UNRESOLVED", "needs_audio": True, "replacement_text": ""})

    rebuilt, audit, _failure = prepare_missing_proposal_candidate(
        srt_text=source, finding=finding, structured_chat="", llm_call=cpa,
        derive_single_span_edit=_derive_single_span_edit, check_request=request, entity_verifier=witness,
    )
    assert len(audio_calls) == 1 and len(calls) == 2
    assert audit["mutation_authorized"] is False
    if observed:
        assert rebuilt["proposed_full_cue"] == "我们分析隐含波动率"
        assert audit["proposal_audio"]["phonetic_evidence"]["provider"] == "gemini_api"
    else:
        assert rebuilt.get("proposed_full_cue") is None


def test_whole_clip_topic_fields_are_reused_without_human_answers():
    rendered = proposal_clip_context(srt("", "目标", "另一句"), 1, {
        "selection_hook": "讨论计算机网络", "topic_resolution": {"topics": ["局域网"]},
        "source_human_evidence": {"correct_text": "不可注入的答案"},
    })
    assert "讨论计算机网络" in rendered and "局域网" in rendered
    assert "不可注入的答案" not in rendered
    assert "cue 1 [4000,5000]: <目标见 CURRENT>" in rendered


def test_foreign_original_language_observation_reaches_proposal():
    from src.autoslice.entity_audio_verifier import _prepare_audio_span
    from src.autoslice.exact_source_transcript_contract import seal_exact_source_transcript_observation

    source = srt("他说阿里嘎多", "他用日语道谢")
    finding = {"cue_index": 1, "suspect": "他说阿里嘎多", "proposed_full_cue": "他说阿里嘎多",
               "suggestion": "他说阿里嘎多", "repair_class": "phonetic", "why": "音频发现日语混说"}
    # The normal CURRENT-only scaffold binds the target before any candidate exists.
    finding["_candidate_blind_missing_proposal_scaffold"] = True
    from src.autoslice.final_review_auditor import _candidate_blind_missing_proposal_scaffold
    missing = {**finding, "proposed_full_cue": None, "suggestion": None}
    request = build_context_adjudication_request(source, _candidate_blind_missing_proposal_scaffold(source, missing))
    calls = []

    def unavailable(blind):
        calls.append(blind)
        assert "current_cue" not in blind and "candidate_entities" not in blind
        return {"schema_version": "subtitle-span-acoustic-witness.v1", "status": "UNCERTAIN",
                "request_sha256": blind["request_sha256"], "reason_code": "WITNESS_REPORT_INVALID"}

    def transcribe(blind):
        calls.append(blind)
        assert "current_cue" not in blind and "candidate_entities" not in blind
        span = _prepare_audio_span(request=blind, context_mode=True, source_media_timeline_offset_ms=0,
                                   source_duration_ms=10000, witness_mode=True)
        return seal_exact_source_transcript_observation(
            request=blind, exact_transcript="他说、ありがとう", audible_language="mixed",
            source_media_sha256="a" * 64, audio_clip_sha256="b" * 64, provider="agy", model="fixture",
            response_sha256="c" * 64, timeline_binding=span.timeline_binding,
        )

    audio = collect_proposal_audio(request=request, finding=missing, entity_verifier=unavailable,
                                   transcript_provider=transcribe)
    assert len(calls) == 2
    assert audio["original_language_observation"]["exact_transcript"] == "他说、ありがとう"

    def propose(prompt):
        assert "他说、ありがとう" in prompt and "CURRENT_TEXT_PINYIN_WEAK" in prompt
        return json.dumps({"status": "PROPOSED", "proposed_cue": "他说、ありがとう",
                           "proposal_basis": "audio_transcript", "reason": "保留原语言听写"}, ensure_ascii=False)

    rebuilt, audit = bootstrap_missing_proposal(
        srt_text=source, finding=missing, structured_chat="", llm_call=propose,
        derive_single_span_edit=_derive_single_span_edit, proposal_audio=audio,
    )
    assert rebuilt["proposed_full_cue"] == "他说、ありがとう"
    assert audit["mutation_authorized"] is False
