"""Synthetic controls for the same-referent pronoun consistency check.

Offline protocol tests with scripted provider replies; they are NOT model
accuracy evidence.
"""

from __future__ import annotations

import json

import pytest

from src.autoslice import pronoun_consistency as native
from src.autoslice import speaker_attribution as sa

POLICY = '公开规则政策（合成）'
CONTEXT = "李豆沙和闺蜜两人对话（合成）。"


def srt(*texts: str) -> str:
    return (
        "\n\n".join(
            f"{i}\n00:00:{i * 2:02d},000 --> 00:00:{i * 2 + 1:02d},000\n{text}"
            for i, text in enumerate(texts, 1)
        )
        + "\n"
    )


def d(occurrence: str, current: str, final: str, referent: str | None, reason: str = "依据"):
    row = {
        "occurrence_id": occurrence,
        "action": "KEEP_CURRENT" if current == final else "REWRITE",
        "current_token": current,
        "replacement_token": final,
        "reason": reason,
    }
    if referent is not None:
        row["referent"] = referent
    return row


class Scripted:
    def __init__(self, *replies):
        self.replies = list(replies)
        self.prompts: list[str] = []

    def __call__(self, prompt: str) -> str:
        self.prompts.append(prompt)
        reply = self.replies.pop(0)
        return reply if isinstance(reply, str) else json.dumps({"decisions": reply}, ensure_ascii=False)


def run(source, llm, **kwargs):
    return native.discover_candidate_pronoun_findings(
        source,
        policy_text=POLICY,
        candidate_context_text=CONTEXT,
        llm_call=llm,
        extract_json=json.loads,
        **kwargs,
    )


# 闺蜜 scenario shaped like the reported cues: TA / TA / 她 for one referent.
BESTIE = srt("那TA说的是那个切片吗", "TA说我们要演一个", "但她没有看过这个")
MIXED = [
    d("cue-1-occurrence-1", "TA", "TA", "OTHER:闺蜜"),
    d("cue-2-occurrence-1", "TA", "TA", "OTHER:闺蜜"),
    d("cue-3-occurrence-1", "她", "她", "OTHER:闺蜜"),
]


def test_prompt_keeps_reviewer_policy_and_adds_consistency_clause():
    llm = Scripted([d("cue-1-occurrence-1", "她", "她", "HOST")])
    run(srt("她来了"), llm)
    prompt = llm.prompts[0]
    assert '公开规则书面政策：已知女性用“她”' in prompt
    assert "只有人的性别确实无法从全文、姓名或\n语境判断时才用“TA/TA们”" in prompt
    assert "同一个 referent 在全片必须用同一个" in prompt
    assert '"referent":"HOST"' in prompt


def test_mixed_forms_for_one_referent_trigger_one_retry_that_can_resolve():
    fixed = [
        d("cue-1-occurrence-1", "TA", "她", "OTHER:闺蜜"),
        d("cue-2-occurrence-1", "TA", "她", "OTHER:闺蜜"),
        d("cue-3-occurrence-1", "她", "她", "OTHER:闺蜜"),
    ]
    llm = Scripted(MIXED, fixed)
    findings, audit = run(BESTIE, llm)
    assert len(llm.prompts) == 2
    assert "MIXED_FORMS" in llm.prompts[1] and "OTHER:闺蜜" in llm.prompts[1]
    assert audit["consistency"]["status"] == "RETRY_RESOLVED"
    assert audit["consistency"]["initial_conflicts"][0]["referent"] == "OTHER:闺蜜"
    assert [f["cue"] for f in findings] == [1, 2]
    assert [f["proposed_full_cue"] for f in findings] == ["那她说的是那个切片吗", "她说我们要演一个"]
    assert audit["mutation_authorized"] is False
    assert audit["completion_sha256"] == audit["consistency"]["retry_completion_sha256"]


