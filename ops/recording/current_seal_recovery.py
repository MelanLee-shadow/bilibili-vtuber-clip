"""Validate a current seal proof for a previously copied native archive row.

The native archive worker's original row remains the historical source
authority.  This module validates a separately hashed, current observation
used only when that old row lacks all of the worker's copy-time open-file
fields.  It never turns the observation into a historical ``FileClosed``
event and it never updates the worker state.
"""

from __future__ import annotations

from datetime import datetime
import hashlib
import json
import math
from pathlib import Path, PurePosixPath
import re
from typing import Any


CURRENT_SEAL_RECOVERY_SCHEMA = "sealed-archive-current-seal-recovery.v1"
CURRENT_SEAL_RECOVERY_AUTHORITY = "N100_115_ARCHIVE_STATE_CURRENT_SEAL_RECOVERY"
CURRENT_OPEN_PROBE_SCHEMA = "container-open-fd-probe-v2"
CURRENT_OPEN_PHASES = (
    "current_before_stability",
    "current_after_stability",
    "current_before_hash",
    "current_after_hash",
    "current_before_publish",
)
STAT_FIELDS = (
    "size_bytes",
    "mtime_ns",
    "ctime_ns",
    "inode",
    "device",
    "mode",
)
PROBE_SUMMARY_FIELDS = (
    "proc_dirs",
    "fd_dirs",
    "fd_entries",
    "readlink_ok",
    "readlink_fail",
    "matched",
    "nonfinal",
    "proc_missing",
    "fd_dir_errors",
    "fd_snapshot_changes",
    "proc_snapshot_changed",
    "complete",
)
SHA256_RX = re.compile(r"[0-9a-fA-F]{64}\Z")
MAX_RECOVERY_BYTES = 16 * 1024 * 1024


class CurrentSealRecoveryError(ValueError):
    """A current seal proof failed closed."""


