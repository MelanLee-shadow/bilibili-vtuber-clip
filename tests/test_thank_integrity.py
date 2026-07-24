"""答谢完整性三件套（2026-07-19 审片第二轮实案回归）。

- 断点吸附标点：SC 对齐重排不许把「大叫」拆进两条 cue（0:33 案）
- 谢谢还原：SC 字幕卡行丢答谢动词按 draft 见证还原（1:45/1:58 案）
- 未答谢披露：打码礼物/SC 发送者无答谢锚点时列候选（0:23 案）
"""

from __future__ import annotations

from src.autoslice.chat_evidence import (
    ChatEvidence,
    normalize_srt_owner_payload_window,
)
from src.autoslice.chat_repair import _repair_sc_sender
from src.autoslice.cue_split_hygiene import _shift_boundary_punct, _snap_split_to_punct
from src.autoslice.thank_integrity import (
    restore_thank_prefixes,
    unthanked_donor_disclosure,
)


def _srt(*rows: tuple[int, int, str]) -> str:
    blocks = []
    for index, (start, end, text) in enumerate(rows, start=1):
        def fmt(ms: int) -> str:
            h, rem = divmod(ms, 3_600_000)
            m, rem = divmod(rem, 60_000)
            s, milli = divmod(rem, 1_000)
            return f"{h:02d}:{m:02d}:{s:02d},{milli:03d}"

        blocks.append(f"{index}\n{fmt(start)} --> {fmt(end)}\n{text}")
    return "\n\n".join(blocks) + "\n"


class TestSnapSplitToPunct:
    def test_dajiao_not_split_across_cues(self) -> None:
        """0:33 实案：「…反应不是很大，大|叫“李姐是侄女”」→ 大 归下一段。"""

        parts = ["主人、老公主人，反应不是很大，大", "叫“李姐是侄女”，好多人笑出声"]
        snapped = _snap_split_to_punct(parts)
        assert snapped == [
            "主人、老公主人，反应不是很大，",
            "大叫“李姐是侄女”，好多人笑出声",
        ]

    def test_boundary_on_punct_untouched(self) -> None:
        parts = ["线下叫妈妈宝宝姐姐，", "老公主人，反应不是很大"]
        assert _snap_split_to_punct(parts) == parts

    def test_no_nearby_punct_untouched(self) -> None:
        """±2 字内没有标点时不动——宁缺毋滥，别为吸附而挪长尾。"""

        parts = ["这一段完全没有标点结尾", "下一段继续"]
        assert _snap_split_to_punct(parts) == parts

    def test_shift_boundary_punct_composes_snap(self) -> None:
        parts = ["反应不是很大，大", "，叫“李姐是侄女”"]
        out = _shift_boundary_punct(parts)
        assert out == ["反应不是很大，", "大，叫“李姐是侄女”"] or out == [
            "反应不是很大，大，",
            "叫“李姐是侄女”",
        ]


class TestRestoreThankPrefixes:
    def test_sc_caption_regains_thanks_with_draft_witness(self) -> None:
        """1:58 实案：字幕卡行 + draft 听到谢谢 → 前缀还原。"""

        final = _srt((109_340, 112_230, "快乐猫猫和忧郁小狗的SC：李姐"))
        draft = _srt((109_300, 112_200, "谢谢快乐猫猫和忧郁小狗的苏恰李姐"))
        out, audit = restore_thank_prefixes(final, draft)
        assert "谢谢快乐猫猫和忧郁小狗的SC：李姐" in out
        assert audit["status"] == "APPLIED"
        assert audit["restorations"][0]["cue_index"] == 1

    def test_no_draft_witness_keeps_text(self) -> None:
        """draft 没听到谢-头就不动（fail-closed）。"""

        final = _srt((0, 2_000, "快乐猫猫和忧郁小狗的SC：李姐"))
        draft = _srt((0, 2_000, "快乐猫猫和忧郁小狗的苏恰李姐"))
        out, audit = restore_thank_prefixes(final, draft)
        assert out == final
        assert audit["status"] == "NO_CHANGE"

    def test_already_thanked_untouched(self) -> None:
        final = _srt((0, 2_000, "谢谢十麻乃的SC，得了一种病"))
        draft = _srt((0, 2_000, "谢谢十麻乃苏恰得了一种病"))
        out, audit = restore_thank_prefixes(final, draft)
        assert out == final
        assert audit["status"] == "NO_CHANGE"

    def test_plain_sentence_not_captioned(self) -> None:
        """非 SC 字幕卡行（普通句子）绝不加谢谢。"""

        final = _srt((0, 2_000, "今天天气很好"))
        draft = _srt((0, 2_000, "谢谢今天天气很好"))
        out, audit = restore_thank_prefixes(final, draft)
        assert out == final
        assert audit["status"] == "NO_CHANGE"


