"""Expected-value glossary bypass with a registered-name equality guard."""

from __future__ import annotations

from typing import Any, Callable, Mapping

from src.autoslice.subtitle_fidelity import _homophone_equal
from src.autoslice.term_authority import expected_value_respell_pairs


def glossary_expected_value_gate(
    row: Mapping[str, Any],
    *,
    base_text: str,
    proposed_text: str,
    registered_term_set: frozenset[str],
    orthography_authority: Callable[[Mapping[str, Any]], Mapping[str, Any]],
    near_homophone_gate: Callable[
        [Mapping[str, Any], str], dict[str, Any] | None
    ],
) -> dict[str, Any] | None:
    """Admit high-prior glossary canon unless both sides are named peers."""

    suspect = str(row.get("suspect") or "")
    suggestion = str(row.get("suggestion") or "")
    if (
        not suspect
        or not suggestion
        or not proposed_text
        or suspect not in base_text
        or base_text.replace(suspect, suggestion, 1) != proposed_text
    ):
        return None
    provenance = row.get("candidate_provenance")
    provenance_kind = (
        str(provenance.get("kind") or "")
        if isinstance(provenance, Mapping)
        else ""
    )
    provenance_surface = (
        str(provenance.get("surface") or "")
        if isinstance(provenance, Mapping)
        else ""
    )
    explicit_rule = (suspect, suggestion) in expected_value_respell_pairs()
    registered_target = suggestion if explicit_rule else provenance_surface
    glossary_backed = bool(
        provenance_kind in {"glossary", "official_roster"}
        and registered_target in registered_term_set
        and registered_target in proposed_text
        and orthography_authority(row)["status"] == "PASS"
    )
    if not explicit_rule and not glossary_backed:
        return None
    current_registered = suspect in registered_term_set
    proposed_registered = registered_target in registered_term_set
    if current_registered and proposed_registered and suspect != suggestion:
        return None
    near_gate = near_homophone_gate(row, base_text)
    if not _homophone_equal(suspect, suggestion) and near_gate is None:
        return None
    return {
        "schema_version": "glossary-expected-value-gate.v1",
        "status": "PASS",
        "policy": (
            "EXPLICIT_PROFILE_RULE"
            if explicit_rule
            else "GLOSSARY_HIGH_PRIOR"
        ),
        "candidate_provenance_kind": provenance_kind or None,
        "registered_target": registered_target,
        "current_registered_term": current_registered,
        "proposed_registered_term": proposed_registered,
        "registered_name_conflict": False,
        "pinyin_gate": (
            {"tier": "exact_homophone", "pinyin_similarity": 1.0}
            if _homophone_equal(suspect, suggestion)
            else near_gate
        ),
    }
