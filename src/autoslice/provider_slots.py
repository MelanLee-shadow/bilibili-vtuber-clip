"""Runtime-local, cross-process permits for provider-bearing work.

The pool is intentionally a filesystem flock pool rather than a thread
semaphore: runner workers, Qixi CLIs, and command-backed adapters are separate
processes.  It controls only provider dispatch, never deterministic auditing or
manifest replay.
"""

from __future__ import annotations

import fcntl
import os
import stat
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator


class ProviderSlotError(RuntimeError):
    """A provider permit could not be established safely."""


class ProviderSlotBusy(ProviderSlotError):
    """Compatibility name for callers that classified immediate saturation."""


class ProviderSlotTimeout(ProviderSlotBusy):
    """No provider permit became available before the bounded wait elapsed."""


@dataclass(frozen=True, slots=True)
class ProviderSlotLease:
    runtime_root: Path
    slot_index: int
    device: int
    inode: int


_thread_locks_guard = threading.Lock()
_thread_locks: dict[tuple[int, int, int], threading.Lock] = {}


def provider_capacity() -> int:
    value = os.environ.get("AUTOSLICE_PROVIDER_CONCURRENCY", "2")
    try:
        capacity = int(value)
    except ValueError as exc:
        raise ProviderSlotError("provider concurrency must be an integer from 1 to 5") from exc
    if not 1 <= capacity <= 5:
        raise ProviderSlotError("provider concurrency must be an integer from 1 to 5")
    return capacity


def provider_wait_seconds() -> float:
    """Return the configured bounded wait; never silently turn it into a retry loop."""

    value = os.environ.get("AUTOSLICE_PROVIDER_WAIT_SECONDS", "600")
    try:
        seconds = int(value)
    except ValueError as exc:
        raise ProviderSlotError("provider wait must be an integer from 1 to 900 seconds") from exc
    if not 1 <= seconds <= 900:
        raise ProviderSlotError("provider wait must be an integer from 1 to 900 seconds")
    return float(seconds)


def _safe_directory(path: Path) -> None:
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        try:
            path.mkdir(mode=0o700)
        except FileExistsError:
            pass
        except OSError as exc:
            raise ProviderSlotError("provider slot directory cannot be created") from exc
        info = os.lstat(path)
    except OSError as exc:
        raise ProviderSlotError("provider slot directory cannot be inspected") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o700:
        raise ProviderSlotError("provider slot directory is unsafe")


def _safe_runtime_root(path: Path) -> os.stat_result:
    if not path.is_absolute():
        raise ProviderSlotError("provider runtime root is unsafe")
    cursor = Path(path.anchor)
    info: os.stat_result | None = None
    for part in path.parts[1:]:
        cursor /= part
        try:
            info = os.lstat(cursor)
        except OSError as exc:
            raise ProviderSlotError("provider runtime root cannot be inspected") from exc
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise ProviderSlotError("provider runtime root is unsafe")
    if info is None:
        try:
            info = os.lstat(path)
        except OSError as exc:
            raise ProviderSlotError("provider runtime root cannot be inspected") from exc
    return info


def _slot_open_flags() -> int:
    """Require a no-follow, close-on-exec open rather than emulating it unsafely."""

    nofollow = getattr(os, "O_NOFOLLOW", None)
    cloexec = getattr(os, "O_CLOEXEC", None)
    if not isinstance(nofollow, int) or not isinstance(cloexec, int):
        raise ProviderSlotError("provider slot safe open is unavailable")
    return os.O_RDWR | os.O_CREAT | nofollow | cloexec


def _same_safe_slot(path: Path, descriptor: int, opened: os.stat_result) -> None:
    try:
        current = os.lstat(path)
        live = os.fstat(descriptor)
    except OSError as exc:
        raise ProviderSlotError("provider slot file cannot be revalidated") from exc
    if (
        not stat.S_ISREG(opened.st_mode)
        or stat.S_IMODE(opened.st_mode) != 0o600
        or stat.S_ISLNK(current.st_mode)
        or not stat.S_ISREG(current.st_mode)
        or stat.S_IMODE(current.st_mode) != 0o600
        or (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino)
        or (live.st_dev, live.st_ino) != (opened.st_dev, opened.st_ino)
        or not stat.S_ISREG(live.st_mode)
        or stat.S_IMODE(live.st_mode) != 0o600
    ):
        raise ProviderSlotError("provider slot file ownership drifted")


def _slot_fd(path: Path) -> tuple[int, os.stat_result]:
    try:
        descriptor = os.open(path, _slot_open_flags(), 0o600)
    except OSError as exc:
        raise ProviderSlotError("provider slot file cannot be opened safely") from exc
    try:
        opened = os.fstat(descriptor)
        _same_safe_slot(path, descriptor, opened)
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor, opened


def _thread_lock(runtime_identity: tuple[int, int], slot_index: int) -> threading.Lock:
    key = (*runtime_identity, slot_index)
    with _thread_locks_guard:
        return _thread_locks.setdefault(key, threading.Lock())


@contextmanager
def provider_slot(
    runtime_root: Path,
    *,
    timeout_seconds: float | None = None,
    poll_seconds: float = 0.05,
) -> Iterator[ProviderSlotLease]:
    """Wait a bounded interval for one runtime-local provider permit.

    Each retry opens and revalidates the slot inode. A process-held ``flock``
    handles inter-process contention; the per-slot thread mutex prevents a
    platform's process-scoped flock behavior from oversubscribing threads in
    one process.
    """

    if timeout_seconds is None:
        timeout_seconds = provider_wait_seconds()
    if not isinstance(timeout_seconds, (int, float)) or isinstance(timeout_seconds, bool) or timeout_seconds <= 0:
        raise ProviderSlotError("provider slot timeout must be positive")
    if not isinstance(poll_seconds, (int, float)) or isinstance(poll_seconds, bool) or not 0 < poll_seconds <= 1:
        raise ProviderSlotError("provider slot poll interval must be between 0 and 1 second")

    runtime_info = _safe_runtime_root(runtime_root)
    runtime_identity = (runtime_info.st_dev, runtime_info.st_ino)
    root = runtime_root / "provider-slots"
    _safe_directory(root)
    capacity = provider_capacity()
    deadline = time.monotonic() + float(timeout_seconds)
    while True:
        for index in range(capacity):
            thread_lock = _thread_lock(runtime_identity, index)
            if not thread_lock.acquire(blocking=False):
                continue
            descriptor: int | None = None
            try:
                live_runtime = _safe_runtime_root(runtime_root)
                if (live_runtime.st_dev, live_runtime.st_ino) != runtime_identity:
                    raise ProviderSlotError("provider runtime root ownership drifted")
                _safe_directory(root)
                descriptor, opened = _slot_fd(root / f"slot-{index}.lock")
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    continue
                # Re-stat only after the flock is owned: a replacement must
                # fail rather than split the capacity across two inodes.
                live_runtime = _safe_runtime_root(runtime_root)
                if (live_runtime.st_dev, live_runtime.st_ino) != runtime_identity:
                    raise ProviderSlotError("provider runtime root ownership drifted")
                _safe_directory(root)
                _same_safe_slot(root / f"slot-{index}.lock", descriptor, opened)
                try:
                    yield ProviderSlotLease(runtime_root, index, opened.st_dev, opened.st_ino)
                finally:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
                return
            finally:
                if descriptor is not None:
                    os.close(descriptor)
                thread_lock.release()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ProviderSlotTimeout("provider capacity wait timed out")
        time.sleep(min(float(poll_seconds), remaining))
