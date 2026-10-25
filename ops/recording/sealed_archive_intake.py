"""Import one naturally sealed 115 archive pair into a local recorder date root.

This is deliberately separate from the BililiveRecorder webhook adapter.  The
archive worker's immutable close row is an external source authority; it is
never converted into a synthetic ``FileClosed`` event.  The only media
producer reused here is ``finalize_recording``.
"""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
from typing import Any, Sequence
import uuid

from ops.recording import bililive_recorder_adapter as adapter
from ops.recording import current_seal_recovery
from ops.recording.recording_topology import load_topology


SEALED_ARCHIVE_AUTHORITY = "N100_115_ARCHIVE_STATE_NATURAL_SEAL"
_DATE_RX = re.compile(r"20\d{2}-\d{2}-\d{2}\Z")
_SHA256_RX = re.compile(r"[0-9a-fA-F]{64}\Z")
_FILENAME_RX = re.compile(
    r"(?P<room>\d+)_(?P<stamp>20\d{6}-\d{2}-\d{2}-\d{2})\.flv\Z"
)
_OPEN_EXCLUSION_FIELDS = (
    "open_exclusion_before_copy",
    "open_exclusion_before_stability",
    "open_exclusion_after_copy",
    "open_exclusion_after_media",
    "open_exclusion_after_stability",
)


class SealedArchiveIntakeError(RuntimeError):
    """A sealed archive intake contract failed closed."""


# Source-selection rule (docs/pipeline/10-source-recording.md, 取源规则): an
# OCI3 self-recording of an interval has priority; the N100 115 archive only
# fills gaps; one session piece is never imported twice.  These OCI3 adapter
# ledgers are consulted read-only by default (CLI) besides the target state.
DEFAULT_COVERAGE_STATE_PATHS = load_topology().coverage_states
_SHANGHAI = timezone(timedelta(hours=8))


def _stem_start(filename: str) -> datetime:
    match = _FILENAME_RX.fullmatch(filename)
    if match is None:
        raise SealedArchiveIntakeError("source filename has no compact start timestamp")
    return datetime.strptime(match.group("stamp"), "%Y%m%d-%H-%M-%S").replace(tzinfo=_SHANGHAI)


def _coverage_conflicts(
    state: dict[str, Any],
    *,
    label: str,
    relative: str,
    source_sha256: str,
    interval: tuple[datetime, datetime],
    is_target: bool,
) -> list[str]:
    """Return the rule violations one adapter ledger shows for this N100 piece.

    * another ledger row already carries the same N100 source bytes, or (in a
      non-target ledger) the same relative piece was already imported:
      DUPLICATE_IMPORT;
    * an OCI3 native recorder file (webhook-journal evidence; imports never
      write webhook rows) overlaps the piece in time: OCI3_SELF_RECORDING_PRIORITY.
      Typed-ignored connection stubs are not coverage.
    """

    conflicts: list[str] = []
    finalized = state.get("finalized")
    webhook_files = state.get("webhook_files") or {}
    dispositions = state.get("source_dispositions") or {}
    if not isinstance(finalized, dict) or not isinstance(webhook_files, dict):
        raise SealedArchiveIntakeError(f"coverage ledger is malformed: {label}")
    for key, row in finalized.items():
        if not isinstance(row, dict):
            continue
        same_key = key == relative
        if is_target and same_key:
            continue  # same-key provenance is _validate_existing_ledger's job
        if row.get("source_sha256") == source_sha256 or (
            same_key and row.get("source_authority") == SEALED_ARCHIVE_AUTHORITY
        ):
            conflicts.append(f"DUPLICATE_IMPORT: {label} already finalized {key}")
    start, end = interval
    for key, evidence in webhook_files.items():
        if not isinstance(evidence, dict) or not evidence.get("opening_event_id"):
            continue
        disposition = dispositions.get(key) if isinstance(dispositions, dict) else None
        if isinstance(disposition, dict) and str(disposition.get("status", "")).startswith("IGNORED"):
            continue
        try:
            opened = adapter._parse_webhook_datetime(evidence.get("file_open_time"), field="FileOpenTime")
            closed = (
                adapter._parse_webhook_datetime(evidence.get("file_close_time"), field="FileCloseTime")
                if evidence.get("status") == "CLOSED"
                else datetime.max.replace(tzinfo=timezone.utc)
            )
        except adapter.AdapterError as exc:
            raise SealedArchiveIntakeError(f"coverage ledger time is invalid: {label} {key}") from exc
        if opened < end and start < closed:
            conflicts.append(
                f"OCI3_SELF_RECORDING_PRIORITY: {label} native {key} overlaps this N100 piece"
            )
    return conflicts


