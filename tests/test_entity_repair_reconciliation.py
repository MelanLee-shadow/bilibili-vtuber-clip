"""同槽矛盾裁定和解：互斥 RESOLVED → 回退+披露，绝不后写者赢。"""

import copy
import hashlib

import pytest

from src.autoslice.chat_repair import apply_audio_entity_verification
from src.autoslice.jingting_chunker import parse_srt_cues

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


CPA_INPUT = """1
00:00:01,000 --> 00:00:02,500
我刚刚发生一个问题

2
00:00:03,000 --> 00:00:04,000
后面一句不改
"""


def _native_cpa_entity_repair(*, context_only=False):
    """Use actual row production; only the remote CPA verdict is synthetic."""
    group = ReferentGroup(
        (ReferentEntity("发生", ("发生",)), ReferentEntity("发现", ("发现",))),
        reason="synthetic local phrase divergence",
        positions=("transcript_only",),
        audio_verify_all_surfaces=True,
    )
    calls = []

    def verdict(request):
        calls.append(request)
        return {
            "schema_version": "chat-entity-verdict.v1",
            "request_sha256": request["request_sha256"],
            "status": "RESOLVED", "canonical_entity": "发现", "confidence": 0.8,
            "authority_kind": ("cpa_context_only_closed_set_adjudication"
                               if context_only else "cpa_witness_adjudication"),
            "decision_authority": "CPA_JUDGE", "witness_authority": "EVIDENCE_ONLY",
            "witness_status": "UNCERTAIN" if context_only else "OBSERVED",
            **{key: hashlib.sha256(("synthetic " + key).encode()).hexdigest()
               for key in ("witness_request_sha256", "judge_prompt_sha256", "judge_completion_sha256")},
        }

    changed, audit = apply_audio_entity_verification(
        CPA_INPUT, referent_groups=[group], entity_verifier=verdict,
    )
    assert len(calls) == 1 and len(audit["repairs"]) == 1
    assert "我刚刚发现一个问题" in changed
    return changed, audit["repairs"][0]


@pytest.mark.parametrize("context_only", [False, True])
def test_registration_guard_keeps_validated_cpa_choice_outside_global_names(context_only):
    changed, row = _native_cpa_entity_repair(context_only=context_only)
    out, disclosures = revert_unregistered_entity_repairs(changed, [row], REGISTERED)
    assert out == changed  # A global vocabulary miss is not a second semantic vote.
    assert disclosures == [] and "reconciliation" not in row
    old_cues, new_cues = parse_srt_cues(CPA_INPUT), parse_srt_cues(out)
    assert [(c.start_ms, c.end_ms) for c in old_cues] == [(c.start_ms, c.end_ms) for c in new_cues]
    assert new_cues[1].text == old_cues[1].text


@pytest.mark.parametrize("key,value", [
    ("schema_version", "wrong"),
    ("status", "UNCERTAIN"),
    ("canonical_entity", "发生"),
    ("authority_kind", "audio_forced_choice"),
    ("authority_kind", "invented"),
    ("decision_authority", "ASR"),
    ("witness_authority", "MUTATION_AUTHORITY"),
    ("witness_status", "FAILED"),
    ("confidence", True),
    ("confidence", float("nan")),
    ("request_sha256", None),
    ("request_sha256", "invalid"),
    ("witness_request_sha256", None),
    ("judge_prompt_sha256", "invalid"),
    ("judge_completion_sha256", None),
])
def test_registration_guard_does_not_trust_incomplete_or_foreign_cpa_claim(key, value):
    changed, row = _native_cpa_entity_repair()
    row["verdict"][key] = value
    out, disclosures = revert_unregistered_entity_repairs(changed, [row], REGISTERED)
    assert out == CPA_INPUT
    assert disclosures[0]["reverted_cues"] == [1]
    assert row["reconciliation"] == "UNREGISTERED_ENTITY_REVERTED"


def test_registration_guard_retains_original_unregistered_reversion_without_verdict():
    changed, row = _native_cpa_entity_repair()
    del row["verdict"]
    out, disclosures = revert_unregistered_entity_repairs(changed, [row], REGISTERED)
    assert out == CPA_INPUT and disclosures[0]["reverted_cues"] == [1]


def test_registration_guard_keeps_resolved_canonical_cpa_row_without_expected_alias():
    changed, row = _native_cpa_entity_repair()
    del row["expected_entity"]
    out, disclosures = revert_unregistered_entity_repairs(changed, [row], REGISTERED)
    assert out == changed and disclosures == []


def test_registration_guard_does_not_reclassify_nonentity_final_text_owners():
    changed, row = _native_cpa_entity_repair()
    owner = {"mode": "final_review_context_adjudication", "cue_indexes": [1],
             "before": row["before"], "after": row["after"]}
    before = copy.deepcopy(owner)
    out, disclosures = revert_unregistered_entity_repairs(changed, [owner], REGISTERED)
    assert out == changed and owner == before and disclosures == []


@pytest.mark.parametrize("row_hash", [None, "0" * 64])
def test_registration_guard_does_not_attach_cpa_verdict_to_another_request(row_hash):
    changed, row = _native_cpa_entity_repair()
    row["request_sha256"] = row_hash
    out, disclosures = revert_unregistered_entity_repairs(changed, [row], REGISTERED)
    assert out == CPA_INPUT and disclosures[0]["reverted_cues"] == [1]
