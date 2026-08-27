"""Deterministic scope policy for Ivan's subtitle correction reports."""

from __future__ import annotations

from typing import Any


SCHEMA_VERSION = "operator-subtitle-correction-plan.v1"


def plan_operator_correction(
    *,
    candidate_id: str,
    issue_count: int,
    explicitly_exhaustive: bool = False,
    only_these_errors: bool | None = None,
) -> dict[str, Any]:
    """Choose whole-clip review versus a bounded, complete delta repair.

    More than three reported points are treated as an exhaustive candidate
    report, but still require a machine-checkable mapping from every changed
    cue/window to a reported point.  Exactly three remains conservative and
    requires a whole-clip review because the operator rule only guarantees
    completeness for ``>3`` (or an explicit short ``only these errors`` claim).
    """

    if not candidate_id:
        raise ValueError("candidate_id is required")
    if isinstance(issue_count, bool) or not isinstance(issue_count, int):
        raise ValueError("issue_count must be an integer")
    if issue_count < 1:
        raise ValueError("issue_count must be positive")
    if only_these_errors is not None and not isinstance(only_these_errors, bool):
        raise ValueError("only_these_errors must be a boolean when provided")
    if not isinstance(explicitly_exhaustive, bool):
        raise ValueError("explicitly_exhaustive must be a boolean")

    explicit_scope = explicitly_exhaustive or bool(only_these_errors)
    exhaustive_candidate = issue_count > 3
    short_explicit_scope = issue_count <= 2 and explicit_scope
    whole_clip = issue_count == 3 or (issue_count <= 2 and not short_explicit_scope)
    targeted = not whole_clip
    return {
        "schema_version": SCHEMA_VERSION,
        "candidate_id": candidate_id,
        "reported_issue_count": issue_count,
        "explicitly_exhaustive": explicitly_exhaustive,
        "only_these_errors": bool(only_these_errors),
        "operator_scope": (
            "EXHAUSTIVE_CANDIDATE"
            if exhaustive_candidate
            else "EXPLICITLY_LIMITED"
            if short_explicit_scope
            else "CONSERVATIVE_WHOLE_CLIP"
        ),
        "mode": (
            "WHOLE_CLIP_RERUN_AND_REVIEW"
            if whole_clip
            else "TARGETED_REPAIR_PLUS_SYSTEMIC_FIX"
        ),
        "whole_clip_rerun_required": whole_clip,
        "targeted_locations_only": targeted,
        "requires_change_coverage_proof": targeted,
        "systemic_pipeline_fix_required": True,
    }