class TestUnthankedDonorDisclosure:
    def test_masked_gift_sender_disclosed(self) -> None:
        """0:23 实案：打码礼物发送者（快***）无答谢锚点 → 披露候选。"""

        final = _srt((0, 2_000, "你没错，怎么办"), (2_000, 4_000, "谢谢你呀"))
        evidence = [
            ChatEvidence(kind="gift", offset_ms=1_000, text="粉丝团灯牌", sender="快***"),
        ]
        audit = unthanked_donor_disclosure(final, evidence)
        assert audit["status"] == "DISCLOSED"
        assert audit["unthanked"][0]["sender"] == "快***"
        assert audit["unthanked"][0]["text"] == "粉丝团灯牌"

    def test_thanked_sc_sender_not_disclosed(self) -> None:
        final = _srt((0, 2_000, "谢谢十麻乃的SC，得了一种病"))
        evidence = [
            ChatEvidence(
                kind="superchat", offset_ms=500, text="得了一种病", sender="十麻乃orient"
            ),
        ]
        audit = unthanked_donor_disclosure(final, evidence)
        assert audit["status"] == "ALL_THANKED"

    def test_danmaku_never_expected_thanks(self) -> None:
        final = _srt((0, 2_000, "大家晚上好"))
        evidence = [
            ChatEvidence(kind="danmaku", offset_ms=100, text="晚上好", sender="路人甲"),
        ]
        audit = unthanked_donor_disclosure(final, evidence)
        assert audit["status"] == "ALL_THANKED"


def test_thank_name_matches_gangbeng_suffix() -> None:
    """「谢谢X的钢镚」进入发送者修复语法（钢镚=2元SC，2026-07-19）。"""

    repaired = _repair_sc_sender("谢谢唐林韵的钢镚", "唐琳韵")
    assert repaired == "谢谢唐琳韵的钢镚"


class TestAddressEnumerationRepair:
    def test_ma_slot_completed_inside_formula(self) -> None:
        """kmx 2:29 案形态：、包夹的 吗 补全为 妈妈。"""

        from src.autoslice.surface_canon import repair_address_enumerations

        srt = _srt((0, 2_000, "还是最可爱的姐姐、吗、主人、宝宝"))
        out, audit = repair_address_enumerations(srt)
        assert "姐姐、妈妈、主人、宝宝" in out
        assert audit["status"] == "APPLIED"

    def test_sentence_final_question_untouched(self) -> None:
        """真疑问句「…宝宝吗」不在射程（无顿号包夹）。"""

        from src.autoslice.surface_canon import repair_address_enumerations

        srt = _srt((0, 2_000, "和最可爱的宝宝吗"), (2_000, 4_000, "最可爱的四个吗"))
        out, audit = repair_address_enumerations(srt)
        assert audit["status"] == "NO_CHANGE"

    def test_non_formula_enumeration_untouched(self) -> None:
        """成员词不足两个的顿号列举（这个、这个）不触发。"""

        from src.autoslice.surface_canon import repair_address_enumerations

        srt = _srt((0, 2_000, "这个、这个八个字的称呼"))
        out, audit = repair_address_enumerations(srt)
        assert audit["status"] == "NO_CHANGE"


