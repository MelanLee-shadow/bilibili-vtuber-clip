from __future__ import annotations

from datetime import datetime
import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
import re
import stat
from typing import Sequence


@dataclass(frozen=True)
class TimeRange:
    start_ms: int
    end_ms: int

    @property
    def duration_ms(self) -> int:
        return max(0, self.end_ms - self.start_ms)

    def to_manifest(self) -> dict[str, int]:
        return {"start_ms": self.start_ms, "end_ms": self.end_ms, "duration_ms": self.duration_ms}


@dataclass(frozen=True)
class MediaSegmentObservation:
    path: str
    start_ms: int
    end_ms: int
    duration_ms: int
    size_bytes: int
    probed_ok: bool = True

    @property
    def range(self) -> TimeRange:
        return TimeRange(self.start_ms, self.end_ms)

    def to_manifest(self) -> dict[str, object]:
        return {
            "path": self.path,
            "start_ms": self.start_ms,
            "end_ms": self.end_ms,
            "duration_ms": self.duration_ms,
            "size_bytes": self.size_bytes,
            "probed_ok": self.probed_ok,
        }


@dataclass(frozen=True)
class SourceIntegrityIssue:
    code: str
    message: str
    range: TimeRange | None = None
    segment_path: str | None = None
    severity: str = "BLOCK"

    def to_manifest(self) -> dict[str, object]:
        data: dict[str, object] = {
            "code": self.code,
            "message": self.message,
            "severity": self.severity,
        }
        if self.range is not None:
            data["range"] = self.range.to_manifest()
        if self.segment_path is not None:
            data["segment_path"] = self.segment_path
        return data


@dataclass(frozen=True)
class SourceRangeLedger:
    room_id: str
    session_date: str
    expected_range: TimeRange
    observed_ranges: tuple[TimeRange, ...]
    missing_ranges: tuple[TimeRange, ...]
    issues: tuple[SourceIntegrityIssue, ...]
    can_use_local_source: bool
    compensation_required: bool
    replay_probe_required: bool
    danmaku_latest_ms: int | None = None

    def to_manifest(self) -> dict[str, object]:
        return {
            "schema_version": "source-range-ledger.v1",
            "room_id": self.room_id,
            "session_date": self.session_date,
            "expected_range": self.expected_range.to_manifest(),
            "observed_ranges": [item.to_manifest() for item in self.observed_ranges],
            "missing_ranges": [item.to_manifest() for item in self.missing_ranges],
            "issues": [issue.to_manifest() for issue in self.issues],
            "can_use_local_source": self.can_use_local_source,
            "compensation_required": self.compensation_required,
            "replay_probe_required": self.replay_probe_required,
            "danmaku_latest_ms": self.danmaku_latest_ms,
        }

    def write_manifest(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(self.to_manifest(), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )


@dataclass(frozen=True)
class BilibiliReplayCompensationPlan:
    room_id: str
    session_date: str
    status: str
    download_ranges: tuple[TimeRange, ...]
    reason_codes: tuple[str, ...] = ()

    def to_manifest(self) -> dict[str, object]:
        return {
            "schema_version": "bilibili-replay-compensation-plan.v1",
            "room_id": self.room_id,
            "session_date": self.session_date,
            "status": self.status,
            "download_ranges": [item.to_manifest() for item in self.download_ranges],
            "reason_codes": list(self.reason_codes),
        }


@dataclass(frozen=True)
class BilibiliReplayDownloadCommandPlan:
    room_id: str
    session_date: str
    status: str
    commands: tuple[tuple[str, ...], ...]
    reason_codes: tuple[str, ...] = ()

    def to_manifest(self) -> dict[str, object]:
        return {
            "schema_version": "bilibili-replay-download-command-plan.v1",
            "room_id": self.room_id,
            "session_date": self.session_date,
            "status": self.status,
            "commands": [_redact_command(command) for command in self.commands],
            "reason_codes": list(self.reason_codes),
        }


_MIN_SEGMENT_DURATION_MS = 10_000
_MIN_SEGMENT_SIZE_BYTES = 64 * 1024
_CONNECTION_STUB_SCHEMA = "recording-connection-stub.v1"
_CONNECTION_STUB_STATUS = "IGNORED_CONNECTION_STUB"
_CONNECTION_STUB_REASON = "RECORDER_CONNECTION_STUB_NO_DECODABLE_VIDEO"
_CONNECTION_STUB_MAX_XML_EVENT_COUNT = 1
_CONNECTION_STUB_REBIND_SCHEMA = "recording-source-fuse-identity-rebind.v1"
_CONNECTION_STUB_REBIND_POLICY = "FUSE_REMOUNT_DEVICE_INODE_REBIND"
_CONNECTION_STUB_TIMESTAMP_REBIND_SCHEMA = "recording-source-fuse-timestamp-rebind.v1"
_CONNECTION_STUB_TIMESTAMP_REBIND_POLICY = "FUSE_SUCCESSOR_MTIME_CTIME_REATTESTATION"
_FILE_FINGERPRINT_KEYS = (
    "size_bytes",
    "mtime_ns",
    "ctime_ns",
    "device",
    "inode",
    "mode",
)
_DISPOSITION_FILE_ROLES = ("source", "xml", "successor_source", "successor_mp4")
_TIMESTAMP_REBIND_ROLES = ("successor_source", "successor_mp4")
_TIMESTAMP_REBIND_FIELDS = ("mtime_ns", "ctime_ns")
_HISTORICAL_SHA_BASIS = "HISTORICAL_SHA256_MATCH"
_LEGACY_SUCCESSOR_SOURCE_BASIS = "LEGACY_NO_PRIOR_SHA256_WEBHOOK_FINALIZED_LEDGER_STABLE_STAT"


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _regular_file_fingerprint(path: Path) -> dict[str, int]:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode):
        raise OSError(f"not a regular non-symlink file: {path}")
    return {
        "size_bytes": info.st_size,
        "mtime_ns": info.st_mtime_ns,
        "ctime_ns": info.st_ctime_ns,
        "device": info.st_dev,
        "inode": info.st_ino,
        "mode": stat.S_IMODE(info.st_mode),
    }


