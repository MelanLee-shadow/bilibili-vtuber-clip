from __future__ import annotations

import hashlib
import json
from copy import deepcopy

import pytest

from src.autoslice.acoustic_witness_adjudication import build_witness_request
from src.autoslice.exact_final_witness_authority import (
    build_acoustic_witness_binding,
    history_audio_recovery_from_adjudication,
    valid_acoustic_witness_binding,
    valid_convergence_mutation_authority,
)
from src.autoslice.exact_final_convergence import (
    _compact_current_evidence,
    _cycle_prompt_receipts,
    collect_exact_final_convergence_memos,
    converge_reconsidered_exact_final_findings,
    exact_final_convergence_memo_identity,
    rebind_exact_final_convergence_memos,
    resolve_findings_from_exact_final_convergence_memos,
    valid_exact_final_convergence_memo_binding,
)
from src.autoslice.producer_package_finalization import (
    _apply_exact_final_cpa_repairs,
)
from src.autoslice.exact_final_cpa_history import retained_runs


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
    assert "candidate_blind_transcript_observations" not in compact
    repaired, receipts = _apply_exact_final_cpa_repairs(
        _srt(current),
        {"findings": [finding]},
    )
    assert proposed in repaired
    assert receipts[0]["candidate_pinyin_similarity"] == {
        "current": 0.25,
        "proposed": 1.0,
    }


def test_history_judge_receives_bound_context_but_not_raw_finding_observations() -> None:
    finding = _reconsidered_finding("三二", "方案二")
    adjudication = finding["exact_release_adjudication"]
    request = adjudication["request"]
    request["context_only_range_acoustic_observations"] = [
        {"evidence_scope": "CONTEXT_ONLY", "exact_transcript": "完整句仍有冲突"},
    ]
    request["require_complete_utterance_support"] = True
    finding["_prior_range_acoustic_observations"] = [{"unvalidated": "不要消费"}]
    compact = _compact_current_evidence(finding, adjudication)
    assert compact["bound_context_observations"] == {
        "context_only_range_acoustic_observations": request["context_only_range_acoustic_observations"],
        "require_complete_utterance_support": True,
        "source_media_timeline_offset_ms": request["source_media_timeline_offset_ms"],
    }
    assert "unvalidated" not in json.dumps(compact, ensure_ascii=False)


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


def test_successor_review_retains_completed_run_for_cycle_judge() -> None:
    current, proposed = "三二", "方案二"
    previous = _authority(proposed, current)["exact_final_cpa_self_heal"]
    previous.update(status="PASS", final_srt_sha256="sha256:" + _sha(_srt(current)))
    authority = {"exact_final_cpa_self_heal": {
        "schema_version": "exact-final-cpa-self-heal-audit.v1",
        "status": "REVIEW_PENDING", "passes": [],
        "retained_previous_runs": retained_runs(previous),
    }}
    prompts = []

    def judge(prompt):
        prompts.append(prompt)
        return json.dumps({"choice": "CURRENT", "reason": "Bound prior reversal and current evidence agree."})

    unresolved, resolved = converge_reconsidered_exact_final_findings(
        _srt(current), [_reconsidered_finding(current, proposed)],
        authority_audit=authority, judge_llm_call=judge,
    )
    assert unresolved == []
    assert len(prompts) == len(resolved) == 1
    assert resolved[0]["exact_release_adjudication"]["policy_branch"] == "CPA_EXACT_FINAL_CYCLE_KEEP_CURRENT"
    assert authority["exact_final_cpa_self_heal"]["passes"] == []


def test_successor_cycle_rejects_tampered_retained_receipt_before_judge() -> None:
    previous = _authority("方案二", "三二")["exact_final_cpa_self_heal"]
    previous.update(status="PASS", final_srt_sha256="sha256:" + _sha(_srt("三二")))
    history = retained_runs(previous)
    history[0]["audit"]["passes"][0]["repairs"][0]["after"] = "tampered"
    authority = {"exact_final_cpa_self_heal": {
        "schema_version": "exact-final-cpa-self-heal-audit.v1",
        "status": "REVIEW_PENDING", "passes": [], "retained_previous_runs": history,
    }}
    with pytest.raises(ValueError, match="EXACT_FINAL_CPA_HISTORY_INVALID"):
        converge_reconsidered_exact_final_findings(
            _srt("三二"), [_reconsidered_finding("三二", "方案二")],
            authority_audit=authority,
            judge_llm_call=lambda prompt: pytest.fail("Tampered history must not reach CPA"),
        )


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


@pytest.mark.parametrize("pending_flag", [
    "needs_audio", "needs_audio_requested", "needs_audio_request_unresolved",
])
def test_matching_historical_memo_cannot_close_pending_acoustic_review(
    pending_flag: str,
) -> None:
    current, proposed = "三二", "方案二"
    unresolved, _ = converge_reconsidered_exact_final_findings(
        _srt(current), [_reconsidered_finding(current, proposed)],
        authority_audit=_authority(proposed, current),
        judge_llm_call=lambda _: json.dumps({
            "choice": "PROPOSED", "reason": "完整历史和语境支持方案二。",
        }, ensure_ascii=False),
    )
    memo = deepcopy(collect_exact_final_convergence_memos({"findings": unresolved})[0])
    gate = memo["acoustic_witness_gate"]
    binding = gate["acoustic_witness_binding"]
    judge = binding["judge"]
    judge.pop("needs_audio_requested", None)
    judge.pop("needs_audio_request_unresolved", None)
    judge[pending_flag] = True
    # Preserve every content hash: the real regression was a structurally
    # valid old memo with an unfinished ear, rather than corrupted storage.
    binding.pop("binding_sha256")
    binding["binding_sha256"] = "sha256:" + _json_sha(binding)
    gate.pop("gate_sha256")
    gate["gate_sha256"] = "sha256:" + _json_sha(gate)
    assert exact_final_convergence_memo_identity(memo) is not None
    finding = {
        "cue_index": 2, "base_text_sha256": _sha(proposed),
        "proposed_full_cue": current, "suspect": proposed, "suggestion": current,
    }
    before = deepcopy(memo)
    pending, resolved = resolve_findings_from_exact_final_convergence_memos(
        _srt(proposed), [finding],
        authority_audit={"exact_final_cpa_convergence_memos": [memo]},
    )
    assert pending == [finding]
    assert resolved == []
    assert memo == before


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


