"""Post-authority semantic boundary review for the producer."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any

from src.autoslice.boundary_endpoint_binding import (
    bind_final_semantic_endpoint,
)
from src.autoslice.boundary_semantic_review import (
    BoundarySemanticReviewError,
    boundary_search_scope_is_valid,
    review_talk_boundary_semantics,
)
from src.autoslice.jingting_chunker import parse_srt_cues


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
                "reason_codes": [
                    "BOUNDARY_SEMANTIC_SEARCH_SCOPE_INVALID"
                ],
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
        )
    except BoundarySemanticReviewError as exc:
        reason = str(exc)
    except Exception as exc:
        reason = "BOUNDARY_SEMANTIC_REVIEW_UNAVAILABLE:" + type(exc).__name__
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
    }


def review_exact_delivery_boundary_semantics(
    *,
    cues: Sequence[object],
    source_boundary_review: Mapping[str, object] | None,
    source_final_start_ms: int,
    source_final_end_ms: int,
    candidate_id: str,
    selection_hook: str,
    selection_scorecard: object,
    structured_context: str,
    candidate_context: str,
    boundary_max_forward_ms: int,
    llm_call: Callable[[str], str],
    extract_json: Callable[[str], Any],
    disabled: bool = False,
) -> dict[str, object]:
    """Re-review and bind the exact delivery grid after every text mutation."""

    final_cues = [
        cue
        for cue in cues
        if str(getattr(cue, "text", "") or "").strip()
    ]
    if not final_cues:
        return {
            "schema_version": "talk-boundary-semantic-review.v1",
            "status": "BLOCK",
            "review_scope": "final_delivery",
            "reason_codes": ["FINAL_DELIVERY_BOUNDARY_NO_CUES"],
        }
    review = review_final_boundary_semantics(
        cues=final_cues,
        boundary_target_ms=int(getattr(final_cues[-1], "end_ms")),
        candidate_id=candidate_id,
        selection_hook=selection_hook,
        selection_scorecard=selection_scorecard,
        structured_context=structured_context,
        candidate_context=candidate_context,
        boundary_max_forward_ms=boundary_max_forward_ms,
        llm_call=llm_call,
        extract_json=extract_json,
        disabled=disabled,
        terminal_source_review=source_boundary_review,
        source_final_start_ms=source_final_start_ms,
        source_final_end_ms=source_final_end_ms,
    )
    review, _ = bind_final_semantic_endpoint(
        semantic_review=review,
        cues=final_cues,
        closure_cue=final_cues[-1],
        snapped_end_ms=int(getattr(final_cues[-1], "end_ms")),
        final_start_ms=0,
        final_end_ms=source_final_end_ms - source_final_start_ms,
    )
    return review


def exact_delivery_correction_audit(
    *,
    final_srt_text: str,
    correction_audit: Mapping[str, object],
    source_final_start_ms: int,
    source_final_end_ms: int,
    candidate_id: str,
    selection_hook: str,
    selection_scorecard: object,
    structured_context: str,
    candidate_context: str,
    boundary_max_forward_ms: int,
    llm_call: Callable[[str], str],
    extract_json: Callable[[str], Any],
    disabled: bool = False,
) -> dict[str, object]:
    """Replace the source review with an exact post-mutation delivery review."""

    source_review = correction_audit.get("boundary_semantic_review")
    delivery_review = review_exact_delivery_boundary_semantics(
        cues=parse_srt_cues(final_srt_text),
        source_boundary_review=(
            source_review if isinstance(source_review, Mapping) else None
        ),
        source_final_start_ms=source_final_start_ms,
        source_final_end_ms=source_final_end_ms,
        candidate_id=candidate_id,
        selection_hook=selection_hook,
        selection_scorecard=selection_scorecard,
        structured_context=structured_context,
        candidate_context=candidate_context,
        boundary_max_forward_ms=boundary_max_forward_ms,
        llm_call=llm_call,
        extract_json=extract_json,
        disabled=disabled,
    )
    result = dict(correction_audit)
    result["boundary_semantic_review"] = delivery_review
    return result
