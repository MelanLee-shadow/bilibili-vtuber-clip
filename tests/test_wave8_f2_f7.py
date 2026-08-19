"""维护者 Wave 8 F2/F7 pristine-machine canaries."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from src.autoslice import producer_text_pipeline as pipeline
from src.autoslice.final_review_auditor import (
    adjudicate_context_finding,
    audit_final_subtitles,
)
from src.autoslice.pronoun_consistency import (
    CandidatePronounAuditError,
    discover_candidate_pronoun_findings,
)


def _srt(*texts: str) -> str:
    return "\n\n".join(
        f"{index}\n00:00:{index * 5:02d},000 --> "
        f"00:00:{index * 5 + 4:02d},000\n{text}"
        for index, text in enumerate(texts, start=1)
    ) + "\n"


F2_PRISTINE_MACHINE_SRT = _srt(
    "但是我，但是他就是不肯跟我抱",
    "我想，我想跟他说",
    "结果它不愿意跟我走",
    "我也觉得TA有问题",
    "TA不给我报身份",
    "确实，我觉得TA也，TA身份有问题",
    "然后我就一直在那看他们",
    "看他们在干嘛",
)


def _keep_every_machine_occurrence(prompt: str) -> str:
    marker = "机器枚举的 occurrence（位置由代码绑定）：\n"
    payload_text = prompt.split(marker, 1)[1].split(
        "\n\n只输出 JSON", 1
    )[0]
    occurrences = json.loads(payload_text)
    return json.dumps(
        {
            "decisions": [
                {
                    "occurrence_id": row["occurrence_id"],
                    "action": "KEEP_CURRENT",
                    "current_token": row["current_token"],
                    "replacement_token": row["current_token"],
                    "reason": "逐项回执测试；不提供人工真值",
                }
                for row in occurrences
            ]
        },
        ensure_ascii=False,
    )


def test_f2_pristine_machine_occurrences_receive_complete_candidate_receipt():
    prompts: list[str] = []

    findings, audit = discover_candidate_pronoun_findings(
        F2_PRISTINE_MACHINE_SRT,
        policy_text="在场者性别需由候选全文与主播政策判断",
        candidate_context_text="机器候选：本段是多人联动对局讨论",
        llm_call=lambda prompt: (
            prompts.append(prompt) or _keep_every_machine_occurrence(prompt)
        ),
        extract_json=json.loads,
    )

    assert findings == []
    assert audit["status"] == "PASS"
    assert audit["occurrence_count"] == audit["decision_count"] == 9
    assert audit["rewrite_count"] == 0
    assert {row["current_token"] for row in audit["occurrences"]} == {
        "他",
        "它",
        "TA",
        "他们",
    }
    assert "每一个 TA/他/她/它/TA们/他们/她们/它们" in prompts[0]
    assert "维护者 2026-07-10" in prompts[0]
    assert audit["mutation_authorized"] is False


def test_f2_pristine_machine_occurrence_omission_fails_closed():
    def omit_last(prompt: str) -> str:
        payload = json.loads(_keep_every_machine_occurrence(prompt))
        payload["decisions"].pop()
        return json.dumps(payload, ensure_ascii=False)

    with pytest.raises(CandidatePronounAuditError) as raised:
        discover_candidate_pronoun_findings(
            F2_PRISTINE_MACHINE_SRT,
            policy_text="在场者性别需由候选全文与主播政策判断",
            candidate_context_text="机器候选：本段是多人联动对局讨论",
            llm_call=omit_last,
            extract_json=json.loads,
        )

    assert raised.value.reason_code == (
        "CANDIDATE_PRONOUN_DECISION_COVERAGE_INVALID"
    )


def test_f2_machine_gender_context_yields_bounded_single_and_plural_findings():
    source = _srt(
        "我也觉得TA有问题",
        "确实，我觉得TA也，TA身份有问题",
        "看他们在干嘛",
    )

    def decide_from_machine_context(prompt: str) -> str:
        target = prompt.split("在场者性别语境:", 1)[1][0]
        marker = "机器枚举的 occurrence（位置由代码绑定）：\n"
        occurrences = json.loads(
            prompt.split(marker, 1)[1].split("\n\n只输出 JSON", 1)[0]
        )
        return json.dumps(
            {
                "decisions": [
                    {
                        "occurrence_id": row["occurrence_id"],
                        "action": "REWRITE",
                        "current_token": row["current_token"],
                        "replacement_token": (
                            target + "们"
                            if row["current_token"].endswith("们")
                            else target
                        ),
                        "reason": "候选机器语境声明了在场者性别",
                    }
                    for row in occurrences
                ]
            },
            ensure_ascii=False,
        )

    findings, audit = discover_candidate_pronoun_findings(
        source,
        policy_text="主播联动代词按在场者性别",
        candidate_context_text="在场者性别语境:她",
        llm_call=decide_from_machine_context,
        extract_json=json.loads,
    )

    assert [row["proposed_full_cue"] for row in findings] == [
        "我也觉得她有问题",
        "确实，我觉得她也，她身份有问题",
        "看她们在干嘛",
    ]
    assert audit["occurrence_count"] == audit["decision_count"] == 4
    assert audit["rewrite_count"] == 4
    assert audit["finding_count"] == 3
    assert all(row["repair_class"] == "phonetic" for row in findings)
    routed = audit_final_subtitles(
        source,
        llm_call=lambda _prompt: json.dumps({"findings": []}),
        extract_json=json.loads,
        extra_raw_findings=findings,
        prioritize_extra_raw_findings=True,
    )
    assert [row["proposed_full_cue"] for row in routed] == [
        "我也觉得她有问题",
        "确实，我觉得她也，她身份有问题",
        "看她们在干嘛",
    ]


def test_f2_exact_final_audit_records_candidate_pronoun_receipt(monkeypatch):
    receipt = {
        "schema_version": "candidate-pronoun-consistency-audit.v1",
        "status": "PASS",
        "occurrence_count": 9,
        "decision_count": 9,
        "rewrite_count": 0,
        "mutation_authorized": False,
    }
    monkeypatch.setattr(
        pipeline,
        "discover_candidate_pronoun_findings",
        lambda *_args, **_kwargs: ([], receipt),
    )
    monkeypatch.setattr(
        pipeline,
        "audit_final_subtitles",
        lambda *_args, **_kwargs: [],
    )
    monkeypatch.setattr(
        pipeline,
        "_build_final_review_llm_call",
        lambda: (lambda _prompt: json.dumps({"findings": []})),
    )
    monkeypatch.setattr(
        pipeline,
        "clip_context_prompt_text",
        lambda _context: "机器候选：本段是多人联动对局讨论",
    )

    audit = pipeline._run_exact_final_release_review(
        srt_text=F2_PRISTINE_MACHINE_SRT,
        correction_audit={},
        adapters=SimpleNamespace(review_glossary=lambda: ""),
        authoritative_chat=(),
        selection_hook="",
        clip_context={},
    )

    assert "candidate_pronoun_consistency_audit" in audit, audit
    assert audit["candidate_pronoun_consistency_audit"] == receipt


@pytest.mark.parametrize(
    ("machine_current", "machine_attempted", "reviewer_truth", "current_is_truth"),
    [
        ("油菜", "太菜", "由菜", False),
        ("家人们", "家人", "家人们", True),
    ],
)
def test_f7_pristine_context_rewrite_without_acoustic_witness_is_disclosure_only(
    machine_current: str,
    machine_attempted: str,
    reviewer_truth: str,
    current_is_truth: bool,
):
    source = _srt("上一句", machine_current, "下一句")
    witness_requests: list[dict[str, object]] = []

    def cpa(prompt: str) -> str:
        if "# 字幕缺失候选重建" in prompt:
            return json.dumps(
                {
                    "status": "UNRESOLVED",
                    "proposed_cue": "",
                    "reason": "机器首轮没有形成局部候选",
                },
                ensure_ascii=False,
            )
        return json.dumps(
            {
                "decision": "REPLACE_WITH_EXACT_TEXT",
                "replacement_text": machine_attempted,
                "reason": "机器 context-only convergence 的原始尝试",
            },
            ensure_ascii=False,
        )

    def pristine_not_required_witness(
        request: dict[str, object],
    ) -> dict[str, object]:
        witness_requests.append(request)
        return {
            "schema_version": "subtitle-span-acoustic-witness.v1",
            "request_sha256": None,
            "status": "NOT_REQUIRED",
            "target_audible": None,
        }

    output, adjudication = adjudicate_context_finding(
        source,
        {
            "cue_index": 2,
            "suspect": machine_current,
            "suggestion": None,
            "proposed_full_cue": None,
            "repair_class": "disclosure_only",
            "why": "机器审片员仅给了语境疑点",
        },
        entity_verifier=pristine_not_required_witness,
        judge_llm_call=cpa,
    )

    assert len(witness_requests) == 1
    assert "proposed_cue" not in witness_requests[0]
    assert output == source
    assert adjudication["repaired"] is False
    assert adjudication["reason_code"] == (
        "CONTEXT_REWRITE_ACOUSTIC_WITNESS_REQUIRED"
    )
    assert adjudication["rebuilt_finding"]["repair_class"] == (
        "disclosure_only"
    )
    assert adjudication["mutation_authority"] == {
        "schema_version": "subtitle-correction-mutation-authority.v1",
        "status": "NOT_APPLIED",
        "basis": "ACOUSTIC_WITNESS_NOT_OBSERVED",
    }
    assert (
        adjudication["cpa_missing_proposal_convergence"][
            "mutation_authorized"
        ]
        is False
    )
    # 维护者 truth is deliberately only a post-execution comparison assertion.
    assert (machine_current == reviewer_truth) is current_is_truth