@pytest.mark.parametrize("field", ["needs_audio_requested", "needs_audio_request_unresolved", "needs_audio"])
def test_explicit_more_audio_request_cannot_be_rebound_as_acoustic_authority(field) -> None:
    finding = _reconsidered_finding("三二", "方案二")
    adjudication = finding["exact_release_adjudication"]
    judge = adjudication["witness_judge"]["judge"]
    judge[field] = True
    assert build_acoustic_witness_binding(
        check_request=adjudication["request"],
        witness=adjudication["verdict"], judge=judge,
    ) is None


@pytest.mark.parametrize("choice", ["CURRENT", "PROPOSED", "UNRESOLVED"])
def test_history_convergence_more_audio_request_does_not_apply_or_resolve(choice) -> None:
    current, proposed = "三二", "方案二"
    unresolved, resolved = converge_reconsidered_exact_final_findings(
        _srt(current), [_reconsidered_finding(current, proposed)],
        authority_audit=_authority("旧文本", current),
        judge_llm_call=lambda _: json.dumps({
            "choice": choice, "needs_audio": True, "reason": "还需完整语义块补听",
        }),
    )
    assert resolved == []
    unchanged, repairs = _apply_exact_final_cpa_repairs(
        _srt(current), {"findings": unresolved},
    )
    assert unchanged == _srt(current)
    assert repairs == []


def test_history_more_audio_preserves_cpa_reason_and_builds_typed_recovery_gate() -> None:
    current, proposed = "三二", "方案二"
    unresolved, resolved = converge_reconsidered_exact_final_findings(
        _srt(current), [_reconsidered_finding(current, proposed)],
        authority_audit=_authority("旧文本", current),
        judge_llm_call=lambda _prompt: json.dumps({
            "choice": "UNRESOLVED",
            "needs_audio": True,
            "reason": "完整句仍需补听",
        }, ensure_ascii=False),
    )
    assert resolved == [] and len(unresolved) == 1
    adjudication = unresolved[0]["exact_release_adjudication"]
    gate = adjudication["history_convergence_acoustic_witness"]
    assert gate["reason_code"] == "CPA_REQUESTS_MORE_AUDIO"
    recovery = history_audio_recovery_from_adjudication(
        adjudication,
        request=adjudication["request"],
    )
    assert recovery is not None
    assert recovery["status"] == "BLOCK"
    assert recovery["reason_code"] == (
        "HISTORY_CONVERGENCE_ACOUSTIC_WITNESS_REQUIRED"
    )
    assert recovery["cpa_reason_code"] == "CPA_REQUESTS_MORE_AUDIO"
    assert recovery["gate"]["reason_code"] == "CPA_REQUESTS_MORE_AUDIO"


def test_history_convergence_can_remain_unresolved_without_forcing_an_audio_retry() -> None:
    current, proposed = "三二", "方案二"
    prompts = []

    def judge(prompt):
        prompts.append(prompt)
        return json.dumps({"choice": "UNRESOLVED", "needs_audio": False,
                           "reason": "现有宽窗与短窗冲突，先核覆盖范围"})

    unresolved, resolved = converge_reconsidered_exact_final_findings(
        _srt(current), [_reconsidered_finding(current, proposed)],
        authority_audit=_authority("旧文本", current), judge_llm_call=judge,
    )
    assert len(prompts) == 1
    assert "UNRESOLVED" in prompts[0] and "needs_audio" in prompts[0]
    assert resolved == []
    gate = unresolved[0]["exact_release_adjudication"]["history_convergence_acoustic_witness"]
    assert gate["reason_code"] == "CPA_HISTORY_CONVERGENCE_UNRESOLVED"
    assert gate["needs_audio"] is False
    unchanged, repairs = _apply_exact_final_cpa_repairs(_srt(current), {"findings": unresolved})
    assert unchanged == _srt(current) and repairs == []


