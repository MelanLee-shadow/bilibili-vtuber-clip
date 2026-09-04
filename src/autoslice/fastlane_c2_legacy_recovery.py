"""C2-only execution-contract recovery, never a producer StoryContract."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

from .fastlane_c2_formal_adapter import CID, NAMES, TITLE, sha256
from .fastlane_c2_release_bridge import _validate_authorization
from .fastlane_c2_technical_receipt import validate_accepted_receipt
from .package_audit_binding import audit_content_binding

SCHEMA = "fastlane-c2-legacy-recovery-contract-proposal.v1"
ACCEPTED_SCHEMA = "fastlane-c2-legacy-recovery-execution-contract.v1"
SCOPE = "C2_FASTLANE_LEGACY_EXECUTION_CONTRACT_NOT_PRODUCER_STORY_CONTRACT"
DATE = "2026-08-13"


def _entry(root: Path, name: str) -> dict[str, object]:
    path = root / name
    if not path.is_file() or path.is_symlink():
        raise ValueError(f"unsafe recovery input: {name}")
    return {"path": name, "bytes": path.stat().st_size, "sha256": "sha256:" + sha256(path)}


def _validate_current_formal_audit_replay(
    *, formal: Path, saved: Mapping[str, Any], current: Mapping[str, Any]
) -> None:
    """Allow only C2 formal-audit relocation and auditor-identity churn.

    ``make_proposal`` is reached only after its exact C2 candidate/date/title
    checks.  The accepted contract still seals the saved audit file by hash;
    this replay merely permits its absolute root and deployed auditor identity
    to differ.  Schema, epoch, inputs, issues, counts, and verdict stay exact.
    """

    current_cmp = audit_content_binding(dict(current))
    saved_cmp = audit_content_binding(dict(saved))
    current_cmp.pop("root", None)
    saved_cmp.pop("root", None)
    saved_root = saved.get("root")
    if (
        current_cmp != saved_cmp
        or not isinstance(saved_root, str)
        or not Path(saved_root).is_absolute()
        or current.get("root") != str(formal.absolute())
        or saved.get("passed") is not True
        or saved.get("issues") != []
        or saved.get("issue_count") != 0
        or saved.get("blocking_issue_count") != 0
        or current.get("passed") is not True
        or current.get("issues") != []
        or current.get("issue_count") != 0
        or current.get("blocking_issue_count") != 0
    ):
        raise ValueError("C2 formal audit replay drift")


def make_proposal(formal: Path, authorization: Path, receipt: Path) -> dict[str, Any]:
    record = json.loads((formal / "c2.formal.record.v1.json").read_text(encoding="utf-8"))
    auth = json.loads(authorization.read_text(encoding="utf-8"))
    _validate_authorization(auth)
    if record.get("candidate_id") != CID or record.get("title") != TITLE:
        raise ValueError("C2 formal identity drift")
    from scripts.audit_review_package import audit_package
    saved = json.loads((formal / "package_audit.json").read_text(encoding="utf-8"))
    current = audit_package(formal)
    _validate_current_formal_audit_replay(formal=formal, saved=saved, current=current)
    receipt_data = json.loads(receipt.read_text(encoding="utf-8"))
    proposal_binding = receipt_data.get("proposal")
    if not isinstance(proposal_binding, Mapping) or not isinstance(proposal_binding.get("path"), str):
        raise ValueError("C2 technical receipt proposal binding absent")
    validate_accepted_receipt(formal, formal / proposal_binding["path"], receipt_data, allow_audit_root_relocation=True)
    cue_graph = json.loads((formal / "cue-graph.v1.json").read_text(encoding="utf-8"))
    rows = cue_graph.get("rows")
    if not isinstance(rows, list) or len(rows) < 21 or rows[4] != {"cue": 5, "time": "00:00:08,720 --> 00:00:11,240", "before": "是刚吗？小豆老公不是你老公", "after": "小豆老公；； 不是你老公", "disposition": "OPERATOR_REPAIR"} or rows[20].get("after") != "小豆哪有好吵":
        raise ValueError("C2 cue5/cue21 freeze drift")
    inputs = {key: _entry(formal, name) for key, name in NAMES.items()}
    for key, name in {"formal_record": "c2.formal.record.v1.json", "formal_publish": "c2.formal.publish.v1.json", "review_manifest": "review_manifest.json", "package_audit": "package_audit.json", "cue_graph": "cue-graph.v1.json", "cover_reprojection": "cover-reprojection.v1.json"}.items():
        inputs[key] = _entry(formal, name)
    proposal = {
        "schema_version": SCHEMA, "candidate_id": CID, "recording_date": DATE,
        "title": TITLE, "accepted": False, "upload_allowed": False, "scope": SCOPE,
        "technical_recovery_projection_not_historical_producer_story_contract": True,
        "formal_inputs": inputs,
        "authorization": {"path": authorization.name, "bytes": authorization.stat().st_size, "sha256": "sha256:" + sha256(authorization), "exact_line_numbers": [947, 1643, 1745]},
        "technical_receipt": {"path": receipt.name, "bytes": receipt.stat().st_size, "sha256": "sha256:" + sha256(receipt)},
        "frozen_claims": {
            "boundary_duration": "final burned video and final SRT are frozen by formal record/review/audit",
            "named_error_closure": "cue5 is exactly 小豆老公；； 不是你老公; cue21 小豆哪有好吵 is the frozen response context to 弹幕 小豆好吵（",
            "unnamed_content": "relative to accepted formal-v4, subtitle/media/title/cover/tags are not changed and every subtitle cue except cue5 is frozen",
            "title": "exact final title only", "cover": "only final cover and rendered text-pixel evidence claims",
        },
        "generic_historical_story_contract": "unavailable_not_reconstructable",
        "unavailable_or_not_applicable": {
            "clip_context": "historical_artifact_unavailable", "source_fact_review": "historical_artifact_unavailable",
            "selection_scorecard": "historical_artifact_unavailable", "candidate_calibration": "not_applicable_under_exhaustive_fastlane_authorization",
        },
    }
    raw = json.dumps(proposal, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    proposal["self_seal"] = {"canonical_json_without_self_seal_sha256": "sha256:" + hashlib.sha256(raw).hexdigest()}
    return proposal


def validate_proposal(value: Mapping[str, Any], *, formal: Path, authorization: Path, receipt: Path) -> None:
    required = {"schema_version", "candidate_id", "recording_date", "title", "accepted", "upload_allowed", "scope", "technical_recovery_projection_not_historical_producer_story_contract", "formal_inputs", "authorization", "technical_receipt", "frozen_claims", "generic_historical_story_contract", "unavailable_or_not_applicable", "self_seal"}
    if set(value) != required or value.get("schema_version") != SCHEMA or value.get("candidate_id") != CID or value.get("recording_date") != DATE or value.get("title") != TITLE or value.get("accepted") is not False or value.get("upload_allowed") is not False or value.get("scope") != SCOPE or value.get("technical_recovery_projection_not_historical_producer_story_contract") is not True:
        raise ValueError("C2 legacy recovery proposal identity/scope drift")
    unsigned = dict(value)
    seal = unsigned.pop("self_seal")
    raw = json.dumps(unsigned, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    if seal != {"canonical_json_without_self_seal_sha256": "sha256:" + hashlib.sha256(raw).hexdigest()}:
        raise ValueError("C2 legacy recovery proposal self-seal drift")
    if dict(value) != make_proposal(formal, authorization, receipt):
        raise ValueError("C2 legacy recovery proposal source binding drift")


def make_accepted_execution_contract(*, proposal: Path, reviewed_at: str, decision_basis: str) -> dict[str, Any]:
    value: dict[str, Any] = {
        "schema_version": ACCEPTED_SCHEMA, "candidate_id": CID, "accepted": True, "upload_allowed": False,
        "reviewer_kind": "delegated_root_agent", "reviewed_by": "Codex root", "reviewed_at": reviewed_at,
        "decision_basis": decision_basis, "proposal": {"path": proposal.name, "bytes": proposal.stat().st_size, "sha256": "sha256:" + sha256(proposal)},
    }
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    value["self_seal"] = {"canonical_json_without_self_seal_sha256": "sha256:" + hashlib.sha256(raw).hexdigest()}
    return value


def validate_accepted_execution_contract(value: Mapping[str, Any], *, proposal: Path, expected_reviewed_at: str | None = None, expected_decision_basis: str | None = None) -> None:
    """Acceptance envelope only; it cannot recreate a producer StoryContract."""
    required = {"schema_version", "candidate_id", "accepted", "upload_allowed", "reviewer_kind", "reviewed_by", "reviewed_at", "decision_basis", "proposal", "self_seal"}
    if set(value) != required or value.get("schema_version") != ACCEPTED_SCHEMA or value.get("candidate_id") != CID or value.get("accepted") is not True or value.get("upload_allowed") is not False or value.get("reviewer_kind") != "delegated_root_agent" or value.get("reviewed_by") != "Codex root" or not isinstance(value.get("reviewed_at"), str) or not value["reviewed_at"].strip() or not isinstance(value.get("decision_basis"), str) or not value["decision_basis"].strip():
        raise ValueError("C2 legacy execution acceptance drift")
    try:
        parsed = datetime.fromisoformat(value["reviewed_at"].replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("C2 legacy execution timestamp invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("C2 legacy execution timestamp lacks timezone")
    if expected_reviewed_at is not None and value["reviewed_at"] != expected_reviewed_at:
        raise ValueError("C2 legacy execution timestamp authority drift")
    if expected_decision_basis is not None and value["decision_basis"] != expected_decision_basis:
        raise ValueError("C2 legacy execution decision authority drift")
    expected = {"path": proposal.name, "bytes": proposal.stat().st_size, "sha256": "sha256:" + sha256(proposal)}
    if value.get("proposal") != expected:
        raise ValueError("C2 legacy execution proposal binding drift")
    unsigned = dict(value)
    seal = unsigned.pop("self_seal")
    raw = json.dumps(unsigned, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    if seal != {"canonical_json_without_self_seal_sha256": "sha256:" + hashlib.sha256(raw).hexdigest()}:
        raise ValueError("C2 legacy execution self-seal drift")
