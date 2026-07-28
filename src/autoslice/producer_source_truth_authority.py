"""Cross-stage source-truth ownership checks for the producer."""

from __future__ import annotations

from collections.abc import Mapping

from src.autoslice.source_subtitle_truth import (
    validated_source_truth_projection,
)
from src.autoslice.source_truth_adjacent_partition import partition_origin_map


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
    """Let required source truth supersede overlapping chat requirements."""

    truth_rows = [
        row
        for key in ("applied", "satisfied")
        for row in (source_truth_audit.get(key) or [])
        if isinstance(row, Mapping)
        and row.get("required") is not False
    ]
    projected_origins = partition_origin_map(
        source_truth_audit.get("cue_grid_partition")
    )

    def truth_indexes(row: Mapping[str, object]) -> set[int]:
        indexes = {
            int(index) for index in (row.get("cue_indexes") or [])
        }
        if projected_origins is None:
            return indexes
        return {projected_origins[index] for index in indexes}

    truth_cues = {
        index
        for row in truth_rows
        for index in truth_indexes(row)
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
            if truth_indexes(row) & row_cues
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

    remaining_requirements = []
    entity_reconciliations = []

    def normalized_sha256(value: object) -> str:
        return str(value or "").removeprefix("sha256:").casefold()

    for requirement in chat_authority_audit.get(
        "entity_verdict_required"
    ) or []:
        if not isinstance(requirement, Mapping):
            remaining_requirements.append(requirement)
            continue
        request = requirement.get("request")
        canonical = str(
            requirement.get("structured_chat_canonical")
            or (
                request.get("structured_chat_canonical")
                if isinstance(request, Mapping)
                else ""
            )
            or ""
        )
        try:
            occurrence_count = int(
                requirement.get("structured_chat_occurrence_count") or 1
            )
        except (TypeError, ValueError):
            occurrence_count = 0
        requirement_cues = {
            int(index)
            for index in (requirement.get("cue_indexes") or [])
            if isinstance(index, int) and not isinstance(index, bool)
        }
        requirement_event_id = str(
            requirement.get("source_event_id") or ""
        )
        requirement_source_sha256 = normalized_sha256(
            requirement.get("source_sha256")
        )
        owners = []
        owned_text = []
        for truth_row in truth_rows:
            projection = validated_source_truth_projection(truth_row)
            if projection is None:
                continue
            same_structured_event = bool(
                requirement.get("reason_code")
                == "REPEATED_CHAT_ENTITY_SLOTS_UNRESOLVED"
                and requirement_event_id
                and requirement_source_sha256
                and str(truth_row.get("source_event_id") or "")
                == requirement_event_id
                and normalized_sha256(
                    truth_row.get("source_event_sha256")
                )
                == requirement_source_sha256
            )
            matching_cues = [
                cue
                for cue in projection["cues"]
                if (
                    same_structured_event
                    or (
                        projected_origins.get(
                            cue["cue_index"], cue["cue_index"]
                        )
                        if projected_origins is not None
                        else cue["cue_index"]
                    )
                    in requirement_cues
                )
            ]
            if not matching_cues:
                continue
            owners.append(str(truth_row.get("truth_id")))
            owned_text.extend(str(cue["after_text"]) for cue in matching_cues)
        resolved_occurrences = (
            "".join(owned_text).casefold().count(canonical.casefold())
            if canonical
            else 0
        )
        if (
            canonical
            and occurrence_count > 0
            and resolved_occurrences >= occurrence_count
        ):
            entity_reconciliations.append(
                {
                    "cue_indexes": sorted(requirement_cues),
                    "structured_chat_canonical": canonical,
                    "required_occurrence_count": occurrence_count,
                    "resolved_occurrence_count": resolved_occurrences,
                    "truth_ids": sorted(set(owners)),
                }
            )
            continue
        remaining_requirements.append(requirement)
    if entity_reconciliations:
        chat_authority_audit[
            "source_truth_entity_requirement_reconciliations"
        ] = entity_reconciliations
        chat_authority_audit[
            "entity_verdict_required"
        ] = remaining_requirements
        if (
            chat_authority_audit.get("status")
            == "ENTITY_VERDICT_REQUIRED"
            and not remaining_requirements
        ):
            chat_authority_audit["status"] = "APPLIED_AND_VERIFIED"
