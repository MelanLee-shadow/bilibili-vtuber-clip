from types import SimpleNamespace

from src.autoslice.subtitle_fidelity import (
    apply_numeric_fact_provenance_guard,
    apply_impossible_punctuation_guard,
    apply_source_language_preservation_guard,
    apply_subtitle_fidelity_guard,
    apply_title_mark_balance_guard,
    audit_foreign_script_consistency,
    digit_reading_equivalent,
    mixed_cjk_latin_findings_covered_by_overrides,
    resolve_deferred_foreign_introductions,
    unproven_foreign_introductions_covered_by_overrides,
)


def _srt(*texts: str) -> str:
    blocks = []
    for index, text in enumerate(texts, start=1):
        blocks.append(
            f"{index}\n00:00:{index * 5:02d},000 --> 00:00:{index * 5 + 4:02d},000\n{text}"
        )
    return "\n\n".join(blocks) + "\n"


def test_late_authority_resolves_only_fully_owned_introduced_kana():
    audit = {
        "unproven_foreign_introductions": [
            {
                "cue_index": 37,
                "start_ms": 85_220,
                "end_ms": 87_900,
                "draft": "分牙三四关就毁神",
                "attempted": "非常やさしい，就病院坂灵",
            }
        ]
    }
    authority_rows = [
        {
            "truth_id": "reviewed-cue",
            "local_windows": [{"start_ms": 85_000, "end_ms": 88_000}],
        }
    ]
    final_srt = (
        "1\n00:01:25,000 --> 00:01:28,000\n分牙三四关就毁神——\n"
    )

    resolution = resolve_deferred_foreign_introductions(
        audit,
        final_srt,
        authority_rows=authority_rows,
        authority_kind="source_subtitle_truth",
    )

    assert resolution["status"] == "PASS"
    assert resolution["findings"][0]["authority_ids"] == ["reviewed-cue"]
    assert resolution["findings"][0]["introduced_surfaces"] == ["やさしい"]


def test_source_truth_can_positively_witness_declared_kana_name():
    audit = {
        "unproven_foreign_introductions": [
            {
                "cue_index": 78,
                "start_ms": 145_860,
                "end_ms": 148_020,
                "draft": "谢谢小路路口的钢棒",
                "attempted": "谢谢小凑るう子的钢镚",
            }
        ]
    }
    final_srt = (
        "1\n00:02:25,860 --> 00:02:28,020\n谢谢小凑るう子的钢镚\n"
    )

    resolution = resolve_deferred_foreign_introductions(
        audit,
        final_srt,
        authority_rows=[
            {
                "truth_id": "structured-sc-sender",
                "assertion_state": "VERIFIED_ACTIVE",
                "entry_sha256": "sha256:" + "a" * 64,
                "action": "replace_cue",
                "declared_output_contract": {
                    "schema_version": "source-truth-declared-output.v1",
                    "action": "replace_cue",
                    "canonical_texts": ["谢谢小凑るう子的钢镚"],
                },
                "local_windows": [
                    {"start_ms": 145_860, "end_ms": 148_020}
                ],
                # Whole-cue output is evidence only; authorization comes from
                # the committed ``text`` field above.
                "after": ["谢谢小凑るう子的钢镚"],
            }
        ],
        authority_kind="source_subtitle_truth",
    )

    assert resolution["status"] == "PASS"
    finding = resolution["findings"][0]
    assert finding["resolved"] is True
    assert finding["witnessed_surfaces"] == ["るう"]
    assert finding["positive_witness_authority_ids"] == [
        "structured-sc-sender"
    ]


def test_unrelated_source_truth_canonical_does_not_witness_kana():
    audit = {
        "unproven_foreign_introductions": [
            {
                "cue_index": 78,
                "start_ms": 145_860,
                "end_ms": 148_020,
                "attempted": "谢谢小凑るう子的钢镚",
            }
        ]
    }
    final_srt = (
        "1\n00:02:25,860 --> 00:02:28,020\n谢谢小凑るう子的钢镚\n"
    )

    resolution = resolve_deferred_foreign_introductions(
        audit,
        final_srt,
        authority_rows=[
            {
                "truth_id": "unrelated-truth",
                "assertion_state": "VERIFIED_ACTIVE",
                "entry_sha256": "sha256:" + "b" * 64,
                "action": "replace_cue",
                "declared_output_contract": {
                    "schema_version": "source-truth-declared-output.v1",
                    "action": "replace_cue",
                    "canonical_texts": ["这里是邪恶守宫"],
                },
                "local_windows": [
                    {"start_ms": 145_000, "end_ms": 149_000}
                ],
                # A broad post-edit cue must not become a positive witness.
                "after": ["谢谢小凑るう子的钢镚"],
            }
        ],
        authority_kind="source_subtitle_truth",
    )

    assert resolution["status"] == "FAILED"
    assert resolution["failures"][0]["reason_code"] == (
        "INTRODUCED_FOREIGN_SURFACE_SURVIVED"
    )