class TestSourceTruthSupersedesDecisionSurfaces:
    """钉子辖区豁免（2026-07-20 kmx r2 案）：ledger 钉子最后落刀且是最高
    权威——被钉子 cue 覆盖的早期决策面不再要求存活于终稿。"""

    @staticmethod
    def _verify(audit):
        from src.autoslice.producer_text_finalization import (
            verify_chat_authority_final_surfaces,
        )

        final = _srt((9_750, 12_000, "谢谢十麻乃的SC，得了一种听到“是侄女”就想笑的病"))
        return verify_chat_authority_final_surfaces(
            audit,
            final_text_srt=final,
            final_speaker_srt=final,
            delivery_start_ms=0,
            delivery_end_ms=20_000,
        )

    def test_pinned_cue_exempts_conflicting_sender_repair(self) -> None:
        audit = {
            "sender_repairs": [
                {"matched_start_ms": 9_750, "matched_end_ms": 12_000,
                 "after": "十麻乃SC得了一种听到\"是侄女\"就想笑的病。"}
            ],
            "source_subtitle_truth_audit": {
                "applied": [
                    {
                        "truth_id": "reviewed-final-sentence",
                        "action": "replace_cue",
                        "cue_indexes": [1],
                        "local_windows": [
                            {"start_ms": 9_750, "end_ms": 12_000}
                        ],
                        "declared_output_contract": {
                            "action": "replace_cue",
                            "canonical_texts": [
                                "谢谢十麻乃的SC，得了一种听到“是侄女”就想笑的病"
                            ],
                            "required_text": "",
                        },
                        "resolved_target_projection": {
                            "schema_version": (
                                "source-truth-resolved-target-projection.v1"
                            ),
                            "selector": (
                                "half-open-overlap-gte-min-then-action-resolution"
                            ),
                            "min_overlap_ms": 80,
                            "action": "replace_cue",
                            "status": "RESOLVED",
                            "cues": [
                                {
                                    "cue_index": 1,
                                    "start_ms": 9_750,
                                    "end_ms": 12_000,
                                    "before_text": (
                                        "十麻乃SC得了一种听到"
                                        '"是侄女"就想笑的病。'
                                    ),
                                    "after_text": (
                                        "谢谢十麻乃的SC，得了一种听到"
                                        "“是侄女”就想笑的病"
                                    ),
                                }
                            ],
                        },
                    }
                ],
            },
        }
        assert self._verify(audit) is True
        row = audit["sender_repairs"][0]
        assert row["final_verification_scope"] == "SUPERSEDED_BY_SOURCE_TRUTH"
        assert audit["final_superseded_by_source_truth_count"] == 1

    def test_without_pin_conflict_still_fails(self) -> None:
        audit = {
            "sender_repairs": [
                {"matched_start_ms": 9_750, "matched_end_ms": 12_000,
                 "after": "十麻乃SC得了一种听到\"是侄女\"就想笑的病。"}
            ],
        }
        assert self._verify(audit) is False

    def test_local_windows_survive_layout_resegmentation(self) -> None:
        """钉子按 post-apply 投影跨 layout 拆句，仍能绑定最终时间区间。"""

        from src.autoslice.producer_text_finalization import (
            verify_chat_authority_final_surfaces,
        )

        # 终稿被 layout 拆成两条；投影逐 cue 绑定时间与 before/after。
        final = _srt(
            (96_950, 98_000, "谢谢十麻乃的SC，"),
            (98_000, 100_970, "得了一种听到“是侄女”就想笑的病"),
        )
        # 决策行 matched_* 与钉子 local_windows 同锚 padded 轴——
        # delivery_start 非零时同轴直比仍必须命中（kmx r4 案）。
        audit = {
            "sender_repairs": [
                {"matched_start_ms": 106_200, "matched_end_ms": 110_700,
                 "after": "十麻乃SC得了一种病。"}
            ],
            "source_subtitle_truth_audit": {
                "applied": [{
                    "truth_id": "layout-independent-reviewed-final",
                    "action": "replace_cue",
                    "cue_indexes": [1, 2],
                    "local_windows": [{"start_ms": 106_700, "end_ms": 110_720}],
                    "declared_output_contract": {
                        "action": "replace_cue",
                        "canonical_texts": [
                            "谢谢十麻乃的SC，得了一种听到“是侄女”就想笑的病"
                        ],
                        "required_text": "",
                    },
                    "resolved_target_projection": {
                        "schema_version": (
                            "source-truth-resolved-target-projection.v1"
                        ),
                        "selector": (
                            "half-open-overlap-gte-min-then-action-resolution"
                        ),
                        "min_overlap_ms": 80,
                        "action": "replace_cue",
                        "status": "RESOLVED",
                        "cues": [
                            {
                                "cue_index": 1,
                                "start_ms": 106_700,
                                "end_ms": 107_750,
                                "before_text": "十麻乃SC",
                                "after_text": "谢谢十麻乃的SC，",
                            },
                            {
                                "cue_index": 2,
                                "start_ms": 107_750,
                                "end_ms": 110_720,
                                "before_text": "得了一种病。",
                                "after_text": (
                                    "得了一种听到“是侄女”就想笑的病"
                                ),
                            },
                        ],
                    },
                }],
            },
        }
        ok = verify_chat_authority_final_surfaces(
            audit, final_text_srt=final, final_speaker_srt=final,
            delivery_start_ms=9_750, delivery_end_ms=170_000,
        )
        assert ok is True
        assert audit["sender_repairs"][0]["final_verification_scope"] == (
            "SUPERSEDED_BY_SOURCE_TRUTH"
        )


