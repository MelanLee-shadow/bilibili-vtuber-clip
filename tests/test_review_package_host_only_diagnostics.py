"""Portable regression for precise HOST_ONLY package diagnostics."""

from __future__ import annotations

import json

from scripts.audit_review_package import (
    _audit_story_bound_cover,
    _host_only_identity_route_blocker_detail,
)
from src.autoslice.cover_route_evidence import (
    build_cover_route_decision,
    record_cover_route_execution,
)


def test_host_only_v3_route_reports_exact_v4_witness_blocker() -> None:
    story = {
        "schema_version": "lidousha-story-contract.v1",
        "selection_hook": "主播说自己被内定",
        "relation_state": "UNKNOWN",
        "participants": [],
        "cover_reference_authority": None,
        "cover_fallback_mode": "HOST_ONLY_GENERIC",
    }
    final_sha256 = "sha256:" + "1" * 64
    comparison_digest = "2" * 64
    generation = {
        "candidate_id": "host-only-v3-diagnostic",
        "title": "主播说自己被内定",
        "cover_text": "主播被内定",
        "method": "screenshot_direct",
        "cover_origin": "SOURCE_SCREENSHOT",
        "story_contract": story,
        "source_composition_verification": {"scene_kind": "talk"},
        "final_cover_sha256": final_sha256,
        "final_host_identity_verification": {
            "schema_version": (
                "lidousha-cover-final-host-identity-verification.v3"
            ),
            "authority": (
                "CPA_PRIMARY_HASH_BOUND_SOURCE_FINAL_IDENTITY_AND_PROMINENCE_COMPARISON"
            ),
            "status": "PASS",
            "final_cover_sha256": final_sha256,
            "comparison_sha256": "sha256:" + comparison_digest,
            "witness": {
                "status": "OBSERVED",
                "provider": "cpa",
                "image_sha256": comparison_digest,
            },
        },
    }
    generation["route_decision"] = build_cover_route_decision(
        selected_treatment="screenshot_direct",
        selected_rationale="verified real screenshot",
        story_contract=story,
        reference_authority=None,
        decision_inputs={
            "cover_mode": "screenshot",
            "subject_confident": True,
            "verified_stream_frame": True,
            "thumbnail_text_requires_punch": False,
        },
        title=generation["title"],
        cover_text=generation["cover_text"],
        source_composition_verification={"scene_kind": "talk"},
    )
    record_cover_route_execution(
        generation,
        actual_treatment="screenshot_direct",
        execution_status="READY",
        image_generation_attempted=False,
        image_generation_used=False,
    )

    detail = _host_only_identity_route_blocker_detail(generation)

    assert detail
    parsed = json.loads(detail)
    assert parsed["required_schema_version"] == (
        "lidousha-cover-final-host-identity-verification.v4"
    )
    assert parsed["actual_schema_version"] == (
        "lidousha-cover-final-host-identity-verification.v3"
    )
    issues: list[dict] = []
    _audit_story_bound_cover(
        issues=issues,
        stem="host-only-v3-diagnostic",
        record_path=None,
        record={
            "publish_staging": {"cover_text": generation["cover_text"]},
            "cover_generation": generation,
        },
        story_contract=story,
        required=True,
    )
    codes = {row["code"] for row in issues}
    assert "COVER_HOST_ONLY_IDENTITY_VERIFICATION_MISSING_OR_STALE" in codes
    assert "COVER_ROUTE_DECISION_MISSING_OR_INVALID" not in codes
