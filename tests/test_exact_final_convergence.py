from __future__ import annotations

import hashlib
import json

from src.autoslice.exact_final_convergence import (
    collect_exact_final_convergence_memos,
    converge_reconsidered_exact_final_findings,
    rebind_exact_final_convergence_memos,
    resolve_findings_from_exact_final_convergence_memos,
)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _srt(cue_text: str, *, neighbor: str = "我试下这个") -> str:
    return (
        "1\n00:00:00,000 --> 00:00:01,000\n还是这个\n\n"
        f"2\n00:00:01,100 --> 00:00:02,100\n{cue_text}\n\n"
        f"3\n00:00:02,200 --> 00:00:03,200\n{neighbor}\n"
    )


def _authority(before: str, after: str) -> dict:
    return {
        "exact_final_cpa_self_heal": {
            "schema_version": "exact-final-cpa-self-heal-audit.v1",
            "status": "REVIEW_PENDING",
            "passes": [
                {
                    "schema_version": "exact-final-cpa-self-heal-pass.v1",
                    "pass_index": 1,
                    "input_srt_sha256": "sha256:" + "a" * 64,
                    "output_srt_sha256": "sha256:" + "b" * 64,
                    "repairs": [
                        {
                            "schema_version": "exact-final-cpa-self-heal.v1",
                            "cue_index": 2,
                            "matched_start_ms": 1_100,
                            "matched_end_ms": 2_100,
                            "before": before,
                            "after": after,
                            "before_sha256": "sha256:" + _sha(before),
                            "after_sha256": "sha256:" + _sha(after),
                            "finding_sha256": "sha256:" + "c" * 64,
                            "request_sha256": "sha256:" + "d" * 64,
                            "decision_authority": "CPA_JUDGE",
                            "mutation_authority": {
                                "schema_version": ("subtitle-correction-mutation-authority.v1"),
                                "status": "PASS",
                                "basis": ("CPA_ACOUSTIC_PRONUNCIATION_DISAMBIGUATION"),
                            },
                            "timing_immutable": True,
                        }
                    ],
                }
            ],
        }
    }


def _reconsidered_finding(current: str, proposed: str) -> dict:
    return {
        "cue_index": 2,
        "base_text_sha256": _sha(current),
        "proposed_full_cue": proposed,
        "suspect": current,
        "suggestion": proposed,
        "repair_class": "phonetic",
        "exact_release_adjudication": {
            "schema_version": "subtitle-span-adjudication.v1",
            "status": "OBSERVED",
            "decision_authority": "CPA_JUDGE",
            "repaired": True,
            "timing_immutable": True,
            "policy_branch": "WITNESS_JUDGE_APPLY_PROPOSED",
            "mutation_authority": {
                "schema_version": "subtitle-correction-mutation-authority.v1",
                "status": "PASS",
                "basis": "CPA_ACOUSTIC_PRONUNCIATION_DISAMBIGUATION",
            },
            "request": {
                "schema_version": "subtitle-span-acoustic-check-request.v1",
                "request_sha256": "e" * 64,
                "base_text_sha256": _sha(current),
                "current_cue": current,
                "proposed_cue": proposed,
                "matched_start_ms": 1_100,
                "matched_end_ms": 2_100,
            },
            "verdict": {
                "schema_version": "subtitle-span-acoustic-witness.v1",
                "status": "OBSERVED",
                "target_audible": True,
                "heard_pinyin": "san er",
                "request_sha256": "f" * 64,
            },
            "witness_judge": {
                "judge": {
                    "schema_version": "acoustic-witness-adjudication.v1",
                    "status": "JUDGED",
                    "choice": "PROPOSED",
                    "reason": "本轮二元闭集选择 proposed",
                    "prompt_sha256": "1" * 64,
                    "completion_sha256": "2" * 64,
                }
            },
        },
    }


def test_reconsidered_cue_gets_history_aware_cpa_final_memo() -> None:
    current = "三二"
    proposed = "方案二"
    unresolved, resolved = converge_reconsidered_exact_final_findings(
        _srt(current),
        [_reconsidered_finding(current, proposed)],
        authority_audit=_authority("方案二", current),
        judge_llm_call=lambda prompt: json.dumps(
            {
                "choice": "CURRENT",
                "reason": "结合此前反向裁决链，三二是最终文字。",
            },
            ensure_ascii=False,
        ),
    )

    assert unresolved == []
    assert len(resolved) == 1
    adjudication = resolved[0]["exact_release_adjudication"]
    assert adjudication["repaired"] is False
    assert adjudication["policy_branch"] == ("CPA_HISTORY_CONVERGENCE_KEEP_CURRENT")
    memo = adjudication["exact_final_cpa_convergence_memo"]
    assert memo["status"] == "RESOLVED"
    assert memo["decision_authority"] == "CPA_JUDGE"
    assert memo["choice"] == "CURRENT"
    assert memo["final_text"] == current
    assert memo["matched_start_ms"] == 1_100
    assert memo["matched_end_ms"] == 2_100
    assert memo["history_receipts"][0]["after_sha256"] == ("sha256:" + _sha(current))


