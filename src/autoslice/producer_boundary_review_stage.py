"""Post-authority semantic boundary review for the producer."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any

from src.autoslice.boundary_semantic_review import (
    BoundarySemanticReviewError,
    boundary_search_scope_is_valid,
    review_talk_boundary_semantics,
)
from src.autoslice.boundary_source_context_coverage import (
    source_context_coverage_block,
)
from src.autoslice.boundary_review_failure_evidence import (
    review_unavailable_evidence as _review_unavailable_evidence,
)
from src.autoslice.producer_exact_delivery_boundary import (
    exact_delivery_correction_audit as _exact_delivery_correction_audit,
    review_exact_delivery_boundary_semantics as _review_exact_delivery_boundary_semantics,
)
from src.autoslice.frozen_boundary_receipt import FrozenBoundaryReview


def review_final_boundary_semantics(
    *,
    cues: Sequence[object],
    boundary_target_ms: int | None,
    candidate_id: str,
    selection_hook: str,
    selection_scorecard: object,
    structured_context: str,
    candidate_context: str,
    boundary_max_forward_ms: int,
    llm_call: Callable[[str], str],
    extract_json: Callable[[str], Any],
    disabled: bool = False,
    terminal_source_review: Mapping[str, object] | None = None,
    source_final_start_ms: int | None = None,
    source_final_end_ms: int | None = None,
    boundary_search_scope: Mapping[str, object] | None = None,
    available_local_source_context_end_ms: int | None = None,
    frozen_review: FrozenBoundaryReview | None = None,
    replay_audit: dict[str, object] | None = None,
) -> dict[str, object]:
    """Review the exact post-authority cue grid used by the resolver."""

    if disabled:
        return {
            "schema_version": "talk-boundary-semantic-review.v1",
            "status": "BLOCK",
            "candidate_id": candidate_id,
            "reason_codes": ["FINAL_REVIEW_DISABLED"],
        }
    if boundary_target_ms is None:
        return {
            "schema_version": "talk-boundary-semantic-review.v1",
            "status": "BLOCK",
            "candidate_id": candidate_id,
            "reason_codes": ["BOUNDARY_TARGET_MISSING"],
        }
    if boundary_search_scope is not None:
        scope = dict(boundary_search_scope)
        if not boundary_search_scope_is_valid(scope):
            return {
                "schema_version": "talk-boundary-semantic-review.v1",
                "status": "BLOCK",
                "candidate_id": candidate_id,
                "boundary_search_scope": scope,
                "reason_codes": ["BOUNDARY_SEMANTIC_SEARCH_SCOPE_INVALID"],
            }
        if scope.get("status") != "PASS":
            return {
                "schema_version": "talk-boundary-semantic-review.v1",
                "status": "BLOCK",
                "candidate_id": candidate_id,
                "boundary_search_scope": scope,
                "reason_codes": list(scope.get("reason_codes") or [])
                or ["BOUNDARY_SEMANTIC_SEARCH_SCOPE_BLOCKED"],
            }
        coverage_block = source_context_coverage_block(
            scope=scope,
            available_local_source_context_end_ms=(available_local_source_context_end_ms),
            candidate_id=candidate_id,
            boundary_max_forward_ms=boundary_max_forward_ms,
        )
        if coverage_block is not None:
            return coverage_block
    try:
        return review_talk_boundary_semantics(
            cues=cues,
            target_ms=boundary_target_ms,
            candidate_id=candidate_id,
            selection_hook=selection_hook,
            selection_scorecard=selection_scorecard,
            structured_context=structured_context,
            candidate_context=candidate_context,
            llm_call=llm_call,
            extract_json=extract_json,
            max_forward_ms=boundary_max_forward_ms,
            terminal_source_review=terminal_source_review,
            source_final_start_ms=source_final_start_ms,
            source_final_end_ms=source_final_end_ms,
            boundary_search_scope=boundary_search_scope,
            frozen_review=frozen_review,
            replay_audit=replay_audit,
        )
    except BoundarySemanticReviewError as exc:
        reason = str(exc)
        unavailable_evidence = _review_unavailable_evidence(exc)
    except Exception as exc:
        reason = "BOUNDARY_SEMANTIC_REVIEW_UNAVAILABLE:" + type(exc).__name__
        unavailable_evidence = _review_unavailable_evidence(exc)
    return {
        "schema_version": "talk-boundary-semantic-review.v1",
        "status": "BLOCK",
        "candidate_id": candidate_id,
        **(
            {"boundary_search_scope": dict(boundary_search_scope)}
            if isinstance(boundary_search_scope, Mapping)
            else {}
        ),
        "reason_codes": [reason],
        **(
            {"unavailable_evidence": unavailable_evidence}
            if unavailable_evidence
            else {}
        ),
    }


def review_exact_delivery_boundary_semantics(
    **kwargs: object,
) -> dict[str, object]:
    """Compatibility seam using this module's patchable source reviewer."""

    return _review_exact_delivery_boundary_semantics(
        review_final_boundary_semantics=review_final_boundary_semantics,
        **kwargs,
    )


def exact_delivery_correction_audit(
    **kwargs: object,
) -> dict[str, object]:
    """Compatibility seam using this module's patchable delivery reviewer."""

    return _exact_delivery_correction_audit(
        review_exact_delivery=review_exact_delivery_boundary_semantics,
        **kwargs,
    )