def _sha256_value(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or _SHA256_RX.fullmatch(value) is None:
        raise SealedArchiveIntakeError(f"{label} must be a plain SHA-256 hex string")
    return value.lower()


def _positive_int(value: Any, *, label: str) -> int:
    if isinstance(value, bool):
        raise SealedArchiveIntakeError(f"{label} must be a positive integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise SealedArchiveIntakeError(f"{label} must be a positive integer") from exc
    if parsed <= 0:
        raise SealedArchiveIntakeError(f"{label} must be a positive integer")
    return parsed


def _mapping(value: Any, *, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise SealedArchiveIntakeError(f"{label} must be an object")
    return value


def _read_immutable_json(
    path: Path, *, expected_sha256: str, label: str = "receipt"
) -> tuple[dict[str, Any], str]:
    expected = _sha256_value(expected_sha256, label=f"expected {label} SHA-256")
    try:
        before = adapter._attest_regular_file(path)
    except adapter.AdapterError as exc:
        raise SealedArchiveIntakeError(f"{label} is not a stable regular file: {path}") from exc
    if before["size_bytes"] > 16 * 1024 * 1024:
        raise SealedArchiveIntakeError(f"{label} is unexpectedly large")
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise SealedArchiveIntakeError(f"cannot read {label}: {path}") from exc
    try:
        after = adapter._attest_regular_file(path)
    except adapter.AdapterError as exc:
        raise SealedArchiveIntakeError(f"{label} changed while reading: {path}") from exc
    actual = hashlib.sha256(raw).hexdigest()
    if before != after or actual != before["sha256"]:
        raise SealedArchiveIntakeError(f"{label} changed while reading")
    if actual != expected:
        raise SealedArchiveIntakeError(f"{label} SHA-256 does not match the expected identity")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SealedArchiveIntakeError(f"{label} is not valid UTF-8 JSON") from exc
    if not isinstance(payload, dict):
        raise SealedArchiveIntakeError(f"{label} root must be an object")
    return payload, actual


def _validate_path_identity(
    raw_path: Any,
    *,
    label: str,
    room_id: str,
    date: str,
    filename: str,
    require_115: bool,
) -> None:
    if not isinstance(raw_path, str) or not raw_path:
        raise SealedArchiveIntakeError(f"receipt {label} path is missing")
    path = PurePosixPath(raw_path)
    parts = path.parts
    if path.name != filename or len(parts) < 4:
        raise SealedArchiveIntakeError(f"receipt {label} path does not bind the exact filename")
    if parts[-2] != date or parts[-3] != room_id:
        raise SealedArchiveIntakeError(f"receipt {label} path has the wrong room/date")
    if require_115 and "115open" not in parts:
        raise SealedArchiveIntakeError(f"receipt {label} path is not an 115 source path")


def _validate_open_exclusions(
    row: dict[str, Any],
    *,
    relative: str,
    source_flv: Path,
    current_open_exclusions: dict[str, dict[str, Any]] | None = None,
) -> None:
    if current_open_exclusions is not None:
        if any(field in row for field in _OPEN_EXCLUSION_FIELDS):
            raise SealedArchiveIntakeError(
                "current seal recovery cannot be mixed with historical open exclusions"
            )
        if set(current_open_exclusions) != set(current_seal_recovery.CURRENT_OPEN_PHASES):
            raise SealedArchiveIntakeError("current seal recovery open phases are incomplete")
        # current_seal_recovery.validate_current_seal_recovery has already
        # performed the phase-level checks.  Retain this seam in the receipt
        # validator so the normal row contract consumes the separate proof.
        return
    current_names = {source_flv.name, source_flv.stem}
    for field in _OPEN_EXCLUSION_FIELDS:
        entry = _mapping(row.get(field), label=f"receipt row {field}")
        if entry.get("source_relative") != relative:
            raise SealedArchiveIntakeError(f"receipt {field} is bound to another source")
        open_relatives = entry.get("open_relatives")
        if not isinstance(open_relatives, list):
            raise SealedArchiveIntakeError(f"receipt {field}.open_relatives must be a list")
        for item in open_relatives:
            if not isinstance(item, str) or not item:
                raise SealedArchiveIntakeError(f"receipt {field} contains an invalid open path")
            candidate = PurePosixPath(item)
            if candidate.name in current_names or item == relative:
                raise SealedArchiveIntakeError(
                    f"receipt {field} still lists the target source as open"
                )


def _validate_stable_pair(
    row: dict[str, Any], *, source_size: int, xml_size: int, source_sha256: str, xml_sha256: str
) -> None:
    stable_pair = _mapping(row.get("stable_pair"), label="receipt row stable_pair")
    for phase in ("before", "after"):
        snapshot = _mapping(stable_pair.get(phase), label=f"receipt stable_pair.{phase}")
        flv = _mapping(snapshot.get("flv"), label=f"receipt stable_pair.{phase}.flv")
        xml = _mapping(snapshot.get("xml"), label=f"receipt stable_pair.{phase}.xml")
        if _positive_int(flv.get("size_bytes"), label=f"stable_pair.{phase}.flv.size_bytes") != source_size:
            raise SealedArchiveIntakeError("receipt stable FLV size does not match its copy hash")
        if _positive_int(xml.get("size_bytes"), label=f"stable_pair.{phase}.xml.size_bytes") != xml_size:
            raise SealedArchiveIntakeError("receipt stable XML size does not match its copy hash")
    if _mapping(row.get("flv_copy"), label="receipt row flv_copy").get("target_sha256") != source_sha256:
        raise SealedArchiveIntakeError("receipt FLV copy hash is inconsistent")
    if _mapping(row.get("xml_copy"), label="receipt row xml_copy").get("target_sha256") != xml_sha256:
        raise SealedArchiveIntakeError("receipt XML copy hash is inconsistent")


def _validate_media_and_xml_counts(row: dict[str, Any]) -> dict[str, int]:
    media = _mapping(row.get("media_verification"), label="receipt row media_verification")
    exact_values = {
        "ffprobe_rc": 0,
        "full_av_decode_rc": 0,
        "video_stream_count": 1,
        "valid_video_geometry": True,
        "ffprobe_stderr_present": False,
        "decode_stderr_present": False,
    }
    for key, expected in exact_values.items():
        if media.get(key) != expected:
            raise SealedArchiveIntakeError(f"receipt media verification failed: {key}")

    xml_parse = _mapping(row.get("xml_parse"), label="receipt row xml_parse")
    if xml_parse.get("root_tag") != "i":
        raise SealedArchiveIntakeError("receipt XML root is not BililiveRecorder <i>")
    counts = _mapping(xml_parse.get("event_counts"), label="receipt XML event_counts")
    normalized: dict[str, int] = {}
    for key in ("d", "gift", "guard", "sc"):
        value = counts.get(key, 0)
        if isinstance(value, bool):
            raise SealedArchiveIntakeError(f"receipt XML event count is invalid: {key}")
        try:
            parsed = int(value)
        except (TypeError, ValueError) as exc:
            raise SealedArchiveIntakeError(f"receipt XML event count is invalid: {key}") from exc
        if parsed < 0:
            raise SealedArchiveIntakeError(f"receipt XML event count is negative: {key}")
        normalized[key] = parsed
    return normalized


def _validate_receipt_row(
    receipt: dict[str, Any],
    *,
    room_id: str,
    date: str,
    source_flv: Path,
    source_xml: Path,
    receipt_sha256: str | None = None,
    current_recovery: dict[str, Any] | None = None,
) -> tuple[str, dict[str, Any], dict[str, int], dict[str, Any] | None]:
    if _DATE_RX.fullmatch(date) is None:
        raise SealedArchiveIntakeError("date must be YYYY-MM-DD")
    if not room_id.isdigit() or not room_id:
        raise SealedArchiveIntakeError("room_id must be numeric")
    if source_flv.suffix.lower() != ".flv" or source_xml.suffix.lower() != ".xml":
        raise SealedArchiveIntakeError("sealed intake requires an FLV/XML pair")
    if receipt.get("state_source") != "unique existing native archive worker":
        raise SealedArchiveIntakeError("receipt is not from the unique native archive worker")
    state_source = receipt.get("state_path")
    if not isinstance(state_source, str) or PurePosixPath(state_source).name != "archive-state.json":
        raise SealedArchiveIntakeError("receipt does not identify an archive-state.json source")
    expected_xml = source_flv.with_suffix(".xml")
    if source_xml != expected_xml:
        raise SealedArchiveIntakeError("source XML must be the exact same-stem sidecar")
    match = _FILENAME_RX.fullmatch(source_flv.name)
    if match is None or match.group("room") != room_id:
        raise SealedArchiveIntakeError("source filename is not bound to the requested room")
    stamp = match.group("stamp")
    if f"{stamp[:4]}-{stamp[4:6]}-{stamp[6:8]}" != date:
        raise SealedArchiveIntakeError("source filename date does not match the requested date")
    _validate_path_identity(
        str(source_flv),
        label="input source_flv",
        room_id=room_id,
        date=date,
        filename=source_flv.name,
        require_115=True,
    )
    _validate_path_identity(
        str(source_xml),
        label="input source_xml",
        room_id=room_id,
        date=date,
        filename=source_xml.name,
        require_115=True,
    )
    relative = f"{date}/{source_flv.name}"
    rows = _mapping(receipt.get("rows"), label="receipt rows")
    row = _mapping(rows.get(relative), label=f"receipt row {relative}")
    if row.get("status") != "fuse_copy_complete":
        raise SealedArchiveIntakeError("receipt row is not fuse_copy_complete")
    if _positive_int(row.get("attempts"), label="receipt row attempts") < 1:
        raise SealedArchiveIntakeError("receipt row has no completed attempt")

    source_size = _positive_int(row.get("source_size_bytes"), label="receipt source_size_bytes")
    xml_copy = _mapping(row.get("xml_copy"), label="receipt row xml_copy")
    flv_copy = _mapping(row.get("flv_copy"), label="receipt row flv_copy")
    xml_size = _positive_int(xml_copy.get("target_size_bytes"), label="receipt XML target_size_bytes")
    source_sha256 = _sha256_value(row.get("source_sha256"), label="receipt source_sha256")
    xml_sha256 = _sha256_value(row.get("xml_sha256"), label="receipt xml_sha256")
    if flv_copy.get("status") != "copied" or xml_copy.get("status") != "copied":
        raise SealedArchiveIntakeError("receipt source/XML copy is not complete")
    if _positive_int(flv_copy.get("target_size_bytes"), label="receipt FLV target_size_bytes") != source_size:
        raise SealedArchiveIntakeError("receipt FLV copy size does not match source_size_bytes")
    if _positive_int(xml_copy.get("target_size_bytes"), label="receipt XML target_size_bytes") != xml_size:
        raise SealedArchiveIntakeError("receipt XML copy size is inconsistent")
    if _sha256_value(flv_copy.get("target_sha256"), label="receipt FLV target_sha256") != source_sha256:
        raise SealedArchiveIntakeError("receipt FLV copy SHA-256 does not match source_sha256")
    if _sha256_value(xml_copy.get("target_sha256"), label="receipt XML target_sha256") != xml_sha256:
        raise SealedArchiveIntakeError("receipt XML copy SHA-256 does not match xml_sha256")

    _validate_path_identity(
        row.get("source"),
        label="source",
        room_id=room_id,
        date=date,
        filename=source_flv.name,
        require_115=False,
    )
    _validate_path_identity(
        row.get("xml"),
        label="xml",
        room_id=room_id,
        date=date,
        filename=source_xml.name,
        require_115=False,
    )
    _validate_path_identity(
        row.get("target_flv"),
        label="target_flv",
        room_id=room_id,
        date=date,
        filename=source_flv.name,
        require_115=True,
    )
    _validate_path_identity(
        row.get("target_xml"),
        label="target_xml",
        room_id=room_id,
        date=date,
        filename=source_xml.name,
        require_115=True,
    )
    current_binding: dict[str, Any] | None = None
    if current_recovery is not None:
        if receipt_sha256 is None:
            raise SealedArchiveIntakeError("current seal recovery requires receipt SHA-256")
        try:
            current_binding = current_seal_recovery.validate_current_seal_recovery(
                current_recovery,
                receipt=receipt,
                receipt_sha256=receipt_sha256,
                room_id=room_id,
                date=date,
                relative=relative,
                row=row,
                source_flv=source_flv,
                source_xml=source_xml,
            )
        except current_seal_recovery.CurrentSealRecoveryError as exc:
            raise SealedArchiveIntakeError(f"current seal recovery rejected: {exc}") from exc
    _validate_open_exclusions(
        row,
        relative=relative,
        source_flv=source_flv,
        current_open_exclusions=(current_binding or {}).get("current_open_exclusions"),
    )
    _validate_stable_pair(
        row,
        source_size=source_size,
        xml_size=xml_size,
        source_sha256=source_sha256,
        xml_sha256=xml_sha256,
    )
    event_counts = _validate_media_and_xml_counts(row)
    return relative, row, event_counts, current_binding


def _validate_xml_binding(
    source_xml: Path,
    *,
    room_id: str,
    date: str,
    source_flv: Path,
    expected_counts: dict[str, int],
) -> dict[str, Any]:
    try:
        jsonl_bytes, record_info, event_count = adapter.xml_to_jsonl(source_xml)
    except adapter.AdapterError as exc:
        raise SealedArchiveIntakeError("XML is not a valid BililiveRecorder sidecar") from exc
    if record_info.get("roomid") != room_id:
        raise SealedArchiveIntakeError("XML RecordInfo roomid does not match the source room")
    raw_start = record_info.get("start_time")
    if not raw_start:
        raise SealedArchiveIntakeError("XML RecordInfo lacks fractional start_time")
    try:
        parsed_start = datetime.fromisoformat(raw_start)
    except ValueError as exc:
        raise SealedArchiveIntakeError("XML RecordInfo start_time is invalid") from exc
    if parsed_start.tzinfo is None:
        raise SealedArchiveIntakeError("XML RecordInfo start_time lacks timezone")
    match = _FILENAME_RX.fullmatch(source_flv.name)
    if match is None:
        raise SealedArchiveIntakeError("source filename binding disappeared")
    expected_start = f"{date}T{match.group('stamp')[9:].replace('-', ':')}"
    if parsed_start.strftime("%Y-%m-%dT%H:%M:%S") != expected_start:
        raise SealedArchiveIntakeError("XML RecordInfo start_time does not match source stem")

    command_counts = Counter()
    try:
        for line in jsonl_bytes.splitlines():
            command = json.loads(line.decode("utf-8")).get("cmd")
            if isinstance(command, str):
                command_counts[command] += 1
    except (UnicodeDecodeError, json.JSONDecodeError, AttributeError) as exc:
        raise SealedArchiveIntakeError("XML JSONL projection is invalid") from exc
    expected_commands = {
        adapter.EVENT_COMMANDS[key]: value for key, value in expected_counts.items()
    }
    for command, expected in expected_commands.items():
        if command_counts.get(command, 0) != expected:
            raise SealedArchiveIntakeError(
                f"XML event count drift for {command}: {command_counts.get(command, 0)} != {expected}"
            )
    if event_count != sum(expected_counts.values()):
        raise SealedArchiveIntakeError("XML event total does not match archive receipt")
    return {
        "record_info": record_info,
        "event_count": event_count,
        "event_counts": expected_counts,
        "jsonl_sha256": hashlib.sha256(jsonl_bytes).hexdigest(),
    }


def _validate_output_separation(source_flv: Path, output_dir: Path, *, date: str) -> None:
    if output_dir.name != date:
        raise SealedArchiveIntakeError("local output directory must be the requested date root")
    if "115open" in output_dir.parts:
        raise SealedArchiveIntakeError("local output directory must not be under the 115 source route")
    source_parent = Path(os.path.realpath(source_flv.parent))
    output_real = Path(os.path.realpath(output_dir))
    if "115open" in output_real.parts:
        raise SealedArchiveIntakeError("local output directory resolves to the 115 source route")
    try:
        output_real.relative_to(source_parent)
    except ValueError:
        pass
    else:
        raise SealedArchiveIntakeError("local output directory overlaps the cloud source directory")


def _copy_original_xml_create_only(
    source_xml: Path,
    local_xml: Path,
    *,
    expected_attestation: dict[str, Any],
) -> dict[str, Any]:
    """Expose the validated XML bytes locally without replacing a target.

    The sidecar is deliberately copied byte-for-byte from the sealed source.
    ``xml_to_jsonl`` already validated the source, but the raw XML remains the
    authority consumed by the normal danmaku discovery paths.  The source is
    re-attested immediately before publishing so a source drift cannot be
    turned into a locally valid-looking sidecar.
    """

    expected_size = expected_attestation.get("size_bytes")
    expected_sha256 = expected_attestation.get("sha256")
    if not isinstance(expected_size, int) or isinstance(expected_size, bool):
        raise SealedArchiveIntakeError("validated XML size is malformed")
    if not isinstance(expected_sha256, str):
        raise SealedArchiveIntakeError("validated XML SHA-256 is malformed")

    if local_xml.is_symlink():
        raise SealedArchiveIntakeError("local XML sidecar must not be a symlink")
    if local_xml.exists():
        if not local_xml.is_file():
            raise SealedArchiveIntakeError("local XML sidecar is not a regular file")
        try:
            current = adapter._attest_regular_file(local_xml)
        except adapter.AdapterError as exc:
            raise SealedArchiveIntakeError("local XML sidecar is not a stable regular file") from exc
        if (
            current["size_bytes"] != expected_size
            or current["sha256"] != expected_sha256
        ):
            raise SealedArchiveIntakeError("local XML sidecar hash or size conflicts with source")
        return current

    try:
        source_before_publish = adapter._attest_regular_file(source_xml)
    except adapter.AdapterError as exc:
        raise SealedArchiveIntakeError("source XML is not stable before local sidecar copy") from exc
    if source_before_publish != expected_attestation:
        raise SealedArchiveIntakeError("source XML changed before local sidecar copy")

    staging_dir = local_xml.parent / ".brec-adapter-staging"
    staging_dir.mkdir(mode=0o700, exist_ok=True)
    staged = staging_dir / f".{local_xml.name}.{uuid.uuid4().hex}.xml"
    try:
        with source_xml.open("rb") as source_handle, staged.open("xb") as staged_handle:
            shutil.copyfileobj(source_handle, staged_handle, length=1024 * 1024)
            staged_handle.flush()
            os.fsync(staged_handle.fileno())
        try:
            source_after_copy = adapter._attest_regular_file(source_xml)
        except adapter.AdapterError as exc:
            raise SealedArchiveIntakeError("source XML became unstable during local sidecar copy") from exc
        if source_after_copy != expected_attestation:
            raise SealedArchiveIntakeError("source XML changed during local sidecar copy")
        try:
            adapter._publish_path_noreplace(staged, local_xml)
        except adapter.AdapterError:
            # A concurrent publisher may have won the no-clobber race.  Only
            # accept it when its bytes are exactly the already-attested source.
            if local_xml.is_symlink():
                raise SealedArchiveIntakeError("local XML sidecar must not be a symlink")
            if not local_xml.exists() or not local_xml.is_file():
                raise
            try:
                current = adapter._attest_regular_file(local_xml)
            except adapter.AdapterError as exc:
                raise SealedArchiveIntakeError(
                    "local XML sidecar publish conflicted with an unstable target"
                ) from exc
            if (
                current["size_bytes"] != expected_size
                or current["sha256"] != expected_sha256
            ):
                raise SealedArchiveIntakeError("local XML sidecar hash or size conflicts with source")
            return current
        try:
            published = adapter._attest_regular_file(local_xml)
        except adapter.AdapterError as exc:
            raise SealedArchiveIntakeError("published local XML sidecar is not stable") from exc
        if (
            published["size_bytes"] != expected_size
            or published["sha256"] != expected_sha256
        ):
            raise SealedArchiveIntakeError("published local XML sidecar does not match source")
        return published
    finally:
        try:
            staged.unlink()
        except FileNotFoundError:
            pass


def _validate_existing_ledger(
    row: Any,
    *,
    source_path: str,
    source_sha256: str,
    xml_sha256: str,
    receipt_path: str,
    receipt_sha256: str,
    target: str,
    local_xml_path: str,
    current_seal_recovery_path: str | None = None,
    current_seal_recovery_sha256: str | None = None,
) -> dict[str, Any] | None:
    if row is None:
        return None
    if not isinstance(row, dict):
        raise SealedArchiveIntakeError("existing finalized ledger row is malformed")
    required = {
        "source_path": source_path,
        "source_sha256": source_sha256,
        "xml_sha256": xml_sha256,
        "receipt_path": receipt_path,
        "receipt_sha256": receipt_sha256,
        "source_authority": SEALED_ARCHIVE_AUTHORITY,
        "target": target,
    }
    for key, expected in required.items():
        if row.get(key) != expected:
            raise SealedArchiveIntakeError(f"existing finalized ledger provenance conflicts: {key}")
    for key in ("source_size", "source_mtime_ns", "target_sha256", "finalized_at"):
        if key not in row:
            raise SealedArchiveIntakeError(f"existing finalized ledger lacks {key}")
    if "local_xml_path" in row or "local_xml_sha256" in row:
        if row.get("local_xml_path") != local_xml_path or row.get("local_xml_sha256") != xml_sha256:
            raise SealedArchiveIntakeError(
                "existing finalized ledger local XML provenance conflicts"
            )
    if current_seal_recovery_path is not None:
        if (
            row.get("current_seal_recovery_path") != current_seal_recovery_path
            or row.get("current_seal_recovery_sha256") != current_seal_recovery_sha256
        ):
            raise SealedArchiveIntakeError(
                "existing finalized ledger current seal recovery provenance conflicts"
            )
    return row


def intake_sealed_archive(
    receipt_path: Path,
    *,
    expected_receipt_sha256: str,
    room_id: str,
    date: str,
    source_flv: Path,
    source_xml: Path,
    output_dir: Path,
    state_path: Path,
    current_seal_recovery_path: Path | None = None,
    expected_current_seal_recovery_sha256: str | None = None,
    ffmpeg_bin: str = "ffmpeg",
    ffprobe_bin: str = "ffprobe",
    coverage_state_paths: Sequence[Path] = (),
) -> dict[str, Any]:
    """Finalize one exact, naturally sealed 115 pair into a local date root.

    Before any output is written, the target ledger and every read-only
    ``coverage_state_paths`` ledger must show neither an OCI3 self-recording
    overlapping this piece nor an earlier import of it (取源规则).
    """

    receipt_path = Path(receipt_path).absolute()
    source_flv = Path(source_flv).absolute()
    source_xml = Path(source_xml).absolute()
    output_dir = Path(output_dir).absolute()
    state_path = Path(state_path).absolute()
    if (current_seal_recovery_path is None) != (
        expected_current_seal_recovery_sha256 is None
    ):
        raise SealedArchiveIntakeError(
            "current seal recovery path and expected SHA-256 must be supplied together"
        )
    if current_seal_recovery_path is not None:
        current_seal_recovery_path = Path(current_seal_recovery_path).absolute()
    if state_path.is_symlink() or not state_path.is_file():
        raise SealedArchiveIntakeError("existing adapter state path is required")
    _validate_output_separation(source_flv, output_dir, date=date)
    if source_flv.is_symlink() or source_xml.is_symlink():
        raise SealedArchiveIntakeError("source FLV/XML must not be symlinks")

    try:
        lock_fd = adapter._truncated_acquire_state_lock(state_path)
    except adapter.AdapterError as exc:
        raise SealedArchiveIntakeError("adapter state owner lock is unavailable") from exc
    try:
        receipt, receipt_sha256 = _read_immutable_json(
            receipt_path, expected_sha256=expected_receipt_sha256
        )
        current_recovery = None
        current_recovery_sha256 = None
        if current_seal_recovery_path is not None:
            try:
                current_recovery, current_recovery_sha256 = (
                    current_seal_recovery.read_current_seal_recovery(
                        current_seal_recovery_path,
                        expected_sha256=str(expected_current_seal_recovery_sha256),
                    )
                )
            except current_seal_recovery.CurrentSealRecoveryError as exc:
                raise SealedArchiveIntakeError(f"current seal recovery cannot be read: {exc}") from exc
        relative, receipt_row, expected_counts, current_binding = _validate_receipt_row(
            receipt,
            room_id=str(room_id),
            date=date,
            source_flv=source_flv,
            source_xml=source_xml,
            receipt_sha256=receipt_sha256,
            current_recovery=current_recovery,
        )
        source_attestation = adapter._attest_regular_file(source_flv)
        xml_attestation = adapter._attest_regular_file(source_xml)
        expected_source_sha = _sha256_value(
            receipt_row.get("source_sha256"), label="receipt source_sha256"
        )
        expected_xml_sha = _sha256_value(receipt_row.get("xml_sha256"), label="receipt xml_sha256")
        if source_attestation["size_bytes"] != receipt_row["source_size_bytes"]:
            raise SealedArchiveIntakeError("OCI3 source size does not match sealed receipt")
        if source_attestation["sha256"] != expected_source_sha:
            raise SealedArchiveIntakeError("OCI3 source SHA-256 does not match sealed receipt")
        if xml_attestation["size_bytes"] != receipt_row["xml_copy"]["target_size_bytes"]:
            raise SealedArchiveIntakeError("OCI3 XML size does not match sealed receipt")
        if xml_attestation["sha256"] != expected_xml_sha:
            raise SealedArchiveIntakeError("OCI3 XML SHA-256 does not match sealed receipt")
        if current_recovery is not None:
            try:
                current_binding = current_seal_recovery.validate_current_seal_recovery(
                    current_recovery,
                    receipt=receipt,
                    receipt_sha256=receipt_sha256,
                    room_id=str(room_id),
                    date=date,
                    relative=relative,
                    row=receipt_row,
                    source_flv=source_flv,
                    source_xml=source_xml,
                    source_attestation=source_attestation,
                    xml_attestation=xml_attestation,
                )
            except current_seal_recovery.CurrentSealRecoveryError as exc:
                raise SealedArchiveIntakeError(f"current seal recovery rejected: {exc}") from exc
        xml_binding = _validate_xml_binding(
            source_xml,
            room_id=str(room_id),
            date=date,
            source_flv=source_flv,
            expected_counts=expected_counts,
        )
        try:
            piece_duration = float(
                adapter.probe_media(source_flv, ffprobe_bin=ffprobe_bin)["duration_seconds"]
            )
        except adapter.AdapterError as exc:
            raise SealedArchiveIntakeError(f"N100 piece duration is unavailable: {exc}") from exc
        piece_start = _stem_start(source_flv.name)
        interval = (piece_start, piece_start + timedelta(seconds=piece_duration))
        conflicts: list[str] = []
        seen: set[str] = set()
        for label, ledger_path, is_target in [
            ("target", state_path, True),
            *(("coverage", Path(path).absolute(), False) for path in coverage_state_paths),
        ]:
            resolved = os.path.realpath(ledger_path)
            if resolved in seen:
                continue
            seen.add(resolved)
            try:
                ledger_state = json.loads(Path(ledger_path).read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise SealedArchiveIntakeError(f"coverage ledger is unreadable: {ledger_path}") from exc
            if not isinstance(ledger_state, dict):
                raise SealedArchiveIntakeError(f"coverage ledger is malformed: {ledger_path}")
            conflicts.extend(
                _coverage_conflicts(
                    ledger_state,
                    label=f"{label}:{ledger_path}",
                    relative=relative,
                    source_sha256=source_attestation["sha256"],
                    interval=interval,
                    is_target=is_target,
                )
            )
        if conflicts:
            raise SealedArchiveIntakeError(
                "source selection refuses this import: " + "; ".join(sorted(conflicts)[:8])
            )

        output_dir.mkdir(parents=True, exist_ok=True)
        target = output_dir / source_flv.with_suffix(".mp4").name
        local_xml = output_dir / source_xml.name
        if target.is_symlink():
            raise SealedArchiveIntakeError("local MP4 target must not be a symlink")
        if any(
            candidate.is_symlink()
            for candidate in (
                target,
                output_dir / source_flv.with_suffix(".jsonl").name,
                output_dir / source_flv.with_suffix(".meta.json").name,
                local_xml,
            )
        ):
            raise SealedArchiveIntakeError("local finalizer output must not contain symlinks")
        try:
            state = adapter.load_or_initialize_state(
                state_path,
                now_epoch=datetime.now(timezone.utc).timestamp(),
            )
        except adapter.AdapterError as exc:
            raise SealedArchiveIntakeError("adapter state is not loadable") from exc
        finalized = state.get("finalized")
        if not isinstance(finalized, dict):
            raise SealedArchiveIntakeError("adapter finalized ledger is malformed")
        receipt_path_string = str(receipt_path)
        source_path_string = str(source_flv)
        existing = _validate_existing_ledger(
            finalized.get(relative),
            source_path=source_path_string,
            source_sha256=source_attestation["sha256"],
            xml_sha256=xml_attestation["sha256"],
            receipt_path=receipt_path_string,
            receipt_sha256=receipt_sha256,
            target=str(target),
            local_xml_path=str(local_xml),
            current_seal_recovery_path=(
                str(current_seal_recovery_path) if current_seal_recovery_path is not None else None
            ),
            current_seal_recovery_sha256=current_recovery_sha256,
        )
        if target.exists() and existing is None:
            raise SealedArchiveIntakeError(
                "local MP4 target exists without matching finalized ledger; refusing implicit success"
            )
        local_xml_attestation = _copy_original_xml_create_only(
            source_xml,
            local_xml,
            expected_attestation=xml_attestation,
        )

        try:
            result = adapter.finalize_recording(
                source_flv,
                ffmpeg_bin=ffmpeg_bin,
                ffprobe_bin=ffprobe_bin,
                existing_ledger=existing,
                output_dir=output_dir,
            )
        except (adapter.AdapterError, OSError) as exc:
            raise SealedArchiveIntakeError(f"sealed source finalization failed: {exc}") from exc

        source_after = adapter._attest_regular_file(source_flv)
        xml_after = adapter._attest_regular_file(source_xml)
        local_xml_after = adapter._attest_regular_file(local_xml)
        if source_after != source_attestation or xml_after != xml_attestation:
            raise SealedArchiveIntakeError("source or XML changed during finalization")
        if local_xml_after["size_bytes"] != xml_attestation["size_bytes"] or local_xml_after[
            "sha256"
        ] != xml_attestation["sha256"]:
            raise SealedArchiveIntakeError("local XML sidecar changed during finalization")
        _read_immutable_json(receipt_path, expected_sha256=receipt_sha256)
        if current_seal_recovery_path is not None:
            try:
                current_seal_recovery.read_current_seal_recovery(
                    current_seal_recovery_path,
                    expected_sha256=str(current_recovery_sha256),
                )
            except current_seal_recovery.CurrentSealRecoveryError as exc:
                raise SealedArchiveIntakeError(
                    f"current seal recovery changed during finalization: {exc}"
                ) from exc
        target_attestation = adapter._attest_regular_file(target)
        if target_attestation["sha256"] != result.get("target_sha256"):
            raise SealedArchiveIntakeError("finalizer target SHA-256 is inconsistent")

        ledger_row = dict(existing or {})
        ledger_row.update(
            {
                "source_size": source_attestation["size_bytes"],
                "source_mtime_ns": source_attestation["mtime_ns"],
                "target": str(target),
                "target_sha256": target_attestation["sha256"],
                "finalized_at": (
                    existing.get("finalized_at")
                    if existing is not None
                    else datetime.now(timezone.utc).isoformat()
                ),
                "source_path": source_path_string,
                "source_sha256": source_attestation["sha256"],
                "xml_sha256": xml_attestation["sha256"],
                "receipt_path": receipt_path_string,
                "receipt_sha256": receipt_sha256,
                "source_authority": SEALED_ARCHIVE_AUTHORITY,
                "local_xml_path": str(local_xml),
                "local_xml_sha256": local_xml_attestation["sha256"],
            }
        )
        if current_seal_recovery_path is not None:
            ledger_row.update(
                {
                    "current_seal_recovery_path": str(current_seal_recovery_path),
                    "current_seal_recovery_sha256": current_recovery_sha256,
                    "current_seal_recovery_schema": current_binding["schema"]
                    if current_binding is not None
                    else current_seal_recovery.CURRENT_SEAL_RECOVERY_SCHEMA,
                }
            )
        prospective = json.loads(json.dumps(state))
        prospective["finalized"][relative] = ledger_row
        state_updated = prospective != state
        if state_updated:
            adapter.atomic_write_json(state_path, prospective, mode=0o600)
        result = {
            "schema_version": "sealed-archive-intake-result.v1",
            "status": result.get("status"),
            "source_relative": relative,
            "source_path": source_path_string,
            "source_sha256": source_attestation["sha256"],
            "xml_sha256": xml_attestation["sha256"],
            "local_xml_path": str(local_xml),
            "local_xml_sha256": local_xml_attestation["sha256"],
            "receipt_sha256": receipt_sha256,
            "target": str(target),
            "target_sha256": target_attestation["sha256"],
            "event_count": xml_binding["event_count"],
            "superchat_event_count": xml_binding["event_counts"]["sc"],
            "state_updated": state_updated,
            "source_authority": SEALED_ARCHIVE_AUTHORITY,
        }
        if current_seal_recovery_path is not None:
            result.update(
                {
                    "current_seal_recovery_path": str(current_seal_recovery_path),
                    "current_seal_recovery_sha256": current_recovery_sha256,
                    "current_seal_recovery_schema": current_binding["schema"]
                    if current_binding is not None
                    else current_seal_recovery.CURRENT_SEAL_RECOVERY_SCHEMA,
                }
            )
        return result
    finally:
        adapter._truncated_release_state_lock(lock_fd)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--receipt", required=True, type=Path)
    parser.add_argument("--expected-receipt-sha256", required=True)
    parser.add_argument("--room-id", required=True)
    parser.add_argument("--date", required=True)
    parser.add_argument("--source-flv", required=True, type=Path)
    parser.add_argument("--source-xml", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--state-path", required=True, type=Path)
    parser.add_argument("--current-seal-recovery", type=Path)
    parser.add_argument("--expected-current-seal-recovery-sha256")
    parser.add_argument(
        "--coverage-state",
        action="append",
        type=Path,
        default=None,
        help=(
            "read-only OCI3 adapter ledger consulted for self-recording priority and "
            "duplicate imports (repeatable; default: the existing OCI3 ledgers)"
        ),
    )
    parser.add_argument("--ffmpeg", default="ffmpeg")
    parser.add_argument("--ffprobe", default="ffprobe")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        result = intake_sealed_archive(
            args.receipt,
            expected_receipt_sha256=args.expected_receipt_sha256,
            room_id=args.room_id,
            date=args.date,
            source_flv=args.source_flv,
            source_xml=args.source_xml,
            output_dir=args.output_dir,
            state_path=args.state_path,
            current_seal_recovery_path=args.current_seal_recovery,
            expected_current_seal_recovery_sha256=args.expected_current_seal_recovery_sha256,
            ffmpeg_bin=args.ffmpeg,
            ffprobe_bin=args.ffprobe,
            coverage_state_paths=(
                args.coverage_state
                if args.coverage_state is not None
                else [path for path in DEFAULT_COVERAGE_STATE_PATHS if path.is_file()]
            ),
        )
    except (SealedArchiveIntakeError, adapter.AdapterError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False, sort_keys=True))
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
