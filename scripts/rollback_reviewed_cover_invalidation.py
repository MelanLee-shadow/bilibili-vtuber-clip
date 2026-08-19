#!/usr/bin/env python3
"""Rollback one committed reviewed-cover invalidation without public edits.

This is a narrow recovery lane for a transaction that invalidated local cover
authority before the publication registry refused generic maintenance.  The
plan binds the committed invalidation, current bytes, recovery preimages, state
row, and the already-published registry row.  Documents are restored first and
the single candidate state row last; no cover generation or upload code runs.
"""

from __future__ import annotations

import argparse
import copy
import fcntl
import hashlib
import json
import os
import re
import stat
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.authorized_upload import (  # noqa: E402
    DEFAULT_UPLOAD_LOCK,
    UploadLockBusy,
    exclusive_upload_lock,
)
from scripts.session_autoslice import BASE  # noqa: E402
from scripts.repair_reviewed_covers import _invalidate_document  # noqa: E402
from src.autoslice.cover_maintenance import _json_file_bytes  # noqa: E402
from src.autoslice.publication_registry import (  # noqa: E402
    cover_maintenance_block_reason,
)


PLAN_SCHEMA = "reviewed-cover-invalidation-rollback-plan.v1"
JOURNAL_SCHEMA = "reviewed-cover-invalidation-rollback-transaction.v1"
INVALIDATION_SCHEMA = "reviewed-cover-invalidation-transaction.v1"
PURPOSE = "ROLLBACK_LOCAL_INVALIDATION_AFTER_PUBLISHED_PREFLIGHT_GAP_NO_PUBLIC_EDIT"
SAFE_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,96}")
DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
SHA_RE = re.compile(r"(?:sha256:)?([0-9a-f]{64})")


class CoverInvalidationRollbackError(RuntimeError):
    pass


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sha(value: object) -> str:
    match = SHA_RE.fullmatch(str(value or ""))
    if match is None:
        raise CoverInvalidationRollbackError(f"invalid SHA256 value: {value!r}")
    return match.group(1)


def _json_bytes(value: Mapping[str, Any]) -> bytes:
    return json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8")


