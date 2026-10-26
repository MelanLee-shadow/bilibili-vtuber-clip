"""H: SUB-18's second terminal form passes G only with every condition met.

Rule: 公开规则：如果有哪些真的听不清，但是你发现语义完全符合，语境完全符合，不会有
任何的问题，那么那个小错是可以发的。(GOAL-03/SUB-18), admin ruling
only noncritical_uncertainty (meaning_reversal never), audio material exhausted,
complete typed meaning_equivalence v2, context fully consistent, UNRESOLVED_BY_AUDIO
disclosure. ``is_audio_decided_keep_current`` keeps its meaning; G asks the union.
Synthetic WAV and protocol stubs; no provider.
"""
from copy import deepcopy
import hashlib

import pytest

from src.autoslice import targeted_relisten as policy
from src.autoslice.final_review_contract import (
    is_audio_decided_keep_current, is_release_resolved_keep, is_sub18_context_meaning_equivalent_keep,
)
from src.autoslice.final_review_meaning_policy import bind_current_risk
from src.autoslice.unresolved_findings_gate import unresolved_findings_problem
from test_targeted_relisten_public_contract import (  # noqa: F401  (rig fixture)
    SRT, acquire_range, no_gain_plan, public_judge, rig, three_observations,
)

EQUIVALENCE = {"status": "EQUIVALENT", "current_meaning": "说话人听到了一声哼唱般的声音",
               "alternative_meaning": "尾音也可能写作哼唱", "shared_meaning": "说话人听到了那个声音",
               "uncertain_detail": "尾音字形", "context_basis": "下一句明确说刚才只是在哼唱",
               "evidence_cue_ids": [2, 3]}


def _noncritical(rig):
    row = {**deepcopy(rig.finding), "content_risk": "noncritical_uncertainty",
           "risk_basis": "尾音字形不改变所指与整句含义。",
           "base_text_sha256": hashlib.sha256("我听到了，呜呜".encode()).hexdigest()}
    row["current_content_risk"] = bind_current_risk(row, srt_text=SRT, cue_count=4,
        prompt="synthetic final review prompt", meaning_equivalence=EQUIVALENCE)
    return row


def _exhausted(rig):
    _, check, finding, _ = three_observations(rig)
    _, audit, _ = acquire_range(rig, finding, check, no_gain_plan)
    assert audit["status"] == "CONTEXT_REVIEW", audit.get("reason_code")
    assert audit["no_gain_proof"]["material_exhaustion"]["argon"]["state"] == "STRUCTURALLY_UNAVAILABLE"
    return audit, finding["exact_release_adjudication"]


def _disclosed(row):
    return {"unresolved_findings_disclosed": [row]}


def _reseal_proof(keep, mutate):
    forged = deepcopy(keep)
    receipt = forged["sub18_context_meaning_equivalent_keep"]
    proof = receipt["no_gain_proof"]
    mutate(proof)
    proof.pop("proof_sha256")
    proof["proof_sha256"] = policy.digest(proof)
    receipt["no_gain_proof_sha256"] = proof["proof_sha256"]
    receipt["material_exhaustion"] = proof.get("material_exhaustion")
    return forged


def test_qualifying_row_passes_g_through_the_union(rig):
    audit, prior = _exhausted(rig)
    row = _noncritical(rig)
    keep = policy.sub18_meaning_equivalent_keep(finding=row, recovery_audit=audit, prior=prior)
    assert keep is not None and keep["repaired"] is False
    assert keep["mutation_authority"]["status"] == "NOT_APPLIED"
    assert keep["sub18_context_meaning_equivalent_keep"]["audio_resolution"] == "UNRESOLVED_BY_AUDIO"
    shipped = {**row, "exact_release_adjudication": keep}
    assert is_sub18_context_meaning_equivalent_keep(shipped) and is_release_resolved_keep(shipped)
    assert not is_audio_decided_keep_current(shipped)  # Its meaning is unchanged.
    assert unresolved_findings_problem(_disclosed(shipped)) is None