def _actual_cue12_applied_successor_case() -> tuple[dict, dict, dict]:
    """Fixture the real 428c cue-12 receipt without reading provider bytes.

    The production receipt is the V1 authority
    (SHA 428c59f6455ce77143c46af6878f20e97e7540a20c712b94c842447c0defbd52):
    its outer candidate is OBSERVED/CPA_JUDGE_APPLY_TRANSCRIPT_CANDIDATE with
    mutation PASS, while the retained candidate-less gate remains explicitly
    UNRESOLVED/needs_audio.  The small SRT keeps this test deterministic while
    preserving the exact current/proposed/window/offset contract shape.
    """
    from src.autoslice.final_review_auditor import build_context_adjudication_request

    def stamp(milliseconds: int) -> str:
        seconds, millis = divmod(milliseconds, 1000)
        minutes, seconds = divmod(seconds, 60)
        hours, minutes = divmod(minutes, 60)
        return f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"

    cues = [
        (index, (index - 1) * 2000, (index - 1) * 2000 + 1000, f"上下文{index}")
        for index in range(1, 12)
    ]
    cues.extend([
        (12, 30_530, 31_530, "你知道吗"),
        (13, 31_530, 33_000, "后面收束"),
    ])
    srt = "\n".join(
        f"{index}\n{stamp(start)} --> {stamp(end)}\n{text}\n"
        for index, start, end, text in cues
    )
    finding = {
        "cue_index": 12,
        "suspect": "你知道吗",
        "suggestion": "No.",
        "proposed_full_cue": "No.",
        "repair_class": "entity",
        "candidate_provenance": {
            "kind": "source_transcript_candidate",
            "mutation_authorized": False,
        },
    }
    request = build_context_adjudication_request(
        srt,
        finding,
        source_media_timeline_offset_ms=9750,
    )
    request_sha = request["request_sha256"]
    gate = {
        "schema_version": "subtitle-missing-proposal-cpa-convergence.v1",
        "status": "UNRESOLVED",
        "decision": "UNRESOLVED",
        "needs_audio": True,
        "decision_authority": "CPA_JUDGE",
        "mutation_authorized": False,
        "timing_immutable": True,
        "reason_code": "CPA_CONVERGENCE_UNRESOLVED",
        "reason": "旧窄窗与完整句冲突，仍需完整原音",
        "cue_index": 12,
        "current_cue_sha256": "sha256:" + _sha(request["current_cue"]),
        "final_srt_sha256": "sha256:" + _sha(request["whole_clip_current_srt"]),
        # These are the actual cue-12 completion/context digests from the
        # retained authority; no provider response is consumed here.
        "prompt_sha256": "sha256:4f2960a30713604132f8ae903f4cf17c4e5322ed35b5b8ef8e195318d1f6eebf",
        "completion_sha256": "sha256:acae7dedb74d1f98964a1f51ac8facf86d71a750efdf753b364b7f31b68620ba",
        "context_sha256": "sha256:b7434a878c08afb8d88fce586d09672df50ecb6fe52a04c12744cddfafbc76dd",
    }
    judge = {
        "schema_version": "acoustic-witness-adjudication.v1",
        "status": "JUDGED",
        "choice": "PROPOSED",
        "decision_contract": "current-proposed-candidate-id-neither.v1",
        "candidate_id": "PROPOSAL",
        "selected_candidate_text": "No.",
        "choice_set": ["CURRENT", "PROPOSED", "NEITHER"],
        "candidate_pinyin_similarity": {"current": None, "proposed": None},
        "check_request_sha256": request_sha,
        "reason": "候选已被记录，但完整原音仍需重新取证",
        "prompt_sha256": "sha256:f8b2a26828f4b963f928f3da881d6791dc4bfbdb9d058ad4b27cf6d0104ad387",
        "completion_sha256": "sha256:e3b4a451307a6fcaba9c94a67aff410b030e9d5efa09fe818b2852b503753dad",
        # This is the actual normalized legacy default.  The ordinary judge
        # validator accepts it; it is not a fresh request to rerun audio.
        "needs_audio": True,
        "needs_audio_requested": False,
    }
    # Keep the fixture's successor on the same complete native witness path as
    # production.  The old candidate-less receipt is only retired after this
    # exact crop, request, and candidate-blind transcript bind successfully.
    from src.autoslice.local_asr_target_evidence import digest, exact_target_evidence

    audio = b"cue-12-native-fixture"
    source_sha = _sha("fixture-source")
    native = exact_target_evidence(
        {
            "provider": "moss",
            "model": "moss-transcribe-diarize-pro",
            "input_audio_sha256": _sha(audio.decode()),
            "response_sha256": _sha("fixture-response"),
            "native_segments": [{"start_ms": 100, "end_ms": 700, "text": "No."}],
            "native_timeline": {
                "has_out_of_order": False,
                "has_overlap": False,
                "one_track_srt_eligible": True,
            },
            "raw_response": {"text": "No.", "segments": []},
        },
        audio=audio,
        source_sha256=source_sha,
        start_ms=40_280,
        end_ms=41_280,
        prefer_provider_text=True,
    )
    native["observation_scope"] = "complete_exact_cue_no_padding"
    native.pop("receipt_sha256")
    native["receipt_sha256"] = digest(native)
    adjudication_witness = {
        "schema_version": "subtitle-span-acoustic-witness.v1",
        "witness_protocol": "candidate_blind_transcript",
        "status": "OBSERVED",
        "target_audible": True,
        "audibility_basis": "nonempty_provider_transcript",
        "candidate_exposure": "none",
        "authority": "EVIDENCE_ONLY",
        "mutation_authorized": False,
        "exact_transcript": native["transcript"],
        "request_sha256": build_witness_request(request)["request_sha256"],
        "source_media_sha256": native["source_media_sha256"],
        "audio_clip_sha256": native["input_audio_sha256"],
        "audio_start_ms": 40_280,
        "audio_end_ms": 41_280,
        "provider": native["provider"],
        "model": native["model"],
        "response_sha256": native["response_sha256"],
        "native_observation": native,
    }
    adjudication = {
        "schema_version": "subtitle-span-adjudication.v1",
        "status": "OBSERVED",
        "repaired": True,
        "timing_immutable": True,
        "decision_authority": "CPA_JUDGE",
        "witness_authority": "EVIDENCE_ONLY",
        "policy_branch": "CPA_JUDGE_APPLY_TRANSCRIPT_CANDIDATE",
        "request": request,
        "verdict": adjudication_witness,
        "witness_judge": {"judge": judge},
        "mutation_authority": {
            "schema_version": "subtitle-correction-mutation-authority.v1",
            "status": "PASS",
            "basis": "CPA_ACOUSTIC_PRONUNCIATION_DISAMBIGUATION",
        },
        "cpa_missing_proposal_convergence": gate,
    }
    return {"exact_release_adjudication": adjudication}, request, srt


def test_applied_successor_supersedes_legacy_missing_gate_for_normal_repair() -> None:
    finding, request, srt_text = _actual_cue12_applied_successor_case()
    adjudication = finding["exact_release_adjudication"]
    assert history_audio_recovery_from_adjudication(adjudication, request=request) is None
    finding.update(
        cue_index=12,
        base_text_sha256=request["base_text_sha256"],
        proposed_full_cue=None,
    )
    repaired_srt, repairs = _apply_exact_final_cpa_repairs(
        srt_text,
        {"findings": [finding]},
    )
    assert "\nNo.\n" in repaired_srt
    assert len(repairs) == 1
    assert repairs[0]["after"] == "No."
    assert adjudication["mutation_authority"]["status"] == "PASS"


@pytest.mark.parametrize(
    "drift", ["current", "srt", "window", "request_identity", "authority"]
)
def test_applied_successor_audio_gate_rejects_binding_drift(drift: str) -> None:
    finding, request, _srt_text = _actual_cue12_applied_successor_case()
    adjudication = finding["exact_release_adjudication"]
    request = deepcopy(request)
    if drift == "current":
        request["current_cue"] = "别的当前句"
        request["base_text_sha256"] = _sha(request["current_cue"])
    elif drift == "srt":
        request["whole_clip_current_srt"] += "\n"
    elif drift == "window":
        request["matched_start_ms"] += 1
    elif drift == "request_identity":
        request["context_after"] = "漂移后的上下文"
    else:
        adjudication["decision_authority"] = "CPA_PROPOSAL_ONLY"
    if drift != "authority":
        payload = dict(request)
        payload.pop("request_sha256", None)
        request["request_sha256"] = _json_sha(payload)
    recovery = history_audio_recovery_from_adjudication(
        adjudication,
        request=request,
    )
    assert recovery is not None
    assert recovery["status"] == "INVALID"
    assert recovery["reason_code"] == "HISTORY_CONVERGENCE_AUDIO_GATE_INVALID"


