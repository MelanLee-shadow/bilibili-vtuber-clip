"""Final-surface ownership for late CPA source-language repairs."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping

from src.autoslice.jingting_chunker import parse_srt_cues


def _register_final_cpa_repairs(
    chat_authority_audit: dict[str, Any],
    *,
    input_srt: str,
    output_srt: str,
    cpa_audit: Mapping[str, Any],
    surface: str,
) -> None:
    """Register a late CPA repair as the owner of its final cue surface."""

    if surface not in {"source_language", "foreign_script"}:
        raise ValueError("FINAL_CPA_SURFACE_INVALID")
    surface_slug = surface.replace("_", "-")
    surface_label = surface.upper()
    error_prefix = f"FINAL_{surface_label}"
    registration_key = f"final_{surface}_cpa_surface_registrations"
    registration_schema = f"final-{surface_slug}-cpa-surface-registration.v1"
    supersession_schema = f"final-{surface_slug}-cpa-supersession.v1"
    exact_read_supersession_schema = (
        f"final-{surface_slug}-cpa-exact-read-supersession.v1"
    )
    supersession_status = f"SUPERSEDED_BY_FINAL_{surface_label}_CPA"
    owner_mode = f"final_{surface}_cpa_adjudication"
    receipt_key = f"final_{surface}_receipt_sha256"
    mutation_basis = (
        "CPA_ACOUSTIC_PRONUNCIATION_DISAMBIGUATION"
        if surface == "source_language"
        else "CPA_FOREIGN_SCRIPT_RETRANSCRIPTION"
    )
    receipts = cpa_audit.get("cpa_adjudication_rows")
    if not isinstance(receipts, list):
        return
    declared_applied = cpa_audit.get("applied_count")
    if (
        isinstance(declared_applied, bool)
        or not isinstance(declared_applied, int)
        or declared_applied <= 0
    ):
        return
    output_sha256 = hashlib.sha256(output_srt.encode("utf-8")).hexdigest()
    if cpa_audit.get("output_srt_sha256") != output_sha256:
        raise ValueError(f"{error_prefix}_OUTPUT_HASH_MISMATCH")
    input_cues = {int(cue.index): cue for cue in parse_srt_cues(input_srt)}
    rows = chat_authority_audit.setdefault("entity_repairs", [])
    if not isinstance(rows, list):
        raise ValueError(f"{error_prefix}_ENTITY_REPAIR_LEDGER_INVALID")
    applied_rows = chat_authority_audit.get("applied", [])
    if not isinstance(applied_rows, list):
        raise ValueError(f"{error_prefix}_APPLIED_LEDGER_INVALID")
    registrations = chat_authority_audit.setdefault(
        registration_key, []
    )
    if not isinstance(registrations, list):
        raise ValueError(f"{error_prefix}_REGISTRATION_LEDGER_INVALID")

    applied = 0
    for receipt in receipts:
        if not isinstance(receipt, Mapping) or receipt.get("choice") != "PROPOSED":
            continue
        adjudication = receipt.get("adjudication")
        judge = adjudication.get("judge") if isinstance(adjudication, Mapping) else None
        cue_index = receipt.get("cue_index")
        current = receipt.get("current")
        proposed = receipt.get("proposed")
        if (
            receipt.get("resolved") is not True
            or receipt.get("decision_authority") != "CPA_JUDGE"
            or not isinstance(adjudication, Mapping)
            or adjudication.get("decision_authority") != "CPA_JUDGE"
            or adjudication.get("witness_authority") != "EVIDENCE_ONLY"
            or not isinstance(judge, Mapping)
            or judge.get("status") != "JUDGED"
            or judge.get("choice") != "PROPOSED"
            or isinstance(cue_index, bool)
            or not isinstance(cue_index, int)
            or not isinstance(current, str)
            or not current
            or not isinstance(proposed, str)
            or not proposed.strip()
            or cue_index not in input_cues
            or input_cues[cue_index].text != current
        ):
            raise ValueError(f"{error_prefix}_CPA_RECEIPT_INVALID")
        cue = input_cues[cue_index]
        receipt_sha256 = "sha256:" + hashlib.sha256(
            json.dumps(
                dict(receipt),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        superseded_indexes: list[int] = []
        for index, row in enumerate(rows):
            if not isinstance(row, dict) or row.get("reconciliation"):
                continue
            expected = str(row.get("structured_exact_text") or "")
            if not expected:
                after = row.get("after")
                if isinstance(after, list) and len(after) == 1:
                    expected = str(after[0] or "")
                elif isinstance(after, str):
                    expected = after
            if (
                row.get("matched_start_ms") == cue.start_ms
                and row.get("matched_end_ms") == cue.end_ms
                and expected == current
            ):
                row["reconciliation"] = {
                    "schema_version": supersession_schema,
                    "status": supersession_status,
                    "receipt_sha256": receipt_sha256,
                    "before_sha256": "sha256:"
                    + hashlib.sha256(current.encode("utf-8")).hexdigest(),
                    "after_sha256": "sha256:"
                    + hashlib.sha256(proposed.encode("utf-8")).hexdigest(),
                    "timing_immutable": True,
                }
                superseded_indexes.append(index)
        superseded_applied_indexes: list[int] = []
        for index, row in enumerate(applied_rows):
            if not isinstance(row, dict) or row.get("reconciliation"):
                continue
            expected = str(row.get("exact_text") or "")
            if (
                row.get("matched_start_ms") == cue.start_ms
                and row.get("matched_end_ms") == cue.end_ms
                and expected
                and expected in current
                and expected not in proposed
            ):
                # A chat exact-read may own only a substring of a cue. Retire
                # it only when this immutable cue geometry and the CPA
                # before->after receipt prove that the text disappeared.
                row["reconciliation"] = {
                    "schema_version": exact_read_supersession_schema,
                    "status": supersession_status,
                    "receipt_sha256": receipt_sha256,
                    "before_sha256": "sha256:"
                    + hashlib.sha256(current.encode("utf-8")).hexdigest(),
                    "after_sha256": "sha256:"
                    + hashlib.sha256(proposed.encode("utf-8")).hexdigest(),
                    "timing_immutable": True,
                }
                superseded_applied_indexes.append(index)
        owner = {
            "mode": owner_mode,
            "decision_authority": "CPA_JUDGE",
            "mutation_authority": {
                "schema_version": "subtitle-correction-mutation-authority.v1",
                "status": "PASS",
                "basis": mutation_basis,
            },
            "cue_indexes": [cue_index],
            "matched_start_ms": cue.start_ms,
            "matched_end_ms": cue.end_ms,
            "before": [current],
            "after": [proposed],
            "structured_exact_text": proposed,
            "survived": True,
            "timing_immutable": True,
            "boundary_required": False,
            "boundary_owner_rejection": "POST_BOUNDARY_FREEZE_FINAL_SURFACE_OWNER",
            receipt_key: receipt_sha256,
            "superseded_entity_repair_indexes": superseded_indexes,
            "superseded_applied_indexes": superseded_applied_indexes,
        }
        rows.append(owner)
        registrations.append(
            {
                "schema_version": registration_schema,
                "status": "REGISTERED",
                "receipt_sha256": receipt_sha256,
                "owner_entity_repair_index": len(rows) - 1,
                "superseded_entity_repair_indexes": superseded_indexes,
                "superseded_applied_indexes": superseded_applied_indexes,
            }
        )
        applied += 1
    if applied != declared_applied:
        raise ValueError(f"{error_prefix}_APPLIED_COUNT_MISMATCH")


def register_final_source_language_cpa_repairs(
    chat_authority_audit: dict[str, Any],
    *,
    input_srt: str,
    output_srt: str,
    source_language_audit: Mapping[str, Any],
) -> None:
    """Register late CPA language repairs and retire same-window predecessors."""

    _register_final_cpa_repairs(
        chat_authority_audit,
        input_srt=input_srt,
        output_srt=output_srt,
        cpa_audit=source_language_audit,
        surface="source_language",
    )


def register_final_foreign_script_cpa_repairs(
    chat_authority_audit: dict[str, Any],
    *,
    input_srt: str,
    output_srt: str,
    foreign_script_audit: Mapping[str, Any],
) -> None:
    """Register late CPA foreign-script repairs and retire predecessors."""

    _register_final_cpa_repairs(
        chat_authority_audit,
        input_srt=input_srt,
        output_srt=output_srt,
        cpa_audit=foreign_script_audit,
        surface="foreign_script",
    )
