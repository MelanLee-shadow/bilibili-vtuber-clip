"""Monotonic private receipt for one source-scoped native audio budget."""

from __future__ import annotations

from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
import threading
from typing import Any, Mapping

from src.autoslice.supplement_audio_budget import (
    MAX_WINDOW_MS,
    _ATTEMPT_FINAL_STATUSES,
    get_budget,
)


SCHEMA_VERSION = "native-audio-budget.v2"


class BudgetReceiptPersistenceError(OSError):
    """A native attempt cannot succeed without a current budget receipt."""

    reason_code = "LOCAL_AUDIO_BUDGET_RECEIPT_PERSIST_FAILED"


def _no_links(path: Path) -> None:
    if any(parent.is_symlink() for parent in (path, *path.parents)):
        raise ValueError("native audio budget receipt path contains a symlink")


class _ReceiptLockState:
    def __init__(self) -> None:
        self.mutex = threading.RLock()
        self.fd: int | None = None


_PROCESS_LOCKS_GUARD = threading.Lock()
_PROCESS_LOCKS: dict[tuple[int, str], _ReceiptLockState] = {}


def _forget_inherited_locks() -> None:
    # Close child duplicates, never LOCK_UN the parent's shared open description.
    global _PROCESS_LOCKS_GUARD, _PROCESS_LOCKS
    for state in _PROCESS_LOCKS.values():
        if state.fd is not None:
            os.close(state.fd)
    _PROCESS_LOCKS_GUARD = threading.Lock()
    _PROCESS_LOCKS = {}


os.register_at_fork(after_in_child=_forget_inherited_locks)


def _validate_lock_identity(path: Path, fd: int) -> None:
    _no_links(path)
    current, opened = path.lstat(), os.fstat(fd)
    if (
        (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino)
        or not stat.S_ISREG(opened.st_mode)
        or opened.st_uid != os.geteuid()
        or stat.S_IMODE(opened.st_mode) != 0o600
        or opened.st_nlink != 1
        or opened.st_size != 0
    ):
        raise BudgetReceiptPersistenceError("native audio budget lock identity is unsafe")


@contextmanager
def exclusive_native_audio_budget(receipt_path: Path):
    """Serialize cooperating processes, without restoring budget or authorizing spend.

    Acquire after the source's existing RLock. The nested canonical writer reuses
    this thread's fd; another process/thread fails before media/provider work.
    Keep the empty lock inode after release so waiters never lock different files.
    """
    path = receipt_path.absolute()
    lock_path = path.with_name(path.name + ".lock")
    owner_pid = os.getpid()
    with _PROCESS_LOCKS_GUARD:
        state = _PROCESS_LOCKS.setdefault((owner_pid, str(path)), _ReceiptLockState())
    if not state.mutex.acquire(blocking=False):
        raise BudgetReceiptPersistenceError("native audio budget receipt is already in use")
    opened: int | None = None
    try:
        try:
            _no_links(path)
            _no_links(lock_path)
            if state.fd is None:
                lock_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                _no_links(lock_path)
                opened = os.open(
                    lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK
                    | os.O_CLOEXEC, 0o600,
                )
                _validate_lock_identity(lock_path, opened)
                fcntl.flock(opened, fcntl.LOCK_EX | fcntl.LOCK_NB)
                _validate_lock_identity(lock_path, opened)
                state.fd = opened
            else:
                _validate_lock_identity(lock_path, state.fd)
        except (OSError, ValueError) as exc:
            if isinstance(exc, BudgetReceiptPersistenceError):
                raise
            raise BudgetReceiptPersistenceError(
                "native audio budget lock unavailable or held by another process"
            ) from exc
        yield
    finally:
        if os.getpid() == owner_pid:
            if opened is not None:
                state.fd = None
                os.close(opened)
            state.mutex.release()


def _canonical_sha256(value: Mapping[str, object]) -> str:
    raw = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def receipt_sha256(receipt: Mapping[str, object]) -> str:
    """Hash a receipt without its self-referential digest."""

    return _canonical_sha256(
        {
            str(key): value
            for key, value in receipt.items()
            if key != "receipt_sha256"
        }
    )


