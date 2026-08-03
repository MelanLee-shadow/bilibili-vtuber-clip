"""Reconcile hash-bound reviewed text with earlier chat-read proposals."""

from __future__ import annotations

from typing import Any, Mapping

from src.autoslice.chat_evidence import (
    _srt_clock_ms,
    _valid_sha256,
    normalize_chat_text,
)


def reconcile_reviewed_text_override_conflicts(
    audit: dict[str, Any],
    text_manifest: Mapping[str, Any] | None,
    *,
    delivery_start_ms: int,
) -> int:
    """Let a later reviewed text decision supersede one conflicting chat read.

    Structured chat remains authoritative for wording that the streamer
    demonstrably reads.  It cannot, however, re-impose a homophone that a
    hash-bound reviewed text decision subsequently corrected in the same cue.
    Reconciliation is deliberately narrow: the exact chat text must occur in
    the reviewed source cue, disappear from the reviewed output, and the two
    absolute time spans must overlap.
    """

    if not isinstance(text_manifest, Mapping) or text_manifest.get("status") != "READY":
        return 0
    decisions = text_manifest.get("decisions") or []
    if not isinstance(decisions, list):
        return 0
    document_hash = str(text_manifest.get("override_document_sha256") or "")
    source_hash = str(text_manifest.get("source_srt_sha256") or "")
    output_hash = str(text_manifest.get("output_srt_sha256") or "")
    if not all(_valid_sha256(value) for value in (document_hash, source_hash, output_hash)):
        return 0

    reconciled = 0
    superseded = audit.setdefault("superseded_chat_proposals", [])
    for row in audit.get("applied") or []:
        if not isinstance(row, dict) or row.get("reconciliation"):
            continue
        exact_text = str(row.get("exact_text") or "")
        exact_norm = normalize_chat_text(exact_text)
        if not exact_norm:
            continue
        try:
            matched_start_ms = int(row["matched_start_ms"])
            matched_end_ms = int(row["matched_end_ms"])
        except (KeyError, TypeError, ValueError):
            continue

        for decision_index, decision in enumerate(decisions):
            if not isinstance(decision, Mapping):
                continue
            source_cue = decision.get("source")
            if not isinstance(source_cue, Mapping):
                continue
            authority = str(decision.get("authority") or "").strip()
            reason = str(decision.get("reason") or "").strip()
            before = str(source_cue.get("text") or "")
            after = str(decision.get("output_text") or "")
            if not authority or not reason or not after:
                continue
            before_norm = normalize_chat_text(before)
            after_norm = normalize_chat_text(after)
            if exact_norm not in before_norm or exact_norm in after_norm:
                continue
            try:
                absolute_start_ms = delivery_start_ms + _srt_clock_ms(
                    str(source_cue.get("start") or "")
                )
                absolute_end_ms = delivery_start_ms + _srt_clock_ms(
                    str(source_cue.get("end") or "")
                )
            except ValueError:
                continue
            if absolute_end_ms <= matched_start_ms or absolute_start_ms >= matched_end_ms:
                continue

            reconciliation = {
                "reason_code": "REVIEWED_TEXT_OVERRIDE_SUPERSEDES_CHAT_READ",
                "override_document_sha256": document_hash,
                "text_override_decision_index": decision_index,
                "override_start_ms": absolute_start_ms,
                "override_end_ms": absolute_end_ms,
                "authority": authority,
                "reason": reason,
            }
            row["reconciliation"] = reconciliation
            superseded.append(
                {
                    "reason_code": reconciliation["reason_code"],
                    "evidence_id": row.get("evidence_id"),
                    "exact_text": exact_text,
                    "matched_start_ms": matched_start_ms,
                    "matched_end_ms": matched_end_ms,
                    "reviewed_source_text": before,
                    "reviewed_output_text": after,
                    **reconciliation,
                }
            )
            reconciled += 1
            break

    audit["reviewed_text_override_chat_reconciliation"] = {
        "status": "APPLIED" if reconciled else "NO_CONFLICT",
        "override_document_sha256": document_hash,
        "reconciled_count": reconciled,
    }
    return reconciled