def test_history_aware_cpa_can_apply_proposed_then_memo_closes_repeat() -> None:
    current = "三二"
    proposed = "方案二"
    unresolved, resolved = converge_reconsidered_exact_final_findings(
        _srt(current),
        [_reconsidered_finding(current, proposed)],
        authority_audit=_authority("方案二", current),
        judge_llm_call=lambda prompt: json.dumps(
            {
                "choice": "PROPOSED",
                "reason": "完整历史和语境支持方案二。",
            },
            ensure_ascii=False,
        ),
    )

    assert resolved == []
    assert len(unresolved) == 1
    adjudication = unresolved[0]["exact_release_adjudication"]
    assert adjudication["repaired"] is True
    assert adjudication["policy_branch"] == ("CPA_HISTORY_CONVERGENCE_APPLY_PROPOSED")
    memo = adjudication["exact_final_cpa_convergence_memo"]
    assert memo["final_text"] == proposed

    promoted = collect_exact_final_convergence_memos(
        {"findings": unresolved, "resolved_findings": []}
    )
    pending, memo_resolved = resolve_findings_from_exact_final_convergence_memos(
        _srt(proposed),
        [
            {
                "cue_index": 2,
                "base_text_sha256": _sha(proposed),
                "proposed_full_cue": current,
                "suspect": proposed,
                "suggestion": current,
            }
        ],
        authority_audit={"exact_final_cpa_convergence_memos": promoted},
    )
    assert pending == []
    assert len(memo_resolved) == 1
    assert memo_resolved[0]["resolution"] == ("CPA_HISTORY_CONVERGENCE_MEMO_FINAL_TEXT")


def test_convergence_memo_does_not_survive_changed_local_context() -> None:
    current = "三二"
    proposed = "方案二"
    unresolved, _resolved = converge_reconsidered_exact_final_findings(
        _srt(current),
        [_reconsidered_finding(current, proposed)],
        authority_audit=_authority("方案二", current),
        judge_llm_call=lambda prompt: json.dumps(
            {
                "choice": "PROPOSED",
                "reason": "完整历史和语境支持方案二。",
            },
            ensure_ascii=False,
        ),
    )
    promoted = collect_exact_final_convergence_memos({"findings": unresolved})
    finding = {
        "cue_index": 2,
        "base_text_sha256": _sha(proposed),
        "proposed_full_cue": current,
        "suspect": proposed,
        "suggestion": current,
    }

    pending, memo_resolved = resolve_findings_from_exact_final_convergence_memos(
        _srt(proposed, neighbor="这里出现了新的绑定语境"),
        [finding],
        authority_audit={"exact_final_cpa_convergence_memos": promoted},
    )
    assert pending == [finding]
    assert memo_resolved == []


def test_same_pass_cpa_neighbor_repair_rebinds_memo_with_audit() -> None:
    current = "三二"
    proposed = "方案二"
    unresolved, _resolved = converge_reconsidered_exact_final_findings(
        _srt(current),
        [_reconsidered_finding(current, proposed)],
        authority_audit=_authority("方案二", current),
        judge_llm_call=lambda prompt: json.dumps(
            {
                "choice": "PROPOSED",
                "reason": "完整历史和语境支持方案二。",
            },
            ensure_ascii=False,
        ),
    )
    memo = collect_exact_final_convergence_memos({"findings": unresolved})[0]

    rebound = rebind_exact_final_convergence_memos(
        _srt(proposed, neighbor="同一轮 CPA 也修复了相邻 cue"),
        [memo],
    )
    assert len(rebound) == 1
    assert rebound[0]["bound_local_context_sha256"] != (memo["bound_local_context_sha256"])
    assert rebound[0]["context_rebindings"][-1]["basis"] == (
        "SAME_PASS_CPA_AUTHORIZED_REPAIRS_FINAL_OUTPUT"
    )

    pending, resolved = resolve_findings_from_exact_final_convergence_memos(
        _srt(proposed, neighbor="同一轮 CPA 也修复了相邻 cue"),
        [
            {
                "cue_index": 2,
                "base_text_sha256": _sha(proposed),
                "proposed_full_cue": current,
                "suspect": proposed,
                "suggestion": current,
            }
        ],
        authority_audit={"exact_final_cpa_convergence_memos": rebound},
    )
    assert pending == []
    assert len(resolved) == 1


def test_invalid_prior_receipt_never_triggers_convergence_judge() -> None:
    current = "三二"
    authority = _authority("方案二", current)
    authority["exact_final_cpa_self_heal"]["passes"][0]["repairs"][0]["after_sha256"] = (
        "sha256:" + "0" * 64
    )
    called = False

    def judge(_prompt: str) -> str:
        nonlocal called
        called = True
        return '{"choice":"CURRENT","reason":"不应调用"}'

    finding = _reconsidered_finding(current, "方案二")
    unresolved, resolved = converge_reconsidered_exact_final_findings(
        _srt(current),
        [finding],
        authority_audit=authority,
        judge_llm_call=judge,
    )

    assert unresolved == [finding]
    assert resolved == []
    assert called is False
