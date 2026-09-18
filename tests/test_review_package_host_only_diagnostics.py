"""Portable regression for precise HOST_ONLY package diagnostics."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from scripts.audit_review_package import (
    _audit_host_only_v4_package_binding,
    _audit_story_bound_cover,
    _host_only_identity_route_blocker_detail,
)
from src.autoslice.cover_route_evidence import (
    build_cover_route_decision,
    record_cover_route_execution,
)
from src.autoslice.host_only_v4_package_binding import (
    BINDING_ISSUE_CODE,
    BINDING_ITEM_KEY,
    build_binding,
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



def _json_bytes(value: dict[str, object]) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")


def _v4_bound_package(tmp_path):
    root = tmp_path / "package"
    evidence = root / "evidence"
    evidence.mkdir(parents=True)
    candidate = "candidate"
    cover = root / "cover.png"
    reference = evidence / "reference.png"
    comparison = evidence / "comparison.png"
    cover.write_bytes(b"cover pixels")
    reference.write_bytes(b"reference pixels")
    comparison.write_bytes(b"comparison pixels")
    def sha(path: Path) -> str:
        return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()

    verification = {
        "schema_version": "lidousha-cover-final-host-identity-verification.v4",
        "authority": (
            "CPA_PRIMARY_HASH_BOUND_SOURCE_FINAL_IDENTITY_PROMINENCE_"
            "AND_HOST_ONLY_COMPARISON"
        ),
        "status": "PASS",
        "host_only_required": True,
        "final_cover_path": cover.name,
        "final_cover_sha256": sha(cover),
        "reference_path": reference.relative_to(root).as_posix(),
        "reference_sha256": sha(reference),
        "comparison_path": comparison.relative_to(root).as_posix(),
        "comparison_sha256": sha(comparison),
        "witness": {
            "status": "OBSERVED",
            "provider": "cpa",
            "image_path": comparison.relative_to(root).as_posix(),
            "image_sha256": sha(comparison).removeprefix("sha256:"),
        },
    }
    receipt = evidence / "receipt.json"
    receipt_bytes = _json_bytes(verification)
    receipt.write_bytes(receipt_bytes)
    item = {
        "candidate_id": candidate,
        "cover": cover.name,
        BINDING_ITEM_KEY: build_binding(
            candidate_id=candidate,
            receipt_path=receipt.relative_to(root).as_posix(),
            receipt_bytes=receipt_bytes,
            comparison_path=comparison.relative_to(root).as_posix(),
            comparison_bytes=comparison.read_bytes(),
            reference_path=reference.relative_to(root).as_posix(),
            reference_bytes=reference.read_bytes(),
            final_cover_path=cover.name,
            final_cover_bytes=cover.read_bytes(),
        ),
    }
    generation = {"final_host_identity_verification": verification}
    return root, item, generation, comparison


def _binding_issues(root, item, generation):
    issues: list[dict] = []
    _audit_host_only_v4_package_binding(
        root=root,
        item=item,
        generation=generation,
        issues=issues,
        stem="candidate",
        record_path=root / "candidate.record.json",
    )
    return issues


def test_host_only_v4_binding_rehashes_all_package_bytes(tmp_path) -> None:
    root, item, generation, comparison = _v4_bound_package(tmp_path)
    assert _binding_issues(root, item, generation) == []

    comparison.write_bytes(b"drifted after witness validation")
    issues = _binding_issues(root, item, generation)
    assert [row["code"] for row in issues] == [BINDING_ISSUE_CODE]
    assert "comparison_sha256 does not match package bytes" in issues[0]["detail"]


def test_host_only_v4_requires_explicit_package_binding(tmp_path) -> None:
    root, item, generation, _comparison = _v4_bound_package(tmp_path)
    item.pop(BINDING_ITEM_KEY)
    issues = _binding_issues(root, item, generation)
    assert [row["code"] for row in issues] == [BINDING_ISSUE_CODE]
    assert "lacks HOST_ONLY v4 binding" in issues[0]["detail"]


def test_host_only_v4_binding_rejects_symlinked_comparison(tmp_path) -> None:
    root, item, generation, comparison = _v4_bound_package(tmp_path)
    external = tmp_path / "external.png"
    external.write_bytes(comparison.read_bytes())
    comparison.unlink()
    comparison.symlink_to(external)
    issues = _binding_issues(root, item, generation)
    assert [row["code"] for row in issues] == [BINDING_ISSUE_CODE]
    assert "symlink component" in issues[0]["detail"]