def test_non_source_truth_authority_cannot_positive_witness_kana():
    audit = {
        "unproven_foreign_introductions": [
            {
                "cue_index": 1,
                "start_ms": 5_000,
                "end_ms": 9_000,
                "attempted": "谢谢小凑るう子的钢镚",
            }
        ]
    }
    resolution = resolve_deferred_foreign_introductions(
        audit,
        _srt("谢谢小凑るう子的钢镚"),
        authority_rows=[
            {
                "truth_id": "not-source-truth",
                "assertion_state": "VERIFIED_ACTIVE",
                "entry_sha256": "sha256:" + "c" * 64,
                "action": "replace_cue",
                "declared_output_contract": {
                    "schema_version": "source-truth-declared-output.v1",
                    "action": "replace_cue",
                    "canonical_texts": ["谢谢小凑るう子的钢镚"],
                },
                "local_windows": [{"start_ms": 5_000, "end_ms": 9_000}],
            }
        ],
        authority_kind="hash_bound_redelivery_baseline",
    )

    assert resolution["status"] == "FAILED"
    assert resolution["failures"][0]["reason_code"] == (
        "INTRODUCED_FOREIGN_SURFACE_SURVIVED"
    )


def test_late_authority_blocks_partial_ownership_or_surviving_kana():
    audit = {
        "unproven_foreign_introductions": [
            {
                "cue_index": 37,
                "start_ms": 85_220,
                "end_ms": 87_900,
                "attempted": "非常やさしい，就病院坂灵",
            }
        ]
    }
    final_srt = (
        "1\n00:01:25,000 --> 00:01:28,000\n仍然非常やさしい\n"
    )

    partial = resolve_deferred_foreign_introductions(
        audit,
        final_srt,
        authority_rows=[
            {
                "truth_id": "partial",
                "local_windows": [{"start_ms": 86_680, "end_ms": 88_000}],
            }
        ],
        authority_kind="source_subtitle_truth",
    )
    surviving = resolve_deferred_foreign_introductions(
        audit,
        final_srt,
        authority_rows=[
            {
                "truth_id": "full",
                "local_windows": [{"start_ms": 85_000, "end_ms": 88_000}],
            }
        ],
        authority_kind="source_subtitle_truth",
    )

    assert partial["status"] == "FAILED"
    assert partial["failures"][0]["reason_code"] == (
        "FINDING_NOT_FULLY_AUTHORITY_OWNED"
    )
    assert surviving["status"] == "FAILED"
    assert surviving["failures"][0]["reason_code"] == (
        "INTRODUCED_FOREIGN_SURFACE_SURVIVED"
    )


def test_number_paraphrase_reverts_to_draft_without_witness():
    """2026-07-11 实案：BCUT「190」被修正层转述成「一米九」。一九零是梗，
    无证人时必须忠实原文。"""
    draft = _srt("就是她小时候被那个190粉毛花臂抢了三部手机")
    final = _srt("就是她小时候被那个一米九粉毛花臂抢了三部手机")

    guarded, audit = apply_subtitle_fidelity_guard(draft, final, agy_srt=None, sanctioned=())

    assert "190" in guarded
    assert "一米九" not in guarded
    assert audit["status"] == "APPLIED"
    assert audit["reverted"][0]["violations"][0]["reason"] == "REPLACE_UNWITNESSED"


def test_digit_reading_rewrite_passes():
    draft = _srt("就是她被那个190粉毛抢了手机")
    final = _srt("就是她被那个一九零粉毛抢了手机")

    guarded, audit = apply_subtitle_fidelity_guard(draft, final, agy_srt=None, sanctioned=())

    assert "一九零" in guarded
    assert audit["status"] == "CLEAN"
    assert digit_reading_equivalent("190", "幺九零")
    assert not digit_reading_equivalent("190", "一米九")


