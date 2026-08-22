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
from typing import Mapping

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
    stable_regular_snapshot,
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
_MAX_SCOPE_RENEWAL = timedelta(days=14)
_MAX_HISTORICAL_RUN = timedelta(minutes=90)


class HistoricalFastlaneAuthorityError(RuntimeError):
    """A historical exception lacks a sealed, local authority."""


@dataclass(frozen=True, slots=True)
class ScopeRenewal:
    state_path: Path
    before: bytes
    after: bytes
    receipt_path: Path
    receipt: dict[str, object]


def _canonical(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _sha(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


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


def _scope(state: Mapping[str, object], *, date: str, candidate_ids: tuple[str, ...], now: datetime) -> dict[str, object]:
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
    if expires <= now:
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
    previous = stable_regular_snapshot(path, label="historical authority receipt")
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
        current = stable_regular_snapshot(path, label="historical authority receipt")
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
    current = _scope(state, date=date, candidate_ids=candidate_ids, now=now)
    after_state = deepcopy(state)
    renewed = deepcopy(current)
    renewed["grant_id"] = new_grant_id
    renewed["expires_at"] = expires_at
    after_state[STATE_KEY] = renewed
    after = state_bytes(after_state)
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
    snapshot = stable_regular_snapshot(path, label="scope renewal receipt")
    if snapshot is None or snapshot.mode != 0o600:
        raise HistoricalFastlaneAuthorityError("HISTORICAL_FASTLANE_RECEIPT_INVALID")
    try:
        document = json.loads(snapshot.payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HistoricalFastlaneAuthorityError("HISTORICAL_FASTLANE_RECEIPT_INVALID") from exc
    return _validate_self_bound(document, schema=SCOPE_RENEWAL_SCHEMA, fields=_SCOPE_FIELDS, field="receipt_sha256")


_RUN_FIELDS = frozenset({
    "schema_version", "status", "nonce", "recording_date", "attempt_limit", "expires_at",
    "runtime_root", "deployed_authority", "state_sha256", "scope_sha256", "source_binding",
    "adapter_status_sha256", "direct_recorder_idle", "upload_allowed", "receipt_sha256",
})


def _source_binding(recording_root: Path, *, date: str, room_id: int) -> dict[str, object]:
    """Seal the complete admitted date tree, including closed FLVs.

    The normal runner's inventory audit remains the content gate.  This
    stronger preflight binding prevents a manual authority from silently
    narrowing the source directory after approval.
    """

    root = _safe_dir(recording_root)
    names = sorted(child.name for child in root.iterdir() if child.is_dir() and _DATE_RX.fullmatch(child.name))
    if names != [date]:
        raise HistoricalFastlaneAuthorityError("HISTORICAL_FASTLANE_SOURCE_DATE_SET_INVALID")
    date_dir = root / date
    _safe_dir(date_dir)
    entries: list[dict[str, object]] = []
    for child in sorted(date_dir.iterdir(), key=lambda item: item.name):
        snapshot = stable_regular_snapshot(child, label="historical source")
        if snapshot is None:
            raise HistoricalFastlaneAuthorityError("HISTORICAL_FASTLANE_SOURCE_ENTRY_INVALID")
        entries.append({
            "name": child.name, "sha256": snapshot.sha256, "size": snapshot.size,
            "device": snapshot.device, "inode": snapshot.inode, "mode": snapshot.mode,
        })
    if not entries:
        raise HistoricalFastlaneAuthorityError("HISTORICAL_FASTLANE_SOURCE_EMPTY")
    audit = audit_finalized_recording_inventory(date_dir, room_id=room_id)
    if audit.get("can_select") is not True:
        raise HistoricalFastlaneAuthorityError("HISTORICAL_FASTLANE_SOURCE_INVENTORY_BLOCKED")
    return {
        "recording_root": str(root), "date": date, "room_id": room_id, "entries": entries,
        "inventory_sha256": _json_sha(entries), "inventory_audit_sha256": _json_sha(audit),
    }


def _validate_source_binding(document: object, *, recording_root: Path, date: str, room_id: int) -> dict[str, object]:
    if not isinstance(document, dict) or set(document) != {"recording_root", "date", "room_id", "entries", "inventory_sha256", "inventory_audit_sha256"}:
        raise HistoricalFastlaneAuthorityError("HISTORICAL_FASTLANE_SOURCE_BINDING_INVALID")
    observed = _source_binding(recording_root, date=date, room_id=room_id)
    if document != observed:
        raise HistoricalFastlaneAuthorityError("HISTORICAL_FASTLANE_SOURCE_PREIMAGE_DRIFT")
    return observed


def _adapter_snapshot(path: Path) -> str:
    snapshot = stable_regular_snapshot(path, label="recorder adapter status")
    if snapshot is None:
        raise HistoricalFastlaneAuthorityError("HISTORICAL_FASTLANE_ADAPTER_STATUS_UNAVAILABLE")
    try:
        document = json.loads(snapshot.payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HistoricalFastlaneAuthorityError("HISTORICAL_FASTLANE_ADAPTER_STATUS_INVALID") from exc
    if not isinstance(document, dict) or document.get("service_reachable") is not True or document.get("streaming") is not False or document.get("recording") is not False or document.get("finalizing") is not False or document.get("error") not in (None, ""):
        raise HistoricalFastlaneAuthorityError("HISTORICAL_FASTLANE_ADAPTER_NOT_CLEAN_IDLE")
    return snapshot.sha256


def prepare_historical_run_authority(
    *, runtime_root: Path, recording_root: Path, adapter_status_path: Path,
    date: str, candidate_ids: tuple[str, ...], nonce: str, expires_at: str,
    expected_state_sha256: str, expected_authority: Mapping[str, str], direct_recorder_idle: bool, room_id: int,
    now: datetime | None = None,
) -> tuple[Path, dict[str, object]]:
    """Render a one-time, disabled-only historical runner authority.

    This is deliberately side-effect free: the caller may inspect its output
    in dry-run mode before creating the authority file.
    """

    root = _runtime_root(runtime_root)
    now = now or datetime.now(timezone.utc)
    if not _NONCE_RX.fullmatch(nonce) or not _DATE_RX.fullmatch(date) or not candidate_ids or len(set(candidate_ids)) != len(candidate_ids) or any(not _ID_RX.fullmatch(cid) for cid in candidate_ids):
        raise HistoricalFastlaneAuthorityError("HISTORICAL_FASTLANE_RUN_ARGUMENT_INVALID")
    if not (root / "DISABLED").exists() or (root / "DISABLED").is_symlink():
        raise HistoricalFastlaneAuthorityError("HISTORICAL_FASTLANE_DISABLED_REQUIRED")
    if not direct_recorder_idle:
        raise HistoricalFastlaneAuthorityError("HISTORICAL_FASTLANE_DIRECT_RECORDER_NOT_IDLE")
    expiry = _utc(expires_at)
    if not now < expiry <= now + _MAX_HISTORICAL_RUN:
        raise HistoricalFastlaneAuthorityError("HISTORICAL_FASTLANE_RUN_EXPIRY_INVALID")
    authority = deployment_authority_binding(root)
    if dict(expected_authority) != authority:
        raise HistoricalFastlaneAuthorityError("HISTORICAL_FASTLANE_DEPLOYMENT_DRIFT")
    state_path = _state_path(root, date)
    state_bytes_before = read_exact_state_preimage(state_path, runtime_root=root)
    if state_bytes_before is None or _sha(state_bytes_before) != expected_state_sha256:
        raise HistoricalFastlaneAuthorityError("HISTORICAL_FASTLANE_STATE_PREIMAGE_DRIFT")
    state = _parse_state(state_bytes_before)
    scope = _scope(state, date=date, candidate_ids=candidate_ids, now=now)
    source = _source_binding(recording_root, date=date, room_id=room_id)
    adapter_sha = _adapter_snapshot(adapter_status_path)
    document = _self_bound({
        "schema_version": HISTORICAL_RUN_SCHEMA, "status": "PREPARED", "nonce": nonce,
        "recording_date": date, "attempt_limit": 1, "expires_at": expires_at,
        "runtime_root": str(root), "deployed_authority": authority,
        "state_sha256": _sha(state_bytes_before), "scope_sha256": _json_sha(scope),
        "source_binding": source, "adapter_status_sha256": adapter_sha,
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
    snapshot = stable_regular_snapshot(path, label="historical run authority")
    if snapshot is None or snapshot.mode != 0o600:
        raise HistoricalFastlaneAuthorityError("HISTORICAL_FASTLANE_RUN_RECEIPT_INVALID")
    try:
        document = json.loads(snapshot.payload.decode("utf-8"))
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
        or any(not isinstance(value.get(key), str) or not _SHA_RX.fullmatch(value[key]) for key in ("state_sha256", "scope_sha256", "adapter_status_sha256"))
    ):
        raise HistoricalFastlaneAuthorityError("HISTORICAL_FASTLANE_RUN_RECEIPT_INVALID")
    _utc(str(value.get("expires_at") or ""))
    return value


def load_and_start_historical_run(
    *, authority_path: Path, runtime_root: Path, recording_root: Path, adapter_status_path: Path,
    now: datetime | None = None, room_id: int,
) -> dict[str, object]:
    """Revalidate and atomically mark PREPARED -> STARTED before normal tick.

    A surviving ``STARTED`` receipt is intentionally not replayable: a caller
    cannot know whether a provider side effect occurred after the checkpoint.
    """

    root = _runtime_root(runtime_root)
    now = now or datetime.now(timezone.utc)
    document = _load_run_authority(authority_path)
    namespace = _safe_dir(root / ".historical-autoslice-once", mode=0o700)
    if authority_path.parent != namespace or authority_path.name != f"{document['recording_date']}-{document['nonce']}.json":
        raise HistoricalFastlaneAuthorityError("HISTORICAL_FASTLANE_NAMESPACE_INVALID")
    if document.get("runtime_root") != str(root) or document.get("status") != "PREPARED":
        raise HistoricalFastlaneAuthorityError("HISTORICAL_FASTLANE_RUN_REPLAY_REFUSED")
    if _utc(str(document["expires_at"])) <= now:
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
    scope = _scope(state, date=date, candidate_ids=ids, now=now)
    if _json_sha(scope) != document["scope_sha256"]:
        raise HistoricalFastlaneAuthorityError("HISTORICAL_FASTLANE_SCOPE_PREIMAGE_DRIFT")
    _validate_source_binding(document["source_binding"], recording_root=recording_root, date=date, room_id=room_id)
    if _adapter_snapshot(adapter_status_path) != document["adapter_status_sha256"]:
        raise HistoricalFastlaneAuthorityError("HISTORICAL_FASTLANE_ADAPTER_STATUS_DRIFT")
    with exclusive_runner_commit(root):
        current_document = _load_run_authority(authority_path)
        if current_document != document:
            raise HistoricalFastlaneAuthorityError("HISTORICAL_FASTLANE_RUN_REPLAY_REFUSED")
        started = _self_bound(
            {key: value for key, value in document.items() if key != "receipt_sha256"} | {"status": "STARTED"},
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
