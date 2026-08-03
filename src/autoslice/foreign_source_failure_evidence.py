"""Typed failure evidence for blocked foreign-source subtitle authority."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path


def foreign_source_gate_violation(
    attempt_output: str,
) -> dict[str, object] | None:
    """Recover the actual still-blocked audit and its exact cue evidence."""

    matches = re.findall(
        r"FOREIGN_SOURCE_TRANSCRIPTION_REQUIRED(?:_AFTER_REDELIVERY)?:\s*([^\s]+\.chat-authority\.json)",
        attempt_output,
    )
    if not matches:
        return None
    path = Path(matches[-1])
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    source_audit = document.get("final_source_language_preservation_audit")
    foreign_audit = document.get("foreign_script_consistency_audit")
    source_blocked = bool(
        isinstance(source_audit, dict)
        and str(source_audit.get("status") or "").startswith("BLOCKED_")
    )
    if source_blocked:
        audit = source_audit
        finding_key = "unproven_foreign_introductions"
        token_class = "UNPROVEN_FOREIGN_LANGUAGE_INTRODUCTION"

        def finding_token(row: dict[str, object]) -> str:
            return str(row.get("attempted") or "")

        def finding_text(row: dict[str, object]) -> object:
            return row.get("attempted")

        audit_surface = "final_source_language_preservation_audit"
    elif isinstance(foreign_audit, dict) and (
        str(foreign_audit.get("status") or "").startswith("BLOCKED_")
        # Pre-status artifacts are retained for backward-compatible failure
        # evidence extraction.  A typed non-blocked status is authoritative.
        or (
            not foreign_audit.get("status")
            and foreign_audit.get("mixed_cjk_latin_cues")
        )
    ):
        audit = foreign_audit
        finding_key = "mixed_cjk_latin_cues"
        token_class = "MIXED_CJK_MULTIWORD_LATIN"

        def finding_token(row: dict[str, object]) -> str:
            return " ".join(str(word) for word in row.get("latin_words") or [])

        def finding_text(row: dict[str, object]) -> object:
            return row.get("text")

        audit_surface = "foreign_script_consistency_audit"
    else:
        return None
    findings = audit.get(finding_key)
    if not isinstance(findings, list) or not findings:
        return None
    valid_findings = [row for row in findings if isinstance(row, dict)]
    if not valid_findings:
        return None
    all_witness_rows = [
        row for row in (audit.get("audio_witness_rows") or []) if isinstance(row, dict)
    ]

    def witnesses_for(finding: dict[str, object]) -> list[dict[str, object]]:
        return [
            row
            for row in all_witness_rows
            if row.get("cue_index") == finding.get("cue_index")
        ]

    cpa_rows = [
        row
        for row in (audit.get("cpa_adjudication_rows") or [])
        if isinstance(row, dict)
    ]

    def cpa_receipts_for(finding: dict[str, object]) -> list[dict[str, object]]:
        return [
            row
            for row in cpa_rows
            if row.get("cue_index") == finding.get("cue_index")
        ]

    def finding_resolved(finding: dict[str, object]) -> bool:
        return any(
            row.get("witnessed") is True for row in witnesses_for(finding)
        ) or any(
            row.get("resolved") is True for row in cpa_receipts_for(finding)
        )

    unresolved = [
        finding
        for finding in valid_findings
        if not finding_resolved(finding)
    ]
    finding = unresolved[0] if unresolved else valid_findings[0]
    witness_rows = witnesses_for(finding)
    cpa_receipts = cpa_receipts_for(finding)
    witnessed = finding_resolved(finding)
    usable_audio = any(
        str(row.get("exact_transcript") or "").strip()
        and not row.get("failure")
        for row in witness_rows
    )
    if witnessed:
        missing_witnesses: list[str] = []
    elif usable_audio:
        missing_witnesses = ["cpa_text_adjudication"]
    else:
        missing_witnesses = ["positive_source_audio_transcription"]
    hashes = {
        "chat_authority_sha256": "sha256:"
        + hashlib.sha256(path.read_bytes()).hexdigest(),
        "input_srt_sha256": document.get("input_srt_sha256"),
        "output_srt_sha256": document.get("output_srt_sha256"),
        "audio_sha256": next(
            (
                "sha256:" + str(row["audio_sha256"]).removeprefix("sha256:")
                for row in witness_rows
                if row.get("audio_sha256")
            ),
            None,
        ),
    }
    return {
        "schema_version": "candidate-gate-violation.v1",
        "gate": "FOREIGN_SOURCE_TRANSCRIPTION_REQUIRED",
        "audit_surface": audit_surface,
        "token_class": token_class,
        "token": finding_token(finding),
        "cue_index": finding.get("cue_index"),
        "start_ms": finding.get("start_ms"),
        "end_ms": finding.get("end_ms"),
        "text": finding_text(finding),
        "available_witnesses": (
            (["bounded_audio"] if witness_rows else [])
            + (["cpa_text_judge"] if cpa_receipts else [])
        ),
        "missing_witnesses": missing_witnesses,
        "witness_rows": witness_rows,
        "cpa_adjudication_rows": cpa_receipts,
        "unresolved_findings": [
            {
                "token": finding_token(unresolved_finding),
                "cue_index": unresolved_finding.get("cue_index"),
                "start_ms": unresolved_finding.get("start_ms"),
                "end_ms": unresolved_finding.get("end_ms"),
                "text": finding_text(unresolved_finding),
            }
            for unresolved_finding in unresolved
        ],
        "artifact_hashes": {key: value for key, value in hashes.items() if value},
    }


def foreign_source_provider_transient(
    violation: dict[str, object] | None,
) -> bool:
    """Distinguish an unavailable audio witness from a textual gate verdict."""

    if not isinstance(violation, dict):
        return False
    unresolved = violation.get("unresolved_findings")
    rows = violation.get("witness_rows")
    if not isinstance(unresolved, list) or not unresolved or not isinstance(rows, list):
        return False
    unresolved_indexes = {
        row.get("cue_index") for row in unresolved if isinstance(row, dict)
    }
    relevant = [
        row
        for row in rows
        if isinstance(row, dict) and row.get("cue_index") in unresolved_indexes
    ]
    if not relevant:
        return False
    provider_markers = (
        "AGY_FOREIGN_WITNESS_",
        "WITNESS_PROVIDERS_FAILED",
        "WITNESS_AUDIO_EXTRACTION_FAILED",
        "HTTPERROR",
        "QUOTA",
        "TIMED OUT",
        "TIMEOUT",
        "SUBPROCESS",
    )
    return all(
        any(marker in str(row.get("failure") or "").upper() for marker in provider_markers)
        for row in relevant
    )
