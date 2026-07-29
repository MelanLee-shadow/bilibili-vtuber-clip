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


def test_baseline_replay_revert_receipt_retires_boundary_owner_row() -> None:
    """1863 SC 案：replay audit 的 before→after 记录证明修复文本曾在字幕
    里、被已验证的 Ivan 已审 baseline 有意替换——只有这种因果证据才允许
    boundary_required 行退位给 baseline（否则 repair-vs-replay 永久死锁）。"""
    from src.autoslice.producer_text_finalization import (
        verify_chat_authority_final_surfaces,
    )

    final = _srt((0, 2_000, "为什么要请大N老师吃火锅"), (2_000, 3_000, "终于和大N见面"))
    audit = {
        "applied": [
            {
                "exact_text": "什么要请大N老师吃火锅，终于和大N见面了",
                "matched_start_ms": 10_000,
                "matched_end_ms": 13_000,
                "boundary_required": True,
                "boundary_owner_id": "exact_read:1:10000:13000",
            }
        ],
        "redelivery_subtitle_baseline_audit": {
            "status": "APPLIED",
            "mappings": [
                {
                    "baseline_cue_index": 1,
                    "start_ms": 0,
                    "end_ms": 2_000,
                    "changed": True,
                    "before": "什么要请大N老师吃火锅，",
                    "after": "为什么要请大N老师吃火锅",
                },
                {
                    "baseline_cue_index": 2,
                    "start_ms": 2_000,
                    "end_ms": 3_000,
                    "changed": True,
                    "before": "终于和大N见面了",
                    "after": "终于和大N见面",
                },
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
    row = audit["applied"][0]
    assert row["final_verification_scope"] == "SUPERSEDED_BY_REDELIVERY_BASELINE"
    assert row["final_verification_scope_reason"] == (
        "BOUNDARY_OWNER_REVERTED_BY_VERIFIED_BASELINE_REPLAY"
    )
    assert row["final_redelivery_baseline_revert"]["baseline_cue_indexes"] == [1, 2]
    assert audit["final_superseded_by_redelivery_baseline_count"] == 1


def test_exact_replay_input_cues_retire_boundary_owner_once() -> None:
    """Exact replay stores overlapping input cues on every output mapping.

    A pre-replay cue may straddle multiple reviewed cues, so reconciliation
    must deduplicate it by current cue index before proving the causal revert.
    """
    from src.autoslice.producer_text_finalization import (
        verify_chat_authority_final_surfaces,
    )

    final = _srt(
        (0, 1_000, "为什么要请大N老师吃火锅"),
        (1_000, 2_000, "终于和大N见面"),
    )
    pre_replay = {
        "current_cue_index": 1,
        "start_ms": 0,
        "end_ms": 2_000,
        "text": "什么要请大N老师吃火锅，终于和大N见面了",
    }
    audit = {
        "applied": [
            {
                "exact_text": "什么要请大N老师吃火锅，终于和大N见面了",
                "matched_start_ms": 10_000,
                "matched_end_ms": 12_000,
                "boundary_required": True,
            }
        ],
        "redelivery_subtitle_baseline_audit": {
            "status": "APPLIED",
            "mappings": [
                {
                    "baseline_cue_index": 1,
                    "start_ms": 0,
                    "end_ms": 1_000,
                    "text": "为什么要请大N老师吃火锅",
                    "pre_replay_cues": [pre_replay],
                },
                {
                    "baseline_cue_index": 2,
                    "start_ms": 1_000,
                    "end_ms": 2_000,
                    "text": "终于和大N见面",
                    "pre_replay_cues": [pre_replay],
                },
            ],
        },
    }

    assert verify_chat_authority_final_surfaces(
        audit,
        final_text_srt=final,
        final_speaker_srt=final,
        delivery_start_ms=10_000,
        delivery_end_ms=12_000,
    )
    row = audit["applied"][0]
    assert row["final_verification_scope"] == (
        "SUPERSEDED_BY_REDELIVERY_BASELINE"
    )
    assert row["final_redelivery_baseline_revert"]["before_payload"] == (
        "什么要请大n老师吃火锅终于和大n见面了"
    )


def test_exact_boundary_pad_overlap_is_sliver_exempt() -> None:
    """1863 sender 案：行 matched_end 恰落在首 cue 起点上，overlap 精确等于
    250ms（片头 pad 常数）且 ratio 0.09——刀刃值必须按 sliver 豁免，
    而不是要求整句在 250ms 窗口里存活。"""
    from src.autoslice.producer_text_finalization import (
        verify_chat_authority_final_surfaces,
    )

    final = _srt((250, 2_000, "为什么要请大N老师吃火锅"))
    audit = {
        "sender_repairs": [
            {
                "matched_start_ms": 7_290,
                "matched_end_ms": 10_040,
                "after": "谢谢南町家的星耀的SC",
                "spoken_sender": "南町家的星耀",
            }
        ]
    }

    assert verify_chat_authority_final_surfaces(
        audit,
        final_text_srt=final,
        final_speaker_srt=final,
        delivery_start_ms=9_790,
        delivery_end_ms=100_640,
    )
    row = audit["sender_repairs"][0]
    assert row["final_verification_scope"] == "OUTSIDE_DELIVERY"
    assert row["final_verification_scope_reason"] == (
        "BOUNDARY_SLIVER_BELOW_MEANINGFUL_AUDIO_THRESHOLD"
    )


def test_scope_rejected_opening_straddler_edge_fragment_is_not_required() -> None:
    """1863 current cut: a padded-context correction rejected as a boundary
    owner leaves only 340/2840ms in the final opening.  That edge fragment
    cannot be required to contain the whole corrected phrase."""
    from src.autoslice.producer_text_finalization import (
        verify_chat_authority_final_surfaces,
    )

    final = _srt((0, 2_000, "为什么要请大N老师吃火锅"))
    audit = {
        "entity_repairs": [
            {
                "mode": "final_review_context_adjudication",
                "matched_start_ms": 7_290,
                "matched_end_ms": 10_130,
                "before": ["谢谢南京家星耀的素菜"],
                "after": ["谢谢南町家的星耀的素菜"],
                "structured_exact_text": "谢谢南町家的星耀的素菜",
                "boundary_required": False,
                "boundary_owner_rejection": (
                    "STRADDLES_IMMUTABLE_STORY_SCOPE"
                ),
            }
        ]
    }

    assert verify_chat_authority_final_surfaces(
        audit,
        final_text_srt=final,
        final_speaker_srt=final,
        delivery_start_ms=9_790,
        delivery_end_ms=100_640,
    )
    row = audit["entity_repairs"][0]
    assert row["final_delivery_overlap_ms"] == 340
    assert row["final_delivery_overlap_ratio"] == 0.119718
    assert row["final_verification_scope"] == "OUTSIDE_DELIVERY"
    assert row["final_verification_scope_reason"] == (
        "SCOPE_REJECTED_EDGE_FRAGMENT_OUTSIDE_OWNER"
    )


def test_scope_rejected_straddler_materially_retained_still_must_survive() -> None:
    from src.autoslice.producer_text_finalization import (
        verify_chat_authority_final_surfaces,
    )

    for matched_start_ms, matched_end_ms in (
        (7_290, 13_130),  # 3.34s is materially retained.
        (9_500, 12_000),  # 2.21/2.5s coverage is materially retained.
    ):
        final = _srt((0, 4_000, "成片没有修复文本"))
        audit = {
            "entity_repairs": [
                {
                    "mode": "final_review_context_adjudication",
                    "matched_start_ms": matched_start_ms,
                    "matched_end_ms": matched_end_ms,
                    "before": ["错词"],
                    "after": ["正确词"],
                    "structured_exact_text": "正确词",
                    "boundary_required": False,
                    "boundary_owner_rejection": (
                        "STRADDLES_IMMUTABLE_STORY_SCOPE"
                    ),
                }
            ]
        }

        assert not verify_chat_authority_final_surfaces(
            audit,
            final_text_srt=final,
            final_speaker_srt=final,
            delivery_start_ms=9_790,
            delivery_end_ms=100_640,
        )
        assert audit["entity_repairs"][0]["final_verification_scope"] == (
            "DELIVERY"
        )


def test_adjudication_reverted_by_baseline_retires_instead_of_deadlock() -> None:
    """672 看/外案：correction pass 的声学修正被 Ivan 已审 baseline 收回
    （同窗 final_owner 逐字验证通过且文本≠修正文本）时，修正行声明性退位，
    提案留在审计里，不再终验死锁。"""
    from src.autoslice.producer_text_finalization import (
        verify_chat_authority_final_surfaces,
    )

    final = _srt((0, 2_040, "就除了在场的几位，看"))
    audit = {
        "entity_repairs": [
            {
                "mode": "final_review_context_adjudication",
                "matched_start_ms": 42_140,
                "matched_end_ms": 44_180,
                "before": ["就除了在场的几位看"],
                "after": ["就除了在场的几位外"],
                "structured_exact_text": "就除了在场的几位外",
                "boundary_required": True,
                "boundary_owner_id": "final-review-adjudication",
            }
        ],
        "redelivery_subtitle_baseline_audit": {
            "status": "APPLIED",
            "mappings": [
                {
                    "baseline_cue_index": 14,
                    "start_ms": 0,
                    "end_ms": 2_040,
                    "text": "就除了在场的几位，看",
                }
            ],
        },
    }

    assert verify_chat_authority_final_surfaces(
        audit,
        final_text_srt=final,
        final_speaker_srt=final,
        delivery_start_ms=42_140,
        delivery_end_ms=44_180,
    )
    row = audit["entity_repairs"][0]
    assert row["final_verification_scope"] == "SUPERSEDED_BY_REDELIVERY_BASELINE"
    assert row["final_verification_scope_reason"] == (
        "ADJUDICATION_PINNED_BY_REVIEWED_BASELINE"
    )


def test_owner_payload_majority_gate_drops_grazing_neighbor() -> None:
    """1573 r11 实案几何：真值窗尾带落值轮网格坐标（40870），本轮句尾早移
    到 40680，窗尾多出的 190ms 以 ≥80ms 绝对门擦进下一句——majority 门
    要求邻句过半重叠，擦入出局、真 owner cue（99% 重叠）保留。"""
    from src.autoslice.chat_evidence import normalize_srt_owner_payload_window

    srt = (
        "1\n00:00:37,600 --> 00:00:40,680\n但其实背地里是被欺负的那种\n\n"
        "2\n00:00:40,680 --> 00:00:42,340\n那我不是一直都是这样的吗\n"
    )
    grazing = normalize_srt_owner_payload_window(
        srt, start_ms=37_610, end_ms=40_870, min_overlap_ms=80
    )
    assert "一直都是这样" in grazing  # absolute gate alone lets the neighbor in
    gated = normalize_srt_owner_payload_window(
        srt, start_ms=37_610, end_ms=40_870, min_overlap_ms=80, majority_ratio=0.5
    )
    assert gated == "但其实背地里是被欺负的那种"


def test_redelivery_baseline_head_rel_conversion() -> None:
    """1573 round-9 案：v2 baseline 的开场必须能换算到 recut 相对轴，
    用于把 fresh 网格的句首吸附钳在 baseline 首 cue 之前。"""
    from src.autoslice.producer_boundary_resolution import (
        _redelivery_baseline_head_rel_ms,
    )

    spec = {
        "pieces": [{"start_ms": 1_563_150, "end_ms": 1_683_000}],
        "subtitle_redelivery_baseline": {
            "schema_version": "subtitle-redelivery-baseline.v2",
            "absolute_source_start_ms": 1_572_910,
        },
    }
    assert _redelivery_baseline_head_rel_ms(spec) == 9_760
    assert _redelivery_baseline_head_rel_ms({"pieces": []}) is None
    assert (
        _redelivery_baseline_head_rel_ms(
            {
                "pieces": [{"start_ms": 0}],
                "subtitle_redelivery_baseline": {"schema_version": "other"},
            }
        )
        is None
    )


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
