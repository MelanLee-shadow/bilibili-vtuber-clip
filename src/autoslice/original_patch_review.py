"""Generate and replay real technical delta evidence without false human review."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from src.autoslice.original_patch_package import (
    TECHNICAL_SCHEMA,
    ROOT,
    json_file,
    need,
    sha_file,
    validate_package,
)


def _bindings(root, audit_path, *, repo_root=ROOT):
    from scripts.audit_review_package import audit_package
    from src.autoslice.package_audit_binding import audit_content_binding

    closure = validate_package(root, repo_root=repo_root)
    saved = json_file(audit_path)
    current = audit_package(root)
    need(
        saved.get("passed") is True
        and current.get("passed") is True
        and saved.get("blocking_issue_count") == current.get("blocking_issue_count") == 0,
        "original technical package audit not passed",
    )
    need(audit_content_binding(saved) == audit_content_binding(current), "technical audit drift")
    return {
        "closure": closure,
        "review_manifest_sha256": sha_file(Path(root) / "review_manifest.json"),
        "package_audit_sha256": sha_file(audit_path),
    }


def build_technical_receipt(root, audit_path, *, repo_root=ROOT):
    bindings = _bindings(root, audit_path, repo_root=repo_root)
    return {
        "schema_version": TECHNICAL_SCHEMA,
        "candidate_id": bindings["closure"]["candidate_id"],
        "reviewed_at": datetime.now().astimezone().isoformat(),
        "reviewed_by": "ChatGPT root",
        "status": "VERIFIED_ORIGINAL_PLUS_DELTA",
        "scope": "original_review_preserved_and_actual_delta_verified",
        "fresh_human_full_playback_claimed": False,
        "new_upload_authorized": False,
        "bindings": bindings,
    }


def validate_technical_receipt(value, root, audit_path, *, publication_authority, repo_root=ROOT):
    need(
        isinstance(value, dict)
        and set(value)
        == {
            "schema_version",
            "candidate_id",
            "reviewed_at",
            "reviewed_by",
            "status",
            "scope",
            "fresh_human_full_playback_claimed",
            "new_upload_authorized",
            "bindings",
        },
        "technical receipt fields",
    )
    expected = _bindings(root, audit_path, repo_root=repo_root)
    need(
        value["schema_version"] == TECHNICAL_SCHEMA
        and value["status"] == "VERIFIED_ORIGINAL_PLUS_DELTA"
        and value["scope"] == "original_review_preserved_and_actual_delta_verified"
        and value["fresh_human_full_playback_claimed"] is False
        and value["new_upload_authorized"] is False,
        "technical evidence mislabeled or overclaiming",
    )
    need(
        value["candidate_id"] == expected["closure"]["candidate_id"]
        and value["bindings"] == expected,
        "technical receipt bindings drift",
    )
    need(publication_authority == expected["closure"]["authority"], "repair target not bound")
    need(value["reviewed_by"] == "ChatGPT root", "technical actor mismatch")
    at = datetime.fromisoformat(value["reviewed_at"].replace("Z", "+00:00"))
    need(at.utcoffset() is not None, "technical timestamp has no timezone")
    return value
