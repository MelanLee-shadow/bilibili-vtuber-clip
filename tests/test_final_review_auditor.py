import hashlib
import json

import pytest

from src.autoslice.final_review_auditor import (
    FinalReviewAuditError,
    adjudicate_context_finding,
    adjudicate_exact_release_findings,
    audit_correction_mutation_authority,
    audit_final_subtitles,
    build_context_adjudication_request,
    route_findings,
)


def _srt(*texts: str) -> str:
    blocks = []
    for index, text in enumerate(texts, start=1):
        blocks.append(
            f"{index}\n00:00:{index * 5:02d},000 --> 00:00:{index * 5 + 4:02d},000\n{text}"
        )
    return "\n\n".join(blocks) + "\n"


def _fake_llm(findings):
    def call(prompt):
        return json.dumps({"findings": findings}, ensure_ascii=False)

    return call


def _extract(raw):
    return json.loads(raw)


def test_auditor_validates_and_drops_unanchored_findings():
    source = _srt("欢迎季下", "正常的一句话")
    findings = audit_final_subtitles(
        source,
        llm_call=_fake_llm([
            {
                "cue": 1,
                "suspect": "季",
                "replacement": "记",
                "kind": "nonword",
                "proposed_full_cue": "欢迎记下",
                "repair_class": "phonetic",
                "why": "非词",
            },
            {"cue": 2, "suspect": "不存在的片段", "kind": "context", "why": "x"},
            {"cue": 99, "suspect": "正常", "kind": "context", "why": "越界"},
        ]),
        extract_json=_extract,
    )

    assert len(findings) == 1
    assert findings[0]["suspect"] == "季"


def test_router_does_not_apply_ungrounded_homophone_spelling():
    """A reviewer proposal is not textual authority for a homophone spelling."""
    source = _srt("欢迎季下", "今天小雨来了没")
    findings = [
        {"cue_index": 1, "suspect": "季下", "kind": "nonword", "suggestion": "记下", "why": "非词"},
        {"cue_index": 2, "suspect": "小雨", "kind": "self_ref", "suggestion": "小李", "why": "自称可疑"},
    ]

    output, audit = route_findings(source, findings, protected_term_set=frozenset())

    assert "欢迎季下" in output
    assert "小雨" in output  # 非同音建议不改写
    assert audit["applied_count"] == 0
    routed = {row["suspect"]: row["routed"] for row in audit["findings"]}
    assert routed == {"季下": "disclosure", "小雨": "disclosure"}
    assert audit["findings"][0]["orthography_authority"]["status"] == "BLOCK"


def test_router_never_touches_protected_chat_cues():
    source = _srt("欢迎季下")
    findings = [
        {"cue_index": 1, "suspect": "季下", "kind": "nonword", "suggestion": "记下", "why": "非词"}
    ]

    output, audit = route_findings(
        source, findings, protected_cue_indexes=[1], protected_term_set=frozenset()
    )

    assert "季下" in output
    assert audit["applied_count"] == 0
    assert audit["findings"][0]["routed"] == "disclosure_protected"


def test_protected_meme_terms_never_auto_fixed():
    """2026-07-14 抽查实证：审片员想把梗词「立语」同音改成「俚语」。词典
    权威高于审片直觉——钦定词面只披露永不自动改写。"""
    from src.autoslice.final_review_auditor import protected_terms

    source = _srt("怎么还有立语啊")
    findings = [
        {"cue_index": 1, "suspect": "立语", "kind": "nonword", "suggestion": "俚语", "why": "x"}
    ]

    output, audit = route_findings(
        source, findings, protected_term_set=frozenset({"立语"})
    )

    assert "立语" in output
    assert "俚语" not in output
    assert audit["findings"][0]["routed"] == "disclosure_protected_term"
    assert audit["applied_count"] == 0

    # 真实词典加载必须护住 立语 与 做0.4（glossary 行首术语解析回归）
    real = protected_terms()
    assert "立语" in real
    assert "做0.4" in real


def test_auditor_llm_failure_blocks_instead_of_becoming_clean():
    def broken(prompt):
        raise RuntimeError("cpa down")

    with pytest.raises(FinalReviewAuditError) as raised:
        audit_final_subtitles(
            _srt("一句"), llm_call=broken, extract_json=_extract
        )

    assert (
        raised.value.reason_code
        == "FINAL_REVIEW_PROVIDER_OR_JSON_UNAVAILABLE"
    )


@pytest.mark.parametrize(
    ("payload", "reason_code"),
    [
        ({}, "FINAL_REVIEW_RESPONSE_FINDINGS_MISSING"),
        ({"findings": None}, "FINAL_REVIEW_RESPONSE_FINDINGS_INVALID"),
        ({"findings": {}}, "FINAL_REVIEW_RESPONSE_FINDINGS_INVALID"),
        (
            {"findings": [{"cue": 99, "suspect": "不存在"}]},
            "FINAL_REVIEW_RESPONSE_FINDINGS_ALL_INVALID",
        ),
    ],
)
def test_auditor_malformed_response_never_collapses_to_empty_findings(
    payload, reason_code
):
    with pytest.raises(FinalReviewAuditError) as raised:
        audit_final_subtitles(
            _srt("一句"),
            llm_call=lambda _prompt: json.dumps(payload),
            extract_json=_extract,
        )

    assert raised.value.reason_code == reason_code


def test_auditor_only_explicit_empty_findings_is_clean_discovery():
    assert (
        audit_final_subtitles(
            _srt("一句"),
            llm_call=lambda _prompt: '{"findings":[]}',
            extract_json=_extract,
        )
        == []
    )


def test_auditor_reasks_cpa_once_after_all_findings_are_schema_invalid():
    prompts = []
    responses = iter(
        [
            '{"findings":[{"cue":99,"suspect":"不存在"}]}',
            '{"findings":[]}',
        ]
    )

    def retrying_llm(prompt):
        prompts.append(prompt)
        return next(responses)

    assert (
        audit_final_subtitles(
            _srt("一句"),
            llm_call=retrying_llm,
            extract_json=_extract,
        )
        == []
    )
    assert len(prompts) == 2
    assert "上一轮返回了非空 findings" not in prompts[0]
    assert "上一轮返回了非空 findings" in prompts[1]
    assert "CUE_OUT_OF_RANGE" in prompts[1]
    assert '"cue": 99' in prompts[1]
    assert "不是新一轮全片审查" in prompts[1]


def test_auditor_schema_retry_cannot_introduce_a_new_finding():
    responses = iter(
        [
            '{"findings":[{"cue":1,"kind":"context","why":"疑点无改法"}]}',
            json.dumps(
                {
                    "findings": [
                        {
                            "cue": 2,
                            "kind": "context",
                            "proposed_full_cue": "第二句修正版",
                            "repair_class": "phonetic",
                            "why": "新找了另一条",
                        }
                    ]
                },
                ensure_ascii=False,
            ),
        ]
    )

    with pytest.raises(FinalReviewAuditError) as raised:
        audit_final_subtitles(
            _srt("第一句", "第二句"),
            llm_call=lambda _prompt: next(responses),
            extract_json=_extract,
        )

    assert raised.value.reason_code == "FINAL_REVIEW_RESPONSE_FINDINGS_ALL_INVALID"
    assert "SCHEMA_REPAIR_NEW_FINDING_FORBIDDEN" in raised.value.detail