@pytest.mark.parametrize("drift", ["source", "audio", "response", "geometry"])
def test_applied_successor_audio_gate_rejects_witness_drift(drift: str) -> None:
    finding, request, _srt_text = _actual_cue12_applied_successor_case()
    adjudication = finding["exact_release_adjudication"]
    witness = adjudication["verdict"]
    if drift == "source":
        witness["source_media_sha256"] = _sha("other-source")
    elif drift == "audio":
        witness["audio_clip_sha256"] = _sha("other-audio")
    elif drift == "response":
        witness["response_sha256"] = _sha("other-response")
    else:
        witness["audio_start_ms"] += 1
    recovery = history_audio_recovery_from_adjudication(
        adjudication,
        request=request,
    )
    assert recovery is not None
    assert recovery["status"] == "INVALID"
    assert recovery["reason_code"] == "HISTORY_CONVERGENCE_AUDIO_GATE_INVALID"


@pytest.mark.parametrize("field", ["needs_audio_requested", "needs_audio_request_unresolved"])
def test_explicit_successor_audio_request_does_not_supersede_old_gate(field: str) -> None:
    finding, request, _srt_text = _actual_cue12_applied_successor_case()
    adjudication = finding["exact_release_adjudication"]
    adjudication["witness_judge"]["judge"][field] = True
    recovery = history_audio_recovery_from_adjudication(
        adjudication,
        request=request,
    )
    assert recovery is not None
    assert recovery["status"] == "INVALID"


@pytest.mark.parametrize("closed_field", ["outer", "judge"])
def test_explicit_audio_closed_successor_does_not_reopen_old_gate(closed_field: str) -> None:
    finding, request, _srt_text = _actual_cue12_applied_successor_case()
    adjudication = finding["exact_release_adjudication"]
    if closed_field == "outer":
        adjudication["needs_audio"] = False
    else:
        adjudication["witness_judge"]["judge"]["needs_audio"] = False
    # The retained nested candidate-less gate is still UNRESOLVED/true, but an
    # explicit current terminal closes that historical collection obligation.
    assert history_audio_recovery_from_adjudication(
        adjudication,
        request=request,
    ) is None


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


def test_cycle_memo_identity_and_repair_binding_fail_closed_on_drift() -> None:
    current = "三二"
    proposed = "方案二"
    unresolved, _resolved = converge_reconsidered_exact_final_findings(
        _srt(current),
        [_reconsidered_finding(current, proposed)],
        authority_audit=_authority(proposed, current),
        judge_llm_call=lambda _prompt: json.dumps(
            {
                "choice": "PROPOSED",
                "reason": "完整历史和语境支持方案二。",
            },
            ensure_ascii=False,
        ),
    )
    memo = collect_exact_final_convergence_memos({"findings": unresolved})[0]
    repaired_srt, repairs = _apply_exact_final_cpa_repairs(
        _srt(current),
        {"findings": unresolved},
    )
    assert len(repairs) == 1
    repair = repairs[0]
    memo_id = exact_final_convergence_memo_identity(memo)
    assert memo_id is not None
    assert valid_exact_final_convergence_memo_binding(
        memo,
        input_srt_sha256="sha256:" + _sha(_srt(current)),
        before_text=repair["before"],
        after_text=repair["after"],
        before_sha256=repair["before_sha256"],
        after_sha256=repair["after_sha256"],
        matched_start_ms=repair["matched_start_ms"],
        matched_end_ms=repair["matched_end_ms"],
    ) is True
    assert repaired_srt != _srt(current)

    assert valid_exact_final_convergence_memo_binding(
        memo,
        input_srt_sha256="sha256:" + _sha(_srt(current + "漂移")),
        before_text=repair["before"],
        after_text=repair["after"],
        before_sha256=repair["before_sha256"],
        after_sha256=repair["after_sha256"],
        matched_start_ms=repair["matched_start_ms"],
        matched_end_ms=repair["matched_end_ms"],
    ) is False

    malformed = deepcopy(memo)
    malformed["final_text"] = "篡改后的文本"
    assert exact_final_convergence_memo_identity(malformed) is None


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


@pytest.mark.parametrize("needs_audio", [False, True])
def test_cycle_cpa_can_request_evidence_without_selecting_text(needs_audio) -> None:
    current, proposed = "三二", "方案二"
    prompts = []

    def judge(prompt):
        prompts.append(prompt)
        return json.dumps({"choice_id": "UNRESOLVED", "needs_audio": needs_audio,
                           "reason": "完整句与短窗冲突，不能选择"})

    unresolved, resolved = converge_reconsidered_exact_final_findings(
        _srt(current), [_reconsidered_finding(current, proposed)],
        authority_audit=_authority(proposed, current), judge_llm_call=judge,
    )
    assert resolved == [] and len(prompts) == 1
    assert "UNRESOLVED" in prompts[0] and "needs_audio" in prompts[0]
    receipt = unresolved[0]["exact_final_cpa_cycle_adjudication"]
    assert receipt["status"] == "BLOCK" and receipt["needs_audio"] is needs_audio
    assert receipt["reason_code"] == (
        "CPA_REQUESTS_MORE_AUDIO" if needs_audio else "CPA_CYCLE_ADJUDICATION_UNRESOLVED"
    )
    adjudication = unresolved[0]["exact_release_adjudication"]
    assert adjudication["decision_authority"] == "CPA_JUDGE_REQUIRED"
    recovery = history_audio_recovery_from_adjudication(
        adjudication, request=adjudication["request"],
    )
    if needs_audio:
        assert recovery["status"] == "BLOCK"
        assert recovery["cpa_reason_code"] == "CPA_REQUESTS_MORE_AUDIO"
    else:
        assert recovery["status"] == "INVALID"
    unchanged, repairs = _apply_exact_final_cpa_repairs(_srt(current), {"findings": unresolved})
    assert unchanged == _srt(current) and repairs == []