def test_unwitnessed_name_guess_reverts_but_agy_witness_passes():
    """2026-07-11 生日结婚实案：BCUT 误听「留下」，AGY 缺席时 CPA 猜成
    「小李」= 无证改写必须回退；AGY 在场且听到「李豆沙」则采信。"""
    draft = _srt("所以你是想看留下跟别人亲亲")
    guessed = _srt("所以你是想看小李跟别人亲亲")

    guarded, audit = apply_subtitle_fidelity_guard(draft, guessed, agy_srt=None, sanctioned=())
    assert "留下" in guarded
    assert "小李" not in guarded
    assert audit["reverted_count"] == 1

    agy = _srt("所以你是想看李豆沙跟别人亲亲")
    adopted = _srt("所以你是想看李豆沙跟别人亲亲")
    guarded2, audit2 = apply_subtitle_fidelity_guard(draft, adopted, agy_srt=agy, sanctioned=())
    assert "李豆沙" in guarded2
    assert audit2["status"] == "CLEAN"


def test_sanctioned_table_and_homophone_sets_pass():
    draft = _srt("我是直女啊", "他今天来了吗")
    final = _srt("我是侄女啊", "她今天来了嘛")

    guarded, audit = apply_subtitle_fidelity_guard(
        draft, final, agy_srt=None, sanctioned=(("直女", "侄女"),)
    )

    assert "侄女" in guarded
    assert "她今天来了嘛" in guarded
    assert audit["status"] == "CLEAN"


def test_unacoustically_authorized_drop_reverts_but_particle_trim_is_allowed():
    draft = _srt("虫儿飞虫儿飞", "就是呢那个她们要去彩排了")
    final = _srt("", "就是那个她们要去彩排了")

    guarded, audit = apply_subtitle_fidelity_guard(draft, final, agy_srt=None, sanctioned=())

    assert audit["hallucination_drops"] == []
    assert audit["unauthorized_drops_reverted"][0]["cue_index"] == 1
    assert "虫儿飞虫儿飞" in guarded
    assert "就是那个她们要去彩排了" in guarded
    assert audit["status"] == "APPLIED"


def test_empty_question_cue_without_acoustic_authority_is_restored():
    draft = _srt("这是什么")
    corrected = _srt("")

    guarded, audit = apply_subtitle_fidelity_guard(
        draft, corrected, agy_srt=None, sanctioned=()
    )

    assert "这是什么" in guarded
    assert audit["hallucination_drops"] == []
    assert audit["unauthorized_drops_reverted"][0]["reason_code"] == (
        "CUE_DELETION_REQUIRES_ACOUSTIC_AUTHORITY"
    )


def test_missing_final_cue_with_agy_audio_witness_is_restored():
    draft = _srt("情切意不")
    agy = _srt("情切意不")

    guarded, audit = apply_subtitle_fidelity_guard(
        draft,
        "",
        agy_srt=agy,
        sanctioned=(),
    )

    assert "情切意不" in guarded
    restored = audit["unauthorized_drops_reverted"][0]
    assert restored["agy_same_timing_text"] == "情切意不"
    assert audit["alignment_gaps"][0]["reason_code"] == (
        "FINAL_CUE_MISSING_UNAUTHORIZED"
    )


def test_pinyin_homophone_respell_passes_without_witness():
    """2026-07-14 五年之约冤杀案回归：季下→记下(jì同音)、小丽→小李(lǐ同音)
    都保留读音，但「小+姓」是人名写法槽，音频不能自证李/丽；普通词法重拼
    仍可放行，姓名写法与转述都回退。"""
    draft = _srt("欢迎季下", "小丽只是恰好处在一个", "就是她被那个190粉毛抢了手机")
    final = _srt("欢迎记下", "小李只是恰好处在一个", "就是她被那个一米九粉毛抢了手机")

    guarded, audit = apply_subtitle_fidelity_guard(draft, final, agy_srt=None, sanctioned=())

    assert "欢迎记下" in guarded
    assert "小丽只是恰好处在一个" in guarded
    assert "小李只是恰好处在一个" not in guarded
    assert "一米九" not in guarded  # 转述仍被回退
    assert audit["reverted_count"] == 2


def test_agy_cannot_self_witness_homophone_name_orthography():
    draft = _srt("毁神应该也一样吧")
    guessed = _srt("灰神应该也一样吧")

    guarded, audit = apply_subtitle_fidelity_guard(
        draft, guessed, agy_srt=guessed, sanctioned=()
    )

    assert "毁神应该也一样吧" in guarded
    assert "灰神" not in guarded
    assert audit["reverted"][0]["violations"][0]["reason"] == (
        "HOMOPHONE_NAME_ORTHOGRAPHY_UNWITNESSED"
    )


def test_name_orthography_guard_preserves_other_agy_edits_in_same_cue():
    draft = _srt("毁神发了一句拯救理解成功")
    refined = _srt("灰神发了一句“拯救李姐成功”")

    guarded, audit = apply_subtitle_fidelity_guard(
        draft, refined, agy_srt=refined, sanctioned=()
    )

    assert "毁神发了一句“拯救李姐成功”" in guarded
    assert "灰神" not in guarded
    assert audit["reverted_count"] == 1


