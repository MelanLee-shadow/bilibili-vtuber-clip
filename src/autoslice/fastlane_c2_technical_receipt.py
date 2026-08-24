"""C2-only technical receipt bridge; it is not an upload authorization lane."""
from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
from typing import Any, Mapping

from .fastlane_c2_formal_adapter import CID, NAMES, TITLE, sha256, visual_inventory

PROPOSAL_SCHEMA = "fastlane-c2-root-technical-receipt-proposal.v1"
ACCEPTED_SCHEMA = "fastlane-c2-accepted-technical-receipt.v1"
READY = "READY_FOR_ROOT_TECHNICAL_ACCEPTANCE"
SCOPE = "C2_NEW_BV_STRICT_PACKAGE_BRIDGE_EXECUTION_RECEIPT_ONLY"
ROOT_CHECKS = [
    "cue5 start/mid/end burned evidence",
    "cue21 start/mid/end burned evidence",
    "intro transition",
    "full burned playback",
    "title and cover visual surface",
]


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read JSON {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"JSON object required: {path}")
    return value


def _regular(path: Path) -> None:
    if not path.is_file() or path.is_symlink():
        raise ValueError(f"regular non-symlink file required: {path}")


def _binding(root: Path, current_audit: Mapping[str, Any]) -> dict[str, Any]:
    manifest = _read_json(root / "review_manifest.json")
    artifacts = {
        key: {"path": name, "sha256": "sha256:" + sha256(root / name)}
        for key, name in NAMES.items()
    }
    return {
        "review_manifest_sha256": "sha256:" + sha256(root / "review_manifest.json"),
        "package_audit_sha256": "sha256:" + sha256(root / "package_audit.json"),
        "audit_policy_fingerprint": current_audit["policy_fingerprint"],
        "artifacts": artifacts,
        "visual_evidence_inventory": manifest["visual_evidence_inventory"],
    }


def make_ready_proposal(root: Path, current_audit: Mapping[str, Any]) -> dict[str, Any]:
    if current_audit.get("passed") is not True or current_audit.get("blocking_issue_count") != 0:
        raise ValueError("current C2 package audit is not a clean pass")
    return {
        "schema_version": PROPOSAL_SCHEMA,
        "candidate_id": CID,
        "title": TITLE,
        "accepted": False,
        "upload_allowed": False,
        "status": READY,
        "bindings": _binding(root, current_audit),
        "required_root_checks": ROOT_CHECKS,
        "root_content_check": "Root checked the exact current visual evidence; the fresh deployed auditor has been reclosed on these exact bytes.",
        "reviewer": "Codex root",
        "reviewed_at": None,
    }


def _validate_bindings(root: Path, bindings: object) -> None:
    if not isinstance(bindings, Mapping):
        raise ValueError("receipt bindings must be an object")
    required = {"review_manifest_sha256", "package_audit_sha256", "audit_policy_fingerprint", "artifacts", "visual_evidence_inventory"}
    if set(bindings) != required:
        raise ValueError("receipt binding field set drift")
    if bindings["review_manifest_sha256"] != "sha256:" + sha256(root / "review_manifest.json"):
        raise ValueError("review manifest binding drift")
    if bindings["package_audit_sha256"] != "sha256:" + sha256(root / "package_audit.json"):
        raise ValueError("package audit binding drift")
    package_audit = _read_json(root / "package_audit.json")
    if (
        package_audit.get("passed") is not True
        or package_audit.get("blocking_issue_count") != 0
        or bindings["audit_policy_fingerprint"] != package_audit.get("policy_fingerprint")
    ):
        raise ValueError("fresh package audit policy/result drift")
    artifacts = bindings["artifacts"]
    if not isinstance(artifacts, Mapping) or set(artifacts) != set(NAMES):
        raise ValueError("artifact binding set drift")
    for key, name in NAMES.items():
        path = root / name
        _regular(path)
        if artifacts[key] != {"path": name, "sha256": "sha256:" + sha256(path)}:
            raise ValueError(f"artifact binding drift: {key}")
    manifest = _read_json(root / "review_manifest.json")
    inventory = bindings["visual_evidence_inventory"]
    if inventory != manifest.get("visual_evidence_inventory") or inventory != visual_inventory(root):
        raise ValueError("visual evidence inventory drift")