def test_auditor_schema_retry_diagnostics_bind_original_row_and_cue_text():
    prompts = []
    responses = iter(
        [
            json.dumps(
                {
                    "findings": [
                        {"cue": 1, "kind": "context", "why": "口吃可疑"}
                    ]
                },
                ensure_ascii=False,
            ),
            '{"findings":[]}',
        ]
    )

    assert audit_final_subtitles(
        _srt("因因为我一会要玩游戏"),
        llm_call=lambda prompt: prompts.append(prompt) or next(responses),
        extract_json=_extract,
    ) == []
    assert '"current_cue": "因因为我一会要玩游戏"' in prompts[1]
    assert '"why": "口吃可疑"' in prompts[1]


def test_auditor_schema_retry_turns_unbounded_doubt_into_bounded_proposal():
    prompts = []
    responses = iter(
        [
            '{"findings":[{"cue":1,"kind":"context","why":"量词语境可疑"}]}',
            json.dumps(
                {
                    "findings": [
                        {
                            "cue": 1,
                            "kind": "context",
                            "proposed_full_cue": "他一副很无辜的样子",
                            "repair_class": "phonetic",
                            "why": "一副与样子构成固定搭配",
                        }
                    ]
                },
                ensure_ascii=False,
            ),
        ]
    )

    findings = audit_final_subtitles(
        _srt("他欺负很无辜的样子"),
        llm_call=lambda prompt: (
            prompts.append(prompt) or next(responses)
        ),
        extract_json=_extract,
    )

    assert findings[0]["proposed_full_cue"] == "他一副很无辜的样子"
    assert findings[0]["suspect"] == "欺负"
    assert findings[0]["suggestion"] == "一副"
    assert "NO_BOUNDED_SPAN_OR_PROPOSAL" in prompts[1]


def test_auditor_schema_repair_retry_is_bounded_and_fail_closed():
    calls = 0

    def always_invalid(_prompt):
        nonlocal calls
        calls += 1
        return '{"findings":[{"cue":99,"suspect":"不存在"}]}'

    with pytest.raises(
        FinalReviewAuditError,
        match="FINAL_REVIEW_RESPONSE_FINDINGS_ALL_INVALID",
    ):
        audit_final_subtitles(
            _srt("一句"),
            llm_call=always_invalid,
            extract_json=_extract,
        )

    assert calls == 2


def test_auditor_prompt_distinguishes_gibberish_code_switch_from_real_foreign_dialogue():
    captured = {}

    def review(prompt):
        captured["prompt"] = prompt
        return '{"findings":[]}'

    audit_final_subtitles(
        _srt("因为李豆沙是侄女，kowa，kowai", "本物の気持ちです"),
        llm_call=review,
        extract_json=_extract,
    )

    prompt = captured["prompt"]
    assert "侄女，kowa，kowai" in prompt
    assert "真正的日语、英语对白" in prompt
    assert "不能翻译" in prompt
    assert "重复或近乎平行的句式" in prompt
    assert "直女/侄女" in prompt
    assert "不行不行，并非不行" in prompt


def test_auditor_derives_bounded_edit_despite_advisory_scope_mismatch():
    source = _srt("地狱在理解，觉得成人吗")
    findings = audit_final_subtitles(
        source,
        llm_call=_fake_llm([
            {
                "cue": 1,
                "suspect": "地狱在理解",
                "replacement": "地狱再爱我，觉得成人吗",
                "kind": "context",
                "proposed_full_cue": "地狱再爱我，觉得成人吗",
                "repair_class": "phonetic",
                "why": "作品名",
            }
        ]),
        extract_json=_extract,
    )

    assert findings[0]["suspect"] == "在理解"
    assert findings[0]["suggestion"] == "再爱我"
    assert findings[0]["proposed_full_cue"] == "地狱再爱我，觉得成人吗"
    assert findings[0]["reported_scope_warnings"] == [
        "REPORTED_SUSPECT_SCOPE_MISMATCH",
        "REPORTED_REPLACEMENT_SCOPE_MISMATCH",
    ]


def test_auditor_keeps_bounded_full_cue_when_advisory_suspect_is_not_verbatim():
    source = _srt("以后做可以煮吗")
    findings = audit_final_subtitles(
        source,
        llm_call=_fake_llm([
            {
                "cue": 1,
                "suspect": "以后做，可以煮吗",
                "replacement": "以后做可以煮久点",
                "kind": "context",
                "proposed_full_cue": "以后做可以煮久点",
                "repair_class": "phonetic",
                "why": "原句语境不完整",
            }
        ]),
        extract_json=_extract,
    )

    assert findings[0]["suspect"] == "吗"
    assert findings[0]["suggestion"] == "久点"
    assert findings[0]["proposed_full_cue"] == "以后做可以煮久点"
    assert findings[0]["reported_scope_warnings"] == [
        "REPORTED_SUSPECT_SCOPE_MISMATCH",
        "REPORTED_REPLACEMENT_SCOPE_MISMATCH",
    ]


def test_auditor_normalizes_machine_verifiable_cue_index_alias():
    findings = audit_final_subtitles(
        _srt("所以剩一点给我"),
        llm_call=_fake_llm([
            {
                "cue_index": 1,
                "kind": "context",
                "proposed_full_cue": "所以顺便带我",
                "repair_class": "phonetic",
                "why": "语境和近音均支持",
            }
        ]),
        extract_json=_extract,
    )

    assert findings[0]["cue_index"] == 1
    assert findings[0]["suspect"] == "剩一点给"
    assert findings[0]["suggestion"] == "顺便带"
    assert findings[0]["input_contract_normalizations"] == [
        "cue_index_to_cue"
    ]


def test_auditor_all_invalid_error_records_bounded_rejection_diagnostics():
    with pytest.raises(FinalReviewAuditError) as raised:
        audit_final_subtitles(
            _srt("一句"),
            llm_call=lambda _prompt: json.dumps(
                {
                    "findings": [
                        {"cue_index": 99, "suspect": "不存在"},
                        {"suspect": "一句"},
                    ]
                },
                ensure_ascii=False,
            ),
            extract_json=_extract,
        )

    detail = json.loads(raised.value.detail)
    assert detail["raw_count"] == 2
    assert detail["invalid_rows"] == [
        {
            "reason": "CUE_OUT_OF_RANGE",
            "cue": 99,
            "cue_count": 1,
            "returned_row": {"cue_index": 99, "suspect": "不存在"},
        },
        {
            "reason": "CUE_MISSING_OR_INVALID",
            "keys": ["suspect"],
            "returned_row": {"suspect": "一句"},
        },
    ]