def test_conflict_surviving_retry_becomes_ordinary_findings_unified_to_she():
    llm = Scripted(MIXED, MIXED)
    findings, audit = run(BESTIE, llm)
    assert len(llm.prompts) == 2  # exactly one consistency retry
    assert audit["consistency"]["status"] == "UNRESOLVED_UNIFIED"
    assert audit["consistency"]["remaining_conflicts"]
    assert sorted(audit["consistency"]["forced_occurrence_ids"]) == [
        "cue-1-occurrence-1",
        "cue-2-occurrence-1",
    ]
    assert [f["proposed_full_cue"] for f in findings] == ["那她说的是那个切片吗", "她说我们要演一个"]
    assert all(f["kind"] == "context" and f["repair_class"] == "phonetic" for f in findings)
    assert "代码一致性统一" in findings[0]["why"]
    forced = audit["decisions"][0]
    assert forced["action"] == "REWRITE" and forced["replacement_token"] == "她"
    assert forced["referent"] == "OTHER:闺蜜"
    # The audit still validates as scoped context evidence.
    assert native.scoped_pronoun_policy_evidence(BESTIE, audit, 1) is not None


def test_unusable_retry_does_not_block_and_still_unifies():
    llm = Scripted(MIXED, "not json at all")
    findings, audit = run(BESTIE, llm)
    assert audit["consistency"]["status"] == "RETRY_UNUSABLE_UNIFIED"
    assert len(findings) == 2


@pytest.mark.parametrize("wrong", ["他", "TA"])
def test_known_female_host_is_she_everywhere(wrong):
    source = srt("我觉得" + wrong + "不会来", "我也说过她不会来")
    decisions = [
        d("cue-1-occurrence-1", wrong, wrong, "HOST"),
        d("cue-2-occurrence-1", "她", "她", "HOST"),
    ]
    llm = Scripted(decisions, decisions)
    findings, audit = run(source, llm)
    assert [f["proposed_full_cue"] for f in findings] == ["我觉得她不会来"]
    assert audit["consistency"]["conflict_count"] == 1


def test_all_ta_host_is_also_a_violation():
    source = srt("TA来了", "TA走了")
    decisions = [
        d("cue-1-occurrence-1", "TA", "TA", "HOST"),
        d("cue-2-occurrence-1", "TA", "TA", "HOST"),
    ]
    findings, audit = run(source, Scripted(decisions, decisions))
    assert len(findings) == 2 and audit["consistency"]["status"] == "UNRESOLVED_UNIFIED"


def test_caller_can_declare_another_participant_known_female():
    source = srt("TA来了", "TA走了")
    decisions = [
        d("cue-1-occurrence-1", "TA", "TA", "OTHER:闺蜜"),
        d("cue-2-occurrence-1", "TA", "TA", "OTHER:闺蜜"),
    ]
    llm = Scripted(decisions)
    findings, _ = run(source, llm)
    assert findings == [] and len(llm.prompts) == 1  # undeterminable gender: TA stays
    llm = Scripted(decisions, decisions)
    findings, _ = run(source, llm, known_female_referents={"OTHER:闺蜜"})
    assert len(findings) == 2


def test_undeterminable_gender_referent_stays_ta_when_consistent():
    source = srt("路人说TA不认识", "后来TA又来了", "他们也到了")
    decisions = [
        d("cue-1-occurrence-1", "TA", "TA", "THIRD:路人"),
        d("cue-2-occurrence-1", "TA", "TA", "THIRD:路人"),
        d("cue-3-occurrence-1", "他们", "他们", "VIEWERS"),
    ]
    llm = Scripted(decisions)
    findings, audit = run(source, llm)
    assert findings == [] and len(llm.prompts) == 1
    assert audit["consistency"] == {"status": "CONSISTENT", "conflict_count": 0}