@pytest.mark.parametrize("drift", [None, "authority", "digest", "window", "cue"])
def test_saved_cycle_audio_gate_with_ordinary_judge_label_remains_recoverable(drift) -> None:
    """Real saved producer receipt: outer CPA_JUDGE, inner CPA_JUDGE_REQUIRED."""
    current, proposed = "三二", "方案二"
    unresolved, _ = converge_reconsidered_exact_final_findings(
        _srt(current), [_reconsidered_finding(current, proposed)],
        authority_audit=_authority(proposed, current),
        judge_llm_call=lambda _: json.dumps({"choice_id": "UNRESOLVED",
                                             "needs_audio": True, "reason": "需要完整句原音"}),
    )
    saved = deepcopy(unresolved[0]["exact_release_adjudication"])
    saved["decision_authority"] = "CPA_JUDGE"
    gate = saved["exact_final_cpa_cycle_adjudication"]
    if drift == "authority":
        gate["decision_authority"] = "CPA_PROPOSAL_ONLY"
    elif drift == "digest":
        gate["judge_completion_sha256"] = "invalid"
    elif drift == "window":
        gate["matched_start_ms"] += 1
    elif drift == "cue":
        gate["current_cue_sha256"] = "f" * 64
    original = deepcopy(saved)
    recovery = history_audio_recovery_from_adjudication(saved, request=saved["request"])
    assert recovery["status"] == ("BLOCK" if drift is None else "INVALID")
    assert saved == original and saved["repaired"] is False
    unchanged, repairs = _apply_exact_final_cpa_repairs(_srt(current), {"findings": [{
        **unresolved[0], "exact_release_adjudication": saved,
    }]})
    assert unchanged == _srt(current) and repairs == []


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


def test_cycle_prompt_deduplicates_nested_receipts_losslessly() -> None:
    observation = {"transcript": "原音未确定，不能据此改字。" * 100, "source_sha256": "a" * 64}
    historical = {"before": "旧候选", "after": "现稿", "observation": observation}
    history = [{"receipt": historical}, {"receipt": {"memo": historical}}]
    evidence = [{"old_receipt": historical, "new_observation": {"heard": "新的独立音节"}}]
    # A literal user field resembling our reference marker must remain literal.
    evidence[0]["literal"] = {"$receipt_ref": "original-value"}
    history_json, evidence_json, section = _cycle_prompt_receipts(history, evidence)
    instruction, table_json = section.split("\n", 3)[2:]
    reference_key = instruction.split("仅含 ", 1)[1].split(" 的对象", 1)[0]
    table = json.loads(table_json)

    def expand(value):
        if isinstance(value, dict):
            if set(value) == {reference_key}:
                return expand(table[value[reference_key]])
            return {key: expand(child) for key, child in value.items()}
        if isinstance(value, list):
            return [expand(child) for child in value]
        return value

    assert expand(json.loads(history_json)) == history
    assert expand(json.loads(evidence_json)) == evidence
    assert _json_sha(expand(json.loads(history_json))) == _json_sha(history)
    assert _json_sha(expand(json.loads(evidence_json))) == _json_sha(evidence)
    assert len(history_json + evidence_json + section) < len(json.dumps(history) + json.dumps(evidence))
    assert reference_key != "$receipt_ref"


def test_cycle_prompt_keeps_small_uncopied_receipts_in_original_form() -> None:
    history, evidence = [{"before": "A", "after": "B"}], [{"reason": "new fact"}]
    h, e, section = _cycle_prompt_receipts(history, evidence)
    assert h == json.dumps(history, ensure_ascii=False, sort_keys=True)
    assert e == json.dumps(evidence, ensure_ascii=False, sort_keys=True)
    assert section == ""


@pytest.mark.parametrize("stage", ["judge_call", "completion_json_parse"])
def test_cycle_judge_failure_records_stage_without_exposing_exception_text(stage) -> None:
    current, proposed = "三二", "方案二"

    def judge(_prompt):
        if stage == "judge_call":
            raise TimeoutError("private provider detail")
        return "invalid completion"

    unresolved, resolved = converge_reconsidered_exact_final_findings(
        _srt(current), [_reconsidered_finding(current, proposed)],
        authority_audit=_authority(proposed, current), judge_llm_call=judge,
    )
    assert resolved == []
    receipt = unresolved[0]["exact_final_cpa_cycle_adjudication"]
    assert receipt["status"] == "BLOCK"
    assert receipt["failure"]["stage"] == stage
    assert receipt["failure"]["prompt_utf8_byte_count"] >= receipt["failure"]["prompt_char_count"]
    assert "private provider detail" not in json.dumps(receipt)
    if stage == "completion_json_parse":
        assert receipt["failure"]["completion_sha256"] == _sha("invalid completion")


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
    from src.autoslice.acoustic_witness_adjudication import AUDIO_EVIDENCE_PRIORITY
    assert AUDIO_EVIDENCE_PRIORITY in forward_prompts[0]
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


def _native_finding() -> dict:
    """Synthetic transport receipt; CPA still explicitly selects the proposal."""
    from src.autoslice.local_asr_target_evidence import exact_target_evidence, digest
    from src.autoslice.acoustic_witness_adjudication import judge_word_choice

    finding = _reconsidered_finding('三二', '方案二')
    adjudication = finding['exact_release_adjudication']
    request = adjudication['request']
    wr = build_witness_request(request)
    native = exact_target_evidence(
        {'provider': 'moss', 'model': 'moss-transcribe-diarize-pro',
         'input_audio_sha256': _sha('fixture-audio'), 'response_sha256': _sha('fixture-response'),
         'native_segments': [{'start_ms': 0, 'end_ms': 1000, 'text': '方案二'}],
         'raw_response': {'text': '方案二'}},
        audio=b'fixture-audio', source_sha256=_sha('fixture-source'),
        start_ms=1100, end_ms=2100, prefer_provider_text=True,
    )
    native['observation_scope'] = 'complete_exact_cue_no_padding'
    native.pop('receipt_sha256')
    native['receipt_sha256'] = digest(native)
    witness = {
        'schema_version': 'subtitle-span-acoustic-witness.v1',
        'witness_protocol': 'candidate_blind_transcript',
        'status': 'OBSERVED', 'target_audible': True,
        'audibility_basis': 'nonempty_provider_transcript',
        'candidate_exposure': 'none', 'authority': 'EVIDENCE_ONLY',
        'mutation_authorized': False, 'exact_transcript': native['transcript'],
        'request_sha256': wr['request_sha256'],
        'source_media_sha256': native['source_media_sha256'],
        'audio_clip_sha256': native['input_audio_sha256'],
        'audio_start_ms': 1100, 'audio_end_ms': 2100,
        'provider': native['provider'], 'model': native['model'],
        'response_sha256': native['response_sha256'], 'native_observation': native,
    }
    adjudication['verdict'] = witness
    adjudication['witness_judge']['judge'] = judge_word_choice(
        llm_call=lambda _p: json.dumps({'choice': 'PROPOSED', 'candidate_id': 'PROPOSAL',
                                       'reason': 'Synthetic native receipt supports this choice'}),
        check_request=request, witness=witness,
    )
    return finding


