"""Boundary-contract checks for portable Li Dousha review packages."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
import re
from typing import Any

from src.autoslice.boundary_endpoint_binding import (
    final_delivery_review_matches_srt,
)
from src.autoslice.boundary_semantic_review import semantic_review_sha256
from src.autoslice.producer_boundary import TAIL_PAD_MS


_SHA256_RX = re.compile(r"^sha256:[0-9a-f]{64}$")


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _semantic_review_is_valid(
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
        and _is_int(review.get("recommended_end_ms"))
        and _is_int(review.get("recommended_end_cue_index"))
        and isinstance(evidence, list)
        and bool(evidence)
        and all(_is_int(value) for value in evidence)
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
            _is_int(endpoint.get(field))
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
        and endpoint.get("final_cue_grid_sha256")
        == reviewed_grid
        and _SHA256_RX.fullmatch(
            str(endpoint.get("closure_text_sha256") or "")
        )
        is not None
    )


def audit_boundary_contract(
    *,
    issue_adder: Callable[..., None],
    issues: list[dict[str, Any]],
    stem: str,
    record_path: Path | None,
    subtitle_path: Path | None,
    exact_final_review: object,
    record: dict[str, Any],
    story_contract: dict[str, Any],
    required: bool,
    is_song: bool,
) -> None:
    if not required or is_song:
        return
    audit = record.get("boundary_audit")
    if not isinstance(audit, dict):
        issue_adder(
            issues,
            "BOUNDARY_AUDIT_MISSING",
            stem=stem,
            path=record_path,
        )
        return
    final_review = story_contract.get("boundary_semantic_review")
    source_review = audit.get("boundary_semantic_review")
    audit_final_review = audit.get(
        "final_delivery_boundary_semantic_review"
    )
    final_endpoint = (
        final_review.get("final_endpoint_binding")
        if isinstance(final_review, dict)
        else None
    )
    source_endpoint = (
        source_review.get("final_endpoint_binding")
        if isinstance(source_review, dict)
        else None
    )
    final_review_valid = _semantic_review_is_valid(
        final_review,
        expected_scope="final_delivery",
    )
    source_review_valid = _semantic_review_is_valid(
        source_review,
        expected_scope="source_full_window",
    )
    if not final_review_valid or not source_review_valid:
        issue_adder(
            issues,
            "BOUNDARY_SEMANTIC_REVIEW_NOT_PASS",
            stem=stem,
            path=record_path,
        )
    human_authority = str(
        story_contract.get("human_boundary_authority") or ""
    ).strip()
    expected_authority = (
        "human_source_reviewed_lower_bound_plus_semantic_review"
        if human_authority
        else "correlated_semantic_review_plus_deterministic_guards"
    )
    if audit.get("boundary_authority") != expected_authority:
        issue_adder(
            issues,
            (
                "HUMAN_BOUNDARY_AUTHORITY_DRIFT"
                if human_authority
                else "BOUNDARY_MULTI_WITNESS_AUTHORITY_MISSING"
            ),
            stem=stem,
            path=record_path,
        )
    if human_authority:
        if (
            str(audit.get("manual_end_authority") or "").strip()
            != human_authority
        ):
            issue_adder(
                issues,
                "HUMAN_BOUNDARY_AUTHORITY_DRIFT",
                stem=stem,
                path=record_path,
            )
    if audit_final_review != final_review:
        issue_adder(
            issues,
            "BOUNDARY_SEMANTIC_REVIEW_BINDING_DRIFT",
            stem=stem,
            path=record_path,
        )
    exact_final_boundary = exact_final_review.get(
        "boundary_semantic_review"
    ) if isinstance(exact_final_review, dict) else None
    if exact_final_boundary != final_review:
        issue_adder(
            issues,
            "BOUNDARY_FINAL_REVIEW_BINDING_DRIFT",
            stem=stem,
            path=record_path,
        )
    if not final_delivery_review_matches_srt(final_review, subtitle_path):
        issue_adder(
            issues,
            "BOUNDARY_FINAL_DELIVERY_CUE_GRID_MISMATCH",
            stem=stem,
            path=subtitle_path or record_path,
        )
    source_recommended_end_ms = (
        source_review.get("recommended_end_ms")
        if isinstance(source_review, dict)
        else None
    )
    snapped_end_ms = audit.get("snapped_sentence_end_ms")
    final_start_ms = audit.get("final_start_ms")
    final_end_ms = audit.get("final_end_ms")
    source_endpoint_valid = bool(
        isinstance(source_endpoint, dict)
        and _is_int(final_start_ms)
        and _is_int(final_end_ms)
        and final_end_ms > final_start_ms
        and _is_int(source_endpoint.get("final_start_ms"))
        and _is_int(source_endpoint.get("final_end_ms"))
        and source_endpoint.get("final_start_ms") == final_start_ms
        and source_endpoint.get("final_end_ms") == final_end_ms
        and source_endpoint.get("final_snapped_end_ms")
        == snapped_end_ms
    )
    duration_ms = (
        final_end_ms - final_start_ms
        if (
            _is_int(final_start_ms)
            and _is_int(final_end_ms)
            and final_end_ms > final_start_ms
        )
        else None
    )
    final_endpoint_valid = bool(
        isinstance(final_endpoint, dict)
        and _is_int(duration_ms)
        and _is_int(final_endpoint.get("final_start_ms"))
        and _is_int(final_endpoint.get("final_end_ms"))
        and final_endpoint.get("final_start_ms") == 0
        and final_endpoint.get("final_end_ms") == duration_ms
        and _is_int(final_endpoint.get("final_snapped_end_ms"))
        and 0
        <= final_endpoint.get("final_snapped_end_ms")
        <= duration_ms
    )
    source_witness = (
        final_review.get("source_separation_witness")
        if isinstance(final_review, dict)
        else None
    )
    source_witness_valid = bool(
        isinstance(source_witness, dict)
        and source_witness.get("schema_version")
        == "talk-boundary-source-separation-witness.v1"
        and source_witness.get("status") == "PASS"
        and source_witness.get("reason_codes") == []
        and isinstance(source_review, dict)
        and source_witness.get("source_review_sha256")
        == semantic_review_sha256(source_review)
        and source_witness.get("source_request_sha256")
        == source_review.get("request_sha256")
        and source_witness.get("source_cue_grid_sha256")
        == source_review.get("cue_grid_sha256")
        and _is_int(source_witness.get("source_recommended_end_ms"))
        and source_witness.get("source_recommended_end_ms")
        == source_recommended_end_ms
        and _is_int(source_witness.get("source_final_start_ms"))
        and _is_int(source_witness.get("source_final_end_ms"))
        and source_witness.get("source_final_start_ms")
        == final_start_ms
        and source_witness.get("source_final_end_ms") == final_end_ms
    )
    if not source_witness_valid:
        issue_adder(
            issues,
            "BOUNDARY_SOURCE_SEPARATION_WITNESS_INVALID",
            stem=stem,
            path=record_path,
        )
    if not final_endpoint_valid:
        issue_adder(
            issues,
            "BOUNDARY_FINAL_DELIVERY_ENDPOINT_INVALID",
            stem=stem,
            path=record_path,
        )
    delivery_lower_bound_ms = audit.get(
        "delivery_coverage_lower_bound_ms"
    )
    delivery_verification = audit.get(
        "delivery_coverage_verification"
    )
    tail_bridge = audit.get("tail_pad_coverage_bridge")
    if (
        not _is_int(source_recommended_end_ms)
        or not _is_int(snapped_end_ms)
        or snapped_end_ms != source_recommended_end_ms
        or not _is_int(final_end_ms)
        or final_end_ms < snapped_end_ms
        or not source_endpoint_valid
        or source_endpoint.get("final_snapped_end_ms")
        != snapped_end_ms
        or source_endpoint.get("final_end_ms") != final_end_ms
    ):
        issue_adder(
            issues,
            "BOUNDARY_RECOMMENDED_END_NOT_MATERIALIZED",
            stem=stem,
            path=record_path,
        )
    delivery_valid = bool(
        _is_int(delivery_lower_bound_ms)
        and _is_int(final_end_ms)
        and final_end_ms >= delivery_lower_bound_ms
        and isinstance(delivery_verification, dict)
        and delivery_verification.get("status") == "PASS"
        and delivery_verification.get("failure") is None
        and isinstance(tail_bridge, dict)
        and tail_bridge.get("status") in {"USED", "NOT_NEEDED"}
        and tail_bridge.get("delivery_lower_bound_ms")
        == delivery_lower_bound_ms
        and _is_int(tail_bridge.get("closure_lower_bound_ms"))
        and tail_bridge.get("closure_lower_bound_ms")
        == source_recommended_end_ms
        and _is_int(tail_bridge.get("maximum_tail_pad_ms"))
        and tail_bridge.get("maximum_tail_pad_ms") == TAIL_PAD_MS
        and (
            (
                tail_bridge.get("status") == "NOT_NEEDED"
                and tail_bridge.get("closure_lower_bound_ms")
                == delivery_lower_bound_ms
            )
            or (
                tail_bridge.get("status") == "USED"
                and tail_bridge.get("closure_lower_bound_ms")
                < delivery_lower_bound_ms
                and delivery_lower_bound_ms
                - tail_bridge.get("closure_lower_bound_ms")
                <= TAIL_PAD_MS
            )
        )
    )
    if not delivery_valid:
        issue_adder(
            issues,
            "BOUNDARY_DELIVERY_COVERAGE_INVALID",
            stem=stem,
            path=record_path,
        )
