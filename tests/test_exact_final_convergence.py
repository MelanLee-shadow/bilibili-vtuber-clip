from __future__ import annotations

import hashlib
import json
from copy import deepcopy

from src.autoslice.exact_final_convergence import (
    collect_exact_final_convergence_memos,
    converge_reconsidered_exact_final_findings,
    rebind_exact_final_convergence_memos,
    resolve_findings_from_exact_final_convergence_memos,
)
from src.autoslice.producer_package_finalization import (
    _apply_exact_final_cpa_repairs,
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
    assert adjudication["policy_branch"] == (
        "CPA_EXACT_FINAL_CYCLE_KEEP_CURRENT"
    )
    memo = adjudication["exact_final_cpa_cycle_memo"]
    assert memo["status"] == "LOCKED"
    assert memo["decision_authority"] == "CPA_JUDGE"
    assert memo["choice_id"] == "TEXT_" + _sha(current)
    assert memo["final_text"] == current
    assert memo["matched_start_ms"] == 1_100
    assert memo["matched_end_ms"] == 2_100
    assert memo["history_receipts"][0]["receipt"][
        "after_sha256"
    ] == ("sha256:" + _sha(current))


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
    assert adjudication["policy_branch"] == (
        "CPA_EXACT_FINAL_CYCLE_CLOSED_SET_APPLY"
    )
    memo = adjudication["exact_final_cpa_cycle_memo"]
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
    assert memo_resolved[0]["resolution"] == (
        "CPA_EXACT_FINAL_CYCLE_MEMO_LOCKED_FINAL_TEXT"
    )


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


def _srt_814(cue_1: str, cue_37: str) -> str:
    rows: list[str] = []
    for index in range(1, 38):
        if index == 1:
            start_ms, end_ms, text = 250, 810, cue_1
        elif index == 37:
            start_ms, end_ms, text = 69_000, 70_160, cue_37
        else:
            start_ms = 1_000 + (index - 2) * 1_800
            end_ms = start_ms + 1_000
            text = f"上下文{index}"

        def timestamp(value: int) -> str:
            hours, remainder = divmod(value, 3_600_000)
            minutes, remainder = divmod(remainder, 60_000)
            seconds, millis = divmod(remainder, 1_000)
            return (
                f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"
            )

        rows.append(
            f"{index}\n{timestamp(start_ms)} --> {timestamp(end_ms)}\n"
            f"{text}\n"
        )
    return "\n".join(rows)


def _repair(
    *,
    cue_index: int,
    start_ms: int,
    end_ms: int,
    before: str,
    after: str,
    salt: str,
) -> dict:
    return {
        "schema_version": "exact-final-cpa-self-heal.v1",
        "cue_index": cue_index,
        "matched_start_ms": start_ms,
        "matched_end_ms": end_ms,
        "before": before,
        "after": after,
        "before_sha256": "sha256:" + _sha(before),
        "after_sha256": "sha256:" + _sha(after),
        "finding_sha256": "sha256:" + _sha("finding-" + salt),
        "request_sha256": "sha256:" + _sha("request-" + salt),
        "decision_authority": "CPA_JUDGE",
        "policy_branch": "WITNESS_JUDGE_APPLY_PROPOSED",
        "judge": {
            "status": "JUDGED",
            "choice": "PROPOSED",
        },
        "acoustic_witness": {
            "status": "OBSERVED",
            "target_audible": True,
            "audio_clip_sha256": "sha256:" + _sha("audio-" + salt),
        },
        "mutation_authority": {
            "schema_version": "subtitle-correction-mutation-authority.v1",
            "status": "PASS",
            "basis": "CPA_ACOUSTIC_PRONUNCIATION_DISAMBIGUATION",
        },
        "timing_immutable": True,
    }


def _finding(
    *,
    cue_index: int,
    start_ms: int,
    end_ms: int,
    current: str,
    proposed: str,
    salt: str,
) -> dict:
    row = deepcopy(_reconsidered_finding(current, proposed))
    row["cue_index"] = cue_index
    row["base_text_sha256"] = _sha(current)
    adjudication = row["exact_release_adjudication"]
    request = adjudication["request"]
    request.update(
        request_sha256=_sha("request-" + salt),
        base_text_sha256=_sha(current),
        current_cue=current,
        proposed_cue=proposed,
        matched_start_ms=start_ms,
        matched_end_ms=end_ms,
    )
    adjudication["verdict"].update(
        audio_clip_sha256=_sha("audio-" + salt),
        heard_pinyin=f"814-{cue_index}-{salt}",
    )
    return row


def _authority_814() -> dict:
    cue_1_a = "谢谢"
    cue_1_b = "萝萝大"
    cue_1_c = "现在落到"
    cue_37_a = "先不说你数学好不好"
    cue_37_b = "先不说你数学了好吧"
    return {
        "exact_final_cpa_self_heal": {
            "schema_version": "exact-final-cpa-self-heal-audit.v1",
            "status": "REVIEW_PENDING",
            "passes": [
                {
                    "schema_version": (
                        "exact-final-cpa-self-heal-pass.v1"
                    ),
                    "pass_index": 1,
                    "input_srt_sha256": "sha256:" + "1" * 64,
                    "output_srt_sha256": "sha256:" + "2" * 64,
                    "repairs": [
                        _repair(
                            cue_index=1,
                            start_ms=250,
                            end_ms=810,
                            before=cue_1_a,
                            after=cue_1_b,
                            salt="a",
                        ),
                        _repair(
                            cue_index=37,
                            start_ms=69_000,
                            end_ms=70_160,
                            before=cue_37_a,
                            after=cue_37_b,
                            salt="d",
                        ),
                    ],
                },
                {
                    "schema_version": (
                        "exact-final-cpa-self-heal-pass.v1"
                    ),
                    "pass_index": 2,
                    "input_srt_sha256": "sha256:" + "3" * 64,
                    "output_srt_sha256": "sha256:" + "4" * 64,
                    "repairs": [
                        _repair(
                            cue_index=1,
                            start_ms=250,
                            end_ms=810,
                            before=cue_1_b,
                            after=cue_1_c,
                            salt="g",
                        )
                    ],
                },
            ],
        }
    }


def test_814_cue_1_and_37_cycles_use_one_closed_set_then_lock() -> None:
    cue_1_current = "现在落到"
    cue_1_final = "谢谢"
    cue_37_current = "先不说你数学了好吧"
    cue_37_final = "先不说你数学好不好"
    srt = _srt_814(cue_1_current, cue_37_current)
    findings = [
        _finding(
            cue_index=1,
            start_ms=250,
            end_ms=810,
            current=cue_1_current,
            proposed="谢谢萝萝的",
            salt="j",
        ),
        _finding(
            cue_index=1,
            start_ms=250,
            end_ms=810,
            current=cue_1_current,
            proposed=cue_1_final,
            salt="l",
        ),
        _finding(
            cue_index=37,
            start_ms=69_000,
            end_ms=70_160,
            current=cue_37_current,
            proposed=cue_37_final,
            salt="n",
        ),
    ]
    prompts: list[str] = []

    def judge(prompt: str) -> str:
        prompts.append(prompt)
        if "MATCHED_START_MS: 250" in prompt:
            assert cue_1_current in prompt
            assert "萝萝大" in prompt
            assert "谢谢萝萝的" in prompt
            assert cue_1_final in prompt
            return json.dumps(
                {
                    "choice_id": "TEXT_" + _sha(cue_1_final),
                    "reason": "完整历史和声学证据支持答谢词。",
                },
                ensure_ascii=False,
            )
        assert "MATCHED_START_MS: 69000" in prompt
        assert cue_37_current in prompt
        assert cue_37_final in prompt
        return json.dumps(
            {
                "choice_id": "TEXT_" + _sha(cue_37_final),
                "reason": "完整语境支持原始疑问句。",
            },
            ensure_ascii=False,
        )

    unresolved, resolved = converge_reconsidered_exact_final_findings(
        srt,
        findings,
        authority_audit=_authority_814(),
        judge_llm_call=judge,
    )

    assert len(prompts) == 2
    assert len(unresolved) == 2
    assert len(resolved) == 1
    repaired_srt, repairs = _apply_exact_final_cpa_repairs(
        srt,
        {"findings": unresolved},
    )
    assert len(repairs) == 2
    assert cue_1_final in repaired_srt
    assert cue_1_current not in repaired_srt
    assert cue_37_final in repaired_srt
    assert cue_37_current not in repaired_srt

    memos = collect_exact_final_convergence_memos(
        {
            "findings": unresolved,
            "resolved_findings": resolved,
        }
    )
    rebound = rebind_exact_final_convergence_memos(
        repaired_srt,
        memos,
    )
    repeated = [
        {
            "cue_index": 1,
            "base_text_sha256": _sha(cue_1_final),
            "proposed_full_cue": cue_1_current,
            "suspect": cue_1_final,
            "suggestion": cue_1_current,
        },
        {
            "cue_index": 37,
            "base_text_sha256": _sha(cue_37_final),
            "proposed_full_cue": cue_37_current,
            "suspect": cue_37_final,
            "suggestion": cue_37_current,
        },
    ]
    pending, locked = resolve_findings_from_exact_final_convergence_memos(
        repaired_srt,
        repeated,
        authority_audit={
            "exact_final_cpa_convergence_memos": rebound
        },
    )
    assert pending == []
    assert len(locked) == 2
    assert all(
        row["resolution"]
        == "CPA_EXACT_FINAL_CYCLE_MEMO_LOCKED_FINAL_TEXT"
        for row in locked
    )


def test_814_cycle_reopens_only_for_new_candidate_or_new_evidence() -> None:
    current = "现在落到"
    final = "谢谢"
    srt = _srt_814(current, "先不说你数学了好吧")
    finding = _finding(
        cue_index=1,
        start_ms=250,
        end_ms=810,
        current=current,
        proposed=final,
        salt="p",
    )
    unresolved, resolved = converge_reconsidered_exact_final_findings(
        srt,
        [finding],
        authority_audit=_authority_814(),
        judge_llm_call=lambda _prompt: json.dumps(
            {
                "choice_id": "TEXT_" + _sha(final),
                "reason": "声学证据支持答谢词。",
            },
            ensure_ascii=False,
        ),
    )
    assert resolved == []
    repaired_srt, _repairs = _apply_exact_final_cpa_repairs(
        srt,
        {"findings": unresolved},
    )
    memos = rebind_exact_final_convergence_memos(
        repaired_srt,
        collect_exact_final_convergence_memos(
            {"findings": unresolved}
        ),
    )
    new_evidence = {
        "cue_index": 1,
        "base_text_sha256": _sha(final),
        "proposed_full_cue": current,
        "candidate_provenance": {
            "kind": "structured_chat_bound",
            "source_sha256": "f" * 64,
        },
    }
    pending, locked = resolve_findings_from_exact_final_convergence_memos(
        repaired_srt,
        [new_evidence],
        authority_audit={
            "exact_final_cpa_convergence_memos": memos
        },
    )
    assert pending == [new_evidence]
    assert locked == []


def test_cycle_cpa_invalid_receipt_is_typed_fail_closed() -> None:
    current = "三二"
    proposed = "方案二"
    finding = _reconsidered_finding(current, proposed)
    unresolved, resolved = converge_reconsidered_exact_final_findings(
        _srt(current),
        [finding],
        authority_audit=_authority(proposed, current),
        judge_llm_call=lambda _prompt: '{"choice_id":"NOT_IN_SET"}',
    )

    assert resolved == []
    assert len(unresolved) == 1
    receipt = unresolved[0]["exact_final_cpa_cycle_adjudication"]
    assert receipt["status"] == "BLOCK"
    assert receipt["reason_code"] == "CPA_CYCLE_ADJUDICATION_INVALID"
    unchanged, repairs = _apply_exact_final_cpa_repairs(
        _srt(current),
        {"findings": unresolved},
    )
    assert unchanged == _srt(current)
    assert repairs == []
