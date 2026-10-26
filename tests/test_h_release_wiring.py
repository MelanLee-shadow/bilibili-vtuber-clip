"""H wired through the release chain, not only the G gate.

Rule: 公开规则：如果有哪些真的听不清，但是你发现语义完全符合，语境完全符合，不会有
任何的问题，那么那个小错是可以发的。(GOAL-03/SUB-18); admin ruling every
consumer that decides whether a disclosed row is resolved asks the union
``is_release_resolved_keep``. The exact-final bucket asks the union alone; a bare
noncritical label (no audio ruling, no exhausted-audio keep) stays blocking.
Synthetic WAV and protocol stubs; no provider.
"""
from copy import deepcopy
import json

import pytest

from src.autoslice import producer_text_pipeline as pipeline
from src.autoslice import targeted_relisten as policy
from src.autoslice.final_review_contract import (
    FinalReviewContractError, is_keep_current_disclosed, is_noncritical_disclosed,
    is_release_resolved_keep, validate_final_review_release,
)
from src.autoslice.producer_package_finalization import _current_postburn_resolution_evidence
from src.autoslice.unresolved_findings_gate import unresolved_disclosed_findings, unresolved_findings_problem
from test_sub18_meaning_equivalent_keep import EQUIVALENCE, _exhausted, _noncritical, _reseal_proof
from test_targeted_relisten_public_contract import SRT, rig  # noqa: F401  (rig fixture)


def _shipped(rig):
    audit, prior = _exhausted(rig)
    row = _noncritical(rig)
    keep = policy.sub18_meaning_equivalent_keep(finding=row, recovery_audit=audit, prior=prior)
    assert keep is not None
    return {**row, "exact_release_adjudication": keep}


def _variant(shipped, kind):
    if kind == "qualifying":
        return shipped
    if kind == "no_material_exhaustion":
        forged = _reseal_proof(shipped["exact_release_adjudication"],
                               lambda p: (p.pop("material_exhaustion"), p.pop("audio_resolution")))
        return {**shipped, "exact_release_adjudication": forged}
    if kind == "meaning_reversal":
        return {**shipped, "content_risk": "meaning_reversal"}
    # A bare noncritical meaning-equivalence label with no adjudication: the
    # legacy classifier accepts it, the release union must not.
    bare = {k: v for k, v in shipped.items() if k != "exact_release_adjudication"}
    assert is_noncritical_disclosed(bare) and not is_release_resolved_keep(bare)
    return bare


def _exact_final(monkeypatch, row):
    from tests.test_producer_text_pipeline_final_review import _adapters, _correction_pass
    monkeypatch.setattr(pipeline, "discover_candidate_pronoun_findings", lambda *a, **k: ([], {}))
    monkeypatch.setattr(pipeline, "clip_context_prompt_text", lambda _: "")
    seen = []

    def adjudicate(_srt, rows, **_kwargs):
        # The real H producer ran in ``_shipped`` (auditor public path is
        # covered by test_sub18_meaning_equivalent_keep); this seam hands its
        # output to the exact-final owner exactly as the auditor returns it.
        rows = list(rows)
        seen.extend(rows)
        assert [r["cue_index"] for r in rows] == [row["cue_index"]]
        return [deepcopy(row)], []

    monkeypatch.setattr(pipeline, "adjudicate_exact_release_findings", adjudicate)
    monkeypatch.setattr(pipeline, "resolve_findings_from_exact_final_convergence_memos",
                        lambda _srt, rows, **_kw: (list(rows), []))
    monkeypatch.setattr(pipeline, "converge_reconsidered_exact_final_findings",
                        lambda _srt, rows, **_kw: (list(rows), []))
    item = {"cue": row["cue_index"], "kind": "context", "repair_class": "disclosure_only",
            "proposed_full_cue": None, "why": "尾音字形听不清。",
            "content_risk": "noncritical_uncertainty", "risk_basis": row["risk_basis"],
            "meaning_equivalence": deepcopy(EQUIVALENCE)}
    result = pipeline._run_exact_final_release_review(
        srt_text=SRT, correction_audit=_correction_pass(), adapters=_adapters(),
        authoritative_chat=(), selection_hook="", clip_context={}, pronoun_audit_llm=lambda _: "{}",
        final_review_llm=lambda _prompt: json.dumps({"findings": [item]}),
        verify_confusable_entity=lambda _: {})
    assert seen, "exact-final adjudication was not reached"
    return result


@pytest.mark.parametrize("kind", ["qualifying", "no_material_exhaustion", "meaning_reversal", "bare_noncritical"])
def test_exact_final_entry_discloses_only_the_release_union(rig, monkeypatch, kind):
    row = _variant(_shipped(rig), kind)
    result = _exact_final(monkeypatch, row)
    record = {"exact_final_review": result}
    if kind == "qualifying":
        assert result["status"] == "CLEAN" and result["release_gate"] == "PASS", result.get("reason_codes")
        assert result["findings"] == [] and len(result["unresolved_findings_disclosed"]) == 1
        disclosed = result["unresolved_findings_disclosed"][0]
        assert {key: value for key, value in disclosed.items() if key != "lifecycle"} == row
        assert disclosed["lifecycle"]["state"] == "KEEP"
        validate_final_review_release(result, expected_srt_sha256=result["reviewed_srt_sha256"])
        # G and the package-audit reader (scripts/audit_review_package.py).
        assert unresolved_findings_problem(record) is None
        assert unresolved_disclosed_findings(record) == []
        # Postburn resolution evidence carries the disclosed H row forward.
        evidence = _current_postburn_resolution_evidence(
            exact_review=result, reviewed_srt_sha256=result["reviewed_srt_sha256"])
        assert evidence and evidence["resolved_findings"] == [disclosed]
    else:
        assert result["release_gate"] == "BLOCK"
        assert not result["unresolved_findings_disclosed"]
        assert [f["cue_index"] for f in result["findings"]] == [row["cue_index"]]
        assert "FINAL_REVIEW_FINDING_REVIEW_PENDING" in result["reason_codes"]
        with pytest.raises(FinalReviewContractError):
            validate_final_review_release(result)


def test_disclosed_row_rechecks_accept_h_and_keep_the_narrow_meaning(rig):
    shipped = _shipped(rig)
    from src.autoslice.final_review_contract import is_audio_decided_keep_current
    assert is_keep_current_disclosed(shipped) and not is_audio_decided_keep_current(shipped)
    for kind in ("no_material_exhaustion", "meaning_reversal"):
        assert not is_keep_current_disclosed(_variant(shipped, kind))