def test_ta_mixed_with_gendered_for_same_unknown_referent_is_unified_to_gendered():
    source = srt("路人说TA不认识", "后来他又来了")
    decisions = [
        d("cue-1-occurrence-1", "TA", "TA", "THIRD:路人"),
        d("cue-2-occurrence-1", "他", "他", "THIRD:路人"),
    ]
    findings, audit = run(source, Scripted(decisions, decisions))
    assert [f["proposed_full_cue"] for f in findings] == ["路人说他不认识"]
    assert audit["decisions"][0]["replacement_token"] == "他"


def test_plural_forms_stay_plural_when_unified():
    source = srt("她们来了", "TA们走了")
    decisions = [
        d("cue-1-occurrence-1", "她们", "她们", "THIRD:粉丝团"),
        d("cue-2-occurrence-1", "TA们", "TA们", "THIRD:粉丝团"),
    ]
    findings, _ = run(source, Scripted(decisions, decisions))
    assert [f["proposed_full_cue"] for f in findings] == ["她们走了"]


def test_different_referents_may_use_different_forms():
    source = srt("她来了", "他走了", "TA不知道")
    decisions = [
        d("cue-1-occurrence-1", "她", "她", "HOST"),
        d("cue-2-occurrence-1", "他", "他", "THIRD:室友"),
        d("cue-3-occurrence-1", "TA", "TA", "THIRD:路人"),
    ]
    llm = Scripted(decisions)
    findings, _ = run(source, llm)
    assert findings == [] and len(llm.prompts) == 1


def test_missing_referent_is_unspecified_and_not_grouped():
    source = srt("她来了", "他走了")
    decisions = [
        d("cue-1-occurrence-1", "她", "她", None),
        d("cue-2-occurrence-1", "他", "他", None),
    ]
    llm = Scripted(decisions)
    findings, audit = run(source, llm)
    assert findings == [] and len(llm.prompts) == 1
    assert {row["referent"] for row in audit["decisions"]} == {"UNSPECIFIED"}


def test_invalid_referent_uses_the_existing_contract_repair_path():
    source = srt("她来了")
    bad = [d("cue-1-occurrence-1", "她", "她", "somebody random")]
    good = [d("cue-1-occurrence-1", "她", "她", "HOST")]
    llm = Scripted(bad, good)
    _, audit = run(source, llm)
    assert audit["contract_repair"]["status"] == "VALIDATED"
    assert audit["decisions"][0]["referent"] == "HOST"


def test_speaker_attribution_is_prompt_context_and_bare_other_gets_its_label():
    attribution = sa.attribute_speakers(
        BESTIE,
        scorer=lambda i: {1: 0.9, 2: 0.9, 3: 0.5}[i],
        llm_call=lambda _p: json.dumps(
            {"labels": [{"n": 3, "speaker": "OTHER", "address": "闺蜜", "confidence": 0.8, "reason": "接话"}]}
        ),
    )
    decisions = [
        d("cue-1-occurrence-1", "TA", "她", "OTHER"),
        d("cue-2-occurrence-1", "TA", "她", "OTHER:闺蜜"),
        d("cue-3-occurrence-1", "她", "她", "OTHER:闺蜜"),
    ]
    llm = Scripted(decisions)
    findings, audit = run(BESTIE, llm, speaker_attribution=attribution)
    assert "3. OTHER:闺蜜（context_expansion）" in llm.prompts[0]
    assert audit["consistency"]["status"] == "CONSISTENT"  # OTHER == OTHER:闺蜜
    assert {row["referent"] for row in audit["decisions"]} == {"OTHER:闺蜜"}
    assert len(findings) == 2 and audit["speaker_attribution_context_sha256"].startswith("sha256:")


def test_stale_speaker_attribution_is_ignored():
    attribution = sa.attribute_speakers(
        BESTIE, scorer=lambda i: 0.9, llm_call=lambda _p: json.dumps({"labels": []})
    )
    llm = Scripted([d("cue-1-occurrence-1", "她", "她", "HOST")])
    _, audit = run(srt("她来了"), llm, speaker_attribution=attribution)
    assert "（无）" in llm.prompts[0].split("说话人归属（内部证据")[1].split("机器终稿候选")[0]
    assert "speaker_attribution_context_sha256" not in audit


