from __future__ import annotations

import hashlib
import json
from copy import deepcopy

import pytest

from src.autoslice.acoustic_witness_adjudication import build_witness_request
from src.autoslice.exact_final_witness_authority import (
    build_acoustic_witness_binding,
    valid_convergence_mutation_authority,
)
from src.autoslice.exact_final_convergence import (
    _compact_current_evidence,
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


def _json_sha(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


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


def _bound_request(
    current: str,
    proposed: str,
    *,
    cue_index: int,
    start_ms: int,
    end_ms: int,
    salt: str,
) -> dict:
    request = {
        "schema_version": "subtitle-span-acoustic-check-request.v1",
        "evidence_id": _sha("evidence-" + salt),
        "kind": "subtitle_span_acoustic_check",
        "cue_indexes": [cue_index],
        "base_text_sha256": _sha(current),
        "matched_start_ms": start_ms,
        "matched_end_ms": end_ms,
        "context_start_ms": max(0, start_ms - 500),
        "context_end_ms": end_ms + 500,
        "source_media_timeline_offset_ms": 0,
        "matched_audio_text": current,
        "suspect": current,
        "replacement": proposed,
        "current_cue": current,
        "proposed_cue": proposed,
        "context_before": "",
        "context_after": "",
        "candidate_entities": [],
        "repair_class": "phonetic",
        "candidate_provenance": None,
        "orthography_authority": None,
        "evidence_cue_ids": [],
        "reason": "synthetic convergence fixture",
    }
    request["request_sha256"] = _json_sha(request)
    return request


def _reconsidered_finding(
    current: str,
    proposed: str,
    *,
    cue_index: int = 2,
    start_ms: int = 1_100,
    end_ms: int = 2_100,
    salt: str = "default",
) -> dict:
    request = _bound_request(
        current,
        proposed,
        cue_index=cue_index,
        start_ms=start_ms,
        end_ms=end_ms,
        salt=salt,
    )
    witness_request = build_witness_request(request)
    return {
        "cue_index": cue_index,
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
            "request": request,
            "verdict": {
                "schema_version": "subtitle-span-acoustic-witness.v1",
                "witness_protocol": "blind_pinyin",
                "status": "OBSERVED",
                "target_audible": True,
                "heard_pinyin": "san er",
                "uncertain_positions": [],
                "syllable_count": 2,
                "confidence": 0.95,
                "request_sha256": witness_request["request_sha256"],
                "source_media_sha256": _sha("media-" + salt),
                "audio_clip_sha256": _sha("audio-" + salt),
                "prompt_sha256": _sha("witness-prompt-" + salt),
                "response_sha256": _sha("witness-response-" + salt),
            },
            "witness_judge": {
                "candidate_pinyin_similarity": {
                    "current": 0.25,
                    "proposed": 1.0,
                },
                "judge": {
                    "schema_version": "acoustic-witness-adjudication.v1",
                    "status": "JUDGED",
                    "choice": "PROPOSED",
                    "decision_contract": "current-proposed-neither.v1",
                    "choice_set": ["CURRENT", "NEITHER", "PROPOSED"],
                    "candidate_pinyin_similarity": {
                        "current": 0.25,
                        "proposed": 1.0,
                    },
                    "reason": "本轮二元闭集选择 proposed",
                    "prompt_sha256": "1" * 64,
                    "completion_sha256": "2" * 64,
                    "check_request_sha256": request["request_sha256"],
                }
            },
        },
    }


def test_double_candidate_scores_survive_compaction_and_repair_receipt() -> None:
    current = "三二"
    proposed = "方案二"
    finding = _reconsidered_finding(current, proposed)
    adjudication = finding["exact_release_adjudication"]

    compact = _compact_current_evidence(finding, adjudication)
    assert compact["candidate_pinyin_similarity"] == {
        "current": 0.25,
        "proposed": 1.0,
    }
    repaired, receipts = _apply_exact_final_cpa_repairs(
        _srt(current),
        {"findings": [finding]},
    )
    assert proposed in repaired
    assert receipts[0]["candidate_pinyin_similarity"] == {
        "current": 0.25,
        "proposed": 1.0,
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
    assert memo["acoustic_witness_gate"]["status"] == "PASS"
    assert memo["acoustic_witness_gate"]["source"] == "FRESH_OBSERVED"

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


def test_history_convergence_without_observed_witness_is_disclosure_only() -> None:
    current = "三二"
    proposed = "方案二"
    finding = _reconsidered_finding(current, proposed)
    finding["exact_release_adjudication"].pop("verdict")

    unresolved, resolved = converge_reconsidered_exact_final_findings(
        _srt(current),
        [finding],
        authority_audit=_authority("旧文本", current),
        judge_llm_call=lambda _prompt: json.dumps(
            {"choice": "PROPOSED", "reason": "history alone prefers proposed"},
            ensure_ascii=False,
        ),
    )

    assert resolved == []
    assert len(unresolved) == 1
    row = unresolved[0]
    adjudication = row["exact_release_adjudication"]
    assert row["repair_class"] == "disclosure_only"
    assert adjudication["repaired"] is False
    assert adjudication["reason_code"] == (
        "HISTORY_CONVERGENCE_ACOUSTIC_WITNESS_REQUIRED"
    )
    assert adjudication["policy_branch"] == (
        "HISTORY_CONVERGENCE_DOWNGRADED_TO_DISCLOSURE_ONLY"
    )
    assert adjudication["mutation_authority"]["status"] == "NOT_APPLIED"
    unchanged, repairs = _apply_exact_final_cpa_repairs(
        _srt(current),
        {"findings": unresolved},
    )
    assert unchanged == _srt(current)
    assert repairs == []


def test_package_rejects_forged_history_convergence_without_gate() -> None:
    current = "三二"
    proposed = "方案二"
    finding = _reconsidered_finding(current, proposed)
    adjudication = finding["exact_release_adjudication"]
    adjudication["policy_branch"] = "CPA_HISTORY_CONVERGENCE_APPLY_PROPOSED"

    unchanged, repairs = _apply_exact_final_cpa_repairs(
        _srt(current),
        {"findings": [finding]},
    )

    assert unchanged == _srt(current)
    assert repairs == []


def test_package_rejects_convergence_relabelled_as_ordinary_mutation() -> None:
    current = "三二"
    proposed = "方案二"
    unresolved, resolved = converge_reconsidered_exact_final_findings(
        _srt(current),
        [_reconsidered_finding(current, proposed)],
        authority_audit=_authority("旧文本", current),
        judge_llm_call=lambda _prompt: json.dumps(
            {"choice": "PROPOSED", "reason": "fresh blind evidence wins"}
        ),
    )
    assert resolved == []
    assert len(unresolved) == 1
    forged = deepcopy(unresolved[0])
    adjudication = forged["exact_release_adjudication"]
    assert "exact_final_cpa_convergence_memo" in adjudication
    adjudication.pop("history_convergence_acoustic_witness")
    adjudication["policy_branch"] = "WITNESS_JUDGE_APPLY_PROPOSED"
    adjudication["mutation_authority"]["basis"] = (
        "CPA_ACOUSTIC_PRONUNCIATION_DISAMBIGUATION"
    )

    unchanged, repairs = _apply_exact_final_cpa_repairs(
        _srt(current),
        {"findings": [forged]},
    )

    assert unchanged == _srt(current)
    assert repairs == []


def test_forged_blind_witness_cannot_authorize_history_convergence() -> None:
    current = "三二"
    proposed = "方案二"
    finding = _reconsidered_finding(current, proposed)
    adjudication = finding["exact_release_adjudication"]
    witness = adjudication["verdict"]
    for key in (
        "heard_pinyin",
        "uncertain_positions",
        "syllable_count",
        "confidence",
    ):
        witness.pop(key)
    witness["proposed_cue"] = proposed
    adjudication["witness_judge"]["judge"]["schema_version"] = "fake.v1"

    unresolved, resolved = converge_reconsidered_exact_final_findings(
        _srt(current),
        [finding],
        authority_audit=_authority("旧文本", current),
        judge_llm_call=lambda _prompt: json.dumps(
            {"choice": "PROPOSED", "reason": "forged evidence says so"}
        ),
    )

    assert resolved == []
    assert len(unresolved) == 1
    gated = unresolved[0]["exact_release_adjudication"]
    assert gated["policy_branch"] == (
        "HISTORY_CONVERGENCE_DOWNGRADED_TO_DISCLOSURE_ONLY"
    )
    assert gated["reason_code"] == (
        "HISTORY_CONVERGENCE_ACOUSTIC_WITNESS_REQUIRED"
    )
    unchanged, repairs = _apply_exact_final_cpa_repairs(
        _srt(current), {"findings": unresolved}
    )
    assert unchanged == _srt(current)
    assert repairs == []


@pytest.mark.parametrize("hidden_text", ["方案二", "﨑", "𠀀"])
def test_candidate_text_hidden_in_witness_reason_fails_closed(
    hidden_text: str,
) -> None:
    current = "三二"
    proposed = "方案二"
    finding = _reconsidered_finding(current, proposed)
    finding["exact_release_adjudication"]["verdict"]["reason"] = hidden_text

    unresolved, resolved = converge_reconsidered_exact_final_findings(
        _srt(current),
        [finding],
        authority_audit=_authority("旧文本", current),
        judge_llm_call=lambda _prompt: json.dumps(
            {"choice": "PROPOSED", "reason": "candidate leak is forbidden"}
        ),
    )

    assert resolved == []
    adjudication = unresolved[0]["exact_release_adjudication"]
    assert adjudication["policy_branch"] == (
        "HISTORY_CONVERGENCE_DOWNGRADED_TO_DISCLOSURE_ONLY"
    )
    unchanged, repairs = _apply_exact_final_cpa_repairs(
        _srt(current), {"findings": unresolved}
    )
    assert unchanged == _srt(current)
    assert repairs == []


def test_malformed_convergence_types_fail_closed_without_exception() -> None:
    current = "三二"
    proposed = "方案二"
    unresolved, _resolved = converge_reconsidered_exact_final_findings(
        _srt(current),
        [_reconsidered_finding(current, proposed)],
        authority_audit=_authority("旧文本", current),
        judge_llm_call=lambda _prompt: json.dumps(
            {"choice": "PROPOSED", "reason": "valid fixture"}
        ),
    )
    adjudication = deepcopy(unresolved[0]["exact_release_adjudication"])
    adjudication["policy_branch"] = []
    adjudication["mutation_authority"]["basis"] = []
    adjudication["witness_judge"]["witness_status"] = []

    assert valid_convergence_mutation_authority(
        adjudication,
        proposed=proposed,
        window=(1_100, 2_100),
    ) is False

    source = _reconsidered_finding(current, proposed)[
        "exact_release_adjudication"
    ]
    bad_judge = deepcopy(source["witness_judge"]["judge"])
    bad_judge["choice_set"] = [{"unhashable": True}]
    assert build_acoustic_witness_binding(
        check_request=source["request"],
        witness=source["verdict"],
        judge=bad_judge,
    ) is None


def test_history_convergence_reuses_proposed_bound_observed_witness() -> None:
    current = "三二"
    proposed = "方案二"
    authority = _authority(proposed, current)
    first = authority["exact_final_cpa_self_heal"]["passes"][0]["repairs"][0]
    prior = deepcopy(first)
    historical_request = _bound_request(
        "旧方案",
        proposed,
        cue_index=2,
        start_ms=1_100,
        end_ms=2_100,
        salt="historical",
    )
    historical_witness_request = build_witness_request(historical_request)
    historical_witness = {
        "schema_version": "subtitle-span-acoustic-witness.v1",
        "witness_protocol": "blind_pinyin",
        "status": "OBSERVED",
        "target_audible": True,
        "heard_pinyin": "fang an er",
        "uncertain_positions": [],
        "syllable_count": 3,
        "confidence": 0.95,
        "request_sha256": historical_witness_request["request_sha256"],
        "source_media_sha256": _sha("historical-media"),
        "audio_clip_sha256": _sha("historical-audio"),
        "prompt_sha256": _sha("historical-witness-prompt"),
        "response_sha256": _sha("historical-witness-response"),
    }
    historical_judge = {
        "schema_version": "acoustic-witness-adjudication.v1",
        "status": "JUDGED",
        "choice": "PROPOSED",
        "decision_contract": "current-proposed-neither.v1",
        "choice_set": ["CURRENT", "NEITHER", "PROPOSED"],
        "reason": "historical blind witness supports proposed",
        "candidate_pinyin_similarity": {
            "current": 0.25,
            "proposed": 1.0,
        },
        "check_request_sha256": historical_request["request_sha256"],
        "prompt_sha256": _sha("historical-prompt"),
        "completion_sha256": _sha("historical-completion"),
    }
    binding = build_acoustic_witness_binding(
        check_request=historical_request,
        witness=historical_witness,
        judge=historical_judge,
    )
    assert binding is not None
    prior.update(
        before="旧方案",
        after=proposed,
        before_sha256="sha256:" + _sha("旧方案"),
        after_sha256="sha256:" + _sha(proposed),
        request_sha256="sha256:" + historical_request["request_sha256"],
        acoustic_witness=historical_witness,
        acoustic_witness_binding=binding,
    )
    authority["exact_final_cpa_self_heal"]["passes"][0]["repairs"] = [
        prior,
        first,
    ]
    finding = _reconsidered_finding(current, proposed)
    finding["exact_release_adjudication"].pop("verdict")

    unresolved, resolved = converge_reconsidered_exact_final_findings(
        _srt(current),
        [finding],
        authority_audit=authority,
        judge_llm_call=lambda _prompt: json.dumps(
            {
                "choice_id": "TEXT_" + _sha(proposed),
                "reason": "bound witness can be reused",
            },
            ensure_ascii=False,
        ),
    )

    assert resolved == []
    gate = unresolved[0]["exact_release_adjudication"][
        "history_convergence_acoustic_witness"
    ]
    assert gate["status"] == "PASS"
    assert gate["source"] == "HISTORY_OBSERVED_REUSE"
    assert gate["witness_protocol"] == "blind_pinyin"
    repaired, receipts = _apply_exact_final_cpa_repairs(
        _srt(current),
        {"findings": unresolved},
    )
    assert proposed in repaired
    assert receipts[0]["acoustic_witness_binding"] == binding

    malformed = deepcopy(unresolved[0])
    malformed_adjudication = malformed["exact_release_adjudication"]
    malformed_gate = deepcopy(
        malformed_adjudication["history_convergence_acoustic_witness"]
    )
    malformed_gate["source"] = []
    malformed_gate.pop("gate_sha256")
    malformed_gate["gate_sha256"] = "sha256:" + _json_sha(malformed_gate)
    malformed_adjudication["history_convergence_acoustic_witness"] = deepcopy(
        malformed_gate
    )
    malformed_adjudication["exact_final_cpa_cycle_memo"][
        "acoustic_witness_gate"
    ] = deepcopy(malformed_gate)
    unchanged, malformed_receipts = _apply_exact_final_cpa_repairs(
        _srt(current),
        {"findings": [malformed]},
    )
    assert unchanged == _srt(current)
    assert malformed_receipts == []


def test_digest_only_legacy_history_witness_is_not_reused() -> None:
    current = "三二"
    proposed = "方案二"
    authority = _authority(proposed, current)
    first = authority["exact_final_cpa_self_heal"]["passes"][0]["repairs"][0]
    prior = deepcopy(first)
    prior.update(
        before="旧方案",
        after=proposed,
        before_sha256="sha256:" + _sha("旧方案"),
        after_sha256="sha256:" + _sha(proposed),
        acoustic_witness={
            "schema_version": "subtitle-span-acoustic-witness.v1",
            "status": "OBSERVED",
            "target_audible": True,
            "request_sha256": "8" * 64,
        },
        acoustic_witness_binding={
            "schema_version": "subtitle-repair-acoustic-witness-binding.v1",
            "status": "PASS",
            "check_request_sha256": "sha256:" + "9" * 64,
            "witness_request_sha256": "sha256:" + "8" * 64,
            "proposed_text_sha256": "sha256:" + _sha(proposed),
            "matched_start_ms": 1_100,
            "matched_end_ms": 2_100,
        },
    )
    authority["exact_final_cpa_self_heal"]["passes"][0]["repairs"] = [
        prior,
        first,
    ]
    finding = _reconsidered_finding(current, proposed)
    finding["exact_release_adjudication"].pop("verdict")

    unresolved, resolved = converge_reconsidered_exact_final_findings(
        _srt(current),
        [finding],
        authority_audit=authority,
        judge_llm_call=lambda _prompt: json.dumps(
            {
                "choice_id": "TEXT_" + _sha(proposed),
                "reason": "legacy digest-only evidence is insufficient",
            },
            ensure_ascii=False,
        ),
    )

    assert resolved == []
    adjudication = unresolved[0]["exact_release_adjudication"]
    assert adjudication["repaired"] is False
    assert adjudication["reason_code"] == (
        "HISTORY_CONVERGENCE_ACOUSTIC_WITNESS_REQUIRED"
    )
    assert adjudication["history_convergence_acoustic_witness"]["status"] == (
        "BLOCK"
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
    row = _reconsidered_finding(
        current,
        proposed,
        cue_index=cue_index,
        start_ms=start_ms,
        end_ms=end_ms,
        salt=salt,
    )
    adjudication = row["exact_release_adjudication"]
    adjudication["verdict"].update(
        audio_clip_sha256=_sha("audio-" + salt),
        heard_pinyin="he cheng yin",
        syllable_count=3,
    )
    return row


def _legal_drop_finding(
    *, current: str, start_ms: int, end_ms: int
) -> dict:
    base_sha = _sha(current)
    request_sha = "f" * 64
    witness_sha = "a" * 64
    prompt_sha = "b" * 64
    completion_sha = "c" * 64
    return {
        "cue_index": 1,
        "base_text_sha256": base_sha,
        "proposed_full_cue": "",
        "suggestion": "",
        "repair_class": "acoustic_drop_cue",
        "exact_release_adjudication": {
            "schema_version": "subtitle-span-adjudication.v1",
            "status": "OBSERVED",
            "decision_authority": "CPA_JUDGE",
            "witness_authority": "EVIDENCE_ONLY",
            "repaired": True,
            "timing_immutable": True,
            "policy_branch": "CPA_JUDGE_APPLY_INAUDIBLE_DROP_CUE",
            "mutation_authority": {
                "schema_version": "subtitle-correction-mutation-authority.v1",
                "status": "PASS",
                "basis": "CPA_EXPLICIT_INAUDIBLE_DROP",
            },
            "request": {
                "schema_version": "subtitle-span-acoustic-check-request.v1",
                "request_sha256": request_sha,
                "base_text_sha256": base_sha,
                "current_cue": current,
                "proposed_cue": "",
                "repair_class": "acoustic_drop_cue",
                "matched_start_ms": start_ms,
                "matched_end_ms": end_ms,
            },
            "verdict": {
                "schema_version": "subtitle-span-acoustic-witness.v1",
                "status": "OBSERVED",
                "request_sha256": witness_sha,
                "target_audible": False,
            },
            "witness_judge": {
                "witness_status": "OBSERVED",
                "decision_authority": "CPA_JUDGE",
                "witness_authority": "EVIDENCE_ONLY",
                "selected_action": "DROP_CUE",
                "selected_repair_class": "acoustic_drop_cue",
                "selected_target_cue": "",
                "judge": {
                    "schema_version": "acoustic-witness-adjudication.v1",
                    "status": "JUDGED",
                    "choice": "DROP",
                    "prompt_sha256": prompt_sha,
                    "completion_sha256": completion_sha,
                    "check_request_sha256": request_sha,
                    "decision_contract": "inaudible-current-proposed-drop.v1",
                    "choice_set": ["CURRENT", "DROP", "PROPOSED"],
                },
            },
            "drop_authority": {
                "schema_version": "subtitle-cpa-inaudible-drop-authority.v1",
                "status": "PASS",
                "decision_authority": "CPA_JUDGE",
                "choice": "DROP",
                "decision_contract": "inaudible-current-proposed-drop.v1",
                "original_request_sha256": "sha256:" + request_sha,
                "effective_drop_request_sha256": "sha256:" + request_sha,
                "original_witness_request_sha256": "sha256:" + witness_sha,
                "effective_witness_request_sha256": "sha256:" + witness_sha,
                "judge_prompt_sha256": "sha256:" + prompt_sha,
                "judge_completion_sha256": "sha256:" + completion_sha,
                "target_audible": False,
                "timing_immutable": True,
            },
        },
    }


def test_cycle_selected_legal_drop_remains_applicable() -> None:
    current = "咳咳"
    start_ms, end_ms = 250, 810
    srt = _srt_814(current, "下一句")
    drop = _legal_drop_finding(
        current=current, start_ms=start_ms, end_ms=end_ms
    )
    competing = _finding(
        cue_index=1,
        start_ms=start_ms,
        end_ms=end_ms,
        current=current,
        proposed="咳一下",
        salt="cycle-drop-competitor",
    )

    unresolved, resolved = converge_reconsidered_exact_final_findings(
        srt,
        [drop, competing],
        authority_audit={},
        judge_llm_call=lambda _prompt: json.dumps(
            {"choice_id": "DROP", "reason": "target interval is silent"}
        ),
    )

    assert len(unresolved) == 1
    assert len(resolved) == 1
    selected = unresolved[0]
    adjudication = selected["exact_release_adjudication"]
    assert selected["proposed_full_cue"] == ""
    assert adjudication["policy_branch"] == (
        "CPA_JUDGE_APPLY_INAUDIBLE_DROP_CUE"
    )
    assert adjudication["exact_final_cpa_cycle_memo"]["final_text"] == ""
    repaired, receipts = _apply_exact_final_cpa_repairs(
        srt, {"findings": unresolved}
    )
    assert current not in repaired
    assert len(receipts) == 1
    assert receipts[0]["action"] == "DROP_CUE"


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


def test_same_pass_competing_proposals_are_one_order_invariant_closed_set() -> None:
    current = "现在落到"
    selected = "谢谢"
    competitor = "谢谢萝萝的"
    srt = _srt_814(current, "先不说你数学了好吧")

    def run(rows: list[dict]) -> tuple[list[dict], list[dict], list[str]]:
        prompts: list[str] = []

        def judge(prompt: str) -> str:
            prompts.append(prompt)
            return json.dumps(
                {
                    "choice_id": "TEXT_" + _sha(selected),
                    "reason": "同轮互斥候选中，完整语境支持答谢词。",
                },
                ensure_ascii=False,
            )

        unresolved, resolved = converge_reconsidered_exact_final_findings(
            srt,
            rows,
            authority_audit={},
            judge_llm_call=judge,
        )
        return unresolved, resolved, prompts

    finding_a = _finding(
        cue_index=1,
        start_ms=250,
        end_ms=810,
        current=current,
        proposed=selected,
        salt="same-pass-a",
    )
    finding_b = _finding(
        cue_index=1,
        start_ms=250,
        end_ms=810,
        current=current,
        proposed=competitor,
        salt="same-pass-b",
    )

    forward, forward_resolved, forward_prompts = run(
        [deepcopy(finding_a), deepcopy(finding_b)]
    )
    reverse, reverse_resolved, reverse_prompts = run(
        [deepcopy(finding_b), deepcopy(finding_a)]
    )

    assert len(forward_prompts) == len(reverse_prompts) == 1
    assert forward_prompts == reverse_prompts
    assert len(forward) == len(reverse) == 1
    assert len(forward_resolved) == len(reverse_resolved) == 1
    assert forward[0]["proposed_full_cue"] == selected
    assert reverse[0]["proposed_full_cue"] == selected
    forward_memo = forward[0]["exact_release_adjudication"][
        "exact_final_cpa_cycle_memo"
    ]
    reverse_memo = reverse[0]["exact_release_adjudication"][
        "exact_final_cpa_cycle_memo"
    ]
    assert forward_memo == reverse_memo
    assert forward_memo["history_receipts"] == []


_TYPED_WITNESS_CONTENT = {
    "target_audible": True,
    "heard_pinyin": "xie xie",
    "syllable_count": 2,
    "confidence": 0.92,
    "model": "agy-audio-v1",
    "completion_sha256": "sha256:" + _sha("heard-xie-xie"),
}


def _cycle_memo_with_typed_witness() -> tuple[str, str, str, list[dict]]:
    current = "现在落到"
    selected = "谢谢"
    srt = _srt_814(current, "先不说你数学了好吧")
    original = _finding(
        cue_index=1,
        start_ms=250,
        end_ms=810,
        current=current,
        proposed=selected,
        salt="same-audio",
    )
    original["exact_release_adjudication"]["verdict"].update(
        _TYPED_WITNESS_CONTENT
    )
    unresolved, _resolved = converge_reconsidered_exact_final_findings(
        srt,
        [original],
        authority_audit=_authority_814(),
        judge_llm_call=lambda _prompt: json.dumps(
            {
                "choice_id": "TEXT_" + _sha(selected),
                "reason": "当前声学内容支持答谢词。",
            },
            ensure_ascii=False,
        ),
    )
    repaired_srt, _repairs = _apply_exact_final_cpa_repairs(
        srt,
        {"findings": unresolved},
    )
    memos = rebind_exact_final_convergence_memos(
        repaired_srt,
        collect_exact_final_convergence_memos({"findings": unresolved}),
    )
    return current, selected, repaired_srt, memos


def _typed_witness_replay(
    *,
    current: str,
    selected: str,
    changes: dict[str, object] | None = None,
) -> dict:
    replay = _finding(
        cue_index=1,
        start_ms=250,
        end_ms=810,
        current=selected,
        proposed=current,
        salt="same-audio",
    )
    replay["exact_release_adjudication"]["verdict"].update(
        _TYPED_WITNESS_CONTENT
    )
    replay["exact_release_adjudication"]["verdict"].update(
        changes or {}
    )
    return replay


def test_cycle_memo_locks_identical_typed_witness_content() -> None:
    current, selected, repaired_srt, memos = (
        _cycle_memo_with_typed_witness()
    )
    identical = _typed_witness_replay(
        current=current,
        selected=selected,
    )
    pending, locked = resolve_findings_from_exact_final_convergence_memos(
        repaired_srt,
        [identical],
        authority_audit={"exact_final_cpa_convergence_memos": memos},
    )

    assert pending == []
    assert len(locked) == 1
    assert locked[0]["resolution"] == (
        "CPA_EXACT_FINAL_CYCLE_MEMO_LOCKED_FINAL_TEXT"
    )


@pytest.mark.parametrize(
    ("field", "changed_value"),
    [
        ("heard_pinyin", "luo luo da"),
        ("target_audible", False),
        ("confidence", 0.41),
        (
            "completion_sha256",
            "sha256:" + _sha("heard-luo-luo-da"),
        ),
    ],
)
def test_cycle_memo_reopens_when_witness_material_changes(
    field: str,
    changed_value: object,
) -> None:
    current, selected, repaired_srt, memos = (
        _cycle_memo_with_typed_witness()
    )

    changed = _typed_witness_replay(
        current=current,
        selected=selected,
        changes={field: changed_value},
    )
    pending, locked = resolve_findings_from_exact_final_convergence_memos(
        repaired_srt,
        [changed],
        authority_audit={"exact_final_cpa_convergence_memos": memos},
    )

    assert pending == [changed]
    assert locked == []
