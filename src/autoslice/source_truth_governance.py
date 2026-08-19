"""Governance checks for the append-only source subtitle truth ledger."""

from __future__ import annotations

from typing import Mapping, Sequence


ASSERTION_STATES = frozenset(
    {"PROPOSED", "VERIFIED_ACTIVE", "REJECTED", "CONFLICTED", "SUPERSEDED"}
)


def assertion_state(entry: Mapping[str, object]) -> str:
    """Legacy rows are active; revision-aware rows must name a valid state."""

    state = entry.get("assertion_state")
    if state is None:
        return "VERIFIED_ACTIVE"
    value = str(state)
    if value not in ASSERTION_STATES:
        raise RuntimeError("SOURCE_SUBTITLE_TRUTH_ASSERTION_STATE_INVALID")
    return value


def validate_ledger_governance(
    document: Mapping[str, object],
    entries: Sequence[object],
) -> None:
    """Enforce the v2 boundary and reserve direct mutation for operator truth."""

    governance = document.get("governance")
    if governance is None:
        return
    if (
        not isinstance(governance, Mapping)
        or governance.get("schema_version")
        != "source-subtitle-truth-governance.v2"
    ):
        raise RuntimeError("SOURCE_SUBTITLE_TRUTH_GOVERNANCE_INVALID")
    start = governance.get("governed_entry_start_index")
    if (
        isinstance(start, bool)
        or not isinstance(start, int)
        or not 0 <= start <= len(entries)
    ):
        raise RuntimeError("SOURCE_SUBTITLE_TRUTH_GOVERNANCE_INVALID")
    for index, entry in enumerate(entries[start:], start=start):
        if not isinstance(entry, Mapping):
            raise RuntimeError("SOURCE_SUBTITLE_TRUTH_ENTRY_INVALID")
        required = (
            "revision_id",
            "assertion_state",
            "evidence_class",
            "authority",
            "decision_authority",
        )
        if any(not str(entry.get(field) or "").strip() for field in required):
            raise RuntimeError(
                "SOURCE_SUBTITLE_TRUTH_GOVERNED_METADATA_MISSING:"
                f"{index}"
            )
        if (
            entry.get("assertion_state") == "VERIFIED_ACTIVE"
            and entry.get("decision_authority") != "REVIEWER_OPERATOR_TRUTH"
        ):
            raise RuntimeError(
                "SOURCE_SUBTITLE_TRUTH_DIRECT_MUTATION_AUTHORITY_FORBIDDEN:"
                f"{index}"
            )
