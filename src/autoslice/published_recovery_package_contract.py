"""Static package-audit contract for published same-BV preflight evidence."""

from __future__ import annotations

import hashlib
import json
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

REGISTRY_REPO_PATH = (
    "assets/lidousha/"
    "recovery_publication_authority_2026-08-25_c4_c5_timeaxis.v1.json"
)
REGISTRY_SHA256 = (
    "sha256:a438157b6cbbe4b17005fe46a30db9dc0882eb0419c280bb7e3f1e4bfdfeaaf7"
)
STATE_AUTHORITY_REPO_PATH = (
    "assets/lidousha/"
    "published_recovery_state_authority_2026-08-25_c4_c5.v1.json"
)
STATE_AUTHORITY_SHA256 = (
    "sha256:fdba4d77646acad918299be4a6e345dca046871d3261856f81c1e4ae6932bef1"
)
_SHA256 = re.compile(r"sha256:[0-9a-f]{64}\Z")
_DATE = re.compile(r"20[0-9]{2}-[0-9]{2}-[0-9]{2}\Z")
_CANDIDATE = re.compile(r"[A-Za-z0-9_-]{1,96}\Z")
_SUFFIX = ".published-recovery-preflight.json"
_FIELDS = {
    "schema_version", "candidate_id", "date", "state_transition",
    "upload_allowed", "publication_allowed", "same_bv_only", "target",
    "production_state", "deployment_authority", "published_state_authority",
    "publication_authority_sha256", "source_record_sha256",
}