def test_auditor_derives_title_span_only_when_source_surface_is_witnessed():
    source = _srt("书名叫地狱再爱我", "地狱在理解，觉得成人吗")
    findings = audit_final_subtitles(
        source,
        llm_call=_fake_llm([
            {
                "cue": 2,
                "kind": "entity",
                "proposed_full_cue": "地狱再爱我，你觉得成人吗",
                "repair_class": "source_backed_entity",
                "source_surface": "地狱再爱我",
                "evidence_cue_ids": [1],
                "why": "前一句给出书名",
            }
        ]),
        extract_json=_extract,
    )

    assert findings[0]["suspect"] == "在理解，"
    assert findings[0]["suggestion"] == "再爱我，你"
    assert findings[0]["candidate_provenance"] == {
        "kind": "transcript_context",
        "surface": "地狱再爱我",
        "nearest_cue_distance": 1,
    }


def test_auditor_can_cite_structured_chat_as_name_spelling_evidence():
    source = _srt("但是因为提")
    findings = audit_final_subtitles(
        source,
        llm_call=_fake_llm([
            {
                "cue": 1,
                "kind": "entity",
                "proposed_full_cue": "但是因为kmx",
                "repair_class": "source_backed_entity",
                "source_surface": "kmx",
                "why": "同一时间窗弹幕重复使用该专名",
            }
        ]),
        extract_json=_extract,
        structured_context_text="danmaku @1000ms: 大家：kmx是这样的",
    )

    assert findings[0]["suggestion"] == "kmx"
    assert findings[0]["candidate_provenance"] == {
        "kind": "structured_context",
        "surface": "kmx",
    }


def test_auditor_rejects_reviewer_only_proper_name():
    source = _srt("那群 P 7赖我的群绝对不止有我一个人")
    findings = audit_final_subtitles(
        source,
        llm_call=_fake_llm([
            {
                "cue": 1,
                "kind": "entity",
                "proposed_full_cue": "那群P7Live的群绝对不止有我一个人",
                "repair_class": "source_backed_entity",
                "source_surface": "P7Live",
                "why": "猜测组织名",
            }
        ]),
        extract_json=_extract,
    )

    assert findings[0]["suggestion"] is None
    assert findings[0]["suggestion_rejected_reason"] == "ENTITY_SOURCE_SURFACE_UNWITNESSED"


def test_parallel_repeat_recovers_misclassified_entity_provenance_but_keeps_audio_gate():
    """7/22 大哥骂赢案：终审已经发现同槽重复，却因模型把 entity
    标成 phonetic 且漏 source_surface 而只披露。完整替换词面在后文逐字
    重复时可恢复 provenance，但引入注册实体仍不得走纯文本自动改写。"""

    source = _srt(
        "小李又被大哥骂赢了",
        "哪里又变成小李被大N霸凌了",
    )
    findings = audit_final_subtitles(
        source,
        llm_call=_fake_llm(
            [
                {
                    "cue": 1,
                    "kind": "entity",
                    "proposed_full_cue": "小李又被大N霸凌了",
                    "repair_class": "phonetic",
                    "evidence_cue_ids": [2],
                    "why": "后文立即平行复述同一句式",
                }
            ]
        ),
        extract_json=_extract,
    )

    finding = findings[0]
    assert finding["suspect"] == "哥骂赢"
    assert finding["suggestion"] == "N霸凌"
    assert finding["repair_class"] == "source_backed_entity"
    assert finding["candidate_provenance"] == {
        "kind": "transcript_context",
        "surface": "N霸凌",
        "nearest_cue_distance": 1,
    }
    assert finding["source_surface_inference"]["basis"] == (
        "exact_replacement_repeated_in_local_authority"
    )

    output, audit = route_findings(
        source,
        findings,
        protected_term_set=frozenset(),
        entity_surface_set=frozenset({"大N"}),
    )
    assert "小李又被大哥骂赢了" in output
    assert audit["findings"][0]["routed"] == "disclosure"
    assert audit["findings"][0]["entity_surface_conflict"] is True


def test_auditor_cannot_bypass_entity_provenance_by_mislabeling_latin_name():
    source = _srt("那群 P 7赖我的群绝对不止有我一个人")
    findings = audit_final_subtitles(
        source,
        llm_call=_fake_llm([
            {
                "cue": 1,
                "kind": "context",
                "proposed_full_cue": "那群P7Live的群绝对不止有我一个人",
                "repair_class": "phonetic",
                "why": "猜测组织名但伪装成近音修复",
            }
        ]),
        extract_json=_extract,
    )

    assert findings[0]["suggestion"] is None
    assert (
        findings[0]["suggestion_rejected_reason"]
        == "LATIN_SCRIPT_REPAIR_REQUIRES_SOURCE_PROVENANCE"
    )


def test_context_request_uses_full_cue_candidates_and_adjacent_lines():
    source = _srt("前一句", "还没有歌杂呢", "后一句", "再后一句")
    request = build_context_adjudication_request(
        source,
        {
            "cue_index": 2,
            "suspect": "歌杂",
            "suggestion": "歌债",
            "proposed_full_cue": "还没有歌债呢",
            "repair_class": "phonetic",
            "why": "重复话题",
        },
    )

    assert request["schema_version"] == "subtitle-span-acoustic-check-request.v1"
    assert [row["canonical"] for row in request["candidate_entities"]] == [
        "还没有歌杂呢",
        "还没有歌债呢",
    ]
    assert [row["candidate_id"] for row in request["candidate_entities"]] == [
        "CURRENT",
        "PROPOSED",
    ]
    assert request["context_before"] == "前一句"
    assert request["context_after"] == "后一句\n再后一句"
    assert request["context_start_ms"] < request["matched_start_ms"]
    assert request["context_end_ms"] > request["matched_end_ms"]
    assert request["context_end_ms"] < 20_000  # cue 4 text is context, its audio is excluded
    assert request["source_media_timeline_offset_ms"] == 0
    assert len(request["request_sha256"]) == 64


def test_context_request_hash_binds_source_media_timeline_offset():
    source = (
        "1\n"
        "00:00:00,250 --> 00:00:02,810\n"
        "就请坐在左边的弹\n"
    )
    finding = {
        "cue_index": 1,
        "suspect": "弹",
        "suggestion": "互相弹",
        "proposed_full_cue": "就请坐在左边的互相弹",
        "repair_class": "phonetic",
        "why": "exact-final release check",
    }

    delivery_local = build_context_adjudication_request(source, finding)
    padded_source = build_context_adjudication_request(
        source,
        finding,
        source_media_timeline_offset_ms=9_770,
    )

    assert delivery_local["matched_start_ms"] == padded_source["matched_start_ms"] == 250
    assert delivery_local["matched_end_ms"] == padded_source["matched_end_ms"] == 2_810
    assert delivery_local["source_media_timeline_offset_ms"] == 0
    assert padded_source["source_media_timeline_offset_ms"] == 9_770
    assert delivery_local["request_sha256"] != padded_source["request_sha256"]
    assert delivery_local["evidence_id"] != padded_source["evidence_id"]


