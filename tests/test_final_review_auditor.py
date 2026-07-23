import json

from src.autoslice.final_review_auditor import (
    adjudicate_context_finding,
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


def test_router_applies_only_homophone_fixes():
    """审片员是发现器不是改写器：同音建议(季下→记下)自动应用；非同音建议
    (小雨→小李)只披露不落盘。"""
    source = _srt("欢迎季下", "今天小雨来了没")
    findings = [
        {"cue_index": 1, "suspect": "季下", "kind": "nonword", "suggestion": "记下", "why": "非词"},
        {"cue_index": 2, "suspect": "小雨", "kind": "self_ref", "suggestion": "小李", "why": "自称可疑"},
    ]

    output, audit = route_findings(source, findings, protected_term_set=frozenset())

    assert "欢迎记下" in output
    assert "小雨" in output  # 非同音建议不改写
    assert audit["applied_count"] == 1
    routed = {row["suspect"]: row["routed"] for row in audit["findings"]}
    assert routed == {"季下": "homophone_fix", "小雨": "disclosure"}


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


def test_auditor_llm_failure_returns_empty():
    def broken(prompt):
        raise RuntimeError("cpa down")

    assert (
        audit_final_subtitles(_srt("一句"), llm_call=broken, extract_json=_extract) == []
    )


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
    assert len(request["request_sha256"]) == 64


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
        return {
            "schema_version": "subtitle-span-acoustic-check-verdict.v1",
            "request_sha256": request["request_sha256"],
            "status": "OBSERVED",
            "target_audible": True,
            "current_fit": "PLAUSIBLE",
            "proposed_fit": "SUPPORTED",
            "heard_syllables": "ge zhai",
        }

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
    )

    assert "还没有歌债呢" in output
    assert "00:00:10,000 --> 00:00:14,000" in output
    assert audit["repaired"] is True
    assert audit["request"]["current_cue"] == "还没有歌杂呢"
    assert audit["verdict"]["heard_syllables"] == "ge zhai"


def test_context_adjudication_keeps_current_when_semantics_conflict_with_audio():
    source = _srt("还没有歌杂呢")

    def acoustics_veto_proposal(request):
        return {
            "schema_version": "subtitle-span-acoustic-check-verdict.v1",
            "request_sha256": request["request_sha256"],
            "status": "OBSERVED",
            "target_audible": True,
            "current_fit": "SUPPORTED",
            "proposed_fit": "INCOMPATIBLE",
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
        entity_verifier=acoustics_veto_proposal,
    )

    assert output == source
    assert audit["status"] == "OBSERVED"
    assert audit["repaired"] is False
    assert audit["policy_branch"] == "PROPOSED_INCOMPATIBLE_KEEP_CURRENT"


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
        assert request["proposed_cue"] == "只有kmx怎么，你为什么会这样称呼李豆沙"
        return {
            "schema_version": "subtitle-span-acoustic-check-verdict.v1",
            "request_sha256": request["request_sha256"],
            "status": "OBSERVED",
            "target_audible": True,
            "current_fit": "UNRESOLVED",
            "proposed_fit": "SUPPORTED",
        }

    repaired_srt, adj = adjudicate_context_finding(
        srt, findings[0], entity_verifier=prefer_proposed
    )
    assert adj["repaired"] is True
    assert "只有kmx怎么" in repaired_srt


def test_plain_insertion_without_source_provenance_still_rejected():
    """非 source_backed 的插入建议依旧被拒（防审片员自由加词）。"""
    srt = "1\n00:00:00,000 --> 00:00:04,000\n只有怎么会这样\n"
    findings = audit_final_subtitles(
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
    assert findings == [] or all(
        row.get("suggestion") is None for row in findings
    )


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
        return {
            "schema_version": "subtitle-span-acoustic-check-verdict.v1",
            "request_sha256": request["request_sha256"],
            "status": "OBSERVED",
            "target_audible": True,
            "current_fit": "INCOMPATIBLE",
            "proposed_fit": "SUPPORTED",
        }

    repaired, audit = adjudicate_context_finding(
        source, findings[0], entity_verifier=strict_delete
    )
    assert "我草" not in repaired
    assert "乱说的啊" in repaired
    assert audit["policy_branch"] == "ACOUSTIC_PARTIAL_DELETE_STRICT_APPLY"


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
        return {
            "schema_version": "subtitle-span-acoustic-check-verdict.v1",
            "request_sha256": request["request_sha256"],
            "status": "OBSERVED",
            "target_audible": False,
            "current_fit": "UNRESOLVED",
            "proposed_fit": "UNRESOLVED",
        }

    repaired, audit = adjudicate_context_finding(
        source, findings[0], entity_verifier=inaudible
    )
    assert repaired == ""
    assert audit["repaired"] is True
    assert audit["policy_branch"] == "TARGET_INAUDIBLE_DROP_CUE"


def test_witnessed_near_homophone_applies_without_audio():
    """T1 车道（2026-07-19 额度事故重构）：词表见证 + 拼音近音 + 非实体选边
    → 纯文本应用，零外部调用（核酸天下→和成天下案型）。"""
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
    assert "核酸天下" not in output
    row = audit["findings"][0]
    assert row["routed"] == "witnessed_near_homophone_fix"
    assert row["pinyin_similarity"] >= 0.45
    assert audit["applied_count"] == 1


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