def test_agy_cannot_drop_name_suffix_into_near_homophone_common_word():
    """毁神→绘声 changes the whole two-character span, so the suffix is no
    longer adjacent to SequenceMatcher's edit.  The draft name morphology and
    the bounded shen/sheng nasal-final drift must still keep its spelling."""

    draft = _srt("然后毁神什么都没有做")
    refined = _srt("然后绘声什么都没有做")

    guarded, audit = apply_subtitle_fidelity_guard(
        draft, refined, agy_srt=refined, sanctioned=()
    )

    assert "然后毁神什么都没有做" in guarded
    assert "绘声" not in guarded
    assert audit["reverted_count"] == 1
    assert audit["reverted"][0]["violations"][0]["reason"] == (
        "HOMOPHONE_NAME_ORTHOGRAPHY_UNWITNESSED"
    )


def test_registered_mapping_can_authorize_homophone_name_orthography():
    draft = _srt("毁神应该也一样吧")
    corrected = _srt("灰神应该也一样吧")

    guarded, audit = apply_subtitle_fidelity_guard(
        draft,
        corrected,
        agy_srt=corrected,
        sanctioned=(("毁神", "灰神"),),
    )

    assert "灰神应该也一样吧" in guarded
    assert audit["reverted_count"] == 0


def test_agy_cannot_rewrite_question_intent_without_text_authority():
    draft = _srt("那那个时候你是什么呢")
    paraphrased = _srt("那那个时候你是怎么排的")

    guarded, audit = apply_subtitle_fidelity_guard(
        draft, paraphrased, agy_srt=paraphrased, sanctioned=()
    )

    assert "那那个时候你是什么呢" in guarded
    assert "怎么排的" not in guarded
    assert audit["reverted"][0]["violations"][0]["reason"] == (
        "QUESTION_INTENT_UNWITNESSED"
    )


def test_cue_count_mismatch_is_aligned_to_draft_timing_instead_of_skipped():
    draft = _srt("一句")
    final = _srt("一句", "多出来的")

    guarded, audit = apply_subtitle_fidelity_guard(draft, final, agy_srt=None, sanctioned=())

    assert audit["status"] == "ALIGNED_WITH_GAPS"
    assert "一句" in guarded
    assert "多出来的" not in guarded
    assert audit["ignored_final_cues"][0]["reason_code"] == (
        "FINAL_CUE_HAS_NO_DRAFT_TIMING_KEY"
    )


def test_guard_reverts_only_bad_span_and_keeps_sanctioned_name_span():
    draft = _srt("让刘莎线下叫提莫怂")
    final = _srt("让礼墨线下叫kmx")

    guarded, audit = apply_subtitle_fidelity_guard(
        draft,
        final,
        sanctioned=(("提莫怂", "kmx"),),
    )

    assert "让刘莎线下叫kmx" in guarded
    assert "让礼墨" not in guarded
    assert audit["status"] == "APPLIED"
    assert audit["reverted"][0]["kept"] == "让刘莎线下叫kmx"


def test_hash_bound_fallback_can_only_support_a_repeated_name_slot():
    draft = _srt("请问熊在线下说", "但是因为提")
    corrected = _srt("kmx在线下说", "但是因为kmx")
    fallback = corrected

    guarded, audit = apply_subtitle_fidelity_guard(
        draft,
        corrected,
        corroborating_srt=fallback,
        sanctioned=(("请问熊", "kmx"),),
    )

    assert "kmx在线下说" in guarded
    assert "但是因为kmx" in guarded
    assert audit["status"] == "CLEAN"

    single_guarded, _single_audit = apply_subtitle_fidelity_guard(
        _srt("但是因为提"),
        _srt("但是因为kmx"),
        corroborating_srt=_srt("但是因为kmx"),
        sanctioned=(),
    )
    assert "但是因为提" in single_guarded
    assert "但是因为kmx" not in single_guarded


def test_numeric_fact_introduced_by_semantic_lane_without_source_is_reverted():
    draft = _srt("所以在这里喂，哈哈")
    final = _srt("所以在这里为李豆沙做0.4")

    guarded, audit = apply_numeric_fact_provenance_guard(draft, final)

    assert "0.4" not in guarded
    assert "所以在这里喂，哈哈" in guarded
    assert audit["status"] == "REVERTED_UNPROVEN_NUMERIC_FACT"
    assert audit["reverted"][0]["unsupported"][0]["token"] == "0.4"