def _v03_cue23_native_finding() -> dict:
    """Reduce the real v03 cue23 MAI/MOSS receipt shape for replay tests."""
    from src.autoslice.acoustic_witness_adjudication import judge_word_choice

    finding = _reconsidered_finding(
        "都别想",
        "想都别想",
        cue_index=23,
        start_ms=96_000,
        end_ms=96_960,
        salt="v03-cue23",
    )
    adjudication = finding["exact_release_adjudication"]
    request = adjudication["request"]
    request.update(
        context_start_ms=85_100,
        context_end_ms=98_900,
        require_complete_utterance_support=True,
    )
    request.pop("request_sha256", None)
    request["request_sha256"] = _json_sha(request)
    witness_request = build_witness_request(request)
    source_sha = "a08ac282405ccb22b7615ee377c6b1b3450e9341e262131e09e110217069b278"
    audio_sha = "0a339006a5ffbf9892fb559d139e5052ae6116954e93b763d9898d2040e4397c"
    # These are the actual native payload fields from v03's cue23 accepted
    # observations.  Keeping their receipt/response/source hashes here makes
    # this a replay fixture, rather than a semantic answer-key fixture.
    native_records = {
        "mai": {
            "authority": "EVIDENCE_ONLY",
            "candidate_exposure": "none",
            "crop_is_exact_target": True,
            "diagnostics": [],
            "elapsed_seconds": 0.6947391917929053,
            "input_audio_sha256": audio_sha,
            "language_observation_available": False,
            "model": "MAI-Transcribe-2",
            "mutation_authorized": False,
            "native_segments": [{
                "end_ms": 760,
                "speaker": "S00",
                "start_ms": 160,
                "text": "想都别想",
                "words": [
                    {"end_ms": 239, "start_ms": 160, "text": "想"},
                    {"end_ms": 440, "start_ms": 360, "text": "都"},
                    {"end_ms": 599, "start_ms": 520, "text": "别"},
                    {"end_ms": 759, "start_ms": 680, "text": "想"},
                ],
                "words_available": True,
            }],
            "native_timeline": {
                "has_out_of_order": False,
                "has_overlap": False,
                "one_track_srt_eligible": True,
            },
            "note": "Provider-native text is fallible evidence. No independent phonetic claim or deletion authority.",
            "observation_scope": "complete_exact_cue_no_padding",
            "provider": "mai",
            "receipt_sha256": "fb6ff305dff965e5bca1a24f231bebd38743d28357f6b58b3dc3ddca7123c62e",
            "response_sha256": "0ae876324fa58250757c73209aa9d8a952b03104b0483ccccf1ec91f9e66897d",
            "schema_version": "exact-target-native-asr-evidence.v1",
            "served_from_cache": False,
            "source_media_sha256": source_sha,
            "source_segments": [{
                "native_end_ms": 760,
                "native_start_ms": 160,
                "source_end_ms": 96760,
                "source_start_ms": 96160,
                "text": "想都别想",
            }],
            "speaker_identity_observed": False,
            "status": "OBSERVED",
            "target_end_ms": 96960,
            "target_start_ms": 96000,
            "transcript": "想都别想",
        },
        "moss": {
            "authority": "EVIDENCE_ONLY",
            "candidate_exposure": "none",
            "crop_is_exact_target": True,
            "diagnostics": [],
            "elapsed_seconds": 4.787528242915869,
            "input_audio_sha256": audio_sha,
            "language_observation_available": False,
            "model": "moss-transcribe-diarize-pro",
            "mutation_authorized": False,
            "native_segments": [{
                "end_ms": 910,
                "speaker": "S01",
                "start_ms": 110,
                "text": "想都别想。",
            }],
            "native_timeline": {
                "has_out_of_order": False,
                "has_overlap": False,
                "one_track_srt_eligible": True,
            },
            "note": "Provider-native text is fallible evidence. No independent phonetic claim or deletion authority.",
            "observation_scope": "complete_exact_cue_no_padding",
            "provider": "moss",
            "raw_native_segments": [{
                "end": 0.91,
                "id": "seg_001",
                "speaker": "S01",
                "start": 0.11,
                "text": "想都别想。",
                "type": "transcript.text.segment",
            }],
            "receipt_sha256": "1ce4c5b4002fb63c8e282de66a9cd10806df6d06922fa2585df57cac0acd98e2",
            "response_sha256": "a506b8e2c7debd820943bfd77798610267195284fa0a1444a2d840f6bc063d8e",
            "schema_version": "exact-target-native-asr-evidence.v1",
            "served_from_cache": False,
            "source_media_sha256": source_sha,
            "source_segments": [{
                "native_end_ms": 910,
                "native_start_ms": 110,
                "source_end_ms": 96910,
                "source_start_ms": 96110,
                "text": "想都别想。",
            }],
            "speaker_identity_observed": False,
            "status": "OBSERVED",
            "target_end_ms": 96960,
            "target_start_ms": 96000,
            "transcript": "想都别想。",
            "transcript_basis": "provider_full_crop_text",
            "transcript_timeline_available": False,
        },
    }
    rows = []
    for provider, native in native_records.items():
        rows.append({
            "schema_version": "subtitle-span-acoustic-witness.v1",
            "witness_protocol": "candidate_blind_transcript",
            "status": "OBSERVED",
            "target_audible": True,
            "audibility_basis": "nonempty_provider_transcript",
            "candidate_exposure": "none",
            "authority": "EVIDENCE_ONLY",
            "mutation_authorized": False,
            "exact_transcript": native["transcript"],
            "request_sha256": witness_request["request_sha256"],
            "source_media_sha256": source_sha,
            "audio_clip_sha256": audio_sha,
            "audio_start_ms": 96_000,
            "audio_end_ms": 96_960,
            "provider": provider,
            "model": native["model"],
            "response_sha256": native["response_sha256"],
            "native_observation": native,
        })

    mai, moss = rows
    adjudication["verdict"] = mai
    adjudication["independent_asr"] = {
        "status": "OBSERVED",
        "mutation_authorized": False,
        "candidate_count": 2,
        "accepted_observations": rows,
    }
    adjudication["witness_judge"]["judge"] = judge_word_choice(
        llm_call=lambda _prompt: json.dumps({
            "choice": "PROPOSED",
            "candidate_id": "PROPOSAL",
            "reason": "Bound v03 native receipts are evidence only",
        }, ensure_ascii=False),
        check_request=request,
        witness=mai,
    )
    return finding


