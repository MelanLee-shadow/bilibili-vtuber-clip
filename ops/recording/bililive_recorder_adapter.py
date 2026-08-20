#!/usr/bin/env python3
"""Bridge official BililiveRecorder output into the autoslice source contract.

The recorder owns live capture and writes closed FLV/XML pairs.  This adapter
owns only the boundary to the existing autoslice system:

* publish a small, recorder-neutral, freshness-bound status document;
* remux closed FLV files to MP4 without deleting or overwriting source media;
* reconstruct Bilibili event JSONL from BililiveRecorder XML ``raw`` fields;
* publish MP4 only after validation, using a same-directory atomic rename.

It deliberately refuses to finalize anything while the room is streaming or
recording, and refuses to guess when the recorder API is unavailable.
"""

from __future__ import annotations

import argparse
import base64
import errno
import fcntl
from datetime import datetime, timedelta, timezone
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import signal
import stat
import subprocess
import sys
import threading
import time
from typing import Any, Iterable
import urllib.error
import urllib.request
import uuid
import xml.etree.ElementTree as ET


SCHEMA_VERSION = "bililive-recorder-adapter.v1"
STATUS_SCHEMA_VERSION = "recorder-neutral-status.v1"
STATE_SCHEMA_VERSION = "bililive-recorder-adapter-state.v1"
SOURCE_DISPOSITION_SCHEMA_VERSION = "recording-connection-stub.v1"
SOURCE_DISPOSITION_STATUS = "IGNORED_CONNECTION_STUB"
SOURCE_DISPOSITION_REASON = "RECORDER_CONNECTION_STUB_NO_DECODABLE_VIDEO"
SOURCE_DISPOSITION_REBIND_SCHEMA_VERSION = "recording-source-fuse-identity-rebind.v1"
SOURCE_DISPOSITION_REBIND_POLICY = "FUSE_REMOUNT_DEVICE_INODE_REBIND"
SOURCE_DISPOSITION_TIMESTAMP_REBIND_SCHEMA_VERSION = (
    "recording-source-fuse-timestamp-rebind.v1"
)
SOURCE_DISPOSITION_TIMESTAMP_REBIND_POLICY = "FUSE_SUCCESSOR_MTIME_CTIME_REATTESTATION"
SOURCE_DISPOSITION_REBIND_TASK_SCHEMA_VERSION = "recording-source-fuse-identity-rebind-task.v1"
SOURCE_DISPOSITION_REBIND_HASH_RESULT_SCHEMA_VERSION = (
    "recording-source-fuse-identity-rebind-hash-result.v1"
)
CONNECTION_STUB_BOOTSTRAP_RECEIPT_SCHEMA_VERSION = "recording-connection-stub-bootstrap.v1"
# A real 531 MiB CloudFS successor took more than 100 seconds to become fully
# readable during the 2026-08-13 remount repair.  The read runs in an isolated
# child, so keep the heartbeat responsive while giving a healthy cold-cache
# read a realistic bounded window.
SOURCE_DISPOSITION_REBIND_HASH_TIMEOUT_SECONDS = 300.0
SOURCE_DISPOSITION_REBIND_MAX_ATTEMPTS = 2
BACKEND = "BililiveRecorder"
QUALITY_PRIORITY = ("avc10000", "avc400", "avc250")
CONNECTION_STUB_MAX_SIZE_BYTES = 5 * 1024 * 1024
CONNECTION_STUB_MAX_DURATION_SECONDS, CONNECTION_STUB_MAX_SUCCESSOR_GAP_SECONDS = 10.0, 3.0
CONNECTION_STUB_MAX_XML_EVENT_COUNT = 1
FILENAME_RX_TEMPLATE = r"^{room}_(?P<stamp>20\d{{6}}-\d{{2}}-\d{{2}}-\d{{2}})\.flv$"
GRAPHQL_ROOM_QUERY = """
query AdapterRoomStatus($roomId: Int!) {
  room(roomId: $roomId) {
    objectId
    name
    title
    recording
    streaming
    danmakuConnected
    ioStats {
      streamHost
      networkBytesDownloaded
      networkMbps
      diskBytesWritten
      diskMBps
    }
    recordingStats {
      currentFileSize
      totalInputBytes
      totalOutputBytes
    }
  }
}
""".strip()
EVENT_COMMANDS = {
    "d": "DANMU_MSG",
    "sc": "SUPER_CHAT_MESSAGE",
    "gift": "SEND_GIFT",
    "guard": "GUARD_BUY",
}
WEBHOOK_EVENT_TYPES = {
    "SessionStarted",
    "SessionEnded",
    "FileOpening",
    "FileClosed",
    "StreamStarted",
    "StreamEnded",
}


class AdapterError(RuntimeError):
    """A fail-closed adapter error safe to surface in status/log output."""


class SourceDispositionIdentityRebindRequired(AdapterError):
    """The row is valid except for one proven typed FUSE metadata transition."""

    def __init__(
        self,
        *,
        validation: dict[str, Any],
        paths: dict[str, Path],
    ) -> None:
        super().__init__("source disposition FUSE identity rebind is required")
        self.validation = validation
        self.paths = paths


_IDENTITY_REBIND_CHILDREN: dict[int, subprocess.Popen[bytes]] = {}


