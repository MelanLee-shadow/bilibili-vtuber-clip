from __future__ import annotations

import hashlib
import json
from copy import deepcopy

import pytest

from src.autoslice.producer_text_finalization import (
    verify_chat_authority_final_surfaces,
)
from src.autoslice.acoustic_witness_adjudication import build_witness_request
from src.autoslice.speaker_common import HOST_SPEAKER
from src.autoslice.surface_canon import canonicalize_expected_value_surfaces


def _srt(*cues: tuple[int, int, str]) -> str:
    blocks = []
    for index, (start_ms, end_ms, text) in enumerate(cues, start=1):
        start_seconds, start_millis = divmod(start_ms, 1_000)
        end_seconds, end_millis = divmod(end_ms, 1_000)
        blocks.append(
            f"{index}\n"
            f"00:00:{start_seconds:02d},{start_millis:03d} --> "
            f"00:00:{end_seconds:02d},{end_millis:03d}\n"
            f"{text}"
        )
    return "\n\n".join(blocks) + "\n"


def _truth_row(
    *,
    truth_id: str,
    start_ms: int,
    end_ms: int,
    canonical: str,
    boundary_role: str = "story_content",
) -> dict:
    return {
        "truth_id": truth_id,
        "required": True,
        "boundary_role": boundary_role,
        "action": "replace_cue",
        "local_windows": [{"start_ms": start_ms, "end_ms": end_ms}],
        "declared_output_contract": {
            "action": "replace_cue",
            "canonical_texts": [canonical],
            "required_text": "",
        },
    }


def _audit(*rows: dict, baseline: dict | None = None) -> dict:
    audit = {
        "source_subtitle_truth_audit": {
            "status": "ALREADY_SATISFIED",
            "applied": [],
            "satisfied": [deepcopy(row) for row in rows],
        }
    }
    if baseline is not None:
        audit["redelivery_subtitle_baseline_audit"] = baseline
    return audit