def _binding_matches_fingerprint(binding: dict[str, object], path: Path) -> bool:
    try:
        current = _regular_file_fingerprint(path)
    except OSError:
        return False
    return all(binding.get(key) == current[key] for key in _FILE_FINGERPRINT_KEYS)


def _decode_mountinfo_field(value: str) -> str:
    return re.sub(
        r"\\([0-7]{3})",
        lambda match: chr(int(match.group(1), 8)),
        value,
    )


def _mount_identity_for_path(path: Path) -> dict[str, object] | None:
    target = os.path.abspath(os.fspath(path))
    best: tuple[int, dict[str, object]] | None = None
    try:
        lines = Path("/proc/self/mountinfo").read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for line in lines:
        fields = line.split()
        try:
            separator = fields.index("-")
            mount_point = _decode_mountinfo_field(fields[4])
            identity: dict[str, object] = {
                "mount_id": int(fields[0]),
                "major_minor": fields[2],
                "root": _decode_mountinfo_field(fields[3]),
                "mount_point": mount_point,
                "filesystem_type": fields[separator + 1],
                "mount_source": _decode_mountinfo_field(fields[separator + 2]),
            }
        except (IndexError, ValueError):
            continue
        boundary = mount_point.rstrip("/") + "/"
        if target != mount_point and not target.startswith(boundary):
            continue
        candidate = (len(mount_point), identity)
        if best is None or candidate[0] > best[0]:
            best = candidate
    return best[1] if best is not None else None


def _is_fuse_mount(identity: object) -> bool:
    if not isinstance(identity, dict):
        return False
    filesystem_type = str(identity.get("filesystem_type") or "").lower()
    return filesystem_type == "fuse" or filesystem_type.startswith("fuse.")


def _paths_share_fuse_mount(paths: Sequence[Path]) -> bool:
    identities = [_mount_identity_for_path(path) for path in paths]
    return bool(
        identities
        and all(_is_fuse_mount(identity) for identity in identities)
        and all(identity == identities[0] for identity in identities[1:])
    )


def _portable_mount_identity(identity: object) -> dict[str, object] | None:
    if not _is_fuse_mount(identity):
        return None
    assert isinstance(identity, dict)
    return {
        "major_minor": identity.get("major_minor"),
        "filesystem_type": identity.get("filesystem_type"),
        "mount_source": identity.get("mount_source"),
    }


def _disposition_binding_projection(binding: dict[str, object]) -> dict[str, object]:
    projection = {"path": binding.get("path")}
    projection.update({key: binding.get(key) for key in _FILE_FINGERPRINT_KEYS})
    sha256 = binding.get("sha256")
    projection["sha256"] = sha256
    projection["verification_basis"] = (
        _HISTORICAL_SHA_BASIS if sha256 is not None else _LEGACY_SUCCESSOR_SOURCE_BASIS
    )
    return projection


def _changed_fingerprint_fields(
    previous: dict[str, dict[str, object]],
    current: dict[str, dict[str, object]],
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


def _identity_rebind_legacy_contract() -> dict[str, object]:
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
    row: dict[str, object],
    previous: dict[str, dict[str, object]],
) -> dict[str, object]:
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