def _v03_cue23_srt() -> str:
    def stamp(ms: int) -> str:
        minutes, remainder = divmod(ms, 60_000)
        seconds, millis = divmod(remainder, 1_000)
        return f"00:{minutes:02d}:{seconds:02d},{millis:03d}"

    rows = []
    for index in range(1, 23):
        start = 0 if index == 1 else (index - 1) * 4_000
        end = start + 1_000
        rows.append(f"{index}\n{stamp(start)} --> {stamp(end)}\n邻句{index}\n")
    rows.append(f"23\n{stamp(96_000)} --> {stamp(96_960)}\n都别想\n")
    rows.append(f"24\n{stamp(96_960)} --> {stamp(98_400)}\n不会真的在想吧\n")
    rows.append(f"25\n{stamp(99_200)} --> {stamp(100_600)}\n后续语境\n")
    return "\n".join(rows)


def test_v03_cue23_native_transcripts_reach_compact_convergence_evidence() -> None:
    finding = _v03_cue23_native_finding()
    adjudication = finding["exact_release_adjudication"]
    compact = _compact_current_evidence(finding, adjudication)
    rows = compact["candidate_blind_transcript_observations"]
    assert [row["provider"] for row in rows] == ["mai", "moss"]
    assert [row["exact_transcript"] for row in rows] == ["想都别想", "想都别想。"]
    for row in rows:
        assert row["authority"] == "EVIDENCE_ONLY"
        assert row["candidate_exposure"] == "none"
        assert row["mutation_authorized"] is False
        assert row["matched_start_ms"] == 96_000
        assert row["matched_end_ms"] == 96_960
        assert row["audio_start_ms"] == 96_000
        assert row["audio_end_ms"] == 96_960
        assert row["require_complete_utterance_support"] is True
        assert row["crop_is_exact_target"] is True
        assert row["observation_scope"] == "complete_exact_cue_no_padding"
        assert row["source_media_sha256"] == (
            "a08ac282405ccb22b7615ee377c6b1b3450e9341e262131e09e110217069b278"
        )
        assert row["native_observation"]["observation_scope"] == (
            "complete_exact_cue_no_padding"
        )
        assert row["native_observation"]["source_segments"]
        assert row["native_observation"]["native_segments"]
        assert "raw_response" not in row["native_observation"]

    prompts = []
    authority = _authority("旧文本", "都别想")
    repair = authority["exact_final_cpa_self_heal"]["passes"][0]["repairs"][0]
    repair.update(cue_index=23, matched_start_ms=96_000, matched_end_ms=96_960)
    unresolved, resolved = converge_reconsidered_exact_final_findings(
        _v03_cue23_srt(),
        [finding],
        authority_audit=authority,
        judge_llm_call=lambda prompt: (
            prompts.append(prompt)
            or json.dumps({"choice": "CURRENT", "reason": "evidence transport fixture"})
        ),
    )
    assert unresolved == []
    assert len(resolved) == 1
    assert len(prompts) == 1
    prompt = prompts[0]
    for marker in (
        "想都别想",
        "MAI-Transcribe-2",
        "moss-transcribe-diarize-pro",
        "complete_exact_cue_no_padding",
        "96160",
        "96910",
        "a08ac282405ccb22b7615ee377c6b1b3450e9341e262131e09e110217069b278",
    ):
        assert marker in prompt


@pytest.mark.parametrize("drift", ["source", "crop", "request"])
def test_v03_native_compact_rejects_drifted_secondary_observation(drift: str) -> None:
    finding = _v03_cue23_native_finding()
    adjudication = finding["exact_release_adjudication"]
    secondary = adjudication["independent_asr"]["accepted_observations"][1]
    if drift == "source":
        from src.autoslice.local_asr_target_evidence import digest

        source_sha = _sha("other-source")
        secondary["source_media_sha256"] = source_sha
        secondary["native_observation"]["source_media_sha256"] = source_sha
        native = secondary["native_observation"]
        receipt = native.pop("receipt_sha256")
        native["receipt_sha256"] = digest(native)
        assert receipt != native["receipt_sha256"]
    elif drift == "crop":
        secondary["audio_start_ms"] += 1
    else:
        secondary["request_sha256"] = "0" * 64
    compact = _compact_current_evidence(finding, adjudication)
    rows = compact["candidate_blind_transcript_observations"]
    assert [row["provider"] for row in rows] == ["mai"]


def test_native_transcript_convergence_consumes_bound_cpa_proposal() -> None:
    finding = _native_finding()
    unresolved, resolved = converge_reconsidered_exact_final_findings(
        _srt('三二'), [finding], authority_audit=_authority('旧文本', '三二'),
        judge_llm_call=lambda _p: json.dumps({'choice': 'PROPOSED', 'reason': 'fixture evidence'}),
    )
    assert resolved == []
    assert len(unresolved) == 1  # The mutation remains pending until canonical self-heal.
    repaired, receipts = _apply_exact_final_cpa_repairs(_srt('三二'), {'findings': unresolved})
    assert repaired == _srt('方案二')
    assert len(receipts) == 1
    binding = receipts[0]['acoustic_witness_binding']
    assert valid_acoustic_witness_binding(binding, proposed='方案二', window=(1100, 2100))
    assert binding['witness_protocol'] == 'candidate_blind_transcript'
    assert 'heard_pinyin' not in binding['witness']


