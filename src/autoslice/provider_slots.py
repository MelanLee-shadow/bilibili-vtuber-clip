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
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path


class ProviderSlotError(RuntimeError):
    """A provider permit could not be established safely."""


class ProviderSlotBusy(ProviderSlotError):
    """Every configured provider slot is currently occupied."""


@dataclass(frozen=True, slots=True)
class ProviderSlotLease:
    runtime_root: Path
    slot_index: int
    device: int
    inode: int


def provider_capacity() -> int:
    value = os.environ.get("AUTOSLICE_PROVIDER_CONCURRENCY", "2")
    try:
        capacity = int(value)
    except ValueError as exc:
        raise ProviderSlotError("provider concurrency must be an integer from 1 to 5") from exc
    if not 1 <= capacity <= 5:
        raise ProviderSlotError("provider concurrency must be an integer from 1 to 5")
    return capacity


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


def _safe_runtime_root(path: Path) -> None:
    try:
        info = os.lstat(path)
    except OSError as exc:
        raise ProviderSlotError("provider runtime root cannot be inspected") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise ProviderSlotError("provider runtime root is unsafe")


def _slot_fd(path: Path) -> tuple[int, os.stat_result]:
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        opened = os.fstat(descriptor)
        observed = os.lstat(path)
    except BaseException:
        os.close(descriptor)
        raise
    if (
        stat.S_ISLNK(observed.st_mode)
        or not stat.S_ISREG(observed.st_mode)
        or stat.S_IMODE(observed.st_mode) != 0o600
        or (observed.st_dev, observed.st_ino) != (opened.st_dev, opened.st_ino)
    ):
        os.close(descriptor)
        raise ProviderSlotError("provider slot file is unsafe")
    return descriptor, opened


@contextmanager
def provider_slot(runtime_root: Path):
    """Take one nonblocking provider permit for this runtime or fail typed."""

    root = runtime_root / "provider-slots"
    _safe_runtime_root(runtime_root)
    _safe_directory(root)
    capacity = provider_capacity()
    for index in range(capacity):
        descriptor, opened = _slot_fd(root / f"slot-{index}.lock")
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                continue
            try:
                yield ProviderSlotLease(runtime_root, index, opened.st_dev, opened.st_ino)
            finally:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            return
        finally:
            os.close(descriptor)
    raise ProviderSlotBusy("provider slots are busy")
