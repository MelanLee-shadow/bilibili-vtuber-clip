#!/usr/bin/env python3
"""Create-only OCI3 source-disposition substitution receipts.

This lane is deliberately separate from the recorder adapter's ordinary FUSE
identity-rebind lane.  It records explicit operator evidence that a different
set of files is being substituted for historical files; it never asserts byte
identity, rewrites webhook identity, publishes media, or talks to a provider.

The request document is the only input contract.  ``migrate`` is dry-run unless
``--write`` is supplied.  ``transform`` creates a new state document containing
an inert, separate migration ledger; it leaves the original disposition and
webhook ledgers untouched and is not consumed by the adapter.
"""

from __future__ import annotations

import argparse
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import stat
import sys
from typing import Any


MIGRATION_SCHEMA_VERSION = "source-disposition-migration.v1"
RECEIPT_SCHEMA_VERSION = "source-disposition-migration-receipt.v1"
TRANSFORMED_STATE_LEDGER_SCHEMA_VERSION = "source-disposition-migration-ledger.v1"
REASON = "EXPLICIT_SOURCE_SUBSTITUTION_NOT_IDENTITY"
TOOL_COMMIT_DEFAULT = "981bc4ab"
FINGERPRINT_KEYS = ("size_bytes", "mtime_ns", "ctime_ns", "device", "inode", "mode")
REPLACEMENT_ROLES = ("source", "xml", "successor_source", "successor_mp4")


class MigrationError(ValueError):
    """A fail-closed migration contract violation."""


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
    except OSError as exc:
        raise MigrationError(f"cannot read {path}: {exc}") from exc
    return digest.hexdigest()


def _attest_regular_file(path: Path, field: str) -> tuple[dict[str, int], str]:
    """Hash through a no-follow descriptor and require a stable stat snapshot."""

    _lstat_regular(path, field)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise MigrationError(f"cannot open {field}: {path}") from exc
    digest = hashlib.sha256()
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise MigrationError(f"{field} changed to a non-regular or hardlinked file")
        while block := os.read(descriptor, 1024 * 1024):
            digest.update(block)
        after = os.fstat(descriptor)
    except OSError as exc:
        raise MigrationError(f"cannot attest {field}: {path}") from exc
    finally:
        os.close(descriptor)
    before_fp = stat_fingerprint(before)
    if before_fp != stat_fingerprint(after):
        raise MigrationError(f"{field} changed while hashing")
    return before_fp, digest.hexdigest()


