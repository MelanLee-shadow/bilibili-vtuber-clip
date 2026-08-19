import hashlib
import json

from src.autoslice import read_aloud_llm_verifier as verifier_module
from src.autoslice.chat_authority import (
    ChatEvidence,
    ReferentEntity,
    ReferentGroup,
    apply_audio_entity_verification,
    apply_authoritative_chat_evidence,
)
from src.autoslice.jingting_chunker import parse_srt_cues

# Real 2026-07-11 case: ASR is structurally deaf (外套→歪了), only the danmaku recovers it.
_DANMU = "小主的外套是可以脱的吗？"
_GARBLE = "小主歪了可以脱吗"


def _srt(*texts: str) -> str:
    blocks = [
        f"{i}\n00:00:{i * 5:02d},000 --> 00:00:{i * 5 + 4:02d},000\n{t}"
        for i, t in enumerate(texts, start=1)
    ]
    return "\n\n".join(blocks) + "\n"


def _audio_witness_stub(calls=None, *, heard="wai tao shi shen me yan se"):
    """Stand-in for candidate-blind AGY pinyin evidence."""

    def verify(request):
        if calls is not None:
            calls.append(request)
        assert (
            request["schema_version"]
            == "subtitle-span-acoustic-witness-request.v1"
        )
        assert "candidate_entities" not in request
        assert "current_cue" not in request
        assert "proposed_cue" not in request
        return {
            "schema_version": "subtitle-span-acoustic-witness.v1",
            "request_sha256": request["request_sha256"],
            "status": "OBSERVED",
            "target_audible": True,
            "heard_pinyin": heard,
            "uncertain_positions": [],
            "syllable_count": len(heard.split()),
            "confidence": 0.97,
            "source_media_sha256": "1" * 64,
            "audio_clip_sha256": "2" * 64,
            "prompt_sha256": "3" * 64,
            "response_sha256": "4" * 64,
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
        "cue_indexes": [1],
        "matched_start_ms": 5_000,
        "matched_end_ms": 9_000,
        "context_start_ms": 3_500,
        "context_end_ms": 10_500,
        "source_media_timeline_offset_ms": 0,
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


def test_confident_context_rejection_is_a_cpa_current_decision_without_audio():
    audio_calls = []
    verify = verifier_module.build_cpa_read_aloud_verifier(
        lambda _prompt: json.dumps(
            {"is_read_aloud": False, "confidence": 0.99}
        ),
        next_verifier=_audio_witness_stub(audio_calls),
    )

    verdict = verify(_request())

    assert verdict["canonical_entity"] == "歪了是什么颜色"
    assert verdict["authority_kind"] == "cpa_context_adjudication"
    assert verdict["decision_authority"] == "CPA_JUDGE"
    assert audio_calls == []


def test_weak_context_uses_candidate_blind_audio_then_cpa_highest_probability():
    prompts = []
    audio_calls = []

    def llm_call(prompt):
        prompts.append(prompt)
        if len(prompts) == 1:
            return json.dumps(
                {"is_read_aloud": True, "confidence": 0.79}
            )
        return json.dumps(
            {
                "ranking": [
                    {"canonical": "外套是什么颜色", "p": 0.61},
                    {"canonical": "歪了是什么颜色", "p": 0.39},
                ],
                "choice": "外套是什么颜色",
                "reason": "拼音与问答语境共同支持",
            },
            ensure_ascii=False,
        )

    verify = verifier_module.build_cpa_read_aloud_verifier(
        llm_call,
        next_verifier=_audio_witness_stub(audio_calls),
    )

    verdict = verify(_request())

    assert verdict["canonical_entity"] == "外套是什么颜色"
    assert verdict["confidence"] == 0.61
    assert verdict["authority_kind"] == "cpa_witness_adjudication"
    assert verdict["decision_authority"] == "CPA_JUDGE"
    assert verdict["witness_authority"] == "EVIDENCE_ONLY"
    assert verdict["witness_status"] == "OBSERVED"
    assert len(audio_calls) == 1
    assert "所有已注册专名平等" in prompts[1]


def test_registered_entity_conflict_also_uses_blind_witness_then_cpa():
    audio_calls = []
    request = _request(
        schema_version="chat-entity-verification-request.v1",
        exact_text="俪墨",
        matched_audio_text="琳墨",
        structured_chat_canonical="俪墨",
        structured_chat_surface="俪墨",
        candidate_entities=[
            {"canonical": "俪墨", "surfaces": ["俪墨"]},
            {"canonical": "琳墨", "surfaces": ["琳墨"]},
        ],
    )

    verify = verifier_module.build_cpa_read_aloud_verifier(
        lambda _prompt: json.dumps(
            {
                "ranking": [
                    {"canonical": "俪墨", "p": 0.72},
                    {"canonical": "琳墨", "p": 0.28},
                ],
                "choice": "俪墨",
                "reason": "结构化文字和话题语境支持",
            },
            ensure_ascii=False,
        ),
        next_verifier=_audio_witness_stub(
            audio_calls, heard="li mo"
        ),
    )

    verdict = verify(request)

    assert verdict["canonical_entity"] == "俪墨"
    assert verdict["reason_code"] == (
        "REGISTERED_ENTITY_CPA_WITNESS_ADJUDICATED"
    )
    assert len(audio_calls) == 1


def test_fresh_read_aloud_rejects_explicit_legacy_sighted_witness():
    def sighted_witness(request):
        witness = _audio_witness_stub()(request)
        witness["witness_protocol"] = "legacy_sighted"
        return witness

    verify = verifier_module.build_cpa_read_aloud_verifier(
        lambda _prompt: json.dumps(
            {
                "ranking": [
                    {"canonical": "歪了是什么颜色", "p": 0.8},
                    {"canonical": "外套是什么颜色", "p": 0.2},
                ],
                "choice": "歪了是什么颜色",
                "reason": "legacy evidence is not acoustic authority",
            },
            ensure_ascii=False,
        ),
        next_verifier=sighted_witness,
    )

    verdict = verify(_request())

    assert verdict["witness_status"] == "UNCERTAIN"
    assert verdict["witness_protocol"] == "legacy_sighted"
    assert verdict["acoustic_evidence_used"] is False


def test_read_aloud_rejects_observed_witness_without_audibility_bit():
    def malformed_witness(request):
        witness = _audio_witness_stub()(request)
        witness.pop("target_audible")
        return witness

    verify = verifier_module.build_cpa_read_aloud_verifier(
        lambda _prompt: json.dumps(
            {
                "ranking": [
                    {"canonical": "歪了是什么颜色", "p": 0.8},
                    {"canonical": "外套是什么颜色", "p": 0.2},
                ],
                "choice": "歪了是什么颜色",
                "reason": "malformed evidence is ignored",
            },
            ensure_ascii=False,
        ),
        next_verifier=malformed_witness,
    )

    verdict = verify(_request())

    assert verdict["witness_status"] == "UNCERTAIN"
    assert verdict["acoustic_evidence_used"] is False


def test_transcript_entity_closed_set_uses_cpa_context_only_without_audio():
    prompts = []
    request = {
        "schema_version": "transcript-entity-verification-request.v1",
        "request_sha256": "sha256:" + "c" * 64,
        "evidence_id": "thanks-japanese",
        "kind": "transcript_entity",
        "matched_audio_text": "谢谢老板，阿里嘎多，怎么样",
        "transcript_canonical": "ありがとう",
        "transcript_surface": "阿里嘎多",
        "context_before": "谢谢老板的礼物",
        "context_after": "下一位老板",
        "whole_clip_context": [
            {"cue_index": 1, "text": "谢谢老板的礼物"},
            {"cue_index": 2, "text": "谢谢老板，阿里嘎多，怎么样"},
            {"cue_index": 3, "text": "下一位老板"},
        ],
        "cue_indexes": [2],
        "matched_start_ms": 5_000,
        "matched_end_ms": 9_000,
        "candidate_entities": [
            {"canonical": "ありがとう"},
            {"canonical": "おめでとう"},
        ],
    }

    def llm_call(prompt):
        prompts.append(prompt)
        return json.dumps(
            {
                "ranking": [
                    {"canonical": "ありがとう", "p": 0.97},
                    {"canonical": "おめでとう", "p": 0.03},
                ],
                "choice": "ありがとう",
                "reason": "连续答谢语境明确支持ありがとう",
            },
            ensure_ascii=False,
        )

    verify = verifier_module.build_cpa_read_aloud_verifier(llm_call)
    verdict = verify(request)

    assert verdict["status"] == "RESOLVED"
    assert verdict["canonical_entity"] == "ありがとう"
    assert verdict["authority_kind"] == (
        "cpa_context_only_closed_set_adjudication"
    )
    assert verdict["decision_authority"] == "CPA_JUDGE"
    assert verdict["witness_authority"] == "EVIDENCE_ONLY"
    assert verdict["witness_status"] == "UNCERTAIN"
    assert verdict["acoustic_evidence_used"] is False
    assert verdict["reason_code"] == (
        "CPA_CONTEXT_ONLY_CLOSED_SET_DISAMBIGUATION"
    )
    assert "下一位老板" in prompts[0]
    assert "whole_clip_context" in prompts[0]


def test_sender_closed_set_routes_to_cpa_with_adjacent_event_chain():
    prompts = []
    request = {
        "schema_version": "chat-sender-verification-request.v1",
        "request_sha256": "d" * 64,
        "evidence_id": "guard-sender",
        "kind": "structured_chat_sender_ambiguity",
        "matched_audio_text": "问15",
        "exact_text": '[{"sender":"water不温"},{"sender":"万事屋_official"}]',
        "context_before": "谢谢前一位老板",
        "context_after": "谢谢舰长",
        "whole_clip_context": {
            "adjacent_structured_event_chain": [
                {
                    "source_event_id": "guard-a",
                    "sender": "water不温",
                    "offset_ms": 0,
                },
                {
                    "source_event_id": "guard-b",
                    "sender": "万事屋_official",
                    "offset_ms": 1_000,
                },
            ]
        },
        "cue_indexes": [1],
        "matched_start_ms": 5_000,
        "matched_end_ms": 9_000,
        "candidate_entities": [
            {"canonical": "water不温"},
            {"canonical": "万事屋_official"},
        ],
    }

    def llm_call(prompt):
        prompts.append(prompt)
        return json.dumps(
            {
                "ranking": [
                    {"canonical": "water不温", "p": 0.8},
                    {"canonical": "万事屋_official", "p": 0.2},
                ],
                "choice": "water不温",
                "reason": "听到的称呼与事件顺序共同支持",
            },
            ensure_ascii=False,
        )

    verify = verifier_module.build_cpa_read_aloud_verifier(llm_call)
    verdict = verify(request)

    assert verdict["status"] == "RESOLVED"
    assert verdict["canonical_entity"] == "water不温"
    assert verdict["decision_authority"] == "CPA_JUDGE"
    assert verdict["witness_authority"] == "EVIDENCE_ONLY"
    assert verdict["acoustic_evidence_used"] is False
    assert "adjacent_structured_event_chain" in prompts[0]
    assert "guard-a" in prompts[0]
    assert "guard-b" in prompts[0]


def test_transcript_entity_context_only_cpa_closes_real_repair_gate():
    source = _srt(
        "谢谢老板的礼物",
        "谢谢老板，阿里嘎多，怎么样",
        "下一位老板",
    )
    group = ReferentGroup(
        (
            ReferentEntity(
                "ありがとう",
                ("ありがとう", "阿里嘎多"),
                ("a ri ga tou",),
            ),
            ReferentEntity(
                "おめでとう",
                ("おめでとう", "没得到"),
                ("o me de tou",),
            ),
        ),
        reason="礼物答谢语境中的日语插话闭集",
        positions=("transcript_only",),
    )
    verify = verifier_module.build_cpa_read_aloud_verifier(
        lambda _prompt: json.dumps(
            {
                "ranking": [
                    {"canonical": "ありがとう", "p": 0.98},
                    {"canonical": "おめでとう", "p": 0.02},
                ],
                "choice": "ありがとう",
                "reason": "答谢礼物语境",
            },
            ensure_ascii=False,
        )
    )

    output, audit = apply_audio_entity_verification(
        source,
        referent_groups=[group],
        entity_verifier=verify,
    )

    assert "谢谢老板，ありがとう，怎么样" in output
    assert audit["status"] == "APPLIED_AND_VERIFIED"
    assert audit["entity_verdict_required"] == []
    repair = audit["repairs"][0]
    assert repair["verdict"]["decision_authority"] == "CPA_JUDGE"
    assert repair["verdict"]["reason_code"] == (
        "CPA_CONTEXT_ONLY_CLOSED_SET_DISAMBIGUATION"
    )


def test_transcript_entity_without_cpa_remains_fail_closed():
    request = {
        "schema_version": "transcript-entity-verification-request.v1",
        "request_sha256": "sha256:" + "d" * 64,
    }
    verify = verifier_module.build_cpa_read_aloud_verifier(None)

    assert verify(request) is None


def test_only_candidate_blind_witness_schema_can_reach_audio_provider():
    calls = []
    audio_calls = []
    fallback = {"source": "other-verifier"}

    def audio_provider(request):
        audio_calls.append(request)
        return fallback

    verify = verifier_module.build_cpa_read_aloud_verifier(
        lambda prompt: calls.append(prompt),
        next_verifier=audio_provider,
    )

    assert verify(_request(schema_version="unrelated.v1")) is None
    witness_request = {
        "schema_version": "subtitle-span-acoustic-witness-request.v1",
    }
    assert verify(witness_request) is fallback
    assert audio_calls == [witness_request]
    assert calls == []


def test_transport_failure_may_collect_blind_witness_but_never_lets_it_choose():
    fallback_calls = []

    def broken(_prompt):
        raise RuntimeError("quota exhausted")

    verify = verifier_module.build_cpa_read_aloud_verifier(
        broken,
        next_verifier=_audio_witness_stub(fallback_calls),
    )

    assert verify(_request()) is None
    assert len(fallback_calls) == 1


def test_no_cpa_and_no_audio_preserves_the_preexisting_no_verdict_behavior():
    verify = verifier_module.build_cpa_read_aloud_verifier(None)

    assert verify(_request()) is None


def test_no_cpa_never_delegates_registered_name_choice_to_audio():
    audio_calls = []
    verify = verifier_module.build_cpa_read_aloud_verifier(
        None,
        next_verifier=lambda request: audio_calls.append(request),
    )

    assert verify(
        _request(schema_version="chat-entity-verification-request.v1")
    ) is None
    assert audio_calls == []


# --------------------------------------------------------------------------- #
# integration: CPA drives the real restoration through chat_authority (no audio)
# --------------------------------------------------------------------------- #
def test_cpa_read_aloud_context_cannot_expand_partial_span_without_audio():
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
    assert texts[0] == _GARBLE, texts
    assert _DANMU not in output
    row = audit["read_aloud_arbitrations"][0]
    assert row["outcome"] == "partial_evidence_no_whole_line_copy"
    assert row["verdict"]["reason_code"] == "READ_ALOUD_CONFIRMED_BY_CONTEXT"
    assert row["whole_line_exact_copy_gate"]["status"] == "BLOCKED_PARTIAL_EVIDENCE"


def test_cpa_context_cannot_own_near_complete_span_without_independent_support():
    exact = "soyo就是妈"
    source = _srt("soyo是真妈", "已经超越妈感")
    verify = verifier_module.build_cpa_read_aloud_verifier(
        lambda _p: json.dumps(
            {"is_read_aloud": True, "confidence": 0.98, "reason": "逐句近完整回声"},
            ensure_ascii=False,
        )
    )

    output, audit = apply_authoritative_chat_evidence(
        source,
        [ChatEvidence("danmaku", 0, exact)],
        entity_verifier=verify,
    )

    assert parse_srt_cues(output)[0].text == "soyo是真妈"
    row = audit["read_aloud_arbitrations"][0]
    assert row["outcome"] == "partial_evidence_no_whole_line_copy"
    gate = row["whole_line_exact_copy_gate"]
    assert gate["status"] == "BLOCKED_PARTIAL_EVIDENCE"
    assert gate["owner_eligible"] is False
    assert gate["proof_basis"] == "partial_evidence"
    assert gate["primary_transcript"]["near_complete_transcript"] is True
    assert gate["independent_supports"] == []
    assert audit["applied"] == []


def test_cpa_witness_judge_can_keep_current_without_audio_final_authority():
    source = _srt(_GARBLE)
    calls = []

    def llm_call(_prompt):
        calls.append(1)
        if len(calls) == 1:
            return json.dumps(
                {"is_read_aloud": False, "confidence": 0.3}
            )
        return json.dumps(
            {
                "ranking": [
                    {"canonical": _DANMU, "p": 0.25},
                    {"canonical": _GARBLE, "p": 0.75},
                ],
                "choice": _GARBLE,
                "reason": "目标音节更接近当前跨度",
            },
            ensure_ascii=False,
        )

    verify = verifier_module.build_cpa_read_aloud_verifier(
        llm_call,
        next_verifier=_audio_witness_stub(),
    )

    output, audit = apply_authoritative_chat_evidence(
        source,
        [ChatEvidence("danmaku", 0, _DANMU)],
        entity_verifier=verify,
    )

    assert parse_srt_cues(output)[0].text == _GARBLE
    assert audit["read_aloud_arbitrations"][0]["outcome"] == (
        "current_confirmed_by_cpa_with_blind_audio_witness"
    )


# --------------------------------------------------------------------------- #
# 2026-08-19 Ivan 审片裁定 #2「弹幕不修正」（主包/主播案）regression coverage
# --------------------------------------------------------------------------- #
_MEME_DANMU = "主包给我讲讲这是怎么回事"
_MEME_ASR = "主播给我讲讲这是怎么回事"


def test_confident_meme_spelling_near_miss_previously_lost_ownership_without_witness():
    """Root-cause lock: a confident text-only ``is_read_aloud`` verdict on a
    near-miss must NOT by itself count as evidence strong enough to own the
    cue (``whole_line_exact_copy_gate`` requires witness/independent-transcript
    corroboration). This is the confidence-inversion gap that let ASR's
    「主播给」survive over danmaku's「主包给」when no audio provider fires the
    fallthrough — the invariant the fix in ``verify()`` must not relax."""

    source = _srt(_MEME_ASR, "完整收束")
    verify = verifier_module.build_cpa_read_aloud_verifier(
        lambda _p: json.dumps(
            {"is_read_aloud": True, "confidence": 0.97, "reason": "紧邻弹幕逐字回声"},
            ensure_ascii=False,
        )
    )

    output, audit = apply_authoritative_chat_evidence(
        source,
        [ChatEvidence("danmaku", 0, _MEME_DANMU)],
        entity_verifier=verify,
    )

    assert parse_srt_cues(output)[0].text == _MEME_ASR
    assert _MEME_DANMU not in output
    assert audit["applied"] == []


def test_confident_meme_spelling_near_miss_wins_ownership_via_closed_set_witness():
    """2026-08-19 主包/主播案 fix: the same confident verdict now falls
    through to the candidate-blind witness + CPA closed-set judge, which CAN
    satisfy ``whole_line_exact_copy_gate`` and let the danmaku meme spelling
    (「主包给」, a common livestream nickname misspelling for 主播) win over
    ASR's "corrected"-looking 「主播给」."""

    calls = []

    def llm_call(prompt):
        calls.append(prompt)
        if len(calls) == 1:
            return json.dumps(
                {"is_read_aloud": True, "confidence": 0.97, "reason": "紧邻弹幕逐字回声"},
                ensure_ascii=False,
            )
        return json.dumps(
            {
                "ranking": [
                    {"canonical": _MEME_DANMU, "p": 0.82},
                    {"canonical": _MEME_ASR, "p": 0.18},
                ],
                "choice": _MEME_DANMU,
                "reason": "拼音与弹幕原文一致；主播是常见ASR过度规范化误写",
            },
            ensure_ascii=False,
        )

    verify = verifier_module.build_cpa_read_aloud_verifier(
        llm_call,
        next_verifier=_audio_witness_stub(
            heard="zhu bao gei wo jiang jiang zhe shi zen me hui shi"
        ),
    )
    source = _srt(_MEME_ASR, "完整收束")

    output, audit = apply_authoritative_chat_evidence(
        source,
        [ChatEvidence("danmaku", 0, _MEME_DANMU)],
        entity_verifier=verify,
    )

    assert parse_srt_cues(output)[0].text == _MEME_DANMU
    row = audit["applied"][0]
    assert row["exact_text"] == _MEME_DANMU
    assert row["read_aloud_verdict"]["authority_kind"] == "cpa_witness_adjudication"
    assert row["read_aloud_verdict"]["canonical_entity"] == _MEME_DANMU
    assert row["read_aloud_verdict"]["decision_authority"] == "CPA_JUDGE"
    arbitration = audit["read_aloud_arbitrations"][0]
    assert arbitration["outcome"] == (
        "authority_confirmed_by_cpa_with_blind_audio_witness"
    )
    assert len(calls) == 2


def test_prompt_and_closed_choice_rules_flag_meme_spelling_and_emote_as_not_typos():
    """Locks the 2026-08-19 prompt-hint fix: the judge must be told a meme
    spelling (主包) or the「；；」emote is not a typo to "correct" away."""

    prompts = []
    verify = verifier_module.build_cpa_read_aloud_verifier(
        lambda p: (prompts.append(p), json.dumps({"is_read_aloud": False, "confidence": 0.99}))[1]
    )
    verify(_request())
    assert "主包" in prompts[0]
    assert "；；" in prompts[0]
    assert "不是待纠正的错字" in prompts[0]

    closed_prompts = []

    def closed_llm_call(p):
        closed_prompts.append(p)
        return json.dumps(
            {
                "ranking": [
                    {"canonical": "外套是什么颜色", "p": 0.6},
                    {"canonical": "歪了是什么颜色", "p": 0.4},
                ],
                "choice": "外套是什么颜色",
                "reason": "占位",
            },
            ensure_ascii=False,
        )

    verify2 = verifier_module.build_cpa_read_aloud_verifier(closed_llm_call)
    verify2(
        _request(
            schema_version="chat-entity-verification-request.v1",
            exact_text="外套是什么颜色",
        )
    )
    assert "梗写" in closed_prompts[0]
    assert "不是待" in closed_prompts[0]


def test_completeness_heuristic_extends_partial_read_to_full_danmu_boundary():
    """2026-08-19 Ivan 审片裁定「念弹幕大多念完整」heuristic: documents that
    the existing ascending-cue-count search (``find_best_read_aloud_candidate``)
    already extends a match across cue boundaries to the full danmaku instead
    of settling for a shorter partial span — here the second cue alone has a
    structural ASR mishearing (懂→瞳) that only a full two-cue read recovers,
    and a corroborating independent transcript is enough to win ownership
    (no interruption evidence exists, so the read-aloud contract must copy
    the full two-cue danmaku, not truncate it or leave the typo behind)."""

    danmu = "谢谢老板的解释，我现在完全懂了这个梗的意思"
    source = _srt("谢谢老板的解释，", "我现在完全瞳了这个梗的意思")
    support = _srt("谢谢老板的解释，", "我现在完全懂了这个梗的意思")

    output, audit = apply_authoritative_chat_evidence(
        source,
        [ChatEvidence("danmaku", 0, danmu)],
        support_srt_texts=[support],
        entity_verifier=lambda _request: None,
    )

    texts = [cue.text for cue in parse_srt_cues(output)]
    assert "".join(texts) == danmu
    assert audit["applied"], audit
    row = audit["applied"][0]
    assert row["cue_indexes"] == [1, 2]
    assert row["owner_eligible"] is True
    assert row["exact_text"] == danmu
