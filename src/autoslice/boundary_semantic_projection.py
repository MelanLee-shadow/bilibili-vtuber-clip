"""Deterministic reconciliation for correlated source/delivery boundary votes."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping

from src.autoslice.boundary_semantic_review import semantic_review_sha256


def project_correlated_source_boundary_pass(
    delivery_review: Mapping[str, object],
    *,
    source_review: Mapping[str, object] | None,
    closure_cue: object,
    final_cue_count: int,
    source_final_start_ms: int,
    source_final_end_ms: int,
) -> dict[str, object]:
    """Resolve a same-family contradiction only for an exact closure projection.

    The source review sees the post-end topic witness directly.  When it and
    the delivery review bind the same terminal text and interval, a later
    delivery-local BLOCK is not an independent vote.  Project the richer
    source PASS only when the delivery reviewer found no continuing story or
    missing content anchor.  Any text/interval drift remains BLOCK.
    """

    result = dict(delivery_review)
    if not isinstance(source_review, Mapping):
        return result
    source_endpoint = source_review.get("final_endpoint_binding")
    witness = delivery_review.get("source_separation_witness")
    closure_text = str(getattr(closure_cue, "text", "") or "")
    closure_end_ms = getattr(closure_cue, "end_ms", None)
    closure_sha256 = (
        "sha256:" + hashlib.sha256(closure_text.encode("utf-8")).hexdigest()
        if closure_text
        else None
    )
    exact_projection = bool(
        delivery_review.get("status") == "BLOCK"
        and delivery_review.get("review_scope") == "final_delivery"
        and delivery_review.get("content_anchor_covered") is True
        and delivery_review.get("same_topic_continues_after_target") is False
        and delivery_review.get("needs_more_context") is False
        and delivery_review.get("recommended_end_cue_index") == final_cue_count
        and delivery_review.get("recommended_end_ms") == closure_end_ms
        and source_review.get("status") == "PASS"
        and source_review.get("review_scope") == "source_full_window"
        and source_review.get("syntax_complete") is True
        and source_review.get("story_closed") is True
        and source_review.get("next_topic_separated") is True
        and isinstance(source_endpoint, Mapping)
        and source_endpoint.get("status") == "PASS"
        and source_endpoint.get("closure_text_sha256") == closure_sha256
        and source_endpoint.get("final_start_ms") == source_final_start_ms
        and source_endpoint.get("final_end_ms") == source_final_end_ms
        and source_review.get("recommended_end_ms")
        == source_final_start_ms + int(closure_end_ms or -1)
        and isinstance(witness, Mapping)
        and witness.get("status") == "PASS"
        and witness.get("source_review_sha256")
        == semantic_review_sha256(source_review)
        and witness.get("source_final_start_ms") == source_final_start_ms
        and witness.get("source_final_end_ms") == source_final_end_ms
    )
    if not exact_projection:
        return result
    result.update(
        status="PASS",
        syntax_complete=True,
        story_closed=True,
        next_topic_separated=True,
        next_topic_witness_valid=True,
        evidence_cue_indexes=[final_cue_count],
        reason_codes=[],
        correlated_source_projection={
            "schema_version": "correlated-source-boundary-projection.v1",
            "status": "PASS",
            "decision_authority": "EXACT_SOURCE_SEMANTIC_PROJECTION",
            "source_review_sha256": witness.get("source_review_sha256"),
            "source_request_sha256": witness.get("source_request_sha256"),
            "source_cue_grid_sha256": witness.get("source_cue_grid_sha256"),
            "closure_text_sha256": closure_sha256,
            "source_final_start_ms": source_final_start_ms,
            "source_final_end_ms": source_final_end_ms,
            "delivery_block_summary": delivery_review.get("summary"),
        },
        summary=(
            "delivery-local 相关复审与同文本 source PASS 冲突；"
            "按精确 closure/interval 投影采用完整 source 语义闭环。"
        ),
    )
    return result