def _sha_bytes(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _regular_bytes(path: Path) -> bytes:
    metadata = path.lstat()
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise ValueError("PUBLISHED_RECOVERY_STATE_AUTHORITY_UNSAFE")
    return path.read_bytes()


def load_published_recovery_state_authority(
    *, repo_root: Path, candidate_id: str,
) -> dict[str, object]:
    path = repo_root / STATE_AUTHORITY_REPO_PATH
    raw = _regular_bytes(path)
    if _sha_bytes(raw) != STATE_AUTHORITY_SHA256:
        raise ValueError("PUBLISHED_RECOVERY_STATE_AUTHORITY_HASH_DRIFT")
    document = json.loads(raw.decode("utf-8"))
    if (
        not isinstance(document, dict)
        or set(document) != {"schema_version", "authority", "entries"}
        or document.get("schema_version")
        != "published-recovery-state-authority.v1"
        or not isinstance(document.get("entries"), list)
    ):
        raise ValueError("PUBLISHED_RECOVERY_STATE_AUTHORITY_INVALID")
    entries = document["entries"]
    by_id = {
        entry.get("candidate_id"): entry
        for entry in entries
        if isinstance(entry, dict)
    }
    if (
        set(by_id) != {"auto_113028_1271_1328", "auto_113028_1602_1698"}
        or len(entries) != len(by_id)
        or candidate_id not in by_id
    ):
        raise ValueError("PUBLISHED_RECOVERY_STATE_AUTHORITY_SCOPE_DRIFT")
    entry = by_id[candidate_id]
    fields = {
        "candidate_id", "status", "bvid", "aid", "published_cid",
        "reconciliation_status", "reconciliation_cid", "title",
        "publication_authority_cid", "predecessor_completed",
    }
    if (
        set(entry) != fields
        or entry.get("status") != "published"
        or not isinstance(entry.get("aid"), int)
        or not isinstance(entry.get("published_cid"), int)
        or not isinstance(entry.get("reconciliation_cid"), int)
        or not isinstance(entry.get("publication_authority_cid"), int)
        or not str(entry.get("bvid") or "")
        or not str(entry.get("title") or "")
    ):
        raise ValueError("PUBLISHED_RECOVERY_STATE_AUTHORITY_ENTRY_INVALID")
    predecessor = entry.get("predecessor_completed")
    if predecessor is None:
        if entry["publication_authority_cid"] != entry["published_cid"]:
            raise ValueError("PUBLISHED_RECOVERY_STATE_AUTHORITY_CID_DRIFT")
    else:
        if not isinstance(predecessor, dict) or set(predecessor) != {
            "repo_path", "sha256", "schema_version", "status", "bvid",
            "aid", "new_cid",
        }:
            raise ValueError("PUBLISHED_RECOVERY_PREDECESSOR_AUTHORITY_INVALID")
        relative = Path(str(predecessor["repo_path"]))
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("PUBLISHED_RECOVERY_PREDECESSOR_AUTHORITY_UNSAFE")
        receipt_path = repo_root / relative
        receipt_raw = _regular_bytes(receipt_path)
        if _sha_bytes(receipt_raw) != predecessor["sha256"]:
            raise ValueError("PUBLISHED_RECOVERY_PREDECESSOR_AUTHORITY_DRIFT")
        receipt = json.loads(receipt_raw.decode("utf-8"))
        if (
            not isinstance(receipt, dict)
            or receipt.get("schema_version") != predecessor["schema_version"]
            or receipt.get("status") != predecessor["status"]
            or receipt.get("bvid") != predecessor["bvid"]
            or predecessor["bvid"] != entry["bvid"]
            or receipt.get("aid") != predecessor["aid"]
            or predecessor["aid"] != entry["aid"]
            or receipt.get("new_cid") != predecessor["new_cid"]
            or predecessor["new_cid"] != entry["published_cid"]
        ):
            raise ValueError("PUBLISHED_RECOVERY_PREDECESSOR_AUTHORITY_DRIFT")
    canonical = json.dumps(
        entry, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return {
        **entry,
        "registry_sha256": STATE_AUTHORITY_SHA256,
        "entry_sha256": _sha_bytes(canonical),
    }


@dataclass(frozen=True, slots=True)
class PublishedRecoveryPreflightIssue:
    code: str
    path: Path
    detail: str


def _issue(code: str, path: Path, detail: str) -> PublishedRecoveryPreflightIssue:
    return PublishedRecoveryPreflightIssue(code, path, detail)


def audit_published_recovery_preflight(
    root: Path,
    recovery_publication_authorities: Mapping[str, Mapping[str, object]],
) -> list[PublishedRecoveryPreflightIssue]:
    """Reject ambiguous or semantically unbound package-only preflight files."""

    paths = sorted(root.glob(f"*{_SUFFIX}"))
    if not paths:
        return []
    if len(paths) != 1:
        return [
            _issue(
                "PUBLISHED_RECOVERY_PREFLIGHT_AMBIGUOUS",
                root,
                f"count={len(paths)}",
            )
        ]
    path = paths[0]
    try:
        metadata = path.lstat()
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return [
            _issue(
                "PUBLISHED_RECOVERY_PREFLIGHT_INVALID",
                path,
                type(exc).__name__,
            )
        ]
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISREG(metadata.st_mode)
        or not isinstance(payload, dict)
        or set(payload) != _FIELDS
    ):
        return [
            _issue(
                "PUBLISHED_RECOVERY_PREFLIGHT_INVALID",
                path,
                "file type, JSON root, or exact field set is invalid",
            )
        ]
    candidate_id = str(payload.get("candidate_id") or "")
    date = str(payload.get("date") or "")
    authority = recovery_publication_authorities.get(candidate_id)
    if (
        _CANDIDATE.fullmatch(candidate_id) is None
        or path.name != candidate_id + _SUFFIX
        or _DATE.fullmatch(date) is None
        or not isinstance(authority, Mapping)
        or payload.get("schema_version")
        != "published-same-bv-recovery-preflight.v1"
        or payload.get("state_transition") != "none"
        or payload.get("upload_allowed") is not False
        or payload.get("publication_allowed") is not False
        or payload.get("same_bv_only") is not True
    ):
        return [
            _issue(
                "PUBLISHED_RECOVERY_PREFLIGHT_CONTRACT_MISMATCH",
                path,
                "identity, authority, or no-upload contract mismatch",
            )
        ]
    target = payload.get("target")
    state = payload.get("production_state")
    deployment = payload.get("deployment_authority")
    state_authority = payload.get("published_state_authority")
    expected_title = str(
        authority.get("observed_public_title") or authority.get("title") or ""
    )
    if (
        not isinstance(target, dict)
        or set(target) != {"bvid", "aid", "current_state_cid", "title"}
        or target.get("bvid") != authority.get("bvid")
        or target.get("aid") != authority.get("aid")
        or target.get("title") != expected_title
        or isinstance(target.get("current_state_cid"), bool)
        or not isinstance(target.get("current_state_cid"), int)
        or target["current_state_cid"] <= 0
        or not isinstance(state_authority, dict)
        or state_authority.get("candidate_id") != candidate_id
        or state_authority.get("bvid") != target.get("bvid")
        or state_authority.get("aid") != target.get("aid")
        or state_authority.get("published_cid")
        != target.get("current_state_cid")
        or state_authority.get("reconciliation_cid")
        != target.get("current_state_cid")
        or state_authority.get("title") != target.get("title")
        or state_authority.get("publication_authority_cid")
        != authority.get("cid")
        or state_authority.get("registry_sha256") != STATE_AUTHORITY_SHA256
        or _SHA256.fullmatch(str(state_authority.get("entry_sha256") or ""))
        is None
    ):
        return [
            _issue(
                "PUBLISHED_RECOVERY_PREFLIGHT_TARGET_MISMATCH",
                path,
                "target does not equal package recovery authority",
            )
        ]
    state_path = str(state.get("path") or "") if isinstance(state, dict) else ""
    if (
        not isinstance(state, dict)
        or set(state) != {"path", "sha256"}
        or not Path(state_path).is_absolute()
        or not state_path.endswith(f"/state/{date}.json")
        or _SHA256.fullmatch(str(state.get("sha256") or "")) is None
        or not isinstance(deployment, dict)
        or not deployment
        or any(not isinstance(key, str) or not isinstance(value, str)
               for key, value in deployment.items())
        or payload.get("publication_authority_sha256")
        != authority.get("authority_sha256")
        or _SHA256.fullmatch(str(payload.get("source_record_sha256") or ""))
        is None
    ):
        return [
            _issue(
                "PUBLISHED_RECOVERY_PREFLIGHT_BINDING_INVALID",
                path,
                "state, deployment, publication, or source binding is invalid",
            )
        ]
    return []


def _file_entry(path: Path) -> dict[str, object]:
    metadata = path.lstat()
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise ValueError("PUBLISHED_RECOVERY_PACKAGE_ARTIFACT_UNSAFE")
    data = path.read_bytes()
    return {
        "path": path.name,
        "sha256": "sha256:" + hashlib.sha256(data).hexdigest(),
        "bytes": len(data),
    }


def build_published_recovery_package_receipt(
    *,
    package_root: Path,
    candidate_id: str,
    recovery_publication_authority: Mapping[str, object],
) -> dict[str, object]:
    preflight = package_root / f"{candidate_id}{_SUFFIX}"
    issues = audit_published_recovery_preflight(
        package_root, {candidate_id: recovery_publication_authority}
    )
    if issues:
        raise ValueError(issues[0].code)
    payload = json.loads(preflight.read_text(encoding="utf-8"))
    return {
        "schema_version": "published-same-bv-recovery-package-receipt.v1",
        "candidate_id": candidate_id,
        "date": payload["date"],
        "state_transition": "none",
        "upload_allowed": False,
        "publication_allowed": False,
        "same_bv_only": True,
        "recovery_publication_authority": dict(recovery_publication_authority),
        "preflight": _file_entry(preflight),
        "artifacts": {
            role: _file_entry(package_root / f"{candidate_id}{suffix}")
            for role, suffix in (
                ("video", ".mp4"),
                ("subtitle", ".srt"),
                ("cover", ".cover.png"),
            )
        },
    }


def validate_published_recovery_package_receipt(
    value: object,
    *,
    package_root: Path,
    candidate_id: str,
    recovery_publication_authority: Mapping[str, object],
) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError("PUBLISHED_RECOVERY_PACKAGE_RECEIPT_INVALID")
    expected = build_published_recovery_package_receipt(
        package_root=package_root,
        candidate_id=candidate_id,
        recovery_publication_authority=recovery_publication_authority,
    )
    if value != expected:
        raise ValueError("PUBLISHED_RECOVERY_PACKAGE_RECEIPT_DRIFT")
    return dict(value)


def audit_published_recovery_manifest_binding(
    root: Path,
    recovery_publication_authorities: Mapping[str, Mapping[str, object]],
    items: object,
) -> list[PublishedRecoveryPreflightIssue]:
    issues = audit_published_recovery_preflight(
        root, recovery_publication_authorities
    )
    preflights = sorted(root.glob(f"*{_SUFFIX}"))
    receipts = sorted(root.glob("*.published-recovery-package-receipt.json"))
    if not preflights and not receipts:
        return issues
    if issues:
        return issues
    if len(preflights) != 1 or len(receipts) != 1 or not isinstance(items, list):
        return [
            _issue(
                "PUBLISHED_RECOVERY_PACKAGE_RECEIPT_AMBIGUOUS",
                root,
                f"preflights={len(preflights)} receipts={len(receipts)}",
            )
        ]
    candidate_id = preflights[0].name.removesuffix(_SUFFIX)
    authority = recovery_publication_authorities.get(candidate_id)
    matching = [
        item for item in items
        if isinstance(item, dict) and item.get("candidate_id") == candidate_id
    ]
    if not isinstance(authority, Mapping) or len(matching) != 1:
        return [
            _issue(
                "PUBLISHED_RECOVERY_PACKAGE_RECEIPT_BINDING_INVALID",
                receipts[0],
                "candidate item or authority is missing",
            )
        ]
    item = matching[0]
    try:
        raw = receipts[0].read_bytes()
        value = json.loads(raw.decode("utf-8"))
        validate_published_recovery_package_receipt(
            value,
            package_root=root,
            candidate_id=candidate_id,
            recovery_publication_authority=authority,
        )
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        return [
            _issue(
                "PUBLISHED_RECOVERY_PACKAGE_RECEIPT_INVALID",
                receipts[0],
                type(exc).__name__,
            )
        ]
    digest = "sha256:" + hashlib.sha256(raw).hexdigest()
    if (
        receipts[0].name
        != item.get("published_recovery_package_receipt")
        or digest != item.get("published_recovery_package_receipt_sha256")
    ):
        return [
            _issue(
                "PUBLISHED_RECOVERY_PACKAGE_RECEIPT_BINDING_INVALID",
                receipts[0],
                "manifest item receipt path/hash drifts",
            )
        ]
    return []


def validate_materialized_published_recovery_package(
    package_root: Path,
    *,
    live_authority_recheck: bool = True,
) -> dict[str, object]:
    """Replay the outer VERIFIED receipt and current production authority."""

    package_root = package_root.resolve(strict=True)
    preflights = sorted(package_root.glob(f"*{_SUFFIX}"))
    receipts = sorted(package_root.glob("*.published-recovery-package-receipt.json"))
    if len(preflights) != 1 or len(receipts) != 1:
        raise ValueError("PUBLISHED_RECOVERY_MATERIALIZED_EVIDENCE_AMBIGUOUS")
    candidate_id = preflights[0].name.removesuffix(_SUFFIX)
    preflight = json.loads(preflights[0].read_text(encoding="utf-8"))
    package_receipt = json.loads(receipts[0].read_text(encoding="utf-8"))
    if not isinstance(preflight, dict) or not isinstance(package_receipt, dict):
        raise ValueError("PUBLISHED_RECOVERY_MATERIALIZED_EVIDENCE_INVALID")
    authority = package_receipt.get("recovery_publication_authority")
    if not isinstance(authority, dict):
        raise ValueError("PUBLISHED_RECOVERY_MATERIALIZED_AUTHORITY_INVALID")
    review = json.loads(
        (package_root / "review_manifest.json").read_text(encoding="utf-8")
    )
    if not isinstance(review, dict):
        raise ValueError("PUBLISHED_RECOVERY_MATERIALIZED_REVIEW_INVALID")
    issues = audit_published_recovery_manifest_binding(
        package_root, {candidate_id: authority}, review.get("items")
    )
    if issues:
        raise ValueError(issues[0].code)
    state = preflight["production_state"]
    if not isinstance(state, dict):
        raise ValueError("PUBLISHED_RECOVERY_MATERIALIZED_STATE_INVALID")
    state_path = Path(str(state["path"]))
    state_bytes = _regular_bytes(state_path)
    if "sha256:" + hashlib.sha256(state_bytes).hexdigest() != state["sha256"]:
        raise ValueError("PUBLISHED_RECOVERY_MATERIALIZED_STATE_DRIFT")
    state_doc = json.loads(state_bytes.decode("utf-8"))
    if not isinstance(state_doc, dict):
        raise ValueError("PUBLISHED_RECOVERY_MATERIALIZED_STATE_INVALID")
    rows = [
        row for row in state_doc.get("picks") or []
        if isinstance(row, dict) and row.get("candidate_id") == candidate_id
    ]
    target = preflight["target"]
    state_authority = preflight.get("published_state_authority")
    if not isinstance(target, dict) or not isinstance(state_authority, dict):
        raise ValueError("PUBLISHED_RECOVERY_MATERIALIZED_TARGET_INVALID")
    if len(rows) != 1 or rows[0].get("status") != state_authority.get("status"):
        raise ValueError("PUBLISHED_RECOVERY_MATERIALIZED_STATE_ROW_DRIFT")
    row = rows[0]
    reconciliation = row.get("publication_reconciliation")
    if (
        row.get("bvid") != target["bvid"]
        or row.get("aid") != state_authority.get("aid")
        or row.get("published_cid") != state_authority.get("published_cid")
        or not isinstance(reconciliation, dict)
        or reconciliation.get("status")
        != state_authority.get("reconciliation_status")
        or reconciliation.get("recording_date") != preflight["date"]
        or reconciliation.get("candidate_id") != candidate_id
        or reconciliation.get("bvid") != target["bvid"]
        or reconciliation.get("aid") != target["aid"]
        or reconciliation.get("cid") != target["current_state_cid"]
        or reconciliation.get("title") != target["title"]
    ):
        raise ValueError("PUBLISHED_RECOVERY_MATERIALIZED_STATE_TUPLE_DRIFT")
    runtime = state_path.parent.parent
    if live_authority_recheck:
        from src.autoslice.producer_delivery_transaction import (
            deployment_authority_binding,
        )
        from src.autoslice.recovery_title_authority import (
            build_recovery_publication_authorities,
        )

        deployed = dict(deployment_authority_binding(runtime))
        if deployed != preflight["deployment_authority"]:
            raise ValueError("PUBLISHED_RECOVERY_MATERIALIZED_DEPLOYMENT_DRIFT")
        current = build_recovery_publication_authorities(
            candidate_ids={candidate_id},
            registry_path=runtime / "repo" / REGISTRY_REPO_PATH,
            expected_registry_sha256=REGISTRY_SHA256,
            repo_root=runtime / "repo",
        )[candidate_id]
        if current != authority:
            raise ValueError("PUBLISHED_RECOVERY_MATERIALIZED_AUTHORITY_DRIFT")
        current_state_authority = load_published_recovery_state_authority(
            repo_root=runtime / "repo", candidate_id=candidate_id
        )
        if current_state_authority != state_authority:
            raise ValueError(
                "PUBLISHED_RECOVERY_MATERIALIZED_STATE_AUTHORITY_DRIFT"
            )
        source_record = (
            runtime / "out" / preflight["date"] / candidate_id
            / "replacement_recuts" / f"{candidate_id}.record.json"
        )
        source_sha = _sha_bytes(_regular_bytes(source_record))
        if source_sha != preflight["source_record_sha256"]:
            raise ValueError("PUBLISHED_RECOVERY_MATERIALIZED_SOURCE_DRIFT")
    outer_path = package_root.parent / "published-recovery-package.json"
    outer = json.loads(outer_path.read_text(encoding="utf-8"))
    if not isinstance(outer, dict):
        raise ValueError("PUBLISHED_RECOVERY_MATERIALIZED_OUTER_RECEIPT_INVALID")
    artifact_names = {
        "video": f"{candidate_id}.mp4",
        "subtitle": f"{candidate_id}.srt",
        "package_audit": "package-audit.json",
        "review_manifest": "review_manifest.json",
        "recovery_preflight": preflights[0].name,
        "package_receipt": receipts[0].name,
    }
    expected_artifacts = {
        role: {"sha256": entry["sha256"], "bytes": entry["bytes"]}
        for role, name in artifact_names.items()
        for entry in (_file_entry(package_root / name),)
    }
    expected_outer = {
        "schema_version": "published-same-bv-recovery-package.v1",
        "status": "VERIFIED_PRIVATE_PACKAGE",
        "candidate_id": candidate_id,
        "date": preflight["date"],
        "state_transition": "none",
        "upload_allowed": False,
        "publication_allowed": False,
        "same_bv_only": True,
        "target": target,
        "production_state": state,
        "deployment_authority": preflight["deployment_authority"],
        "published_state_authority": state_authority,
        "publication_authority_sha256": preflight["publication_authority_sha256"],
        "source_record_sha256": preflight["source_record_sha256"],
        "package_root": str(package_root),
        "artifacts": expected_artifacts,
    }
    if outer != expected_outer:
        raise ValueError("PUBLISHED_RECOVERY_MATERIALIZED_OUTER_RECEIPT_DRIFT")
    entry = _file_entry(outer_path)
    entry["path"] = str(outer_path.resolve())
    return entry