def test_proof_from_another_cue_is_not_borrowed(rig):
    audit, prior = _exhausted(rig)
    row = _noncritical(rig)
    keep = policy.sub18_meaning_equivalent_keep(finding=row, recovery_audit=audit, prior=prior)
    assert is_sub18_context_meaning_equivalent_keep({**row, "exact_release_adjudication": keep})
    borrowed = _reseal_proof(keep, lambda proof: proof["anchor"].update(
        current_cue_sha256=hashlib.sha256("另一句字幕".encode()).hexdigest()))
    row["exact_release_adjudication"] = borrowed
    assert is_sub18_context_meaning_equivalent_keep(row) is False


def test_proof_from_another_whole_srt_is_not_borrowed(rig):
    audit, prior = _exhausted(rig)
    row = _noncritical(rig)
    keep = policy.sub18_meaning_equivalent_keep(finding=row, recovery_audit=audit, prior=prior)
    assert is_sub18_context_meaning_equivalent_keep({**row, "exact_release_adjudication": keep})
    borrowed = _reseal_proof(keep, lambda proof: proof["anchor"].update(
        current_srt_sha256=hashlib.sha256("另一份整片字幕".encode()).hexdigest()))
    row["exact_release_adjudication"] = borrowed
    assert is_sub18_context_meaning_equivalent_keep(row) is False


def test_each_missing_condition_keeps_g_blocked(rig):
    audit, prior = _exhausted(rig)
    row = _noncritical(rig)
    keep = policy.sub18_meaning_equivalent_keep(finding=row, recovery_audit=audit, prior=prior)
    shipped = {**row, "exact_release_adjudication": keep}
    # meaning_reversal never takes this exit.
    reversal = {**row, "content_risk": "meaning_reversal"}
    assert policy.sub18_meaning_equivalent_keep(finding=reversal, recovery_audit=audit, prior=prior) is None
    assert unresolved_findings_problem(_disclosed({**shipped, "content_risk": "meaning_reversal"}))
    # Missing material exhaustion.
    no_exhaustion = _reseal_proof(keep, lambda p: (p.pop("material_exhaustion"), p.pop("audio_resolution")))
    assert unresolved_findings_problem(_disclosed({**row, "exact_release_adjudication": no_exhaustion}))
    # Missing typed v2 meaning-equivalence evidence.
    no_v2 = {k: v for k, v in shipped.items() if k != "current_content_risk"}
    assert unresolved_findings_problem(_disclosed(no_v2))
    # Missing UNRESOLVED_BY_AUDIO disclosure.
    undisclosed = deepcopy(keep)
    undisclosed["sub18_context_meaning_equivalent_keep"].pop("audio_resolution")
    assert unresolved_findings_problem(_disclosed({**row, "exact_release_adjudication": undisclosed}))
    # Argon not terminal while three distinct observations exist.
    pending = _reseal_proof(keep, lambda p: p["material_exhaustion"].update(
        argon={"state": "NOT_TRIGGERED"}))
    assert unresolved_findings_problem(_disclosed({**row, "exact_release_adjudication": pending}))


def test_public_exact_release_entry_reaches_second_form_with_one_terminal_judgment(rig):
    from src.autoslice import final_review_auditor as auditor
    from src.autoslice.final_review_contract import SUB18_KEEP_BRANCH
    row = _noncritical(rig)
    judge = public_judge(rig, terminal_keep=True)
    unresolved, resolved = auditor.adjudicate_exact_release_findings(
        SRT, [row], entity_verifier=rig.verifier, clip_context=rig.context, judge_llm_call=judge)
    assert not resolved and len(unresolved) == 1
    audit = unresolved[0]["exact_release_adjudication"]
    assert audit["policy_branch"] == SUB18_KEEP_BRANCH, (audit.get("reason_code"), audit.get("policy_branch"),
        (audit.get("semantic_audio_recovery") or {}).get("sub18_keep_reason_code"))
    assert audit["repaired"] is False and audit["contextual_terminal_handoff"]["apply_required"] is False
    assert not [p for p in judge.prompts if '"audio_gain_termination"' in p]  # No editorial rewrite call.
    terminal_prompts = [p for p in judge.prompts if p.startswith('# 字幕材料穷尽终裁 v1')]
    assert len(terminal_prompts) == 1 and '"needs_audio"' not in terminal_prompts[0]
    assert unresolved_findings_problem(_disclosed(unresolved[0])) is None
