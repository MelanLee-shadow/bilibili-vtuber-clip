"""Narrow retirement of legacy text owners by exhaustive operator truth."""

from __future__ import annotations

from collections.abc import Mapping

from .truth_ownership_mutation_audit import valid_truth_ownership_zero_mutation_skip


def supersede_legacy_text_owner(
    row: dict,
    ownership: Mapping[str, object] | None,
) -> bool:
    """Retire one legacy text owner only for a fully validated operator receipt."""

    if not valid_truth_ownership_zero_mutation_skip(ownership):
        return False
    row["final_verification_scope"] = "SUPERSEDED_BY_OPERATOR_REVIEWED_BASELINE"
    return True