def test_redelivery_baseline_supersedes_stochastic_decision_outside_truth() -> None:
    from src.autoslice.producer_text_finalization import (
        verify_chat_authority_final_surfaces,
    )

    final = _srt((0, 3_000, "上一版已审定口播"))
    audit = {
        "entity_repairs": [
            {
                "matched_start_ms": 11_000,
                "matched_end_ms": 12_000,
                "expected_entity": "本轮随机改写",
            }
        ],
        "redelivery_subtitle_baseline_audit": {
            "status": "APPLIED",
            "owned_intervals": [{"start_ms": 0, "end_ms": 3_000}],
            "mappings": [
                {
                    "baseline_cue_index": 1,
                    "start_ms": 0,
                    "end_ms": 3_000,
                    "after": "上一版已审定口播",
                }
            ],
        },
    }

    assert verify_chat_authority_final_surfaces(
        audit,
        final_text_srt=final,
        final_speaker_srt=final,
        delivery_start_ms=10_000,
        delivery_end_ms=13_000,
    )
    assert audit["entity_repairs"][0]["final_verification_scope"] == (
        "SUPERSEDED_BY_REDELIVERY_BASELINE"
    )
    assert audit["final_superseded_by_redelivery_baseline_count"] == 1


def test_redelivery_baseline_cannot_overwrite_story_bound_supported_repair() -> None:
    from src.autoslice.producer_text_finalization import (
        verify_chat_authority_final_surfaces,
    )

    final = _srt((0, 3_000, "这直播间这很非常包容"))
    audit = {
        "entity_repairs": [
            {
                "matched_start_ms": 11_000,
                "matched_end_ms": 12_500,
                "expected_entity": "还是非常包容",
                "boundary_required": True,
                "boundary_owner_id": "supported-acoustic-repair",
            }
        ],
        "redelivery_subtitle_baseline_audit": {
            "status": "APPLIED",
            "mappings": [
                {
                    "baseline_cue_index": 1,
                    "start_ms": 0,
                    "end_ms": 3_000,
                    "after": "这直播间这很非常包容",
                }
            ],
        },
    }

    assert not verify_chat_authority_final_surfaces(
        audit,
        final_text_srt=final,
        final_speaker_srt=final,
        delivery_start_ms=10_000,
        delivery_end_ms=13_000,
    )
    repair = audit["entity_repairs"][0]
    assert repair["final_verification_scope"] == "DELIVERY"
    assert repair["survived_final_text_srt"] is False
    assert audit["final_superseded_by_redelivery_baseline_count"] == 0