def _canonical_record_sha256(record: Mapping[str, Any]) -> str:
    return _sha256_bytes(
        json.dumps(
            record,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    )


def _read_json_bytes(path: Path, *, label: str) -> tuple[dict[str, Any], bytes]:
    try:
        raw = path.read_bytes()
        value = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise CoverInvalidationRollbackError(
            f"{label} is unreadable: {path}: {exc}"
        ) from exc
    if not isinstance(value, dict):
        raise CoverInvalidationRollbackError(f"{label} must be a JSON object: {path}")
    return value, raw


def _resolved_regular_file(
    path: Path,
    *,
    label: str,
    roots: tuple[Path, ...],
) -> Path:
    if not path.is_absolute() or path.is_symlink():
        raise CoverInvalidationRollbackError(f"{label} must be an absolute regular non-symlink")
    try:
        resolved = path.resolve(strict=True)
        info = path.stat()
    except OSError as exc:
        raise CoverInvalidationRollbackError(f"{label} is inaccessible: {path}: {exc}") from exc
    if resolved != path or not stat.S_ISREG(info.st_mode):
        raise CoverInvalidationRollbackError(f"{label} has symlink ancestry or is not regular: {path}")
    resolved_roots = tuple(root.resolve(strict=True) for root in roots)
    if not any(resolved.is_relative_to(root) for root in resolved_roots):
        raise CoverInvalidationRollbackError(f"{label} escapes its allowed roots: {path}")
    return resolved


def _assert_file_hash(path: Path, expected: object, *, label: str) -> bytes:
    raw = path.read_bytes()
    actual = _sha256_bytes(raw)
    if actual != _sha(expected):
        raise CoverInvalidationRollbackError(
            f"{label} hash drifted: expected={_sha(expected)} actual={actual} path={path}"
        )
    return raw


def _candidate_location(
    state: Mapping[str, Any], candidate_id: str, *, label: str
) -> tuple[str, int, dict[str, Any]]:
    found: list[tuple[str, int, dict[str, Any]]] = []
    for lane in ("picks", "songs"):
        rows = state.get(lane, [])
        if not isinstance(rows, list):
            raise CoverInvalidationRollbackError(f"{label}.{lane} must be a list")
        for index, row in enumerate(rows):
            if isinstance(row, dict) and row.get("candidate_id") == candidate_id:
                found.append((lane, index, row))
    if len(found) != 1:
        raise CoverInvalidationRollbackError(
            f"{label} must contain exactly one candidate row for {candidate_id}: {len(found)}"
        )
    return found[0]


def _validate_publication_registry(
    registry: Mapping[str, Any],
    *,
    candidate_id: str,
    date: str,
    bvid: str,
) -> None:
    entries = [
        row
        for row in registry.get("entries", [])
        if isinstance(row, dict)
        and row.get("candidate_id") == candidate_id
        and row.get("recording_date") == date
    ]
    if len(entries) != 1 or entries[0].get("status") != "published" or entries[0].get("bvid") != bvid:
        raise CoverInvalidationRollbackError("published registry authority drifted")
    reason = cover_maintenance_block_reason(
        candidate_id,
        recording_date=date,
        registry=registry,
    )
    if reason is None or bvid not in reason:
        raise CoverInvalidationRollbackError("published generic-cover block is not active")


def load_plan(path: Path) -> tuple[dict[str, Any], str]:
    if path.is_symlink():
        raise CoverInvalidationRollbackError("rollback plan may not be a symlink")
    resolved = path.resolve(strict=True)
    if not resolved.is_relative_to(ROOT.resolve(strict=True)):
        raise CoverInvalidationRollbackError("rollback plan escapes the deployed repo")
    plan, raw = _read_json_bytes(resolved, label="rollback plan")
    if plan.get("schema_version") != PLAN_SCHEMA or plan.get("purpose") != PURPOSE:
        raise CoverInvalidationRollbackError("rollback plan schema/purpose is invalid")
    date = str(plan.get("date") or "")
    candidate_id = str(plan.get("candidate_id") or "")
    if (
        DATE_RE.fullmatch(date) is None
        or SAFE_ID_RE.fullmatch(candidate_id) is None
        or plan.get("upload_enabled") is not False
        or plan.get("public_edit_enabled") is not False
    ):
        raise CoverInvalidationRollbackError("rollback plan authority boundary is invalid")
    return plan, _sha256_bytes(raw)


def _validate_preimage_document(
    payload: bytes,
    *,
    candidate_id: str,
    expected_cover_sha256: str,
    kind: str,
) -> None:
    try:
        document = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise CoverInvalidationRollbackError("document preimage is not valid JSON") from exc
    if not isinstance(document, dict):
        raise CoverInvalidationRollbackError("document preimage must be an object")
    hashes = document.get("artifact_hashes")
    if not isinstance(hashes, dict) or _sha(hashes.get("cover_sha256")) != expected_cover_sha256:
        raise CoverInvalidationRollbackError("document preimage cover hash is not state-bound")
    if kind == "record":
        story = document.get("story_contract")
        if not isinstance(story, dict) or story.get("candidate_id") != candidate_id:
            raise CoverInvalidationRollbackError("record preimage candidate authority drifted")
    elif kind == "publish":
        if (
            document.get("schema_version") != "shadow-publish-draft.v1"
            or document.get("candidate_id") != candidate_id
            or document.get("upload_enabled") is not False
            or document.get("cover_status") != "AI_COVER_READY"
            or not isinstance(document.get("cover_generation"), dict)
        ):
            raise CoverInvalidationRollbackError("publish preimage authority drifted")
    else:
        raise CoverInvalidationRollbackError(f"unsupported document kind: {kind}")


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_write(path: Path, payload: bytes) -> None:
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _write_blob_once(path: Path, payload: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags, 0o600)
    except FileExistsError:
        if path.is_symlink() or _sha256_file(path) != _sha256_bytes(payload):
            raise CoverInvalidationRollbackError(f"recovery blob drifted: {path}")
        return
    try:
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise CoverInvalidationRollbackError(f"short recovery blob write: {path}")
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    _fsync_directory(path.parent)


def _preflight(plan: Mapping[str, Any], plan_sha256: str) -> dict[str, Any]:
    date = str(plan["date"])
    candidate_id = str(plan["candidate_id"])
    state_root = BASE / "state"
    out_root = BASE / "out" / date
    delivery_root = ROOT / "lidousha" / date
    package_root = out_root / candidate_id
    repo_plan_root = ROOT / "assets" / "lidousha" / "cover_repair_plans"

    registry_entry = plan.get("publication_registry")
    invalidation_plan_entry = plan.get("invalidation_plan")
    transaction_entry = plan.get("invalidation_transaction")
    state_entry = plan.get("state")
    documents = plan.get("documents")
    if not all(isinstance(value, dict) for value in (registry_entry, invalidation_plan_entry, transaction_entry, state_entry)):
        raise CoverInvalidationRollbackError("rollback plan envelope is incomplete")
    if not isinstance(documents, list) or len(documents) != 3:
        raise CoverInvalidationRollbackError("rollback plan requires exactly three active documents")

    registry_path = _resolved_regular_file(
        Path(str(registry_entry["path"])), label="publication registry", roots=(ROOT,)
    )
    registry_raw = _assert_file_hash(registry_path, registry_entry["sha256"], label="publication registry")
    registry = json.loads(registry_raw)
    _validate_publication_registry(
        registry,
        candidate_id=candidate_id,
        date=date,
        bvid=str(registry_entry.get("bvid") or ""),
    )

    invalidation_plan_path = _resolved_regular_file(
        Path(str(invalidation_plan_entry["path"])),
        label="invalidation plan",
        roots=(repo_plan_root,),
    )
    invalidation_plan_raw = _assert_file_hash(
        invalidation_plan_path,
        invalidation_plan_entry["sha256"],
        label="invalidation plan",
    )
    invalidation_plan = json.loads(invalidation_plan_raw)
    if (
        invalidation_plan.get("schema_version") != "reviewed-cover-repair-plan.v1"
        or invalidation_plan.get("date") != date
        or invalidation_plan.get("upload_enabled") is not False
    ):
        raise CoverInvalidationRollbackError("invalidation plan authority drifted")
    upload_ledger = invalidation_plan.get("upload_ledger")
    if not isinstance(upload_ledger, dict):
        raise CoverInvalidationRollbackError("invalidation plan upload ledger binding is missing")
    upload_ledger_path = _resolved_regular_file(
        Path(str(upload_ledger.get("path") or "")),
        label="upload ledger",
        roots=(BASE / "reports",),
    )
    _assert_file_hash(
        upload_ledger_path,
        upload_ledger.get("sha256"),
        label="upload ledger",
    )

    transaction_path = _resolved_regular_file(
        Path(str(transaction_entry["path"])),
        label="committed invalidation transaction",
        roots=(out_root / "reviewed_cover_repairs",),
    )
    transaction_raw = _assert_file_hash(
        transaction_path,
        transaction_entry["sha256"],
        label="committed invalidation transaction",
    )
    transaction = json.loads(transaction_raw)
    if (
        transaction.get("schema_version") != INVALIDATION_SCHEMA
        or transaction.get("status") != "COMMITTED"
        or transaction.get("date") != date
        or transaction.get("plan_sha256") != _sha(invalidation_plan_entry["sha256"])
        or transaction.get("upload_enabled") is not False
    ):
        raise CoverInvalidationRollbackError("committed invalidation transaction drifted")

    state_path = _resolved_regular_file(
        Path(str(state_entry["path"])), label="current state", roots=(state_root,)
    )
    state_raw = state_path.read_bytes()
    state_file_sha256 = _sha256_bytes(state_raw)
    state = json.loads(state_raw)
    lane, index, current_record = _candidate_location(state, candidate_id, label="current state")
    current_record_sha256 = _canonical_record_sha256(current_record)

    backup_path = _resolved_regular_file(
        Path(str(state_entry["preimage_path"])), label="state preimage", roots=(state_root,)
    )
    backup_raw = _assert_file_hash(backup_path, state_entry["preimage_file_sha256"], label="state preimage")
    backup = json.loads(backup_raw)
    backup_lane, _backup_index, preimage_record = _candidate_location(
        backup, candidate_id, label="state preimage"
    )
    if backup_lane != lane or _canonical_record_sha256(preimage_record) != _sha(state_entry["preimage_record_sha256"]):
        raise CoverInvalidationRollbackError("state candidate preimage drifted")
    state_already_restored = current_record_sha256 == _sha(
        state_entry["preimage_record_sha256"]
    )
    if not state_already_restored and (
        current_record_sha256 != _sha(state_entry["expected_current_record_sha256"])
        or state_file_sha256 != _sha(state_entry["expected_current_file_sha256"])
    ):
        raise CoverInvalidationRollbackError("current invalidated candidate row/state drifted")
    expected_cover_sha256 = _sha(preimage_record.get("cover_sha256"))
    if (
        preimage_record.get("status") != "published"
        or preimage_record.get("cover_status") != "AI_COVER_READY"
        or not isinstance(preimage_record.get("cover_generation"), dict)
    ):
        raise CoverInvalidationRollbackError("state candidate preimage is not published/cover-ready")

    intended_state = copy.deepcopy(state)
    intended_state[lane][index] = copy.deepcopy(preimage_record)
    intended_state_raw = _json_bytes(intended_state)

    transaction_targets = {
        Path(str(entry.get("target") or "")): entry
        for entry in transaction.get("entries", [])
        if isinstance(entry, dict) and entry.get("candidate_id") == candidate_id
    }
    matching_invalidation_rows = [
        row
        for row in invalidation_plan.get("invalidations", [])
        if isinstance(row, dict) and row.get("candidate_id") == candidate_id
    ]
    if len(matching_invalidation_rows) != 1:
        raise CoverInvalidationRollbackError("invalidation plan candidate authority is ambiguous")
    invalidation_authority = matching_invalidation_rows[0]
    plan_original_docs = {
        Path(str(entry.get("path") or "")): entry
        for entry in invalidation_authority.get("documents", [])
        if isinstance(entry, dict)
    }
    document_entries: list[dict[str, Any]] = []
    seen_targets: set[Path] = set()
    any_document_already_restored = False
    for document in documents:
        if not isinstance(document, dict):
            raise CoverInvalidationRollbackError("document recovery entry must be an object")
        target = _resolved_regular_file(
            Path(str(document["target"])),
            label="invalidated document target",
            roots=(delivery_root, package_root),
        )
        preimage = _resolved_regular_file(
            Path(str(document["preimage_path"])),
            label="document recovery preimage",
            roots=(delivery_root, package_root),
        )
        if target in seen_targets or target not in transaction_targets or target not in plan_original_docs:
            raise CoverInvalidationRollbackError("document target set does not match committed invalidation")
        seen_targets.add(target)
        invalidation_entry = transaction_targets[target]
        original_entry = plan_original_docs[target]
        if (
            _sha(document["expected_invalidated_sha256"])
            != _sha(invalidation_entry.get("intended_sha256"))
            or _sha(document["original_expected_sha256"])
            != _sha(original_entry.get("sha256"))
        ):
            raise CoverInvalidationRollbackError("document invalidation/original authority drifted")
        current_raw = target.read_bytes()
        current_sha = _sha256_bytes(current_raw)
        recovery_raw = _assert_file_hash(
            preimage, document["recovery_preimage_sha256"], label="document recovery preimage"
        )
        recovery_sha = _sha256_bytes(recovery_raw)
        if current_sha not in {_sha(document["expected_invalidated_sha256"]), recovery_sha}:
            raise CoverInvalidationRollbackError(f"document target drifted before recovery: {target}")
        any_document_already_restored = any_document_already_restored or current_sha == recovery_sha
        if recovery_sha != _sha(document["original_expected_sha256"]):
            if (
                document.get("preimage_kind") != "SANCTIONED_ACTIVE_SIBLING"
                or not isinstance(document.get("original_preimage_unavailable_reason"), str)
                or not str(document["original_preimage_unavailable_reason"]).strip()
            ):
                raise CoverInvalidationRollbackError("non-exact document recovery lacks explicit sibling authority")
            recovered_document = json.loads(recovery_raw)
            reinvalidated = _json_file_bytes(
                _invalidate_document(
                    recovered_document,
                    title=str(invalidation_authority.get("title") or ""),
                    cover_text=(
                        str(invalidation_authority["expected_cover_text"])
                        if isinstance(
                            invalidation_authority.get("expected_cover_text"), str
                        )
                        else None
                    ),
                )
            )
            if _sha256_bytes(reinvalidated) != _sha(
                document["expected_invalidated_sha256"]
            ):
                raise CoverInvalidationRollbackError(
                    "sanctioned sibling does not reproduce the committed invalidation"
                )
        elif document.get("preimage_kind") != "EXACT_ORIGINAL_PREIMAGE":
            raise CoverInvalidationRollbackError("exact document preimage kind is invalid")
        _validate_preimage_document(
            recovery_raw,
            candidate_id=candidate_id,
            expected_cover_sha256=expected_cover_sha256,
            kind=str(document.get("kind") or ""),
        )
        document_entries.append(
            {
                "kind": "document",
                "document_kind": str(document.get("kind") or ""),
                "target": target,
                "accepted_prior_sha256": _sha(document["expected_invalidated_sha256"]),
                "intended_payload": recovery_raw,
                "prior_payload": current_raw,
                "intended_sha256": recovery_sha,
                "preimage_path": str(preimage),
                "preimage_kind": document["preimage_kind"],
                "original_expected_sha256": _sha(document["original_expected_sha256"]),
            }
        )
    if seen_targets != set(transaction_targets) or seen_targets != set(plan_original_docs):
        raise CoverInvalidationRollbackError("document recovery set is incomplete")
    publish_entries = [
        entry for entry in document_entries if entry["document_kind"] == "publish"
    ]
    record_entries = [
        entry for entry in document_entries if entry["document_kind"] == "record"
    ]
    if len(publish_entries) != 1 or len(record_entries) != 2:
        raise CoverInvalidationRollbackError("record/publish recovery document roles are invalid")
    publish_sha256 = publish_entries[0]["intended_sha256"]
    for entry in record_entries:
        record_document = json.loads(entry["intended_payload"])
        if _sha(record_document.get("artifact_hashes", {}).get("publish_draft_sha256")) != publish_sha256:
            raise CoverInvalidationRollbackError(
                "record preimage does not bind the sanctioned publish sibling"
            )

    return {
        "date": date,
        "candidate_id": candidate_id,
        "bvid": str(registry_entry["bvid"]),
        "plan_sha256": plan_sha256,
        "transaction_path": transaction_path,
        "document_entries": document_entries,
        "state_entry": {
            "kind": "state",
            "target": state_path,
            "accepted_prior_sha256": _sha(state_entry["expected_current_file_sha256"]),
            "intended_payload": intended_state_raw,
            "prior_payload": state_raw,
            "intended_sha256": _sha256_bytes(intended_state_raw),
            "preimage_path": str(backup_path),
            "preimage_record_sha256": _sha(state_entry["preimage_record_sha256"]),
        },
        "partial_applied": state_already_restored or any_document_already_restored,
        "registry_path": registry_path,
        "registry_sha256": _sha(registry_entry["sha256"]),
        "upload_ledger_path": upload_ledger_path,
        "upload_ledger_sha256": _sha(upload_ledger["sha256"]),
        "invalidation_transaction_sha256": _sha(transaction_entry["sha256"]),
    }


def run(plan_path: Path, *, apply: bool) -> dict[str, Any]:
    plan, plan_sha256 = load_plan(plan_path)
    prepared = _preflight(plan, plan_sha256)
    entries = [*prepared["document_entries"], prepared["state_entry"]]
    result = {
        "schema_version": JOURNAL_SCHEMA,
        "status": "VALIDATED_ONLY" if not apply else "PREPARED",
        "purpose": PURPOSE,
        "date": prepared["date"],
        "candidate_id": prepared["candidate_id"],
        "bvid": prepared["bvid"],
        "rollback_plan_path": str(plan_path.resolve()),
        "rollback_plan_sha256": plan_sha256,
        "invalidation_transaction_path": str(prepared["transaction_path"]),
        "upload_enabled": False,
        "public_edit_enabled": False,
        "public_actions_performed": False,
        "source_publish_recovered_by": "INVERSE_INVALIDATION_EQUIVALENT",
        "entries": [
            {
                key: (str(value) if isinstance(value, Path) else value)
                for key, value in entry.items()
                if key not in {"intended_payload", "prior_payload"}
            }
            for entry in entries
        ],
    }
    if not apply:
        return result

    transaction_parent = prepared["transaction_path"].parent
    recovery_root = transaction_parent / f"rollback-{plan_sha256[:16]}"
    journal_path = recovery_root / "rollback-transaction.json"
    if recovery_root.exists():
        if recovery_root.is_symlink() or not recovery_root.is_dir():
            raise CoverInvalidationRollbackError("rollback transaction root is invalid")
    else:
        if prepared["partial_applied"]:
            raise CoverInvalidationRollbackError(
                "rollback targets contain restored bytes without their recovery journal"
            )
        recovery_root.mkdir(mode=0o700)
        _fsync_directory(recovery_root.parent)
    existing_prepared: dict[str, Any] | None = None
    if journal_path.exists():
        existing, _raw = _read_json_bytes(journal_path, label="rollback transaction")
        if (
            existing.get("schema_version") != JOURNAL_SCHEMA
            or existing.get("rollback_plan_sha256") != plan_sha256
            or existing.get("candidate_id") != prepared["candidate_id"]
            or existing.get("status")
            not in {
                "PREPARED",
                "ROLLED_BACK_PUBLISHED_CANDIDATE_PREFLIGHT_GAP",
            }
        ):
            raise CoverInvalidationRollbackError("rollback transaction journal drifted")
        expected_entries = {
            str(entry["target"]): entry["intended_sha256"] for entry in entries
        }
        existing_entries = {
            str(entry.get("target") or ""): str(entry.get("intended_sha256") or "")
            for entry in existing.get("entries", [])
            if isinstance(entry, dict)
        }
        if existing_entries != expected_entries:
            raise CoverInvalidationRollbackError("rollback transaction entry set drifted")
        if existing.get("status") == "ROLLED_BACK_PUBLISHED_CANDIDATE_PREFLIGHT_GAP":
            for target, expected in existing_entries.items():
                if _sha256_file(Path(target)) != expected:
                    raise CoverInvalidationRollbackError("committed rollback target drifted")
            return existing
        existing_prepared = existing

    if existing_prepared is None:
        journal_entries = []
        for index, entry in enumerate(entries):
            original_blob = recovery_root / f"original-{index:02d}-{entry['kind']}.bin"
            intended_blob = recovery_root / f"intended-{index:02d}-{entry['kind']}.bin"
            _write_blob_once(original_blob, entry["prior_payload"])
            _write_blob_once(intended_blob, entry["intended_payload"])
            journal_entries.append(
                {
                    key: (str(value) if isinstance(value, Path) else value)
                    for key, value in entry.items()
                    if key not in {"intended_payload", "prior_payload"}
                }
                | {
                    "original_blob": str(original_blob),
                    "original_blob_sha256": _sha256_bytes(entry["prior_payload"]),
                    "intended_blob": str(intended_blob),
                }
            )
        journal = result | {
            "status": "PREPARED",
            "prepared_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "entries": journal_entries,
        }
        _atomic_write(journal_path, _json_bytes(journal))
    else:
        journal = existing_prepared

    for entry in entries:  # documents first; published state authority last
        current_sha = _sha256_file(entry["target"])
        if current_sha == entry["intended_sha256"]:
            continue
        if current_sha != entry["accepted_prior_sha256"]:
            raise CoverInvalidationRollbackError(
                f"rollback target changed after preflight: {entry['target']}"
            )
        _atomic_write(entry["target"], entry["intended_payload"])
        if _sha256_file(entry["target"]) != entry["intended_sha256"]:
            raise CoverInvalidationRollbackError(f"rollback verification failed: {entry['target']}")

    revalidated = _preflight(plan, plan_sha256)
    for entry in [*revalidated["document_entries"], revalidated["state_entry"]]:
        if _sha256_file(entry["target"]) != entry["intended_sha256"]:
            raise CoverInvalidationRollbackError("post-rollback authority verification failed")
    _assert_file_hash(
        revalidated["registry_path"],
        revalidated["registry_sha256"],
        label="publication registry after rollback",
    )
    _assert_file_hash(
        revalidated["upload_ledger_path"],
        revalidated["upload_ledger_sha256"],
        label="upload ledger after rollback",
    )
    _assert_file_hash(
        revalidated["transaction_path"],
        revalidated["invalidation_transaction_sha256"],
        label="original invalidation transaction after rollback",
    )
    if not (BASE / "DISABLED").is_file():
        raise CoverInvalidationRollbackError("DISABLED disappeared during rollback")
    journal["status"] = "ROLLED_BACK_PUBLISHED_CANDIDATE_PREFLIGHT_GAP"
    journal["committed_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    _atomic_write(journal_path, _json_bytes(journal))
    return journal


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    if not (BASE / "DISABLED").is_file():
        raise CoverInvalidationRollbackError("DISABLED must exist for rollback")
    runner_lock = BASE / "runner.lock"
    with runner_lock.open("a+") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise CoverInvalidationRollbackError("runner.lock is busy") from exc
        try:
            with exclusive_upload_lock(DEFAULT_UPLOAD_LOCK):
                if not (BASE / "DISABLED").is_file():
                    raise CoverInvalidationRollbackError("DISABLED disappeared before rollback")
                result = run(args.plan, apply=args.apply)
        except UploadLockBusy as exc:
            raise CoverInvalidationRollbackError("upload.lock is busy") from exc
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
