"""A real correction receipt, not a later clean scan, retires stale gift text."""
from copy import deepcopy
import hashlib
import json

import pytest

from src.autoslice.final_review_auditor import (
    adjudicate_context_finding, audit_correction_mutation_authority, route_findings,
)
from src.autoslice.final_review_contract import validate_final_review_release
from src.autoslice.producer_text_checkpoint import retain_post_transcript_text
from src.autoslice.producer_text_finalization import verify_chat_authority_final_surfaces


def sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


def srt(text, start=2500, end=4500):
    def ts(t):
        return f"00:00:{t // 1000:02d},{t % 1000:03d}"
    return f"1\n{ts(start)} --> {ts(end)}\n{text}\n"


def clean_review(final, correction):
    grid = "sha256:" + "d" * 64
    return {
        "schema_version": "final-review-audit.v2", "status": "CLEAN",
        "release_gate": "PASS", "reviewed_srt_sha256": "sha256:" + sha(final),
        "discovery": {"status": "COMPLETE"}, "findings": [],
        "validated_finding_count": 0, "correction_pass": correction,
        "correction_mutation_authority": audit_correction_mutation_authority(correction),
        "boundary_semantic_review": {
            "status": "PASS", "review_scope": "final_delivery",
            "request_sha256": "sha256:" + "e" * 64, "cue_grid_sha256": grid,
            "final_endpoint_binding": {
                "schema_version": "talk-boundary-final-endpoint-binding.v1",
                "status": "PASS", "recommended_end_cue_index": 1,
                "recommended_end_ms": 3500, "final_closure_cue_index": 1,
                "final_snapped_end_ms": 3500, "reason_codes": [],
                "semantic_cue_grid_sha256": grid, "final_cue_grid_sha256": grid,
            },
            "source_separation_witness": {
                "schema_version": "talk-boundary-source-separation-witness.v1",
                "status": "PASS", "source_review_sha256": "sha256:" + "f" * 64,
                "source_request_sha256": "sha256:" + "0" * 64,
                "source_cue_grid_sha256": "sha256:" + "1" * 64,
                "source_final_start_ms": 1000, "source_final_end_ms": 5000,
                "reason_codes": [],
            },
        },
    }


def fixture(kind="cpa"):
    before, after = "谢谢测试的星光花朵", "谢谢测试的星光灯牌"
    suspect, replacement = "花朵", "灯牌"
    if kind == "canon":
        before, after = "谢谢林墨", "谢谢礼墨"
        suspect, replacement = "林墨", "礼墨"
    finding = {
        "cue_index": 1, "kind": "context", "repair_class": "spoken_unit",
        "suspect": suspect, "suggestion": replacement,
        "proposed_full_cue": after, "base_text_sha256": sha(before),
        "span_start_codepoint": before.index(suspect),
        "span_end_codepoint": before.index(suspect) + len(suspect),
        "why": "Synthetic test of persisted mutation ownership.",
    }
    if kind == "cpa":
        def no_audio(request):
            raise AssertionError("Text-first choice did not ask for audio")
        def judge(prompt):
            return json.dumps({"choice": "PROPOSED", "needs_audio": False,
                "ranking": [{"choice": "PROPOSED", "p": .95},
                            {"choice": "CURRENT", "p": .04},
                            {"choice": "NEITHER", "p": .01}],
                "reason": "Fixture's complete text evidence selects proposed."})
        output, adjudication = adjudicate_context_finding(
            srt(before), finding, entity_verifier=no_audio, judge_llm_call=judge,
        )
        assert adjudication["repaired"] is True
        finding.update(routed="context_audio_adjudicated_fix",
                       context_audio_adjudication=adjudication)
        correction = {"schema_version": "final-review-audit.v1", "status": "APPLIED",
                      "applied_count": 1, "findings": [finding]}
    else:
        finding.update(kind="entity", candidate_provenance={"kind": "glossary", "surface": "礼墨"})
        output, correction = route_findings(srt(before), [finding])
        assert correction["findings"][0]["routed"] == "expected_value_canon"
    assert after in output
    final = srt(after, 1500, 3500)
    old = {"cue_index": 1, "matched_start_ms": 2500, "matched_end_ms": 4500,
           "before": "earlier draft" if kind == "cpa" else before, "after": before,
           "outcome": "gift_name_repaired" if kind == "cpa" else "asr_win_no_change",
           "boundary_required": True, "boundary_owner_id": "gift:synthetic"}
    audit = {"gift_repairs": [old], "entity_repairs": [], "applied": [],
             "final_review_audit": clean_review(final, correction)}
    retain_post_transcript_text(audit, output)
    validate_final_review_release(audit["final_review_audit"], expected_srt_sha256="sha256:" + sha(final))
    assert audit_correction_mutation_authority(correction)["status"] == "PASS"
    return audit, final