def test_story_bound_repair_cannot_escape_as_outside_delivery() -> None:
    from src.autoslice.producer_text_finalization import (
        verify_chat_authority_final_surfaces,
    )

    final = _srt((0, 2_000, "现有成片"))
    audit = {
        "entity_repairs": [
            {
                "matched_start_ms": 20_000,
                "matched_end_ms": 22_000,
                "expected_entity": "邪恶守宫",
                "boundary_required": True,
                "boundary_owner_id": "late-story-repair",
            }
        ]
    }

    assert not verify_chat_authority_final_surfaces(
        audit,
        final_text_srt=final,
        final_speaker_srt=final,
        delivery_start_ms=0,
        delivery_end_ms=10_000,
    )
    assert audit["entity_repairs"][0]["final_verification_scope"] == (
        "BOUNDARY_REQUIRED_OWNER_EXCLUDED"
    )
    assert audit["final_boundary_required_exclusion_count"] == 1


def test_source_truth_owner_mismatch_cannot_pass_with_zero_required_rows() -> None:
    from src.autoslice.producer_text_finalization import (
        verify_chat_authority_final_surfaces,
    )

    final = _srt((0, 2_000, "好像是灰神吧"))
    audit = {
        "source_subtitle_truth_audit": {
            "satisfied": [
                {
                    "truth_id": "huishen-pronunciation",
                    "action": "replace_substring",
                    "local_windows": [{"start_ms": 0, "end_ms": 2_000}],
                    "declared_output_contract": {
                        "action": "replace_substring",
                        "canonical_texts": [],
                        "required_text": "毁神",
                    },
                }
            ]
        }
    }

    assert not verify_chat_authority_final_surfaces(
        audit,
        final_text_srt=final,
        final_speaker_srt=final,
        delivery_start_ms=0,
        delivery_end_ms=2_000,
    )
    assert audit["final_source_truth_owner_verification"]["status"] == "FAIL"
    assert audit["final_verification_failure"] == (
        "SOURCE_TRUTH_FINAL_OWNER_NOT_VERIFIED"
    )


def test_exact_owner_window_excludes_touching_neighbours() -> None:
    final = _srt(
        (0, 1_000, "前句"),
        (1_000, 2_000, "真值"),
        (2_000, 3_000, "后句"),
    )

    assert normalize_srt_owner_payload_window(
        final,
        start_ms=1_000,
        end_ms=2_000,
        min_overlap_ms=80,
    ) == "真值"


def test_replace_cue_owner_accepts_exact_cue_between_touching_neighbours() -> None:
    from src.autoslice.producer_text_finalization import (
        verify_chat_authority_final_surfaces,
    )

    final = _srt(
        (0, 1_000, "前句"),
        (1_000, 2_000, "姐感妹"),
        (2_000, 3_000, "秦秦"),
    )
    audit = {
        "source_subtitle_truth_audit": {
            "applied": [
                {
                    "truth_id": "jiegammei",
                    "action": "replace_cue",
                    "local_windows": [{"start_ms": 1_000, "end_ms": 2_000}],
                    "declared_output_contract": {
                        "action": "replace_cue",
                        "canonical_texts": ["姐感妹"],
                        "required_text": "",
                    },
                }
            ]
        }
    }

    assert verify_chat_authority_final_surfaces(
        audit,
        final_text_srt=final,
        final_speaker_srt=final,
        delivery_start_ms=0,
        delivery_end_ms=3_000,
    )


def test_replace_cue_owner_preserves_exact_text_split_across_owned_cues() -> None:
    from src.autoslice.producer_text_finalization import (
        verify_chat_authority_final_surfaces,
    )

    final = _srt(
        (0, 1_000, "前句"),
        (1_000, 1_500, "姐感"),
        (1_500, 2_000, "妹"),
        (2_000, 3_000, "后句"),
    )
    audit = {
        "source_subtitle_truth_audit": {
            "applied": [
                {
                    "truth_id": "split-jiegammei",
                    "action": "replace_cue",
                    "local_windows": [{"start_ms": 1_000, "end_ms": 2_000}],
                    "declared_output_contract": {
                        "action": "replace_cue",
                        "canonical_texts": ["姐感", "妹"],
                        "required_text": "",
                    },
                }
            ]
        }
    }

    assert verify_chat_authority_final_surfaces(
        audit,
        final_text_srt=final,
        final_speaker_srt=final,
        delivery_start_ms=0,
        delivery_end_ms=3_000,
    )