@pytest.mark.parametrize("invalid_offset", [-1, True, 1.5, "9770"])
def test_context_request_rejects_invalid_source_media_timeline_offset(
    invalid_offset,
):
    source = _srt("还没有歌杂呢")
    with pytest.raises(ValueError, match="SOURCE_MEDIA_TIMELINE_OFFSET_INVALID"):
        build_context_adjudication_request(
            source,
            {
                "cue_index": 1,
                "suspect": "歌杂",
                "suggestion": "歌债",
                "proposed_full_cue": "还没有歌债呢",
                "repair_class": "phonetic",
            },
            source_media_timeline_offset_ms=invalid_offset,
        )



def _witness(request, heard, *, audible=True, uncertain=()):
    """Valid dictation-witness verdict for the given witness request."""
    return {
        "schema_version": "subtitle-span-acoustic-witness.v1",
        "request_sha256": request["request_sha256"],
        "status": "OBSERVED",
        "target_audible": audible,
        "heard_pinyin": heard,
        "uncertain_positions": list(uncertain),
        "syllable_count": len(heard.split()),
        "confidence": 0.9,
        "reason": "test witness",
    }


def _judge(choice):
    return lambda prompt: json.dumps({"choice": choice, "reason": "test"})

def test_exact_release_adjudication_threads_source_media_timeline_offset():
    source = (
        "1\n"
        "00:00:00,250 --> 00:00:02,810\n"
        "就请坐在左边的弹\n"
    )
    seen_requests = []

    def reject_proposal(request):
        seen_requests.append(request)
        return _witness(request, "jiu qing zuo zai zuo bian de tan")

    unresolved, resolved = adjudicate_exact_release_findings(
        source,
        [
            {
                "cue_index": 1,
                "suspect": "弹",
                "suggestion": "互相弹",
                "proposed_full_cue": "就请坐在左边的互相弹",
                "repair_class": "phonetic",
            }
        ],
        entity_verifier=reject_proposal,
        source_media_timeline_offset_ms=9_770,
        judge_llm_call=_judge("CURRENT"),
    )

    assert not unresolved
    assert len(resolved) == 1
    assert seen_requests[0]["matched_start_ms"] == 250
    assert seen_requests[0]["matched_end_ms"] == 2_810
    assert seen_requests[0]["source_media_timeline_offset_ms"] == 9_770


def test_context_request_builds_real_title_fix_without_duplicating_suffix():
    source = _srt("地狱在理解，觉得成人吗")
    request = build_context_adjudication_request(
        source,
        {
            "cue_index": 1,
            "suspect": "在理解，",
            "suggestion": "再爱我，你",
            "proposed_full_cue": "地狱再爱我，你觉得成人吗",
            "repair_class": "source_backed_entity",
            "why": "前后文反复出现作品名",
        },
    )

    assert request["proposed_cue"] == "地狱再爱我，你觉得成人吗"


def test_context_adjudication_applies_only_exact_candidate_and_keeps_timing():
    source = _srt("前一句", "还没有歌杂呢", "后一句")

    def choose_proposed(request):
        return _witness(request, "hai mei you ge zhai ne")

    output, audit = adjudicate_context_finding(
        source,
        {
            "cue_index": 2,
            "suspect": "歌杂",
            "suggestion": "歌债",
            "proposed_full_cue": "还没有歌债呢",
            "repair_class": "phonetic",
            "why": "重复话题",
        },
        entity_verifier=choose_proposed,
        judge_llm_call=_judge("PROPOSED"),
    )

    assert "还没有歌债呢" in output
    assert "00:00:10,000 --> 00:00:14,000" in output
    assert audit["repaired"] is True
    assert audit["policy_branch"] == "WITNESS_JUDGE_APPLY_PROPOSED"
    assert audit["request"]["current_cue"] == "还没有歌杂呢"
    assert audit["verdict"]["heard_pinyin"] == "hai mei you ge zhai ne"


def test_context_adjudication_keeps_current_when_semantics_conflict_with_audio():
    source = _srt("还没有歌杂呢")

    def acoustics_veto_proposal(request):
        return _witness(request, "hai mei you ge za ne")

    output, audit = adjudicate_context_finding(
        source,
        {
            "cue_index": 1,
            "suspect": "歌杂",
            "suggestion": "歌债",
            "proposed_full_cue": "还没有歌债呢",
            "repair_class": "phonetic",
            "why": "重复话题",
        },
        entity_verifier=acoustics_veto_proposal,
        judge_llm_call=_judge("CURRENT"),
    )

    assert output == source
    assert audit["status"] == "OBSERVED"
    assert audit["repaired"] is False
    assert audit["policy_branch"] == "JUDGE_KEEPS_CURRENT"


def test_source_backed_letter_name_spelling_survives_acoustic_grapheme_veto():
    source = _srt("哪里又变成小李被大大恩霸凌了")
    finding = {
        "cue_index": 1,
        "suspect": "大大恩",
        "suggestion": "大N",
        "proposed_full_cue": "哪里又变成小李被大N霸凌了",
        "repair_class": "source_backed_entity",
        "candidate_provenance": {
            "kind": "glossary",
            "surface": "大N",
        },
        "why": "同一人物昵称已有来源见证",
    }

    def acoustics_reports_spoken_en(request):
        return _witness(
            request, "na li you bian cheng xiao li bei da da en ba ling le"
        )

    output, audit = adjudicate_context_finding(
        source,
        finding,
        entity_verifier=acoustics_reports_spoken_en,
        judge_llm_call=_judge("PROPOSED"),
    )

    assert "大N霸凌" in output
    assert "大大恩" not in output
    assert audit["repaired"] is True
    assert audit["policy_branch"] == (
        "CPA_JUDGE_WITH_TEXT_AUTHORITY_APPLY_PROPOSED"
    )
    assert audit["orthography_equivalence"]["matched"] is True


def test_cpa_can_use_distinguishing_pinyin_without_text_authority():
    source = _srt("毁神来了")
    finding = {
        "cue_index": 1,
        "kind": "context",
        "suspect": "毁神",
        "suggestion": "绘声",
        "proposed_full_cue": "绘声来了",
        "repair_class": "phonetic",
        "why": "模型猜测另一种写法",
    }

    def acoustics_claims_spelling_difference(request):
        return _witness(request, "hui sheng lai le")

    output, audit = adjudicate_context_finding(
        source,
        finding,
        entity_verifier=acoustics_claims_spelling_difference,
        judge_llm_call=_judge("PROPOSED"),
    )

    assert "绘声来了" in output
    assert audit["repaired"] is True
    assert audit["orthography_ambiguous"] is False
    assert audit["orthography_authority"]["status"] == "BLOCK"
    assert audit["policy_branch"] == "WITNESS_JUDGE_APPLY_PROPOSED"
    assert audit["mutation_authority"]["basis"] == (
        "CPA_ACOUSTIC_PRONUNCIATION_DISAMBIGUATION"
    )


