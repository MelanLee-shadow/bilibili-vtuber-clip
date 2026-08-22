"""Narrow, durable authority for one manually initiated historical runner tick.

This module deliberately does not select, produce, upload, or talk to a
provider.  It turns two operator actions into sealed local authorities:
renewing an already-valid v2 processing scope, and admitting one disabled
historical ``--once`` run.  The runner consumes the latter explicitly; cron
cannot obtain either exception accidentally.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Mapping

from ops.recording.bililive_recorder_adapter import (
    AdapterError,
    load_env_file,
    query_room_status,
)

from src.autoslice.operator_processing_scope import (
    FAILED_PICK_RECOVERY_GRANT_SCHEMA,
    FAILED_PICK_RECOVERY_INTENT,
    STATE_KEY,
    _validate_grant,
)
from src.autoslice.producer_delivery_transaction import deployment_authority_binding
from src.autoslice.qixi_transaction_core import (
    exclusive_runner_commit,
    require_runner_commit_lease,
)
from src.autoslice.runner_state_writeback import (
    read_exact_state_preimage,
    state_bytes,
    write_exact_state_bytes_under_lease,
)
from src.autoslice.source_integrity import audit_finalized_recording_inventory


SCOPE_RENEWAL_SCHEMA = "operator-processing-scope-renewal.v2"
HISTORICAL_RUN_SCHEMA = "historical-autoslice-once-authority.v1"
_DATE_RX = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_ID_RX = re.compile(r"^[A-Za-z0-9_-]{1,96}$")
_NONCE_RX = re.compile(r"^[a-z0-9][a-z0-9_-]{7,95}$")
_SHA_RX = re.compile(r"^sha256:[0-9a-f]{64}$")
_COMMIT_RX = re.compile(r"^[0-9a-f]{40}$")
_ROOM_ID_RX = re.compile(r"^[1-9][0-9]*$")
_MAX_SCOPE_RENEWAL = timedelta(days=14)
_MAX_HISTORICAL_RUN = timedelta(minutes=90)
_ADAPTER_STATUS_MAX_AGE_SECONDS = 90.0
_SMALL_DOCUMENT_MAX_BYTES = 4 * 1024 * 1024


class HistoricalFastlaneAuthorityError(RuntimeError):
    """A historical exception lacks a sealed, local authority."""


@dataclass(frozen=True, slots=True)
class ScopeRenewal:
    state_path: Path
    before: bytes
    after: bytes
    receipt_path: Path
    receipt: dict[str, object]


@dataclass(frozen=True, slots=True)
class RegularFingerprint:
    """Stable streaming file observation; source bytes are never retained."""

    path: Path
    device: int
    inode: int
    mode: int
    size: int
    mtime_ns: int
    ctime_ns: int
    sha256: str


def normalize_historical_room_id(value: object) -> int:
    """Return the sole sealed room-id representation used by this authority."""

    if isinstance(value, bool):
        raise HistoricalFastlaneAuthorityError("HISTORICAL_FASTLANE_ROOM_ID_INVALID")
    if isinstance(value, int):
        if value > 0:
            return value
        raise HistoricalFastlaneAuthorityError("HISTORICAL_FASTLANE_ROOM_ID_INVALID")
    if isinstance(value, str) and _ROOM_ID_RX.fullmatch(value):
        return int(value)
    raise HistoricalFastlaneAuthorityError("HISTORICAL_FASTLANE_ROOM_ID_INVALID")


def _canonical(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _sha(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _streaming_regular_fingerprint(path: Path, *, label: str) -> RegularFingerprint | None:
    """Hash a regular file through O_NOFOLLOW without retaining its bytes."""

    path = Path(path)
    _safe_dir(path.parent)
    try:
        before = os.lstat(path)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise HistoricalFastlaneAuthorityError(f"HISTORICAL_FASTLANE_{label.upper()}_UNAVAILABLE") from exc
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
        raise HistoricalFastlaneAuthorityError(f"HISTORICAL_FASTLANE_{label.upper()}_UNSAFE")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise HistoricalFastlaneAuthorityError(f"HISTORICAL_FASTLANE_{label.upper()}_UNSAFE") from exc
    try:
        opened = os.fstat(descriptor)
        initial = (before.st_dev, before.st_ino, stat.S_IMODE(before.st_mode), before.st_size, before.st_mtime_ns, before.st_ctime_ns)
        current = (opened.st_dev, opened.st_ino, stat.S_IMODE(opened.st_mode), opened.st_size, opened.st_mtime_ns, opened.st_ctime_ns)
        if current != initial or not stat.S_ISREG(opened.st_mode):
            raise HistoricalFastlaneAuthorityError(f"HISTORICAL_FASTLANE_{label.upper()}_DRIFT")
        digest = hashlib.sha256()
        while chunk := os.read(descriptor, 1024 * 1024):
            digest.update(chunk)
        after_fd = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    try:
        after_path = os.lstat(path)
    except OSError as exc:
        raise HistoricalFastlaneAuthorityError(f"HISTORICAL_FASTLANE_{label.upper()}_DRIFT") from exc
    final = (after_fd.st_dev, after_fd.st_ino, stat.S_IMODE(after_fd.st_mode), after_fd.st_size, after_fd.st_mtime_ns, after_fd.st_ctime_ns)
    named = (after_path.st_dev, after_path.st_ino, stat.S_IMODE(after_path.st_mode), after_path.st_size, after_path.st_mtime_ns, after_path.st_ctime_ns)
    if final != initial or named != initial or stat.S_ISLNK(after_path.st_mode) or not stat.S_ISREG(after_path.st_mode):
        raise HistoricalFastlaneAuthorityError(f"HISTORICAL_FASTLANE_{label.upper()}_DRIFT")
    return RegularFingerprint(path, *initial, "sha256:" + digest.hexdigest())


def _read_small_regular(path: Path, *, fingerprint: RegularFingerprint, label: str) -> bytes:
    """Read a bounded control document and prove it still matches a stream hash."""

    if fingerprint.size > _SMALL_DOCUMENT_MAX_BYTES:
        raise HistoricalFastlaneAuthorityError(f"HISTORICAL_FASTLANE_{label.upper()}_TOO_LARGE")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise HistoricalFastlaneAuthorityError(f"HISTORICAL_FASTLANE_{label.upper()}_UNSAFE") from exc
    try:
        opened = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino, stat.S_IMODE(opened.st_mode), opened.st_size, opened.st_mtime_ns, opened.st_ctime_ns) != (
            fingerprint.device, fingerprint.inode, fingerprint.mode, fingerprint.size, fingerprint.mtime_ns, fingerprint.ctime_ns,
        ):
            raise HistoricalFastlaneAuthorityError(f"HISTORICAL_FASTLANE_{label.upper()}_DRIFT")
        chunks: list[bytes] = []
        while chunk := os.read(descriptor, 1024 * 1024):
            chunks.append(chunk)
    finally:
        os.close(descriptor)
    payload = b"".join(chunks)
    observed = _streaming_regular_fingerprint(path, label=label)
    if observed != fingerprint or _sha(payload) != fingerprint.sha256:
        raise HistoricalFastlaneAuthorityError(f"HISTORICAL_FASTLANE_{label.upper()}_DRIFT")
    return payload


def _json_sha(value: object) -> str:
    return _sha(_canonical(value))


def _utc(value: str) -> datetime:
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise HistoricalFastlaneAuthorityError("HISTORICAL_FASTLANE_TIME_INVALID") from exc
    if moment.tzinfo is None:
        raise HistoricalFastlaneAuthorityError("HISTORICAL_FASTLANE_TIME_INVALID")
    return moment.astimezone(timezone.utc)


def _safe_dir(path: Path, *, mode: int | None = None, create: bool = False) -> Path:
    path = Path(path).absolute()
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        try:
            observed = os.lstat(current)
        except FileNotFoundError:
            if not create or current != path:
                raise HistoricalFastlaneAuthorityError("HISTORICAL_FASTLANE_PATH_UNAVAILABLE")
            os.mkdir(current, mode if mode is not None else 0o700)
            observed = os.lstat(current)
        except OSError as exc:
            raise HistoricalFastlaneAuthorityError("HISTORICAL_FASTLANE_PATH_UNAVAILABLE") from exc
        if stat.S_ISLNK(observed.st_mode) or not stat.S_ISDIR(observed.st_mode):
            raise HistoricalFastlaneAuthorityError("HISTORICAL_FASTLANE_PATH_UNSAFE")
    if mode is not None and stat.S_IMODE(os.lstat(path).st_mode) != mode:
        raise HistoricalFastlaneAuthorityError("HISTORICAL_FASTLANE_PATH_MODE_INVALID")
    return path


def _runtime_root(root: Path) -> Path:
    return _safe_dir(root)


def _state_path(root: Path, date: str) -> Path:
    if not _DATE_RX.fullmatch(date):
        raise HistoricalFastlaneAuthorityError("HISTORICAL_FASTLANE_DATE_INVALID")
    path = root / "state" / f"{date}.json"
    # runner_state_writeback separately enforces this exact grammar; repeat it
    # here before creating any authority namespace.
    if path.parent != root / "state":
        raise HistoricalFastlaneAuthorityError("HISTORICAL_FASTLANE_STATE_PATH_INVALID")
    _safe_dir(path.parent)
    return path


def _parse_state(payload: bytes) -> dict[str, object]:
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HistoricalFastlaneAuthorityError("HISTORICAL_FASTLANE_STATE_INVALID") from exc
    if not isinstance(value, dict):
        raise HistoricalFastlaneAuthorityError("HISTORICAL_FASTLANE_STATE_INVALID")
    return value


def _state_terminal_newline(before: bytes, state: Mapping[str, object]) -> bytes:
    """Accept only the two deployed state renderings and retain its ending."""

    canonical = state_bytes(state)
    if before == canonical:
        return b""
    if before == canonical + b"\n":
        return b"\n"
    raise HistoricalFastlaneAuthorityError("HISTORICAL_FASTLANE_STATE_FORMAT_INVALID")


def _scope(
    state: Mapping[str, object], *, date: str, candidate_ids: tuple[str, ...], now: datetime,
    allow_expired: bool = False,
) -> dict[str, object]:
    block = state.get(STATE_KEY)
    normalized, reason = _validate_grant(block)
    if normalized is None or reason != "OK":
        raise HistoricalFastlaneAuthorityError("HISTORICAL_FASTLANE_SCOPE_INVALID")
    expected_fields = {
        "schema_version", "grant_id", "recording_date", "reason", "candidate_ids",
        "user_authorization", "expires_at", "intent",
    }
    if not isinstance(block, dict) or set(block) != expected_fields:
        raise HistoricalFastlaneAuthorityError("HISTORICAL_FASTLANE_SCOPE_MISMATCH")
    if (
        normalized.get("schema_version") != FAILED_PICK_RECOVERY_GRANT_SCHEMA
        or normalized.get("intent") != FAILED_PICK_RECOVERY_INTENT
        or normalized.get("recording_date") != date
        or tuple(normalized.get("candidate_ids") or ()) != candidate_ids
    ):
        raise HistoricalFastlaneAuthorityError("HISTORICAL_FASTLANE_SCOPE_MISMATCH")
    expires = _utc(str(normalized.get("expires_at") or ""))
    if not allow_expired and expires <= now:
        raise HistoricalFastlaneAuthorityError("HISTORICAL_FASTLANE_SCOPE_EXPIRED")
    return deepcopy(block)


def _mkdir_private(root: Path, name: str) -> Path:
    if not re.fullmatch(r"\.?[A-Za-z0-9_-]{1,95}", name) or name in {".", ".."}:
        raise HistoricalFastlaneAuthorityError("HISTORICAL_FASTLANE_NAMESPACE_INVALID")
    parent = _safe_dir(root)
    path = parent / name
    try:
        observed = os.lstat(path)
    except FileNotFoundError:
        try:
            os.mkdir(path, 0o700)
        except FileExistsError:
            observed = os.lstat(path)
        else:
            observed = os.lstat(path)
    if stat.S_ISLNK(observed.st_mode) or not stat.S_ISDIR(observed.st_mode) or stat.S_IMODE(observed.st_mode) != 0o700:
        raise HistoricalFastlaneAuthorityError("HISTORICAL_FASTLANE_NAMESPACE_UNSAFE")
    return path


def _fsync_dir(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


@contextmanager
def exclusive_tick(runtime_root: Path):
    """Acquire the outer tick lease with the same pathname/inode checks.

    Callers acquire this before :class:`RunnerCommitLease`; it intentionally
    has no reentrant escape hatch.
    """

    import fcntl
    root = _runtime_root(runtime_root)
    path = root / "tick.lock"
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError as exc:
        raise HistoricalFastlaneAuthorityError("HISTORICAL_FASTLANE_TICK_LOCK_UNSAFE") from exc
    try:
        opened = os.fstat(descriptor)
        live = os.lstat(path)
        if not stat.S_ISREG(live.st_mode) or (live.st_dev, live.st_ino) != (opened.st_dev, opened.st_ino):
            raise HistoricalFastlaneAuthorityError("HISTORICAL_FASTLANE_TICK_LOCK_UNSAFE")
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        live = os.lstat(path)
        if not stat.S_ISREG(live.st_mode) or (live.st_dev, live.st_ino) != (opened.st_dev, opened.st_ino):
            raise HistoricalFastlaneAuthorityError("HISTORICAL_FASTLANE_TICK_LOCK_UNSAFE")
        yield
    finally:
        os.close(descriptor)


def _write_all(descriptor: int, payload: bytes) -> None:
    view = memoryview(payload)
    while view:
        count = os.write(descriptor, view)
        if not isinstance(count, int) or count <= 0:
            raise OSError("short authority write")
        view = view[count:]


def _create_only(path: Path, payload: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(path, flags, 0o600)
    except FileExistsError as exc:
        raise HistoricalFastlaneAuthorityError("HISTORICAL_FASTLANE_RECEIPT_EXISTS") from exc
    try:
        _write_all(descriptor, payload)
        os.fsync(descriptor)
    except BaseException:
        try:
            identity = os.fstat(descriptor)
            live = os.lstat(path)
            if stat.S_ISREG(live.st_mode) and (live.st_dev, live.st_ino) == (identity.st_dev, identity.st_ino):
                os.unlink(path)
        except OSError:
            pass
        raise
    finally:
        os.close(descriptor)
    observed = os.lstat(path)
    if not stat.S_ISREG(observed.st_mode) or stat.S_IMODE(observed.st_mode) != 0o600:
        raise HistoricalFastlaneAuthorityError("HISTORICAL_FASTLANE_RECEIPT_UNSAFE")
    _fsync_dir(path.parent)


def _replace_owned(path: Path, payload: bytes) -> None:
    previous = _streaming_regular_fingerprint(path, label="receipt")
    if previous is None or previous.mode != 0o600:
        raise HistoricalFastlaneAuthorityError("HISTORICAL_FASTLANE_RECEIPT_UNSAFE")
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        _write_all(descriptor, payload)
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        current = _streaming_regular_fingerprint(path, label="receipt")
        if current != previous:
            raise HistoricalFastlaneAuthorityError("HISTORICAL_FASTLANE_RECEIPT_DRIFT")
        os.replace(temporary, path)
        _fsync_dir(path.parent)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


def _self_bound(document: dict[str, object], field: str) -> dict[str, object]:
    body = dict(document)
    digest = _json_sha(body)
    body[field] = digest
    return body


def _validate_self_bound(document: object, *, schema: str, fields: frozenset[str], field: str) -> dict[str, object]:
    if not isinstance(document, dict) or set(document) != fields or document.get("schema_version") != schema:
        raise HistoricalFastlaneAuthorityError("HISTORICAL_FASTLANE_RECEIPT_INVALID")
    declared = document.get(field)
    body = dict(document)
    body.pop(field, None)
    if not isinstance(declared, str) or declared != _json_sha(body):
        raise HistoricalFastlaneAuthorityError("HISTORICAL_FASTLANE_RECEIPT_INVALID")
    return document


_SCOPE_FIELDS = frozenset({
    "schema_version", "status", "recording_date", "grant_id", "deployed_authority",
    "state_before_sha256", "state_after_sha256", "scope_before_sha256", "scope_after_sha256",
    "pointer_diff", "receipt_sha256",
})


def prepare_scope_renewal(
    *, runtime_root: Path, date: str, candidate_ids: tuple[str, ...], new_grant_id: str,
    expires_at: str, expected_state_sha256: str, expected_authority: Mapping[str, str], now: datetime | None = None,
) -> ScopeRenewal:
    """Validate an exact v2 scope and render its sole permitted after-image."""

    root = _runtime_root(runtime_root)
    if not _ID_RX.fullmatch(new_grant_id) or not candidate_ids or len(set(candidate_ids)) != len(candidate_ids) or any(not _ID_RX.fullmatch(cid) for cid in candidate_ids):
        raise HistoricalFastlaneAuthorityError("HISTORICAL_FASTLANE_SCOPE_ARGUMENT_INVALID")
    if not _SHA_RX.fullmatch(expected_state_sha256):
        raise HistoricalFastlaneAuthorityError("HISTORICAL_FASTLANE_STATE_HASH_INVALID")
    now = now or datetime.now(timezone.utc)
    expiry = _utc(expires_at)
    if not now < expiry <= now + _MAX_SCOPE_RENEWAL:
        raise HistoricalFastlaneAuthorityError("HISTORICAL_FASTLANE_SCOPE_EXPIRY_INVALID")
    if dict(expected_authority) != deployment_authority_binding(root):
        raise HistoricalFastlaneAuthorityError("HISTORICAL_FASTLANE_DEPLOYMENT_DRIFT")
    state_path = _state_path(root, date)
    before = read_exact_state_preimage(state_path, runtime_root=root)
    if before is None or _sha(before) != expected_state_sha256:
        raise HistoricalFastlaneAuthorityError("HISTORICAL_FASTLANE_STATE_PREIMAGE_DRIFT")
    state = _parse_state(before)
    terminal_newline = _state_terminal_newline(before, state)
    current = _scope(state, date=date, candidate_ids=candidate_ids, now=now, allow_expired=True)
    after_state = deepcopy(state)
    renewed = deepcopy(current)
    renewed["grant_id"] = new_grant_id
    renewed["expires_at"] = expires_at
    after_state[STATE_KEY] = renewed
    after = state_bytes(after_state) + terminal_newline
    changes = [
        {"path": f"/{STATE_KEY}/grant_id", "before_sha256": _json_sha(current["grant_id"]), "after_sha256": _json_sha(new_grant_id)},
        {"path": f"/{STATE_KEY}/expires_at", "before_sha256": _json_sha(current["expires_at"]), "after_sha256": _json_sha(expires_at)},
    ]
    receipt = _self_bound({
        "schema_version": SCOPE_RENEWAL_SCHEMA, "status": "PREPARED", "recording_date": date,
        "grant_id": new_grant_id, "deployed_authority": dict(expected_authority),
        "state_before_sha256": _sha(before), "state_after_sha256": _sha(after),
        "scope_before_sha256": _json_sha(current), "scope_after_sha256": _json_sha(renewed),
        "pointer_diff": changes,
    }, "receipt_sha256")
    path = root / ".operator-scope-renewals" / f"{date}-{new_grant_id}.json"
    return ScopeRenewal(state_path, before, after, path, receipt)


def commit_scope_renewal(prepared: ScopeRenewal, *, runtime_root: Path) -> Path:
    """Commit/recover only a PREPARED exact after-image under tick then runner."""

    root = _runtime_root(runtime_root)
    if root != _runtime_root(prepared.state_path.parents[1]):
        raise HistoricalFastlaneAuthorityError("HISTORICAL_FASTLANE_RUNTIME_MISMATCH")
    with exclusive_tick(root):
        with exclusive_runner_commit(root) as lease:
            require_runner_commit_lease(lease, runtime_root=root)
            if prepared.receipt.get("deployed_authority") != deployment_authority_binding(root):
                raise HistoricalFastlaneAuthorityError("HISTORICAL_FASTLANE_DEPLOYMENT_DRIFT")
            namespace = _mkdir_private(root, ".operator-scope-renewals")
            if prepared.receipt_path.parent != namespace:
                raise HistoricalFastlaneAuthorityError("HISTORICAL_FASTLANE_NAMESPACE_INVALID")
            current = read_exact_state_preimage(prepared.state_path, runtime_root=root)
            payload = _canonical(prepared.receipt)
            if prepared.receipt_path.exists() or prepared.receipt_path.is_symlink():
                document = _load_scope_receipt(prepared.receipt_path)
                expected_committed = _self_bound(
                    {key: value for key, value in prepared.receipt.items() if key != "receipt_sha256"} | {"status": "COMMITTED"},
                    "receipt_sha256",
                )
                if document == expected_committed and current == prepared.after:
                    return prepared.receipt_path
                if document != prepared.receipt:
                    raise HistoricalFastlaneAuthorityError("HISTORICAL_FASTLANE_RECEIPT_EXISTS")
                if current == prepared.after:
                    _replace_owned(prepared.receipt_path, _canonical(expected_committed))
                    return prepared.receipt_path
                if current != prepared.before:
                    raise HistoricalFastlaneAuthorityError("HISTORICAL_FASTLANE_STATE_PREIMAGE_DRIFT")
            else:
                if current != prepared.before:
                    raise HistoricalFastlaneAuthorityError("HISTORICAL_FASTLANE_STATE_PREIMAGE_DRIFT")
                _create_only(prepared.receipt_path, payload)
            write_exact_state_bytes_under_lease(prepared.state_path, runtime_root=root, lease=lease, expected_before=prepared.before, after_bytes=prepared.after)
            committed = dict(prepared.receipt)
            committed["status"] = "COMMITTED"
            committed = _self_bound({key: value for key, value in committed.items() if key != "receipt_sha256"}, "receipt_sha256")
            _replace_owned(prepared.receipt_path, _canonical(committed))
            return prepared.receipt_path


def _load_scope_receipt(path: Path) -> dict[str, object]:
    snapshot = _streaming_regular_fingerprint(path, label="scope_receipt")
    if snapshot is None or snapshot.mode != 0o600:
        raise HistoricalFastlaneAuthorityError("HISTORICAL_FASTLANE_RECEIPT_INVALID")
    try:
        document = json.loads(_read_small_regular(path, fingerprint=snapshot, label="scope_receipt").decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HistoricalFastlaneAuthorityError("HISTORICAL_FASTLANE_RECEIPT_INVALID") from exc
    return _validate_self_bound(document, schema=SCOPE_RENEWAL_SCHEMA, fields=_SCOPE_FIELDS, field="receipt_sha256")


_RUN_FIELDS = frozenset({
    "schema_version", "status", "nonce", "recording_date", "attempt_limit", "expires_at",
    "runtime_root", "deployed_authority", "state_sha256", "scope_sha256", "source_binding",
    "authorization_adapter_status_sha256", "authorization_adapter_observed_at_epoch",
    "started_adapter_status_sha256", "started_adapter_observed_at_epoch",
    "authorization_direct_recorder_checked_at_epoch", "started_direct_recorder_checked_at_epoch",
    "direct_recorder_idle", "upload_allowed", "receipt_sha256",
})


def _historical_date_only(date: str, *, now: datetime) -> None:
    if not _DATE_RX.fullmatch(date):
        raise HistoricalFastlaneAuthorityError("HISTORICAL_FASTLANE_DATE_INVALID")
    utc_day = now.astimezone(timezone.utc).date().isoformat()
    beijing_day = (now.astimezone(timezone(timedelta(hours=8))).date().isoformat())
    if date >= utc_day or date >= beijing_day:
        raise HistoricalFastlaneAuthorityError("HISTORICAL_FASTLANE_DATE_NOT_CLOSED")


def _directory_entry(path: Path, *, relative: str) -> dict[str, object]:
    try:
        observed = os.lstat(path)
    except OSError as exc:
        raise HistoricalFastlaneAuthorityError("HISTORICAL_FASTLANE_SOURCE_ENTRY_INVALID") from exc
    if stat.S_ISLNK(observed.st_mode) or not stat.S_ISDIR(observed.st_mode):
        raise HistoricalFastlaneAuthorityError("HISTORICAL_FASTLANE_SOURCE_ENTRY_INVALID")
    return {
        "relative_path": relative, "type": "directory", "device": observed.st_dev,
        "inode": observed.st_ino, "mode": stat.S_IMODE(observed.st_mode),
        "mtime_ns": observed.st_mtime_ns, "ctime_ns": observed.st_ctime_ns,
    }


def _source_tree(date_dir: Path) -> list[dict[str, object]]:
    """Bind every target-date node, including staging dirs, without payloads."""

    entries: list[dict[str, object]] = []
    directories: list[tuple[Path, dict[str, object]]] = []

    def visit(path: Path, relative: str) -> None:
        try:
            observed = os.lstat(path)
        except OSError as exc:
            raise HistoricalFastlaneAuthorityError("HISTORICAL_FASTLANE_SOURCE_ENTRY_INVALID") from exc
        if stat.S_ISLNK(observed.st_mode):
            raise HistoricalFastlaneAuthorityError("HISTORICAL_FASTLANE_SOURCE_ENTRY_INVALID")
        if stat.S_ISDIR(observed.st_mode):
            entry = _directory_entry(path, relative=relative)
            entries.append(entry)
            directories.append((path, entry))
            try:
                children = sorted(path.iterdir(), key=lambda item: item.name)
            except OSError as exc:
                raise HistoricalFastlaneAuthorityError("HISTORICAL_FASTLANE_SOURCE_ENTRY_INVALID") from exc
            for child in children:
                child_relative = child.name if relative == "." else f"{relative}/{child.name}"
                if child_relative.startswith("/") or "/../" in f"/{child_relative}/" or child_relative in {"", ".", ".."}:
                    raise HistoricalFastlaneAuthorityError("HISTORICAL_FASTLANE_SOURCE_ENTRY_INVALID")
                visit(child, child_relative)
            return
        if not stat.S_ISREG(observed.st_mode):
            raise HistoricalFastlaneAuthorityError("HISTORICAL_FASTLANE_SOURCE_ENTRY_INVALID")
        fingerprint = _streaming_regular_fingerprint(path, label="source")
        if fingerprint is None:
            raise HistoricalFastlaneAuthorityError("HISTORICAL_FASTLANE_SOURCE_ENTRY_INVALID")
        entries.append({
            "relative_path": relative, "type": "regular", "sha256": fingerprint.sha256,
            "size": fingerprint.size, "device": fingerprint.device, "inode": fingerprint.inode,
            "mode": fingerprint.mode, "mtime_ns": fingerprint.mtime_ns, "ctime_ns": fingerprint.ctime_ns,
        })

    visit(date_dir, ".")
    for path, expected in reversed(directories):
        if _directory_entry(path, relative=str(expected["relative_path"])) != expected:
            raise HistoricalFastlaneAuthorityError("HISTORICAL_FASTLANE_SOURCE_PREIMAGE_DRIFT")
    return entries


def _source_binding(
    recording_root: Path, *, date: str, room_id: object, adapter_state_path: Path,
    now: datetime,
) -> dict[str, object]:
    """Seal the complete admitted date tree, including closed FLVs.

    The normal runner's inventory audit remains the content gate.  This
    stronger preflight binding prevents a manual authority from silently
    narrowing the source directory after approval.
    """

    canonical_room_id = normalize_historical_room_id(room_id)
    root = _safe_dir(recording_root)
    _historical_date_only(date, now=now)
    date_dir = root / date
    _safe_dir(date_dir)
    entries = _source_tree(date_dir)
    if not any(entry.get("type") == "regular" for entry in entries):
        raise HistoricalFastlaneAuthorityError("HISTORICAL_FASTLANE_SOURCE_EMPTY")
    if _streaming_regular_fingerprint(adapter_state_path, label="adapter_state") is None:
        raise HistoricalFastlaneAuthorityError("HISTORICAL_FASTLANE_ADAPTER_STATE_UNAVAILABLE")
    audit = audit_finalized_recording_inventory(
        date_dir, room_id=canonical_room_id, adapter_state_path=adapter_state_path,
    )
    if audit.get("can_select") is not True or not isinstance(audit.get("consumer_segments"), list) or not audit["consumer_segments"]:
        raise HistoricalFastlaneAuthorityError("HISTORICAL_FASTLANE_SOURCE_INVENTORY_BLOCKED")
    return {
        "recording_root": str(root), "date": date, "room_id": canonical_room_id, "entries": entries,
        "inventory_sha256": _json_sha(entries), "inventory_audit_sha256": _json_sha(audit),
        "adapter_state_path": str(adapter_state_path.absolute()),
    }


def _validate_source_binding(
    document: object, *, recording_root: Path, date: str, room_id: object,
    adapter_state_path: Path, now: datetime,
) -> dict[str, object]:
    if not isinstance(document, dict) or set(document) != {
        "recording_root", "date", "room_id", "entries", "inventory_sha256", "inventory_audit_sha256",
        "adapter_state_path",
    }:
        raise HistoricalFastlaneAuthorityError("HISTORICAL_FASTLANE_SOURCE_BINDING_INVALID")
    observed = _source_binding(
        recording_root, date=date, room_id=room_id,
        adapter_state_path=adapter_state_path, now=now,
    )
    if document != observed:
        raise HistoricalFastlaneAuthorityError("HISTORICAL_FASTLANE_SOURCE_PREIMAGE_DRIFT")
    return observed


def _adapter_snapshot(path: Path, *, room_id: object, now: datetime) -> str:
    canonical_room_id = normalize_historical_room_id(room_id)
    snapshot = _streaming_regular_fingerprint(path, label="adapter_status")
    if snapshot is None:
        raise HistoricalFastlaneAuthorityError("HISTORICAL_FASTLANE_ADAPTER_STATUS_UNAVAILABLE")
    try:
        document = json.loads(_read_small_regular(path, fingerprint=snapshot, label="adapter_status").decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HistoricalFastlaneAuthorityError("HISTORICAL_FASTLANE_ADAPTER_STATUS_INVALID") from exc
    if not isinstance(document, dict):
        raise HistoricalFastlaneAuthorityError("HISTORICAL_FASTLANE_ADAPTER_STATUS_INVALID")
    try:
        age = now.timestamp() - float(document.get("generated_at_epoch"))
    except (TypeError, ValueError):
        raise HistoricalFastlaneAuthorityError("HISTORICAL_FASTLANE_ADAPTER_STATUS_STALE")
    if age < -300 or age > _ADAPTER_STATUS_MAX_AGE_SECONDS:
        raise HistoricalFastlaneAuthorityError("HISTORICAL_FASTLANE_ADAPTER_STATUS_STALE")
    if (
        document.get("schema_version") != "recorder-neutral-status.v1"
        or document.get("service_reachable") is not True
        or document.get("streaming") is not False
        or document.get("recording") is not False
        or document.get("finalizing") is not False
        or document.get("live_status") not in (0, False)
        or document.get("error") not in (None, "")
    ):
        raise HistoricalFastlaneAuthorityError("HISTORICAL_FASTLANE_ADAPTER_NOT_CLEAN_IDLE")
    try:
        observed_room_id = normalize_historical_room_id(document.get("room_id"))
    except HistoricalFastlaneAuthorityError as exc:
        raise HistoricalFastlaneAuthorityError("HISTORICAL_FASTLANE_ADAPTER_STATUS_INVALID") from exc
    if observed_room_id != canonical_room_id:
        raise HistoricalFastlaneAuthorityError("HISTORICAL_FASTLANE_ADAPTER_NOT_CLEAN_IDLE")
    return snapshot.sha256


def _direct_recorder_idle(*, endpoint: str, env_file: Path, room_id: object) -> None:
    """Query BililiveRecorder directly; credentials never enter the receipt."""

    canonical_room_id = normalize_historical_room_id(room_id)
    before = _streaming_regular_fingerprint(env_file, label="recorder_env")
    if before is None:
        raise HistoricalFastlaneAuthorityError("HISTORICAL_FASTLANE_DIRECT_RECORDER_UNAVAILABLE")
    environment = load_env_file(env_file)
    if _streaming_regular_fingerprint(env_file, label="recorder_env") != before:
        raise HistoricalFastlaneAuthorityError("HISTORICAL_FASTLANE_DIRECT_RECORDER_UNAVAILABLE")
    try:
        observed = query_room_status(
            endpoint, canonical_room_id,
            username=environment.get("BREC_HTTP_BASIC_USER", ""),
            password=environment.get("BREC_HTTP_BASIC_PASS", ""),
        )
    except (AdapterError, OSError, ValueError) as exc:
        raise HistoricalFastlaneAuthorityError("HISTORICAL_FASTLANE_DIRECT_RECORDER_UNAVAILABLE") from exc
    if not isinstance(observed, Mapping):
        raise HistoricalFastlaneAuthorityError("HISTORICAL_FASTLANE_DIRECT_RECORDER_UNAVAILABLE")
    if observed.get("streaming") is not False or observed.get("recording") is not False:
        raise HistoricalFastlaneAuthorityError("HISTORICAL_FASTLANE_DIRECT_RECORDER_NOT_IDLE")


def prepare_historical_run_authority(
    *, runtime_root: Path, recording_root: Path, adapter_status_path: Path,
    date: str, candidate_ids: tuple[str, ...], nonce: str, expires_at: str,
    expected_state_sha256: str, expected_authority: Mapping[str, str], room_id: object,
    adapter_state_path: Path, recorder_endpoint: str, recorder_env: Path,
    now: datetime | None = None, clock: Callable[[], datetime] | None = None,
) -> tuple[Path, dict[str, object]]:
    """Render a one-time, disabled-only historical runner authority.

    This is deliberately side-effect free: the caller may inspect its output
    in dry-run mode before creating the authority file.
    """

    root = _runtime_root(runtime_root)
    canonical_room_id = normalize_historical_room_id(room_id)
    initial_now = now or (clock() if clock is not None else datetime.now(timezone.utc))
    if not _NONCE_RX.fullmatch(nonce) or not _DATE_RX.fullmatch(date) or not candidate_ids or len(set(candidate_ids)) != len(candidate_ids) or any(not _ID_RX.fullmatch(cid) for cid in candidate_ids):
        raise HistoricalFastlaneAuthorityError("HISTORICAL_FASTLANE_RUN_ARGUMENT_INVALID")
    _historical_date_only(date, now=initial_now)
    if not (root / "DISABLED").exists() or (root / "DISABLED").is_symlink():
        raise HistoricalFastlaneAuthorityError("HISTORICAL_FASTLANE_DISABLED_REQUIRED")
    expiry = _utc(expires_at)
    if not initial_now < expiry <= initial_now + _MAX_HISTORICAL_RUN:
        raise HistoricalFastlaneAuthorityError("HISTORICAL_FASTLANE_RUN_EXPIRY_INVALID")
    authority = deployment_authority_binding(root)
    if dict(expected_authority) != authority:
        raise HistoricalFastlaneAuthorityError("HISTORICAL_FASTLANE_DEPLOYMENT_DRIFT")
    state_path = _state_path(root, date)
    state_bytes_before = read_exact_state_preimage(state_path, runtime_root=root)
    if state_bytes_before is None or _sha(state_bytes_before) != expected_state_sha256:
        raise HistoricalFastlaneAuthorityError("HISTORICAL_FASTLANE_STATE_PREIMAGE_DRIFT")
    state = _parse_state(state_bytes_before)
    scope = _scope(state, date=date, candidate_ids=candidate_ids, now=initial_now)
    source = _source_binding(
        recording_root, date=date, room_id=canonical_room_id,
        adapter_state_path=adapter_state_path, now=initial_now,
    )
    observed_now = clock() if clock is not None else (now if now is not None else datetime.now(timezone.utc))
    _historical_date_only(date, now=observed_now)
    if expiry <= observed_now:
        raise HistoricalFastlaneAuthorityError("HISTORICAL_FASTLANE_RUN_EXPIRED")
    if _json_sha(_scope(state, date=date, candidate_ids=candidate_ids, now=observed_now)) != _json_sha(scope):
        raise HistoricalFastlaneAuthorityError("HISTORICAL_FASTLANE_SCOPE_PREIMAGE_DRIFT")
    adapter_sha = _adapter_snapshot(adapter_status_path, room_id=canonical_room_id, now=observed_now)
    _direct_recorder_idle(endpoint=recorder_endpoint, env_file=recorder_env, room_id=canonical_room_id)
    document = _self_bound({
        "schema_version": HISTORICAL_RUN_SCHEMA, "status": "PREPARED", "nonce": nonce,
        "recording_date": date, "attempt_limit": 1, "expires_at": expires_at,
        "runtime_root": str(root), "deployed_authority": authority,
        "state_sha256": _sha(state_bytes_before), "scope_sha256": _json_sha(scope),
        "source_binding": source,
        "authorization_adapter_status_sha256": adapter_sha,
        "authorization_adapter_observed_at_epoch": observed_now.timestamp(),
        "started_adapter_status_sha256": None,
        "started_adapter_observed_at_epoch": None,
        "authorization_direct_recorder_checked_at_epoch": observed_now.timestamp(),
        "started_direct_recorder_checked_at_epoch": None,
        "direct_recorder_idle": True, "upload_allowed": False,
    }, "receipt_sha256")
    return root / ".historical-autoslice-once" / f"{date}-{nonce}.json", document


def create_historical_run_authority(path: Path, document: Mapping[str, object]) -> Path:
    """Create a never-reusable PREPARED authority after strict self validation."""

    normalized = _validate_run_authority(document)
    if normalized["status"] != "PREPARED":
        raise HistoricalFastlaneAuthorityError("HISTORICAL_FASTLANE_RUN_STATUS_INVALID")
    root = Path(str(normalized["runtime_root"]))
    namespace = _mkdir_private(_runtime_root(root), ".historical-autoslice-once")
    if path.parent != namespace or path.name != f"{normalized['recording_date']}-{normalized['nonce']}.json":
        raise HistoricalFastlaneAuthorityError("HISTORICAL_FASTLANE_NAMESPACE_INVALID")
    _create_only(path, _canonical(normalized))
    return path


def _load_run_authority(path: Path) -> dict[str, object]:
    snapshot = _streaming_regular_fingerprint(path, label="run_receipt")
    if snapshot is None or snapshot.mode != 0o600:
        raise HistoricalFastlaneAuthorityError("HISTORICAL_FASTLANE_RUN_RECEIPT_INVALID")
    try:
        document = json.loads(_read_small_regular(path, fingerprint=snapshot, label="run_receipt").decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HistoricalFastlaneAuthorityError("HISTORICAL_FASTLANE_RUN_RECEIPT_INVALID") from exc
    return _validate_run_authority(document)


def _validate_run_authority(document: object) -> dict[str, object]:
    value = _validate_self_bound(document, schema=HISTORICAL_RUN_SCHEMA, fields=_RUN_FIELDS, field="receipt_sha256")
    if (
        value.get("status") not in {"PREPARED", "STARTED", "COMPLETED", "FAILED"}
        or not isinstance(value.get("nonce"), str) or not _NONCE_RX.fullmatch(value["nonce"])
        or not isinstance(value.get("recording_date"), str) or not _DATE_RX.fullmatch(value["recording_date"])
        or value.get("attempt_limit") != 1 or value.get("upload_allowed") is not False
        or value.get("direct_recorder_idle") is not True
        or not isinstance(value.get("runtime_root"), str)
        or not isinstance(value.get("deployed_authority"), dict)
        or any(not isinstance(value.get(key), str) or not _SHA_RX.fullmatch(value[key]) for key in ("state_sha256", "scope_sha256", "authorization_adapter_status_sha256"))
    ):
        raise HistoricalFastlaneAuthorityError("HISTORICAL_FASTLANE_RUN_RECEIPT_INVALID")
    if not isinstance(value.get("authorization_adapter_observed_at_epoch"), (int, float)) or isinstance(value.get("authorization_adapter_observed_at_epoch"), bool):
        raise HistoricalFastlaneAuthorityError("HISTORICAL_FASTLANE_RUN_RECEIPT_INVALID")
    if not isinstance(value.get("authorization_direct_recorder_checked_at_epoch"), (int, float)) or isinstance(value.get("authorization_direct_recorder_checked_at_epoch"), bool):
        raise HistoricalFastlaneAuthorityError("HISTORICAL_FASTLANE_RUN_RECEIPT_INVALID")
    started = value.get("status") in {"STARTED", "COMPLETED", "FAILED"}
    if started:
        if not isinstance(value.get("started_adapter_status_sha256"), str) or not _SHA_RX.fullmatch(value["started_adapter_status_sha256"]):
            raise HistoricalFastlaneAuthorityError("HISTORICAL_FASTLANE_RUN_RECEIPT_INVALID")
        if any(not isinstance(value.get(key), (int, float)) or isinstance(value.get(key), bool) for key in ("started_adapter_observed_at_epoch", "started_direct_recorder_checked_at_epoch")):
            raise HistoricalFastlaneAuthorityError("HISTORICAL_FASTLANE_RUN_RECEIPT_INVALID")
    elif value.get("started_adapter_status_sha256") is not None or value.get("started_adapter_observed_at_epoch") is not None or value.get("started_direct_recorder_checked_at_epoch") is not None:
        raise HistoricalFastlaneAuthorityError("HISTORICAL_FASTLANE_RUN_RECEIPT_INVALID")
    _utc(str(value.get("expires_at") or ""))
    return value


def load_and_start_historical_run(
    *, authority_path: Path, runtime_root: Path, recording_root: Path, adapter_status_path: Path,
    adapter_state_path: Path, recorder_endpoint: str, recorder_env: Path,
    now: datetime | None = None, room_id: object, clock: Callable[[], datetime] | None = None,
) -> dict[str, object]:
    """Revalidate and atomically mark PREPARED -> STARTED before normal tick.

    A surviving ``STARTED`` receipt is intentionally not replayable: a caller
    cannot know whether a provider side effect occurred after the checkpoint.
    """

    root = _runtime_root(runtime_root)
    canonical_room_id = normalize_historical_room_id(room_id)
    initial_now = now or (clock() if clock is not None else datetime.now(timezone.utc))
    document = _load_run_authority(authority_path)
    namespace = _safe_dir(root / ".historical-autoslice-once", mode=0o700)
    if authority_path.parent != namespace or authority_path.name != f"{document['recording_date']}-{document['nonce']}.json":
        raise HistoricalFastlaneAuthorityError("HISTORICAL_FASTLANE_NAMESPACE_INVALID")
    if document.get("runtime_root") != str(root) or document.get("status") != "PREPARED":
        raise HistoricalFastlaneAuthorityError("HISTORICAL_FASTLANE_RUN_REPLAY_REFUSED")
    if _utc(str(document["expires_at"])) <= initial_now:
        raise HistoricalFastlaneAuthorityError("HISTORICAL_FASTLANE_RUN_EXPIRED")
    if not (root / "DISABLED").exists() or (root / "DISABLED").is_symlink():
        raise HistoricalFastlaneAuthorityError("HISTORICAL_FASTLANE_DISABLED_REQUIRED")
    if document.get("deployed_authority") != deployment_authority_binding(root):
        raise HistoricalFastlaneAuthorityError("HISTORICAL_FASTLANE_DEPLOYMENT_DRIFT")
    date = str(document["recording_date"])
    current = read_exact_state_preimage(_state_path(root, date), runtime_root=root)
    if current is None or _sha(current) != document["state_sha256"]:
        raise HistoricalFastlaneAuthorityError("HISTORICAL_FASTLANE_STATE_PREIMAGE_DRIFT")
    state = _parse_state(current)
    ids = tuple((state.get(STATE_KEY) or {}).get("candidate_ids") or ())
    scope = _scope(state, date=date, candidate_ids=ids, now=initial_now)
    if _json_sha(scope) != document["scope_sha256"]:
        raise HistoricalFastlaneAuthorityError("HISTORICAL_FASTLANE_SCOPE_PREIMAGE_DRIFT")
    _validate_source_binding(
        document["source_binding"], recording_root=recording_root, date=date, room_id=canonical_room_id,
        adapter_state_path=adapter_state_path, now=initial_now,
    )
    observed_now = clock() if clock is not None else (now if now is not None else datetime.now(timezone.utc))
    _historical_date_only(date, now=observed_now)
    if _utc(str(document["expires_at"])) <= observed_now:
        raise HistoricalFastlaneAuthorityError("HISTORICAL_FASTLANE_RUN_EXPIRED")
    if not (root / "DISABLED").exists() or (root / "DISABLED").is_symlink():
        raise HistoricalFastlaneAuthorityError("HISTORICAL_FASTLANE_DISABLED_REQUIRED")
    if document.get("deployed_authority") != deployment_authority_binding(root):
        raise HistoricalFastlaneAuthorityError("HISTORICAL_FASTLANE_DEPLOYMENT_DRIFT")
    current = read_exact_state_preimage(_state_path(root, date), runtime_root=root)
    if current is None or _sha(current) != document["state_sha256"]:
        raise HistoricalFastlaneAuthorityError("HISTORICAL_FASTLANE_STATE_PREIMAGE_DRIFT")
    state = _parse_state(current)
    ids = tuple((state.get(STATE_KEY) or {}).get("candidate_ids") or ())
    scope = _scope(state, date=date, candidate_ids=ids, now=observed_now)
    if _json_sha(scope) != document["scope_sha256"]:
        raise HistoricalFastlaneAuthorityError("HISTORICAL_FASTLANE_SCOPE_PREIMAGE_DRIFT")
    started_adapter_sha = _adapter_snapshot(adapter_status_path, room_id=canonical_room_id, now=observed_now)
    _direct_recorder_idle(endpoint=recorder_endpoint, env_file=recorder_env, room_id=canonical_room_id)
    with exclusive_runner_commit(root):
        current_document = _load_run_authority(authority_path)
        if current_document != document:
            raise HistoricalFastlaneAuthorityError("HISTORICAL_FASTLANE_RUN_REPLAY_REFUSED")
        started = _self_bound(
            {key: value for key, value in document.items() if key != "receipt_sha256"} | {
                "status": "STARTED",
                "started_adapter_status_sha256": started_adapter_sha,
                "started_adapter_observed_at_epoch": observed_now.timestamp(),
                "started_direct_recorder_checked_at_epoch": observed_now.timestamp(),
            },
            "receipt_sha256",
        )
        _replace_owned(authority_path, _canonical(started))
    return started


def finish_historical_run(*, authority_path: Path, runtime_root: Path, failed: bool = False) -> None:
    """Write the only terminal state; terminal receipts are never reusable."""

    root = _runtime_root(runtime_root)
    with exclusive_runner_commit(root):
        document = _load_run_authority(authority_path)
        namespace = _safe_dir(root / ".historical-autoslice-once", mode=0o700)
        if (
            authority_path.parent != namespace
            or authority_path.name != f"{document['recording_date']}-{document['nonce']}.json"
            or document.get("status") != "STARTED"
            or document.get("runtime_root") != str(root)
        ):
            raise HistoricalFastlaneAuthorityError("HISTORICAL_FASTLANE_RUN_REPLAY_REFUSED")
        terminal = _self_bound(
            {key: value for key, value in document.items() if key != "receipt_sha256"} | {"status": "FAILED" if failed else "COMPLETED"},
            "receipt_sha256",
        )
        _replace_owned(authority_path, _canonical(terminal))