def test_drop_cue_owner_ignores_speech_touching_silent_window() -> None:
    from src.autoslice.producer_text_finalization import (
        verify_chat_authority_final_surfaces,
    )

    final = _srt((0, 1_000, "前句"), (2_000, 3_000, "后句"))
    audit = {
        "source_subtitle_truth_audit": {
            "applied": [
                {
                    "truth_id": "silent-gap",
                    "action": "drop_cue",
                    "local_windows": [{"start_ms": 1_000, "end_ms": 2_000}],
                    "declared_output_contract": {
                        "action": "drop_cue",
                        "canonical_texts": [],
                        "required_text": "",
                    },
                }
            ]
        }
    }

    assert verify_chat_authority_final_surfaces(
        audit,
        final_text_srt=final,
        final_speaker_srt=final,
        delivery_start_ms=0,
        delivery_end_ms=3_000,
    )


def test_drop_cue_owner_uses_same_eighty_ms_overlap_threshold_as_truth_apply() -> None:
    from src.autoslice.producer_text_finalization import (
        verify_chat_authority_final_surfaces,
    )

    def verifies(overlap_ms: int) -> bool:
        final = _srt((2_000 - overlap_ms, 2_500, "幻听"))
        audit = {
            "source_subtitle_truth_audit": {
                "applied": [
                    {
                        "truth_id": f"silent-gap-{overlap_ms}",
                        "action": "drop_cue",
                        "local_windows": [
                            {"start_ms": 1_000, "end_ms": 2_000}
                        ],
                        "declared_output_contract": {
                            "action": "drop_cue",
                            "canonical_texts": [],
                            "required_text": "",
                        },
                    }
                ]
            }
        }
        return verify_chat_authority_final_surfaces(
            audit,
            final_text_srt=final,
            final_speaker_srt=final,
            delivery_start_ms=0,
            delivery_end_ms=3_000,
        )

    assert verifies(79)
    assert not verifies(80)


def test_replace_cue_owner_still_rejects_merged_extra_speech() -> None:
    from src.autoslice.producer_text_finalization import (
        verify_chat_authority_final_surfaces,
    )

    final = _srt((500, 2_500, "前句姐感妹后句"))
    audit = {
        "source_subtitle_truth_audit": {
            "applied": [
                {
                    "truth_id": "merged-extra-speech",
                    "action": "replace_cue",
                    "local_windows": [{"start_ms": 1_000, "end_ms": 2_000}],
                    "declared_output_contract": {
                        "action": "replace_cue",
                        "canonical_texts": ["姐感妹"],
                        "required_text": "",
                    },
                }
            ]
        }
    }

    assert not verify_chat_authority_final_surfaces(
        audit,
        final_text_srt=final,
        final_speaker_srt=final,
        delivery_start_ms=0,
        delivery_end_ms=3_000,
    )
    failure = audit["final_source_truth_owner_verification"]["failures"][0]
    assert failure["text_payload"] == "前句姐感妹后句"


def test_replace_cue_owner_does_not_accept_canonical_only_in_nearby_cue() -> None:
    from src.autoslice.producer_text_finalization import (
        verify_chat_authority_final_surfaces,
    )

    final = _srt(
        (1_000, 2_000, "错词"),
        (2_050, 2_500, "姐感妹"),
    )
    audit = {
        "source_subtitle_truth_audit": {
            "applied": [
                {
                    "truth_id": "canonical-outside-owner",
                    "action": "replace_cue",
                    "local_windows": [{"start_ms": 1_000, "end_ms": 2_000}],
                    "declared_output_contract": {
                        "action": "replace_cue",
                        "canonical_texts": ["姐感妹"],
                        "required_text": "",
                    },
                }
            ]
        }
    }

    assert not verify_chat_authority_final_surfaces(
        audit,
        final_text_srt=final,
        final_speaker_srt=final,
        delivery_start_ms=0,
        delivery_end_ms=3_000,
    )
    failure = audit["final_source_truth_owner_verification"]["failures"][0]
    assert failure["text_payload"] == "错词"