def test_strict_homophone_tie_judge_semantic_tiebreak_applies_proposed():
    """Ivan 2026-07-27 概率裁定令：一/咦 同音，音频定义上中立，judge 按语义
    排序拍板施改；mutation basis 为 SEMANTIC_JUDGE_ORTHOGRAPHY_TIEBREAK。"""

    source = _srt("一、那现在就等")
    finding = {
        "cue_index": 1,
        "kind": "context",
        "suspect": "一、",
        "suggestion": "咦，",
        "proposed_full_cue": "咦，那现在就等",
        "repair_class": "phonetic",
        "why": "语气词更通顺",
    }

    def neutral_witness(request):
        return _witness(request, "yi na xian zai jiu deng")

    output, audit = adjudicate_context_finding(
        source,
        finding,
        entity_verifier=neutral_witness,
        judge_llm_call=_judge("PROPOSED"),
    )

    assert "咦，那现在就等" in output
    assert audit["repaired"] is True
    assert audit["orthography_ambiguous"] is True
    assert audit["orthography_authority"]["status"] == "BLOCK"
    assert audit["policy_branch"] == (
        "CPA_SEMANTIC_ORTHOGRAPHY_TIEBREAK_APPLY_PROPOSED"
    )
    assert audit["mutation_authority"]["status"] == "PASS"
    assert audit["mutation_authority"]["basis"] == (
        "SEMANTIC_JUDGE_ORTHOGRAPHY_TIEBREAK"
    )


def test_strict_homophone_tie_judge_current_keeps_and_discloses():
    source = _srt("一、那现在就等")
    finding = {
        "cue_index": 1,
        "kind": "context",
        "suspect": "一、",
        "suggestion": "咦，",
        "proposed_full_cue": "咦，那现在就等",
        "repair_class": "phonetic",
        "why": "语气词更通顺",
    }

    output, audit = adjudicate_context_finding(
        source,
        finding,
        entity_verifier=lambda request: _witness(
            request, "yi na xian zai jiu deng"
        ),
        judge_llm_call=_judge("CURRENT"),
    )

    assert output == source
    assert audit["repaired"] is False
    assert audit["policy_branch"] == "JUDGE_KEEPS_CURRENT"
    assert audit["mutation_authority"]["status"] == "NOT_APPLIED"


def test_near_homophone_without_authority_still_goes_to_cpa():
    """近音可由拼音区分；没有文字权威时也必须由 CPA 明确裁决，不能由
    预判门在 CPA 之前替它保持现文本。"""

    source = _srt("毁神来了")
    judge_calls = []

    def counting_judge(prompt):
        judge_calls.append(prompt)
        return _judge("PROPOSED")(prompt)

    output, audit = adjudicate_context_finding(
        source,
        {
            "cue_index": 1,
            "kind": "context",
            "suspect": "毁神",
            "suggestion": "绘声",
            "proposed_full_cue": "绘声来了",
            "repair_class": "phonetic",
            "why": "模型猜测另一种写法",
        },
        entity_verifier=lambda request: _witness(
            request, "hui sheng lai le"
        ),
        judge_llm_call=counting_judge,
    )

    assert "绘声来了" in output
    assert audit["repaired"] is True
    assert audit["policy_branch"] == "WITNESS_JUDGE_APPLY_PROPOSED"
    assert len(judge_calls) == 1


def test_mutation_audit_accepts_semantic_tiebreak_and_blocks_laundering():
    """审计端与生产端对称复算：真实语义拍板收 PASS；把 judge 实际选
    CURRENT 的行贴上 SEMANTIC basis 必须 BLOCK（防洗白）。"""

    import copy

    source = _srt("一、那现在就等")
    finding = {
        "cue_index": 1,
        "kind": "context",
        "suspect": "一、",
        "suggestion": "咦，",
        "proposed_full_cue": "咦，那现在就等",
        "repair_class": "phonetic",
        "why": "语气词更通顺",
    }
    _output, adj = adjudicate_context_finding(
        source,
        finding,
        entity_verifier=lambda request: _witness(
            request, "yi na xian zai jiu deng"
        ),
        judge_llm_call=_judge("PROPOSED"),
    )
    row = {
        **finding,
        "routed": "context_audio_adjudicated_fix",
        "context_audio_adjudication": adj,
    }

    audit = audit_correction_mutation_authority(
        {
            "schema_version": "final-review-audit.v1",
            "status": "APPLIED",
            "findings": [row],
            "applied_count": 1,
        }
    )
    assert audit["status"] == "PASS"
    assert audit["validated_mutation_count"] == 1

    forged = copy.deepcopy(row)
    forged["context_audio_adjudication"]["witness_judge"]["judge"][
        "choice"
    ] = "CURRENT"
    forged_audit = audit_correction_mutation_authority(
        {
            "schema_version": "final-review-audit.v1",
            "status": "APPLIED",
            "findings": [forged],
            "applied_count": 1,
        }
    )
    assert forged_audit["status"] == "BLOCK"


def test_context_adjudication_rejects_unbound_verdict():
    source = _srt("还没有歌杂呢")

    def stale_verdict(request):
        return {
            "schema_version": "subtitle-span-acoustic-check-verdict.v1",
            "request_sha256": "0" * 64,
            "status": "OBSERVED",
            "target_audible": True,
            "current_fit": "PLAUSIBLE",
            "proposed_fit": "SUPPORTED",
        }

    output, audit = adjudicate_context_finding(
        source,
        {
            "cue_index": 1,
            "suspect": "歌杂",
            "suggestion": "歌债",
            "proposed_full_cue": "还没有歌债呢",
            "repair_class": "phonetic",
            "why": "重复话题",
        },
        entity_verifier=stale_verdict,
    )

    assert output == source
    assert audit["status"] == "UNCERTAIN"


def test_context_adjudication_rejects_any_model_returned_text_channel():
    source = _srt("还没有歌杂呢")

    def illicit_text_response(request):
        return {
            "schema_version": "subtitle-span-acoustic-check-verdict.v1",
            "request_sha256": request["request_sha256"],
            "status": "OBSERVED",
            "target_audible": True,
            "current_fit": "PLAUSIBLE",
            "proposed_fit": "SUPPORTED",
            "canonical_entity": "模型自行写出的第三句",
        }

    output, audit = adjudicate_context_finding(
        source,
        {
            "cue_index": 1,
            "suspect": "歌杂",
            "suggestion": "歌债",
            "proposed_full_cue": "还没有歌债呢",
            "repair_class": "phonetic",
        },
        entity_verifier=illicit_text_response,
    )

    assert output == source
    assert audit["status"] == "UNCERTAIN"