def load_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return values
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.removeprefix("export ").split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def atomic_write_bytes(path: Path, payload: bytes, *, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    try:
        with tmp.open("wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


def atomic_write_json(path: Path, payload: dict[str, Any], *, mode: int = 0o644) -> None:
    encoded = (json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode(
        "utf-8"
    )
    atomic_write_bytes(path, encoded, mode=mode)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


_FILE_FINGERPRINT_KEYS = (
    "size_bytes",
    "mtime_ns",
    "ctime_ns",
    "device",
    "inode",
    "mode",
)
_FILE_STABLE_FINGERPRINT_KEYS = ("size_bytes", "mtime_ns", "ctime_ns", "mode")
_DISPOSITION_FILE_ROLES = ("source", "xml", "successor_source", "successor_mp4")
_TIMESTAMP_REBIND_ROLES = ("successor_source", "successor_mp4")
_TIMESTAMP_REBIND_FIELDS = ("mtime_ns", "ctime_ns")
_HISTORICAL_SHA_BASIS = "HISTORICAL_SHA256_MATCH"
_LEGACY_SUCCESSOR_SOURCE_BASIS = "LEGACY_NO_PRIOR_SHA256_WEBHOOK_FINALIZED_LEDGER_STABLE_STAT"


def _stat_fingerprint(info: os.stat_result) -> dict[str, int]:
    return {
        "size_bytes": info.st_size,
        "mtime_ns": info.st_mtime_ns,
        "ctime_ns": info.st_ctime_ns,
        "device": info.st_dev,
        "inode": info.st_ino,
        "mode": stat.S_IMODE(info.st_mode),
    }


def _regular_file_fingerprint(path: Path) -> dict[str, int]:
    try:
        info = path.lstat()
    except OSError as exc:
        raise AdapterError(f"cannot stat evidence file {path.name}: {exc}") from exc
    if not stat.S_ISREG(info.st_mode):
        raise AdapterError(f"evidence path is not a regular non-symlink file: {path}")
    return _stat_fingerprint(info)


def _attest_regular_file(path: Path) -> dict[str, Any]:
    """Hash one stable regular-file snapshot without following a leaf symlink."""

    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise AdapterError(f"cannot open evidence file {path.name}: {exc}") from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise AdapterError(f"evidence path is not a regular file: {path}")
        digest = hashlib.sha256()
        while block := os.read(descriptor, 1024 * 1024):
            digest.update(block)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    fingerprint = _stat_fingerprint(before)
    if fingerprint != _stat_fingerprint(after):
        raise AdapterError(f"evidence file changed while hashing: {path.name}")
    return {**fingerprint, "sha256": digest.hexdigest()}


def _binding_matches_fingerprint(binding: Any, path: Path) -> bool:
    if not isinstance(binding, dict):
        return False
    try:
        current = _regular_file_fingerprint(path)
    except AdapterError:
        return False
    return all(binding.get(key) == current[key] for key in _FILE_FINGERPRINT_KEYS)


def _decode_mountinfo_field(value: str) -> str:
    return re.sub(
        r"\\([0-7]{3})",
        lambda match: chr(int(match.group(1), 8)),
        value,
    )


def _mount_identity_for_path(path: Path) -> dict[str, Any] | None:
    """Return the owning Linux mount without invoking a process or touching bytes."""

    target = os.path.abspath(os.fspath(path))
    best: tuple[int, dict[str, Any]] | None = None
    try:
        lines = Path("/proc/self/mountinfo").read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for line in lines:
        fields = line.split()
        try:
            separator = fields.index("-")
            mount_point = _decode_mountinfo_field(fields[4])
            mount_id = int(fields[0])
            filesystem_type = fields[separator + 1]
            mount_source = _decode_mountinfo_field(fields[separator + 2])
        except (IndexError, ValueError):
            continue
        boundary = mount_point.rstrip("/") + "/"
        if target != mount_point and not target.startswith(boundary):
            continue
        identity = {
            "mount_id": mount_id,
            "major_minor": fields[2],
            "root": _decode_mountinfo_field(fields[3]),
            "mount_point": mount_point,
            "filesystem_type": filesystem_type,
            "mount_source": mount_source,
        }
        candidate = (len(mount_point), identity)
        if best is None or candidate[0] > best[0]:
            best = candidate
    return best[1] if best is not None else None


def _is_fuse_mount(identity: Any) -> bool:
    if not isinstance(identity, dict):
        return False
    filesystem_type = str(identity.get("filesystem_type") or "").lower()
    return filesystem_type == "fuse" or filesystem_type.startswith("fuse.")


def _portable_mount_identity(identity: Any) -> dict[str, Any] | None:
    """Project one FUSE identity across container mount namespaces."""

    if not _is_fuse_mount(identity):
        return None
    return {
        "major_minor": identity.get("major_minor"),
        "filesystem_type": identity.get("filesystem_type"),
        "mount_source": identity.get("mount_source"),
    }


def _shared_fuse_mount_identity(paths: Iterable[Path]) -> dict[str, Any] | None:
    identities = [_mount_identity_for_path(path) for path in paths]
    if not identities or not all(_is_fuse_mount(identity) for identity in identities):
        return None
    first = identities[0]
    if any(identity != first for identity in identities[1:]):
        return None
    return first


def _disposition_binding_projection(binding: dict[str, Any]) -> dict[str, Any]:
    projection = {"path": binding.get("path")}
    projection.update({key: binding.get(key) for key in _FILE_FINGERPRINT_KEYS})
    sha256 = binding.get("sha256")
    projection["sha256"] = sha256
    projection["verification_basis"] = (
        _HISTORICAL_SHA_BASIS if sha256 is not None else _LEGACY_SUCCESSOR_SOURCE_BASIS
    )
    return projection


def _changed_fingerprint_fields(
    previous: dict[str, dict[str, Any]],
    current: dict[str, dict[str, Any]],
) -> dict[str, list[str]]:
    return {
        role: [
            key
            for key in _FILE_FINGERPRINT_KEYS
            if previous[role].get(key) != current[role].get(key)
        ]
        for role in _DISPOSITION_FILE_ROLES
        if any(
            previous[role].get(key) != current[role].get(key)
            for key in _FILE_FINGERPRINT_KEYS
        )
    }


def _identity_rebind_legacy_contract() -> dict[str, Any]:
    return {
        "successor_source": {
            "verification_basis": _LEGACY_SUCCESSOR_SOURCE_BASIS,
            "historical_sha256_available": False,
            "strict_bindings": [
                "path",
                "size_bytes",
                "mtime_ns",
                "ctime_ns",
                "mode",
                "webhook_file_size",
                "finalized_ledger_source_size",
                "successor_mp4_historical_sha256",
            ],
        }
    }


def _timestamp_rebind_legacy_contract(
    row: dict[str, Any],
    previous: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    successor = previous["successor_source"]
    session = row.get("session") if isinstance(row.get("session"), dict) else {}
    webhook = (
        session.get("successor_webhook")
        if isinstance(session.get("successor_webhook"), dict)
        else {}
    )
    finalized = (
        session.get("successor_finalized_ledger")
        if isinstance(session.get("successor_finalized_ledger"), dict)
        else {}
    )
    return {
        "successor_source": {
            "verification_basis": _LEGACY_SUCCESSOR_SOURCE_BASIS,
            "historical_sha256_available": False,
            "historical_sha256": None,
            "path": successor.get("path"),
            "size_bytes": successor.get("size_bytes"),
            "mode": successor.get("mode"),
            "webhook_file_size": webhook.get("file_size"),
            "finalized_ledger_source_size": finalized.get("source_size"),
            "successor_mp4_historical_sha256": previous["successor_mp4"].get("sha256"),
        }
    }


def _receipt_sha256(receipt: dict[str, Any]) -> str:
    return str(receipt["canonical_integrity"]["canonical_json_sha256"])


def _validate_disposition_rebind_chain(
    *,
    row: dict[str, Any],
    bindings: dict[str, dict[str, Any]],
    identity_rebinds: Any,
) -> tuple[dict[str, dict[str, Any]], str | None, dict[str, Any] | None]:
    if identity_rebinds is None:
        receipts: list[Any] = []
    elif isinstance(identity_rebinds, list):
        receipts = identity_rebinds
    else:
        raise AdapterError("source disposition identity rebind ledger is malformed")
    previous = {
        role: _disposition_binding_projection(bindings[role]) for role in _DISPOSITION_FILE_ROLES
    }
    disposition_sha256 = str(
        (row.get("canonical_integrity") or {}).get("canonical_json_sha256") or ""
    )
    previous_receipt_sha256: str | None = None
    previous_mount: dict[str, Any] | None = None
    base_receipt_fields = {
        "schema_version",
        "policy",
        "source_disposition_canonical_sha256",
        "previous_receipt_canonical_sha256",
        "rebound_at",
        "current_mount",
        "previous_bindings",
        "current_bindings",
        "legacy_promotion",
        "canonical_integrity",
    }
    for receipt in receipts:
        if not isinstance(receipt, dict):
            raise AdapterError("source disposition identity rebind receipt is malformed")
        is_identity_rebind = (
            receipt.get("schema_version") == SOURCE_DISPOSITION_REBIND_SCHEMA_VERSION
            and receipt.get("policy") == SOURCE_DISPOSITION_REBIND_POLICY
        )
        is_timestamp_rebind = (
            receipt.get("schema_version") == SOURCE_DISPOSITION_TIMESTAMP_REBIND_SCHEMA_VERSION
            and receipt.get("policy") == SOURCE_DISPOSITION_TIMESTAMP_REBIND_POLICY
        )
        expected_fields = base_receipt_fields | (
            {"changed_fields"} if is_timestamp_rebind else set()
        )
        if (not is_identity_rebind and not is_timestamp_rebind) or set(receipt) != expected_fields:
            raise AdapterError("source disposition identity rebind receipt is malformed")
        integrity = receipt.get("canonical_integrity")
        unsigned = {key: value for key, value in receipt.items() if key != "canonical_integrity"}
        if not isinstance(integrity, dict) or integrity != {
            "algorithm": "sha256",
            "canonical_json_sha256": _canonical_json_sha256(unsigned),
        }:
            raise AdapterError("source disposition identity rebind integrity mismatch")
        current = receipt.get("current_bindings")
        if (
            receipt.get("source_disposition_canonical_sha256") != disposition_sha256
            or receipt.get("previous_receipt_canonical_sha256") != previous_receipt_sha256
            or receipt.get("previous_bindings") != previous
            or not _is_fuse_mount(receipt.get("current_mount"))
            or not isinstance(current, dict)
            or set(current) != set(_DISPOSITION_FILE_ROLES)
        ):
            raise AdapterError("source disposition identity rebind chain drifted")
        for role in _DISPOSITION_FILE_ROLES:
            binding = current.get(role)
            prior = previous[role]
            if (
                not isinstance(binding, dict)
                or set(binding) != {"path", "sha256", "verification_basis", *_FILE_FINGERPRINT_KEYS}
                or binding.get("path") != bindings[role].get("path")
                or binding.get("verification_basis") != prior.get("verification_basis")
                or (
                    prior.get("sha256") is not None
                    and re.fullmatch(r"[0-9a-f]{64}", str(binding.get("sha256") or "")) is None
                )
                or (prior.get("sha256") is None and binding.get("sha256") is not None)
                or (
                    prior.get("sha256") is not None and binding.get("sha256") != prior.get("sha256")
                )
            ):
                raise AdapterError("source disposition identity rebind binding drifted")
        changes = _changed_fingerprint_fields(previous, current)
        if is_identity_rebind:
            if (
                receipt.get("legacy_promotion") != _identity_rebind_legacy_contract()
                or not changes
                or any(
                    field not in {"device", "inode"}
                    for fields in changes.values()
                    for field in fields
                )
                or (
                    previous_mount is not None
                    and _portable_mount_identity(receipt.get("current_mount"))
                    == _portable_mount_identity(previous_mount)
                )
            ):
                raise AdapterError("source disposition identity rebind binding drifted")
        else:
            assert is_timestamp_rebind
            if (
                receipt.get("legacy_promotion") != _timestamp_rebind_legacy_contract(row, previous)
                or receipt.get("changed_fields") != changes
                or set(changes) != set(_TIMESTAMP_REBIND_ROLES)
                or any(
                    field not in _TIMESTAMP_REBIND_FIELDS
                    for fields in changes.values()
                    for field in fields
                )
                or (
                    previous_mount is not None
                    and _portable_mount_identity(receipt.get("current_mount"))
                    != _portable_mount_identity(previous_mount)
                )
            ):
                raise AdapterError("source disposition timestamp rebind binding drifted")
        previous = current
        previous_receipt_sha256 = _receipt_sha256(receipt)
        previous_mount = receipt["current_mount"]
    return previous, previous_receipt_sha256, previous_mount


def _prepare_disposition_identity_validation(
    *,
    row: dict[str, Any],
    bindings: dict[str, dict[str, Any]],
    paths: dict[str, Path],
    identity_rebinds: Any,
) -> dict[str, Any]:
    effective, previous_receipt_sha256, previous_mount = _validate_disposition_rebind_chain(
        row=row,
        bindings=bindings,
        identity_rebinds=identity_rebinds,
    )
    try:
        current = {role: _regular_file_fingerprint(paths[role]) for role in _DISPOSITION_FILE_ROLES}
    except AdapterError:
        raise
    exact_fingerprints = all(
        all(effective[role].get(key) == current[role][key] for key in _FILE_FINGERPRINT_KEYS)
        for role in _DISPOSITION_FILE_ROLES
    )
    has_receipts = isinstance(identity_rebinds, list) and bool(identity_rebinds)
    current_mount = _shared_fuse_mount_identity(paths.values()) if has_receipts else None
    if exact_fingerprints:
        if not has_receipts:
            return {"current": current, "pending_rebind": False}
        if current_mount is None:
            raise AdapterError(
                "source disposition FUSE rebind is no longer on one shared FUSE mount"
            )
        if _portable_mount_identity(current_mount) == _portable_mount_identity(previous_mount):
            # Docker assigns a namespace-local mount_id/mount_point on each
            # container restart. Exact file identity plus the portable FUSE
            # projection proves this is not another source remount.
            return {"current": current, "pending_rebind": False}
        raise AdapterError("source disposition FUSE mount changed without file identity drift")
    changes = _changed_fingerprint_fields(effective, current)
    timestamp_rebind = set(changes) == set(_TIMESTAMP_REBIND_ROLES) and all(
        fields and all(field in _TIMESTAMP_REBIND_FIELDS for field in fields)
        for fields in changes.values()
    )
    if timestamp_rebind:
        current_mount = current_mount or _shared_fuse_mount_identity(paths.values())
        if current_mount is None:
            raise AdapterError(
                "source disposition successor timestamp drifted outside one shared FUSE mount"
            )
        if has_receipts and _portable_mount_identity(current_mount) != _portable_mount_identity(
            previous_mount
        ):
            raise AdapterError("source disposition timestamp drift crossed FUSE mount identity")
        if not isinstance(identity_rebinds, list):
            raise AdapterError("source disposition FUSE rebind lacks a durable receipt ledger")
        return {
            "current": current,
            "effective": effective,
            "previous_receipt_sha256": previous_receipt_sha256,
            "previous_mount": previous_mount,
            "current_mount": current_mount,
            "changed_fields": changes,
            "rebind_schema_version": SOURCE_DISPOSITION_TIMESTAMP_REBIND_SCHEMA_VERSION,
            "rebind_policy": SOURCE_DISPOSITION_TIMESTAMP_REBIND_POLICY,
            "pending_rebind": True,
        }
    if any(
        effective[role].get(key) != current[role][key]
        for role in _DISPOSITION_FILE_ROLES
        for key in _FILE_STABLE_FINGERPRINT_KEYS
    ):
        raise AdapterError("source disposition non-identity fingerprint drifted")
    current_mount = current_mount or _shared_fuse_mount_identity(paths.values())
    if current_mount is None:
        raise AdapterError("source disposition device/inode drifted outside one shared FUSE mount")
    if has_receipts and _portable_mount_identity(current_mount) == _portable_mount_identity(
        previous_mount
    ):
        raise AdapterError("source disposition identity drifted within one FUSE mount epoch")
    if not isinstance(identity_rebinds, list):
        raise AdapterError("source disposition FUSE rebind lacks a durable receipt ledger")
    return {
        "current": current,
        "effective": effective,
        "previous_receipt_sha256": previous_receipt_sha256,
        "previous_mount": previous_mount,
        "current_mount": current_mount,
        "changed_fields": changes,
        "rebind_schema_version": SOURCE_DISPOSITION_REBIND_SCHEMA_VERSION,
        "rebind_policy": SOURCE_DISPOSITION_REBIND_POLICY,
        "pending_rebind": True,
    }


def _finish_disposition_identity_rebind(
    *,
    row: dict[str, Any],
    paths: dict[str, Path],
    validation: dict[str, Any],
    identity_rebinds: list[dict[str, Any]],
    attestations: dict[str, dict[str, Any]],
) -> None:
    if not validation.get("pending_rebind"):
        return
    expected_hash_roles = {
        role
        for role in _DISPOSITION_FILE_ROLES
        if validation["effective"][role].get("sha256") is not None
    }
    if set(attestations) != expected_hash_roles:
        raise AdapterError("source disposition legacy rebind attestation is malformed")
    if _shared_fuse_mount_identity(paths.values()) != validation["current_mount"]:
        raise AdapterError("source disposition FUSE mount changed during identity rebind")
    current_bindings: dict[str, dict[str, Any]] = {}
    for role in _DISPOSITION_FILE_ROLES:
        expected_sha256 = validation["effective"][role].get("sha256")
        if expected_sha256 is None:
            if role != "successor_source" or role in attestations:
                raise AdapterError("source disposition legacy rebind attestation is malformed")
            current = validation["current"][role]
        else:
            attestation = attestations.get(role)
            if not isinstance(attestation, dict) or set(attestation) != {
                "sha256",
                *_FILE_FINGERPRINT_KEYS,
            }:
                raise AdapterError("source disposition FUSE rebind attestation is malformed")
            if any(
                attestation.get(key) != validation["current"][role][key]
                for key in _FILE_FINGERPRINT_KEYS
            ):
                raise AdapterError("source disposition file changed during FUSE identity rebind")
            if attestation["sha256"] != expected_sha256:
                raise AdapterError("source disposition full-byte hash drifted during FUSE rebind")
            current = attestation
        current_bindings[role] = {
            "path": str(paths[role]),
            **{key: current[key] for key in _FILE_FINGERPRINT_KEYS},
            "sha256": expected_sha256,
            "verification_basis": validation["effective"][role]["verification_basis"],
        }
    observed_after = {
        role: _regular_file_fingerprint(paths[role]) for role in _DISPOSITION_FILE_ROLES
    }
    if observed_after != validation["current"]:
        raise AdapterError("source disposition file changed during FUSE identity rebind")
    if _shared_fuse_mount_identity(paths.values()) != validation["current_mount"]:
        raise AdapterError("source disposition FUSE mount changed during identity rebind")
    if _changed_fingerprint_fields(validation["effective"], current_bindings) != validation.get(
        "changed_fields"
    ):
        raise AdapterError("source disposition changed-field projection drifted during rebind")
    timestamp_rebind = (
        validation.get("rebind_schema_version")
        == SOURCE_DISPOSITION_TIMESTAMP_REBIND_SCHEMA_VERSION
        and validation.get("rebind_policy") == SOURCE_DISPOSITION_TIMESTAMP_REBIND_POLICY
    )
    receipt: dict[str, Any] = {
        "schema_version": validation["rebind_schema_version"],
        "policy": validation["rebind_policy"],
        "source_disposition_canonical_sha256": row["canonical_integrity"]["canonical_json_sha256"],
        "previous_receipt_canonical_sha256": validation["previous_receipt_sha256"],
        "rebound_at": datetime.now(timezone.utc).isoformat(),
        "current_mount": validation["current_mount"],
        "previous_bindings": validation["effective"],
        "current_bindings": current_bindings,
        "legacy_promotion": (
            _timestamp_rebind_legacy_contract(row, validation["effective"])
            if timestamp_rebind
            else _identity_rebind_legacy_contract()
        ),
    }
    if timestamp_rebind:
        receipt["changed_fields"] = validation["changed_fields"]
    receipt["canonical_integrity"] = {
        "algorithm": "sha256",
        "canonical_json_sha256": _canonical_json_sha256(receipt),
    }
    identity_rebinds.append(receipt)


def _local_json(path: Path, *, maximum_bytes: int = 256 * 1024) -> dict[str, Any]:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError:
        # Callers use absence as the normal "child is still working" state.
        # Preserve that distinction while wrapping every other open failure.
        raise
    except OSError as exc:
        raise AdapterError(f"cannot open local identity-rebind file: {path.name}") from exc
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.geteuid()
            or before.st_size > maximum_bytes
        ):
            raise AdapterError(f"invalid local identity-rebind file: {path.name}")
        chunks: list[bytes] = []
        size = 0
        while block := os.read(descriptor, min(64 * 1024, maximum_bytes + 1 - size)):
            chunks.append(block)
            size += len(block)
            if size > maximum_bytes:
                raise AdapterError(f"oversized local identity-rebind file: {path.name}")
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    stable_fields = (
        "st_dev",
        "st_ino",
        "st_mode",
        "st_uid",
        "st_size",
        "st_mtime_ns",
        "st_ctime_ns",
    )
    if size != before.st_size or any(
        getattr(before, field) != getattr(after, field) for field in stable_fields
    ):
        raise AdapterError(f"local identity-rebind file drifted: {path.name}")
    payload = b"".join(chunks)
    try:
        document = json.loads(payload)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise AdapterError(f"invalid local identity-rebind JSON: {path.name}") from exc
    if not isinstance(document, dict):
        raise AdapterError(f"invalid local identity-rebind document: {path.name}")
    return document


def _ensure_identity_rebind_spool(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():
        raise AdapterError("identity-rebind spool is not a private local directory")
    if stat.S_IMODE(info.st_mode) != 0o700:
        path.chmod(0o700)
    identity = _mount_identity_for_path(path)
    if sys.platform.startswith("linux") and identity is None:
        raise AdapterError("identity-rebind spool filesystem identity is unknown")
    if _is_fuse_mount(identity):
        raise AdapterError("identity-rebind spool must not be on FUSE")


def _identity_rebind_spool_key(relative: str) -> str:
    return hashlib.sha256(relative.encode("utf-8")).hexdigest()


def _identity_rebind_run_dir(spool_root: Path, relative: str) -> Path:
    return spool_root / _identity_rebind_spool_key(relative)


def _clear_identity_rebind_run_dir(run_dir: Path) -> None:
    if not run_dir.exists():
        return
    info = run_dir.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():
        raise AdapterError("identity-rebind run path is unsafe")
    allowed = {"request.json", "result.json", "result.tmp"}
    entries = list(run_dir.iterdir())
    if any(entry.name not in allowed or entry.is_symlink() or entry.is_dir() for entry in entries):
        raise AdapterError("identity-rebind run directory has unexpected content")
    for entry in entries:
        entry.unlink()
    run_dir.rmdir()


def _pid_start_ticks(pid: int) -> str | None:
    try:
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        tail = raw[raw.rindex(")") + 2 :].split()
        if tail[0] == "Z":
            return None
        return tail[19]
    except (OSError, IndexError, ValueError):
        return None


def _process_matches(pid: Any, start_ticks: Any) -> bool:
    try:
        parsed_pid = int(pid)
    except (TypeError, ValueError):
        return False
    observed = _pid_start_ticks(parsed_pid)
    return observed is not None and observed == str(start_ticks or "")


def _kill_identity_rebind_child(pid: Any, start_ticks: Any) -> None:
    try:
        parsed_pid = int(pid)
    except (TypeError, ValueError):
        return
    tracked = _IDENTITY_REBIND_CHILDREN.get(parsed_pid)
    if tracked is not None:
        if tracked.poll() is not None:
            _IDENTITY_REBIND_CHILDREN.pop(parsed_pid, None)
            return
    elif not _process_matches(parsed_pid, start_ticks):
        return
    try:
        os.killpg(parsed_pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def _identity_rebind_child_alive(task: dict[str, Any]) -> bool:
    try:
        pid = int(task.get("pid") or 0)
    except (TypeError, ValueError):
        return False
    child = _IDENTITY_REBIND_CHILDREN.get(pid)
    if child is not None:
        if child.poll() is None:
            return True
        _IDENTITY_REBIND_CHILDREN.pop(pid, None)
        return False
    return _process_matches(pid, task.get("pid_start_ticks"))


def _seal_identity_rebind_task(task: dict[str, Any]) -> None:
    task.pop("canonical_integrity", None)
    task["canonical_integrity"] = {
        "algorithm": "sha256",
        "canonical_json_sha256": _canonical_json_sha256(task),
    }


def _validate_identity_rebind_task(task: Any) -> dict[str, Any]:
    if not isinstance(task, dict):
        raise AdapterError("source disposition identity rebind task is malformed")
    integrity = task.get("canonical_integrity")
    unsigned = {key: value for key, value in task.items() if key != "canonical_integrity"}
    if not isinstance(integrity, dict) or integrity != {
        "algorithm": "sha256",
        "canonical_json_sha256": _canonical_json_sha256(unsigned),
    }:
        raise AdapterError("source disposition identity rebind task integrity mismatch")
    if task.get("schema_version") != SOURCE_DISPOSITION_REBIND_TASK_SCHEMA_VERSION:
        raise AdapterError("source disposition identity rebind task schema mismatch")
    return task


def _identity_rebind_task_base(
    *,
    relative: str,
    row: dict[str, Any],
    validation: dict[str, Any],
    paths: dict[str, Path],
    attempt: int,
) -> dict[str, Any]:
    return {
        "schema_version": SOURCE_DISPOSITION_REBIND_TASK_SCHEMA_VERSION,
        "source_relative_path": relative,
        "source_disposition_canonical_sha256": row["canonical_integrity"]["canonical_json_sha256"],
        "attempt": attempt,
        "detected_mount": validation["current_mount"],
        "expected_fingerprints": validation["current"],
        "paths": {role: str(paths[role]) for role in _DISPOSITION_FILE_ROLES},
        "hash_roles": [
            role
            for role in _DISPOSITION_FILE_ROLES
            if validation["effective"][role].get("sha256") is not None
        ],
        "legacy_successor_source_basis": _LEGACY_SUCCESSOR_SOURCE_BASIS,
    }


def _waiting_identity_rebind_task(
    *,
    relative: str,
    row: dict[str, Any],
    validation: dict[str, Any],
    paths: dict[str, Path],
) -> dict[str, Any]:
    task = {
        **_identity_rebind_task_base(
            relative=relative,
            row=row,
            validation=validation,
            paths=paths,
            attempt=0,
        ),
        "status": "WAITING_FOR_IDLE",
        "token": None,
        "pid": None,
        "pid_start_ticks": None,
        "started_at_epoch": None,
        "deadline_epoch": None,
        "last_error": None,
    }
    _seal_identity_rebind_task(task)
    return task


def _launch_identity_rebind_hash_task(
    *,
    relative: str,
    row: dict[str, Any],
    validation: dict[str, Any],
    paths: dict[str, Path],
    spool_root: Path,
    attempt: int,
) -> dict[str, Any]:
    _ensure_identity_rebind_spool(spool_root)
    run_dir = _identity_rebind_run_dir(spool_root, relative)
    _clear_identity_rebind_run_dir(run_dir)
    run_dir.mkdir(mode=0o700)
    token = uuid.uuid4().hex
    base = _identity_rebind_task_base(
        relative=relative,
        row=row,
        validation=validation,
        paths=paths,
        attempt=attempt,
    )
    request = {
        "schema_version": SOURCE_DISPOSITION_REBIND_HASH_RESULT_SCHEMA_VERSION,
        "token": token,
        "paths": base["paths"],
        "hash_roles": base["hash_roles"],
        "expected_fingerprints": base["expected_fingerprints"],
    }
    atomic_write_json(run_dir / "request.json", request, mode=0o600)
    try:
        process = subprocess.Popen(
            [
                sys.executable,
                str(Path(__file__).resolve()),
                "--identity-rebind-hash-child",
                str(run_dir / "request.json"),
                str(run_dir / "result.json"),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            start_new_session=True,
        )
    except OSError as exc:
        _clear_identity_rebind_run_dir(run_dir)
        raise AdapterError(f"cannot start source disposition identity rebind child: {exc}") from exc
    start_ticks = _pid_start_ticks(process.pid)
    if start_ticks is None and not sys.platform.startswith("linux"):
        start_ticks = f"tracked:{process.pid}"
    if start_ticks is None:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        raise AdapterError("cannot bind source disposition identity rebind child PID")
    _IDENTITY_REBIND_CHILDREN[process.pid] = process
    started = time.time()
    task = {
        **base,
        "status": "PENDING_HASH",
        "token": token,
        "pid": process.pid,
        "pid_start_ticks": start_ticks,
        "started_at_epoch": started,
        "deadline_epoch": started + SOURCE_DISPOSITION_REBIND_HASH_TIMEOUT_SECONDS,
        "last_error": None,
    }
    _seal_identity_rebind_task(task)
    return task


def _identity_rebind_hash_child(request_path: Path, result_path: Path) -> int:
    try:
        request = _local_json(request_path)
        token = str(request.get("token") or "")
        paths = request.get("paths")
        hash_roles = request.get("hash_roles")
        expected = request.get("expected_fingerprints")
        if (
            request.get("schema_version") != SOURCE_DISPOSITION_REBIND_HASH_RESULT_SCHEMA_VERSION
            or not token
            or not isinstance(paths, dict)
            or not isinstance(hash_roles, list)
            or not isinstance(expected, dict)
            or any(role not in _DISPOSITION_FILE_ROLES for role in hash_roles)
        ):
            raise AdapterError("identity-rebind hash request is malformed")
        attestations: dict[str, dict[str, Any]] = {}
        for role in hash_roles:
            path = Path(str(paths[role]))
            attestation = _attest_regular_file(path)
            if any(
                attestation.get(key) != expected[role].get(key) for key in _FILE_FINGERPRINT_KEYS
            ):
                raise AdapterError("identity-rebind source changed while hashing")
            attestations[role] = {
                **{key: attestation[key] for key in _FILE_FINGERPRINT_KEYS},
                "sha256": attestation["sha256"],
            }
        result = {
            "schema_version": SOURCE_DISPOSITION_REBIND_HASH_RESULT_SCHEMA_VERSION,
            "status": "OK",
            "token": token,
            "attestations": attestations,
        }
        return_code = 0
    except (AdapterError, OSError, KeyError, TypeError, ValueError) as exc:
        result = {
            "schema_version": SOURCE_DISPOSITION_REBIND_HASH_RESULT_SCHEMA_VERSION,
            "status": "ERROR",
            "token": str(locals().get("token") or ""),
            "error_type": type(exc).__name__,
            "error": str(exc)[:500],
        }
        return_code = 2
    atomic_write_json(result_path, result, mode=0o600)
    return return_code


def _load_identity_rebind_hash_result(
    *,
    relative: str,
    task: dict[str, Any],
    spool_root: Path,
) -> dict[str, dict[str, Any]] | None:
    result_path = _identity_rebind_run_dir(spool_root, relative) / "result.json"
    try:
        result = _local_json(result_path)
    except FileNotFoundError:
        return None
    if result.get(
        "schema_version"
    ) != SOURCE_DISPOSITION_REBIND_HASH_RESULT_SCHEMA_VERSION or result.get("token") != task.get(
        "token"
    ):
        raise AdapterError("source disposition identity rebind result binding mismatch")
    if result.get("status") != "OK":
        raise AdapterError(
            "source disposition identity rebind hash failed: "
            f"{str(result.get('error_type') or 'unknown')[:80]}"
        )
    attestations = result.get("attestations")
    if not isinstance(attestations, dict) or set(attestations) != set(task["hash_roles"]):
        raise AdapterError("source disposition identity rebind attestations are malformed")
    return attestations


def _advance_identity_rebind_task(
    *,
    relative: str,
    row: dict[str, Any],
    validation: dict[str, Any],
    paths: dict[str, Path],
    task_ledger: dict[str, Any],
    spool_root: Path,
    allow_start: bool,
) -> dict[str, dict[str, Any]]:
    task = task_ledger.get(relative)
    if task is None:
        if not allow_start:
            task_ledger[relative] = _waiting_identity_rebind_task(
                relative=relative,
                row=row,
                validation=validation,
                paths=paths,
            )
            raise AdapterError("source disposition identity rebind is waiting for recorder idle")
        task = _launch_identity_rebind_hash_task(
            relative=relative,
            row=row,
            validation=validation,
            paths=paths,
            spool_root=spool_root,
            attempt=1,
        )
        task_ledger[relative] = task
        raise AdapterError("source disposition identity rebind hash is pending")
    task = _validate_identity_rebind_task(task)
    expected_base = _identity_rebind_task_base(
        relative=relative,
        row=row,
        validation=validation,
        paths=paths,
        attempt=int(task.get("attempt") or 0),
    )
    if any(task.get(key) != value for key, value in expected_base.items() if key != "attempt"):
        _kill_identity_rebind_child(task.get("pid"), task.get("pid_start_ticks"))
        raise AdapterError("source disposition identity rebind task drifted from current mount")
    if task.get("status") == "WAITING_FOR_IDLE":
        if not allow_start:
            raise AdapterError("source disposition identity rebind is waiting for recorder idle")
        task = _launch_identity_rebind_hash_task(
            relative=relative,
            row=row,
            validation=validation,
            paths=paths,
            spool_root=spool_root,
            attempt=1,
        )
        task_ledger[relative] = task
        raise AdapterError("source disposition identity rebind hash is pending")
    child_alive = _identity_rebind_child_alive(task)
    attestations = _load_identity_rebind_hash_result(
        relative=relative,
        task=task,
        spool_root=spool_root,
    )
    if attestations is not None:
        if child_alive:
            raise AdapterError(
                "source disposition identity rebind result is sealed; child exit is pending"
            )
        return attestations
    if child_alive:
        if time.time() >= float(task.get("deadline_epoch") or 0):
            _kill_identity_rebind_child(task.get("pid"), task.get("pid_start_ticks"))
            task["status"] = "TIMEOUT_ORPHAN_PENDING"
            task["last_error"] = "IDENTITY_REBIND_HASH_TIMEOUT"
            _seal_identity_rebind_task(task)
        raise AdapterError(
            "source disposition identity rebind hash is pending"
            if task.get("status") == "PENDING_HASH"
            else "source disposition identity rebind timed-out child is still pending"
        )
    attempt = int(task.get("attempt") or 0)
    if not allow_start:
        task["status"] = "WAITING_FOR_IDLE"
        task["pid"] = None
        task["pid_start_ticks"] = None
        task["started_at_epoch"] = None
        task["deadline_epoch"] = None
        _seal_identity_rebind_task(task)
        raise AdapterError("source disposition identity rebind is waiting for recorder idle")
    if attempt >= SOURCE_DISPOSITION_REBIND_MAX_ATTEMPTS:
        task["status"] = "ERROR"
        task["last_error"] = "IDENTITY_REBIND_HASH_RETRY_EXHAUSTED"
        _seal_identity_rebind_task(task)
        raise AdapterError("source disposition identity rebind hash retry exhausted")
    run_dir = _identity_rebind_run_dir(spool_root, relative)
    _clear_identity_rebind_run_dir(run_dir)
    task = _launch_identity_rebind_hash_task(
        relative=relative,
        row=row,
        validation=validation,
        paths=paths,
        spool_root=spool_root,
        attempt=attempt + 1,
    )
    task_ledger[relative] = task
    raise AdapterError("source disposition identity rebind hash retry is pending")


def _canonical_json_sha256(payload: dict[str, Any]) -> str:
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _parse_webhook_datetime(value: Any, *, field: str) -> datetime:
    raw = _validate_webhook_timestamp(value, field=field)
    pattern = r"(?P<head>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?P<fraction>\.\d{1,7})?(?P<zone>Z|[+-]\d{2}:\d{2})"
    match = re.fullmatch(pattern, raw)
    if match is None:  # pragma: no cover - validation above owns this branch
        raise AdapterError(f"webhook {field} is invalid")
    fraction = match.group("fraction") or ""
    if fraction:
        fraction = "." + fraction[1:7].ljust(6, "0")
    zone = "+00:00" if match.group("zone") == "Z" else match.group("zone")
    return datetime.fromisoformat(f"{match.group('head')}{fraction}{zone}")


def _webhook_record_relative_path(relative_path: str, *, room_id: int) -> str:
    path = PurePosixPath(relative_path)
    parts = path.parts
    if path.is_absolute() or ".." in parts:
        raise AdapterError("webhook RelativePath is not a safe relative path")
    if len(parts) != 4 or parts[0:2] != ("Videos", str(room_id)):
        raise AdapterError("webhook RelativePath is outside the configured room root")
    date_name, file_name = parts[2], parts[3]
    if not re.fullmatch(r"20\d{2}-\d{2}-\d{2}", date_name):
        raise AdapterError("webhook RelativePath has an invalid date directory")
    filename_rx = re.compile(FILENAME_RX_TEMPLATE.format(room=re.escape(str(room_id))))
    if not filename_rx.fullmatch(file_name):
        raise AdapterError("webhook RelativePath has an unexpected recorder filename")
    return f"{date_name}/{file_name}"


def _validate_webhook_timestamp(value: Any, *, field: str) -> str:
    """Validate the seven-digit ISO timestamps emitted by .NET DateTimeOffset."""
    raw = str(value or "")
    match = re.fullmatch(
        r"(?P<head>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})"
        r"(?P<fraction>\.\d{1,7})?"
        r"(?P<zone>Z|[+-]\d{2}:\d{2})",
        raw,
    )
    if match is None:
        raise AdapterError(f"webhook {field} is invalid")
    fraction = match.group("fraction") or ""
    if fraction:
        fraction = "." + fraction[1:7].ljust(6, "0")
    zone = "+00:00" if match.group("zone") == "Z" else match.group("zone")
    try:
        datetime.fromisoformat(f"{match.group('head')}{fraction}{zone}")
    except ValueError as exc:
        raise AdapterError(f"webhook {field} is invalid") from exc
    return raw


def validate_webhook_event(payload: Any, *, room_id: int) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise AdapterError("webhook body must be a JSON object")
    event_type = str(payload.get("EventType") or "")
    if event_type not in WEBHOOK_EVENT_TYPES:
        raise AdapterError(f"unsupported webhook EventType: {event_type!r}")
    event_id = str(payload.get("EventId") or "")
    try:
        uuid.UUID(event_id)
    except (ValueError, AttributeError) as exc:
        raise AdapterError("webhook EventId is not a UUID") from exc
    event_timestamp = _validate_webhook_timestamp(
        payload.get("EventTimestamp"),
        field="EventTimestamp",
    )
    data = payload.get("EventData")
    if not isinstance(data, dict):
        raise AdapterError("webhook EventData must be an object")
    try:
        observed_room = int(data.get("RoomId"))
    except (TypeError, ValueError) as exc:
        raise AdapterError("webhook EventData.RoomId is invalid") from exc
    if observed_room != room_id:
        raise AdapterError("webhook event is for a different room")

    record_relative_path = None
    if event_type in {"FileOpening", "FileClosed"}:
        record_relative_path = _webhook_record_relative_path(
            str(data.get("RelativePath") or ""),
            room_id=room_id,
        )
        if not str(data.get("SessionId") or ""):
            raise AdapterError("file webhook lacks SessionId")
        _validate_webhook_timestamp(data.get("FileOpenTime"), field="FileOpenTime")
    if event_type == "FileClosed":
        try:
            file_size = int(data["FileSize"])
            duration = float(data["Duration"])
        except (KeyError, TypeError, ValueError) as exc:
            raise AdapterError("FileClosed has invalid size or duration") from exc
        if file_size <= 0 or duration <= 0:
            raise AdapterError("FileClosed has non-positive size or duration")
        _validate_webhook_timestamp(data.get("FileCloseTime"), field="FileCloseTime")

    return {
        "event_id": event_id,
        "event_type": event_type,
        "event_timestamp": event_timestamp,
        "event_data": data,
        "record_relative_path": record_relative_path,
    }


def append_webhook_event(
    journal_path: Path,
    payload: Any,
    *,
    room_id: int,
) -> None:
    validate_webhook_event(payload, room_id=room_id)
    encoded = (
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")
    if len(encoded) > 256 * 1024:
        raise AdapterError("webhook event exceeds 256 KiB")
    journal_path.parent.mkdir(parents=True, exist_ok=True)
    if journal_path.is_symlink():
        raise AdapterError("webhook journal must not be a symlink")
    fd = os.open(journal_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.chmod(journal_path, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        view = memoryview(encoded)
        while view:
            written = os.write(fd, view)
            view = view[written:]
        os.fsync(fd)
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def reconcile_webhook_journal(
    journal_path: Path,
    state: dict[str, Any],
    *,
    room_id: int,
) -> bool:
    try:
        stat = journal_path.stat()
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise AdapterError(f"webhook journal is unreadable: {exc}") from exc
    if journal_path.is_symlink() or not journal_path.is_file():
        raise AdapterError("webhook journal is not a regular file")
    if stat.st_size > 128 * 1024 * 1024:
        raise AdapterError("webhook journal exceeds 128 MiB; compact before continuing")

    seen = state.setdefault("webhook_event_ids", {})
    files = state.setdefault("webhook_files", {})
    if not isinstance(seen, dict) or not isinstance(files, dict):
        raise AdapterError("webhook ledger in adapter state is malformed")
    changed = False
    try:
        with journal_path.open("r", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_SH)
            try:
                lines = handle.read().splitlines()
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except (OSError, UnicodeError) as exc:
        raise AdapterError(f"webhook journal cannot be decoded: {exc}") from exc
    for line_number, line in enumerate(lines, start=1):
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as exc:
            raise AdapterError(f"webhook journal line {line_number} is invalid JSON") from exc
        normalized = validate_webhook_event(payload, room_id=room_id)
        digest = hashlib.sha256(
            json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        event_id = normalized["event_id"]
        previous = seen.get(event_id)
        if previous is not None:
            if not isinstance(previous, dict) or previous.get("sha256") != digest:
                raise AdapterError(f"webhook EventId payload conflict: {event_id}")
            continue

        event_type = normalized["event_type"]
        data = normalized["event_data"]
        record_relative = normalized["record_relative_path"]
        if event_type in {"FileOpening", "FileClosed"} and record_relative:
            row = files.setdefault(record_relative, {})
            if not isinstance(row, dict):
                raise AdapterError(f"webhook file ledger is malformed: {record_relative}")
            session_id = str(data.get("SessionId") or "")
            previous_session = str(row.get("session_id") or "")
            if previous_session and previous_session != session_id:
                raise AdapterError(f"recorder path reused by another session: {record_relative}")
            row["session_id"] = session_id
            row["file_open_time"] = str(data.get("FileOpenTime") or "")
            if event_type == "FileOpening":
                row["opening_event_id"] = event_id
                if row.get("status") != "CLOSED":
                    row["status"] = "OPEN"
            else:
                close_identity = {
                    "file_size": int(data["FileSize"]),
                    "duration": float(data["Duration"]),
                    "file_close_time": str(data.get("FileCloseTime") or ""),
                }
                if row.get("status") == "CLOSED":
                    for key, value in close_identity.items():
                        if row.get(key) != value:
                            raise AdapterError(
                                f"conflicting FileClosed evidence: {record_relative}"
                            )
                row.update(close_identity)
                row["closing_event_id"] = event_id
                row["status"] = "CLOSED"
        state["last_webhook_event"] = {
            "event_id": event_id,
            "event_type": event_type,
            "event_timestamp": normalized["event_timestamp"],
        }
        seen[event_id] = {"sha256": digest, "event_type": event_type}
        changed = True
    return changed


def _basic_auth_header(username: str, password: str) -> str:
    encoded = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
    return f"Basic {encoded}"


def query_room_status(
    endpoint: str,
    room_id: int,
    *,
    username: str = "",
    password: str = "",
    timeout_seconds: float = 15.0,
) -> dict[str, Any]:
    body = json.dumps(
        {"query": GRAPHQL_ROOM_QUERY, "variables": {"roomId": room_id}},
        separators=(",", ":"),
    ).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if username or password:
        headers["Authorization"] = _basic_auth_header(username, password)
    request = urllib.request.Request(endpoint, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            payload = json.loads(response.read().decode("utf-8", "replace"))
    except (OSError, urllib.error.URLError, ValueError) as exc:
        raise AdapterError(f"recorder GraphQL unavailable: {type(exc).__name__}: {exc}") from exc
    errors = payload.get("errors") if isinstance(payload, dict) else None
    if errors:
        first = errors[0] if isinstance(errors, list) and errors else errors
        raise AdapterError(f"recorder GraphQL error: {str(first)[:300]}")
    room = (payload.get("data") or {}).get("room") if isinstance(payload, dict) else None
    if not isinstance(room, dict):
        raise AdapterError(f"room {room_id} is not configured in BililiveRecorder")
    return room


def probe_cookie_health(
    config_path: Path,
    state: dict[str, Any],
    *,
    now_epoch: float,
    refresh_seconds: int = 6 * 60 * 60,
    timeout_seconds: float = 20.0,
) -> tuple[dict[str, Any], bool]:
    """Check login without ever returning or logging the Cookie itself."""

    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
        cookie = str(
            ((((config.get("global") or {}).get("Cookie") or {}).get("Value")) or "")
        ).strip()
    except (OSError, ValueError, AttributeError):
        cookie = ""
    cookie_sha256 = hashlib.sha256(cookie.encode("utf-8")).hexdigest() if cookie else ""
    cached = state.get("cookie_health")
    if isinstance(cached, dict):
        try:
            fresh = now_epoch - float(cached["checked_at_epoch"]) < refresh_seconds
        except (KeyError, TypeError, ValueError):
            fresh = False
        if fresh and cached.get("cookie_sha256") == cookie_sha256:
            return dict(cached), False
    result: dict[str, Any] = {
        "configured": bool(cookie),
        "login_valid": False if not cookie else None,
        "checked_at_epoch": now_epoch,
        "checked_at": datetime.fromtimestamp(now_epoch, tz=timezone.utc).isoformat(),
        "cookie_sha256": cookie_sha256,
        "api_code": None,
        "error": None,
    }
    if cookie:
        request = urllib.request.Request(
            "https://api.bilibili.com/x/web-interface/nav",
            headers={"Cookie": cookie, "User-Agent": "Mozilla/5.0"},
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
                payload = json.loads(response.read().decode("utf-8", "replace"))
            data = payload.get("data") if isinstance(payload, dict) else None
            result["api_code"] = payload.get("code") if isinstance(payload, dict) else None
            result["login_valid"] = bool(isinstance(data, dict) and data.get("isLogin") is True)
        except (OSError, urllib.error.URLError, ValueError) as exc:
            result["error"] = f"{type(exc).__name__}: {str(exc)[:200]}"
    state["cookie_health"] = result
    return result, True


def _run(
    command: list[str],
    *,
    timeout_seconds: int,
) -> subprocess.CompletedProcess[str]:
    try:
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AdapterError(
            f"command failed to run: {command[0]}: {type(exc).__name__}: {exc}"
        ) from exc
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()[-1200:]
        raise AdapterError(f"{command[0]} exited {completed.returncode}: {detail}")
    return completed


def probe_media(path: Path, *, ffprobe_bin: str = "ffprobe") -> dict[str, Any]:
    completed = _run(
        [
            ffprobe_bin,
            "-v",
            "error",
            "-show_entries",
            "format=duration,size:stream=index,codec_type,codec_name,width,height",
            "-of",
            "json",
            str(path),
        ],
        timeout_seconds=120,
    )
    try:
        payload = json.loads(completed.stdout)
        streams = payload.get("streams") or []
        media_format = payload.get("format") or {}
        duration = float(media_format.get("duration") or 0)
        size = int(media_format.get("size") or path.stat().st_size)
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise AdapterError(f"invalid ffprobe result for {path.name}: {exc}") from exc
    video = next((row for row in streams if row.get("codec_type") == "video"), None)
    audio = next((row for row in streams if row.get("codec_type") == "audio"), None)
    if not isinstance(video, dict) or not isinstance(audio, dict):
        raise AdapterError(f"{path.name} must contain both video and audio streams")
    if duration <= 0 or size <= 0:
        raise AdapterError(f"{path.name} has invalid duration/size: {duration}/{size}")
    return {
        "duration_seconds": duration,
        "size_bytes": size,
        "video_codec": video.get("codec_name"),
        "audio_codec": audio.get("codec_name"),
        "width": int(video.get("width") or 0),
        "height": int(video.get("height") or 0),
    }


def probe_stream_shape(path: Path, *, ffprobe_bin: str = "ffprobe") -> dict[str, Any]:
    """Read only the opening seconds of an active FLV for objective quality telemetry."""

    completed = _run(
        [
            ffprobe_bin,
            "-v",
            "error",
            "-read_intervals",
            "%+2",
            "-show_entries",
            "stream=codec_type,codec_name,width,height",
            "-of",
            "json",
            str(path),
        ],
        timeout_seconds=30,
    )
    try:
        streams = (json.loads(completed.stdout) or {}).get("streams") or []
    except (TypeError, json.JSONDecodeError) as exc:
        raise AdapterError(f"invalid active-source ffprobe result for {path.name}: {exc}") from exc
    video = next((row for row in streams if row.get("codec_type") == "video"), None)
    audio = next((row for row in streams if row.get("codec_type") == "audio"), None)
    if not isinstance(video, dict):
        raise AdapterError(f"{path.name} active probe found no video stream")
    return {
        "video_codec": video.get("codec_name"),
        "audio_codec": audio.get("codec_name") if isinstance(audio, dict) else None,
        "width": int(video.get("width") or 0),
        "height": int(video.get("height") or 0),
    }


def packet_scan(path: Path, *, ffmpeg_bin: str = "ffmpeg") -> None:
    _run(
        [
            ffmpeg_bin,
            "-nostdin",
            "-v",
            "error",
            "-xerror",
            "-i",
            str(path),
            "-map",
            "0:v:0",
            "-map",
            "0:a:0",
            "-c",
            "copy",
            "-f",
            "null",
            "-",
        ],
        timeout_seconds=6 * 60 * 60,
    )


def _video_probe_json(path: Path, arguments: list[str], *, ffprobe_bin: str) -> dict[str, Any]:
    completed = _run(
        [
            ffprobe_bin,
            "-v",
            "error",
            "-select_streams",
            "v:0",
            *arguments,
            "-of",
            "json",
            str(path),
        ],
        timeout_seconds=120,
    )
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise AdapterError(f"invalid video probe JSON for {path.name}") from exc
    if not isinstance(payload, dict):
        raise AdapterError(f"invalid video probe payload for {path.name}")
    return payload


def probe_connection_stub_video(
    path: Path,
    *,
    ffprobe_bin: str = "ffprobe",
) -> dict[str, Any]:
    """Prove that a tiny FLV advertises H.264 but contains no video payload.

    ``-count_frames`` alone is not sufficient for the incident shape: ffprobe
    omits the count fields when the H.264 stream has no usable dimensions.
    Explicit ``-show_frames`` and ``-show_packets`` scans make absence an
    objective empty-list observation instead of treating a missing counter as
    zero by assumption.
    """
    shape_payload = _video_probe_json(
        path,
        ["-show_entries", "format=duration,size:stream=codec_type,codec_name,width,height"],
        ffprobe_bin=ffprobe_bin,
    )
    frame_payload = _video_probe_json(
        path,
        ["-show_frames", "-show_entries", "frame=media_type"],
        ffprobe_bin=ffprobe_bin,
    )
    packet_payload = _video_probe_json(
        path,
        ["-show_packets", "-show_entries", "packet=codec_type"],
        ffprobe_bin=ffprobe_bin,
    )
    try:
        streams = shape_payload.get("streams") or []
        media_format = shape_payload.get("format") or {}
        video = next(row for row in streams if row.get("codec_type") == "video")
        duration = float(media_format.get("duration") or 0)
        size = int(media_format.get("size") or path.stat().st_size)
        decoded_frames = frame_payload.get("frames") or []
        video_packets = packet_payload.get("packets") or []
    except (OSError, StopIteration, TypeError, ValueError) as exc:
        raise AdapterError(f"invalid connection-stub probe result for {path.name}: {exc}") from exc
    if not isinstance(decoded_frames, list) or not isinstance(video_packets, list):
        raise AdapterError(f"invalid frame/packet probe result for {path.name}")
    return {
        "duration_seconds": duration,
        "size_bytes": size,
        "video_codec": str(video.get("codec_name") or ""),
        "width": int(video.get("width") or 0),
        "height": int(video.get("height") or 0),
        "decoded_video_frames": len(decoded_frames),
        "video_packets": len(video_packets),
    }


def _xml_record_info(root: ET.Element) -> dict[str, str]:
    node = root.find("BililiveRecorderRecordInfo")
    if node is None:
        return {}
    return {str(key): str(value) for key, value in node.attrib.items()}


def _start_epoch_seconds(record_info: dict[str, str]) -> float | None:
    raw = record_info.get("start_time")
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.timestamp()


def xml_to_jsonl(xml_path: Path) -> tuple[bytes, dict[str, str], int]:
    try:
        root = ET.parse(xml_path).getroot()
    except (OSError, ET.ParseError) as exc:
        raise AdapterError(f"invalid BililiveRecorder XML {xml_path.name}: {exc}") from exc
    record_info = _xml_record_info(root)
    if root.find("BililiveRecorder") is None or not record_info:
        raise AdapterError(f"{xml_path.name} is not an official BililiveRecorder XML sidecar")
    lines: list[str] = []
    for element in root:
        command = EVENT_COMMANDS.get(element.tag)
        if command is None:
            continue
        raw_value = element.attrib.get("raw")
        if not raw_value:
            raise AdapterError(f"{xml_path.name} event <{element.tag}> lacks required raw evidence")
        try:
            raw: Any = json.loads(raw_value)
        except json.JSONDecodeError as exc:
            raise AdapterError(
                f"{xml_path.name} event <{element.tag}> has invalid raw JSON"
            ) from exc
        if element.tag == "d" and isinstance(raw, list):
            payload = {"cmd": command, "info": raw}
        elif element.tag != "d" and isinstance(raw, dict):
            payload = {"cmd": command, "data": raw}
        else:
            expected = "array" if element.tag == "d" else "object"
            raise AdapterError(
                f"{xml_path.name} event <{element.tag}> raw must be a JSON {expected}"
            )
        lines.append(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    data = (("\n".join(lines) + "\n") if lines else "").encode("utf-8")
    return data, record_info, len(lines)


def _fsync_directory(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        try:
            os.fsync(fd)
        except OSError as exc:
            if exc.errno not in {errno.EINVAL, errno.ENOTSUP, errno.EROFS}:
                raise
    finally:
        os.close(fd)


def _publish_path_noreplace(staged: Path, target: Path) -> str:
    """Atomically expose staged bytes without silently replacing target.

    CloudDrive's FUSE mount rejects hard links and RENAME_NOREPLACE.  Prefer a
    hard-link publish where supported; otherwise serialize compliant writers
    with an exclusive reservation directory and use same-directory rename.
    """

    try:
        os.link(staged, target)
        staged.unlink()
        _fsync_directory(target.parent)
        return "hardlink_noreplace"
    except FileExistsError as exc:
        raise AdapterError(f"target conflict; refusing overwrite: {target.name}") from exc
    except OSError as exc:
        if exc.errno not in {
            errno.EPERM,
            errno.EACCES,
            errno.EOPNOTSUPP,
            errno.ENOTSUP,
            errno.EXDEV,
            errno.EINVAL,
        }:
            raise AdapterError(f"atomic publish failed for {target.name}: {exc}") from exc

    reservation = target.with_name(f".{target.name}.publish-reservation")
    try:
        reservation.mkdir(mode=0o700)
    except FileExistsError as exc:
        raise AdapterError(f"publish reservation conflict: {target.name}") from exc
    try:
        if target.exists():
            raise AdapterError(f"target conflict; refusing overwrite: {target.name}")
        os.rename(staged, target)
        _fsync_directory(target.parent)
        return "reserved_atomic_rename"
    finally:
        try:
            reservation.rmdir()
        except FileNotFoundError:
            pass


def _write_if_absent_or_equal(path: Path, payload: bytes, *, mode: int = 0o644) -> None:
    if path.exists():
        try:
            current = path.read_bytes()
        except OSError as exc:
            raise AdapterError(f"cannot read existing sidecar {path.name}: {exc}") from exc
        if current != payload:
            raise AdapterError(f"refusing to overwrite non-matching existing sidecar {path.name}")
        return
    staging = path.parent / ".brec-adapter-staging"
    staging.mkdir(mode=0o700, exist_ok=True)
    tmp = staging / f"{uuid.uuid4().hex}.{path.name}"
    try:
        with tmp.open("xb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, mode)
        _publish_path_noreplace(tmp, path)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


def _meta_payload(
    source_flv: Path,
    xml_path: Path,
    record_info: dict[str, str],
    *,
    event_count: int,
) -> dict[str, Any]:
    source_stat = source_flv.stat()
    return {
        "schema_version": SCHEMA_VERSION,
        "description": {"RecordStartTime": record_info.get("start_time")},
        "segment_start_time": record_info.get("start_time"),
        "event_evidence": "bililiverecorder_xml_raw_subset",
        "recorder": {
            "backend": BACKEND,
            "room_id": record_info.get("roomid"),
            "room_name": record_info.get("name"),
            "title": record_info.get("title"),
            "event_count": event_count,
        },
        "source": {
            "flv": source_flv.name,
            "xml": xml_path.name,
            "size_bytes": source_stat.st_size,
            "mtime_ns": source_stat.st_mtime_ns,
            "delete_source": "never",
        },
    }


def _json_bytes(payload: dict[str, Any]) -> bytes:
    return (json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode(
        "utf-8"
    )


def finalize_recording(
    source_flv: Path,
    *,
    ffmpeg_bin: str = "ffmpeg",
    ffprobe_bin: str = "ffprobe",
    existing_ledger: dict[str, Any] | None = None,
) -> dict[str, Any]:
    xml_path = source_flv.with_suffix(".xml")
    if not xml_path.is_file():
        raise AdapterError(f"closed recording lacks XML sidecar: {source_flv.name}")
    jsonl_bytes, record_info, event_count = xml_to_jsonl(xml_path)
    target_mp4 = source_flv.with_suffix(".mp4")
    staging_dir = target_mp4.parent / ".brec-adapter-staging"
    staging_dir.mkdir(mode=0o700, exist_ok=True)
    tmp_mp4 = staging_dir / f"{uuid.uuid4().hex}.mp4"
    jsonl_path = source_flv.with_suffix(".jsonl")
    meta_path = source_flv.with_suffix(".meta.json")

    if target_mp4.exists():
        source_stat = source_flv.stat()
        if not isinstance(existing_ledger, dict):
            raise AdapterError(
                f"target exists without adapter ledger; refusing implicit success: {target_mp4.name}"
            )
        if (
            int(existing_ledger.get("source_size") or -1) != source_stat.st_size
            or int(existing_ledger.get("source_mtime_ns") or -1) != source_stat.st_mtime_ns
        ):
            raise AdapterError(f"source changed after finalization: {source_flv.name}")
        expected_sha = str(existing_ledger.get("target_sha256") or "")
        if not expected_sha or sha256_file(target_mp4) != expected_sha:
            raise AdapterError(f"target hash conflicts with adapter ledger: {target_mp4.name}")
        media = probe_media(target_mp4, ffprobe_bin=ffprobe_bin)
        _write_if_absent_or_equal(jsonl_path, jsonl_bytes)
        _write_if_absent_or_equal(
            meta_path,
            _json_bytes(
                _meta_payload(
                    source_flv,
                    xml_path,
                    record_info,
                    event_count=event_count,
                )
            ),
        )
        return {
            "source": str(source_flv),
            "target": str(target_mp4),
            "status": "already_finalized",
            "event_count": event_count,
            "media": media,
            "target_sha256": expected_sha,
            "publish_method": "existing_ledger_verified",
        }

    try:
        try:
            tmp_mp4.unlink()
        except FileNotFoundError:
            pass
        _run(
            [
                ffmpeg_bin,
                "-nostdin",
                "-hide_banner",
                "-n",
                "-loglevel",
                "warning",
                "-fflags",
                "+genpts",
                "-i",
                str(source_flv),
                "-map",
                "0:v:0",
                "-map",
                "0:a:0",
                "-c",
                "copy",
                "-avoid_negative_ts",
                "make_zero",
                "-movflags",
                "+faststart",
                "-f",
                "mp4",
                str(tmp_mp4),
            ],
            timeout_seconds=6 * 60 * 60,
        )
        media = probe_media(tmp_mp4, ffprobe_bin=ffprobe_bin)
        packet_scan(tmp_mp4, ffmpeg_bin=ffmpeg_bin)
        with tmp_mp4.open("rb") as handle:
            os.fsync(handle.fileno())
        target_sha256 = sha256_file(tmp_mp4)
        _write_if_absent_or_equal(jsonl_path, jsonl_bytes)
        _write_if_absent_or_equal(
            meta_path,
            _json_bytes(
                _meta_payload(
                    source_flv,
                    xml_path,
                    record_info,
                    event_count=event_count,
                )
            ),
        )
        publish_method = _publish_path_noreplace(tmp_mp4, target_mp4)
    finally:
        try:
            tmp_mp4.unlink()
        except FileNotFoundError:
            pass
    return {
        "source": str(source_flv),
        "target": str(target_mp4),
        "status": "finalized",
        "event_count": event_count,
        "media": media,
        "target_sha256": target_sha256,
        "publish_method": publish_method,
    }


def _webhook_file_binding(evidence: dict[str, Any]) -> dict[str, Any]:
    return {
        key: evidence.get(key)
        for key in (
            "status",
            "session_id",
            "opening_event_id",
            "closing_event_id",
            "file_open_time",
            "file_close_time",
            "file_size",
            "duration",
        )
    }


def build_connection_stub_disposition(
    source_flv: Path,
    *,
    record_root: Path,
    webhook_files: dict[str, Any],
    finalized: dict[str, Any],
    ffprobe_bin: str = "ffprobe",
) -> dict[str, Any] | None:
    """Return one fully bound typed ignore row, or ``None`` for normal media.

    The classifier is intentionally narrow.  A file which misses any positive
    predicate remains in the ordinary finalization lane; it is never ignored
    merely because remuxing failed.
    """

    try:
        relative = source_flv.relative_to(record_root).as_posix()
        source_fingerprint = _regular_file_fingerprint(source_flv)
    except (AdapterError, ValueError):
        return None
    if relative in finalized or source_flv.with_suffix(".mp4").exists():
        return None
    evidence = webhook_files.get(relative)
    if not isinstance(evidence, dict) or evidence.get("status") != "CLOSED":
        return None
    try:
        closed_size = int(evidence.get("file_size") or -1)
        event_duration = float(evidence.get("duration") or 0)
        opened_at = _parse_webhook_datetime(evidence.get("file_open_time"), field="FileOpenTime")
        closed_at = _parse_webhook_datetime(evidence.get("file_close_time"), field="FileCloseTime")
    except (AdapterError, TypeError, ValueError):
        return None
    wall_duration = (closed_at - opened_at).total_seconds()
    if (
        source_fingerprint["size_bytes"] != closed_size
        or source_fingerprint["size_bytes"] >= CONNECTION_STUB_MAX_SIZE_BYTES
        or event_duration <= 0
        or event_duration >= CONNECTION_STUB_MAX_DURATION_SECONDS
        or wall_duration < 0
        or wall_duration >= CONNECTION_STUB_MAX_DURATION_SECONDS
        or not evidence.get("opening_event_id")
        or not evidence.get("closing_event_id")
    ):
        return None
    session_id = str(evidence.get("session_id") or "")
    if not session_id:
        return None

    openings: list[tuple[datetime, str, dict[str, Any]]] = []
    for candidate_relative, candidate_evidence in webhook_files.items():
        if not isinstance(candidate_evidence, dict) or not candidate_evidence.get(
            "opening_event_id"
        ):
            continue
        try:
            candidate_opened = _parse_webhook_datetime(
                candidate_evidence.get("file_open_time"), field="FileOpenTime"
            )
        except AdapterError:
            return None
        openings.append((candidate_opened, str(candidate_relative), candidate_evidence))
    openings.sort(key=lambda item: (item[0], item[1]))
    source_positions = [index for index, item in enumerate(openings) if item[1] == relative]
    if len(source_positions) != 1:
        return None
    source_position = source_positions[0]
    if any(
        str(item[2].get("session_id") or "") == session_id for item in openings[:source_position]
    ) or source_position + 1 >= len(openings):
        return None
    successor_opened, successor_relative, successor_evidence = openings[source_position + 1]
    successor_gap = (successor_opened - closed_at).total_seconds()
    if (
        successor_gap < 0
        or successor_gap > CONNECTION_STUB_MAX_SUCCESSOR_GAP_SECONDS
        or successor_evidence.get("status") != "CLOSED"
        or str(successor_evidence.get("session_id") or "") != session_id
        or not successor_evidence.get("closing_event_id")
    ):
        return None
    successor_path = PurePosixPath(successor_relative)
    source_path = PurePosixPath(relative)
    if (
        successor_path.is_absolute()
        or ".." in successor_path.parts
        or len(successor_path.parts) != 2
        or successor_path.parts[0] != source_path.parts[0]
        or re.fullmatch(
            FILENAME_RX_TEMPLATE.format(room=re.escape(source_flv.name.split("_", 1)[0])),
            successor_path.parts[1],
        )
        is None
    ):
        return None

    xml_path = source_flv.with_suffix(".xml")
    try:
        source_attestation = _attest_regular_file(source_flv)
        xml_attestation = _attest_regular_file(xml_path)
        _jsonl, record_info, event_count = xml_to_jsonl(xml_path)
        source_probe = probe_connection_stub_video(source_flv, ffprobe_bin=ffprobe_bin)
    except (AdapterError, OSError):
        return None
    if (
        not isinstance(event_count, int)
        or isinstance(event_count, bool)
        or not 0 <= event_count <= CONNECTION_STUB_MAX_XML_EVENT_COUNT
        or record_info.get("roomid") != source_flv.name.split("_", 1)[0]
        or source_probe.get("video_codec") != "h264"
        or source_probe.get("width") != 0
        or source_probe.get("height") != 0
        or source_probe.get("decoded_video_frames") != 0
        or source_probe.get("video_packets") != 0
        or int(source_probe.get("size_bytes") or -1) != source_fingerprint["size_bytes"]
        or float(source_probe.get("duration_seconds") or 0) <= 0
        or float(source_probe.get("duration_seconds") or 0) >= CONNECTION_STUB_MAX_DURATION_SECONDS
        or abs(float(source_probe["duration_seconds"]) - event_duration) > 0.001
        or not _binding_matches_fingerprint(source_attestation, source_flv)
        or not _binding_matches_fingerprint(xml_attestation, xml_path)
    ):
        return None

    successor_source = record_root / successor_relative
    successor_ledger = finalized.get(successor_relative)
    if not isinstance(successor_ledger, dict):
        return None
    successor_target = successor_source.with_suffix(".mp4")
    try:
        successor_fingerprint = _regular_file_fingerprint(successor_source)
        successor_target_attestation = _attest_regular_file(successor_target)
        successor_event_size = int(successor_evidence.get("file_size") or -1)
        ledger_source_size = int(successor_ledger.get("source_size") or -1)
        ledger_source_mtime_ns = int(successor_ledger.get("source_mtime_ns") or -1)
    except (AdapterError, OSError, TypeError, ValueError):
        return None
    expected_target = str(successor_ledger.get("target") or "")
    expected_target_sha256 = str(successor_ledger.get("target_sha256") or "")
    if (
        successor_fingerprint["size_bytes"] != successor_event_size
        or successor_fingerprint["size_bytes"] != ledger_source_size
        or expected_target != str(successor_target)
        or not expected_target_sha256
        or successor_target_attestation["sha256"] != expected_target_sha256
    ):
        return None
    try:
        successor_media = probe_media(successor_target, ffprobe_bin=ffprobe_bin)
    except AdapterError:
        return None
    if (
        not _binding_matches_fingerprint(successor_target_attestation, successor_target)
        or int(successor_media.get("width") or 0) <= 0
        or int(successor_media.get("height") or 0) <= 0
    ):
        return None

    row: dict[str, Any] = {
        "schema_version": SOURCE_DISPOSITION_SCHEMA_VERSION,
        "status": SOURCE_DISPOSITION_STATUS,
        "reason_code": SOURCE_DISPOSITION_REASON,
        "source_relative_path": relative,
        "source": {
            "path": str(source_flv),
            **source_attestation,
        },
        "xml": {
            "path": str(xml_path),
            **xml_attestation,
            "official_bililiverecorder": True,
            "event_count": event_count,
            "record_info": record_info,
        },
        "webhook": _webhook_file_binding(evidence),
        "decode": source_probe,
        "session": {
            "session_id": session_id,
            "prior_same_session_openings": 0,
            "successor_gap_seconds": successor_gap,
            "successor_relative_path": successor_relative,
            "successor_webhook": _webhook_file_binding(successor_evidence),
            "successor_source": {
                "path": str(successor_source),
                **successor_fingerprint,
            },
            "successor_finalized_ledger": {
                "source_size": ledger_source_size,
                "source_mtime_ns": ledger_source_mtime_ns,
                "target": expected_target,
                "target_sha256": expected_target_sha256,
                "finalized_at": successor_ledger.get("finalized_at"),
            },
            "successor_mp4": {
                "path": str(successor_target),
                **successor_target_attestation,
                "media": successor_media,
            },
        },
        "source_action": {"finalized": False, "delete_source": "never", "move_source": "never"},
    }
    row["canonical_integrity"] = {
        "algorithm": "sha256",
        "canonical_json_sha256": _canonical_json_sha256(row),
    }
    return row


def prepare_connection_stub_bootstrap(
    *,
    record_root: Path,
    state_path: Path,
    receipt_root: Path,
    receipt_id: str,
    source_relatives: list[str],
    candidate_adapter_sha256: str,
    installed_adapter_path: Path,
    status_path: Path,
    ffprobe_bin: str = "ffprobe",
) -> dict[str, Any]:
    """Create-or-revalidate a narrow, candidate-bound bootstrap receipt.

    This deliberately never writes adapter state or status. The old daemon
    cannot validate a three-second row, so only the freshly installed adapter
    may persist one after the deploy transaction switches bytes.
    """

    if re.fullmatch(r"[0-9a-f]{40}", receipt_id) is None:
        raise AdapterError("connection-stub bootstrap receipt id is invalid")
    if re.fullmatch(r"[0-9a-f]{64}", candidate_adapter_sha256) is None:
        raise AdapterError("connection-stub bootstrap candidate SHA is invalid")
    if not source_relatives or len(source_relatives) != len(set(source_relatives)):
        raise AdapterError("connection-stub bootstrap sources must be non-empty and unique")
    try:
        state_raw = state_path.read_bytes()
        state = json.loads(state_raw)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise AdapterError("connection-stub bootstrap state is unreadable") from exc
    if not isinstance(state, dict) or state.get("schema_version") != STATE_SCHEMA_VERSION:
        raise AdapterError("connection-stub bootstrap state schema mismatch")
    try:
        status = json.loads(status_path.read_bytes())
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise AdapterError("connection-stub bootstrap status is unreadable") from exc
    if not isinstance(status, dict):
        raise AdapterError("connection-stub bootstrap status is malformed")
    webhook_files = state.get("webhook_files")
    finalized = state.get("finalized")
    dispositions = state.get("source_dispositions")
    if not all(isinstance(value, dict) for value in (webhook_files, finalized, dispositions)):
        raise AdapterError("connection-stub bootstrap ledgers are malformed")
    rows: dict[str, dict[str, Any]] = {}
    for relative in sorted(source_relatives):
        path = PurePosixPath(relative)
        if path.is_absolute() or ".." in path.parts or len(path.parts) != 2:
            raise AdapterError("connection-stub bootstrap source path is unsafe")
        if relative in dispositions:
            raise AdapterError("connection-stub bootstrap source already has a disposition")
        row = build_connection_stub_disposition(
            record_root / path,
            record_root=record_root,
            webhook_files=webhook_files,
            finalized=finalized,
            ffprobe_bin=ffprobe_bin,
        )
        if row is None:
            raise AdapterError(f"connection-stub bootstrap source is not eligible: {relative}")
        rows[relative] = row
    expected_sources = {str(record_root / PurePosixPath(relative)) for relative in source_relatives}
    status_errors = status.get("finalize_errors")
    if not (
        status.get("service_reachable") is True
        and status.get("streaming") is False
        and status.get("recording") is False
        and status.get("finalizing") is False
        and status.get("error") == f"{len(source_relatives)} closed recording(s) failed finalization"
        and isinstance(status_errors, list)
        and len(status_errors) == len(source_relatives)
        and {entry.get("source") for entry in status_errors if isinstance(entry, dict)} == expected_sources
    ):
        raise AdapterError("connection-stub bootstrap status does not exactly bind eligible sources")
    # The legacy daemon refreshes only this observation timestamp while it is
    # otherwise idle.  It must not turn a receipt into an impossible-to-use
    # raw-byte snapshot, but every other state key remains transaction-bound.
    state_material = {
        key: value for key, value in state.items() if key != "last_room_status_epoch"
    }
    cookie_health = state_material.get("cookie_health")
    if not isinstance(cookie_health, dict):
        raise AdapterError("connection-stub bootstrap cookie health is malformed")
    state_material["cookie_health"] = {
        key: value
        for key, value in cookie_health.items()
        if key not in {"checked_at", "checked_at_epoch"}
    }
    # A fresh status changes its timestamp and ffmpeg happens to put unstable
    # process addresses in the two retained finalization diagnostics.  Preserve
    # every other byte-level JSON value, normalising only those proven
    # heartbeat/process-local fields; this is deliberately not a generic
    # finalization-error waiver.
    status_preimage = json.loads(json.dumps(status))
    status_preimage.pop("generated_at", None)
    status_preimage.pop("generated_at_epoch", None)
    cookie_status = status_preimage.get("bilibili_cookie")
    if not isinstance(cookie_status, dict):
        raise AdapterError("connection-stub bootstrap cookie status is malformed")
    status_preimage["bilibili_cookie"] = {
        key: value
        for key, value in cookie_status.items()
        if key not in {"checked_at", "checked_at_epoch"}
    }
    errors = status_preimage.get("finalize_errors")
    if isinstance(errors, list):
        for entry in errors:
            if isinstance(entry, dict) and isinstance(entry.get("error"), str):
                entry["error"] = re.sub(r"0x[0-9a-fA-F]+", "0x<address>", entry["error"])
    receipt = {
        "schema_version": CONNECTION_STUB_BOOTSTRAP_RECEIPT_SCHEMA_VERSION,
        "receipt_id": receipt_id,
        "candidate_adapter_sha256": candidate_adapter_sha256,
        "installed_adapter_sha256": sha256_file(installed_adapter_path),
        "adapter_state_sha256": hashlib.sha256(state_raw).hexdigest(),
        "adapter_state_material_sha256": _canonical_json_sha256(state_material),
        "adapter_status_preimage_sha256": _canonical_json_sha256(status_preimage),
        "source_relative_paths": sorted(source_relatives),
        "rows": rows,
    }
    receipt["canonical_integrity"] = {
        "algorithm": "sha256",
        "canonical_json_sha256": _canonical_json_sha256(receipt),
    }
    _ensure_identity_rebind_spool(receipt_root)
    destination = receipt_root / f"{receipt_id}.json"
    encoded = (json.dumps(receipt, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode()
    try:
        descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        existing = _local_json(destination)
        if existing != receipt:
            raise AdapterError("connection-stub bootstrap receipt already exists with different evidence")
        return receipt
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        try:
            destination.unlink()
        except OSError:
            pass
        raise
    return receipt


def validate_connection_stub_disposition(
    source_flv: Path,
    row: Any,
    *,
    record_root: Path,
    webhook_files: dict[str, Any],
    finalized: dict[str, Any],
    ffprobe_bin: str = "ffprobe",
    identity_rebinds: list[dict[str, Any]] | None = None,
    identity_rebind_attestations: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Revalidate immutable identity without rereading historical media bytes.

    Full SHA-256 and ffprobe attestation happens once when the row is created.
    Recurring heartbeat validation compares canonical state, live webhook and
    finalized-ledger projections, and exact regular-file fingerprints. A
    CloudFS may either rebind ``device``/``inode`` across a remount or change
    only both successor files' ``mtime_ns``/``ctime_ns`` metadata. Each narrow
    case requires a shared live FUSE mount, full-byte historical-SHA
    re-attestation, and its own durable chained receipt. All local-filesystem,
    content, path, size, mode, or other mixed drift stays fatal.
    """

    if not isinstance(row, dict):
        raise AdapterError("source disposition row is malformed")
    if set(row) != {
        "schema_version",
        "status",
        "reason_code",
        "source_relative_path",
        "source",
        "xml",
        "webhook",
        "decode",
        "session",
        "source_action",
        "canonical_integrity",
    }:
        raise AdapterError("source disposition field set is invalid")
    integrity = row.get("canonical_integrity")
    unsigned = {key: value for key, value in row.items() if key != "canonical_integrity"}
    if not isinstance(integrity, dict) or integrity != {
        "algorithm": "sha256",
        "canonical_json_sha256": _canonical_json_sha256(unsigned),
    }:
        raise AdapterError("source disposition canonical integrity mismatch")
    del ffprobe_bin  # recurring validation deliberately performs no media probe
    try:
        relative = source_flv.relative_to(record_root).as_posix()
    except ValueError as exc:
        raise AdapterError("source disposition escaped recording root") from exc
    if (
        row.get("schema_version") != SOURCE_DISPOSITION_SCHEMA_VERSION
        or row.get("status") != SOURCE_DISPOSITION_STATUS
        or row.get("reason_code") != SOURCE_DISPOSITION_REASON
        or row.get("source_relative_path") != relative
        or row.get("source_action")
        != {"finalized": False, "delete_source": "never", "move_source": "never"}
        or relative in finalized
        or source_flv.with_suffix(".mp4").exists()
    ):
        raise AdapterError("source disposition evidence drifted: identity/action invalid")

    source = row.get("source")
    xml = row.get("xml")
    decode = row.get("decode")
    session = row.get("session")
    if not all(isinstance(value, dict) for value in (source, xml, decode, session)):
        raise AdapterError("source disposition evidence is malformed")
    fingerprint_keys = set(_FILE_FINGERPRINT_KEYS)
    if (
        set(source) != {"path", "sha256", *fingerprint_keys}
        or set(xml)
        != {
            "path",
            "sha256",
            *fingerprint_keys,
            "official_bililiverecorder",
            "event_count",
            "record_info",
        }
        or set(decode)
        != {
            "duration_seconds",
            "size_bytes",
            "video_codec",
            "width",
            "height",
            "decoded_video_frames",
            "video_packets",
        }
        or set(session)
        != {
            "session_id",
            "prior_same_session_openings",
            "successor_gap_seconds",
            "successor_relative_path",
            "successor_webhook",
            "successor_source",
            "successor_finalized_ledger",
            "successor_mp4",
        }
    ):
        raise AdapterError("source disposition nested field set is invalid")
    xml_path = source_flv.with_suffix(".xml")
    record_info = xml.get("record_info")
    if (
        source.get("path") != str(source_flv)
        or xml.get("path") != str(xml_path)
        or re.fullmatch(r"[0-9a-f]{64}", str(source.get("sha256") or "")) is None
        or re.fullmatch(r"[0-9a-f]{64}", str(xml.get("sha256") or "")) is None
        or xml.get("official_bililiverecorder") is not True
        or not isinstance(xml.get("event_count"), int)
        or isinstance(xml.get("event_count"), bool)
        or not 0 <= xml["event_count"] <= CONNECTION_STUB_MAX_XML_EVENT_COUNT
        or not isinstance(record_info, dict)
        or record_info.get("roomid") != source_flv.name.split("_", 1)[0]
    ):
        raise AdapterError("source/XML immutable evidence drifted")

    evidence = webhook_files.get(relative)
    if not isinstance(evidence, dict) or row.get("webhook") != _webhook_file_binding(evidence):
        raise AdapterError("source disposition webhook evidence drifted")
    try:
        opened_at = _parse_webhook_datetime(evidence.get("file_open_time"), field="FileOpenTime")
        closed_at = _parse_webhook_datetime(evidence.get("file_close_time"), field="FileCloseTime")
        event_duration = float(evidence.get("duration") or 0)
        wall_duration = (closed_at - opened_at).total_seconds()
    except (AdapterError, TypeError, ValueError) as exc:
        raise AdapterError("source disposition webhook time evidence is invalid") from exc
    session_id = str(evidence.get("session_id") or "")
    if (
        evidence.get("status") != "CLOSED"
        or not evidence.get("opening_event_id")
        or not evidence.get("closing_event_id")
        or not session_id
        or evidence.get("file_size") != source.get("size_bytes")
        or int(source.get("size_bytes") or -1) >= CONNECTION_STUB_MAX_SIZE_BYTES
        or not 0 < event_duration < CONNECTION_STUB_MAX_DURATION_SECONDS
        or not 0 <= wall_duration < CONNECTION_STUB_MAX_DURATION_SECONDS
        or decode.get("video_codec") != "h264"
        or decode.get("width") != 0
        or decode.get("height") != 0
        or decode.get("decoded_video_frames") != 0
        or decode.get("video_packets") != 0
        or decode.get("size_bytes") != source.get("size_bytes")
        or abs(float(decode.get("duration_seconds") or 0) - event_duration) > 0.001
    ):
        raise AdapterError("source disposition thresholds/decode evidence drifted")

    openings: list[tuple[datetime, str, dict[str, Any]]] = []
    try:
        for candidate_relative, candidate in webhook_files.items():
            if isinstance(candidate, dict) and candidate.get("opening_event_id"):
                openings.append(
                    (
                        _parse_webhook_datetime(
                            candidate.get("file_open_time"), field="FileOpenTime"
                        ),
                        str(candidate_relative),
                        candidate,
                    )
                )
    except AdapterError as exc:
        raise AdapterError("same-session opening evidence is invalid") from exc
    openings.sort(key=lambda item: (item[0], item[1]))
    source_positions = [index for index, item in enumerate(openings) if item[1] == relative]
    if len(source_positions) != 1:
        raise AdapterError("source disposition opening evidence is not unique")
    source_position = source_positions[0]
    if any(
        str(item[2].get("session_id") or "") == session_id for item in openings[:source_position]
    ) or source_position + 1 >= len(openings):
        raise AdapterError("source disposition is not the first same-session opening")
    successor_opened, successor_relative, successor_evidence = openings[source_position + 1]
    successor_path = PurePosixPath(successor_relative)
    source_path = PurePosixPath(relative)
    if (
        successor_path.is_absolute()
        or ".." in successor_path.parts
        or len(successor_path.parts) != 2
        or successor_path.parts[0] != source_path.parts[0]
        or re.fullmatch(
            FILENAME_RX_TEMPLATE.format(room=re.escape(source_flv.name.split("_", 1)[0])),
            successor_path.parts[1],
        )
        is None
    ):
        raise AdapterError("same-session successor path is invalid")
    successor_gap = (successor_opened - closed_at).total_seconds()
    if (
        not 0 <= successor_gap <= CONNECTION_STUB_MAX_SUCCESSOR_GAP_SECONDS
        or successor_evidence.get("status") != "CLOSED"
        or str(successor_evidence.get("session_id") or "") != session_id
        or not successor_evidence.get("opening_event_id")
        or not successor_evidence.get("closing_event_id")
        or session.get("session_id") != session_id
        or session.get("prior_same_session_openings") != 0
        or session.get("successor_gap_seconds") != successor_gap
        or session.get("successor_relative_path") != successor_relative
        or session.get("successor_webhook") != _webhook_file_binding(successor_evidence)
    ):
        raise AdapterError("same-session successor evidence drifted")

    successor_source = record_root / successor_relative
    successor_mp4 = successor_source.with_suffix(".mp4")
    ledger = finalized.get(successor_relative)
    embedded_ledger = session.get("successor_finalized_ledger")
    successor_source_binding = session.get("successor_source")
    successor_mp4_binding = session.get("successor_mp4")
    if not all(
        isinstance(value, dict)
        for value in (ledger, embedded_ledger, successor_source_binding, successor_mp4_binding)
    ):
        raise AdapterError("successor finalized evidence is malformed")
    if set(successor_source_binding) != {"path", *fingerprint_keys} or set(
        successor_mp4_binding
    ) != {"path", "sha256", "media", *fingerprint_keys}:
        raise AdapterError("successor file fingerprint field set is invalid")
    disposition_bindings = {
        "source": source,
        "xml": xml,
        "successor_source": successor_source_binding,
        "successor_mp4": successor_mp4_binding,
    }
    disposition_paths = {
        "source": source_flv,
        "xml": xml_path,
        "successor_source": successor_source,
        "successor_mp4": successor_mp4,
    }
    identity_validation = _prepare_disposition_identity_validation(
        row=row,
        bindings=disposition_bindings,
        paths=disposition_paths,
        identity_rebinds=identity_rebinds,
    )
    expected_ledger = {
        "source_size": ledger.get("source_size"),
        "source_mtime_ns": ledger.get("source_mtime_ns"),
        "target": ledger.get("target"),
        "target_sha256": ledger.get("target_sha256"),
        "finalized_at": ledger.get("finalized_at"),
    }
    media = successor_mp4_binding.get("media")
    if (
        embedded_ledger != expected_ledger
        or successor_source_binding.get("path") != str(successor_source)
        or successor_mp4_binding.get("path") != str(successor_mp4)
        or ledger.get("target") != str(successor_mp4)
        or successor_source_binding.get("size_bytes") != successor_evidence.get("file_size")
        or ledger.get("source_size") != successor_source_binding.get("size_bytes")
        or successor_mp4_binding.get("sha256") != ledger.get("target_sha256")
        or re.fullmatch(r"[0-9a-f]{64}", str(successor_mp4_binding.get("sha256") or "")) is None
        or not isinstance(media, dict)
        or media.get("size_bytes") != successor_mp4_binding.get("size_bytes")
        or float(media.get("duration_seconds") or 0) <= 0
        or not media.get("video_codec")
        or not media.get("audio_codec")
        or int(media.get("width") or 0) <= 0
        or int(media.get("height") or 0) <= 0
    ):
        raise AdapterError("successor finalized ledger/media evidence drifted")
    if identity_validation.get("pending_rebind"):
        if not isinstance(identity_rebinds, list):  # pragma: no cover - prepare owns this gate
            raise AdapterError("source disposition FUSE rebind lacks a durable receipt ledger")
        if identity_rebind_attestations is None:
            raise SourceDispositionIdentityRebindRequired(
                validation=identity_validation,
                paths=disposition_paths,
            )
        _finish_disposition_identity_rebind(
            row=row,
            paths=disposition_paths,
            validation=identity_validation,
            identity_rebinds=identity_rebinds,
            attestations=identity_rebind_attestations,
        )
    return row


def revalidate_source_dispositions(
    state: dict[str, Any],
    *,
    record_root: Path,
    room_id: int,
    ffprobe_bin: str = "ffprobe",
    allow_identity_rebind_start: bool = False,
    identity_rebind_spool: Path | None = None,
) -> set[str]:
    """Revalidate every persisted ignore row on every adapter iteration."""
    dispositions = state.get("source_dispositions")
    identity_rebind_ledger = state.setdefault("source_disposition_identity_rebinds", {})
    identity_rebind_tasks = state.setdefault("source_disposition_identity_rebind_tasks", {})
    webhook_files = state.get("webhook_files") or {}
    finalized = state.get("finalized")
    if (
        not isinstance(dispositions, dict)
        or not isinstance(identity_rebind_ledger, dict)
        or not isinstance(identity_rebind_tasks, dict)
        or not isinstance(finalized, dict)
    ):
        raise AdapterError("adapter source disposition state is malformed")
    if any(relative not in dispositions for relative in identity_rebind_ledger):
        raise AdapterError("adapter source disposition identity rebind ledger has an orphan row")
    if any(relative not in dispositions for relative in identity_rebind_tasks):
        raise AdapterError(
            "adapter source disposition identity rebind task ledger has an orphan row"
        )
    if not dispositions:
        return set()
    if not isinstance(webhook_files, dict):
        raise AdapterError("adapter source disposition webhook state is malformed")
    filename_rx = re.compile(FILENAME_RX_TEMPLATE.format(room=re.escape(str(room_id))))
    validated: set[str] = set()
    for relative, row in dispositions.items():
        path = PurePosixPath(str(relative))
        if (
            path.is_absolute()
            or ".." in path.parts
            or len(path.parts) != 2
            or not re.fullmatch(r"20\d{2}-\d{2}-\d{2}", path.parts[0])
            or not filename_rx.fullmatch(path.parts[1])
        ):
            raise AdapterError("source disposition path is invalid")
        source = record_root / path.parts[0] / path.parts[1]
        existing_rebinds = identity_rebind_ledger.get(str(relative))
        if existing_rebinds is None:
            identity_rebinds: list[dict[str, Any]] = []
        elif isinstance(existing_rebinds, list):
            identity_rebinds = existing_rebinds
        else:
            raise AdapterError("source disposition identity rebind ledger row is malformed")
        try:
            validate_connection_stub_disposition(
                source,
                row,
                record_root=record_root,
                webhook_files=webhook_files,
                finalized=finalized,
                ffprobe_bin=ffprobe_bin,
                identity_rebinds=identity_rebinds,
            )
        except SourceDispositionIdentityRebindRequired as required:
            if identity_rebind_spool is None:
                raise AdapterError("source disposition identity rebind spool is unavailable")
            attestations = _advance_identity_rebind_task(
                relative=str(relative),
                row=row,
                validation=required.validation,
                paths=required.paths,
                task_ledger=identity_rebind_tasks,
                spool_root=identity_rebind_spool,
                allow_start=allow_identity_rebind_start,
            )
            validate_connection_stub_disposition(
                source,
                row,
                record_root=record_root,
                webhook_files=webhook_files,
                finalized=finalized,
                ffprobe_bin=ffprobe_bin,
                identity_rebinds=identity_rebinds,
                identity_rebind_attestations=attestations,
            )
        except AdapterError as exc:
            raise AdapterError(f"source disposition drift: {relative}: {exc}") from exc
        if identity_rebinds and existing_rebinds is None:
            identity_rebind_ledger[str(relative)] = identity_rebinds
        task = identity_rebind_tasks.get(str(relative))
        if task is not None:
            task = _validate_identity_rebind_task(task)
            if _identity_rebind_child_alive(task):
                raise AdapterError(
                    f"source disposition drift: {relative}: "
                    "completed identity rebind child exit is pending"
                )
            if identity_rebind_spool is None:
                raise AdapterError("source disposition identity rebind spool is unavailable")
            _clear_identity_rebind_run_dir(
                _identity_rebind_run_dir(identity_rebind_spool, str(relative))
            )
            del identity_rebind_tasks[str(relative)]
        validated.add(str(relative))
    return validated


def load_or_initialize_state(
    path: Path,
    *,
    now_epoch: float,
    managed_since_epoch: float | None = None,
) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        payload = {
            "schema_version": STATE_SCHEMA_VERSION,
            "managed_since_epoch": float(
                managed_since_epoch if managed_since_epoch is not None else now_epoch
            ),
            "finalized": {},
            "source_dispositions": {},
            "source_disposition_identity_rebinds": {},
            "source_disposition_identity_rebind_tasks": {},
        }
        atomic_write_json(path, payload, mode=0o600)
        return payload
    except (OSError, ValueError) as exc:
        raise AdapterError(f"adapter state is unreadable; refusing reset: {exc}") from exc
    if payload.get("schema_version") != STATE_SCHEMA_VERSION:
        raise AdapterError("adapter state schema mismatch; refusing reset")
    if not isinstance(payload.get("finalized"), dict):
        raise AdapterError("adapter state finalized ledger is malformed")
    dispositions = payload.setdefault("source_dispositions", {})
    if not isinstance(dispositions, dict):
        raise AdapterError("adapter state source dispositions ledger is malformed")
    identity_rebinds = payload.setdefault("source_disposition_identity_rebinds", {})
    if not isinstance(identity_rebinds, dict):
        raise AdapterError("adapter state source disposition identity rebind ledger is malformed")
    identity_rebind_tasks = payload.setdefault("source_disposition_identity_rebind_tasks", {})
    if not isinstance(identity_rebind_tasks, dict):
        raise AdapterError(
            "adapter state source disposition identity rebind task ledger is malformed"
        )
    try:
        float(payload["managed_since_epoch"])
    except (KeyError, TypeError, ValueError) as exc:
        raise AdapterError("adapter state lacks managed_since_epoch") from exc
    return payload


def discover_managed_flvs(
    record_root: Path,
    *,
    room_id: int,
    managed_since_epoch: float,
    explicit_relative_paths: Iterable[str] = (),
) -> list[Path]:
    """Find post-migration FLVs plus exact paths already owned by the webhook ledger.

    Do not inspect every historical XML file to identify its producer. On
    CloudDrive an uncached 9 KiB XML read can block for minutes, making the
    recorder status stale. FileOpening/FileClosed paths have already passed
    strict room/date/filename validation, so they are the bounded authority for
    official files whose remote mtime is unexpectedly old.
    """

    filename_rx = re.compile(FILENAME_RX_TEMPLATE.format(room=re.escape(str(room_id))))
    # Recorder directories use Asia/Shanghai dates. A one-day UTC lookback is
    # deliberately conservative around timezone/midnight boundaries while
    # bounding CloudDrive metadata traversal to the migration window.
    managed_date_floor = (
        datetime.fromtimestamp(managed_since_epoch, tz=timezone.utc).date() - timedelta(days=1)
    ).isoformat()
    candidates: list[Path] = []
    try:
        date_dirs = sorted(
            path
            for path in record_root.iterdir()
            if (
                path.name >= managed_date_floor
                and re.fullmatch(r"20\d{2}-\d{2}-\d{2}", path.name)
                and path.is_dir()
            )
        )
    except OSError as exc:
        raise AdapterError(f"recording root unreadable: {record_root}: {exc}") from exc
    for date_dir in date_dirs:
        try:
            flvs: Iterable[Path] = date_dir.glob(f"{room_id}_*.flv")
            for flv in flvs:
                if flv.parent != date_dir or not filename_rx.fullmatch(flv.name):
                    continue
                try:
                    new_enough = flv.stat().st_mtime >= managed_since_epoch
                except OSError:
                    continue
                if new_enough:
                    candidates.append(flv)
        except OSError:
            continue

    for relative in explicit_relative_paths:
        path = PurePosixPath(str(relative))
        parts = path.parts
        if (
            path.is_absolute()
            or ".." in parts
            or len(parts) != 2
            or not re.fullmatch(r"20\d{2}-\d{2}-\d{2}", parts[0])
            or not filename_rx.fullmatch(parts[1])
        ):
            raise AdapterError(f"webhook ledger path is invalid: {relative}")
        source = record_root / parts[0] / parts[1]
        try:
            if source.is_file():
                candidates.append(source)
        except OSError:
            continue
    return sorted(set(candidates))


def _newest_source_probe(
    record_root: Path,
    room_id: int,
    *,
    ffprobe_bin: str = "ffprobe",
    include_media: bool = True,
) -> dict[str, Any] | None:
    try:
        date_dirs = sorted(
            path
            for path in record_root.iterdir()
            if re.fullmatch(r"20\d{2}-\d{2}-\d{2}", path.name) and path.is_dir()
        )[-2:]
        sources = sorted(
            (source for date_dir in date_dirs for source in date_dir.glob(f"{room_id}_*.flv")),
            key=lambda path: path.stat().st_mtime_ns,
            reverse=True,
        )
    except OSError:
        return None
    if not sources:
        return None
    source = sources[0]
    try:
        stat = source.stat()
    except OSError:
        return None
    result: dict[str, Any] = {
        "path": str(source),
        "size_bytes": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
    }
    if include_media and stat.st_size >= 256 * 1024:
        try:
            result["media"] = probe_stream_shape(source, ffprobe_bin=ffprobe_bin)
        except AdapterError as exc:
            result["media_probe_error"] = str(exc)[:300]
    return result


def build_status(
    *,
    room_id: int,
    room: dict[str, Any] | None,
    now_epoch: float,
    record_root: Path,
    error: str | None = None,
    finalizing: bool = False,
    finalized: list[dict[str, Any]] | None = None,
    finalize_errors: list[dict[str, str]] | None = None,
    ffprobe_bin: str = "ffprobe",
    cookie_health: dict[str, Any] | None = None,
    inactive_grace_remaining_seconds: int | None = None,
) -> dict[str, Any]:
    io_stats = (room or {}).get("ioStats") or {}
    recording_stats = (room or {}).get("recordingStats") or {}
    streaming = bool((room or {}).get("streaming")) if room is not None else None
    recording = bool((room or {}).get("recording")) if room is not None else None
    current_size = int(recording_stats.get("currentFileSize") or 0)
    total_input_bytes = int(recording_stats.get("totalInputBytes") or 0)
    total_output_bytes = int(recording_stats.get("totalOutputBytes") or 0)
    # An idle adapter must not traverse or pull historical CloudDrive files
    # merely to refresh a status heartbeat. During recording, inspect only the
    # two newest date directories; the current source is local/hot and objective
    # codec/resolution telemetry is worth the bounded probe.
    newest = (
        _newest_source_probe(
            record_root,
            room_id,
            ffprobe_bin=ffprobe_bin,
            include_media=True,
        )
        if recording
        else None
    )
    if newest and current_size <= 0 and recording:
        current_size = int(newest["size_bytes"])
    network_mbps = float(io_stats.get("networkMbps") or 0.0)
    service_reachable = room is not None and error is None
    effective_error = error
    if effective_error is None and finalize_errors:
        effective_error = f"{len(finalize_errors)} closed recording(s) failed finalization"
    live_status = None if not service_reachable else int(bool(streaming or recording))
    public_cookie_health = (
        {key: value for key, value in cookie_health.items() if key != "cookie_sha256"}
        if isinstance(cookie_health, dict)
        else None
    )
    return {
        "schema_version": STATUS_SCHEMA_VERSION,
        "backend": BACKEND,
        "generated_at": datetime.fromtimestamp(now_epoch, tz=timezone.utc).isoformat(),
        "generated_at_epoch": now_epoch,
        "room_id": str(room_id),
        "service_reachable": service_reachable,
        "live_status": live_status,
        "streaming": streaming,
        "recording": recording,
        "danmaku_connected": (
            bool((room or {}).get("danmakuConnected")) if room is not None else None
        ),
        "running_status": (
            "recording"
            if recording
            else "streaming"
            if streaming
            else "finalizing"
            if finalizing
            else "idle"
            if service_reachable
            else "unknown"
        ),
        "rec_total": current_size if service_reachable else None,
        "total_input_bytes": total_input_bytes if service_reachable else None,
        "total_output_bytes": total_output_bytes if service_reachable else None,
        "rec_rate": int(network_mbps * 1_000_000 / 8) if service_reachable else None,
        "stream_host": io_stats.get("streamHost") if service_reachable else None,
        "real_stream_format": "flv-standard",
        "requested_quality_priority": list(QUALITY_PRIORITY),
        "requested_quality_number": 10000,
        "actual_quality_number": None,
        "bilibili_cookie": public_cookie_health,
        "recording_path": newest.get("path") if newest and recording else None,
        "latest_source": newest,
        "finalizing": bool(finalizing),
        "inactive_grace_remaining_seconds": inactive_grace_remaining_seconds,
        "finalized": finalized or [],
        "finalize_errors": finalize_errors or [],
        "error": effective_error,
    }


def run_once(args: argparse.Namespace) -> int:
    now_epoch = time.time()
    state = load_or_initialize_state(
        args.state_path,
        now_epoch=now_epoch,
        managed_since_epoch=args.managed_since_epoch,
    )
    try:
        webhook_changed = reconcile_webhook_journal(
            args.webhook_journal,
            state,
            room_id=args.room,
        )
    except AdapterError as exc:
        status = build_status(
            room_id=args.room,
            room=None,
            now_epoch=time.time(),
            record_root=args.record_root,
            error=str(exc),
            ffprobe_bin=args.ffprobe,
        )
        atomic_write_json(args.status_path, status, mode=0o644)
        print(json.dumps(status, ensure_ascii=False))
        return 2
    if webhook_changed:
        atomic_write_json(args.state_path, state, mode=0o600)
    cookie_health, cookie_health_changed = probe_cookie_health(
        args.recorder_config,
        state,
        now_epoch=now_epoch,
        refresh_seconds=args.cookie_health_refresh,
        timeout_seconds=args.cookie_health_timeout,
    )
    if cookie_health_changed:
        atomic_write_json(args.state_path, state, mode=0o600)
    env = load_env_file(args.env_file)
    username = env.get("BREC_HTTP_BASIC_USER", "")
    password = env.get("BREC_HTTP_BASIC_PASS", "")
    try:
        room = query_room_status(
            args.endpoint,
            args.room,
            username=username,
            password=password,
            timeout_seconds=args.api_timeout,
        )
    except AdapterError as exc:
        state_changed = False
        for key in ("inactive_since_epoch", "last_room_status_epoch"):
            if state.pop(key, None) is not None:
                state_changed = True
        if state_changed:
            atomic_write_json(args.state_path, state, mode=0o600)
        status = build_status(
            room_id=args.room,
            room=None,
            now_epoch=time.time(),
            record_root=args.record_root,
            error=str(exc),
            ffprobe_bin=args.ffprobe,
            cookie_health=cookie_health,
        )
        atomic_write_json(args.status_path, status, mode=0o644)
        print(json.dumps(status, ensure_ascii=False))
        return 2

    observed_epoch = time.time()
    try:
        last_room_status_epoch = float(state["last_room_status_epoch"])
        status_continuous = (
            0 <= observed_epoch - last_room_status_epoch <= args.status_continuity_max_gap_seconds
        )
    except (KeyError, TypeError, ValueError):
        status_continuous = False
    state["last_room_status_epoch"] = observed_epoch

    try:
        validated_dispositions = revalidate_source_dispositions(
            state,
            record_root=args.record_root,
            room_id=args.room,
            ffprobe_bin=args.ffprobe,
            allow_identity_rebind_start=not bool(room.get("streaming") or room.get("recording")),
            identity_rebind_spool=args.state_path.parent / ".source-disposition-identity-rebind",
        )
    except AdapterError as exc:
        atomic_write_json(args.state_path, state, mode=0o600)
        status = build_status(
            room_id=args.room,
            room=room,
            now_epoch=time.time(),
            record_root=args.record_root,
            error=str(exc),
            ffprobe_bin=args.ffprobe,
            cookie_health=cookie_health,
        )
        atomic_write_json(args.status_path, status, mode=0o644)
        print(json.dumps(status, ensure_ascii=False))
        return 2

    active = bool(room.get("streaming") or room.get("recording"))
    finalized: list[dict[str, Any]] = []
    finalize_errors: list[dict[str, str]] = []
    if active:
        state.pop("inactive_since_epoch", None)
        atomic_write_json(args.state_path, state, mode=0o600)
        status = build_status(
            room_id=args.room,
            room=room,
            now_epoch=time.time(),
            record_root=args.record_root,
            ffprobe_bin=args.ffprobe,
            cookie_health=cookie_health,
        )
        atomic_write_json(args.status_path, status, mode=0o644)
        print(json.dumps(status, ensure_ascii=False))
        return 0

    if not status_continuous:
        state.pop("inactive_since_epoch", None)
    try:
        inactive_since = float(state["inactive_since_epoch"])
        if inactive_since > observed_epoch + 60:
            raise ValueError("future inactivity timestamp")
    except (KeyError, TypeError, ValueError):
        inactive_since = observed_epoch
        state["inactive_since_epoch"] = inactive_since
    atomic_write_json(args.state_path, state, mode=0o600)
    inactive_age = max(0.0, observed_epoch - inactive_since)
    grace_remaining = max(0.0, args.inactive_grace_seconds - inactive_age)
    if grace_remaining > 0:
        status = build_status(
            room_id=args.room,
            room=room,
            now_epoch=time.time(),
            record_root=args.record_root,
            finalizing=True,
            ffprobe_bin=args.ffprobe,
            cookie_health=cookie_health,
            inactive_grace_remaining_seconds=math.ceil(grace_remaining),
        )
        atomic_write_json(args.status_path, status, mode=0o644)
        print(json.dumps(status, ensure_ascii=False))
        return 0

    closed_files = state.get("webhook_files") or {}
    candidates = discover_managed_flvs(
        args.record_root,
        room_id=args.room,
        managed_since_epoch=float(state["managed_since_epoch"]),
        explicit_relative_paths=(closed_files.keys() if isinstance(closed_files, dict) else ()),
    )
    finalized_ledger = state["finalized"]
    source_dispositions = state.setdefault("source_dispositions", {})
    disposition_state_changed = False
    eligible: list[Path] = []
    for source in candidates:
        relative = str(source.relative_to(args.record_root))
        evidence = closed_files.get(relative) if isinstance(closed_files, dict) else None
        if not isinstance(evidence, dict) or evidence.get("status") != "CLOSED":
            finalize_errors.append(
                {
                    "source": str(source),
                    "error": "source lacks BililiveRecorder FileClosed evidence",
                }
            )
            continue
        try:
            source_size = source.stat().st_size
        except OSError as exc:
            finalize_errors.append({"source": str(source), "error": str(exc)[:1200]})
            continue
        if source_size != int(evidence.get("file_size") or -1):
            finalize_errors.append(
                {
                    "source": str(source),
                    "error": (
                        "source size does not match BililiveRecorder FileClosed "
                        f"({source_size} != {evidence.get('file_size')})"
                    ),
                }
            )
            continue
        existing_disposition = source_dispositions.get(relative)
        if existing_disposition is not None:
            if relative not in validated_dispositions:
                finalize_errors.append(
                    {
                        "source": str(source),
                        "error": "source disposition was not revalidated",
                    }
                )
            continue
        if relative not in finalized_ledger and not source.with_suffix(".mp4").exists():
            disposition = build_connection_stub_disposition(
                source,
                record_root=args.record_root,
                webhook_files=closed_files,
                finalized=finalized_ledger,
                ffprobe_bin=args.ffprobe,
            )
            if disposition is not None:
                source_dispositions[relative] = disposition
                disposition_state_changed = True
                continue
        eligible.append(source)

    if disposition_state_changed:
        atomic_write_json(args.state_path, state, mode=0o600)

    if isinstance(closed_files, dict):
        for relative, evidence in closed_files.items():
            if not isinstance(evidence, dict):
                continue
            source = args.record_root / relative
            if evidence.get("status") == "OPEN":
                finalize_errors.append(
                    {
                        "source": str(source),
                        "error": "FileOpening has no matching FileClosed event",
                    }
                )
            elif evidence.get("status") == "CLOSED" and not source.is_file():
                finalize_errors.append(
                    {
                        "source": str(source),
                        "error": "FileClosed event points to a missing source file",
                    }
                )

    pending = [
        source
        for source in eligible
        if str(source.relative_to(args.record_root)) not in state["finalized"]
        or not source.with_suffix(".mp4").is_file()
    ]
    if pending or finalize_errors:
        in_progress = build_status(
            room_id=args.room,
            room=room,
            now_epoch=time.time(),
            record_root=args.record_root,
            finalizing=True,
            ffprobe_bin=args.ffprobe,
            cookie_health=cookie_health,
        )
        atomic_write_json(args.status_path, in_progress, mode=0o644)

    for source in pending[: args.max_finalize]:
        relative = str(source.relative_to(args.record_root))
        try:
            result = finalize_recording(
                source,
                ffmpeg_bin=args.ffmpeg,
                ffprobe_bin=args.ffprobe,
                existing_ledger=state["finalized"].get(relative),
            )
            finalized.append(result)
            state["finalized"][relative] = {
                "source_size": source.stat().st_size,
                "source_mtime_ns": source.stat().st_mtime_ns,
                "target": str(source.with_suffix(".mp4")),
                "target_sha256": result["target_sha256"],
                "finalized_at": datetime.now(timezone.utc).isoformat(),
            }
            atomic_write_json(args.state_path, state, mode=0o600)
        except (AdapterError, OSError) as exc:
            finalize_errors.append({"source": str(source), "error": str(exc)[:1200]})

    status = build_status(
        room_id=args.room,
        room=room,
        now_epoch=time.time(),
        record_root=args.record_root,
        finalizing=len(pending) > args.max_finalize,
        finalized=finalized,
        finalize_errors=finalize_errors,
        ffprobe_bin=args.ffprobe,
        cookie_health=cookie_health,
    )
    atomic_write_json(args.status_path, status, mode=0o644)
    print(json.dumps(status, ensure_ascii=False))
    return 1 if finalize_errors else 0


def _make_webhook_handler(
    *,
    room_id: int,
    journal_path: Path,
) -> type[BaseHTTPRequestHandler]:
    class WebhookHandler(BaseHTTPRequestHandler):
        server_version = "BililiveRecorderAdapterWebhook/1"
        protocol_version = "HTTP/1.1"

        def _empty_response(self, status: int) -> None:
            self.send_response(status)
            self.send_header("Content-Length", "0")
            self.send_header("Connection", "close")
            self.end_headers()

        def do_POST(self) -> None:  # noqa: N802 — BaseHTTPRequestHandler API
            if self.path != "/webhook":
                self._empty_response(404)
                return
            try:
                content_length = int(self.headers.get("Content-Length") or "")
            except ValueError:
                self._empty_response(411)
                return
            if content_length < 2 or content_length > 256 * 1024:
                self._empty_response(413)
                return
            payload: Any = None
            try:
                payload = json.loads(self.rfile.read(content_length))
                append_webhook_event(
                    journal_path,
                    payload,
                    room_id=room_id,
                )
            except (AdapterError, json.JSONDecodeError, OSError) as exc:
                event_type = payload.get("EventType") if isinstance(payload, dict) else None
                event_data = payload.get("EventData") if isinstance(payload, dict) else None
                relative = event_data.get("RelativePath") if isinstance(event_data, dict) else None
                event_timestamp = (
                    payload.get("EventTimestamp") if isinstance(payload, dict) else None
                )
                print(
                    "webhook rejected "
                    f"type={event_type!r} timestamp={str(event_timestamp)[:80]!r} "
                    f"relative={str(relative)[:240]!r}: {exc}",
                    file=sys.stderr,
                    flush=True,
                )
                self._empty_response(400)
                return
            self._empty_response(204)

        def do_GET(self) -> None:  # noqa: N802 — BaseHTTPRequestHandler API
            self._empty_response(404)

        def log_message(self, _format: str, *args: Any) -> None:
            return

    return WebhookHandler


def serve_adapter(args: argparse.Namespace) -> int:
    stop_event = threading.Event()

    def worker() -> None:
        while not stop_event.is_set():
            started = time.monotonic()
            try:
                run_once(args)
            except Exception as exc:  # noqa: BLE001 — daemon must publish failure state
                message = f"adapter worker failure: {type(exc).__name__}: {str(exc)[:500]}"
                try:
                    status = build_status(
                        room_id=args.room,
                        room=None,
                        now_epoch=time.time(),
                        record_root=args.record_root,
                        error=message,
                        ffprobe_bin=args.ffprobe,
                    )
                    atomic_write_json(args.status_path, status, mode=0o644)
                except Exception:
                    pass
                print(message, file=sys.stderr, flush=True)
            elapsed = time.monotonic() - started
            stop_event.wait(max(1.0, args.poll_interval - elapsed))

    handler = _make_webhook_handler(
        room_id=args.room,
        journal_path=args.webhook_journal,
    )
    server = ThreadingHTTPServer((args.webhook_bind, args.webhook_port), handler)
    thread = threading.Thread(target=worker, name="adapter-reconcile", daemon=True)
    thread.start()
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        stop_event.set()
        server.server_close()
        thread.join(timeout=5)
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--once", action="store_true", help="run one status/finalization pass")
    mode.add_argument(
        "--serve",
        action="store_true",
        help="serve Webhook v2 and periodically reconcile/finalize",
    )
    mode.add_argument(
        "--prepare-connection-stub-bootstrap",
        action="store_true",
        help="create-only candidate receipt; never writes adapter state or status",
    )
    parser.add_argument("--room", type=int, default=22966160)
    parser.add_argument(
        "--endpoint",
        default="http://127.0.0.1:23566/graphql",
        help="localhost BililiveRecorder GraphQL endpoint",
    )
    parser.add_argument(
        "--record-root",
        type=Path,
        default=Path("/root/clouddrive2/CloudNAS/CloudDrive/123云盘/live-streaming/22966160"),
    )
    parser.add_argument(
        "--status-path",
        type=Path,
        default=Path("/opt/bilive/recording/status.json"),
    )
    parser.add_argument(
        "--state-path",
        type=Path,
        default=Path("/opt/bilive/recording/adapter-state.json"),
    )
    parser.add_argument(
        "--webhook-journal",
        type=Path,
        default=Path("/opt/bilive/recording/webhook-events.jsonl"),
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        default=Path("/opt/bilive/recorder.env"),
    )
    parser.add_argument(
        "--recorder-config",
        type=Path,
        default=Path("/opt/bilive/bililive-recorder/config.json"),
    )
    parser.add_argument("--cookie-health-refresh", type=int, default=6 * 60 * 60)
    parser.add_argument("--cookie-health-timeout", type=float, default=20.0)
    parser.add_argument("--managed-since-epoch", type=float)
    parser.add_argument("--api-timeout", type=float, default=15.0)
    parser.add_argument(
        "--inactive-grace-seconds",
        type=int,
        default=360,
        help="require this much continuously inactive time before finalization",
    )
    parser.add_argument(
        "--status-continuity-max-gap-seconds",
        type=int,
        default=150,
        help="reset inactivity grace when healthy recorder polls are too far apart",
    )
    parser.add_argument("--max-finalize", type=int, default=8)
    parser.add_argument("--ffmpeg", default="ffmpeg")
    parser.add_argument("--ffprobe", default="ffprobe")
    parser.add_argument("--webhook-bind", default="127.0.0.1")
    parser.add_argument("--webhook-port", type=int, default=18080)
    parser.add_argument("--poll-interval", type=float, default=60.0)
    parser.add_argument("--bootstrap-receipt-root", type=Path)
    parser.add_argument("--bootstrap-receipt-id")
    parser.add_argument("--bootstrap-source-relative", action="append", default=[])
    parser.add_argument("--bootstrap-candidate-adapter-sha256")
    parser.add_argument("--bootstrap-installed-adapter-path", type=Path)
    parser.add_argument("--bootstrap-status-path", type=Path)
    args = parser.parse_args(argv)
    if args.max_finalize < 1:
        parser.error("--max-finalize must be positive")
    if args.inactive_grace_seconds < 0:
        parser.error("--inactive-grace-seconds must be non-negative")
    if args.status_continuity_max_gap_seconds < 1:
        parser.error("--status-continuity-max-gap-seconds must be positive")
    if not (1 <= args.webhook_port <= 65535):
        parser.error("--webhook-port is out of range")
    if args.poll_interval < 5:
        parser.error("--poll-interval must be at least 5 seconds")
    if args.prepare_connection_stub_bootstrap and not all(
        (
            args.bootstrap_receipt_root,
            args.bootstrap_receipt_id,
            args.bootstrap_source_relative,
            args.bootstrap_candidate_adapter_sha256,
            args.bootstrap_installed_adapter_path,
            args.bootstrap_status_path,
        )
    ):
        parser.error("bootstrap receipt arguments are required")
    return args


def main(argv: list[str] | None = None) -> int:
    effective_argv = list(sys.argv[1:] if argv is None else argv)
    if len(effective_argv) == 3 and effective_argv[0] == "--identity-rebind-hash-child":
        return _identity_rebind_hash_child(
            Path(effective_argv[1]),
            Path(effective_argv[2]),
        )
    args = parse_args(effective_argv)
    if args.prepare_connection_stub_bootstrap:
        receipt = prepare_connection_stub_bootstrap(
            record_root=args.record_root,
            state_path=args.state_path,
            receipt_root=args.bootstrap_receipt_root,
            receipt_id=args.bootstrap_receipt_id,
            source_relatives=args.bootstrap_source_relative,
            candidate_adapter_sha256=args.bootstrap_candidate_adapter_sha256,
            installed_adapter_path=args.bootstrap_installed_adapter_path,
            status_path=args.bootstrap_status_path,
            ffprobe_bin=args.ffprobe,
        )
        print(json.dumps(receipt, ensure_ascii=False, sort_keys=True))
        return 0
    return serve_adapter(args) if args.serve else run_once(args)


if __name__ == "__main__":
    sys.exit(main())
