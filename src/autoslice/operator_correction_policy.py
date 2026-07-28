"""Deterministic scope policy for Ivan's subtitle correction reports."""

from __future__ import annotations

from typing import Any


SCHEMA_VERSION = "operator-subtitle-correction-plan.v1"


def plan_operator_correction(
    *,
    candidate_id: str,
    issue_count: int,
    explicitly_exhaustive: bool = False,
) -> dict[str, Any]:
    """Choose whole-clip review versus bounded targeted repair."""

    if not candidate_id:
        raise ValueError("candidate_id is required")
    if isinstance(issue_count, bool) or not isinstance(issue_count, int):
        raise ValueError("issue_count must be an integer")
    if issue_count < 1:
        raise ValueError("issue_count must be positive")

    whole_clip = issue_count <= 2 and not explicitly_exhaustive
    return {
        "schema_version": SCHEMA_VERSION,
        "candidate_id": candidate_id,
        "reported_issue_count": issue_count,
        "explicitly_exhaustive": explicitly_exhaustive,
        "mode": (
            "WHOLE_CLIP_RERUN_AND_REVIEW"
            if whole_clip
            else "TARGETED_REPAIR_PLUS_SYSTEMIC_FIX"
        ),
        "whole_clip_rerun_required": whole_clip,
        "targeted_locations_only": not whole_clip,
        "systemic_pipeline_fix_required": True,
    }