def test_replace_cue_owner_checks_speaker_subtitle_exactly_too() -> None:
    from src.autoslice.producer_text_finalization import (
        verify_chat_authority_final_surfaces,
    )

    text_srt = _srt((1_000, 2_000, "姐感妹"))
    speaker_srt = _srt((1_000, 2_000, "[李豆沙] 姐感妹秦秦"))
    audit = {
        "source_subtitle_truth_audit": {
            "applied": [
                {
                    "truth_id": "speaker-extra-speech",
                    "action": "replace_cue",
                    "local_windows": [{"start_ms": 1_000, "end_ms": 2_000}],
                    "declared_output_contract": {
                        "action": "replace_cue",
                        "canonical_texts": ["姐感妹"],
                        "required_text": "",
                    },
                }
            ]
        }
    }

    assert not verify_chat_authority_final_surfaces(
        audit,
        final_text_srt=text_srt,
        final_speaker_srt=speaker_srt,
        delivery_start_ms=0,
        delivery_end_ms=3_000,
    )
    failure = audit["final_source_truth_owner_verification"]["failures"][0]
    assert failure["text_ok"] is True
    assert failure["speaker_ok"] is False


def test_redelivery_baseline_owner_uses_strict_overlap_for_adjacent_cues() -> None:
    from src.autoslice.producer_text_finalization import (
        verify_chat_authority_final_surfaces,
    )

    final = _srt(
        (0, 1_000, "前句"),
        (1_000, 2_000, "旧版已审定"),
        (2_000, 3_000, "后句"),
    )
    audit = {
        "redelivery_subtitle_baseline_audit": {
            "status": "APPLIED",
            "mappings": [
                {
                    "baseline_cue_index": 2,
                    "start_ms": 1_000,
                    "end_ms": 2_000,
                    "text": "旧版已审定",
                }
            ],
        }
    }

    assert verify_chat_authority_final_surfaces(
        audit,
        final_text_srt=final,
        final_speaker_srt=final,
        delivery_start_ms=0,
        delivery_end_ms=3_000,
    )


def test_redelivery_baseline_owner_rejects_extra_speech_inside_owner_cue() -> None:
    from src.autoslice.producer_text_finalization import (
        verify_chat_authority_final_surfaces,
    )

    final = _srt((1_000, 2_000, "旧版已审定但多了内容"))
    audit = {
        "redelivery_subtitle_baseline_audit": {
            "status": "APPLIED",
            "mappings": [
                {
                    "baseline_cue_index": 1,
                    "start_ms": 1_000,
                    "end_ms": 2_000,
                    "text": "旧版已审定",
                }
            ],
        }
    }

    assert not verify_chat_authority_final_surfaces(
        audit,
        final_text_srt=final,
        final_speaker_srt=final,
        delivery_start_ms=0,
        delivery_end_ms=3_000,
    )
    failure = audit["final_redelivery_baseline_owner_verification"][
        "failures"
    ][0]
    assert failure["reason_code"] == (
        "REDELIVERY_BASELINE_FINAL_OWNER_MISMATCH"
    )


def test_redelivery_baseline_replays_only_typed_substring_truth_change() -> None:
    from src.autoslice.producer_text_finalization import (
        verify_chat_authority_final_surfaces,
    )

    final = _srt((0, 3_000, "A正词B"))
    audit = {
        "source_subtitle_truth_audit": {
            "applied": [
                {
                    "truth_id": "typed-substring",
                    "required": True,
                    "action": "replace_substring",
                    "cue_indexes": [1],
                    "local_windows": [{"start_ms": 0, "end_ms": 3_000}],
                    "declared_output_contract": {
                        "action": "replace_substring",
                        "canonical_texts": [],
                        "required_text": "正词",
                    },
                    "replacements": [
                        {
                            "cue_index": 1,
                            "surface": "误词",
                            "canonical": "正词",
                        }
                    ],
                    "resolved_target_projection": {
                        "schema_version": (
                            "source-truth-resolved-target-projection.v1"
                        ),
                        "selector": (
                            "half-open-overlap-gte-min-then-action-resolution"
                        ),
                        "min_overlap_ms": 80,
                        "action": "replace_substring",
                        "status": "RESOLVED",
                        "cues": [
                            {
                                "cue_index": 1,
                                "start_ms": 0,
                                "end_ms": 3_000,
                                "before_text": "A误词B",
                                "after_text": "A正词B",
                            }
                        ],
                    },
                }
            ]
        },
        "redelivery_subtitle_baseline_audit": {
            "status": "APPLIED",
            "mappings": [
                {
                    "baseline_cue_index": 1,
                    "start_ms": 0,
                    "end_ms": 3_000,
                    "text": "A误词B",
                }
            ],
        },
    }

    assert verify_chat_authority_final_surfaces(
        audit,
        final_text_srt=final,
        final_speaker_srt=final,
        delivery_start_ms=0,
        delivery_end_ms=3_000,
    )
    verification = audit[
        "final_redelivery_baseline_owner_verification"
    ]
    assert verification["required_mapping_count"] == 1
    mapping = audit["redelivery_subtitle_baseline_audit"]["mappings"][0]
    assert mapping["final_owner_scope"] == (
        "TRANSFORMED_BY_SOURCE_TRUTH_OWNER"
    )
    assert mapping["final_owner_expected"] == "a正词b"