@pytest.mark.parametrize("kind", ["cpa", "canon"])
def test_native_verifier_accepts_only_actual_correction_successor(kind):
    audit, final = fixture(kind)
    original = deepcopy(audit["gift_repairs"][0])
    assert verify_chat_authority_final_surfaces(audit, final_text_srt=final,
        final_speaker_srt=final, delivery_start_ms=1000, delivery_end_ms=5000)
    row = audit["gift_repairs"][0]
    assert all(row[k] == value for k, value in original.items())
    assert row["boundary_required"] is True
    assert row["final_verification_scope"] == "SUPERSEDED_BY_CORRECTION_PASS"
    assert "reconciliation" not in row
    assert "survived_final_text_srt" not in row
    assert audit["final_superseded_by_correction_pass_count"] == 1


@pytest.mark.parametrize("tamper", ["checkpoint", "checkpoint_hash", "request_hash",
    "before", "geometry", "bool_geometry", "judge", "mutation", "final_hash",
    "missing_correction", "different_speaker", "missing_checkpoint"])
def test_malformed_or_drifted_cpa_chain_does_not_retire_gift(tamper):
    audit, final = fixture()
    speaker = final
    review = audit["final_review_audit"]
    adj = review["correction_pass"]["findings"][0]["context_audio_adjudication"]
    row = audit["gift_repairs"][0]
    if tamper == "checkpoint":
        audit["post_transcript_entity_output_srt"] += "drift"
    elif tamper == "checkpoint_hash":
        audit["post_transcript_entity_output_srt_sha256"] = "a" * 64
    elif tamper == "request_hash":
        adj["request"]["request_sha256"] = "a" * 64
    elif tamper == "before":
        row["after"] = "unrelated original"
    elif tamper == "geometry":
        row["matched_end_ms"] += 1
    elif tamper == "bool_geometry":
        row["matched_start_ms"] = True
    elif tamper == "judge":
        adj["witness_judge"]["judge"]["choice"] = "CURRENT"
    elif tamper == "mutation":
        adj["mutation_authority"]["status"] = "BLOCK"
    elif tamper == "final_hash":
        review["reviewed_srt_sha256"] = "sha256:" + "a" * 64
    elif tamper == "missing_correction":
        review.pop("correction_pass")
    elif tamper == "different_speaker":
        speaker = final.replace("灯牌", "别的")
    elif tamper == "missing_checkpoint":
        audit.pop("post_transcript_entity_output_srt")
    assert not verify_chat_authority_final_surfaces(audit, final_text_srt=final,
        final_speaker_srt=speaker, delivery_start_ms=1000, delivery_end_ms=5000)
    assert "correction_pass_gift_supersession" not in row


def test_package_recomputes_receipt_and_rejects_claim_only_tampering():
    from src.autoslice.gift_correction_supersession import gift_correction_receipts_valid
    audit, final = fixture()
    assert verify_chat_authority_final_surfaces(audit, final_text_srt=final,
        final_speaker_srt=final, delivery_start_ms=1000, delivery_end_ms=5000)
    assert gift_correction_receipts_valid(audit, final, delivery_start_ms=1000)
    receipt = audit["gift_repairs"][0]["correction_pass_gift_supersession"]
    receipt["finding_sha256"] = "sha256:" + "a" * 64
    assert not gift_correction_receipts_valid(audit, final, delivery_start_ms=1000)


def test_canon_cannot_retire_a_mutating_gift_or_invalid_original_gate():
    for change in ("mutating_gift", "bad_gate"):
        audit, final = fixture("canon")
        if change == "mutating_gift":
            audit["gift_repairs"][0].update(before="different", outcome="gift_name_repaired")
        else:
            audit["final_review_audit"]["correction_pass"]["findings"][0]["expected_value_gate"] = {}
        assert not verify_chat_authority_final_surfaces(audit, final_text_srt=final,
            final_speaker_srt=final, delivery_start_ms=1000, delivery_end_ms=5000)