def _expected_value_806_fixture(
    *,
    before_cpa: str = "就礼墨拉布里二叔、七宝",
    after_cpa: str = "就林墨拉布里二叔、七宝",
) -> tuple[dict, str, dict, str, str]:
    """CPA only chose the pronunciation; deterministic canon still owns spelling."""

    canon_after, canon_replacements = canonicalize_expected_value_surfaces(
        after_cpa
    )
    evidence_id = (
        "dd4672628af57649a38c5e80d686bc7951e36e70753b269d74e17813c140956f"
    )
    before_sha256 = hashlib.sha256(before_cpa.encode()).hexdigest()
    after_sha256 = hashlib.sha256(after_cpa.encode()).hexdigest()
    span_start = 0
    while (
        span_start < len(before_cpa)
        and span_start < len(after_cpa)
        and before_cpa[span_start] == after_cpa[span_start]
    ):
        span_start += 1
    common_suffix = 0
    while (
        common_suffix < len(before_cpa) - span_start
        and common_suffix < len(after_cpa) - span_start
        and before_cpa[len(before_cpa) - common_suffix - 1]
        == after_cpa[len(after_cpa) - common_suffix - 1]
    ):
        common_suffix += 1
    span_end = len(before_cpa) - common_suffix
    after_span_end = len(after_cpa) - common_suffix
    suspect = before_cpa[span_start:span_end]
    suggestion = after_cpa[span_start:after_span_end]
    candidate_provenance = {
        "audit_sha256": (
            "sha256:47c20749f6d8211a011520f4ac8282fc5d2594a4e71ce773f25ba171f6275ec9"
        ),
        "cue_index": 8,
        "current_text_sha256": "sha256:" + before_sha256,
        "draft_fidelity_kept": True,
        "end_ms": 21_820,
        "kept_candidate": "PROPOSED",
        "kept_text_sha256": "sha256:" + after_sha256,
        "kind": "draft_fidelity_kept",
        "mutation_authorized": False,
        "schema_version": "draft-fidelity-kept-candidate.v1",
        "scope": "cue",
        "source_srt_sha256": (
            "sha256:100461a079e420a9edd528feaf56515487b89199f30824e75e747576817ccd84"
        ),
        "start_ms": 16_309,
        "surface": "拉布里",
        "violation_reason_codes": ["REPLACE_UNWITNESSED"],
    }
    structured_evidence = {
        "cue_index": 8,
        "draft_fidelity": {
            "current_similarity": 0.909091,
            "draft_fidelity_kept": True,
            "favored_candidate": "PROPOSED",
            "kept_text_sha256": "sha256:" + after_sha256,
            "proposed_similarity": 1.0,
        },
        "evidence_sha256": (
            "sha256:33af6a818a981c0cca7fef75e346a475be2398a605ba6e01798805faba70b82f"
        ),
        "neighbor_lexical_hits": {
            "candidate": "PROPOSED",
            "cue_count": 0,
            "cue_ids": [],
            "radius_cues": 2,
            "surface": "拉布里",
        },
        "schema_version": "subtitle-closed-set-structured-evidence.v1",
        "structured_chat_binding": {
            "bound_event_count": 0,
            "candidate": "PROPOSED",
            "evidence_cue_count": 0,
            "evidence_cue_ids": [],
            "kind": None,
            "source_event_ids": [],
            "source_sha256s": [],
            "surface": "拉布里",
        },
    }
    orthography_authority = {
        "schema_version": "subtitle-orthography-authority.v1",
        "status": "BLOCK",
        "provenance_kind": "draft_fidelity_kept",
        "reason_code": "ORTHOGRAPHY_TEXT_AUTHORITY_REQUIRED",
    }
    request = {
        "schema_version": "subtitle-span-acoustic-check-request.v1",
        "evidence_id": evidence_id,
        "kind": "subtitle_span_acoustic_check",
        "cue_indexes": [8],
        "base_text_sha256": before_sha256,
        "matched_start_ms": 16_309,
        "matched_end_ms": 21_820,
        "context_start_ms": 13_290,
        "context_end_ms": 24_200,
        "source_media_timeline_offset_ms": 0,
        "matched_audio_text": before_cpa,
        "suspect": suspect,
        "replacement": suggestion,
        "current_cue": before_cpa,
        "proposed_cue": after_cpa,
        "context_before": "其实我真的非常非常羡慕，嗯\n就是在公司的大家",
        "context_after": "南町南町，对吧\n很很羡慕",
        "candidate_entities": [
            {
                "candidate_id": "CURRENT",
                "canonical": before_cpa,
                "text_sha256": before_sha256,
                "surfaces": [],
                "readings": [],
            },
            {
                "candidate_id": "PROPOSED",
                "canonical": after_cpa,
                "text_sha256": after_sha256,
                "surfaces": [],
                "readings": [],
            },
        ],
        "repair_class": "phonetic",
        "candidate_provenance": candidate_provenance,
        "orthography_authority": orthography_authority,
        "evidence_cue_ids": [],
        "closed_set_structured_evidence": structured_evidence,
        "reason": (
            "fidelity guard kept this draft-side cue before the current "
            "surface drifted; candidate only, final choice belongs to CPA"
        ),
        "clip_context_binding": {
            "schema_version": "lidousha-clip-context.v1",
            "context_sha256": (
                "sha256:27bc0d632da76285e39630e48b6d6613fe70fd54543d75f8bcbde56f7c6fc51d"
            ),
            "whole_clip_draft_srt_sha256": (
                "sha256:1185c1fb2acbf86ba8da76fd0804665aa91201775d90da60838d5bdbdb9b4596"
            ),
            "speech_memory_ledger_sha256": (
                "sha256:d87532b5d56457496a868c6910091a545bd2c612a807182d742aa0ca26d30c95"
            ),
            "mutation_authorized": False,
        },
    }
    request_sha256 = hashlib.sha256(
        json.dumps(
            request,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    request["request_sha256"] = request_sha256
    witness_request_sha256 = build_witness_request(request)["request_sha256"]
    witness = {
        "schema_version": "subtitle-span-acoustic-witness.v1",
        "status": "OBSERVED",
        "witness_protocol": "blind_pinyin",
        "target_audible": True,
        "request_sha256": witness_request_sha256,
        "heard_pinyin": "zuo ling mo la bu li er san qi ba",
        "syllable_count": 10,
        "confidence": 0.4,
        "uncertain_positions": list(range(10)),
        "self_count_mismatch": False,
        "reason": (
            "speech is heavily accented or non-native, exact pinyin "
            "transcription is uncertain"
        ),
        "provider": "gemini_api",
        "model": "gemini-3.6-flash",
        "key_tier": "free",
        "prompt_sha256": (
            "1bd98fa361046cfd6f94316c5b761b7124cde09ffa46fbcd55a6973514da4fa1"
        ),
        "response_sha256": (
            "2e0d92dc03c9724f1bf0258f344db74298c004082d9a18c859425625dd87ea9c"
        ),
        "audio_clip_sha256": (
            "c5fdc505067c47d32402ffbcc144fe3cb741b04e1ec9af43e121c51b6134d2d5"
        ),
        "source_media_sha256": (
            "7a818c1c5a2775eeb866a2a15b4413e65daa209f635da561e34d4ae5711f2442"
        ),
        "audio_start_ms": 15_900,
        "audio_end_ms": 22_300,
        "timeline_binding": {
            "schema_version": "subtitle-audio-timeline-binding.v1",
            "source_media_timeline_offset_ms": 0,
            "delivery_local": {
                "context_start_ms": 13_290,
                "context_end_ms": 24_200,
                "target_start_ms": 16_309,
                "target_end_ms": 21_820,
            },
            "source_media": {
                "crop_start_ms": 15_900,
                "crop_end_ms": 22_300,
                "target_start_ms": 16_309,
                "target_end_ms": 21_820,
            },
        },
    }
    judge = {
        "schema_version": "acoustic-witness-adjudication.v1",
        "status": "JUDGED",
        "choice": "PROPOSED",
        "choice_set": ["CURRENT", "NEITHER", "PROPOSED"],
        "decision_contract": "current-proposed-neither.v1",
        "check_request_sha256": request_sha256,
        "prompt_sha256": (
            "6c32683e349d93fc57834d8a08cbc6814d506aed8a37ac52b0895c56786a28f9"
        ),
        "completion_sha256": (
            "ff10a42e71ab331c4632cfcbe2dc366956700937ba60b643e2a99fc4a2459c32"
        ),
        "candidate_pinyin_similarity": {
            "current": 0.9523809523809523,
            "proposed": 0.9523809523809523,
        },
        "closed_set_structured_evidence": structured_evidence,
    }
    mutation = {
        "schema_version": "subtitle-correction-mutation-authority.v1",
        "status": "PASS",
        "basis": "CPA_ACOUSTIC_PRONUNCIATION_DISAMBIGUATION",
    }
    witness_judge = {
        "schema_version": "acoustic-witness-adjudication.v1",
        "witness_status": "OBSERVED",
        "decision_authority": "CPA_JUDGE",
        "witness_authority": "EVIDENCE_ONLY",
        "witness_protocol": "blind_pinyin",
        "witness_request_sha256": witness_request_sha256,
        "witness_diagnostic_conflict": False,
        "candidate_pinyin_similarity": {
            "current": 0.9523809523809523,
            "proposed": 0.9523809523809523,
        },
        "pinyin_compatibility": {
            "current": 0.9523809523809523,
            "proposed": 0.9523809523809523,
        },
        "closed_set_structured_evidence": structured_evidence,
        "judge": judge,
    }
    adjudication = {
        "schema_version": "subtitle-span-adjudication.v1",
        "status": "OBSERVED",
        "repaired": True,
        "policy_branch": "WITNESS_JUDGE_APPLY_PROPOSED",
        "timing_immutable": True,
        "decision_authority": "CPA_JUDGE",
        "witness_authority": "EVIDENCE_ONLY",
        "orthography_ambiguous": False,
        "orthography_authority": orthography_authority,
        "orthography_equivalence": {"matched": False},
        "request": request,
        "verdict": witness,
        "witness_judge": witness_judge,
        "mutation_authority": mutation,
    }
    owner = {
        "mode": "final_review_context_adjudication",
        "action": "REPLACE_CUE_TEXT",
        "decision_authority": "CPA_JUDGE",
        "policy_branch": "WITNESS_JUDGE_APPLY_PROPOSED",
        "timing_immutable": True,
        "mutation_authority": mutation,
        "evidence_id": evidence_id,
        "request_sha256": request_sha256,
        "judge": judge,
        "verdict": witness,
        "acoustic_witness": witness,
        "cue_indexes": [8],
        "matched_start_ms": 16_309,
        "matched_end_ms": 21_820,
        "before": [before_cpa],
        "after": [after_cpa],
        "structured_exact_text": after_cpa,
        "survived": True,
        "boundary_required": True,
        "boundary_owner_id": "entity_repair:1:16309:21820",
        "repair_class": "phonetic",
        "survived_final_text_srt": False,
        "survived_final_speaker_srt": False,
    }
    padded_before = _srt(
        (1_000, 1_500, "占位一"),
        (2_000, 2_500, "占位二"),
        (3_000, 3_500, "占位三"),
        (4_000, 4_500, "占位四"),
        (5_000, 5_500, "占位五"),
        (6_000, 6_500, "占位六"),
        (7_000, 7_500, "占位七"),
        (16_309, 21_820, after_cpa),
    )
    padded_after = padded_before.replace(after_cpa, canon_after)
    input_sha256 = hashlib.sha256(padded_before.encode()).hexdigest()
    output_sha256 = hashlib.sha256(padded_after.encode()).hexdigest()
    final = _srt(
        (0, 400, "占位一"),
        (500, 900, "占位二"),
        (1_000, 1_400, "占位三"),
        (1_500, 1_900, "占位四"),
        (2_000, 2_400, "占位五"),
        (2_500, 2_900, "占位六"),
        (3_000, 3_400, "占位七"),
        (6_529, 12_040, canon_after),
    )
    final_sha256 = hashlib.sha256(final.encode()).hexdigest()
    target_finding = {
        "cue_index": 8,
        "base_text_sha256": before_sha256,
        "proposed_full_cue": after_cpa,
        "routed": "context_audio_adjudicated_fix",
        "repair_class": "phonetic",
        "span_start_codepoint": span_start,
        "span_end_codepoint": span_end,
        "suspect": suspect,
        "suggestion": suggestion,
        "candidate_provenance": candidate_provenance,
        "context_audio_adjudication": adjudication,
    }
    # The live correction pass applied four independent rows.  Only cue 8 is
    # material to this canary; synthetic nonmatching rows preserve the outer
    # count/shape without asserting content for the other three live cues.
    other_applied_findings = []
    for cue_index in (44, 74, 71):
        other = deepcopy(target_finding)
        other["cue_index"] = cue_index
        other_applied_findings.append(other)
    correction_pass = {
        "schema_version": "final-review-audit.v1",
        "status": "APPLIED",
        "applied_count": 4,
        "findings": [target_finding, *other_applied_findings],
    }
    correction_mutation = {
        "schema_version": "subtitle-correction-mutation-audit.v1",
        "status": "PASS",
        "applied_count": 4,
        "validated_mutation_count": 4,
        "failures": [],
    }
    audit = {
        "entity_repairs": [owner],
        "final_review_audit": {
            "schema_version": "final-review-audit.v2",
            "status": "CLEAN",
            "release_gate": "PASS",
            "reviewed_srt_sha256": "sha256:" + final_sha256,
            "correction_mutation_authority": correction_mutation,
            "correction_pass": correction_pass,
        },
        "final_hard_meme_surface_audit": {
            "schema_version": "hard-meme-surface-audit.v1",
            "status": "NO_CHANGE",
            "input_srt_sha256": input_sha256,
            "output_srt_sha256": input_sha256,
        },
        "final_expected_value_surface_audit": {
            "schema_version": "expected-value-surface-audit.v1",
            "status": "APPLIED",
            "decision_authority": "EXPECTED_VALUE_CANON",
            "registered_name_conflicts": [],
            "input_srt_sha256": input_sha256,
            "output_srt_sha256": output_sha256,
            "repairs": [
                {
                    "cue_index": 8,
                    "matched_start_ms": 16_309,
                    "matched_end_ms": 21_820,
                    "before": after_cpa,
                    "after": canon_after,
                    "decision_authority": "EXPECTED_VALUE_CANON",
                    "replacements": canon_replacements,
                }
            ],
        },
        "final_output_srt_sha256": final_sha256,
    }

    if (
        before_cpa == "就礼墨拉布里二叔、七宝"
        and after_cpa == "就林墨拉布里二叔、七宝"
    ):
        assert request_sha256 == (
            "3ab8a25c77cdc1c1c6dec24ce61e8f8b31b4d7224656f7c2917be768b9430697"
        )
        assert witness_request_sha256 == (
            "08b5d26b21c3bf5587bef5360d907c7793fa01222c858a88807bfdfb064717dd"
        )
    assert output_sha256 != final_sha256

    return audit, final, owner, before_cpa, after_cpa


def test_806_expected_value_canon_typed_supersession_retires_entity_surface() -> None:
    audit, final, owner, _before_cpa, _after_cpa = _expected_value_806_fixture()

    assert audit["final_review_audit"]["correction_pass"]["applied_count"] == 4
    assert verify_chat_authority_final_surfaces(
        audit,
        final_text_srt=final,
        final_speaker_srt=final,
        delivery_start_ms=9_780,
        delivery_end_ms=25_000,
    )
    assert owner["final_verification_scope"] == (
        "SUPERSEDED_BY_EXPECTED_VALUE_CANON"
    )
    assert owner["expected_value_canon_supersession"]["status"] == (
        "SUPERSEDED_BY_EXPECTED_VALUE_CANON"
    )
    assert owner["expected_value_canon_supersession"]["cpa_request_sha256"] == (
        "sha256:3ab8a25c77cdc1c1c6dec24ce61e8f8b31b4d7224656f7c2917be768b9430697"
    )
    assert owner["expected_value_canon_supersession"][
        "final_output_srt_sha256"
    ] == "sha256:" + hashlib.sha256(final.encode()).hexdigest()
    assert "survived_final_text_srt" not in owner
    assert "survived_final_speaker_srt" not in owner
    assert audit["final_superseded_by_expected_value_canon_count"] == 1


def test_806_expected_value_canon_supersession_accepts_clean_partial_correction() -> None:
    audit, final, owner, _before_cpa, _after_cpa = _expected_value_806_fixture()

    correction_pass = audit["final_review_audit"]["correction_pass"]
    correction_pass["status"] = "PARTIAL"

    assert verify_chat_authority_final_surfaces(
        audit,
        final_text_srt=final,
        final_speaker_srt=final,
        delivery_start_ms=9_780,
        delivery_end_ms=25_000,
    )
    assert owner["final_verification_scope"] == (
        "SUPERSEDED_BY_EXPECTED_VALUE_CANON"
    )
    assert owner["expected_value_canon_supersession"]["status"] == (
        "SUPERSEDED_BY_EXPECTED_VALUE_CANON"
    )


@pytest.mark.parametrize(
    "broken_contract",
    (
        "missing_owner_field",
        "malformed_owner",
        "missing_boundary_owner",
        "mismatched_boundary_owner",
        "missing_correction_audit",
        "malformed_correction_audit",
        "missing_correction_pass",
        "malformed_correction_pass",
        "unsupported_correction_status",
        "mismatched_declared_mutation",
        "missing_mutation_authority",
        "blocked_mutation_authority",
        "mismatched_cue",
        "mismatched_timing",
        "mismatched_request",
        "mismatched_evidence_id",
        "mismatched_request_payload_hash",
        "mismatched_witness_request",
        "mismatched_witness_timeline",
        "witness_not_fully_uncertain",
        "non_tied_pinyin",
        "orthography_authority_not_blocked",
        "mismatched_before",
        "mismatched_after",
        "missing_canon_audit",
        "malformed_canon_audit",
        "mismatched_canon_input_hash",
        "non_mutating_canon_output_hash",
        "mismatched_canon_cue",
        "mismatched_canon_timing",
        "malformed_canon_replacement",
        "registered_canon_conflict",
        "mismatched_final_output_hash",
        "malformed_reviewed_srt_hash",
        "extra_final_text",
        "extra_final_speaker_text",
        "overlapping_final_text",
        "overlapping_final_speaker_text",
    ),
)
def test_expected_value_canon_supersession_fails_closed(
    broken_contract: str,
) -> None:
    audit, final, owner, before_cpa, _after_cpa = _expected_value_806_fixture()
    review_audit = audit["final_review_audit"]
    correction_pass = review_audit["correction_pass"]
    correction_finding = correction_pass["findings"][0]
    adjudication = correction_finding["context_audio_adjudication"]
    request = adjudication["request"]
    witness = adjudication["verdict"]
    canon_audit = audit["final_expected_value_surface_audit"]
    canon_repair = canon_audit["repairs"][0]
    if broken_contract == "missing_owner_field":
        owner.pop("request_sha256")
    elif broken_contract == "malformed_owner":
        owner["judge"] = "not-a-judge"
    elif broken_contract == "missing_boundary_owner":
        owner.pop("boundary_owner_id")
    elif broken_contract == "mismatched_boundary_owner":
        owner["boundary_owner_id"] = "entity_repair:2:16309:21820"
    elif broken_contract == "missing_correction_audit":
        audit.pop("final_review_audit")
    elif broken_contract == "malformed_correction_audit":
        audit["final_review_audit"]["schema_version"] = "invalid"
    elif broken_contract == "missing_correction_pass":
        review_audit.pop("correction_pass")
    elif broken_contract == "malformed_correction_pass":
        correction_pass["schema_version"] = "invalid"
    elif broken_contract == "unsupported_correction_status":
        correction_pass["status"] = "BLOCKED"
    elif broken_contract == "mismatched_declared_mutation":
        review_audit["correction_mutation_authority"][
            "validated_mutation_count"
        ] = 2
    elif broken_contract == "missing_mutation_authority":
        owner.pop("mutation_authority")
    elif broken_contract == "blocked_mutation_authority":
        owner["mutation_authority"]["status"] = "BLOCK"
    elif broken_contract == "mismatched_cue":
        owner["cue_indexes"] = [7]
    elif broken_contract == "mismatched_timing":
        owner["matched_start_ms"] += 1
    elif broken_contract == "mismatched_request":
        owner["request_sha256"] = "c" * 64
    elif broken_contract == "mismatched_evidence_id":
        owner["evidence_id"] = "c" * 64
    elif broken_contract == "mismatched_request_payload_hash":
        request["context_after"] += "被篡改"
    elif broken_contract == "mismatched_witness_request":
        witness["request_sha256"] = "c" * 64
    elif broken_contract == "mismatched_witness_timeline":
        witness["timeline_binding"]["delivery_local"][
            "target_start_ms"
        ] += 1
    elif broken_contract == "witness_not_fully_uncertain":
        witness["uncertain_positions"] = list(range(9))
    elif broken_contract == "non_tied_pinyin":
        owner["judge"]["candidate_pinyin_similarity"]["proposed"] = 0.99
    elif broken_contract == "orthography_authority_not_blocked":
        adjudication["orthography_authority"]["status"] = "PASS"
    elif broken_contract == "mismatched_before":
        owner["before"] = ["就别的名字拉布里二叔、七宝"]
    elif broken_contract == "mismatched_after":
        owner["after"] = ["就林墨拉布里二叔、别的名字"]
    elif broken_contract == "missing_canon_audit":
        audit.pop("final_expected_value_surface_audit")
    elif broken_contract == "malformed_canon_audit":
        canon_audit["schema_version"] = "invalid"
    elif broken_contract == "mismatched_canon_input_hash":
        canon_audit["input_srt_sha256"] = "c" * 64
    elif broken_contract == "non_mutating_canon_output_hash":
        canon_audit["output_srt_sha256"] = canon_audit["input_srt_sha256"]
    elif broken_contract == "mismatched_canon_cue":
        canon_repair["cue_index"] = 7
    elif broken_contract == "mismatched_canon_timing":
        canon_repair["matched_start_ms"] += 1
    elif broken_contract == "malformed_canon_replacement":
        canon_repair["replacements"][0]["count"] = 2
    elif broken_contract == "registered_canon_conflict":
        canon_audit["registered_name_conflicts"] = [{"surface": "林墨"}]
    elif broken_contract == "mismatched_final_output_hash":
        audit["final_output_srt_sha256"] = "c" * 64
    elif broken_contract == "malformed_reviewed_srt_hash":
        review_audit["reviewed_srt_sha256"] = "not-a-hash"
    elif broken_contract == "extra_final_text":
        final = final.replace(before_cpa, before_cpa + "额外")
        final_sha256 = hashlib.sha256(final.encode()).hexdigest()
        audit["final_output_srt_sha256"] = final_sha256
        review_audit["reviewed_srt_sha256"] = "sha256:" + final_sha256
    elif broken_contract == "extra_final_speaker_text":
        speaker_final = final.replace(before_cpa, before_cpa + "额外")
    elif broken_contract == "overlapping_final_text":
        final += "\n9\n00:00:07,000 --> 00:00:08,000\n额外\n"
        final_sha256 = hashlib.sha256(final.encode()).hexdigest()
        audit["final_output_srt_sha256"] = final_sha256
        review_audit["reviewed_srt_sha256"] = "sha256:" + final_sha256
    elif broken_contract == "overlapping_final_speaker_text":
        speaker_final = final + (
            "\n9\n00:00:07,000 --> 00:00:08,000\n[李豆沙] 额外\n"
        )
    else:  # pragma: no cover - parameter list is closed above
        raise AssertionError(broken_contract)

    if broken_contract not in {
        "extra_final_speaker_text",
        "overlapping_final_speaker_text",
    }:
        speaker_final = final
    assert not verify_chat_authority_final_surfaces(
        audit,
        final_text_srt=final,
        final_speaker_srt=speaker_final,
        delivery_start_ms=9_780,
        delivery_end_ms=25_000,
    )
    assert audit.get("final_superseded_by_expected_value_canon_count", 0) == 0
    assert "expected_value_canon_supersession" not in owner


def test_expected_value_canon_supersession_revalidates_stale_receipt() -> None:
    audit, final, owner, _before_cpa, _after_cpa = _expected_value_806_fixture()

    assert verify_chat_authority_final_surfaces(
        audit,
        final_text_srt=final,
        final_speaker_srt=final,
        delivery_start_ms=9_780,
        delivery_end_ms=25_000,
    )
    assert "expected_value_canon_supersession" in owner

    audit["final_expected_value_surface_audit"]["repairs"][0][
        "matched_start_ms"
    ] += 1
    assert not verify_chat_authority_final_surfaces(
        audit,
        final_text_srt=final,
        final_speaker_srt=final,
        delivery_start_ms=9_780,
        delivery_end_ms=25_000,
    )
    assert "expected_value_canon_supersession" not in owner
    assert audit["final_superseded_by_expected_value_canon_count"] == 0


def test_expected_value_canon_supersession_rejects_unrelated_same_cue_surface() -> None:
    audit, final, owner, _before_cpa, _after_cpa = _expected_value_806_fixture(
        before_cpa="林墨先来，就礼墨拉布里二叔、七宝",
        after_cpa="林墨先来，就林墨拉布里二叔、七宝",
    )

    assert "礼墨先来，就礼墨拉布里二叔、七宝" in final
    assert not verify_chat_authority_final_surfaces(
        audit,
        final_text_srt=final,
        final_speaker_srt=final,
        delivery_start_ms=9_780,
        delivery_end_ms=25_000,
    )
    assert "expected_value_canon_supersession" not in owner


@pytest.mark.parametrize(
    ("forbidden_surface", "failure"),
    (
        ("直女", "UNBYPASSABLE_HARD_MEME_SURFACE_PRESENT"),
        ("ore", "JAPANESE_ROMAJI_SURFACE_PRESENT"),
    ),
)
def test_expected_value_canon_supersession_does_not_bypass_final_canons(
    forbidden_surface: str,
    failure: str,
) -> None:
    audit, final, owner, before_cpa, _after_cpa = _expected_value_806_fixture()
    final = final.replace(before_cpa, before_cpa + forbidden_surface)

    assert not verify_chat_authority_final_surfaces(
        audit,
        final_text_srt=final,
        final_speaker_srt=final,
        delivery_start_ms=9_780,
        delivery_end_ms=25_000,
    )
    assert audit["final_verification_failure"] == failure
    assert "expected_value_canon_supersession" not in owner


def test_ordinary_post_context_truth_is_explicitly_context_only() -> None:
    audit = _audit(
        _truth_row(
            truth_id="ordinary-post-context",
            start_ms=11_000,
            end_ms=12_000,
            canonical="后续正确文本",
        )
    )
    final = _srt((0, 5_000, "当前故事完整收尾"))

    assert verify_chat_authority_final_surfaces(
        audit,
        final_text_srt=final,
        final_speaker_srt=final,
        delivery_start_ms=5_000,
        delivery_end_ms=10_000,
    )

    row = audit["source_subtitle_truth_audit"]["satisfied"][0]
    receipt = audit["final_source_truth_owner_verification"]
    assert row["final_owner_scope"] == ("CONTEXT_ONLY_OUTSIDE_FINAL_DELIVERY")
    assert row["final_owner_verified"] is False
    assert row["final_owner_windows"][0]["delivery_relation"] == ("OUTSIDE_AFTER_FINAL_DELIVERY")
    assert receipt["final_delivery_interval"] == {
        "timeline": "padded_source_local_ms",
        "interval_semantics": "half_open",
        "start_ms": 5_000,
        "end_ms": 10_000,
    }
    assert receipt["required_truth_row_count"] == 0
    assert receipt["context_only_truth_row_count"] == 1
    assert receipt["context_only_truth_ids"] == ["ordinary-post-context"]
    assert receipt["context_only_truth_evidence"] == [
        {
            "truth_id": "ordinary-post-context",
            "boundary_role": "story_content",
            "final_owner_scope": (
                "CONTEXT_ONLY_OUTSIDE_FINAL_DELIVERY"
            ),
            "final_owner_windows": row["final_owner_windows"],
        }
    ]
    assert receipt["required_window_count"] == 0


def test_ordinary_truth_inside_final_delivery_remains_required() -> None:
    row = _truth_row(
        truth_id="ordinary-story-owner",
        start_ms=7_000,
        end_ms=8_000,
        canonical="正文正确文本",
    )
    wrong = _srt((2_000, 3_000, "正文错误文本"))
    audit = _audit(row)

    assert not verify_chat_authority_final_surfaces(
        audit,
        final_text_srt=wrong,
        final_speaker_srt=wrong,
        delivery_start_ms=5_000,
        delivery_end_ms=10_000,
    )
    receipt = audit["final_source_truth_owner_verification"]
    assert receipt["required_truth_row_count"] == 1
    assert receipt["context_only_truth_row_count"] == 0
    assert "SOURCE_TRUTH_FINAL_CONTRACT_MISMATCH" in {
        failure["reason_code"] for failure in receipt["failures"]
    }


def test_contiguous_projected_truth_accepts_exact_final_recue_merge() -> None:
    """Release hygiene may merge two adjacent projected cues without drift."""

    row = _truth_row(
        truth_id="contiguous-recue-merge",
        start_ms=1_000,
        end_ms=4_280,
        canonical="切，那就差乙乙没吃了",
    )
    row.update(
        {
            "cue_indexes": [16, 17],
            "resolved_target_projection": {
                "schema_version": (
                    "source-truth-resolved-target-projection.v1"
                ),
                "selector": (
                    "half-open-overlap-gte-min-then-action-resolution"
                ),
                "min_overlap_ms": 80,
                "action": "replace_cue",
                "status": "RESOLVED",
                "cues": [
                    {
                        "cue_index": 16,
                        "start_ms": 1_000,
                        "end_ms": 1_720,
                        "before_text": "切，",
                        "after_text": "切，",
                    },
                    {
                        "cue_index": 17,
                        "start_ms": 1_720,
                        "end_ms": 4_280,
                        "before_text": "那就差乙乙没吃了",
                        "after_text": "那就差乙乙没吃了",
                    },
                ],
            },
        }
    )
    merged = _srt((1_000, 4_280, "切，那就差乙乙没吃了"))
    audit = _audit(row)

    assert verify_chat_authority_final_surfaces(
        audit,
        final_text_srt=merged,
        final_speaker_srt=merged,
        delivery_start_ms=0,
        delivery_end_ms=5_000,
    )
    verified = audit["source_subtitle_truth_audit"]["satisfied"][0]
    receipt = verified["final_owner_contract"]["recue_coalescence"]
    assert receipt["status"] == "PASS"
    assert receipt["projected_window_count"] == 2
    assert verified["final_owner_verified"] is True


def test_contiguous_projected_truth_recue_merge_rejects_extra_speech() -> None:
    row = _truth_row(
        truth_id="contiguous-recue-extra",
        start_ms=1_000,
        end_ms=4_280,
        canonical="切，那就差乙乙没吃了",
    )
    row.update(
        {
            "cue_indexes": [16, 17],
            "resolved_target_projection": {
                "schema_version": (
                    "source-truth-resolved-target-projection.v1"
                ),
                "selector": (
                    "half-open-overlap-gte-min-then-action-resolution"
                ),
                "min_overlap_ms": 80,
                "action": "replace_cue",
                "status": "RESOLVED",
                "cues": [
                    {
                        "cue_index": 16,
                        "start_ms": 1_000,
                        "end_ms": 1_720,
                        "before_text": "切，",
                        "after_text": "切，",
                    },
                    {
                        "cue_index": 17,
                        "start_ms": 1_720,
                        "end_ms": 4_280,
                        "before_text": "那就差乙乙没吃了",
                        "after_text": "那就差乙乙没吃了",
                    },
                ],
            },
        }
    )
    merged = _srt(
        (900, 4_400, "前句切，那就差乙乙没吃了后句")
    )
    audit = _audit(row)

    assert not verify_chat_authority_final_surfaces(
        audit,
        final_text_srt=merged,
        final_speaker_srt=merged,
        delivery_start_ms=0,
        delivery_end_ms=5_000,
    )
    verified = audit["source_subtitle_truth_audit"]["satisfied"][0]
    receipt = verified["final_owner_contract"]["recue_coalescence"]
    assert receipt["status"] == "FAIL"
    assert receipt["text_payload"] == (
        "前句切那就差乙乙没吃了后句"
    )


@pytest.mark.parametrize(
    ("start_ms", "end_ms", "expected_relation"),
    [
        (4_500, 5_500, "STRADDLES_FINAL_DELIVERY_START"),
        (9_500, 10_500, "STRADDLES_FINAL_DELIVERY_END"),
        (4_500, 10_500, "STRADDLES_BOTH_FINAL_DELIVERY_ENDPOINTS"),
    ],
)
def test_truth_straddling_final_endpoint_fails_closed(
    start_ms: int,
    end_ms: int,
    expected_relation: str,
) -> None:
    audit = _audit(
        _truth_row(
            truth_id="straddling-story-owner",
            start_ms=start_ms,
            end_ms=end_ms,
            canonical="不能靠残片放行",
        )
    )
    final = _srt((0, 5_000, "不能靠残片放行"))

    assert not verify_chat_authority_final_surfaces(
        audit,
        final_text_srt=final,
        final_speaker_srt=final,
        delivery_start_ms=5_000,
        delivery_end_ms=10_000,
    )
    receipt = audit["final_source_truth_owner_verification"]
    assert receipt["straddling_truth_row_count"] == 1
    assert receipt["failures"][0]["reason_code"] == ("SOURCE_TRUTH_FINAL_WINDOW_STRADDLES_DELIVERY")
    assert receipt["failures"][0]["windows"][0]["delivery_relation"] == expected_relation


def test_next_topic_witness_uses_interval_not_role_to_decide_ownership() -> None:
    outside = _truth_row(
        truth_id="next-topic-outside",
        start_ms=11_000,
        end_ms=12_000,
        canonical="后话题",
        boundary_role="next_topic_witness",
    )
    inside = _truth_row(
        truth_id="next-topic-inside",
        start_ms=7_000,
        end_ms=8_000,
        canonical="误裁进来的后话题",
        boundary_role="next_topic_witness",
    )
    final = _srt((2_000, 3_000, "错误表面"))

    outside_audit = _audit(outside)
    assert verify_chat_authority_final_surfaces(
        outside_audit,
        final_text_srt=final,
        final_speaker_srt=final,
        delivery_start_ms=5_000,
        delivery_end_ms=10_000,
    )
    assert (
        outside_audit["final_source_truth_owner_verification"]["context_only_truth_row_count"] == 1
    )

    inside_audit = _audit(inside)
    assert not verify_chat_authority_final_surfaces(
        inside_audit,
        final_text_srt=final,
        final_speaker_srt=final,
        delivery_start_ms=5_000,
        delivery_end_ms=10_000,
    )
    assert inside_audit["final_source_truth_owner_verification"]["required_truth_row_count"] == 1


def test_context_only_truth_does_not_relax_baseline_final_surface() -> None:
    audit = _audit(
        _truth_row(
            truth_id="ordinary-post-context",
            start_ms=11_000,
            end_ms=12_000,
            canonical="后续正确文本",
        ),
        baseline={
            "status": "APPLIED",
            "mappings": [
                {
                    "baseline_cue_index": 1,
                    "start_ms": 0,
                    "end_ms": 1_000,
                    "text": "人工审定正文",
                }
            ],
        },
    )
    wrong = _srt((0, 1_000, "被错误改写的正文"))

    assert not verify_chat_authority_final_surfaces(
        audit,
        final_text_srt=wrong,
        final_speaker_srt=wrong,
        delivery_start_ms=5_000,
        delivery_end_ms=10_000,
    )
    assert audit["final_source_truth_owner_verification"]["status"] == "PASS"
    assert audit["final_redelivery_baseline_owner_verification"]["status"] == "FAIL"
    assert audit["final_verification_failure"] == ("REDELIVERY_BASELINE_FINAL_OWNER_NOT_VERIFIED")


def test_native_script_canon_preserves_redelivery_baseline_owner() -> None:
    audit = _audit(
        baseline={
            "status": "APPLIED",
            "mappings": [
                {
                    "baseline_cue_index": 1,
                    "start_ms": 0,
                    "end_ms": 1_000,
                    "text": "TA要是boku，不过不是ore",
                }
            ],
        },
    )
    final_text = _srt((0, 1_000, "TA要是ぼく，不过不是おれ"))
    final_speaker = _srt((0, 1_000, f"[{HOST_SPEAKER}] TA要是ぼく，不过不是おれ"))

    assert verify_chat_authority_final_surfaces(
        audit,
        final_text_srt=final_text,
        final_speaker_srt=final_speaker,
        delivery_start_ms=0,
        delivery_end_ms=1_000,
    )

    row = audit["redelivery_subtitle_baseline_audit"]["mappings"][0]
    assert row["final_owner_expected"] == "ta要是ぼく不过不是おれ"
    assert row["final_owner_scope"] == (
        "TRANSFORMED_BY_JAPANESE_NATIVE_SCRIPT_CANON"
    )
    assert {
        replacement["surface"]
        for replacement in row[
            "final_owner_japanese_native_script_replacements"
        ]
    } == {"boku", "ore"}
    assert audit["final_redelivery_baseline_owner_verification"]["status"] == (
        "PASS"
    )


def test_native_script_canon_does_not_relax_unrelated_baseline_change() -> None:
    audit = _audit(
        baseline={
            "status": "APPLIED",
            "mappings": [
                {
                    "baseline_cue_index": 1,
                    "start_ms": 0,
                    "end_ms": 1_000,
                    "text": "TA要是boku",
                }
            ],
        },
    )
    wrong = _srt((0, 1_000, "TA要是おれ"))

    assert not verify_chat_authority_final_surfaces(
        audit,
        final_text_srt=wrong,
        final_speaker_srt=wrong,
        delivery_start_ms=0,
        delivery_end_ms=1_000,
    )
    failure = audit["final_redelivery_baseline_owner_verification"][
        "failures"
    ][0]
    assert failure["expected"] == "ta要是ぼく"
    assert failure["text_payload"] == "ta要是おれ"


def test_hash_bound_release_grade_merge_preserves_baseline_owner() -> None:
    final = _srt(
        (0, 880, "哦，这样吗"),
        (2_000, 4_560, "嘻，晓得吧，行"),
    )
    final_hash = hashlib.sha256(final.encode("utf-8")).hexdigest()
    receipts = [
        {"block": 1, "text": "哦", "action": "MERGED_INTO_NEXT"},
        {"block": 3, "text": "行", "action": "MERGED_INTO_PREV"},
    ]
    audit = _audit(
        baseline={
            "status": "APPLIED",
            "mappings": [
                {
                    "baseline_cue_index": 1,
                    "start_ms": 0,
                    "end_ms": 240,
                    "text": "哦",
                },
                {
                    "baseline_cue_index": 2,
                    "start_ms": 240,
                    "end_ms": 880,
                    "text": "这样吗",
                },
                {
                    "baseline_cue_index": 3,
                    "start_ms": 2_000,
                    "end_ms": 3_480,
                    "text": "嘻，晓得吧",
                },
                {
                    "baseline_cue_index": 4,
                    "start_ms": 3_560,
                    "end_ms": 4_560,
                    "text": "行",
                },
            ],
            "final_release_grade_cue_merges": deepcopy(receipts),
            "post_release_grade_output_sha256": final_hash,
        },
    )
    audit["final_release_grade_cue_merges"] = deepcopy(receipts)
    audit["final_output_srt_sha256"] = final_hash
    audit["final_verification_failure"] = (
        "REDELIVERY_BASELINE_FINAL_OWNER_NOT_VERIFIED"
    )

    assert verify_chat_authority_final_surfaces(
        audit,
        final_text_srt=final,
        final_speaker_srt=final,
        delivery_start_ms=0,
        delivery_end_ms=5_000,
    )
    receipt = audit["final_redelivery_baseline_owner_verification"]
    assert receipt["status"] == "PASS"
    assert "final_verification_failure" not in audit
    assert receipt["release_grade_merge_group_count"] == 2
    assert all(
        row["final_owner_scope"]
        == "DETERMINISTIC_RELEASE_GRADE_CUE_MERGE"
        for row in audit["redelivery_subtitle_baseline_audit"]["mappings"]
    )


@pytest.mark.parametrize("tamper", ["hash", "receipt", "extra_text"])
def test_release_grade_merge_owner_receipt_fails_closed(tamper: str) -> None:
    final = _srt((0, 880, "哦，这样吗"))
    final_hash = hashlib.sha256(final.encode("utf-8")).hexdigest()
    receipts = [
        {"block": 1, "text": "哦", "action": "MERGED_INTO_NEXT"},
    ]
    audit = _audit(
        baseline={
            "status": "APPLIED",
            "mappings": [
                {
                    "baseline_cue_index": 1,
                    "start_ms": 0,
                    "end_ms": 240,
                    "text": "哦",
                },
                {
                    "baseline_cue_index": 2,
                    "start_ms": 240,
                    "end_ms": 880,
                    "text": "这样吗",
                },
            ],
            "final_release_grade_cue_merges": deepcopy(receipts),
            "post_release_grade_output_sha256": final_hash,
        },
    )
    audit["final_release_grade_cue_merges"] = deepcopy(receipts)
    audit["final_output_srt_sha256"] = final_hash
    if tamper == "hash":
        audit["redelivery_subtitle_baseline_audit"][
            "post_release_grade_output_sha256"
        ] = "0" * 64
    elif tamper == "receipt":
        audit["final_release_grade_cue_merges"][0]["action"] = (
            "MERGED_INTO_PREV"
        )
    else:
        final = _srt((0, 880, "哦，这样吗，额外词"))

    assert not verify_chat_authority_final_surfaces(
        audit,
        final_text_srt=final,
        final_speaker_srt=final,
        delivery_start_ms=0,
        delivery_end_ms=1_000,
    )
    assert audit["final_verification_failure"] == (
        "REDELIVERY_BASELINE_FINAL_OWNER_NOT_VERIFIED"
    )