def test_replay_reuses_unified_decisions_without_provider_calls():
    llm = Scripted(MIXED, MIXED)
    findings, audit = run(BESTIE, llm)
    replay_llm = Scripted()
    replay_findings, replay_audit = run(BESTIE, replay_llm, prior_audit=audit)
    assert replay_llm.prompts == []
    assert replay_audit["prior_audit_replay"]["provider_calls"] == 0
    assert replay_findings == findings
    assert replay_audit["consistency"] == audit["consistency"]


def test_prompt_states_viewers_are_he_not_ta():
    llm = Scripted([d("cue-1-occurrence-1", "他", "他", "VIEWERS")])
    run(srt("他说得对"), llm)
    assert "VIEWERS（弹幕、发弹幕的人或观众群体）是例外" in llm.prompts[0]
    assert "一律写\n“他/他们”，不写 TA" in llm.prompts[0]


def test_viewers_written_as_ta_are_flagged_and_unified_to_he():
    source = srt("弹幕说TA们都笑了", "有TA在刷屏")
    decisions = [
        d("cue-1-occurrence-1", "TA们", "TA们", "VIEWERS"),
        d("cue-2-occurrence-1", "TA", "TA", "VIEWERS"),
    ]
    llm = Scripted(decisions, decisions)
    findings, audit = run(source, llm)
    assert len(llm.prompts) == 2  # a retry is requested like any violation
    assert "VIEWERS_NOT_HE_FORM" in llm.prompts[1]
    assert audit["consistency"]["status"] == "UNRESOLVED_UNIFIED"
    assert [f["proposed_full_cue"] for f in findings] == ["弹幕说他们都笑了", "有他在刷屏"]


def test_viewers_ta_can_be_fixed_by_the_retry():
    source = srt("弹幕说TA不对")
    llm = Scripted(
        [d("cue-1-occurrence-1", "TA", "TA", "VIEWERS")],
        [d("cue-1-occurrence-1", "TA", "他", "VIEWERS")],
    )
    findings, audit = run(source, llm)
    assert audit["consistency"]["status"] == "RETRY_RESOLVED"
    assert [f["proposed_full_cue"] for f in findings] == ["弹幕说他不对"]


def test_viewers_target_is_he_even_when_most_say_she():
    source = srt("观众她们说", "观众她们又说", "观众他们说")
    decisions = [
        d("cue-1-occurrence-1", "她们", "她们", "VIEWERS"),
        d("cue-2-occurrence-1", "她们", "她们", "VIEWERS"),
        d("cue-3-occurrence-1", "他们", "他们", "VIEWERS"),
    ]
    findings, _ = run(source, Scripted(decisions, decisions))
    assert [f["cue"] for f in findings] == [1, 2]
    assert all("他们" in f["proposed_full_cue"] for f in findings)


def test_viewers_written_as_he_is_consistent():
    source = srt("弹幕说他们来了", "有他在刷屏")
    decisions = [
        d("cue-1-occurrence-1", "他们", "他们", "VIEWERS"),
        d("cue-2-occurrence-1", "他", "他", "VIEWERS"),
    ]
    llm = Scripted(decisions)
    findings, audit = run(source, llm)
    assert findings == [] and len(llm.prompts) == 1
    assert audit["consistency"] == {"status": "CONSISTENT", "conflict_count": 0}


def test_other_referents_keep_the_ta_rules_after_the_viewers_exception():
    source = srt("路人说TA不认识", "后来TA又来了")
    decisions = [
        d("cue-1-occurrence-1", "TA", "TA", "THIRD:路人"),
        d("cue-2-occurrence-1", "TA", "TA", "THIRD:路人"),
    ]
    llm = Scripted(decisions)
    findings, _ = run(source, llm)
    assert findings == [] and len(llm.prompts) == 1
