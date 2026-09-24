"""Preserve frozen geometry when a registered exact-final edit retires text."""

from __future__ import annotations
import hashlib
import re
from collections.abc import Callable, Mapping
from pathlib import Path
from src.autoslice.acoustic_witness_adjudication import valid_inaudible_drop_repair


def _is_sha256(value: object) -> bool:
    return isinstance(value, str) and bool(re.fullmatch(r"sha256:[0-9a-f]{64}", value))


def exact_final_superseded_boundary_owner_valid(
    *,
    validate_successor: Callable[..., bool],
    owner_kind: str = "entity_repair",
    row: Mapping[str, object],
    row_index: int,
    chat_authority: Mapping[str, object],
    chat_authority_path: Path | None = None,
) -> bool:
    """Keep one frozen boundary owner after an exact same-window CPA edit."""

    reconciliation = row.get("reconciliation")
    if not isinstance(reconciliation, Mapping):
        return False
    if owner_kind not in {"entity_repair", "exact_read"}:
        return False
    exact_read = owner_kind == "exact_read"
    schema = (
        "exact-final-cpa-exact-read-supersession.v1"
        if exact_read
        else "exact-final-cpa-supersession.v1"
    )
    index_key = "superseded_applied_indexes" if exact_read else "superseded_entity_repair_indexes"
    repair_sha256 = reconciliation.get("exact_final_repair_sha256")
    if not (
        reconciliation.get("schema_version") == schema
        and reconciliation.get("status") == "SUPERSEDED_BY_EXACT_FINAL_CPA"
        and reconciliation.get("timing_immutable") is True
        and _is_sha256(repair_sha256)
        and reconciliation.get("before_sha256")
        and reconciliation.get("after_sha256")
    ):
        return False

    registrations = chat_authority.get("exact_final_cpa_surface_registrations")
    entity_rows = chat_authority.get("entity_repairs")
    if not isinstance(registrations, list) or not isinstance(entity_rows, list):
        return False
    matching = [
        registration
        for registration in registrations
        if isinstance(registration, Mapping)
        and registration.get("schema_version") == "exact-final-cpa-surface-registration.v1"
        and registration.get("status") == "REGISTERED"
        and registration.get("exact_final_repair_sha256") == repair_sha256
        and isinstance(registration.get(index_key), list)
        and row_index in registration[index_key]
    ]
    if len(matching) != 1:
        return False
    successor_index = matching[0].get("owner_entity_repair_index")
    if (
        isinstance(successor_index, bool)
        or not isinstance(successor_index, int)
        or not 0 <= successor_index < len(entity_rows)
    ):
        return False
    successor = entity_rows[successor_index]
    predecessor_text = row.get("structured_exact_text")
    if not isinstance(predecessor_text, str) or not predecessor_text:
        predecessor_after = row.get("after")
        predecessor_text = (
            predecessor_after[0]
            if isinstance(predecessor_after, list)
            and len(predecessor_after) == 1
            and isinstance(predecessor_after[0], str)
            else predecessor_after
        )
    successor_text = (
        successor.get("structured_exact_text") if isinstance(successor, Mapping) else None
    )
    if exact_read:
        before = successor.get("before") if isinstance(successor, Mapping) else None
        predecessor_text = before[0] if isinstance(before, list) and len(before) == 1 else None
        exact = row.get("exact_text")
        if not (
            isinstance(predecessor_text, str)
            and isinstance(exact, str)
            and exact
            and exact in predecessor_text
            and isinstance(successor_text, str)
            and exact not in successor_text
        ):
            return False
    successor_is_typed_drop = bool(
        isinstance(successor, Mapping)
        and successor.get("mode") == "exact_final_cpa_self_heal"
        and valid_inaudible_drop_repair(successor)
    )
    return bool(
        isinstance(successor, Mapping)
        and isinstance(predecessor_text, str)
        and predecessor_text
        and isinstance(successor_text, str)
        and (successor_text or successor_is_typed_drop)
        and reconciliation.get("before_sha256")
        == "sha256:" + hashlib.sha256(predecessor_text.encode("utf-8")).hexdigest()
        and reconciliation.get("after_sha256")
        == "sha256:" + hashlib.sha256(successor_text.encode("utf-8")).hexdigest()
        and successor.get("matched_start_ms") == row.get("matched_start_ms")
        and successor.get("matched_end_ms") == row.get("matched_end_ms")
        and successor.get("exact_final_repair_sha256") == repair_sha256
        and validate_successor(
            row=successor,
            row_index=successor_index,
            chat_authority=chat_authority,
            chat_authority_path=chat_authority_path,
        )
    )
