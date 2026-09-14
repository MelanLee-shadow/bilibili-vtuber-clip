"""Current-package mechanical acceptance without inventing human playback.

维护者's rule: a validated burn pipeline plus its actual current
input/output checks does not need another full viewing of every product.
Semantic, source, boundary, cover and publication authority stay with the
existing canonical gates. This receipt is an alternative to a new human claim,
not an upload permission or a way to relabel failed upstream evidence.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Mapping

from scripts.audit_review_package import (
    AUDIT_POLICY_EPOCH,
    AUDIT_SCHEMA_VERSION,
    audit_package,
)
from src.autoslice import final_human_review as review
from src.autoslice.package_audit_binding import audit_binding
from src.autoslice.producer_media import _validated_burned_artifact, _validated_burned_ass_artifact
from src.autoslice.review_package_portable_evidence import contained_package_artifact

SCHEMA_VERSION = "lidousha-mechanical-delivery-review.v1"
STATUS = "VERIFIED_MECHANICAL_DELIVERY"
_FIELDS = frozenset({
    "schema_version", "status", "scope", "checked_at", "checked_by",
    "fresh_human_full_playback_claimed", "new_upload_authorized", "bindings",
})


def _need(condition: bool, detail: str) -> None:
    if not condition:
        raise ValueError("mechanical delivery review: " + detail)


def _binding(root: Path, relative: str) -> dict[str, object]:
    path = review._regular_package_file(root, relative)
    return {"path": relative, "sha256": review._sha256(path), "bytes": path.stat().st_size}


def _bindings(package_root: Path, audit_path: Path) -> dict[str, object]:
    root = review._package_directory(package_root)
    audit_relative = str(audit_path.absolute().relative_to(root))
    safe_audit = review._regular_package_file(root, audit_relative)
    manifest_path = review._regular_package_file(root, "review_manifest.json")
    saved = review._json_object(safe_audit, source="package audit")
    current = audit_package(root)
    _need(
        current.get("schema_version") == AUDIT_SCHEMA_VERSION
        and current.get("policy_epoch") == AUDIT_POLICY_EPOCH
        and current.get("passed") is True
        and current.get("blocking_issue_count") == 0,
        "current canonical audit rejected the package",
    )
    _need(audit_binding(saved) == audit_binding(current), "canonical audit is stale")
    manifest = review._json_object(manifest_path, source="review manifest")
    # Reuse the exact current candidate/title/target/cover/intro identity checks;
    # do not reuse or fabricate a human observer's eight textual observations.
    order, closures = review._manifest_items(manifest, package_root=root)
    items = []
    for candidate_id in order:
        closure = closures[candidate_id]
        record_path = str(closure["record_path"])
        record = review._json_object(
            review._regular_package_file(root, record_path), source="record",
        )
        preview = record.get("burned_preview")
        _need(isinstance(preview, dict), "burn record is missing")
        video = contained_package_artifact(root, preview.get("path"), label="burned video")
        ass = contained_package_artifact(root, preview.get("ass_path"), label="burned ASS")
        _need(video == review._regular_package_file(root, closure["artifacts"]["video"]),
              "burned video differs from the manifest video")
        # Portable paths are projected only in memory. Reuse the producer's own
        # status and dual video/ASS hash checks, rather than trust a PASS label.
        portable_record = {**record, "burned_preview": {
            **preview, "path": str(video), "ass_path": str(ass),
        }}
        try:
            _validated_burned_artifact(portable_record)
            _validated_burned_ass_artifact(portable_record)
        except RuntimeError as exc:
            raise ValueError("mechanical delivery review: burn output binding invalid") from exc
        items.append({
            "candidate_id": candidate_id,
            "burned_ass": _binding(root, ass.relative_to(root).as_posix()),
            "title": closure["title"],
            "publication_target": closure["publication_target"],
            "publication_authority": record["recovery_publication_authority"],
            "final_duration_ms": closure["final_duration_ms"],
            "record": _binding(root, record_path),
            "artifacts": {
                kind: _binding(root, str(relative))
                for kind, relative in closure["artifacts"].items()
            },
        })
    return {
        "review_manifest": _binding(root, "review_manifest.json"),
        "package_audit": _binding(root, audit_relative),
        "canonical_audit": audit_binding(current),
        "items": items,
    }


def build_mechanical_receipt(package_root: Path, audit_path: Path) -> dict[str, object]:
    return {
        "schema_version": SCHEMA_VERSION,
        "status": STATUS,
        "scope": "same_bv_validated_pipeline_current_package",
        "checked_at": datetime.now().astimezone().isoformat(),
        "checked_by": "canonical-package-auditor",
        "fresh_human_full_playback_claimed": False,
        "new_upload_authorized": False,
        "bindings": _bindings(package_root, audit_path),
    }


def validate_mechanical_receipt(
    value: Mapping[str, object], package_root: Path, audit_path: Path,
    *, publication_authority: object = None,
) -> dict[str, object]:
    _need(isinstance(value, Mapping) and set(value) == _FIELDS, "invalid receipt fields")
    _need(
        value["schema_version"] == SCHEMA_VERSION and value["status"] == STATUS
        and value["scope"] == "same_bv_validated_pipeline_current_package"
        and value["checked_by"] == "canonical-package-auditor"
        and value["fresh_human_full_playback_claimed"] is False
        and value["new_upload_authorized"] is False,
        "receipt overclaims review or authorization",
    )
    _need(isinstance(value["checked_at"], str), "invalid timestamp")
    at = datetime.fromisoformat(value["checked_at"].replace("Z", "+00:00"))
    _need(at.utcoffset() is not None, "timestamp lacks timezone")
    expected = _bindings(package_root, audit_path)
    _need(value["bindings"] == expected, "current package binding drift")
    if publication_authority is not None:
        _need(
            isinstance(publication_authority, Mapping)
            and sum(item["publication_authority"] == publication_authority
                    for item in expected["items"]) == 1,
            "publication target does not match the audited package",
        )
    return dict(value)