@pytest.mark.parametrize('cyclic', [False, True])
def test_fresh_bound_convergence_retires_prior_cycle_block_for_self_heal(cyclic) -> None:
    current, proposed = '三二', '方案二'
    old, _ = converge_reconsidered_exact_final_findings(
        _srt(current), [_reconsidered_finding(current, proposed)],
        authority_audit=_authority(proposed, current),
        judge_llm_call=lambda _: json.dumps({'choice_id': 'UNRESOLVED',
                                             'needs_audio': True, 'reason': '需要完整原音'}),
    )
    original = deepcopy(old[0])
    fresh = _native_finding()
    fresh['exact_final_cpa_cycle_adjudication'] = old[0]['exact_final_cpa_cycle_adjudication']
    fresh['exact_release_adjudication']['exact_final_cpa_cycle_adjudication'] = old[0]['exact_final_cpa_cycle_adjudication']
    authority = _authority(proposed if cyclic else '旧文本', current)

    def judge(prompt):
        if cyclic:
            # Use the normal candidate identity; the full memo and acoustic
            # witness are still validated by production convergence/self-heal.
            from src.autoslice.exact_final_convergence import _candidate_id
            return json.dumps({'choice_id': _candidate_id(proposed), 'reason': 'fixture new native evidence'})
        return json.dumps({'choice': 'PROPOSED', 'reason': 'fixture new native evidence'})

    unresolved, resolved = converge_reconsidered_exact_final_findings(
        _srt(current), [fresh], authority_audit=authority, judge_llm_call=judge,
    )
    assert resolved == [] and len(unresolved) == 1
    row = unresolved[0]
    assert 'exact_final_cpa_cycle_adjudication' not in row
    assert row['prior_exact_final_cpa_cycle_adjudications'] == [original['exact_final_cpa_cycle_adjudication']]
    if not cyclic:  # Closed-set application creates a new exact wrapper.
        assert row['exact_release_adjudication']['prior_exact_final_cpa_cycle_adjudications'] == [original['exact_final_cpa_cycle_adjudication']]
    assert old[0] == original
    output, repairs = _apply_exact_final_cpa_repairs(_srt(current), {'findings': unresolved})
    assert output == _srt(proposed) and len(repairs) == 1


@pytest.mark.parametrize('drift', ['srt', 'completion', 'window', 'old_gate_window'])
def test_invalid_successor_memo_cannot_retire_prior_cycle_block(drift) -> None:
    from src.autoslice.exact_final_convergence import _retain_superseded_cycle_blocks
    source = _srt('三二')
    row, _ = converge_reconsidered_exact_final_findings(
        source, [_native_finding()], authority_audit=_authority('旧文本', '三二'),
        judge_llm_call=lambda _: json.dumps({'choice': 'PROPOSED', 'reason': 'fixture evidence'}),
    )
    finding = row[0]
    memo = finding['exact_release_adjudication']['exact_final_cpa_convergence_memo']
    finding['exact_final_cpa_cycle_adjudication'] = {
        'schema_version': 'exact-final-cpa-cycle-adjudication.v1', 'status': 'BLOCK',
        'matched_start_ms': 1100, 'matched_end_ms': 2100,
        'current_cue_sha256': 'sha256:' + _sha('三二'),
    }
    if drift == 'srt':
        source = _srt('三二', neighbor='changed context')
    elif drift == 'completion':
        memo['completion_sha256'] = 'invalid'
    elif drift == 'window':
        memo['matched_start_ms'] += 1
    else:
        finding['exact_final_cpa_cycle_adjudication']['matched_start_ms'] += 1
    original = deepcopy(finding['exact_final_cpa_cycle_adjudication'])
    _retain_superseded_cycle_blocks(finding, memo=memo, srt_text=source)
    assert finding['exact_final_cpa_cycle_adjudication'] == original
    assert 'prior_exact_final_cpa_cycle_adjudications' not in finding
    unchanged, repairs = _apply_exact_final_cpa_repairs(_srt('三二'), {'findings': [finding]})
    assert unchanged == _srt('三二') and repairs == []


def test_native_selection_cannot_authorize_another_proposal_or_typed_history() -> None:
    from src.autoslice.final_review_contract import (
        FinalReviewContractError, _validate_exact_final_cpa_self_heal,
    )
    finding = _native_finding()
    source = _srt('三二')
    output, repairs = _apply_exact_final_cpa_repairs(source, {'findings': [finding]})
    assert len(repairs) == 1
    receipt = {
        'schema_version': 'exact-final-cpa-self-heal-audit.v1', 'status': 'PASS',
        'final_srt_sha256': 'sha256:' + _sha(output),
        'passes': [{
            'schema_version': 'exact-final-cpa-self-heal-pass.v1', 'pass_index': 1,
            'input_srt_sha256': 'sha256:' + _sha(source),
            'output_srt_sha256': 'sha256:' + _sha(output), 'repairs': repairs,
        }],
    }
    _validate_exact_final_cpa_self_heal(
        {'exact_final_cpa_self_heal': receipt}, expected_srt_sha256='sha256:' + _sha(output),
    )
    repairs[0]['judge']['selected_candidate_text'] = '另一份听写'
    # The whole-review owner wraps the altered source-selection identity at
    # the span-compilation boundary; the typed cause must still be retained.
    with pytest.raises(FinalReviewContractError, match='FINAL_REVIEW_SPAN_COMPILATION_INVALID') as rejected:
        _validate_exact_final_cpa_self_heal(
            {'exact_final_cpa_self_heal': receipt}, expected_srt_sha256='sha256:' + _sha(output),
        )
    assert 'SPAN_COMPILATION_IDENTITY_DRIFT' in str(rejected.value.__cause__)
    finding['exact_release_adjudication']['witness_judge']['judge']['selected_candidate_text'] = '另一份听写'
    unchanged, refused = _apply_exact_final_cpa_repairs(source, {'findings': [finding]})
    assert unchanged == source and refused == []


@pytest.mark.parametrize('tamper', [
    'transcript', 'source', 'geometry', 'receipt', 'confidence', 'candidate',
    'selected_text', 'native_model', 'native_exposure',
])
def test_native_convergence_rejects_wrong_or_unbound_evidence(tamper: str) -> None:
    from src.autoslice.local_asr_target_evidence import digest
    finding = _native_finding()
    a = finding['exact_release_adjudication']
    w, j = a['verdict'], a['witness_judge']['judge']
    if tamper == 'transcript':
        w['exact_transcript'] = '三二'
    elif tamper == 'source':
        w['source_media_sha256'] = _sha('other-source')
    elif tamper == 'geometry':
        w['audio_start_ms'] += 10
    elif tamper == 'receipt':
        w['native_observation']['receipt_sha256'] = '0' * 64
    elif tamper == 'confidence':
        w['confidence'] = .99
    elif tamper == 'candidate':
        j['candidate_id'] = 'SOURCE_1'
    elif tamper == 'selected_text':
        j['selected_candidate_text'] = '别的文本'
    else:
        n = w['native_observation']
        if tamper == 'native_model':
            n['model'] = w['model'] = 'wrong-model'
        else:
            n['candidate_exposure'] = 'current-and-proposed'
        n.pop('receipt_sha256')
        n['receipt_sha256'] = digest(n)
    assert build_acoustic_witness_binding(check_request=a['request'], witness=w, judge=j) is None