def test_source_backed_entity_insertion_survives_contract(monkeypatch):
    """2026-07-18 kmx 整词漏听案：ASR 零召回的专名只能靠插入修复。
    source_backed_entity + 词表见证 + 声学仲裁三重门下允许空 suspect 插入。"""
    srt = (
        "1\n00:00:00,000 --> 00:00:04,000\n只有怎么，你为什么会这样称呼李豆沙\n\n"
        "2\n00:00:05,000 --> 00:00:09,000\n第二句正常文本\n"
    )
    findings = audit_final_subtitles(
        srt,
        llm_call=lambda prompt: json.dumps(
            {
                "findings": [
                    {
                        "cue": 1,
                        "kind": "entity",
                        "proposed_full_cue": "只有kmx怎么，你为什么会这样称呼李豆沙",
                        "repair_class": "source_backed_entity",
                        "source_surface": "kmx",
                        "evidence_cue_ids": [],
                        "why": "SC称呼串只有kmx会说，ASR整词漏听",
                    }
                ]
            },
            ensure_ascii=False,
        ),
        extract_json=json.loads,
        glossary_text="- 人名/ID：kmx（李豆沙常提的人）",
        structured_context_text="selection_hook: 只有kmx会这样称呼李豆沙",
    )
    assert len(findings) == 1
    finding = findings[0]
    assert finding["suspect"] == ""
    assert finding["suggestion"] == "kmx"
    assert finding.get("suggestion_rejected_reason") is None
    assert finding["candidate_provenance"] == {"kind": "glossary", "surface": "kmx"}

    # 插入建议不走同音自动应用（空 suspect 与 kmx 不同音），必须进声学仲裁。
    output, audit = route_findings(srt, findings)
    assert "只有kmx怎么" not in output
    row = audit["findings"][0]
    assert row["routed"] == "disclosure"

    # 声学仲裁支持插入候选时才真正落地。
    def prefer_proposed(request):
        return _witness(
            request,
            "zhi you kmx zen me ni wei shen me hui zhe yang cheng hu li dou sha",
        )

    repaired_srt, adj = adjudicate_context_finding(
        srt, findings[0], entity_verifier=prefer_proposed,
        judge_llm_call=_judge("PROPOSED"),
    )
    assert adj["repaired"] is True
    assert "只有kmx怎么" in repaired_srt


def test_plain_insertion_without_source_provenance_still_rejected():
    """非 source_backed 的插入建议依旧被拒（防审片员自由加词）。"""
    srt = "1\n00:00:00,000 --> 00:00:04,000\n只有怎么会这样\n"
    with pytest.raises(FinalReviewAuditError) as raised:
        audit_final_subtitles(
            srt,
            llm_call=lambda prompt: json.dumps(
                {
                    "findings": [
                        {
                            "cue": 1,
                            "kind": "context",
                            "proposed_full_cue": "只有他怎么会这样",
                            "repair_class": "phonetic",
                            "why": "凭感觉加词",
                        }
                    ]
                },
                ensure_ascii=False,
            ),
            extract_json=json.loads,
        )
    assert (
        raised.value.reason_code
        == "FINAL_REVIEW_RESPONSE_FINDINGS_ALL_INVALID"
    )


def test_spoken_unit_insertion_is_bounded_then_goes_through_cpa():
    """A missing ordinary spoken word is not an entity-only insertion.

    The full-cue diff owns an empty span, but the candidate still cannot
    mutate bytes until AGY supplies candidate-blind pronunciation evidence
    and CPA chooses PROPOSED from the closed set.
    """

    source = _srt("为什么是")
    findings = audit_final_subtitles(
        source,
        llm_call=_fake_llm(
            [
                {
                    "cue": 1,
                    "kind": "context",
                    "proposed_full_cue": "为什么是又",
                    "repair_class": "spoken_unit",
                    "why": "疑似漏了追问里的又",
                }
            ]
        ),
        extract_json=json.loads,
    )

    finding = findings[0]
    assert finding["suspect"] == ""
    assert finding["suggestion"] == "又"
    assert finding["span_start_codepoint"] == finding["span_end_codepoint"]
    routed, audit = route_findings(source, findings)
    assert routed == source
    assert audit["findings"][0]["routed"] == "disclosure"

    repaired, adjudication = adjudicate_context_finding(
        source,
        finding,
        entity_verifier=lambda request: _witness(
            request, "wei shen me shi you"
        ),
        judge_llm_call=_judge("PROPOSED"),
    )
    assert "为什么是又" in repaired
    assert adjudication["decision_authority"] == "CPA_JUDGE"


def test_acoustic_partial_delete_removes_only_unspoken_prefix():
    source = _srt("我草，乱说的啊")
    findings = audit_final_subtitles(
        source,
        llm_call=_fake_llm(
            [
                {
                    "cue": 1,
                    "kind": "context",
                    "proposed_full_cue": "乱说的啊",
                    "repair_class": "acoustic_delete",
                    "why": "前缀疑似无声，后半句有口播",
                }
            ]
        ),
        extract_json=json.loads,
    )
    assert findings[0]["suggestion"] == ""
    routed, route_audit = route_findings(source, findings)
    assert routed == source
    assert route_audit["findings"][0]["routed"] == "disclosure"

    def strict_delete(request):
        return _witness(request, "luan shuo de a")

    repaired, audit = adjudicate_context_finding(
        source, findings[0], entity_verifier=strict_delete,
        judge_llm_call=_judge("PROPOSED"),
    )
    assert "我草" not in repaired
    assert "乱说的啊" in repaired
    assert audit["policy_branch"] == "WITNESS_JUDGE_APPLY_PROPOSED"


def test_acoustic_drop_cue_requires_whole_target_inaudible():
    source = _srt("我草")
    findings = audit_final_subtitles(
        source,
        llm_call=_fake_llm(
            [
                {
                    "cue": 1,
                    "kind": "context",
                    "proposed_full_cue": "",
                    "repair_class": "acoustic_drop_cue",
                    "why": "整条疑似无声幻听",
                }
            ]
        ),
        extract_json=json.loads,
    )
    assert findings[0]["suspect"] == "我草"

    def inaudible(request):
        return _witness(request, "?", audible=False, uncertain=(0,))

    repaired, audit = adjudicate_context_finding(
        source,
        findings[0],
        entity_verifier=inaudible,
        judge_llm_call=_judge("PROPOSED"),
    )
    assert repaired == ""
    assert audit["repaired"] is True
    assert audit["policy_branch"] == "CPA_JUDGE_APPLY_INAUDIBLE_DROP_CUE"