def _providers(snapshot: Mapping[str, object]) -> list[str]:
    values: set[str] = set()
    for key in ("attempts", "cache_hits", "refusals"):
        rows = snapshot.get(key)
        if not isinstance(rows, list):
            continue
        for row in rows:
            provider = row.get("provider") if isinstance(row, Mapping) else None
            if isinstance(provider, str) and provider:
                values.add(provider)
    return sorted(values)


def _load_existing(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    if not path.is_file() or path.is_symlink():
        raise BudgetReceiptPersistenceError(
            "native audio budget receipt is not a regular file"
        )
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise BudgetReceiptPersistenceError(
            "native audio budget receipt is unreadable"
        ) from exc
    if not isinstance(value, dict):
        raise BudgetReceiptPersistenceError(
            "native audio budget receipt is not an object"
        )
    return value


def _validate_existing(
    existing: Mapping[str, object],
    *,
    source_media_sha256: str,
) -> int:
    budget = existing.get("budget")
    revision = budget.get("revision") if isinstance(budget, Mapping) else None
    if (
        existing.get("schema_version") != SCHEMA_VERSION
        or existing.get("source_media_sha256") != source_media_sha256
        or isinstance(revision, bool)
        or not isinstance(revision, int)
        or revision < 0
        or existing.get("revision") != revision
        or existing.get("receipt_sha256") != receipt_sha256(existing)
    ):
        raise BudgetReceiptPersistenceError(
            "native audio budget receipt binding is invalid"
        )
    return revision


def require_native_audio_budget_continuity(
    *,
    source_media: Path,
    source_media_sha256: str,
    receipt_path: Path,
    require_existing: bool = False,
) -> None:
    """Fail closed unless an occupied receipt equals the active process ledger.

    This is a read-only admission check, not cross-process budget restoration.
    Reuse the existing typed receipt error; never infer balance from revision.
    The caller holds the active budget lock through observation and persistence.
    """
    resolved_receipt = receipt_path.absolute()
    try:
        _no_links(resolved_receipt)
    except (OSError, ValueError) as exc:
        raise BudgetReceiptPersistenceError(
            "native audio budget continuity path is unsafe"
        ) from exc
    existing = _load_existing(resolved_receipt)
    if existing is None:
        if require_existing:
            raise BudgetReceiptPersistenceError(
                "native audio budget receipt disappeared before observation"
            )
        return
    _validate_existing(existing, source_media_sha256=source_media_sha256)
    # Matching hashes and matching snapshots do not prove valid event structure.
    _validate_budget_history(existing["budget"])
    budget = get_budget(source_media)
    if budget is None:
        raise BudgetReceiptPersistenceError(
            "existing native audio budget requires process-ledger reconciliation"
        )
    with budget.lock:
        snapshot = budget.snapshot()
        _validate_budget_history(snapshot)
        expected = {
            "schema_version": SCHEMA_VERSION,
            "source_media_sha256": source_media_sha256,
            "revision": snapshot.get("revision"),
            "providers": _providers(snapshot),
            "budget": snapshot,
        }
        expected["receipt_sha256"] = receipt_sha256(expected)
        if snapshot.get("source_media") != str(source_media.resolve()) or existing != expected:
            raise BudgetReceiptPersistenceError(
                "native audio budget receipt does not match the active process ledger"
            )
        if snapshot.get("pending_attempt_count") != 0:
            raise BudgetReceiptPersistenceError(
                "native audio budget has an unresolved dispatched attempt"
            )


def _history_error(detail: str) -> None:
    raise BudgetReceiptPersistenceError("native audio budget history " + detail)


def _validate_budget_history(snapshot: Mapping[str, object]) -> None:
    """Validate the frozen v2 event ledger and its derived counters.

    Snapshot identity is not run ownership. This check does not restore a
    budget, add a provider dispatch, or authorize a pending audio request.
    """
    if not isinstance(snapshot, Mapping):
        _history_error("snapshot is not an object")
    limits = ("max_windows", "max_window_ms", "max_audio_ms")
    for key in limits:
        if type(snapshot.get(key)) is not int or snapshot[key] <= 0:
            _history_error("limits are invalid")
    if snapshot["max_window_ms"] != MAX_WINDOW_MS:
        _history_error("per-window limit changed")
    if not isinstance(snapshot.get("source_media"), str) or not snapshot["source_media"]:
        _history_error("source path is invalid")

    def rows(name: str, id_key: str, extra_keys: set[str]) -> list:
        values = snapshot.get(name)
        if not isinstance(values, list):
            _history_error("event collection is invalid")
        keys = {id_key, "provider", "model", "start_ms", "end_ms", "duration_ms"} | extra_keys
        for index, row in enumerate(values, 1):
            if not isinstance(row, dict) or set(row) != keys:
                _history_error("event fields are invalid")
            if type(row[id_key]) is not int or row[id_key] != index:
                _history_error("event sequence is invalid")
            if any(not isinstance(row[k], str) or not row[k].strip() for k in ("provider", "model")):
                _history_error("provider identity is invalid")
            if any(type(row[k]) is not int for k in ("start_ms", "end_ms", "duration_ms")):
                _history_error("event geometry is invalid")
            if not 0 <= row["start_ms"] < row["end_ms"]:
                _history_error("event geometry is invalid")
            if row["duration_ms"] != row["end_ms"] - row["start_ms"] or row["duration_ms"] > MAX_WINDOW_MS:
                _history_error("event duration is invalid")
        return values

    attempts = rows("attempts", "attempt_id", {"status", "reason_code", "http_status"})
    hits = rows("cache_hits", "cache_hit_id", set())
    refusals = rows("refusals", "refusal_id", {"reason_code"})
    windows: dict[tuple[int, int], int] = {}
    total = sealed = pending = failed = 0
    for row in attempts:
        status = row["status"]
        if not isinstance(status, str) or status not in _ATTEMPT_FINAL_STATUSES | {"DISPATCHED"}:
            _history_error("attempt outcome is invalid")
        reason = row["reason_code"]
        http_status = row["http_status"]
        if reason is not None and not isinstance(reason, str):
            _history_error("attempt reason is invalid")
        if http_status is not None and (type(http_status) is not int or not 100 <= http_status <= 599):
            _history_error("attempt HTTP status is invalid")
        if status == "DISPATCHED":
            if reason is not None or http_status is not None:
                _history_error("pending attempt has a terminal outcome")
            pending += 1
        else:
            sealed += 1
        failed += status in {"FAILED", "RESPONSE_REJECTED"}
        window = (row["start_ms"], row["end_ms"])
        windows[window] = windows.get(window, 0) + 1
        total += row["duration_ms"]
    for row in refusals:
        if not isinstance(row["reason_code"], str) or not row["reason_code"]:
            _history_error("refusal reason is invalid")
    expected_windows = [
        {"start_ms": a, "end_ms": b, "duration_ms": b - a, "attempt_count": count}
        for (a, b), count in sorted(windows.items())
    ]
    actual_windows = snapshot.get("windows")
    if not isinstance(actual_windows, list) or len(actual_windows) != len(expected_windows):
        _history_error("window counters are invalid")
    for actual, expected in zip(actual_windows, expected_windows):
        if (not isinstance(actual, dict) or set(actual) != set(expected)
                or any(type(value) is not int for value in actual.values()) or actual != expected):
            _history_error("window counters are invalid")
    counters = {
        "revision": len(attempts) + sealed + len(hits) + len(refusals),
        "distinct_window_count": len(windows),
        "total_audio_ms": total,
        "attempt_count": len(attempts),
        "sealed_attempt_count": sealed,
        "pending_attempt_count": pending,
        "failed_attempt_count": failed,
        "cache_hit_count": len(hits),
        "refusal_count": len(refusals),
        "remaining_windows": max(0, snapshot["max_windows"] - len(windows)),
        "remaining_audio_ms": max(0, snapshot["max_audio_ms"] - total),
    }
    if len(windows) > snapshot["max_windows"] or total > snapshot["max_audio_ms"]:
        _history_error("attempts exceed configured limits")
    for key, value in counters.items():
        if type(snapshot.get(key)) is not int or snapshot[key] != value:
            _history_error("derived counters do not match events")
    if set(snapshot) != set(counters) | set(limits) | {"source_media", "windows", "attempts", "cache_hits", "refusals"}:
        _history_error("snapshot fields are invalid")


def _require_budget_history_extension(existing: Mapping[str, object], payload: Mapping[str, object]) -> None:
    """Allow append and pending-to-terminal sealing, never erase old events."""
    old = existing.get("budget")
    new = payload.get("budget")
    _validate_budget_history(old)
    _validate_budget_history(new)
    for key in ("source_media", "max_windows", "max_window_ms", "max_audio_ms"):
        if old[key] != new[key]:
            _history_error("source or limits changed across receipts")
    if existing.get("providers") != _providers(old) or payload.get("providers") != _providers(new):
        _history_error("provider summary does not match events")
    if len(new["attempts"]) < len(old["attempts"]):
        _history_error("attempts were removed")
    outcome_keys = {"status", "reason_code", "http_status"}
    for before, after in zip(old["attempts"], new["attempts"]):
        if {k: v for k, v in before.items() if k not in outcome_keys} != {
            k: v for k, v in after.items() if k not in outcome_keys
        }:
            _history_error("prior attempt identity changed")
        if before["status"] != "DISPATCHED" and before != after:
            _history_error("sealed attempt outcome changed")
        # Both rows are structurally validated: a pending row can only remain
        # pending or transition once to an existing permitted terminal status.
    for name in ("cache_hits", "refusals"):
        if new[name][:len(old[name])] != old[name]:
            _history_error("prior cache-hit or refusal events changed")


def _write_atomic(path: Path, payload: Mapping[str, object]) -> None:
    temp: Path | None = None
    try:
        _no_links(path)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        _no_links(path.parent)
        descriptor, name = tempfile.mkstemp(
            prefix=".native-audio-budget-",
            suffix=".json",
            dir=path.parent,
        )
        temp = Path(name)
        # Atomic rename alone is not a durable pre-dispatch reservation. Flush
        # the complete payload first, then commit and sync the directory entry.
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fchmod(handle.fileno(), 0o600)
            os.fsync(handle.fileno())
        _no_links(path)
        os.replace(temp, path)
        temp = None
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except (OSError, TypeError, ValueError) as exc:
        raise BudgetReceiptPersistenceError(
            "native audio budget receipt could not be persisted"
        ) from exc
    finally:
        if temp is not None:
            temp.unlink(missing_ok=True)


def persist_native_audio_budget_receipt(
    *,
    source_media: Path,
    source_media_sha256: str,
    receipt_path: Path,
) -> dict[str, Any]:
    """Persist the newest source budget snapshot without revision rollback."""

    budget = get_budget(source_media)
    if budget is None:
        raise BudgetReceiptPersistenceError(
            "native audio budget is not registered"
        )
    resolved_source = source_media.resolve()
    resolved_receipt = receipt_path.absolute()
    with budget.lock, exclusive_native_audio_budget(resolved_receipt):
        snapshot = budget.snapshot()
        if snapshot.get("source_media") != str(resolved_source):
            raise BudgetReceiptPersistenceError(
                "native audio budget source binding is invalid"
            )
        revision = snapshot.get("revision")
        if isinstance(revision, bool) or not isinstance(revision, int):
            raise BudgetReceiptPersistenceError(
                "native audio budget revision is invalid"
            )
        _validate_budget_history(snapshot)
        payload: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "source_media_sha256": source_media_sha256,
            "revision": revision,
            "providers": _providers(snapshot),
            "budget": snapshot,
        }
        payload["receipt_sha256"] = receipt_sha256(payload)
        existing = _load_existing(resolved_receipt)
        if existing is not None:
            existing_revision = _validate_existing(
                existing,
                source_media_sha256=source_media_sha256,
            )
            if existing_revision > revision:
                raise BudgetReceiptPersistenceError(
                    "native audio budget receipt revision would regress"
                )
            if existing_revision == revision:
                if existing != payload:
                    raise BudgetReceiptPersistenceError(
                        "native audio budget receipt conflicts at one revision"
                    )
            # The monotonic revision check is necessary but not sufficient:
            # another process can have a larger revision and unrelated history.
            _require_budget_history_extension(existing, payload)
            if existing_revision == revision:
                return dict(existing)
        _write_atomic(resolved_receipt, payload)
        return payload


__all__ = [
    "BudgetReceiptPersistenceError",
    "SCHEMA_VERSION",
    "require_native_audio_budget_continuity",
    "exclusive_native_audio_budget",
    "persist_native_audio_budget_receipt",
    "receipt_sha256",
]
