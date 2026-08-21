"""Candidate-scoped read-only authority for one interrupted cover repair.

This is intentionally not a general transaction migration.  It proves the
specific two-layer state left by the 2026-08-11 manual-title cover repair
before a newer code fingerprint may resume the existing recovery path.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Mapping

from src.autoslice.repository_asset_authority import (
    RepositoryAssetAuthorityError,
    require_repository_asset_authority,
)


ROOT = Path(__file__).resolve().parents[2]
SCHEMA_VERSION = "reviewed-cover-committed-recovery-authority.v1"
AUTHORITY_PATH = Path(
    "assets/lidousha/reviewed_cover_committed_recovery_authorities/"
    "auto_173005_934_1166.v1.json"
)
_SHA = re.compile(r"sha256:[0-9a-f]{64}\Z")


class ReviewedCoverCommittedRecoveryAuthorityError(ValueError):
    """The uniquely-sealed committed recovery image is not present."""


def _json_sha256(value: object) -> str:
    return "sha256:" + hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _bytes_sha256(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _read_bytes(path: Path, *, expected: str, label: str) -> bytes:
    if path.is_symlink() or not path.is_file() or _SHA.fullmatch(expected) is None:
        raise ReviewedCoverCommittedRecoveryAuthorityError(f"{label} is unavailable")
    try:
        value = path.read_bytes()
    except OSError as exc:
        raise ReviewedCoverCommittedRecoveryAuthorityError(f"{label} is unreadable") from exc
    if _bytes_sha256(value) != expected:
        raise ReviewedCoverCommittedRecoveryAuthorityError(f"{label} hash drifted")
    return value


def _read_json(path: Path, *, expected: str, label: str) -> dict[str, Any]:
    try:
        value = json.loads(_read_bytes(path, expected=expected, label=label))
    except json.JSONDecodeError as exc:
        raise ReviewedCoverCommittedRecoveryAuthorityError(f"{label} is not JSON") from exc
    if not isinstance(value, dict):
        raise ReviewedCoverCommittedRecoveryAuthorityError(f"{label} is not an object")
    return value


def load_committed_recovery_authority(
    *, repo_root: Path = ROOT
) -> dict[str, Any]:
    """Load the one sealed authority document with an exact closed schema."""

    path = repo_root / AUTHORITY_PATH
    try:
        raw = path.read_bytes()
        require_repository_asset_authority(
            repo_root=repo_root, relative_path=AUTHORITY_PATH, observed_bytes=raw
        )
        document = json.loads(raw)
    except (OSError, UnicodeError, json.JSONDecodeError, RepositoryAssetAuthorityError) as exc:
        raise ReviewedCoverCommittedRecoveryAuthorityError(
            "committed recovery authority is unavailable"
        ) from exc
    expected = {
        "schema_version", "candidate", "plan", "invalidation_journal",
        "state_preimage", "successor", "successor_code_fingerprint",
        "active_documents", "immutable", "authority_sha256",
    }
    if not isinstance(document, dict) or set(document) != expected:
        raise ReviewedCoverCommittedRecoveryAuthorityError("committed recovery authority schema drifted")
    declared = document.pop("authority_sha256")
    try:
        valid = (
            document.get("schema_version") == SCHEMA_VERSION
            and isinstance(declared, str)
            and _SHA.fullmatch(declared) is not None
            and _json_sha256(document) == declared
        )
    finally:
        document["authority_sha256"] = declared
    if not valid:
        raise ReviewedCoverCommittedRecoveryAuthorityError("committed recovery authority digest drifted")
    return document


def _absolute(runtime_root: Path, entry: Mapping[str, Any], *, label: str) -> Path:
    if set(entry) != {"absolute_path", "runtime_relative_path", "sha256"}:
        raise ReviewedCoverCommittedRecoveryAuthorityError(f"{label} schema drifted")
    absolute = entry.get("absolute_path")
    relative = entry.get("runtime_relative_path")
    if not isinstance(absolute, str) or not isinstance(relative, str):
        raise ReviewedCoverCommittedRecoveryAuthorityError(f"{label} path is invalid")
    path = Path(absolute)
    try:
        root = runtime_root.resolve(strict=True)
        resolved = path.resolve(strict=True)
        if not path.is_absolute() or str(resolved) != absolute or resolved != (root / relative).resolve(strict=True):
            raise ValueError
    except (OSError, ValueError) as exc:
        raise ReviewedCoverCommittedRecoveryAuthorityError(f"{label} path drifted") from exc
    return resolved


def _runtime_path(
    runtime_root: Path, *, absolute_path: object, runtime_relative_path: object, label: str
) -> Path:
    return _absolute(
        runtime_root,
        {
            "absolute_path": absolute_path,
            "runtime_relative_path": runtime_relative_path,
            "sha256": "sha256:" + "0" * 64,
        },
        label=label,
    )


def _require_entry(runtime_root: Path, entry: Mapping[str, Any], *, label: str) -> Path:
    path = _absolute(
        runtime_root,
        {
            "absolute_path": entry.get("absolute_path"),
            "runtime_relative_path": entry.get("runtime_relative_path"),
            "sha256": entry.get("sha256"),
        },
        label=label,
    )
    _read_bytes(path, expected=str(entry["sha256"]), label=label)
    return path


def _canonical_state_sha256(record: Mapping[str, Any]) -> str:
    return _json_sha256(record)


def validate_committed_recovery(
    *,
    repo_root: Path,
    runtime_root: Path,
    plan_path: Path,
    plan_sha256: str,
    journal_path: Path,
    journal: Mapping[str, Any],
    records: Mapping[str, Mapping[str, Any]],
    state_is_bound: bool,
    code_fingerprint: str,
) -> None:
    """Replay the sealed recovery evidence.  This function never writes."""

    authority = load_committed_recovery_authority(repo_root=repo_root)
    if (
        not isinstance(code_fingerprint, str)
        or _SHA.fullmatch(code_fingerprint) is None
        or code_fingerprint != authority.get("successor_code_fingerprint")
    ):
        raise ReviewedCoverCommittedRecoveryAuthorityError("successor code fingerprint drifted")
    candidate = authority["candidate"]
    if not isinstance(candidate, Mapping) or set(candidate) != {"candidate_id", "recording_date", "title"}:
        raise ReviewedCoverCommittedRecoveryAuthorityError("candidate authority schema drifted")
    candidate_id = candidate["candidate_id"]
    if not isinstance(candidate_id, str) or not state_is_bound or set(records) != {candidate_id}:
        raise ReviewedCoverCommittedRecoveryAuthorityError("candidate recovery scope is not exact")
    record = records[candidate_id]
    if record.get("candidate_id") != candidate_id or record.get("title") != candidate["title"]:
        raise ReviewedCoverCommittedRecoveryAuthorityError("candidate state title drifted")
    preimage = authority["state_preimage"]
    if not isinstance(preimage, Mapping) or set(preimage) != {"canonical_sha256"} or _canonical_state_sha256(record) != preimage.get("canonical_sha256"):
        raise ReviewedCoverCommittedRecoveryAuthorityError("candidate stale state preimage drifted")
    plan = authority["plan"]
    if not isinstance(plan, Mapping) or set(plan) != {"repo_relative_path", "file_sha256", "plan_sha256"}:
        raise ReviewedCoverCommittedRecoveryAuthorityError("plan authority schema drifted")
    try:
        expected_plan = (repo_root / str(plan["repo_relative_path"])).resolve(strict=True)
    except OSError as exc:
        raise ReviewedCoverCommittedRecoveryAuthorityError("plan authority path is unavailable") from exc
    if plan_path.resolve(strict=True) != expected_plan or plan_sha256 != plan["plan_sha256"]:
        raise ReviewedCoverCommittedRecoveryAuthorityError("plan authority binding drifted")
    _read_bytes(expected_plan, expected=str(plan["file_sha256"]), label="plan")
    invalidation = authority["invalidation_journal"]
    if not isinstance(invalidation, Mapping) or set(invalidation) != {
        "absolute_path", "runtime_relative_path", "sha256", "schema_version", "status", "old_code_fingerprint"
    }:
        raise ReviewedCoverCommittedRecoveryAuthorityError("invalidation authority schema drifted")
    invalidation_path = _require_entry(runtime_root, invalidation, label="invalidation journal")
    if journal_path.resolve(strict=True) != invalidation_path or dict(journal) != _read_json(invalidation_path, expected=str(invalidation["sha256"]), label="invalidation journal"):
        raise ReviewedCoverCommittedRecoveryAuthorityError("invalidation journal changed while validating")
    if (
        journal.get("schema_version") != invalidation.get("schema_version")
        or journal.get("status") != "COMMITTED"
        or journal.get("code_fingerprint") != invalidation.get("old_code_fingerprint")
        or journal.get("date") != candidate.get("recording_date")
        or journal.get("plan_sha256") != plan_sha256
        or journal.get("upload_enabled") is not False
    ):
        raise ReviewedCoverCommittedRecoveryAuthorityError("invalidation journal content drifted")
    successor = authority["successor"]
    if not isinstance(successor, Mapping) or set(successor) != {
        "transaction", "generation_manifest", "binding", "generated_final_cover",
        "delivery_cover", "delivery_publish", "transaction_entries",
    }:
        raise ReviewedCoverCommittedRecoveryAuthorityError("successor authority schema drifted")
    transaction_path = _require_entry(runtime_root, successor["transaction"], label="successor transaction")
    transaction = _read_json(transaction_path, expected=str(successor["transaction"]["sha256"]), label="successor transaction")
    if transaction.get("schema_version") != "lidousha-cover-transaction.v1" or transaction.get("status") != "COMMITTED" or transaction.get("candidate_id") != candidate_id or transaction.get("title") != candidate["title"] or transaction.get("upload_enabled") is not False:
        raise ReviewedCoverCommittedRecoveryAuthorityError("successor transaction content drifted")
    generation_path = _require_entry(runtime_root, successor["generation_manifest"], label="generation manifest")
    binding_path = _require_entry(runtime_root, successor["binding"], label="cover binding")
    generated_cover_path = _require_entry(
        runtime_root, successor["generated_final_cover"], label="generated final cover"
    )
    delivery_cover_path = _require_entry(
        runtime_root, successor["delivery_cover"], label="delivery cover"
    )
    delivery_publish_path = _require_entry(
        runtime_root, successor["delivery_publish"], label="delivery publish"
    )
    generation = _read_json(generation_path, expected=str(successor["generation_manifest"]["sha256"]), label="generation manifest")
    binding = _read_json(binding_path, expected=str(successor["binding"]["sha256"]), label="cover binding")
    if generation.get("candidate_id") != candidate_id or generation.get("title") != candidate["title"] or generation.get("status") != "AI_COVER_READY" or binding.get("candidate_id") != candidate_id or binding.get("title") != candidate["title"] or binding.get("schema_version") != "lidousha-cover-repair-binding.v1":
        raise ReviewedCoverCommittedRecoveryAuthorityError("successor candidate/title drifted")
    if (
        Path(str(generation.get("final_cover") or "")).resolve(strict=True) != generated_cover_path
        or Path(str(binding.get("cover_path") or "")).resolve(strict=True) != delivery_cover_path
        or generation.get("final_cover_sha256") != successor["generated_final_cover"]["sha256"]
        or binding.get("cover_sha256") != successor["delivery_cover"]["sha256"]
        or binding.get("generation_manifest_path") != str(generation_path)
        or binding.get("generation_manifest_sha256") != successor["generation_manifest"]["sha256"]
        or binding.get("generation_cover_path") != str(generated_cover_path)
        or binding.get("generation_cover_sha256") != successor["generated_final_cover"]["sha256"]
    ):
        raise ReviewedCoverCommittedRecoveryAuthorityError("successor final cover path drifted")
    generation_root = generation_path.parent.resolve(strict=True)
    generations_root = generation_root.parent
    if {child.resolve(strict=True) for child in generations_root.iterdir()} != {generation_root}:
        raise ReviewedCoverCommittedRecoveryAuthorityError("unexpected cover generation exists")
    if transaction.get("generation_dir") != str(generation_root):
        raise ReviewedCoverCommittedRecoveryAuthorityError("successor generation directory drifted")
    expected_targets = {
        "delivery_cover": delivery_cover_path,
        "binding": binding_path,
        "delivery_publish": delivery_publish_path,
    }
    documents = authority["active_documents"]
    if not isinstance(documents, list) or len(documents) != 3:
        raise ReviewedCoverCommittedRecoveryAuthorityError("active document authority schema drifted")
    for index, entry in enumerate(documents):
        if not isinstance(entry, Mapping) or set(entry) != {"absolute_path", "runtime_relative_path", "sha256", "title"} or entry.get("title") != candidate["title"]:
            raise ReviewedCoverCommittedRecoveryAuthorityError("active document authority schema drifted")
        document_path = _require_entry(runtime_root, entry, label=f"active document {index}")
        document = _read_json(document_path, expected=str(entry["sha256"]), label=f"active document {index}")
        view = document if document.get("schema_version") == "shadow-publish-draft.v1" else document.get("publish_staging")
        pointer = document.get("cover_repair_binding")
        hashes = document.get("artifact_hashes")
        if (
            not isinstance(view, Mapping)
            or view.get("title") != candidate["title"]
            or view.get("cover_path") != str(delivery_cover_path)
            or view.get("cover_generation") != generation
            or not isinstance(pointer, Mapping)
            or pointer.get("path") != str(binding_path)
            or pointer.get("sha256") != successor["binding"]["sha256"]
            or not isinstance(hashes, Mapping)
            or hashes.get("cover_sha256") != successor["delivery_cover"]["sha256"]
        ):
            raise ReviewedCoverCommittedRecoveryAuthorityError("active document title drifted")
        if index == 0:
            expected_targets["delivery_record"] = document_path
        elif index == 1:
            expected_targets["source_record"] = document_path
        else:
            expected_targets["source_publish"] = document_path
    entries = successor["transaction_entries"]
    if (
        not isinstance(entries, list)
        or len(entries) != 6
        or not isinstance(transaction.get("entries"), list)
        or len(transaction["entries"]) != 6
        or [entry.get("role") for entry in entries if isinstance(entry, Mapping)] != [
            "delivery_cover", "binding", "delivery_publish", "delivery_record", "source_record", "source_publish"
        ]
    ):
        raise ReviewedCoverCommittedRecoveryAuthorityError("successor transaction entries drifted")
    for index, entry in enumerate(entries):
        if not isinstance(entry, Mapping) or set(entry) != {
            "role", "target_absolute_path", "target_runtime_relative_path",
            "intended_blob_absolute_path", "intended_blob_runtime_relative_path", "intended_sha256",
        }:
            raise ReviewedCoverCommittedRecoveryAuthorityError("successor transaction entry schema drifted")
        expected_entry = (transaction.get("entries") or [])[index] if isinstance(transaction.get("entries"), list) and index < len(transaction["entries"]) else None
        if not isinstance(expected_entry, Mapping):
            raise ReviewedCoverCommittedRecoveryAuthorityError("successor transaction entry is missing")
        target = _runtime_path(runtime_root, absolute_path=entry["target_absolute_path"], runtime_relative_path=entry["target_runtime_relative_path"], label="successor target")
        blob = _runtime_path(runtime_root, absolute_path=entry["intended_blob_absolute_path"], runtime_relative_path=entry["intended_blob_runtime_relative_path"], label="successor intended blob")
        if (
            target != expected_targets.get(entry["role"])
            or expected_entry.get("target") != str(target)
            or expected_entry.get("intended_blob") != str(blob)
            or expected_entry.get("intended_sha256") != entry["intended_sha256"]
        ):
            raise ReviewedCoverCommittedRecoveryAuthorityError("successor transaction entry drifted")
        _read_bytes(blob, expected=str(entry["intended_sha256"]), label="successor intended blob")
        _read_bytes(target, expected=str(entry["intended_sha256"]), label="successor target")
    immutable = authority["immutable"]
    if not isinstance(immutable, Mapping) or set(immutable) != {"video", "subtitle", "ass", "boundary_audit_sha256", "story_contract_sha256"}:
        raise ReviewedCoverCommittedRecoveryAuthorityError("immutable authority schema drifted")
    immutable_video = _require_entry(runtime_root, immutable["video"], label="immutable video")
    immutable_subtitle = _require_entry(runtime_root, immutable["subtitle"], label="immutable subtitle")
    if (
        not isinstance(immutable.get("ass"), Mapping)
        or set(immutable["ass"]) != {"absolute_path", "sha256"}
        or immutable["ass"].get("absolute_path") is not None
        or immutable["ass"].get("sha256") is not None
        or not _SHA.fullmatch(str(immutable.get("boundary_audit_sha256") or ""))
        or not _SHA.fullmatch(str(immutable.get("story_contract_sha256") or ""))
    ):
        raise ReviewedCoverCommittedRecoveryAuthorityError("immutable authority schema drifted")
    record_documents = [
        _read_json(
            _require_entry(runtime_root, entry, label="active record document"),
            expected=str(entry["sha256"]),
            label="active record document",
        )
        for entry in documents[:2]
    ]
    if any(document.get("schema_version") == "shadow-publish-draft.v1" for document in record_documents):
        raise ReviewedCoverCommittedRecoveryAuthorityError("record document role drifted")
    if any(
        not isinstance(document.get("boundary_audit"), Mapping)
        or not isinstance(document.get("story_contract"), Mapping)
        or document.get("media_path") != str(immutable_video)
        or document.get("subtitle_path") != str(immutable_subtitle)
        or not isinstance(document.get("artifact_hashes"), Mapping)
        or document["artifact_hashes"].get("video_sha256") != immutable["video"]["sha256"]
        or document["artifact_hashes"].get("subtitle_sha256") != immutable["subtitle"]["sha256"]
        or _json_sha256(document["boundary_audit"]) != immutable["boundary_audit_sha256"]
        or _json_sha256(document["story_contract"]) != immutable["story_contract_sha256"]
        for document in record_documents
    ):
        raise ReviewedCoverCommittedRecoveryAuthorityError("immutable record authority drifted")
