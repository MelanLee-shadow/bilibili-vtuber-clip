"""Deterministic source-window coverage gate for boundary review."""

from __future__ import annotations

from collections.abc import Mapping


def source_context_coverage_block(
    *,
    scope: Mapping[str, object],
    available_local_source_context_end_ms: int | None,
    candidate_id: str,
    boundary_max_forward_ms: int,
) -> dict[str, object] | None:
    """Return a typed pre-LLM block, or ``None`` when coverage is complete."""

    required_context_end_ms = scope.get(
        "required_local_source_context_end_ms"
    )
    coverage_valid = (
        isinstance(available_local_source_context_end_ms, int)
        and not isinstance(available_local_source_context_end_ms, bool)
        and available_local_source_context_end_ms >= 0
        and isinstance(required_context_end_ms, int)
        and not isinstance(required_context_end_ms, bool)
        and required_context_end_ms >= 0
    )
    coverage = {
        "schema_version": "talk-boundary-source-context-coverage.v1",
        "status": "BLOCK",
        "scope_sha256": scope.get("scope_sha256"),
        "available_local_source_context_end_ms": (
            available_local_source_context_end_ms
        ),
        "required_local_source_context_end_ms": required_context_end_ms,
        "deficit_ms": None,
    }
    result: dict[str, object] = {
        "schema_version": "talk-boundary-semantic-review.v1",
        "status": "BLOCK",
        "review_scope": "source_full_window",
        "candidate_id": candidate_id,
        "max_forward_ms": boundary_max_forward_ms,
        "boundary_search_scope": dict(scope),
        "source_context_coverage": coverage,
    }
    if not coverage_valid:
        result.update(
            {
                "needs_more_context": False,
                "retry_scope": "none",
                "reason_codes": [
                    "BOUNDARY_SOURCE_CONTEXT_COVERAGE_INVALID"
                ],
            }
        )
        return result
    if available_local_source_context_end_ms >= required_context_end_ms:
        return None
    coverage["deficit_ms"] = (
        required_context_end_ms - available_local_source_context_end_ms
    )
    result.update(
        {
            "needs_more_context": True,
            "same_topic_continues_after_target": False,
            "retry_scope": "source_witness_reserve",
            "reason_codes": [
                "BOUNDARY_SOURCE_WITNESS_RESERVE_INCOMPLETE"
            ],
        }
    )
    return result