def test_numeric_fact_survives_when_same_time_structured_chat_contains_it():
    draft = _srt("抽卡惩罚就做这个")
    final = _srt("抽卡惩罚就为礼墨做0.6")
    evidence = [
        SimpleNamespace(
            offset_ms=5_500,
            text="为礼墨做0.6",
            kind="danmaku",
        )
    ]

    guarded, audit = apply_numeric_fact_provenance_guard(
        draft, final, structured_evidence=evidence
    )

    assert "为礼墨做0.6" in guarded
    assert audit["status"] == "CLEAN"


def test_numeric_fact_survives_confirmed_delayed_chat_read_span():
    draft = _srt("大熊猫三个字能说熊猫")
    final = _srt("大熊猫是3个字能说熊猫")
    raw_evidence = [
        SimpleNamespace(
            offset_ms=-10_000,
            text="大熊猫是3个字",
            kind="danmaku",
        )
    ]
    matched_evidence = [
        {
            "evidence_id": "confirmed-read",
            "kind": "danmaku",
            "exact_text": "大熊猫是3个字",
            "matched_start_ms": 5_000,
            "matched_end_ms": 9_000,
            "survived": True,
        }
    ]

    guarded, audit = apply_numeric_fact_provenance_guard(
        draft,
        final,
        structured_evidence=raw_evidence,
        matched_structured_evidence=matched_evidence,
    )

    assert "大熊猫是3个字能说熊猫" in guarded
    assert audit["status"] == "CLEAN"
    assert audit["supported"][0]["evidence"][0]["basis"] == (
        "chat_authority_matched_spoken_span"
    )


def test_numeric_fact_ignores_unverified_chat_match_span():
    draft = _srt("大熊猫三个字能说熊猫")
    final = _srt("大熊猫是3个字能说熊猫")
    matched_evidence = [
        {
            "evidence_id": "unverified-read",
            "kind": "danmaku",
            "exact_text": "大熊猫是3个字",
            "matched_start_ms": 5_000,
            "matched_end_ms": 9_000,
            "survived": False,
        }
    ]

    guarded, audit = apply_numeric_fact_provenance_guard(
        draft,
        final,
        matched_structured_evidence=matched_evidence,
    )

    assert "大熊猫三个字能说熊猫" in guarded
    assert audit["status"] == "REVERTED_UNPROVEN_NUMERIC_FACT"


def test_source_language_guard_reverts_japanese_speech_translation():
    draft = _srt(
        "フェイトちゃん、テスタロッサさん。私はフェイトちゃんと結婚したいだけなんだけど"
    )
    translated = _srt("Fate Testarossa-san，我只是想和奈叶结婚而已啊！")

    guarded, audit = apply_source_language_preservation_guard(draft, translated)

    assert "フェイトちゃん" in guarded
    assert "只是想和奈叶结婚" not in guarded
    assert audit["status"] == "REVERTED_TRANSLATION"
    assert (
        audit["reverted"][0]["reason"]
        == "SOURCE_LANGUAGE_TRANSLATED_IN_CORRECTION_LANE"
    )


def test_source_language_guard_allows_sanctioned_kana_name_respell():
    draft = _srt("セキちゃんが来た")
    corrected = _srt("萱萱卡娅ちゃんが来た")

    guarded, audit = apply_source_language_preservation_guard(
        draft,
        corrected,
        sanctioned=(("セキ", "萱萱卡娅"),),
    )

    assert "萱萱卡娅ちゃん" in guarded
    assert audit["status"] == "CLEAN"


def test_source_language_guard_reverts_english_speech_translation():
    draft = _srt("I just want to marry Fate, that's all.")
    translated = _srt("我只是想和菲特结婚，仅此而已。")

    guarded, audit = apply_source_language_preservation_guard(draft, translated)

    assert "I just want to marry Fate" in guarded
    assert "我只是想" not in guarded
    assert audit["status"] == "REVERTED_TRANSLATION"


def test_source_language_guard_does_not_restore_mixed_cjk_latin_echo():
    draft = _srt("h tb 这个 NN 和 L 的排列是怎么排的")
    corrected = _srt("这个 NN 和 L 的排列是怎么排的")

    guarded, audit = apply_source_language_preservation_guard(draft, corrected)

    assert "h tb" not in guarded
    assert "这个 NN 和 L" in guarded
    assert audit["status"] == "CLEAN"
    assert audit["reverted_count"] == 0


def test_source_language_guard_does_not_treat_cp_formula_as_english_passage():
    draft = _srt("n n l l")
    corrected = _srt("都是NNLL")

    guarded, audit = apply_source_language_preservation_guard(draft, corrected)

    assert "都是NNLL" in guarded
    assert audit["status"] == "CLEAN"
    assert audit["reverted_count"] == 0