def _mapping(value: Any, *, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise CurrentSealRecoveryError(f"{label} must be an object")
    return value


def _sha256(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or SHA256_RX.fullmatch(value) is None:
        raise CurrentSealRecoveryError(f"{label} must be a plain SHA-256 hex string")
    return value.lower()


def _positive_int(value: Any, *, label: str) -> int:
    if type(value) is not int:
        raise CurrentSealRecoveryError(f"{label} must be an integer")
    if value < 0:
        raise CurrentSealRecoveryError(f"{label} must not be negative")
    return value


def canonical_json_sha256(value: Any) -> str:
    """Hash a JSON value with the proof's deterministic identity encoding."""

    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    return hashlib.sha256(raw).hexdigest()


def _stable_file_identity(stat_result: Any) -> tuple[int, int, int, int, int]:
    """Return file identity fields unaffected by a read-only atime update."""

    return (
        stat_result.st_dev,
        stat_result.st_ino,
        stat_result.st_size,
        stat_result.st_mtime_ns,
        stat_result.st_ctime_ns,
    )


def _timestamp(value: Any, *, label: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise CurrentSealRecoveryError(f"{label} must be an ISO-8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CurrentSealRecoveryError(f"{label} is not an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise CurrentSealRecoveryError(f"{label} must include a timezone")
    return parsed


def _stat(value: Any, *, label: str) -> dict[str, int]:
    raw = _mapping(value, label=label)
    normalized: dict[str, int] = {}
    for key in STAT_FIELDS:
        normalized[key] = _positive_int(raw.get(key), label=f"{label}.{key}")
    if normalized["size_bytes"] <= 0:
        raise CurrentSealRecoveryError(f"{label}.size_bytes must be positive")
    return normalized


def _validate_stability(
    stability: Any,
    *,
    source_size_bytes: int,
    xml_size_bytes: int,
    current_open_exclusions: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    raw = _mapping(stability, label="current_stability")
    wait_seconds = raw.get("wait_seconds")
    if type(wait_seconds) not in (int, float) or not math.isfinite(wait_seconds):
        raise CurrentSealRecoveryError("current_stability.wait_seconds is invalid")
    wait = float(wait_seconds)
    if wait < 10.0:
        raise CurrentSealRecoveryError("current_stability.wait_seconds is shorter than 10 seconds")
    stability_before = _timestamp(
        current_open_exclusions["current_before_stability"]["observed_at_utc"],
        label="current_open_exclusions.current_before_stability.observed_at_utc",
    )
    stability_after = _timestamp(
        current_open_exclusions["current_after_stability"]["observed_at_utc"],
        label="current_open_exclusions.current_after_stability.observed_at_utc",
    )
    observed_wait = (stability_after - stability_before).total_seconds()
    if not math.isfinite(observed_wait) or observed_wait < 10.0:
        raise CurrentSealRecoveryError(
            "current stability phase window is shorter than 10 seconds"
        )
    before = _mapping(raw.get("before"), label="current_stability.before")
    after = _mapping(raw.get("after"), label="current_stability.after")
    before_flv = _stat(before.get("flv"), label="current_stability.before.flv")
    before_xml = _stat(before.get("xml"), label="current_stability.before.xml")
    after_flv = _stat(after.get("flv"), label="current_stability.after.flv")
    after_xml = _stat(after.get("xml"), label="current_stability.after.xml")
    if before_flv != after_flv or before_xml != after_xml:
        raise CurrentSealRecoveryError("current source/XML changed during stability wait")
    if before_flv["size_bytes"] != source_size_bytes:
        raise CurrentSealRecoveryError("current stable FLV size does not match historical row")
    if before_xml["size_bytes"] != xml_size_bytes:
        raise CurrentSealRecoveryError("current stable XML size does not match historical row")
    return {
        "wait_seconds": wait,
        "observed_wait_seconds": observed_wait,
        "before": {"flv": before_flv, "xml": before_xml},
        "after": {"flv": after_flv, "xml": after_xml},
    }


def _validate_open_phase(
    value: Any,
    *,
    phase: str,
    relative: str,
    source_flv: Path,
    admission_mode: str,
) -> dict[str, Any]:
    raw = _mapping(value, label=f"current_open_exclusions.{phase}")
    if raw.get("phase") != phase:
        raise CurrentSealRecoveryError(f"current open phase name mismatch: {phase}")
    if raw.get("source_relative") != relative:
        raise CurrentSealRecoveryError(f"current open phase is bound to another source: {phase}")
    _timestamp(raw.get("observed_at_utc"), label=f"current_open_exclusions.{phase}.observed_at_utc")
    probe = _mapping(raw.get("probe"), label=f"current_open_exclusions.{phase}.probe")
    if probe.get("schema") != CURRENT_OPEN_PROBE_SCHEMA:
        raise CurrentSealRecoveryError(f"current open phase has the wrong probe schema: {phase}")
    summary = _mapping(probe.get("summary"), label=f"current_open_exclusions.{phase}.probe.summary")
    if set(summary) != set(PROBE_SUMMARY_FIELDS):
        raise CurrentSealRecoveryError(f"current open phase summary fields are incomplete: {phase}")
    counters = {key: _positive_int(summary[key], label=f"{phase}.probe.summary.{key}") for key in PROBE_SUMMARY_FIELDS}
    if counters["complete"] != 1:
        raise CurrentSealRecoveryError(f"current open phase enumeration is incomplete: {phase}")
    if counters["proc_dirs"] <= 0 or counters["fd_dirs"] > counters["proc_dirs"]:
        raise CurrentSealRecoveryError(f"current open phase process counts are invalid: {phase}")
    if counters["fd_entries"] != counters["readlink_ok"] + counters["readlink_fail"]:
        raise CurrentSealRecoveryError(f"current open phase FD counts are inconsistent: {phase}")
    for key in (
        "readlink_fail",
        "nonfinal",
        "proc_missing",
        "fd_dir_errors",
        "fd_snapshot_changes",
        "proc_snapshot_changed",
    ):
        if counters[key] != 0:
            raise CurrentSealRecoveryError(f"current open phase has {key}: {phase}")
    open_relatives = probe.get("open_relatives")
    if not isinstance(open_relatives, list) or any(
        not isinstance(item, str) or not item for item in open_relatives
    ):
        raise CurrentSealRecoveryError(f"current open phase paths are invalid: {phase}")
    if open_relatives != sorted(set(open_relatives)):
        raise CurrentSealRecoveryError(f"current open phase paths are not unique/sorted: {phase}")
    matched_records = probe.get("matched_records")
    matched_records = _positive_int(matched_records, label=f"{phase}.probe.matched_records")
    if matched_records != counters["matched"] or matched_records < len(open_relatives):
        raise CurrentSealRecoveryError(f"current open phase match counts are inconsistent: {phase}")
    if admission_mode == "active" and not open_relatives:
        raise CurrentSealRecoveryError("active current recovery has an empty open set")
    if admission_mode not in {"idle", "active"}:
        raise CurrentSealRecoveryError("current recovery admission_mode is invalid")
    target_names = {source_flv.name, source_flv.stem}
    for item in open_relatives:
        candidate = PurePosixPath(item)
        if candidate.name in target_names or item == relative:
            raise CurrentSealRecoveryError(f"current open phase still lists the target: {phase}")
    return {
        "phase": phase,
        "source_relative": relative,
        "observed_at_utc": raw["observed_at_utc"],
        "probe": {"schema": CURRENT_OPEN_PROBE_SCHEMA, "summary": counters, "open_relatives": list(open_relatives), "matched_records": matched_records},
    }


def _validate_current_open_exclusions(
    value: Any,
    *,
    relative: str,
    source_flv: Path,
    admission_mode: str,
) -> dict[str, dict[str, Any]]:
    raw = _mapping(value, label="current_open_exclusions")
    if set(raw) != set(CURRENT_OPEN_PHASES):
        raise CurrentSealRecoveryError("current open phases are missing or unexpected")
    validated = {
        phase: _validate_open_phase(
            raw[phase],
            phase=phase,
            relative=relative,
            source_flv=source_flv,
            admission_mode=admission_mode,
        )
        for phase in CURRENT_OPEN_PHASES
    }
    timestamps = [_timestamp(validated[phase]["observed_at_utc"], label=phase) for phase in CURRENT_OPEN_PHASES]
    if any(left >= right for left, right in zip(timestamps, timestamps[1:])):
        raise CurrentSealRecoveryError("current open phases are not strictly ordered")
    return validated


def _attestation_matches(
    attestation: Any,
    *,
    expected_size: int,
    expected_sha256: str,
    label: str,
) -> None:
    value = _mapping(attestation, label=label)
    if _positive_int(value.get("size_bytes"), label=f"{label}.size_bytes") != expected_size:
        raise CurrentSealRecoveryError(f"{label} size does not match current recovery")
    if _sha256(value.get("sha256"), label=f"{label}.sha256") != expected_sha256:
        raise CurrentSealRecoveryError(f"{label} SHA-256 does not match current recovery")


def validate_current_seal_recovery(
    recovery: dict[str, Any],
    *,
    receipt: dict[str, Any],
    receipt_sha256: str,
    room_id: str,
    date: str,
    relative: str,
    row: dict[str, Any],
    source_flv: Path,
    source_xml: Path,
    source_attestation: dict[str, Any] | None = None,
    xml_attestation: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Validate one independent current proof against one immutable receipt row."""

    if recovery.get("schema") != CURRENT_SEAL_RECOVERY_SCHEMA:
        raise CurrentSealRecoveryError("current recovery schema mismatch")
    if recovery.get("authority") != CURRENT_SEAL_RECOVERY_AUTHORITY:
        raise CurrentSealRecoveryError("current recovery authority mismatch")
    if recovery.get("evidence_scope") != "current_seal_recovery":
        raise CurrentSealRecoveryError("current recovery evidence scope is invalid")
    if _sha256(recovery.get("historical_receipt_sha256"), label="historical_receipt_sha256") != receipt_sha256:
        raise CurrentSealRecoveryError("current recovery is bound to another receipt")
    if not room_id.isdigit() or recovery.get("room_id") != room_id:
        raise CurrentSealRecoveryError("current recovery room identity mismatch")
    if recovery.get("date") != date or recovery.get("source_relative") != relative:
        raise CurrentSealRecoveryError("current recovery source identity mismatch")
    if source_xml != source_flv.with_suffix(".xml"):
        raise CurrentSealRecoveryError("current recovery input XML is not the same-stem sidecar")

    native = _mapping(recovery.get("native_source"), label="native_source")
    if native.get("path") != row.get("source") or native.get("xml_path") != row.get("xml"):
        raise CurrentSealRecoveryError("current recovery native source path mismatch")
    if _sha256(native.get("worker_sha256"), label="native_source.worker_sha256") is None:
        raise CurrentSealRecoveryError("current recovery worker identity is missing")
    worker_path = native.get("worker_path")
    if not isinstance(worker_path, str) or PurePosixPath(worker_path).name != "archive_115_worker.py":
        raise CurrentSealRecoveryError("current recovery worker path is invalid")
    state_path = native.get("state_path")
    if state_path != receipt.get("state_path"):
        raise CurrentSealRecoveryError("current recovery native state path mismatch")
    _sha256(native.get("state_sha256"), label="native_source.state_sha256")

    historical = _mapping(recovery.get("historical_row"), label="historical_row")
    if historical.get("row_sha256") != canonical_json_sha256(row):
        raise CurrentSealRecoveryError("current recovery historical row identity mismatch")
    for key in ("status", "attempts", "source_size_bytes", "source_sha256", "xml_sha256"):
        if historical.get(key) != row.get(key):
            raise CurrentSealRecoveryError(f"current recovery historical row mismatch: {key}")
    if historical.get("status") != "fuse_copy_complete":
        raise CurrentSealRecoveryError("current recovery historical row is not fuse_copy_complete")
    if any(field in row for field in ("open_exclusion_before_copy", "open_exclusion_before_stability", "open_exclusion_after_copy", "open_exclusion_after_media", "open_exclusion_after_stability")):
        raise CurrentSealRecoveryError("current recovery cannot mix current evidence with historical open fields")

    source_size = _positive_int(row.get("source_size_bytes"), label="row.source_size_bytes")
    xml_copy = _mapping(row.get("xml_copy"), label="row.xml_copy")
    xml_size = _positive_int(xml_copy.get("target_size_bytes"), label="row.xml_copy.target_size_bytes")
    source_sha = _sha256(row.get("source_sha256"), label="row.source_sha256")
    xml_sha = _sha256(row.get("xml_sha256"), label="row.xml_sha256")
    current_hash = _mapping(recovery.get("current_hash"), label="current_hash")
    hash_before = _mapping(current_hash.get("before"), label="current_hash.before")
    hash_after = _mapping(current_hash.get("after"), label="current_hash.after")
    hash_before_normalized = {
        "flv": _stat(hash_before.get("flv"), label="current_hash.before.flv"),
        "xml": _stat(hash_before.get("xml"), label="current_hash.before.xml"),
    }
    hash_after_normalized = {
        "flv": _stat(hash_after.get("flv"), label="current_hash.after.flv"),
        "xml": _stat(hash_after.get("xml"), label="current_hash.after.xml"),
    }
    if hash_before_normalized != hash_after_normalized:
        raise CurrentSealRecoveryError("source/XML changed during current hash window")
    if _positive_int(current_hash.get("flv_size_bytes"), label="current_hash.flv_size_bytes") != source_size:
        raise CurrentSealRecoveryError("current FLV hash size mismatch")
    if _positive_int(current_hash.get("xml_size_bytes"), label="current_hash.xml_size_bytes") != xml_size:
        raise CurrentSealRecoveryError("current XML hash size mismatch")
    if _sha256(current_hash.get("flv_sha256"), label="current_hash.flv_sha256") != source_sha:
        raise CurrentSealRecoveryError("current FLV hash does not match historical row")
    if _sha256(current_hash.get("xml_sha256"), label="current_hash.xml_sha256") != xml_sha:
        raise CurrentSealRecoveryError("current XML hash does not match historical row")

    admission_mode = recovery.get("admission_mode")
    if admission_mode not in {"idle", "active"}:
        raise CurrentSealRecoveryError("current recovery admission_mode is invalid")
    current_open = _validate_current_open_exclusions(
        recovery.get("current_open_exclusions"),
        relative=relative,
        source_flv=source_flv,
        admission_mode=admission_mode,
    )
    stability = _validate_stability(
        recovery.get("current_stability"),
        source_size_bytes=source_size,
        xml_size_bytes=xml_size,
        current_open_exclusions=current_open,
    )
    if hash_before_normalized != stability["after"]:
        raise CurrentSealRecoveryError("current hash window is not bound to the stable pair")
    xml_parse = _mapping(recovery.get("current_xml_parse"), label="current_xml_parse")
    if xml_parse.get("root_tag") != "i":
        raise CurrentSealRecoveryError("current XML root is not BililiveRecorder <i>")
    expected_counts = _mapping(row.get("xml_parse"), label="row.xml_parse").get("event_counts")
    expected_counts = _mapping(expected_counts, label="row.xml_parse.event_counts")
    counts = _mapping(xml_parse.get("event_counts"), label="current_xml_parse.event_counts")
    for key in ("d", "gift", "guard", "sc"):
        if _positive_int(counts.get(key, 0), label=f"current_xml_parse.event_counts.{key}") != _positive_int(expected_counts.get(key, 0), label=f"row.xml_parse.event_counts.{key}"):
            raise CurrentSealRecoveryError(f"current XML event count mismatch: {key}")
    if source_attestation is not None:
        _attestation_matches(source_attestation, expected_size=source_size, expected_sha256=source_sha, label="OCI3 source")
    if xml_attestation is not None:
        _attestation_matches(xml_attestation, expected_size=xml_size, expected_sha256=xml_sha, label="OCI3 XML")
    return {
        "schema": CURRENT_SEAL_RECOVERY_SCHEMA,
        "sha256_bound_to_receipt": receipt_sha256,
        "current_source_sha256": source_sha,
        "current_xml_sha256": xml_sha,
        "current_source_size_bytes": source_size,
        "current_xml_size_bytes": xml_size,
        "current_open_exclusions": current_open,
        "current_stability": stability,
        "current_xml_parse": {"root_tag": "i", "event_counts": {key: int(counts.get(key, 0)) for key in ("d", "gift", "guard", "sc")}},
    }


def read_current_seal_recovery(path: Path, *, expected_sha256: str) -> tuple[dict[str, Any], str]:
    """Read a bounded immutable current proof without changing any state."""

    path = Path(path)
    expected = _sha256(expected_sha256, label="expected current recovery SHA-256")
    try:
        before = path.stat()
        before_is_symlink = path.is_symlink()
        before_is_file = path.is_file()
        if before_is_symlink or not before_is_file or before.st_size > MAX_RECOVERY_BYTES:
            raise CurrentSealRecoveryError("current recovery is not a bounded regular file")
        raw = path.read_bytes()
        after = path.stat()
        after_is_symlink = path.is_symlink()
        after_is_file = path.is_file()
    except OSError as exc:
        raise CurrentSealRecoveryError("current recovery cannot be read") from exc
    actual = hashlib.sha256(raw).hexdigest()
    if (
        before_is_symlink != after_is_symlink
        or before_is_file != after_is_file
        or after_is_symlink
        or not after_is_file
        or after.st_size > MAX_RECOVERY_BYTES
        or _stable_file_identity(before) != _stable_file_identity(after)
        or actual != expected
    ):
        raise CurrentSealRecoveryError("current recovery changed or SHA-256 mismatched")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CurrentSealRecoveryError("current recovery is not UTF-8 JSON") from exc
    if not isinstance(payload, dict):
        raise CurrentSealRecoveryError("current recovery root must be an object")
    return payload, actual
