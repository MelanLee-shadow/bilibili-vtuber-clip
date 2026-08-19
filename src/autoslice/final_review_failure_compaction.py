"""Compact exact-final evidence retained on failed talk records."""

from __future__ import annotations

import re
from collections.abc import Mapping

from src.autoslice import provider_failure


def compact_final_review_finding(row: object) -> dict[str, object] | None:
    if not isinstance(row, dict):
        return None
    summary = {
        key: row.get(key)
        for key in (
            "cue_index",
            "kind",
            "repair_class",
            "suspect",
            "suggestion",
            "proposed_full_cue",
            "suggestion_rejected_reason",
            "why",
            "force_acoustic",
            "correlated_text_witness",
            "base_text_sha256",
            "candidate_memory_id",
            "evidence_cue_ids",
            "reported_scope_warnings",
            "span_start_codepoint",
            "span_end_codepoint",
        )
        if row.get(key) is not None
    }
    provenance = row.get("candidate_provenance")
    if isinstance(provenance, dict):
        summary["candidate_provenance"] = {
            key: provenance.get(key)
            for key in (
                "schema_version",
                "kind",
                "scope",
                "surface",
                "memory_id",
                "mutation_authorized",
                "ledger_sha256",
                "nearest_cue_distance",
                "session_scope_id",
                "source_srt_sha256",
                "current_srt_sha256",
                "occurrence_count",
                "occurrence_cue_count",
                "occurrence_positions",
                "max_cue_distance",
                "global_glossary_authorized",
                "draft_fidelity_kept",
                "kept_candidate",
                "current_text_sha256",
                "audit_sha256",
                "kept_text_sha256",
                "violation_reason_codes",
            )
            if provenance.get(key) is not None
        }
    fidelity_context = row.get("draft_fidelity_kept_provenance")
    if isinstance(fidelity_context, dict):
        summary["draft_fidelity_kept_provenance"] = {
            key: fidelity_context.get(key)
            for key in (
                "schema_version",
                "kind",
                "scope",
                "cue_index",
                "surface",
                "draft_fidelity_kept",
                "kept_candidate",
                "audit_sha256",
                "source_srt_sha256",
                "kept_text_sha256",
                "current_text_sha256",
                "violation_reason_codes",
                "mutation_authorized",
            )
            if fidelity_context.get(key) is not None
        }
    adjudication = row.get("exact_release_adjudication")
    if isinstance(adjudication, Mapping):
        summary["exact_release_adjudication"] = {
            key: adjudication.get(key)
            for key in (
                "schema_version",
                "status",
                "repaired",
                "provider_adjudication_count",
                "provider_adjudication_budget",
            )
            if adjudication.get(key) is not None
        }
    return summary


def compact_boundary_semantic_review(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        return {"status": "MISSING"}
    return {
        key: value.get(key)
        for key in (
            "schema_version",
            "status",
            "reason_codes",
            "target_ms",
            "recommended_end_ms",
            "syntax_complete",
            "story_closed",
            "content_anchor_covered",
            "next_topic_separated",
            "summary",
            "request_sha256",
        )
        if value.get(key) is not None
    }


def final_review_contract_reason_code(attempt_output: str) -> str | None:
    match = re.search(
        r"FINAL_REVIEW_RELEASE_BLOCKED:\s*([A-Z0-9_]+)",
        attempt_output,
    )
    return match.group(1) if match else None


def compact_correction_pass(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping):
        return {"status": "MISSING"}
    reason_codes = value.get("reason_codes")
    if isinstance(reason_codes, str):
        normalized_reason_codes = [reason_codes] if reason_codes else []
    elif isinstance(reason_codes, list):
        normalized_reason_codes = [
            str(code) for code in reason_codes if str(code)
        ]
    else:
        normalized_reason_codes = []
    discovery = value.get("discovery")
    return {
        key: item
        for key, item in {
            "schema_version": value.get("schema_version"),
            "status": value.get("status"),
            "reason_codes": normalized_reason_codes,
            "discovery": (
                {
                    field: discovery.get(field)
                    for field in provider_failure.DISCOVERY_EVIDENCE_FIELDS
                    if discovery.get(field) is not None
                }
                if isinstance(discovery, Mapping)
                else {"status": "MISSING"}
            ),
            "error_type": value.get("error_type"),
            "applied_count": value.get("applied_count"),
        }.items()
        if item is not None
    }


def compact_correction_mutation_authority(
    value: object,
) -> dict[str, object]:
    if not isinstance(value, Mapping):
        return {"status": "MISSING", "failures": []}
    compact_failures: list[dict[str, object]] = []
    for row in value.get("failures") or []:
        if not isinstance(row, Mapping):
            continue
        compact_failures.append(
            {
                key: row.get(key)
                for key in (
                    "reason_code",
                    "upstream_reason_codes",
                    "cue_index",
                    "routed",
                )
                if row.get(key) is not None
            }
        )
    return {
        key: item
        for key, item in {
            "schema_version": value.get("schema_version"),
            "status": value.get("status"),
            "applied_count": value.get("applied_count"),
            "validated_mutation_count": value.get(
                "validated_mutation_count"
            ),
            "failures": compact_failures,
        }.items()
        if item is not None
    }