def test_glossary_near_homophone_uses_expected_value_canon():
    """未登记误听面→glossary 规范词走高收益零 CPA 通道。"""
    srt = _srt("只剩下核酸天下了", "第二句正常文本")
    findings = audit_final_subtitles(
        srt,
        llm_call=_fake_llm(
            [
                {
                    "cue": 1,
                    "kind": "context",
                    "proposed_full_cue": "只剩下和成天下了",
                    "repair_class": "phonetic",
                    "source_surface": "和成天下",
                    "why": "槟榔品牌语境，核酸天下不成词",
                }
            ]
        ),
        extract_json=json.loads,
        glossary_text="- 品牌/话题词：和成天下（槟榔品牌）",
    )
    assert findings[0]["candidate_provenance"] == {"kind": "glossary", "surface": "和成天下"}

    output, audit = route_findings(srt, findings)
    assert "和成天下" in output
    row = audit["findings"][0]
    assert row["routed"] == "expected_value_canon"
    assert row["expected_value_gate"]["policy"] == "GLOSSARY_HIGH_PRIOR"
    assert row["expected_value_gate"]["registered_name_conflict"] is False
    assert row["mutation_authority"]["decision_authority"] == (
        "EXPECTED_VALUE_CANON"
    )
    assert audit["applied_count"] == 1
    assert audit_correction_mutation_authority(audit)["status"] == "PASS"


def test_registered_proper_names_are_equal_and_exit_expected_value_lane():
    srt = _srt("我今天在看恋青", "第二句")
    finding = {
        "cue_index": 1,
        "kind": "entity",
        "suspect": "恋青",
        "suggestion": "恋死",
        "proposed_full_cue": "我今天在看恋死",
        "repair_class": "phonetic",
        "candidate_provenance": {"kind": "glossary", "surface": "恋死"},
        "base_text_sha256": hashlib.sha256(
            "我今天在看恋青".encode("utf-8")
        ).hexdigest(),
        "why": "两个登记作品名近音",
    }

    output, audit = route_findings(
        srt,
        [finding],
        protected_term_set=frozenset({"恋青", "恋死"}),
        registered_term_set=frozenset({"恋青", "恋死"}),
    )

    assert output == srt
    assert audit["applied_count"] == 0
    assert audit["findings"][0]["routed"] == "disclosure"
    assert audit["findings"][0]["requires_cpa_judge"] is True
    assert audit["findings"][0]["registered_name_conflict"] is True
    assert audit["findings"][0]["expected_value_gate"] == {
        "schema_version": "glossary-expected-value-gate.v1",
        "status": "BLOCK",
        "policy": "REGISTERED_NAME_EQUALITY",
        "candidate_provenance_kind": "glossary",
        "registered_target": "恋死",
        "current_registered_term": True,
        "proposed_registered_term": True,
        "registered_name_conflict": True,
        "routed": "CPA_REQUIRED",
    }


def test_entity_surface_suspect_never_text_applied():
    """kmx/乒乓球 保向铁律：suspect 是注册实体词面时 T1 让位声学仲裁。"""
    srt = _srt("我的乒乓球又来直播间了", "第二句")
    findings = audit_final_subtitles(
        srt,
        llm_call=_fake_llm(
            [
                {
                    "cue": 1,
                    "kind": "entity",
                    "proposed_full_cue": "我的kmx又来直播间了",
                    "repair_class": "source_backed_entity",
                    "source_surface": "kmx",
                    "why": "帕鲁语境乒乓球是kmx误听",
                }
            ]
        ),
        extract_json=json.loads,
        glossary_text="- 人名/ID：kmx",
    )
    assert findings and findings[0]["suggestion"] == "kmx"

    # protected_term_set 置空以隔离测试实体选边车道本身（真实运行里
    # 乒乓球还会先被词表保护车道拦下——两道防线殊途同归都不许纯文本改写）。
    output, audit = route_findings(
        srt,
        findings,
        protected_term_set=frozenset(),
        entity_surface_set=frozenset({"kmx", "乒乓球"}),
    )
    assert "乒乓球" in output  # 未被纯文本改写
    row = audit["findings"][0]
    assert row["routed"] == "disclosure"
    assert row.get("entity_surface_conflict") is True


def test_witnessed_but_phonetically_distant_stays_disclosure():
    srt = _srt("只剩下苹果手机了", "上面提到和成天下")
    findings = audit_final_subtitles(
        srt,
        llm_call=_fake_llm(
            [
                {
                    "cue": 1,
                    "kind": "context",
                    "proposed_full_cue": "只剩下和成天下了",
                    "repair_class": "phonetic",
                    "source_surface": "和成天下",
                    "why": "强行替换",
                }
            ]
        ),
        extract_json=json.loads,
        glossary_text="- 品牌/话题词：和成天下（槟榔品牌）",
    )
    output, audit = route_findings(srt, findings)
    assert "苹果手机" in output
    assert audit["findings"][0]["routed"] == "disclosure"


def test_same_derived_transcript_witness_requires_acoustic_confirmation():
    """醉堆→这一堆可以由前文召回，但同一 ASR 派生文本不能自证落字。"""
    srt = _srt("旁边这一堆都是新来的", "醉堆小李好可爱哦")
    findings = audit_final_subtitles(
        srt,
        llm_call=_fake_llm(
            [
                {
                    "cue": 2,
                    "kind": "context",
                    "proposed_full_cue": "这一堆小李好可爱哦",
                    "repair_class": "phonetic",
                    "source_surface": "这一堆",
                    "why": "醉堆不成词，前文刚说旁边这一堆",
                }
            ]
        ),
        extract_json=json.loads,
    )
    assert findings[0]["candidate_provenance"]["kind"] == "transcript_context"
    assert findings[0]["candidate_provenance"]["nearest_cue_distance"] == 1
    assert findings[0]["force_acoustic"] is True
    assert findings[0]["correlated_text_witness"] is True

    output, audit = route_findings(srt, findings)
    assert "醉堆小李好可爱哦" in output
    row = audit["findings"][0]
    assert row["routed"] == "disclosure"


def test_widened_span_does_not_admit_absurd_shared_tail():
    """反例：苹果天下→和成天下（词表见证 + 共享「天下」尾巴）不许被扩窗
    量法抬上线——扩窗只放 1 共享字 + 0.65 高阈值。"""
    srt = _srt("只剩下苹果天下了", "第二句")
    findings = audit_final_subtitles(
        srt,
        llm_call=_fake_llm(
            [
                {
                    "cue": 1,
                    "kind": "context",
                    "proposed_full_cue": "只剩下和成天下了",
                    "repair_class": "phonetic",
                    "source_surface": "和成天下",
                    "why": "强行替换",
                }
            ]
        ),
        extract_json=json.loads,
        glossary_text="- 品牌/话题词：和成天下（槟榔品牌）",
    )
    output, audit = route_findings(srt, findings)
    assert "苹果天下" in output
    assert audit["findings"][0]["routed"] == "disclosure"


def test_candidate_only_memory_cannot_be_promoted_to_source_witness():
    source = _srt("就是霸凌的那种小的吧")
    findings = audit_final_subtitles(
        source,
        llm_call=_fake_llm(
            [
                {
                    "cue": 1,
                    "kind": "context",
                    "proposed_full_cue": "就是霸凌的那种晓得吧",
                    "repair_class": "source_backed_entity",
                    "source_surface": "晓得吧",
                    "why": "长期口癖候选",
                }
            ]
        ),
        extract_json=json.loads,
        candidate_context_text="idiolect candidate only: 晓得吧",
    )

    assert len(findings) == 1
    assert findings[0]["suggestion"] is None
    assert (
        findings[0]["suggestion_rejected_reason"]
        == "ENTITY_SOURCE_SURFACE_UNWITNESSED"
    )


