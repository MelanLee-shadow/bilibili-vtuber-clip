"""Typed retirement of a CPA pronunciation choice by deterministic canon."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping

from .acoustic_witness_adjudication import (
    build_witness_request,
    valid_witness_evidence,
)
from .chat_evidence import normalize_chat_text
from .chat_evidence import normalize_srt_owner_payload_window
from .final_review_auditor import audit_correction_mutation_authority
from .jingting_chunker import parse_srt_cues
from .surface_canon import canonicalize_expected_value_surfaces


_SHA256_RX = re.compile(r"^(?:sha256:)?[0-9a-f]{64}$")


def _digest(value: object) -> str:
    return str(value or "").removeprefix("sha256:")


def _valid_sha256(value: object) -> bool:
    return isinstance(value, str) and _SHA256_RX.fullmatch(value) is not None


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _sha256_json(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _request_sha256(request: Mapping[str, object]) -> str:
    return _sha256_json({key: value for key, value in request.items() if key != "request_sha256"})


def _single_span_edit_matches(finding: Mapping[str, object], *, before: str, after: str) -> bool:
    prefix = 0
    while prefix < len(before) and prefix < len(after) and before[prefix] == after[prefix]:
        prefix += 1
    suffix = 0
    while (
        suffix < len(before) - prefix
        and suffix < len(after) - prefix
        and before[len(before) - suffix - 1] == after[len(after) - suffix - 1]
    ):
        suffix += 1
    before_end = len(before) - suffix if suffix else len(before)
    after_end = len(after) - suffix if suffix else len(after)
    suspect = before[prefix:before_end]
    suggestion = after[prefix:after_end]
    return bool(
        suspect
        and suggestion
        and finding.get("span_start_codepoint") == prefix
        and finding.get("span_end_codepoint") == before_end
        and finding.get("suspect") == suspect
        and finding.get("suggestion") == suggestion
    )


def _blind_witness_binding_valid(
    *,
    request: Mapping[str, object],
    witness: Mapping[str, object],
    witness_judge: Mapping[str, object],
    judge: Mapping[str, object],
) -> bool:
    try:
        witness_request = build_witness_request(request)
    except (KeyError, TypeError, ValueError):
        return False
    witness_request_sha256 = str(witness_request.get("request_sha256") or "")
    timeline = witness.get("timeline_binding")
    delivery_timeline = timeline.get("delivery_local") if isinstance(timeline, Mapping) else None
    source_timeline = timeline.get("source_media") if isinstance(timeline, Mapping) else None
    offset = request.get("source_media_timeline_offset_ms")
    matched_start = request.get("matched_start_ms")
    matched_end = request.get("matched_end_ms")
    confidence = witness.get("confidence")
    syllable_count = witness.get("syllable_count")
    uncertain_positions = witness.get("uncertain_positions")
    current_similarity = (
        judge.get("candidate_pinyin_similarity")
        if isinstance(judge.get("candidate_pinyin_similarity"), Mapping)
        else None
    )
    choice_set = judge.get("choice_set")
    return bool(
        valid_witness_evidence(
            witness,
            request_sha256=witness_request_sha256,
        )
        and request.get("kind") == "subtitle_span_acoustic_check"
        and request.get("repair_class") == "phonetic"
        and isinstance(offset, int)
        and not isinstance(offset, bool)
        and isinstance(matched_start, int)
        and not isinstance(matched_start, bool)
        and isinstance(matched_end, int)
        and not isinstance(matched_end, bool)
        and _digest(witness.get("request_sha256")) == _digest(witness_request_sha256)
        and all(
            _valid_sha256(witness.get(field))
            for field in (
                "source_media_sha256",
                "audio_clip_sha256",
                "prompt_sha256",
                "response_sha256",
            )
        )
        and isinstance(witness.get("provider"), str)
        and bool(witness.get("provider"))
        and isinstance(witness.get("model"), str)
        and bool(witness.get("model"))
        and isinstance(confidence, (int, float))
        and not isinstance(confidence, bool)
        and 0.0 <= float(confidence) <= 0.4
        and isinstance(syllable_count, int)
        and not isinstance(syllable_count, bool)
        and syllable_count > 0
        and uncertain_positions == list(range(syllable_count))
        and witness.get("self_count_mismatch") is False
        and isinstance(timeline, Mapping)
        and timeline.get("schema_version") == "subtitle-audio-timeline-binding.v1"
        and timeline.get("source_media_timeline_offset_ms") == offset
        and isinstance(delivery_timeline, Mapping)
        and delivery_timeline.get("target_start_ms") == matched_start
        and delivery_timeline.get("target_end_ms") == matched_end
        and delivery_timeline.get("context_start_ms") == request.get("context_start_ms")
        and delivery_timeline.get("context_end_ms") == request.get("context_end_ms")
        and isinstance(source_timeline, Mapping)
        and source_timeline.get("target_start_ms") == matched_start + offset
        and source_timeline.get("target_end_ms") == matched_end + offset
        and source_timeline.get("crop_start_ms") == witness.get("audio_start_ms")
        and source_timeline.get("crop_end_ms") == witness.get("audio_end_ms")
        and isinstance(current_similarity, Mapping)
        and isinstance(current_similarity.get("current"), (int, float))
        and not isinstance(current_similarity.get("current"), bool)
        and isinstance(current_similarity.get("proposed"), (int, float))
        and not isinstance(current_similarity.get("proposed"), bool)
        and current_similarity.get("current") == current_similarity.get("proposed")
        and judge.get("decision_contract") == "current-proposed-neither.v1"
        and isinstance(choice_set, list)
        and all(isinstance(choice, str) for choice in choice_set)
        and set(choice_set) == {"CURRENT", "PROPOSED", "NEITHER"}
        and witness_judge.get("witness_protocol") == "blind_pinyin"
        and _digest(witness_judge.get("witness_request_sha256")) == _digest(witness_request_sha256)
    )


def _matching_correction_finding(
    *,
    owner: Mapping[str, object],
    correction: Mapping[str, object],
    declared_mutation_audit: Mapping[str, object],
    before: str,
    after: str,
    cue_index: int,
    start_ms: int,
    end_ms: int,
) -> Mapping[str, object] | None:
    applied_count = correction.get("applied_count")
    if not (
        correction.get("schema_version") == "final-review-audit.v1"
        and correction.get("status") in {"APPLIED", "PARTIAL"}
        and isinstance(applied_count, int)
        and not isinstance(applied_count, bool)
        and applied_count > 0
    ):
        return None
    try:
        mutation_audit = audit_correction_mutation_authority(correction)
    except (KeyError, TypeError, ValueError):
        return None
    if not (
        mutation_audit.get("schema_version") == "subtitle-correction-mutation-audit.v1"
        and mutation_audit.get("status") == "PASS"
        and mutation_audit.get("applied_count") == applied_count
        and mutation_audit.get("validated_mutation_count") == applied_count
        and mutation_audit.get("failures") == []
        and dict(declared_mutation_audit) == mutation_audit
    ):
        return None
    findings = correction.get("findings")
    if not isinstance(findings, list):
        return None
    matches: list[Mapping[str, object]] = []
    for finding in findings:
        if not isinstance(finding, Mapping):
            continue
        adjudication = finding.get("context_audio_adjudication")
        if not isinstance(adjudication, Mapping):
            continue
        request = adjudication.get("request")
        witness = adjudication.get("verdict")
        witness_judge = adjudication.get("witness_judge")
        mutation = adjudication.get("mutation_authority")
        judge = witness_judge.get("judge") if isinstance(witness_judge, Mapping) else None
        if not all(
            isinstance(value, Mapping)
            for value in (request, witness, witness_judge, mutation, judge)
        ):
            continue
        request_sha256 = _digest(owner.get("request_sha256"))
        try:
            recomputed_request_sha256 = _request_sha256(request)
        except (TypeError, ValueError):
            continue
        request_candidates = request.get("candidate_entities")
        expected_candidates = [
            {
                "candidate_id": "CURRENT",
                "canonical": before,
                "text_sha256": _sha256_text(before),
                "surfaces": [],
                "readings": [],
            },
            {
                "candidate_id": "PROPOSED",
                "canonical": after,
                "text_sha256": _sha256_text(after),
                "surfaces": [],
                "readings": [],
            },
        ]
        orthography = adjudication.get("orthography_authority")
        request_orthography = request.get("orthography_authority")
        candidate_provenance = finding.get("candidate_provenance")
        structured_evidence = request.get("closed_set_structured_evidence")
        if not _blind_witness_binding_valid(
            request=request,
            witness=witness,
            witness_judge=witness_judge,
            judge=judge,
        ):
            continue
        if (
            finding.get("routed")
            not in {
                "context_audio_adjudicated_fix",
                "same_cue_readjudicated_fix",
            }
            or finding.get("cue_index") != cue_index
            or finding.get("base_text_sha256") != _sha256_text(before)
            or finding.get("proposed_full_cue") != after
            or finding.get("repair_class") != "phonetic"
            or not _single_span_edit_matches(
                finding,
                before=before,
                after=after,
            )
            or adjudication.get("schema_version") != "subtitle-span-adjudication.v1"
            or adjudication.get("status") != "OBSERVED"
            or adjudication.get("repaired") is not True
            or adjudication.get("policy_branch") != "WITNESS_JUDGE_APPLY_PROPOSED"
            or adjudication.get("timing_immutable") is not True
            or adjudication.get("decision_authority") != "CPA_JUDGE"
            or adjudication.get("witness_authority") != "EVIDENCE_ONLY"
            or adjudication.get("orthography_ambiguous") is not False
            or adjudication.get("orthography_equivalence") != {"matched": False}
            or not isinstance(orthography, Mapping)
            or dict(orthography)
            != {
                "schema_version": "subtitle-orthography-authority.v1",
                "status": "BLOCK",
                "provenance_kind": "draft_fidelity_kept",
                "reason_code": "ORTHOGRAPHY_TEXT_AUTHORITY_REQUIRED",
            }
            or not isinstance(candidate_provenance, Mapping)
            or candidate_provenance.get("schema_version") != "draft-fidelity-kept-candidate.v1"
            or candidate_provenance.get("kind") != "draft_fidelity_kept"
            or candidate_provenance.get("mutation_authorized") is not False
            or candidate_provenance.get("kept_candidate") != "PROPOSED"
            or candidate_provenance.get("cue_index") != cue_index
            or candidate_provenance.get("start_ms") != start_ms
            or candidate_provenance.get("end_ms") != end_ms
            or _digest(candidate_provenance.get("current_text_sha256")) != _sha256_text(before)
            or _digest(candidate_provenance.get("kept_text_sha256")) != _sha256_text(after)
            or request.get("schema_version") != "subtitle-span-acoustic-check-request.v1"
            or recomputed_request_sha256 != request_sha256
            or request.get("evidence_id") != owner.get("evidence_id")
            or not _valid_sha256(request.get("evidence_id"))
            or request.get("cue_indexes") != [cue_index]
            or request.get("base_text_sha256") != _sha256_text(before)
            or request.get("matched_start_ms") != start_ms
            or request.get("matched_end_ms") != end_ms
            or request.get("matched_audio_text") != before
            or request.get("current_cue") != before
            or request.get("proposed_cue") != after
            or request.get("suspect") != finding.get("suspect")
            or request.get("replacement") != finding.get("suggestion")
            or request.get("repair_class") != finding.get("repair_class")
            or request_candidates != expected_candidates
            or request.get("candidate_provenance") != candidate_provenance
            or request_orthography != orthography
            or not isinstance(structured_evidence, Mapping)
            or witness_judge.get("closed_set_structured_evidence") != structured_evidence
            or judge.get("closed_set_structured_evidence") != structured_evidence
            or witness_judge.get("candidate_pinyin_similarity")
            != judge.get("candidate_pinyin_similarity")
            or _digest(request.get("request_sha256")) != request_sha256
            or witness.get("schema_version") != "subtitle-span-acoustic-witness.v1"
            or witness.get("status") != "OBSERVED"
            or witness.get("witness_protocol") != "blind_pinyin"
            or witness.get("target_audible") is not True
            or not _valid_sha256(witness.get("request_sha256"))
            or witness_judge.get("schema_version") != "acoustic-witness-adjudication.v1"
            or witness_judge.get("witness_status") != "OBSERVED"
            or witness_judge.get("decision_authority") != "CPA_JUDGE"
            or witness_judge.get("witness_authority") != "EVIDENCE_ONLY"
            or witness_judge.get("witness_diagnostic_conflict") is not False
            or judge.get("schema_version") != "acoustic-witness-adjudication.v1"
            or judge.get("status") != "JUDGED"
            or judge.get("choice") != "PROPOSED"
            or _digest(judge.get("check_request_sha256")) != request_sha256
            or not _valid_sha256(judge.get("prompt_sha256"))
            or not _valid_sha256(judge.get("completion_sha256"))
            or dict(judge) != dict(owner.get("judge") or {})
            or dict(witness) != dict(owner.get("acoustic_witness") or {})
            or dict(witness) != dict(owner.get("verdict") or {})
            or dict(mutation) != dict(owner.get("mutation_authority") or {})
        ):
            continue
        matches.append(finding)
    return matches[0] if len(matches) == 1 else None


def _matching_canon_repair(
    *,
    audit: Mapping[str, object],
    before: str,
    after: str,
    cue_index: int,
    start_ms: int,
    end_ms: int,
) -> Mapping[str, object] | None:
    hard_audit = audit.get("final_hard_meme_surface_audit")
    canon_audit = audit.get("final_expected_value_surface_audit")
    if not isinstance(hard_audit, Mapping) or not isinstance(canon_audit, Mapping):
        return None
    input_sha256 = _digest(canon_audit.get("input_srt_sha256"))
    output_sha256 = _digest(canon_audit.get("output_srt_sha256"))
    if not (
        hard_audit.get("schema_version") == "hard-meme-surface-audit.v1"
        and hard_audit.get("status") in {"NO_CHANGE", "APPLIED"}
        and _valid_sha256(hard_audit.get("input_srt_sha256"))
        and _valid_sha256(hard_audit.get("output_srt_sha256"))
        and _digest(hard_audit.get("output_srt_sha256")) == input_sha256
        and canon_audit.get("schema_version") == "expected-value-surface-audit.v1"
        and canon_audit.get("status") == "APPLIED"
        and canon_audit.get("decision_authority") == "EXPECTED_VALUE_CANON"
        and canon_audit.get("registered_name_conflicts") == []
        and all(
            _valid_sha256(value)
            for value in (
                canon_audit.get("input_srt_sha256"),
                canon_audit.get("output_srt_sha256"),
            )
        )
        and input_sha256 != output_sha256
    ):
        return None
    repairs = canon_audit.get("repairs")
    if not isinstance(repairs, list) or not all(isinstance(repair, Mapping) for repair in repairs):
        return None
    expected_after, expected_replacements = canonicalize_expected_value_surfaces(before)
    if expected_after != after or len(expected_replacements) != 1:
        return None
    replacement = expected_replacements[0]
    if replacement.get("count") != 1:
        return None
    matches = [
        repair
        for repair in repairs
        if repair.get("cue_index") == cue_index
        and repair.get("matched_start_ms") == start_ms
        and repair.get("matched_end_ms") == end_ms
        and repair.get("before") == before
        and repair.get("after") == after
        and repair.get("decision_authority") == "EXPECTED_VALUE_CANON"
        and repair.get("replacements") == expected_replacements
    ]
    return matches[0] if len(matches) == 1 else None


def expected_value_canon_supersession_receipt(
    row: Mapping[str, object],
    *,
    audit: Mapping[str, object],
    final_text_srt: str,
    final_speaker_srt: str,
    delivery_start_ms: int,
) -> dict[str, object] | None:
    """Retire only one exact CPA spelling reversal proven by canon receipts."""

    before_rows = row.get("before")
    after_rows = row.get("after")
    mutation = row.get("mutation_authority")
    judge = row.get("judge")
    witness = row.get("acoustic_witness")
    verdict = row.get("verdict")
    cue_indexes = row.get("cue_indexes")
    entity_rows = audit.get("entity_repairs")
    owner_positions = (
        [index for index, candidate in enumerate(entity_rows, start=1) if candidate is row]
        if isinstance(entity_rows, list)
        else []
    )
    if not (
        row.get("mode") == "final_review_context_adjudication"
        and row.get("action") == "REPLACE_CUE_TEXT"
        and row.get("decision_authority") == "CPA_JUDGE"
        and row.get("policy_branch") == "WITNESS_JUDGE_APPLY_PROPOSED"
        and row.get("timing_immutable") is True
        and row.get("survived") is True
        and isinstance(mutation, Mapping)
        and dict(mutation)
        == {
            "schema_version": "subtitle-correction-mutation-authority.v1",
            "status": "PASS",
            "basis": "CPA_ACOUSTIC_PRONUNCIATION_DISAMBIGUATION",
        }
        and isinstance(judge, Mapping)
        and isinstance(witness, Mapping)
        and isinstance(verdict, Mapping)
        and isinstance(cue_indexes, list)
        and len(cue_indexes) == 1
        and isinstance(cue_indexes[0], int)
        and not isinstance(cue_indexes[0], bool)
        and isinstance(before_rows, list)
        and len(before_rows) == 1
        and isinstance(before_rows[0], str)
        and isinstance(after_rows, list)
        and len(after_rows) == 1
        and isinstance(after_rows[0], str)
        and row.get("structured_exact_text") == after_rows[0]
        and _valid_sha256(row.get("request_sha256"))
        and row.get("repair_class") == "phonetic"
        and owner_positions
        and len(owner_positions) == 1
    ):
        return None
    start_ms = row.get("matched_start_ms")
    end_ms = row.get("matched_end_ms")
    if not (
        isinstance(start_ms, int)
        and not isinstance(start_ms, bool)
        and isinstance(end_ms, int)
        and not isinstance(end_ms, bool)
        and 0 <= start_ms < end_ms
    ):
        return None
    cue_index = cue_indexes[0]
    owner_before = before_rows[0]
    owner_after = after_rows[0]
    if row.get("boundary_owner_id") != (f"entity_repair:{owner_positions[0]}:{start_ms}:{end_ms}"):
        return None
    final_output_sha256 = _digest(audit.get("final_output_srt_sha256"))
    final_text_sha256 = _sha256_text(final_text_srt)
    final_review = audit.get("final_review_audit")
    if not isinstance(final_review, Mapping):
        return None
    correction = final_review.get("correction_pass")
    declared_mutation_audit = final_review.get("correction_mutation_authority")
    if not (
        final_review.get("schema_version") == "final-review-audit.v2"
        and final_review.get("status") == "CLEAN"
        and final_review.get("release_gate") == "PASS"
        and _digest(final_review.get("reviewed_srt_sha256")) == final_text_sha256
        and _valid_sha256(final_review.get("reviewed_srt_sha256"))
        and final_output_sha256 == final_text_sha256
        and _valid_sha256(audit.get("final_output_srt_sha256"))
        and isinstance(correction, Mapping)
        and isinstance(declared_mutation_audit, Mapping)
    ):
        return None
    finding = _matching_correction_finding(
        owner=row,
        correction=correction,
        declared_mutation_audit=declared_mutation_audit,
        before=owner_before,
        after=owner_after,
        cue_index=cue_index,
        start_ms=start_ms,
        end_ms=end_ms,
    )
    canon_repair = _matching_canon_repair(
        audit=audit,
        before=owner_after,
        after=owner_before,
        cue_index=cue_index,
        start_ms=start_ms,
        end_ms=end_ms,
    )
    if finding is None or canon_repair is None:
        return None
    local_start = start_ms - delivery_start_ms
    local_end = end_ms - delivery_start_ms
    text_cues = [
        cue
        for cue in parse_srt_cues(final_text_srt)
        if cue.start_ms == local_start and cue.end_ms == local_end
    ]
    speaker_cues = [
        cue
        for cue in parse_srt_cues(final_speaker_srt)
        if cue.start_ms == local_start and cue.end_ms == local_end
    ]
    if len(text_cues) != 1 or len(speaker_cues) != 1:
        return None
    speaker_payload = normalize_chat_text(
        normalize_srt_owner_payload_window(
            final_speaker_srt,
            start_ms=local_start,
            end_ms=local_end,
            min_overlap_ms=1,
            strip_speaker_labels=True,
        )
    )
    text_payload = normalize_chat_text(
        normalize_srt_owner_payload_window(
            final_text_srt,
            start_ms=local_start,
            end_ms=local_end,
            min_overlap_ms=1,
            strip_speaker_labels=False,
        )
    )
    normalized_before = normalize_chat_text(owner_before)
    if (
        text_cues[0].text != owner_before
        or text_payload != normalized_before
        or speaker_payload != normalized_before
    ):
        return None
    canon_audit = audit["final_expected_value_surface_audit"]
    assert isinstance(canon_audit, Mapping)
    return {
        "schema_version": "expected-value-canon-entity-supersession.v1",
        "status": "SUPERSEDED_BY_EXPECTED_VALUE_CANON",
        "decision_authority": "EXPECTED_VALUE_CANON",
        "superseded_decision_authority": "CPA_JUDGE",
        "cue_index": cue_index,
        "matched_start_ms": start_ms,
        "matched_end_ms": end_ms,
        "final_relative_start_ms": local_start,
        "final_relative_end_ms": local_end,
        "cpa_request_sha256": "sha256:" + _digest(row["request_sha256"]),
        "correction_finding_sha256": "sha256:" + _sha256_json(dict(finding)),
        "canon_repair_sha256": "sha256:" + _sha256_json(dict(canon_repair)),
        "canon_input_srt_sha256": "sha256:" + _digest(canon_audit["input_srt_sha256"]),
        "canon_output_srt_sha256": "sha256:" + _digest(canon_audit["output_srt_sha256"]),
        "final_output_srt_sha256": "sha256:" + final_output_sha256,
        "owner_before_sha256": "sha256:" + _sha256_text(owner_before),
        "owner_after_sha256": "sha256:" + _sha256_text(owner_after),
        "final_text_cue_sha256": "sha256:" + _sha256_text(text_cues[0].text),
        "final_text_payload_sha256": "sha256:" + _sha256_text(text_payload),
        "final_speaker_payload_sha256": "sha256:" + _sha256_text(speaker_payload),
        "replacements": list(canon_repair["replacements"]),
        "timing_immutable": True,
    }