def _connection_stub_identity_matches(
    *,
    row: dict[str, object],
    bindings: dict[str, dict[str, object]],
    paths: dict[str, Path],
    relative_paths: dict[str, str],
    identity_rebinds: object,
) -> tuple[bool, str]:
    if identity_rebinds is None:
        receipts: list[object] = []
    elif isinstance(identity_rebinds, list):
        receipts = identity_rebinds
    else:
        return False, "source disposition identity rebind ledger is malformed"
    previous = {
        role: _disposition_binding_projection(bindings[role]) for role in _DISPOSITION_FILE_ROLES
    }
    disposition_sha256 = str(
        (row.get("canonical_integrity") or {}).get("canonical_json_sha256")  # type: ignore[union-attr]
        or ""
    )
    previous_receipt_sha256: str | None = None
    previous_mount: dict[str, object] | None = None
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
            return False, "source disposition identity rebind receipt is malformed"
        is_identity_rebind = (
            receipt.get("schema_version") == _CONNECTION_STUB_REBIND_SCHEMA
            and receipt.get("policy") == _CONNECTION_STUB_REBIND_POLICY
        )
        is_timestamp_rebind = (
            receipt.get("schema_version") == _CONNECTION_STUB_TIMESTAMP_REBIND_SCHEMA
            and receipt.get("policy") == _CONNECTION_STUB_TIMESTAMP_REBIND_POLICY
        )
        expected_fields = base_receipt_fields | (
            {"changed_fields"} if is_timestamp_rebind else set()
        )
        if (not is_identity_rebind and not is_timestamp_rebind) or set(receipt) != expected_fields:
            return False, "source disposition identity rebind receipt is malformed"
        integrity = receipt.get("canonical_integrity")
        unsigned = {key: value for key, value in receipt.items() if key != "canonical_integrity"}
        if not isinstance(integrity, dict) or integrity != {
            "algorithm": "sha256",
            "canonical_json_sha256": _canonical_json_sha256(unsigned),
        }:
            return False, "source disposition identity rebind integrity mismatch"
        current = receipt.get("current_bindings")
        if (
            receipt.get("source_disposition_canonical_sha256") != disposition_sha256
            or receipt.get("previous_receipt_canonical_sha256") != previous_receipt_sha256
            or receipt.get("previous_bindings") != previous
            or not _is_fuse_mount(receipt.get("current_mount"))
            or not isinstance(current, dict)
            or set(current) != set(_DISPOSITION_FILE_ROLES)
        ):
            return False, "source disposition identity rebind chain drifted"
        for role in _DISPOSITION_FILE_ROLES:
            binding = current.get(role)
            prior = previous[role]
            if (
                not isinstance(binding, dict)
                or set(binding) != {"path", "sha256", "verification_basis", *_FILE_FINGERPRINT_KEYS}
                or not _path_ends_with(binding.get("path"), relative_paths[role])
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
                return False, "source disposition identity rebind binding drifted"
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
                return False, "source disposition identity rebind binding drifted"
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
                return False, "source disposition timestamp rebind binding drifted"
        previous = current
        previous_receipt_sha256 = str(integrity["canonical_json_sha256"])
        previous_mount = receipt["current_mount"]
    try:
        current_fingerprints = {
            role: _regular_file_fingerprint(paths[role]) for role in _DISPOSITION_FILE_ROLES
        }
    except OSError as exc:
        return False, f"source disposition evidence cannot be statted: {type(exc).__name__}"
    if not all(
        previous[role].get(key) == current_fingerprints[role][key]
        for role in _DISPOSITION_FILE_ROLES
        for key in _FILE_FINGERPRINT_KEYS
    ):
        return False, "source disposition effective fingerprint drifted"
    if receipts:
        identities = [_mount_identity_for_path(path) for path in paths.values()]
        if (
            not _paths_share_fuse_mount(list(paths.values()))
            or not identities
            or _portable_mount_identity(identities[0]) != _portable_mount_identity(previous_mount)
        ):
            return False, "source disposition FUSE rebind mount identity drifted"
    return True, "source disposition effective identity revalidated"


def _canonical_json_sha256(payload: dict[str, object]) -> str:
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _parse_event_time(value: object) -> datetime:
    raw = str(value or "")
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    head, separator, tail = raw.partition(".")
    if separator and ("+" in tail or "-" in tail):
        plus = tail.rfind("+")
        minus = tail.rfind("-")
        split = max(plus, minus)
        fraction, zone = tail[:split], tail[split:]
        raw = f"{head}.{fraction[:6].ljust(6, '0')}{zone}"
    return datetime.fromisoformat(raw)


def _webhook_binding(evidence: dict[str, object]) -> dict[str, object]:
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


def _path_ends_with(value: object, relative: str) -> bool:
    return str(value or "").replace("\\", "/").endswith("/" + relative)


def _verify_connection_stub_disposition(
    source_flv: Path,
    *,
    date_dir: Path,
    adapter_state_path: Path | None,
) -> tuple[bool, str]:
    """Revalidate a typed adapter disposition against current bytes and state."""

    if adapter_state_path is None:
        return False, "adapter_state_path was not provided"
    try:
        state = json.loads(adapter_state_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return False, f"adapter state is unreadable: {type(exc).__name__}"
    if state.get("schema_version") != "bililive-recorder-adapter-state.v1":
        return False, "adapter state schema mismatch"
    dispositions = state.get("source_dispositions")
    identity_rebind_ledger = state.get("source_disposition_identity_rebinds") or {}
    webhook_files = state.get("webhook_files")
    finalized = state.get("finalized")
    if not all(
        isinstance(item, dict)
        for item in (dispositions, identity_rebind_ledger, webhook_files, finalized)
    ):
        return False, "adapter disposition/webhook/finalized ledgers are malformed"

    relative = f"{date_dir.name}/{source_flv.name}"
    row = dispositions.get(relative)
    if row is None:
        return False, "no typed source disposition exists for this FLV"
    if not isinstance(row, dict):
        return False, "typed source disposition row is malformed"
    if (
        row.get("schema_version") != _CONNECTION_STUB_SCHEMA
        or row.get("status") != _CONNECTION_STUB_STATUS
        or row.get("reason_code") != _CONNECTION_STUB_REASON
        or row.get("source_relative_path") != relative
    ):
        return False, "typed source disposition identity is invalid"
    integrity = row.get("canonical_integrity")
    unsigned = {key: value for key, value in row.items() if key != "canonical_integrity"}
    if not isinstance(integrity, dict) or integrity != {
        "algorithm": "sha256",
        "canonical_json_sha256": _canonical_json_sha256(unsigned),
    }:
        return False, "typed source disposition canonical integrity mismatch"
    action = row.get("source_action")
    if action != {
        "finalized": False,
        "delete_source": "never",
        "move_source": "never",
    }:
        return False, "typed source disposition action contract is invalid"
    if relative in finalized or source_flv.with_suffix(".mp4").exists():
        return False, "ignored source unexpectedly entered the finalized lane"

    source_binding = row.get("source")
    xml_binding = row.get("xml")
    decode = row.get("decode")
    session = row.get("session")
    if not all(isinstance(item, dict) for item in (source_binding, xml_binding, decode, session)):
        return False, "typed source disposition evidence is malformed"
    xml_path = source_flv.with_suffix(".xml")
    xml_relative = str(Path(relative).with_suffix(".xml")).replace("\\", "/")
    if not _path_ends_with(source_binding.get("path"), relative) or not _path_ends_with(
        xml_binding.get("path"), xml_relative
    ):
        return False, "typed source/XML path binding is invalid"
    try:
        source_stat = _regular_file_fingerprint(source_flv)
        _regular_file_fingerprint(xml_path)
    except OSError as exc:
        return False, f"ignored source/XML cannot be revalidated: {type(exc).__name__}"
    record_info = xml_binding.get("record_info")
    early_successor_source = session.get("successor_source")
    early_successor_mp4 = session.get("successor_mp4")
    if not isinstance(early_successor_source, dict) or not isinstance(early_successor_mp4, dict):
        return False, "successor typed evidence is malformed"
    identity_ok, identity_detail = _connection_stub_identity_matches(
        row=row,
        bindings={
            "source": source_binding,
            "xml": xml_binding,
            "successor_source": early_successor_source,
            "successor_mp4": early_successor_mp4,
        },
        paths={
            "source": source_flv,
            "xml": xml_path,
            "successor_source": date_dir.parent / str(session.get("successor_relative_path") or ""),
            "successor_mp4": (
                date_dir.parent / str(session.get("successor_relative_path") or "")
            ).with_suffix(".mp4"),
        },
        relative_paths={
            "source": relative,
            "xml": xml_relative,
            "successor_source": str(session.get("successor_relative_path") or ""),
            "successor_mp4": str(
                Path(str(session.get("successor_relative_path") or "")).with_suffix(".mp4")
            ).replace("\\", "/"),
        },
        identity_rebinds=identity_rebind_ledger.get(relative),
    )
    if not identity_ok:
        return False, identity_detail
    if (
        re.fullmatch(r"[0-9a-f]{64}", str(source_binding.get("sha256") or "")) is None
        or re.fullmatch(r"[0-9a-f]{64}", str(xml_binding.get("sha256") or "")) is None
        or not isinstance(record_info, dict)
        or record_info.get("roomid") != source_flv.name.split("_", 1)[0]
        or xml_binding.get("official_bililiverecorder") is not True
        or not isinstance(xml_binding.get("event_count"), int)
        or isinstance(xml_binding.get("event_count"), bool)
        or not 0 <= xml_binding["event_count"] <= _CONNECTION_STUB_MAX_XML_EVENT_COUNT
    ):
        return False, "ignored source/XML fingerprint or bounded-event evidence drifted"
    if (
        decode.get("video_codec") != "h264"
        or decode.get("width") != 0
        or decode.get("height") != 0
        or decode.get("decoded_video_frames") != 0
        or decode.get("video_packets") != 0
        or decode.get("size_bytes") != source_stat["size_bytes"]
    ):
        return False, "ignored source decode evidence is invalid"

    source_webhook = webhook_files.get(relative)
    if not isinstance(source_webhook, dict) or row.get("webhook") != _webhook_binding(
        source_webhook
    ):
        return False, "ignored source webhook evidence drifted"
    try:
        opened = _parse_event_time(source_webhook.get("file_open_time"))
        closed = _parse_event_time(source_webhook.get("file_close_time"))
        event_duration = float(source_webhook.get("duration") or 0)
        wall_duration = (closed - opened).total_seconds()
    except (TypeError, ValueError):
        return False, "ignored source webhook time evidence is invalid"
    session_id = str(source_webhook.get("session_id") or "")
    if (
        source_webhook.get("status") != "CLOSED"
        or not source_webhook.get("opening_event_id")
        or not source_webhook.get("closing_event_id")
        or not session_id
        or source_webhook.get("file_size") != source_stat["size_bytes"]
        or source_stat["size_bytes"] >= 5 * 1024 * 1024
        or not 0 < event_duration < 10
        or not 0 <= wall_duration < 10
        or abs(float(decode.get("duration_seconds") or 0) - event_duration) > 0.001
    ):
        return False, "ignored source thresholds or CLOSED evidence drifted"

    openings: list[tuple[datetime, str, dict[str, object]]] = []
    try:
        for candidate_relative, candidate in webhook_files.items():
            if isinstance(candidate, dict) and candidate.get("opening_event_id"):
                openings.append(
                    (
                        _parse_event_time(candidate.get("file_open_time")),
                        str(candidate_relative),
                        candidate,
                    )
                )
    except (TypeError, ValueError):
        return False, "same-session opening evidence is invalid"
    openings.sort(key=lambda item: (item[0], item[1]))
    source_positions = [index for index, item in enumerate(openings) if item[1] == relative]
    if len(source_positions) != 1:
        return False, "ignored source opening evidence is not unique"
    source_position = source_positions[0]
    if any(
        str(item[2].get("session_id") or "") == session_id for item in openings[:source_position]
    ) or source_position + 1 >= len(openings):
        return False, "ignored source is no longer the first same-session opening"
    successor_opened, successor_relative, successor_webhook = openings[source_position + 1]
    successor_relative_path = PurePosixPath(successor_relative)
    if (
        successor_relative_path.is_absolute()
        or ".." in successor_relative_path.parts
        or len(successor_relative_path.parts) != 2
        or successor_relative_path.parts[0] != date_dir.name
        or not successor_relative_path.parts[1].startswith(f"{source_flv.name.split('_', 1)[0]}_")
        or successor_relative_path.suffix.lower() != ".flv"
    ):
        return False, "same-session successor path is invalid"
    gap = (successor_opened - closed).total_seconds()
    if (
        not 0 <= gap <= 2
        or successor_webhook.get("status") != "CLOSED"
        or str(successor_webhook.get("session_id") or "") != session_id
        or not successor_webhook.get("opening_event_id")
        or not successor_webhook.get("closing_event_id")
        or session.get("session_id") != session_id
        or session.get("prior_same_session_openings") != 0
        or session.get("successor_gap_seconds") != gap
        or session.get("successor_relative_path") != successor_relative
        or session.get("successor_webhook") != _webhook_binding(successor_webhook)
    ):
        return False, "same-session successor event/time binding drifted"

    successor_source = date_dir.parent / successor_relative
    successor_mp4 = successor_source.with_suffix(".mp4")
    successor_ledger = finalized.get(successor_relative)
    if not isinstance(successor_ledger, dict):
        return False, "successor finalized ledger is missing"
    embedded_ledger = session.get("successor_finalized_ledger")
    embedded_source = session.get("successor_source")
    embedded_mp4 = session.get("successor_mp4")
    if not all(isinstance(item, dict) for item in (embedded_ledger, embedded_source, embedded_mp4)):
        return False, "successor typed evidence is malformed"
    expected_ledger = {
        "source_size": successor_ledger.get("source_size"),
        "source_mtime_ns": successor_ledger.get("source_mtime_ns"),
        "target": successor_ledger.get("target"),
        "target_sha256": successor_ledger.get("target_sha256"),
        "finalized_at": successor_ledger.get("finalized_at"),
    }
    successor_mp4_relative = str(Path(successor_relative).with_suffix(".mp4")).replace("\\", "/")
    if (
        not _path_ends_with(embedded_source.get("path"), successor_relative)
        or not _path_ends_with(embedded_mp4.get("path"), successor_mp4_relative)
        or not _path_ends_with(successor_ledger.get("target"), successor_mp4_relative)
    ):
        return False, "successor source/MP4 path binding is invalid"
    try:
        successor_stat = _regular_file_fingerprint(successor_source)
        successor_mp4_stat = _regular_file_fingerprint(successor_mp4)
    except OSError as exc:
        return False, f"successor source/MP4 cannot be read: {type(exc).__name__}"
    if (
        embedded_ledger != expected_ledger
        or successor_stat["size_bytes"] != successor_webhook.get("file_size")
        or successor_ledger.get("source_size") != successor_stat["size_bytes"]
        or embedded_mp4.get("sha256") != successor_ledger.get("target_sha256")
        or re.fullmatch(r"[0-9a-f]{64}", str(embedded_mp4.get("sha256") or "")) is None
    ):
        return False, "successor finalized ledger or real MP4 drifted"
    media = embedded_mp4.get("media")
    if (
        not isinstance(media, dict)
        or float(media.get("duration_seconds") or 0) <= 0
        or media.get("size_bytes") != successor_mp4_stat["size_bytes"]
        or not media.get("video_codec")
        or not media.get("audio_codec")
        or int(media.get("width") or 0) <= 0
        or int(media.get("height") or 0) <= 0
    ):
        return False, "successor finalized MP4 media evidence is invalid"
    return True, "typed connection-stub disposition revalidated"


def audit_finalized_recording_inventory(
    date_dir: Path,
    *,
    room_id: str,
    adapter_state_path: Path | None = None,
) -> dict[str, object]:
    """Fail closed when recorder output never reached the runner's MP4 lane.

    The unattended runner consumes only root-level ``<room>_*.mp4`` files. A
    finalized HLS playlist, raw fMP4, or closed BililiveRecorder FLV without
    that sibling therefore represents real source bytes which selection cannot
    see.  The 2026-07-22
    incident had exactly this shape: segment two ended cleanly as m3u8/m4s but
    never became MP4, so the first 30 minutes were falsely reported as the
    whole session.

    This audit is intentionally cheap (directory names plus at most 1 MiB of
    playlist text) and contains no wall-clock assumptions.  It can run before
    the runner's early "nothing new" return on every tick.
    """

    stems: dict[str, dict[str, Path]] = {}
    prefix = f"{room_id}_"
    try:
        paths = sorted(date_dir.iterdir()) if date_dir.is_dir() else []
    except OSError as exc:
        return {
            "schema_version": "recording-inventory-audit.v1",
            "status": "BLOCKED",
            "can_select": False,
            "room_id": room_id,
            "date_dir": str(date_dir),
            "consumer_segments": [],
            "observed_stems": [],
            "issues": [
                {
                    "code": "SOURCE_DIRECTORY_UNREADABLE",
                    "severity": "BLOCK",
                    "message": f"Recording directory is unreadable: {type(exc).__name__}",
                    "path": str(date_dir),
                }
            ],
        }

    for path in paths:
        if not path.is_file() or not path.name.startswith(prefix):
            continue
        suffix = path.suffix.lower()
        if suffix not in {".mp4", ".flv", ".m4s", ".m3u8"}:
            continue
        stems.setdefault(path.stem, {})[suffix] = path

    issues: list[dict[str, object]] = []
    consumer_segments: list[str] = []
    for stem, siblings in sorted(stems.items()):
        mp4 = siblings.get(".mp4")
        if mp4 is not None:
            consumer_segments.append(str(mp4))
            continue

        playlist = siblings.get(".m3u8")
        raw_media = siblings.get(".m4s") or siblings.get(".flv")
        playlist_finalized = False
        if playlist is not None:
            try:
                with playlist.open("r", encoding="utf-8", errors="replace") as handle:
                    playlist_finalized = "#EXT-X-ENDLIST" in handle.read(1024 * 1024)
            except OSError:
                issues.append(
                    {
                        "code": "SOURCE_PLAYLIST_UNREADABLE",
                        "severity": "BLOCK",
                        "message": "Recorder playlist exists but cannot be read.",
                        "segment_stem": stem,
                        "path": str(playlist),
                    }
                )
                continue

        if playlist_finalized:
            code = "FINALIZED_PLAYLIST_WITHOUT_MP4"
            message = (
                "Recorder playlist has ENDLIST and raw media, but the MP4 consumed "
                "by autoslice was never finalized."
            )
        elif playlist is not None:
            code = "PLAYLIST_NOT_FINALIZED"
            message = (
                "Recorder playlist has no ENDLIST and no consumable MP4 after the "
                "session processing gate."
            )
        elif raw_media is not None:
            if raw_media.suffix.lower() == ".flv":
                disposition_ok, disposition_detail = _verify_connection_stub_disposition(
                    raw_media,
                    date_dir=date_dir,
                    adapter_state_path=adapter_state_path,
                )
                if disposition_ok:
                    issues.append(
                        {
                            "code": _CONNECTION_STUB_REASON,
                            "severity": "WARN",
                            "message": disposition_detail,
                            "segment_stem": stem,
                            "path": str(raw_media),
                            "source_disposition_schema": _CONNECTION_STUB_SCHEMA,
                            "source_disposition_status": _CONNECTION_STUB_STATUS,
                        }
                    )
                    continue
            code = (
                "CLOSED_FLV_WITHOUT_MP4"
                if raw_media.suffix.lower() == ".flv"
                else "RAW_MEDIA_WITHOUT_MP4"
            )
            message = (
                "Closed BililiveRecorder FLV exists without the MP4 consumed by autoslice."
                if raw_media.suffix.lower() == ".flv"
                else "Recorder raw media exists without the MP4 consumed by autoslice."
            )
        else:
            continue
        issues.append(
            {
                "code": code,
                "severity": "BLOCK",
                "message": message,
                "segment_stem": stem,
                "path": str(playlist or raw_media),
                "raw_media_path": str(raw_media) if raw_media is not None else None,
                "source_disposition_error": (
                    disposition_detail
                    if raw_media is not None and raw_media.suffix.lower() == ".flv"
                    else None
                ),
            }
        )

    blocking_issues = [
        issue for issue in issues if str(issue.get("severity") or "BLOCK") == "BLOCK"
    ]
    return {
        "schema_version": "recording-inventory-audit.v1",
        "status": "BLOCKED" if blocking_issues else "PASS",
        "can_select": not blocking_issues,
        "room_id": room_id,
        "date_dir": str(date_dir),
        "consumer_segments": consumer_segments,
        "observed_stems": sorted(stems),
        "issues": issues,
    }


def build_source_range_ledger(
    *,
    room_id: str,
    session_date: str,
    segments: Sequence[MediaSegmentObservation],
    expected_start_ms: int,
    expected_end_ms: int,
    danmaku_latest_ms: int | None = None,
    active_media_size_growth_bytes: int | None = None,
    max_gap_ms: int = 2_000,
    min_segment_duration_ms: int = _MIN_SEGMENT_DURATION_MS,
    min_segment_size_bytes: int = _MIN_SEGMENT_SIZE_BYTES,
) -> SourceRangeLedger:
    expected_range = TimeRange(expected_start_ms, expected_end_ms)
    ordered = sorted(segments, key=lambda item: (item.start_ms, item.end_ms, item.path))
    issues: list[SourceIntegrityIssue] = []

    if not ordered:
        issues.append(
            SourceIntegrityIssue(
                code="SOURCE_MEDIA_MISSING",
                message="No local media segment observations are available for the expected source range.",
                range=expected_range,
            )
        )

    observed_ranges: list[TimeRange] = []
    for segment in ordered:
        observed = TimeRange(
            max(expected_start_ms, segment.start_ms), min(expected_end_ms, segment.end_ms)
        )
        if observed.duration_ms > 0:
            observed_ranges.append(observed)

    merged_ranges = _merge_ranges(observed_ranges, max_gap_ms=max_gap_ms)
    missing_ranges = _missing_ranges(expected_range, merged_ranges, max_gap_ms=max_gap_ms)
    coverage_complete = not missing_ranges

    for segment in ordered:
        observed = TimeRange(
            max(expected_start_ms, segment.start_ms), min(expected_end_ms, segment.end_ms)
        )
        if not segment.probed_ok:
            # An unprobeable file (e.g. raw fmp4 .m4s sidecar) is harmless only when a
            # probed sibling segment records the same interval; otherwise it may hold
            # unique content and must block.
            has_probed_coverage = _has_redundant_probed_coverage(
                segment, ordered, max_gap_ms=max_gap_ms
            )
            degraded_severity = "WARN" if has_probed_coverage else "BLOCK"
            issues.append(
                SourceIntegrityIssue(
                    code="MEDIA_PROBE_FAILED",
                    message="Media segment could not be probed successfully."
                    + (
                        " Probed sibling coverage exists; treated as a redundant sidecar."
                        if has_probed_coverage
                        else ""
                    ),
                    range=observed if observed.duration_ms else segment.range,
                    segment_path=segment.path,
                    severity=degraded_severity,
                )
            )
        else:
            # A verified-but-tiny segment (recorder-restart stub) is only evidence of
            # loss when the session coverage is actually incomplete.
            degraded_severity = "WARN" if coverage_complete else "BLOCK"
        if segment.duration_ms < min_segment_duration_ms:
            issues.append(
                SourceIntegrityIssue(
                    code="MEDIA_SEGMENT_TOO_SHORT",
                    message="Media segment duration is too short to cover a live recording interval reliably.",
                    range=observed if observed.duration_ms else segment.range,
                    segment_path=segment.path,
                    severity=degraded_severity,
                )
            )
        if segment.size_bytes < min_segment_size_bytes:
            issues.append(
                SourceIntegrityIssue(
                    code="MEDIA_SEGMENT_TOO_SMALL",
                    message="Media segment is suspiciously small for a live recording interval.",
                    range=observed if observed.duration_ms else segment.range,
                    segment_path=segment.path,
                    severity=degraded_severity,
                )
            )
    for missing in missing_ranges:
        issues.append(
            SourceIntegrityIssue(
                code="MEDIA_COVERAGE_GAP",
                message="Expected source timeline has no local media coverage for this range.",
                range=missing,
            )
        )

    coverage_end_ms = max((item.end_ms for item in merged_ranges), default=expected_start_ms)
    if danmaku_latest_ms is not None and danmaku_latest_ms > coverage_end_ms + max_gap_ms:
        outrun_range = TimeRange(coverage_end_ms, min(expected_end_ms, danmaku_latest_ms))
        issues.append(
            SourceIntegrityIssue(
                code="DANMAKU_OUTRUNS_MEDIA",
                message="Danmaku/event timeline advances beyond available local media coverage.",
                range=outrun_range,
            )
        )
        if active_media_size_growth_bytes == 0:
            issues.append(
                SourceIntegrityIssue(
                    code="MEDIA_STALLED_WHILE_DANMAKU_ADVANCES",
                    message="Local media did not grow while danmaku/events continued advancing.",
                    range=outrun_range,
                )
            )

    blocking_issues = [issue for issue in issues if issue.severity != "WARN"]
    compensation_required = bool(missing_ranges or blocking_issues)
    can_use_local_source = not compensation_required
    replay_probe_required = compensation_required
    return SourceRangeLedger(
        room_id=room_id,
        session_date=session_date,
        expected_range=expected_range,
        observed_ranges=tuple(merged_ranges),
        missing_ranges=tuple(missing_ranges),
        issues=tuple(_dedupe_issues(issues)),
        can_use_local_source=can_use_local_source,
        compensation_required=compensation_required,
        replay_probe_required=replay_probe_required,
        danmaku_latest_ms=danmaku_latest_ms,
    )


def plan_bilibili_replay_compensation(
    ledger: SourceRangeLedger,
    *,
    auth_available: bool,
    replay_available: bool | None,
) -> BilibiliReplayCompensationPlan:
    if not ledger.compensation_required:
        return BilibiliReplayCompensationPlan(
            room_id=ledger.room_id,
            session_date=ledger.session_date,
            status="NOT_REQUIRED",
            download_ranges=(),
        )

    if not auth_available:
        return BilibiliReplayCompensationPlan(
            room_id=ledger.room_id,
            session_date=ledger.session_date,
            status="BLOCKED",
            download_ranges=(),
            reason_codes=("BILIBILI_REPLAY_AUTH_REQUIRED",),
        )

    if replay_available is None:
        return BilibiliReplayCompensationPlan(
            room_id=ledger.room_id,
            session_date=ledger.session_date,
            status="PROBE_REQUIRED",
            download_ranges=ledger.missing_ranges,
            reason_codes=("BILIBILI_REPLAY_AVAILABILITY_UNKNOWN",),
        )

    if not replay_available:
        return BilibiliReplayCompensationPlan(
            room_id=ledger.room_id,
            session_date=ledger.session_date,
            status="RETRY_INFRA",
            download_ranges=(),
            reason_codes=("BILIBILI_REPLAY_UNAVAILABLE",),
        )

    return BilibiliReplayCompensationPlan(
        room_id=ledger.room_id,
        session_date=ledger.session_date,
        status="READY_TO_DOWNLOAD",
        download_ranges=ledger.missing_ranges or (ledger.expected_range,),
    )


def plan_bilibili_replay_download_commands(
    compensation: BilibiliReplayCompensationPlan,
    *,
    replay_url: str | None,
    output_dir: Path,
    cookie_file: Path | None,
    tool_path: str | Path | None,
) -> BilibiliReplayDownloadCommandPlan:
    if compensation.status != "READY_TO_DOWNLOAD":
        return BilibiliReplayDownloadCommandPlan(
            room_id=compensation.room_id,
            session_date=compensation.session_date,
            status=compensation.status,
            commands=(),
            reason_codes=compensation.reason_codes,
        )
    if not replay_url:
        return BilibiliReplayDownloadCommandPlan(
            room_id=compensation.room_id,
            session_date=compensation.session_date,
            status="BLOCKED",
            commands=(),
            reason_codes=("BILIBILI_REPLAY_URL_REQUIRED",),
        )
    if cookie_file is None:
        return BilibiliReplayDownloadCommandPlan(
            room_id=compensation.room_id,
            session_date=compensation.session_date,
            status="BLOCKED",
            commands=(),
            reason_codes=("BILIBILI_REPLAY_AUTH_REQUIRED",),
        )
    if tool_path is None:
        return BilibiliReplayDownloadCommandPlan(
            room_id=compensation.room_id,
            session_date=compensation.session_date,
            status="RETRY_INFRA",
            commands=(),
            reason_codes=("BILIBILI_DOWNLOAD_TOOL_MISSING",),
        )

    commands: list[tuple[str, ...]] = []
    for item in compensation.download_ranges:
        output_template = (
            output_dir
            / f"{compensation.room_id}_{compensation.session_date}_{item.start_ms}_{item.end_ms}.%(ext)s"
        )
        commands.append(
            (
                str(tool_path),
                replay_url,
                "--download-sections",
                f"*{_format_ms(item.start_ms)}-{_format_ms(item.end_ms)}",
                "--cookies",
                str(cookie_file),
                "-o",
                str(output_template),
            )
        )
    return BilibiliReplayDownloadCommandPlan(
        room_id=compensation.room_id,
        session_date=compensation.session_date,
        status="READY",
        commands=tuple(commands),
    )


def _has_redundant_probed_coverage(
    segment: MediaSegmentObservation,
    segments: Sequence[MediaSegmentObservation],
    *,
    max_gap_ms: int,
) -> bool:
    probed_ranges: list[TimeRange] = []
    for other in segments:
        if other is segment or not other.probed_ok or other.duration_ms <= 0:
            continue
        if segment.duration_ms <= 0:
            if abs(other.start_ms - segment.start_ms) <= max_gap_ms:
                return True
            continue
        probed_ranges.append(other.range)

    if segment.duration_ms <= 0:
        return False
    merged = _merge_ranges(probed_ranges, max_gap_ms=max_gap_ms)
    return not _missing_ranges(segment.range, merged, max_gap_ms=max_gap_ms)


def _format_ms(value: int) -> str:
    millis = max(0, int(value))
    total_seconds, ms = divmod(millis, 1000)
    minutes_total, seconds = divmod(total_seconds, 60)
    hours, minutes = divmod(minutes_total, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}.{ms:03d}"


def _redact_command(command: Sequence[str]) -> list[str]:
    redacted: list[str] = []
    hide_next = False
    for part in command:
        if hide_next:
            redacted.append("[REDACTED_COOKIE_FILE]")
            hide_next = False
            continue
        redacted.append(part)
        if part == "--cookies":
            hide_next = True
    return redacted


def _merge_ranges(ranges: Sequence[TimeRange], *, max_gap_ms: int) -> list[TimeRange]:
    merged: list[TimeRange] = []
    for item in sorted(
        (r for r in ranges if r.duration_ms > 0), key=lambda r: (r.start_ms, r.end_ms)
    ):
        if not merged or item.start_ms > merged[-1].end_ms + max_gap_ms:
            merged.append(item)
        else:
            previous = merged[-1]
            merged[-1] = TimeRange(previous.start_ms, max(previous.end_ms, item.end_ms))
    return merged


def _missing_ranges(
    expected: TimeRange, observed: Sequence[TimeRange], *, max_gap_ms: int
) -> list[TimeRange]:
    missing: list[TimeRange] = []
    cursor = expected.start_ms
    for item in observed:
        if item.end_ms <= expected.start_ms or item.start_ms >= expected.end_ms:
            continue
        start = max(expected.start_ms, item.start_ms)
        end = min(expected.end_ms, item.end_ms)
        if start > cursor + max_gap_ms:
            missing.append(TimeRange(cursor, start))
        cursor = max(cursor, end)
    if expected.end_ms > cursor + max_gap_ms:
        missing.append(TimeRange(cursor, expected.end_ms))
    return missing


def _dedupe_issues(issues: Sequence[SourceIntegrityIssue]) -> list[SourceIntegrityIssue]:
    seen: set[tuple[object, ...]] = set()
    result: list[SourceIntegrityIssue] = []
    for issue in issues:
        key = (
            issue.code,
            issue.segment_path,
            issue.range.start_ms if issue.range else None,
            issue.range.end_ms if issue.range else None,
        )
        if key in seen:
            continue
        seen.add(key)
        result.append(issue)
    return result