def validate_ready_proposal(root: Path, proposal: Mapping[str, Any]) -> None:
    required = {"schema_version", "candidate_id", "title", "accepted", "upload_allowed", "status", "bindings", "required_root_checks", "root_content_check", "reviewer", "reviewed_at"}
    if set(proposal) != required:
        raise ValueError("proposal field set drift")
    if proposal["schema_version"] != PROPOSAL_SCHEMA or proposal["candidate_id"] != CID or proposal["title"] != TITLE:
        raise ValueError("proposal C2 identity/title drift")
    if proposal["accepted"] is not False or proposal["upload_allowed"] is not False or proposal["status"] != READY:
        raise ValueError("proposal is not an unaccepted READY C2 proposal")
    if proposal["reviewer"] != "Codex root" or proposal["reviewed_at"] is not None:
        raise ValueError("proposal reviewer fields drift")
    if proposal["required_root_checks"] != ROOT_CHECKS or not isinstance(proposal["root_content_check"], str) or "fresh deployed auditor has been reclosed" not in proposal["root_content_check"]:
        raise ValueError("proposal root technical check drift")
    _validate_bindings(root, proposal["bindings"])


def _validate_timestamp(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("reviewed_at must be an ISO-8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("reviewed_at is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("reviewed_at must include timezone")
    return value


def make_accepted_receipt(root: Path, proposal_path: Path, reviewed_at: str, decision_basis: str) -> dict[str, Any]:
    _regular(proposal_path)
    proposal = _read_json(proposal_path)
    validate_ready_proposal(root, proposal)
    if not isinstance(decision_basis, str) or not decision_basis.strip():
        raise ValueError("decision basis is required")
    return {
        "schema_version": ACCEPTED_SCHEMA,
        "candidate_id": CID,
        "title": TITLE,
        "accepted": True,
        "upload_allowed": False,
        "scope": SCOPE,
        "reviewer_kind": "delegated_root_agent",
        "reviewed_by": "Codex root",
        "reviewed_at": _validate_timestamp(reviewed_at),
        "decision_basis": decision_basis,
        "proposal": {"path": proposal_path.name, "bytes": proposal_path.stat().st_size, "sha256": "sha256:" + sha256(proposal_path)},
        "bindings": proposal["bindings"],
        "required_root_checks": ROOT_CHECKS,
        "non_authorizations": [
            "does not replace Ivan line947 authorization",
            "does not replace CPA title-cover QC",
            "does not authorize AUTO_UPLOAD or any upload",
        ],
    }


def validate_accepted_receipt(root: Path, proposal_path: Path, receipt: Mapping[str, Any]) -> None:
    required = {"schema_version", "candidate_id", "title", "accepted", "upload_allowed", "scope", "reviewer_kind", "reviewed_by", "reviewed_at", "decision_basis", "proposal", "bindings", "required_root_checks", "non_authorizations"}
    if set(receipt) != required:
        raise ValueError("accepted receipt field set drift")
    if receipt["schema_version"] != ACCEPTED_SCHEMA or receipt["candidate_id"] != CID or receipt["title"] != TITLE:
        raise ValueError("accepted receipt C2 identity/title drift")
    if receipt["accepted"] is not True or receipt["upload_allowed"] is not False or receipt["scope"] != SCOPE:
        raise ValueError("accepted receipt acceptance/scope drift")
    if receipt["reviewer_kind"] != "delegated_root_agent" or receipt["reviewed_by"] != "Codex root":
        raise ValueError("accepted receipt reviewer drift")
    _validate_timestamp(receipt["reviewed_at"])
    if not isinstance(receipt["decision_basis"], str) or not receipt["decision_basis"].strip():
        raise ValueError("accepted receipt decision basis drift")
    _regular(proposal_path)
    proposal = _read_json(proposal_path)
    validate_ready_proposal(root, proposal)
    expected_proposal = {"path": proposal_path.name, "bytes": proposal_path.stat().st_size, "sha256": "sha256:" + sha256(proposal_path)}
    if receipt["proposal"] != expected_proposal or receipt["bindings"] != proposal["bindings"]:
        raise ValueError("accepted receipt proposal/binding drift")
    if receipt["required_root_checks"] != ROOT_CHECKS:
        raise ValueError("accepted receipt root check drift")
    if receipt["non_authorizations"] != ["does not replace Ivan line947 authorization", "does not replace CPA title-cover QC", "does not authorize AUTO_UPLOAD or any upload"]:
        raise ValueError("accepted receipt non-authorization drift")