def test_source_language_guard_blocks_unproven_adjacent_kana_introduction():
    draft = _srt("都问那么多", "所有的都为我所用", "正常中文")
    corrected = _srt("どうも、どうも", "すべての、私のために", "正常中文")

    guarded, audit = apply_source_language_preservation_guard(draft, corrected)

    assert "どうも、どうも" in guarded
    assert audit["status"] == "BLOCKED_UNPROVEN_FOREIGN_SPEAKER"
    assert [
        row["cue_index"] for row in audit["unproven_foreign_introductions"]
    ] == [1, 2]


def test_source_language_guard_holds_isolated_japanese_for_speaker_authority():
    draft = _srt("哦，姐姐桑", "正常中文")
    corrected = _srt("お姉さん", "正常中文")

    guarded, audit = apply_source_language_preservation_guard(draft, corrected)

    assert "お姉さん" in guarded
    assert audit["status"] == "BLOCKED_UNPROVEN_FOREIGN_SPEAKER"
    assert audit["unproven_foreign_introductions"][0]["cue_index"] == 1


def test_source_language_guard_keeps_blocking_across_cue_resegmentation():
    draft = _srt("都问那么多，所有的都为我所用", "正常中文")
    corrected = (
        "1\n00:00:05,000 --> 00:00:06,900\nどうも、どうも\n\n"
        "2\n00:00:06,900 --> 00:00:09,000\nすべての、私のために\n\n"
        "3\n00:00:10,000 --> 00:00:14,000\n正常中文\n"
    )

    guarded, audit = apply_source_language_preservation_guard(draft, corrected)

    assert guarded == corrected
    assert audit["draft_cue_count"] == 2
    assert audit["final_cue_count"] == 3
    assert audit["status"] == "BLOCKED_UNPROVEN_FOREIGN_SPEAKER"
    assert [
        row["cue_index"] for row in audit["unproven_foreign_introductions"]
    ] == [1, 2]


def test_unproven_foreign_cluster_defers_only_for_exact_reviewed_repairs():
    draft = _srt("都问那么多", "所有的都为我所用")
    corrected = _srt("どうも、どうも", "すべての、私のために")
    _, audit = apply_source_language_preservation_guard(draft, corrected)
    document = {
        "schema_version": 3,
        "overrides": [
            {
                "action": "replace",
                "expect": {
                    "start": "00:00:05,000",
                    "end": "00:00:09,000",
                    "text": "どうも、どうも",
                },
                "text": "都问那么多",
            },
            {
                "action": "replace",
                "expect": {
                    "start": "00:00:10,000",
                    "end": "00:00:14,000",
                    "text": "すべての、私のために",
                },
                "text": "所有的都为我所用",
            },
        ],
    }

    assert unproven_foreign_introductions_covered_by_overrides(audit, document)
    document["overrides"].pop()
    assert not unproven_foreign_introductions_covered_by_overrides(audit, document)


def test_isolated_background_japanese_can_be_resolved_by_exact_drop_override():
    draft = _srt("所有的都为我所用")
    corrected = _srt("裏表すごいし")
    _, audit = apply_source_language_preservation_guard(draft, corrected)
    document = {
        "schema_version": 3,
        "overrides": [
            {
                "source_cue": 1,
                "action": "drop",
                "expect": {
                    "start": "00:00:05,000",
                    "end": "00:00:09,000",
                    "text": "裏表すごいし",
                },
                "authority": "Ivan confirmed this is watched-video audio",
                "reason": "Background media speech is outside host subtitle authority",
            }
        ],
    }

    assert audit["status"] == "BLOCKED_UNPROVEN_FOREIGN_SPEAKER"
    assert unproven_foreign_introductions_covered_by_overrides(audit, document)


def test_foreign_script_consistency_blocks_japanese_passage_decoded_as_english_word_salad():
    audit = audit_foreign_script_consistency(
        _srt(
            "私は稼げない。",
            "だって小学生だもん。",
            "what's happening ah it's true",
            "you don't know how to answer",
            "普通的中文主播反应",
        )
    )

    assert audit["status"] == "BLOCKED_MIXED_FOREIGN_SCRIPT_CLUSTER"
    assert [row["cue_index"] for row in audit["latin_heavy_cues"]] == [3, 4]


def test_foreign_script_consistency_allows_real_japanese_or_isolated_code_switch():
    audit = audit_foreign_script_consistency(
        _srt("私は稼げない。", "だって小学生だもん。", "AI is useful", "正常中文")
    )

    assert audit["status"] == "CLEAN"