def test_full_truth_supersession_requires_causal_before_surface() -> None:
    from src.autoslice.producer_text_finalization import (
        verify_chat_authority_final_surfaces,
    )

    def build_audit(decision_text: str) -> dict:
        return {
            "sender_repairs": [
                {
                    "matched_start_ms": 900,
                    "matched_end_ms": 1_100,
                    "after": decision_text,
                }
            ],
            "source_subtitle_truth_audit": {
                "applied": [
                    {
                        "truth_id": "causal-full-owner",
                        "required": True,
                        "action": "replace_cue",
                        "cue_indexes": [1],
                        "local_windows": [
                            {"start_ms": 0, "end_ms": 1_000}
                        ],
                        "declared_output_contract": {
                            "action": "replace_cue",
                            "canonical_texts": ["新句"],
                            "required_text": "",
                        },
                        "resolved_target_projection": {
                            "schema_version": (
                                "source-truth-resolved-target-projection.v1"
                            ),
                            "selector": (
                                "half-open-overlap-gte-min-then-action-resolution"
                            ),
                            "min_overlap_ms": 80,
                            "action": "replace_cue",
                            "status": "RESOLVED",
                            "cues": [
                                {
                                    "cue_index": 1,
                                    "start_ms": 0,
                                    "end_ms": 1_000,
                                    "before_text": "旧名",
                                    "after_text": "新句",
                                }
                            ],
                        },
                    }
                ]
            },
        }

    final = _srt((0, 1_000, "新句"))
    causal = build_audit("旧名")
    assert verify_chat_authority_final_surfaces(
        causal,
        final_text_srt=final,
        final_speaker_srt=final,
        delivery_start_ms=0,
        delivery_end_ms=2_000,
    )
    assert causal["sender_repairs"][0]["final_verification_scope"] == (
        "SUPERSEDED_BY_SOURCE_TRUTH"
    )

    unrelated = build_audit("别的名")
    assert not verify_chat_authority_final_surfaces(
        unrelated,
        final_text_srt=final,
        final_speaker_srt=final,
        delivery_start_ms=0,
        delivery_end_ms=2_000,
    )
    assert unrelated["sender_repairs"][0]["final_verification_scope"] == (
        "DELIVERY"
    )


def test_bare_baseline_interval_is_not_final_owner_evidence() -> None:
    from src.autoslice.producer_text_finalization import (
        verify_chat_authority_final_surfaces,
    )

    final = _srt((0, 2_000, "上一版文字"))
    audit = {
        "redelivery_subtitle_baseline_audit": {
            "status": "APPLIED",
            "owned_intervals": [{"start_ms": 0, "end_ms": 2_000}],
            "mappings": [],
        }
    }
    assert not verify_chat_authority_final_surfaces(
        audit,
        final_text_srt=final,
        final_speaker_srt=final,
        delivery_start_ms=0,
        delivery_end_ms=2_000,
    )
    assert audit["final_redelivery_baseline_owner_verification"]["status"] == (
        "FAIL"
    )