def test_candidate_only_memory_forces_acoustic_even_when_homophone():
    source = _srt("就是霸凌的那种小的吧")
    memory_id = "lidousha.idiolect.xiaodeba.r1"
    candidate_context = {
        "speech_memory": {
            "ledger_sha256": "sha256:" + "a" * 64,
            "entries": [
                {
                    "memory_id": memory_id,
                    "candidate_canonicals": ["晓得吧"],
                }
            ],
        }
    }
    findings = audit_final_subtitles(
        source,
        llm_call=_fake_llm(
            [
                {
                    "cue": 1,
                    "kind": "context",
                    "proposed_full_cue": "就是霸凌的那种晓得吧",
                    "repair_class": "phonetic",
                    "candidate_memory_id": memory_id,
                    "why": "长期口癖候选",
                }
            ]
        ),
        extract_json=json.loads,
        candidate_context_text="candidate only",
        candidate_context=candidate_context,
    )
    assert findings[0]["force_acoustic"] is True
    assert findings[0]["candidate_provenance"]["kind"] == "speech_memory_candidate"

    output, audit = route_findings(source, findings, protected_term_set=frozenset())
    assert "小的吧" in output
    assert audit["findings"][0]["routed"] == "disclosure"


def test_candidate_memory_display_prefix_is_normalized_only_to_verified_id():
    source = _srt("她是一个桔梗妹")
    memory_id = "lidousha.idiolect.jieganmei.r1"
    candidate_context = {
        "speech_memory": {
            "ledger_sha256": "sha256:" + "b" * 64,
            "entries": [
                {
                    "memory_id": memory_id,
                    "candidate_canonicals": ["姐感妹"],
                }
            ],
        }
    }
    findings = audit_final_subtitles(
        source,
        llm_call=_fake_llm(
            [
                {
                    "cue": 1,
                    "kind": "context",
                    "proposed_full_cue": "她是一个姐感妹",
                    "repair_class": "phonetic",
                    "candidate_memory_id": f"id={memory_id}",
                    "why": "模型复制了上下文展示标签",
                }
            ]
        ),
        extract_json=json.loads,
        candidate_context_text=f"id={memory_id}",
        candidate_context=candidate_context,
    )

    assert findings[0]["suggestion"] == "姐感"
    assert findings[0]["candidate_memory_id"] == memory_id
    assert findings[0]["candidate_memory_id_raw"] == f"id={memory_id}"
    assert findings[0]["force_acoustic"] is True
    assert findings[0]["candidate_provenance"]["memory_id"] == memory_id


def _weak_witness(request, heard, *, confidence=0.6, uncertain=()):
    return {
        "schema_version": "subtitle-span-acoustic-witness.v1",
        "request_sha256": request["request_sha256"],
        "status": "OBSERVED",
        "target_audible": True,
        "heard_pinyin": heard,
        "uncertain_positions": list(uncertain),
        "syllable_count": len(heard.split()),
        "confidence": confidence,
        "reason": "fast speech",
    }


def test_screen_read_escalation_recovers_fast_spoken_ui_line():
    """424_522 1:24 案（Ivan 2026-07-27）：快速念屏「战斗回合用尽，即将
    离开战场」音频糊——弱证词触发读屏，OCR 池拼音对齐命中，verified_ocr
    出处进入同一裁决引擎，judge PROPOSED 后施改。"""

    source = _srt("战斗回合永进即将离开占场")
    finding = {
        "cue_index": 1,
        "kind": "context",
        "suspect": "永进",
        "suggestion": "用尽",
        "proposed_full_cue": "战斗回合用尽即将离开占场",
        "repair_class": "phonetic",
        "why": "语境不通",
    }
    probes = []

    def fake_screen_probe(start_ms, end_ms):
        probes.append((start_ms, end_ms))
        return {
            "schema_version": "screen-read-witness.v1",
            "media_path": "/x/padded.mp4",
            "span_start_ms": start_ms,
            "span_end_ms": end_ms,
            "frame_ms": [start_ms + 100],
            "pool": ["战斗回合用尽，即将离开战场", "设置", "退出对局"],
        }

    output, audit = adjudicate_context_finding(
        source,
        finding,
        entity_verifier=lambda request: _weak_witness(
            request,
            "zhan dou hui he yong jin ji jiang li kai zhan chang",
            confidence=0.6,
        ),
        judge_llm_call=_judge("PROPOSED"),
        screen_read_probe=fake_screen_probe,
    )

    assert probes, "弱证词必须触发读屏探针"
    assert "战斗回合用尽，即将离开战场" in output
    assert audit["repaired"] is True
    esc = audit["screen_read_witness"]
    assert esc["source"] == "screen_frames"
    assert esc["match"]["text"] == "战斗回合用尽，即将离开战场"
    assert audit["request"]["candidate_provenance"]["kind"] == "verified_ocr"
    assert audit["mutation_authority"]["status"] == "PASS"


def test_semantic_trigger_prefers_danmaku_pool_before_frames():
    """Ivan 同日扩展：语境不通（有 finding 无出处）即触发，且弹幕池先于
    画面——池命中时探针一次都不该调用，出处=structured_chat_bound。"""

    source = _srt("大家说的都是磨牙好赢")
    finding = {
        "cue_index": 1,
        "kind": "context",
        "suspect": "磨牙好赢",
        "suggestion": "摩耶好楹",
        "proposed_full_cue": "大家说的都是摩耶好楹",
        "repair_class": "phonetic",
        "why": "语境不通",
    }
    probes = []

    def never_screen_probe(start_ms, end_ms):
        probes.append((start_ms, end_ms))
        return {"pool": []}

    output, audit = adjudicate_context_finding(
        source,
        finding,
        entity_verifier=lambda request: _weak_witness(
            request,
            "da jia shuo de dou shi mo ye hao ying",
            confidence=0.95,  # 听得清——触发靠语境（finding 无出处）
        ),
        judge_llm_call=_judge("PROPOSED"),
        clip_context={
            "structured_chat": [
                {"sender": "观众A", "text": "魔涯号营来啦"},
                {"sender": "观众B", "text": "今天天气不错"},
            ]
        },
        screen_read_probe=never_screen_probe,
    )

    assert probes == [], "弹幕池命中时不许烧视觉调用"
    assert "大家说的都是魔涯号营来啦" in output  # 嵌入式拼接，句架保留
    esc = audit["screen_read_witness"]
    assert esc["source"] == "danmaku_pool"
    assert (
        audit["request"]["candidate_provenance"]["kind"]
        == "structured_chat_bound"
    )