def test_foreign_script_consistency_allows_registered_franchise_and_chat_terms():
    audit = audit_foreign_script_consistency(
        _srt(
            "怎么又把soyo也Mujica？对我们MyGO!!!!!想做什么？",
            "谢谢她的SC，绯闻女友ID已被注册",
            "我想3D Live的宣传图，也想做同样的姿势",
            "下次3Dlive继续参加",
        )
    )

    assert audit["status"] == "CLEAN"


def test_foreign_script_consistency_allows_cp_formulas_beside_one_latin_name():
    audit = audit_foreign_script_consistency(
        _srt(
            "这不是最喜欢的南町nightin吗，LLNNHHB",
            "NNL一般都是NNLL，是吗",
            "一般不是NNLLLHHB或者NN吗",
        )
    )

    assert audit["status"] == "CLEAN"
    assert audit["mixed_cjk_latin_cues"] == []


def test_foreign_script_consistency_treats_uppercase_ta_as_chinese_pronoun():
    audit = audit_foreign_script_consistency(
        _srt("TA说，TA说你不否认拿烟头烫我这件事，说别躲了")
    )

    assert audit["status"] == "CLEAN"
    assert audit["mixed_cjk_latin_cues"] == []


def test_foreign_script_consistency_blocks_unapproved_latin_phrase_inside_chinese_talk():
    audit = audit_foreign_script_consistency(
        _srt("都问那么多，所有的", "don't know那么多，所有的", "玩成Galgame")
    )

    assert audit["status"] == "BLOCKED_MIXED_CJK_LATIN_PHRASE"
    assert audit["mixed_cjk_latin_cues"] == [
        {
            "cue_index": 2,
            "start_ms": 10_000,
            "end_ms": 14_000,
            "text": "don't know那么多，所有的",
            "latin_words": ["don't", "know"],
        }
    ]


def test_mixed_cjk_latin_block_defers_only_for_exact_timeline_bound_repair():
    audit = audit_foreign_script_consistency(
        _srt("正常中文", "don't know那么多，所有的")
    )
    covering = {
        "schema_version": 3,
        "overrides": [
            {
                "action": "replace_substring",
                "locator": {
                    "start": "00:00:09,000",
                    "end": "00:00:15,000",
                },
                "old_text": "don't know",
                "text": "都问",
            }
        ],
    }
    unrelated = {
        "schema_version": 3,
        "overrides": [
            {
                "action": "replace_substring",
                "locator": {
                    "start": "00:00:20,000",
                    "end": "00:00:25,000",
                },
                "old_text": "don't know",
                "text": "都问",
            }
        ],
    }

    assert mixed_cjk_latin_findings_covered_by_overrides(audit, covering)
    assert not mixed_cjk_latin_findings_covered_by_overrides(audit, unrelated)


def test_title_mark_guard_closes_one_dangling_open_mark_before_punctuation():
    guarded, audit = apply_title_mark_balance_guard(
        _srt("一起《与你打灰到生命尽头。", "完整《标题》不变")
    )

    assert "一起《与你打灰到生命尽头》。" in guarded
    assert "完整《标题》不变" in guarded
    assert audit["status"] == "APPLIED"
    assert audit["repair_count"] == 1


def test_title_mark_guard_opens_one_leading_title_with_dangling_close_mark():
    guarded, audit = apply_title_mark_balance_guard(
        _srt("冒险与打灰》，这个什么与什么的", "普通句子不变")
    )

    assert "《冒险与打灰》，这个什么与什么的" in guarded
    assert audit["status"] == "APPLIED"
    assert audit["repair_count"] == 1


def test_title_mark_guard_does_not_break_a_title_spanning_two_cues():
    source = _srt("接下来唱《旅行", "的意义》给大家")

    guarded, audit = apply_title_mark_balance_guard(source)

    assert guarded == source
    assert audit["status"] == "CROSS_CUE_BALANCED"
    assert audit["repair_count"] == 0


def test_title_mark_guard_removes_one_surplus_close_but_keeps_legal_nesting():
    guarded, audit = apply_title_mark_balance_guard(
        _srt(
            "《躲在屏幕后面抽烟的二人》》这个名字需要这么长吗",
            "《A《B》》是合法嵌套",
        )
    )

    assert "《躲在屏幕后面抽烟的二人》这个名字" in guarded
    assert "《A《B》》是合法嵌套" in guarded
    assert audit["status"] == "APPLIED"
    assert audit["repairs"][0]["reason"] == (
        "ONE_DUPLICATED_CHINESE_TITLE_CLOSE_MARK"
    )


