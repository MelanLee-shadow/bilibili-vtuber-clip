"""Cross-stage source-truth ownership checks for the producer."""

from __future__ import annotations

from collections.abc import Mapping


PREVIEW_SCHEMA_VERSION = "source-truth-deterministic-preview.v1"


def verify_source_truth_preview_formal_binding(
    chat_authority_audit: Mapping[str, object],
    source_truth_audit: Mapping[str, object],
) -> None:
    """Require both previews and formal application to share one ledger."""

    preview_receipts = chat_authority_audit.get(
        "source_truth_preview_receipts"
    )
    if not isinstance(preview_receipts, Mapping):
        raise RuntimeError("SOURCE_TRUTH_PREVIEW_RECEIPTS_MISSING")
    formal_ledger_sha256 = source_truth_audit.get("ledger_sha256")
    for preview_stage in (
        "pre_entity_arbitration",
        "pre_correction_review",
    ):
        preview = preview_receipts.get(preview_stage)
        if (
            not isinstance(preview, Mapping)
            or preview.get("schema_version") != PREVIEW_SCHEMA_VERSION
            or preview.get("ledger_sha256") != formal_ledger_sha256
        ):
            raise RuntimeError(
                "SOURCE_TRUTH_PREVIEW_FORMAL_LEDGER_BINDING_INVALID"
            )


def reconcile_required_source_truth_chat_authority(
    chat_authority_audit: dict,
    source_truth_audit: Mapping[str, object],
) -> None:
    """Let required source truth supersede overlapping whole-line chat text."""

    truth_rows = [
        row
        for key in ("applied", "satisfied")
        for row in (source_truth_audit.get(key) or [])
        if isinstance(row, Mapping)
        and row.get("required") is not False
    ]
    truth_cues = {
        index
        for row in truth_rows
        for index in (row.get("cue_indexes") or [])
    }
    if not truth_cues:
        return
    reconciled = []
    for chat_row in chat_authority_audit.get("applied") or []:
        if chat_row.get("reconciliation"):
            continue
        row_cues = set(chat_row.get("cue_indexes") or [])
        if not row_cues & truth_cues:
            continue
        owners = sorted(
            str(row.get("truth_id"))
            for row in truth_rows
            if set(row.get("cue_indexes") or []) & row_cues
        )
        chat_row["reconciliation"] = {
            "kind": "SOURCE_INTERVAL_TRUTH_SUPERSEDES",
            "truth_ids": owners,
            "note": (
                "Ivan source-interval truth owns this cue; read-aloud "
                "surface no longer a final requirement"
            ),
        }
        reconciled.append(
            {"cue_indexes": sorted(row_cues), "truth_ids": owners}
        )
    if reconciled:
        chat_authority_audit["source_truth_reconciliations"] = reconciled
