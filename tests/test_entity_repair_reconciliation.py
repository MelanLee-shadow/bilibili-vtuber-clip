"""同槽矛盾裁定和解（2026-07-14 恋死/恋青/练死案）：互斥 RESOLVED → 回退+披露，绝不后写者赢。"""

from src.autoslice.chat_authority import (
    ReferentEntity,
    ReferentGroup,
    reconcile_contradictory_entity_repairs,
    registered_entity_names,
    revert_unregistered_entity_repairs,
)

SRT = """1
00:00:01,000 --> 00:00:02,000
恋死看吗？我们已经看了

2
00:00:02,500 --> 00:00:04,000
我们在看练死，我们已经看完第一集了

3
00:00:04,500 --> 00:00:05,000
无关句子
"""


def test_contradiction_reverts_to_earliest_before_and_discloses():
    repairs = [
        {
            "cue_indexes": [2],
            "before": ["我们在看恋死，我们已经看完第一集了"],
            "after": ["我们在看恋青，我们已经看完第一集了"],
            "expected_entity": "恋青",
        },
        {
            "cue_indexes": [2],
            "before": ["我们在看恋青，我们已经看完第一集了"],
            "after": ["我们在看练死，我们已经看完第一集了"],
            "expected_entity": "练死",
        },
    ]
    out, disclosures = reconcile_contradictory_entity_repairs(SRT, repairs)
    assert "我们在看恋死，我们已经看完第一集了" in out
    assert disclosures == [
        {
            "cue_index": 2,
            "expected_entities": ["恋青", "练死"],
            "reverted_to": "我们在看恋死，我们已经看完第一集了",
        }
    ]
    assert all(r["reconciliation"] == "CONTRADICTORY_VERDICTS_REVERTED" for r in repairs)


def test_disjoint_and_agreeing_repairs_untouched():
    repairs = [
        {"cue_indexes": [2], "before": ["a"], "after": ["b"], "expected_entity": "X"},
        {"cue_indexes": [3], "before": ["c"], "after": ["d"], "expected_entity": "Y"},
    ]
    out, disclosures = reconcile_contradictory_entity_repairs(SRT, repairs)
    assert out == SRT and disclosures == []
    assert "reconciliation" not in repairs[0]

    agreeing = [
        {"cue_indexes": [2], "before": ["a"], "after": ["b"], "expected_entity": "X"},
        {"cue_indexes": [2], "before": ["b"], "after": ["b2"], "expected_entity": "X"},
    ]
    out2, disclosures2 = reconcile_contradictory_entity_repairs(SRT, agreeing)
    assert out2 == SRT and disclosures2 == []


def test_unexpected_current_text_discloses_without_rewrite():
    repairs = [
        {"cue_indexes": [3], "before": ["原文"], "after": ["改A"], "expected_entity": "A"},
        {"cue_indexes": [3], "before": ["改A"], "after": ["改B"], "expected_entity": "B"},
    ]
    out, disclosures = reconcile_contradictory_entity_repairs(SRT, repairs)
    assert out == SRT
    assert disclosures[0]["reverted_to"] is None
    assert repairs[0]["reconciliation"] == "CONTRADICTORY_VERDICTS_REVERTED"


REGISTERED = registered_entity_names(
    [
        ReferentGroup(
            (
                ReferentEntity("恋青", ("恋青",)),
                ReferentEntity("恋死", ("恋死",)),
            ),
            reason="test",
        )
    ]
)

UNREG_SRT = """1
00:00:01,000 --> 00:00:02,000
等小主什么时候来看恋青呢

2
00:00:02,500 --> 00:00:04,000
到时我自己有看
"""


def test_unregistered_entity_repair_is_reverted():
    repairs = [
        {
            "cue_indexes": [2],
            "before": ["恋青我自己有看"],
            "after": ["到时我自己有看"],
            "expected_entity": "到时",
        }
    ]
    out, disclosures = revert_unregistered_entity_repairs(UNREG_SRT, repairs, REGISTERED)
    assert "恋青我自己有看" in out
    assert "到时我自己有看" not in out
    assert repairs[0]["reconciliation"] == "UNREGISTERED_ENTITY_REVERTED"
    assert disclosures == [
        {"cue_indexes": [2], "expected_entity": "到时", "reverted_cues": [2]}
    ]
    # 已回退的行不再进入矛盾检测
    out2, contradiction_disclosures = reconcile_contradictory_entity_repairs(out, repairs)
    assert contradiction_disclosures == []


def test_registered_entity_repair_untouched():
    repairs = [
        {
            "cue_indexes": [2],
            "before": ["恋死我自己有看"],
            "after": ["恋青我自己有看"],
            "expected_entity": "恋青",
        }
    ]
    out, disclosures = revert_unregistered_entity_repairs(UNREG_SRT, repairs, REGISTERED)
    assert out == UNREG_SRT and disclosures == []
    assert "reconciliation" not in repairs[0]
