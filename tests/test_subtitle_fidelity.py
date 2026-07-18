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
    unproven_foreign_introductions_covered_by_overrides,
)


def _srt(*texts: str) -> str:
    blocks = []
    for index, text in enumerate(texts, start=1):
        blocks.append(
            f"{index}\n00:00:{index * 5:02d},000 --> 00:00:{index * 5 + 4:02d},000\n{text}"
        )
    return "\n\n".join(blocks) + "\n"


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


def test_hallucination_drop_and_small_particle_trim_allowed():
    draft = _srt("虫儿飞虫儿飞", "就是呢那个她们要去彩排了")
    final = _srt("", "就是那个她们要去彩排了")

    guarded, audit = apply_subtitle_fidelity_guard(draft, final, agy_srt=None, sanctioned=())

    assert audit["hallucination_drops"][0]["cue_index"] == 1
    assert "虫儿飞" not in guarded.split("\n\n")[0].split("\n")[-1]
    assert audit["status"] == "CLEAN"


def test_pinyin_homophone_respell_passes_without_witness():
    """2026-07-14 五年之约冤杀案回归：季下→记下(jì同音)、小丽→小李(lǐ同音)
    是声学保真的合法重拼，AGY 缺席也必须放行；转述(一米九)与非同音实体换写
    (留下→小李)仍回退。"""
    draft = _srt("欢迎季下", "小丽只是恰好处在一个", "就是她被那个190粉毛抢了手机")
    final = _srt("欢迎记下", "小李只是恰好处在一个", "就是她被那个一米九粉毛抢了手机")

    guarded, audit = apply_subtitle_fidelity_guard(draft, final, agy_srt=None, sanctioned=())

    assert "欢迎记下" in guarded
    assert "小李只是恰好处在一个" in guarded
    assert "一米九" not in guarded  # 转述仍被回退
    assert audit["reverted_count"] == 1


def test_cue_count_mismatch_skips_guard():
    draft = _srt("一句")
    final = _srt("一句", "多出来的")

    guarded, audit = apply_subtitle_fidelity_guard(draft, final, agy_srt=None, sanctioned=())

    assert audit["status"] == "SKIPPED_CUE_COUNT_MISMATCH"
    assert guarded == final


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
        )
    )

    assert audit["status"] == "CLEAN"


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
    assert audit["status"] == "UNRESOLVED_COMPLEX_IMBALANCE"
    assert audit["repair_count"] == 0


def test_impossible_punctuation_guard_collapses_comma_before_terminal_mark():
    guarded, audit = apply_impossible_punctuation_guard(
        _srt("豆沙绯闻女友ID已被注册，。", "真的吗？！", "正常，停顿")
    )

    assert "豆沙绯闻女友ID已被注册。" in guarded
    assert "真的吗？！" in guarded
    assert "正常，停顿" in guarded
    assert audit["status"] == "APPLIED"
    assert audit["repair_count"] == 1
