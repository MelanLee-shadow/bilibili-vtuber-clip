"""Exact post-mutation delivery-boundary review and correction binding."""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from src.autoslice.boundary_endpoint_binding import bind_final_semantic_endpoint
from src.autoslice.boundary_semantic_projection import (
    project_correlated_source_boundary_pass,
)
from src.autoslice.c6_exhaustive_boundary_authority import apply_content_anchor_override
from src.autoslice.frozen_boundary_receipt import (
    FrozenBoundaryReceipt,
    FrozenBoundaryReview,
    matching_final_delivery_review,
)
from src.autoslice.jingting_chunker import parse_srt_cues
from src.autoslice.reviewed_exact_source_interval import prepare_exact_delivery_review


def review_exact_delivery_boundary_semantics(
    *,
    review_final_boundary_semantics: Callable[..., dict[str, object]],
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
    frozen_review: FrozenBoundaryReview | None = None,
    replay_audit: dict[str, object] | None = None,
    delivery_srt_sha256: str | None = None,
    recording_date: str | None = None,
) -> dict[str, object]:
    """Re-review and bind the exact delivery grid after every text mutation."""

    final_cues, exact_review = prepare_exact_delivery_review(
        cues=cues,
        source_boundary_review=source_boundary_review,
        source_final_start_ms=source_final_start_ms,
        source_final_end_ms=source_final_end_ms,
        candidate_id=candidate_id,
        selection_scorecard=selection_scorecard,
    )
    if exact_review is not None:
        return exact_review
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
        frozen_review=frozen_review,
        replay_audit=replay_audit,
    )
    review = project_correlated_source_boundary_pass(
        review,
        source_review=source_boundary_review,
        closure_cue=final_cues[-1],
        final_cue_count=len(final_cues),
        source_final_start_ms=source_final_start_ms,
        source_final_end_ms=source_final_end_ms,
    )
    review = apply_content_anchor_override(
        review,
        candidate_id,
        recording_date,
        (source_final_start_ms, source_final_end_ms),
        delivery_srt_sha256,
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
    review_exact_delivery: Callable[..., dict[str, object]],
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
    frozen_boundary_receipt: FrozenBoundaryReceipt | None = None,
    frozen_source_review: FrozenBoundaryReview | None = None,
    recording_date: str | None = None,
) -> dict[str, object]:
    """Replace the source review with an exact post-mutation delivery review."""

    source_review = correction_audit.get("boundary_semantic_review")
    delivery_replay_audit: dict[str, object] = {}
    delivery_review = review_exact_delivery(
        cues=parse_srt_cues(final_srt_text),
        source_boundary_review=(
            source_review if isinstance(source_review, Mapping) else None
        ),
        source_final_start_ms=source_final_start_ms,
        source_final_end_ms=source_final_end_ms,
        candidate_id=candidate_id,
        recording_date=recording_date,
        selection_hook=selection_hook,
        selection_scorecard=selection_scorecard,
        structured_context=structured_context,
        candidate_context=candidate_context,
        boundary_max_forward_ms=boundary_max_forward_ms,
        llm_call=llm_call,
        extract_json=extract_json,
        disabled=disabled,
        frozen_review=(
            matching_final_delivery_review(frozen_boundary_receipt, final_srt_text)
            if frozen_boundary_receipt is not None
            else frozen_source_review
        ),
        replay_audit=delivery_replay_audit,
        delivery_srt_sha256="sha256:"
        + hashlib.sha256(final_srt_text.encode("utf-8")).hexdigest(),
    )
    result = dict(correction_audit)
    result["boundary_semantic_review"] = delivery_review
    if delivery_replay_audit:
        existing_replay = result.get("boundary_receipt_replay")
        replay = dict(existing_replay) if isinstance(existing_replay, Mapping) else {}
        replay["final_delivery"] = delivery_replay_audit
        result["boundary_receipt_replay"] = replay
    return result


__all__ = [
    "exact_delivery_correction_audit",
    "review_exact_delivery_boundary_semantics",
]
