"""Deterministic scope policy for 维护者's subtitle correction reports."""

from __future__ import annotations

from collections.abc import Mapping
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

    Three or more reported points are treated as an exhaustive candidate
    report, but still require a machine-checkable mapping from every changed
    cue/window to a reported point (维护者's "3 or more" instruction).
    An explicit complete report takes precedence over count heuristics too.
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

    if explicitly_exhaustive and only_these_errors is False:
        raise ValueError("conflicting explicit operator review scope")

    # None means no scope statement. False is an explicit request to find
    # additional errors and must outrank the count-based historical heuristic.
    non_exhaustive = only_these_errors is False
    explicit_scope = explicitly_exhaustive or only_these_errors is True
    exhaustive_candidate = issue_count >= 3
    short_explicit_scope = issue_count <= 2 and explicit_scope
    whole_clip = non_exhaustive or (issue_count <= 2 and not explicit_scope)
    targeted = not whole_clip
    return {
        "schema_version": SCHEMA_VERSION,
        "candidate_id": candidate_id,
        "reported_issue_count": issue_count,
        "explicitly_exhaustive": explicitly_exhaustive,
        "only_these_errors": only_these_errors,
        "operator_scope": (
            "EXPLICITLY_NON_EXHAUSTIVE"
            if non_exhaustive
            else "EXHAUSTIVE_CANDIDATE"
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


def require_explicit_exhaustive_review_plan(
    value: object, *, candidate_id: str,
) -> dict[str, Any]:
    """Recheck the scope plan before compiling full-text operator ownership.

    A numeric targeted-repair policy, a publication approval, and the number
    of changed cues do not establish a whole-transcript human review.
    """
    message = "explicit exhaustive operator review plan is required"
    if not isinstance(value, Mapping) or value.get("candidate_id") != candidate_id:
        raise ValueError(message)
    try:
        expected = plan_operator_correction(
            candidate_id=candidate_id,
            issue_count=value.get("reported_issue_count"),
            explicitly_exhaustive=value.get("explicitly_exhaustive"),
            only_these_errors=value.get("only_these_errors"),
        )
    except (TypeError, ValueError) as exc:
        raise ValueError(message) from exc
    if dict(value) != expected or not (
        expected["explicitly_exhaustive"] is True
        or expected["only_these_errors"] is True
    ):
        raise ValueError(message)
    return expected
