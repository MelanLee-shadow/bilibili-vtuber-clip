"""Typed validators shared by the portable boundary package gate."""

from __future__ import annotations

import re
from typing import Any


_SHA256_RX = re.compile(r"^sha256:[0-9a-f]{64}$")


def is_boundary_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def semantic_boundary_review_is_valid(
    review: object,
    *,
    expected_scope: str,
) -> bool:
    if not isinstance(review, dict):
        return False
    endpoint = review.get("final_endpoint_binding")
    reviewed_grid = str(review.get("cue_grid_sha256") or "")
    evidence = review.get("evidence_cue_indexes")
    return bool(
        review.get("schema_version")
        == "talk-boundary-semantic-review.v1"
        and review.get("status") == "PASS"
        and review.get("review_scope") == expected_scope
        and all(
            review.get(field) is True
            for field in (
                "syntax_complete",
                "story_closed",
                "next_topic_separated",
                "content_anchor_covered",
                "next_topic_witness_valid",
            )
        )
        and is_boundary_int(review.get("recommended_end_ms"))
        and is_boundary_int(
            review.get("recommended_end_cue_index")
        )
        and isinstance(evidence, list)
        and bool(evidence)
        and all(is_boundary_int(value) for value in evidence)
        and isinstance(review.get("selector_story_witness"), dict)
        and review["selector_story_witness"].get("status") == "PASS"
        and _SHA256_RX.fullmatch(
            str(review.get("request_sha256") or "")
        )
        is not None
        and _SHA256_RX.fullmatch(reviewed_grid) is not None
        and isinstance(endpoint, dict)
        and endpoint.get("schema_version")
        == "talk-boundary-final-endpoint-binding.v1"
        and endpoint.get("status") == "PASS"
        and endpoint.get("reason_codes") == []
        and all(
            is_boundary_int(endpoint.get(field))
            for field in (
                "recommended_end_cue_index",
                "recommended_end_ms",
                "final_closure_cue_index",
                "final_snapped_end_ms",
                "final_start_ms",
                "final_end_ms",
            )
        )
        and endpoint.get("semantic_request_sha256")
        == review.get("request_sha256")
        and endpoint.get("recommended_end_cue_index")
        == review.get("recommended_end_cue_index")
        and endpoint.get("recommended_end_ms")
        == review.get("recommended_end_ms")
        and endpoint.get("final_closure_cue_index")
        == review.get("recommended_end_cue_index")
        and endpoint.get("final_snapped_end_ms")
        == review.get("recommended_end_ms")
        and endpoint.get("semantic_cue_grid_sha256")
        == reviewed_grid
        and endpoint.get("final_cue_grid_sha256") == reviewed_grid
        and _SHA256_RX.fullmatch(
            str(endpoint.get("closure_text_sha256") or "")
        )
        is not None
    )


def expected_boundary_authority(
    record: dict[str, Any],
    audit: dict[str, Any],
    *,
    human_authority: str,
) -> tuple[str, bool]:
    if not human_authority:
        return (
            "correlated_semantic_review_plus_deterministic_guards",
            True,
        )
    publication = record.get("recovery_publication_authority")
    publication_mode = (
        publication.get("boundary_end_mode")
        if isinstance(publication, dict)
        else None
    )
    expected_mode = (
        "exact_source_pin"
        if publication_mode == "exact_source_pin"
        else "semantic_lower_bound"
    )
    expected_authority = (
        "human_source_exact_pin_plus_semantic_review"
        if expected_mode == "exact_source_pin"
        else "human_source_reviewed_lower_bound_plus_semantic_review"
    )
    return (
        expected_authority,
        audit.get("manual_end_mode") == expected_mode,
    )