def _safe_relative(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value or "\\" in value:
        raise MigrationError(f"{field} must be an exact POSIX relative path")
    parsed = PurePosixPath(value)
    if parsed.is_absolute() or ".." in parsed.parts or "." in parsed.parts or len(parsed.parts) < 1:
        raise MigrationError(f"{field} must not be absolute, dotted, or parent-relative")
    if any(ch in value for ch in "*?[]{}"):
        raise MigrationError(f"{field} must not use glob or fuzzy matching")
    if str(parsed) != value:
        raise MigrationError(f"{field} is not canonical")
    return value


def _hex(value: Any, field: str) -> str:
    if not isinstance(value, str) or len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
        raise MigrationError(f"{field} must be a lowercase SHA-256")
    return value


def _int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise MigrationError(f"{field} must be a non-negative integer")
    return value


def _lstat_regular(path: Path, field: str) -> os.stat_result:
    try:
        info = path.lstat()
    except OSError as exc:
        raise MigrationError(f"{field} is missing: {path}") from exc
    if stat.S_ISLNK(info.st_mode):
        raise MigrationError(f"{field} must not be a symlink: {path}")
    if not stat.S_ISREG(info.st_mode):
        raise MigrationError(f"{field} must be a regular file: {path}")
    if info.st_nlink != 1:
        raise MigrationError(f"{field} must not be a hardlink: {path}")
    return info


def _read_json(path: Path, field: str) -> tuple[dict[str, Any], bytes]:
    _lstat_regular(path, field)
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise MigrationError(f"cannot read {field}: {exc}") from exc
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MigrationError(f"{field} is not UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise MigrationError(f"{field} must contain a JSON object")
    return value, raw


def stat_fingerprint(info: os.stat_result) -> dict[str, int]:
    return {
        "size_bytes": info.st_size,
        "mtime_ns": info.st_mtime_ns,
        "ctime_ns": info.st_ctime_ns,
        "device": info.st_dev,
        "inode": info.st_ino,
        "mode": stat.S_IMODE(info.st_mode),
    }


def _evidence_path(entry: dict[str, Any], role: str) -> str:
    return _safe_relative(entry.get("relative_path", entry.get("path")), f"replacement.{role}.path")


def _reject_symlink_components(record_root: Path, relative: str, field: str) -> None:
    try:
        root_info = record_root.lstat()
    except OSError as exc:
        raise MigrationError(f"record root is missing: {record_root}") from exc
    if stat.S_ISLNK(root_info.st_mode):
        raise MigrationError(f"{field} record root must not be a symlink")
    current = record_root
    for component in PurePosixPath(relative).parts[:-1]:
        current = current / component
        try:
            info = current.lstat()
        except OSError as exc:
            raise MigrationError(f"{field} parent path is missing: {current}") from exc
        if stat.S_ISLNK(info.st_mode):
            raise MigrationError(f"{field} parent path must not be a symlink: {current}")
        if not stat.S_ISDIR(info.st_mode):
            raise MigrationError(f"{field} parent path is not a directory: {current}")


def _normalise_evidence(entry: Any, role: str, record_root: Path) -> dict[str, Any]:
    if not isinstance(entry, dict):
        raise MigrationError(f"replacement.{role} evidence must be an object")
    relative = _evidence_path(entry, role)
    _reject_symlink_components(record_root, relative, f"replacement.{role}")
    actual = record_root / relative
    actual_fp, actual_sha = _attest_regular_file(actual, f"replacement.{role}")
    expected = entry.get("stat") if isinstance(entry.get("stat"), dict) else entry
    fingerprint: dict[str, int] = {}
    for key in FINGERPRINT_KEYS:
        fingerprint[key] = _int(expected.get(key), f"replacement.{role}.{key}")
    if fingerprint != actual_fp:
        raise MigrationError(f"replacement.{role} stat fingerprint mismatch")
    expected_sha = _hex(entry.get("sha256"), f"replacement.{role}.sha256")
    if expected_sha != actual_sha:
        raise MigrationError(f"replacement.{role} SHA-256 mismatch")
    result: dict[str, Any] = {"relative_path": relative, "sha256": expected_sha, **fingerprint}
    if role == "successor_mp4":
        media = entry.get("media_probe", entry.get("media"))
        if not isinstance(media, dict):
            raise MigrationError("replacement.successor_mp4.media_probe evidence is required")
        required = ("size_bytes", "duration_seconds", "video_codec", "audio_codec", "width", "height")
        if any(key not in media for key in required):
            raise MigrationError("replacement.successor_mp4.media_probe is incomplete")
        if _int(media["size_bytes"], "replacement.successor_mp4.media_probe.size_bytes") != actual_fp["size_bytes"]:
            raise MigrationError("replacement.successor_mp4 media size mismatch")
        try:
            duration = float(media["duration_seconds"])
        except (TypeError, ValueError) as exc:
            raise MigrationError("replacement.successor_mp4 media duration is invalid") from exc
        if duration <= 0 or not isinstance(media["video_codec"], str) or not media["video_codec"]:
            raise MigrationError("replacement.successor_mp4 media probe is not decodable")
        if not isinstance(media["audio_codec"], str) or not media["audio_codec"]:
            raise MigrationError("replacement.successor_mp4 media audio evidence is invalid")
        if _int(media["width"], "replacement.successor_mp4 media width") <= 0 or _int(
            media["height"], "replacement.successor_mp4 media height"
        ) <= 0:
            raise MigrationError("replacement.successor_mp4 media dimensions are invalid")
        result["media_probe"] = deepcopy(media)
    return result


def _row_from_state(state: dict[str, Any], relative: str) -> dict[str, Any]:
    dispositions = state.get("source_dispositions")
    if not isinstance(dispositions, dict) or relative not in dispositions:
        raise MigrationError("old disposition relative is absent from old state snapshot")
    row = dispositions[relative]
    if not isinstance(row, dict):
        raise MigrationError("old disposition row is malformed")
    integrity = row.get("canonical_integrity")
    unsigned = {key: value for key, value in row.items() if key != "canonical_integrity"}
    if not isinstance(integrity, dict) or integrity.get("algorithm") != "sha256":
        raise MigrationError("old disposition row canonical integrity is missing")
    calculated = canonical_sha256(unsigned)
    if integrity.get("canonical_json_sha256") != calculated:
        raise MigrationError("old disposition row canonical integrity mismatch")
    return row


def _collect_webhook_ids(value: Any) -> list[str]:
    found: list[str] = []
    if isinstance(value, dict):
        for key, child in value.items():
            if key in {"opening_event_id", "closing_event_id"}:
                if not isinstance(child, str) or not child:
                    raise MigrationError("old webhook event ID is malformed")
                found.append(child)
            else:
                found.extend(_collect_webhook_ids(child))
    elif isinstance(value, list):
        for child in value:
            found.extend(_collect_webhook_ids(child))
    return found


def _authority(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or not value:
        raise MigrationError("operator authority JSON must be a non-empty object")
    return deepcopy(value)


def _replacement_request(request: dict[str, Any]) -> dict[str, Any]:
    replacement = request.get("replacement")
    if not isinstance(replacement, dict):
        replacement = {
            "source": request.get("replacement_source"),
            "xml": request.get("replacement_xml"),
            "successor_source": request.get("replacement_successor_flv"),
            "successor_mp4": request.get("replacement_successor_mp4"),
        }
    return replacement


def validate_request(request: Any, *, record_root: Path) -> dict[str, Any]:
    if not isinstance(request, dict):
        raise MigrationError("migration request must be a JSON object")
    if request.get("schema_version") != MIGRATION_SCHEMA_VERSION:
        raise MigrationError("migration request schema mismatch")
    if request.get("reason") != REASON:
        raise MigrationError(f"reason must be {REASON}")
    tool_commit = request.get("tool_commit")
    if not isinstance(tool_commit, str) or not tool_commit:
        raise MigrationError("schema/tool commit is required")
    authority = _authority(request.get("operator_authority"))

    old = request.get("old_state_snapshot")
    if not isinstance(old, dict):
        raise MigrationError("old_state_snapshot is required")
    state_path_value = old.get("path")
    if not isinstance(state_path_value, str) or not state_path_value:
        raise MigrationError("old_state_snapshot.path is required")
    state_path = Path(state_path_value)
    expected_state_sha = _hex(old.get("sha256"), "old_state_snapshot.sha256")
    state, raw = _read_json(state_path, "old state snapshot")
    if hashlib.sha256(raw).hexdigest() != expected_state_sha:
        raise MigrationError("old state snapshot SHA-256 drifted")
    old_state_canonical_sha = canonical_sha256(state)
    old_relative = _safe_relative(request.get("old_disposition_relative"), "old_disposition_relative")
    row = _row_from_state(state, old_relative)
    expected_row_sha = _hex(request.get("old_row_canonical_sha256"), "old_row_canonical_sha256")
    row_sha = row["canonical_integrity"]["canonical_json_sha256"]
    if row_sha != expected_row_sha:
        raise MigrationError("old row canonical SHA-256 mismatch")

    event_ids = request.get("old_webhook_event_ids")
    if not isinstance(event_ids, list) or not event_ids or any(not isinstance(x, str) or not x for x in event_ids):
        raise MigrationError("exact old webhook event IDs are required")
    if len(set(event_ids)) != len(event_ids):
        raise MigrationError("old webhook event IDs must be unique")
    found_ids = _collect_webhook_ids(row)
    if sorted(found_ids) != sorted(event_ids) or len(found_ids) != len(event_ids):
        raise MigrationError("old webhook event IDs do not exactly match the old row")
    webhook_index = state.get("webhook_event_ids")
    if isinstance(webhook_index, dict) and any(event_id not in webhook_index for event_id in event_ids):
        raise MigrationError("old webhook event ID is absent from the state event index")

    old_target = request.get("old_finalized_target_evidence")
    embedded_old_target = ((row.get("session") or {}).get("successor_finalized_ledger"))
    if not isinstance(old_target, dict) or old_target != embedded_old_target:
        raise MigrationError("old finalized target evidence must exactly match the old row snapshot")
    new_target = request.get("new_finalized_target_evidence")
    if not isinstance(new_target, dict):
        raise MigrationError("new finalized target evidence is required")

    replacement = _replacement_request(request)
    if set(replacement) != set(REPLACEMENT_ROLES):
        raise MigrationError("replacement must contain exactly source/xml/successor_source/successor_mp4")
    evidence = {
        role: _normalise_evidence(replacement[role], role, record_root) for role in REPLACEMENT_ROLES
    }
    paths = [evidence[role]["relative_path"] for role in REPLACEMENT_ROLES]
    if len(set(paths)) != len(paths):
        raise MigrationError("replacement mapping contains duplicate paths")
    if old_relative in paths:
        raise MigrationError("replacement mapping must not reuse the old disposition path")

    target = evidence["successor_mp4"]
    target_path = new_target.get("target", new_target.get("relative_path"))
    if target_path is not None and _safe_relative(target_path, "new_finalized_target_evidence.target") != target["relative_path"]:
        raise MigrationError("new finalized target path mismatch")
    if new_target.get("target_sha256", new_target.get("sha256")) != target["sha256"]:
        raise MigrationError("new finalized target SHA-256 mismatch")
    if "size_bytes" in new_target and new_target["size_bytes"] != target["size_bytes"]:
        raise MigrationError("new finalized target size mismatch")
    supplied_media = new_target.get("media_probe", new_target.get("media"))
    if supplied_media is None or supplied_media != target["media_probe"]:
        raise MigrationError("new finalized target media probe mismatch")

    return {
        "schema_version": MIGRATION_SCHEMA_VERSION,
        "tool_commit": tool_commit,
        "reason": REASON,
        "operator_authority": authority,
        "old_state_path": str(state_path),
        "old_state": state,
        "old_state_raw_sha256": expected_state_sha,
        "old_state_canonical_sha256": old_state_canonical_sha,
        "old_disposition_relative": old_relative,
        "old_row": deepcopy(row),
        "old_row_canonical_sha256": row_sha,
        "old_webhook_event_ids": list(event_ids),
        "old_finalized_target_evidence": deepcopy(old_target),
        "new_finalized_target_evidence": deepcopy(new_target),
        "replacement": evidence,
    }


def _mapping_material(validated: dict[str, Any]) -> dict[str, Any]:
    return {
        "old_disposition_relative": validated["old_disposition_relative"],
        "replacement": {
            role: validated["replacement"][role]["relative_path"] for role in REPLACEMENT_ROLES
        },
    }


def _build_receipt(validated: dict[str, Any], request: dict[str, Any]) -> dict[str, Any]:
    material = {
        key: value
        for key, value in request.items()
        if key not in {"old_state_snapshot", "operator_authority"}
    }
    receipt: dict[str, Any] = {
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "migration_schema_version": MIGRATION_SCHEMA_VERSION,
        "tool_commit": validated["tool_commit"],
        "reason": REASON,
        "operator_authority": deepcopy(validated["operator_authority"]),
        "request_canonical_sha256": canonical_sha256(request),
        "mapping_canonical_sha256": canonical_sha256(_mapping_material(validated)),
        "mapping": _mapping_material(validated),
        "old_state_snapshot": {
            "path": validated["old_state_path"],
            "raw_sha256": validated["old_state_raw_sha256"],
            "canonical_sha256": validated["old_state_canonical_sha256"],
        },
        "old_disposition": {
            "relative_path": validated["old_disposition_relative"],
            "row_snapshot": deepcopy(validated["old_row"]),
            "row_canonical_sha256": validated["old_row_canonical_sha256"],
            "webhook_event_ids": list(validated["old_webhook_event_ids"]),
        },
        "replacement_evidence": deepcopy(validated["replacement"]),
        "old_finalized_target_evidence": deepcopy(validated["old_finalized_target_evidence"]),
        "new_finalized_target_evidence": deepcopy(validated["new_finalized_target_evidence"]),
        "identity_proven": False,
        "source_substitution": True,
        "ordinary_rebind": False,
        "created_only": True,
        "publication_authority": None,
        "publication_effects": [],
        "request_material_sha256": canonical_sha256(material),
    }
    receipt["canonical_integrity"] = {
        "algorithm": "sha256",
        "canonical_json_sha256": canonical_sha256(receipt),
    }
    return receipt


def validate_receipt(receipt: Any) -> dict[str, Any]:
    if not isinstance(receipt, dict) or receipt.get("schema_version") != RECEIPT_SCHEMA_VERSION:
        raise MigrationError("migration receipt schema mismatch")
    integrity = receipt.get("canonical_integrity")
    unsigned = {key: value for key, value in receipt.items() if key != "canonical_integrity"}
    if integrity != {"algorithm": "sha256", "canonical_json_sha256": canonical_sha256(unsigned)}:
        raise MigrationError("migration receipt canonical self-seal mismatch")
    if receipt.get("reason") != REASON or receipt.get("identity_proven") is not False:
        raise MigrationError("migration receipt identity contract is invalid")
    if receipt.get("source_substitution") is not True or receipt.get("ordinary_rebind") is not False:
        raise MigrationError("migration receipt is not distinct from ordinary rebind")
    if receipt.get("publication_authority") is not None or receipt.get("publication_effects") != []:
        raise MigrationError("migration receipt must have no publication authority")
    return receipt


def _write_create_only(path: Path, payload: dict[str, Any]) -> bool:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8") + b"\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        existing, _ = _read_json(path, "existing migration receipt/state")
        return canonical_bytes(existing) == canonical_bytes(payload)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        try:
            path.unlink()
        except OSError:
            pass
        raise
    return True


def migrate(request: dict[str, Any], *, record_root: Path, receipt_path: Path | None, write: bool) -> dict[str, Any]:
    validated = validate_request(request, record_root=record_root)
    receipt = _build_receipt(validated, request)
    if receipt_path is not None and receipt_path.exists():
        existing, _ = _read_json(receipt_path, "existing migration receipt")
        validate_receipt(existing)
        if existing.get("mapping_canonical_sha256") != receipt["mapping_canonical_sha256"]:
            raise MigrationError("different mapping conflicts with existing create-only receipt")
        if canonical_bytes(existing) != canonical_bytes(receipt):
            raise MigrationError("same mapping has different evidence; refusing overwrite")
        receipt["idempotent_existing"] = True
        return receipt
    if write and receipt_path is None:
        raise MigrationError("--write requires --receipt")
    if write and receipt_path is not None:
        if not _write_create_only(receipt_path, receipt):
            raise MigrationError("existing output differs; refusing overwrite")
    return receipt


def transform_state(receipt: dict[str, Any], *, output_path: Path, write: bool) -> dict[str, Any]:
    receipt = validate_receipt(receipt)
    old_path = Path(receipt["old_state_snapshot"]["path"])
    state, raw = _read_json(old_path, "old state snapshot")
    if hashlib.sha256(raw).hexdigest() != receipt["old_state_snapshot"]["raw_sha256"]:
        raise MigrationError("old state snapshot drifted before state transformation")
    if canonical_sha256(state) != receipt["old_state_snapshot"]["canonical_sha256"]:
        raise MigrationError("old state canonical snapshot drifted before transformation")
    old_relative = receipt["old_disposition"]["relative_path"]
    if state.get("source_dispositions", {}).get(old_relative) != receipt["old_disposition"]["row_snapshot"]:
        raise MigrationError("original disposition snapshot drifted before transformation")
    transformed = deepcopy(state)
    ledger = transformed.get("source_disposition_migrations", [])
    if not isinstance(ledger, list):
        raise MigrationError("source_disposition_migrations ledger is malformed")
    entry = {
        "schema_version": TRANSFORMED_STATE_LEDGER_SCHEMA_VERSION,
        "receipt_canonical_sha256": receipt["canonical_integrity"]["canonical_json_sha256"],
        "old_disposition_relative": old_relative,
        "replacement": deepcopy(receipt["mapping"]["replacement"]),
        "old_disposition_snapshot": deepcopy(receipt["old_disposition"]["row_snapshot"]),
        "identity_proven": False,
        "source_substitution": True,
        "ordinary_rebind": False,
        "adapter_consumption": "NOT_CONSUMED_BY_ADAPTER",
        "publication_authority": None,
    }
    if ledger and entry not in ledger:
        raise MigrationError("different migration mapping conflicts with transformed state")
    if not ledger:
        transformed["source_disposition_migrations"] = [entry]
    if output_path.exists():
        existing, _ = _read_json(output_path, "existing transformed state")
        if canonical_bytes(existing) != canonical_bytes(transformed):
            raise MigrationError("different transformed state exists; refusing overwrite")
        return existing
    if write and not _write_create_only(output_path, transformed):
        raise MigrationError("existing transformed state differs; refusing overwrite")
    return transformed


def validate_snapshot(
    path: Path, *, expected_sha256: str, expected_canonical_sha256: str | None = None
) -> dict[str, str]:
    """Validate one immutable JSON snapshot without changing it."""

    expected_sha256 = _hex(expected_sha256, "expected snapshot SHA-256")
    state, raw = _read_json(path, "snapshot")
    actual_sha256 = hashlib.sha256(raw).hexdigest()
    if actual_sha256 != expected_sha256:
        raise MigrationError("snapshot SHA-256 drifted")
    actual_canonical = canonical_sha256(state)
    if expected_canonical_sha256 is not None and actual_canonical != _hex(
        expected_canonical_sha256, "expected canonical snapshot SHA-256"
    ):
        raise MigrationError("canonical snapshot SHA-256 drifted")
    return {"raw_sha256": actual_sha256, "canonical_sha256": actual_canonical}


def snapshot_diff(before: Path, after: Path) -> dict[str, Any]:
    before_state, before_raw = _read_json(before, "before snapshot")
    after_state, after_raw = _read_json(after, "after snapshot")
    return {
        "before_sha256": hashlib.sha256(before_raw).hexdigest(),
        "after_sha256": hashlib.sha256(after_raw).hexdigest(),
        "same_raw": before_raw == after_raw,
        "same_canonical": canonical_bytes(before_state) == canonical_bytes(after_state),
        "top_level_added": sorted(set(after_state) - set(before_state)),
        "top_level_removed": sorted(set(before_state) - set(after_state)),
        "top_level_changed": sorted(
            key for key in set(before_state) & set(after_state) if before_state[key] != after_state[key]
        ),
    }


def _load_request(args: argparse.Namespace) -> dict[str, Any]:
    request, _ = _read_json(Path(args.request), "migration request")
    if args.operator_authority:
        authority, _ = _read_json(Path(args.operator_authority), "operator authority JSON")
        request["operator_authority"] = authority
    return request


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    migrate_parser = sub.add_parser("migrate", help="validate explicit evidence and create receipt")
    migrate_parser.add_argument("--request", required=True)
    migrate_parser.add_argument("--record-root", required=True, type=Path)
    migrate_parser.add_argument("--receipt", type=Path)
    migrate_parser.add_argument("--operator-authority", type=Path)
    migrate_parser.add_argument("--write", action="store_true", help="write receipt; default is dry-run")
    transform_parser = sub.add_parser("transform", help="create inert separate migration ledger state")
    transform_parser.add_argument("--receipt", required=True, type=Path)
    transform_parser.add_argument("--output-state", required=True, type=Path)
    transform_parser.add_argument("--write", action="store_true", help="write state; default is dry-run")
    validate_parser = sub.add_parser("validate-snapshot", help="validate one JSON snapshot hash")
    validate_parser.add_argument("--snapshot", required=True, type=Path)
    validate_parser.add_argument("--sha256", required=True)
    validate_parser.add_argument("--canonical-sha256")
    diff_parser = sub.add_parser("snapshot-diff", help="deterministically compare two JSON snapshots")
    diff_parser.add_argument("--before", required=True, type=Path)
    diff_parser.add_argument("--after", required=True, type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "migrate":
            request = _load_request(args)
            result = migrate(request, record_root=args.record_root, receipt_path=args.receipt, write=args.write)
        elif args.command == "transform":
            receipt, _ = _read_json(args.receipt, "migration receipt")
            result = transform_state(receipt, output_path=args.output_state, write=args.write)
        elif args.command == "validate-snapshot":
            result = validate_snapshot(
                args.snapshot,
                expected_sha256=args.sha256,
                expected_canonical_sha256=args.canonical_sha256,
            )
        else:
            result = snapshot_diff(args.before, args.after)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
        return 0
    except MigrationError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