def test_impossible_punctuation_guard_collapses_comma_before_terminal_mark():
    guarded, audit = apply_impossible_punctuation_guard(
        _srt("豆沙绯闻女友ID已被注册，。", "真的吗？！", "正常，停顿")
    )

    assert "豆沙绯闻女友ID已被注册。" in guarded
    assert "真的吗？！" in guarded
    assert "正常，停顿" in guarded
    assert audit["status"] == "APPLIED"
    assert audit["repair_count"] == 1


def test_language_guard_allows_glossary_witnessed_transliteration_fix():
    """2026-07-19 看花篮案：ASR 把 ありがとう 音译成「日嘎多」，修复层按
    词表 canon 换回日语原词并调了尾标点——白名单改写 + 标点归一比对下
    不再被判无见证外语引入。"""
    from src.autoslice.subtitle_fidelity import apply_source_language_preservation_guard

    draft = "1\n00:00:06,660 --> 00:00:09,720\n谢谢你，日嘎多，谢谢哦\n"
    final = "1\n00:00:06,660 --> 00:00:09,720\n谢谢你，ありがとう，谢谢哦！\n"
    output, audit = apply_source_language_preservation_guard(
        draft, final, sanctioned=[("日嘎多", "ありがとう")]
    )
    assert audit["status"] == "CLEAN"
    assert audit["unproven_foreign_introductions"] == []
    assert "ありがとう" in output


def test_language_guard_still_blocks_unwitnessed_foreign_introduction():
    """同形反例：音译字被换成外语但改写对不在白名单（无词表见证）→ 仍 BLOCK。"""
    from src.autoslice.subtitle_fidelity import apply_source_language_preservation_guard

    draft = "1\n00:00:06,660 --> 00:00:09,720\n谢谢你，日嘎多，谢谢哦\n"
    final = "1\n00:00:06,660 --> 00:00:09,720\n谢谢你，ありがとう，谢谢哦\n"
    _, audit = apply_source_language_preservation_guard(draft, final, sanctioned=[])
    assert audit["status"].startswith("BLOCKED_")
    assert audit["unproven_foreign_introductions"]


class TestPhoneticTransliterationWitness:
    """假名引入的拼音见证（2026-07-20 领个多→ありがとう 七星 r3 拦截案）。"""

    @staticmethod
    def _srt(text: str) -> str:
        return f"1\n00:00:10,000 --> 00:00:12,000\n{text}\n"

    def test_new_variant_passes_with_phonetic_witness(self) -> None:
        from src.autoslice.subtitle_fidelity import (
            apply_source_language_preservation_guard,
        )

        # 未注册变体测见证本体（领个多已注册,走 sanctioned 白名单路）。
        out, audit = apply_source_language_preservation_guard(
            self._srt("李根多收到了"),
            self._srt("ありがとう，收到了"),
        )
        assert audit["status"] != "BLOCKED_UNPROVEN_FOREIGN_SPEAKER"
        assert not audit["unproven_foreign_introductions"]
        rows = audit.get("witnessed_foreign_introductions") or []
        assert rows and rows[0]["witness"]["target"] == "ありがとう"
        assert rows[0]["witness"]["phonetic_score"] >= 0.55

    def test_phonetically_incompatible_introduction_still_blocked(self) -> None:
        from src.autoslice.subtitle_fidelity import (
            apply_source_language_preservation_guard,
        )

        out, audit = apply_source_language_preservation_guard(
            self._srt("今天天气收到了"),
            self._srt("ありがとう，收到了"),
        )
        assert audit["unproven_foreign_introductions"]

    def test_repeated_insert_witnessed(self) -> None:
        """连说形态（灵感多案）：ありがとう×2 仍可被单次 draft 乱码见证。"""

        from src.autoslice.subtitle_fidelity import (
            apply_source_language_preservation_guard,
        )

        # 用未注册变体测见证机制本体（已注册面走 sanctioned 白名单路，
        # 轮不到见证——灵感多注册后正是如此）。
        out, audit = apply_source_language_preservation_guard(
            self._srt("谢谢你呀！凌敢多"),
            self._srt("谢谢你呀，ありがとう！ありがとう"),
        )
        assert audit["status"] != "BLOCKED_UNPROVEN_FOREIGN_SPEAKER"
        rows = audit.get("witnessed_foreign_introductions") or []
        assert rows and rows[0]["witness"]["repeat"] == 2

    def test_trailing_punct_does_not_break_witness(self) -> None:
        """r7 案：尾部句号不许掐断公共后缀对齐（去标点形态上比对）。"""

        from src.autoslice.subtitle_fidelity import (
            _phonetic_transliteration_witness,
        )

        w = _phonetic_transliteration_witness("凌敢多收到了", "ありがとう，收到了。")
        assert w is not None and w["target"] == "ありがとう"
