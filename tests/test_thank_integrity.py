"""答谢完整性三件套（2026-07-19 审片第二轮实案回归）。

- 断点吸附标点：SC 对齐重排不许把「大叫」拆进两条 cue（0:33 案）
- 谢谢还原：SC 字幕卡行丢答谢动词按 draft 见证还原（1:45/1:58 案）
- 未答谢披露：打码礼物/SC 发送者无答谢锚点时列候选（0:23 案）
"""

from __future__ import annotations

from src.autoslice.chat_evidence import ChatEvidence
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
                "applied": [{"cue_indexes": [1]}],
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
