"""Private Qixi transaction-core primitives shared by fixed closure lanes."""

from __future__ import annotations

import hashlib
import os
import stat
from dataclasses import dataclass
from pathlib import Path


class QixiTransactionCoreError(ValueError):
    """A fixed-lane transaction cannot establish exclusive ownership."""


@dataclass(frozen=True, slots=True)
class FileSnapshot:
    """One regular inode whose bytes and mode were observed atomically enough for CAS.

    This is deliberately data-model agnostic.  A lane still owns its journal
    schema, authority binding, and semantic after-image validator; the core
    only owns filesystem identity through prepare/install/rollback phases.
    """

    path: Path
    device: int
    inode: int
    sha256: str
    mode: int
    payload: bytes
    size: int = 0
    mtime_ns: int = 0
    ctime_ns: int = 0


def _safe_parent(path: Path) -> None:
    cursor = Path(path.anchor)
    for part in path.parts[1:-1]:
        cursor /= part
        try:
            mode = os.lstat(cursor).st_mode
        except OSError as exc:
            raise QixiTransactionCoreError("transaction target parent unavailable") from exc
        if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
            raise QixiTransactionCoreError("transaction target parent unsafe")


def safe_parent(path: Path) -> None:
    """Public spelling for the fixed-lane safe-parent assertion."""

    _safe_parent(path)


def _digest(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def stable_regular_snapshot(path: Path, *, label: str) -> FileSnapshot | None:
    """Read one non-symlink file and prove the pathname still names that inode.

    The descriptor is opened with ``O_NOFOLLOW`` and compared with both an
    lstat before and after its byte read.  This is the common primitive for
    journal preimages, staged files, installed targets, and rollback backups.
    """

    _safe_parent(path)
    try:
        before = os.lstat(path)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise QixiTransactionCoreError(f"{label} cannot be inspected") from exc
    if not stat.S_ISREG(before.st_mode) or stat.S_ISLNK(before.st_mode):
        raise QixiTransactionCoreError(f"{label} is not a regular file")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise QixiTransactionCoreError(f"{label} cannot be opened safely") from exc
    try:
        opened = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise QixiTransactionCoreError(f"{label} inode drifted before read")
        chunks: list[bytes] = []
        while chunk := os.read(descriptor, 1024 * 1024):
            chunks.append(chunk)
        payload = b"".join(chunks)
        after_fd = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    try:
        after_path = os.lstat(path)
    except OSError as exc:
        raise QixiTransactionCoreError(f"{label} disappeared during read") from exc
    identity = (before.st_dev, before.st_ino, stat.S_IMODE(before.st_mode), before.st_size, before.st_mtime_ns, before.st_ctime_ns)
    if (
        (after_fd.st_dev, after_fd.st_ino, stat.S_IMODE(after_fd.st_mode), after_fd.st_size, after_fd.st_mtime_ns, after_fd.st_ctime_ns) != identity
        or (after_path.st_dev, after_path.st_ino, stat.S_IMODE(after_path.st_mode), after_path.st_size, after_path.st_mtime_ns, after_path.st_ctime_ns) != identity
        or not stat.S_ISREG(after_path.st_mode)
        or stat.S_ISLNK(after_path.st_mode)
    ):
        raise QixiTransactionCoreError(f"{label} inode drifted during read")
    return FileSnapshot(
        path, before.st_dev, before.st_ino, _digest(payload), identity[2], payload,
        identity[3], identity[4], identity[5],
    )


def require_snapshot(snapshot: FileSnapshot, *, label: str) -> None:
    """Re-read and compare all ownership-relevant fields before a mutation."""

    current = stable_regular_snapshot(snapshot.path, label=label)
    exact_observation = bool(snapshot.size or snapshot.mtime_ns or snapshot.ctime_ns)
    if current is None or (current != snapshot if exact_observation else not _matches_journal_image(current, snapshot)):
        raise QixiTransactionCoreError(f"{label} ownership drifted")


def _matches_journal_image(current: FileSnapshot, expected: FileSnapshot) -> bool:
    """Compare a journal's immutable byte/inode contract, not observation times."""

    return (
        current.path == expected.path
        and current.device == expected.device
        and current.inode == expected.inode
        and current.sha256 == expected.sha256
        and current.mode == expected.mode
        and current.payload == expected.payload
    )


def create_staged_inode(path: Path, *, payload: bytes, mode: int, label: str) -> FileSnapshot:
    """Create one deterministic staged file without following or overwriting it."""

    _safe_parent(path)
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    except FileExistsError as exc:
        raise QixiTransactionCoreError(f"{label} already exists") from exc
    except OSError as exc:
        raise QixiTransactionCoreError(f"{label} cannot be created") from exc
    opened = os.fstat(descriptor)
    try:
        view = memoryview(payload)
        while view:
            view = view[os.write(descriptor, view) :]
        os.fchmod(descriptor, mode)
        os.fsync(descriptor)
        completed = os.fstat(descriptor)
    except BaseException:
        os.close(descriptor)
        _unlink_owned(path, device=opened.st_dev, inode=opened.st_ino, label=label)
        raise
    os.close(descriptor)
    snapshot = stable_regular_snapshot(path, label=label)
    if snapshot is None or (snapshot.device, snapshot.inode) != (completed.st_dev, completed.st_ino):
        raise QixiTransactionCoreError(f"{label} ownership drifted after creation")
    if snapshot.payload != payload or snapshot.mode != mode:
        raise QixiTransactionCoreError(f"{label} bytes drifted after creation")
    return snapshot


def install_staged_inode(
    staged: FileSnapshot,
    *,
    target: Path,
    expected_before: FileSnapshot | None,
    label: str,
) -> FileSnapshot:
    """CAS-install a lane-owned staged inode, including crash-after-rename replay."""

    _safe_parent(target)
    current = stable_regular_snapshot(target, label=label)
    if current is not None and (current.device, current.inode) == (staged.device, staged.inode):
        if current.payload != staged.payload or current.mode != staged.mode:
            raise QixiTransactionCoreError(f"{label} installed inode bytes drifted")
        return current
    if expected_before is None:
        if current is not None:
            raise QixiTransactionCoreError(f"{label} foreign target appeared")
    elif not _matches_journal_image(current, expected_before):
        raise QixiTransactionCoreError(f"{label} target preimage drifted")
    require_snapshot(staged, label=label)
    os.replace(staged.path, target)
    directory = os.open(target.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
    installed = stable_regular_snapshot(target, label=label)
    if installed is None or (installed.device, installed.inode) != (staged.device, staged.inode):
        raise QixiTransactionCoreError(f"{label} installed inode ownership was lost")
    if installed.payload != staged.payload or installed.mode != staged.mode:
        raise QixiTransactionCoreError(f"{label} installed bytes drifted")
    return installed


def _unlink_owned(path: Path, *, device: int, inode: int, label: str) -> None:
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return
    except OSError as exc:
        raise QixiTransactionCoreError(f"{label} cannot be inspected") from exc
    if (
        not stat.S_ISREG(info.st_mode)
        or stat.S_ISLNK(info.st_mode)
        or (info.st_dev, info.st_ino) != (device, inode)
    ):
        raise QixiTransactionCoreError(f"{label} ownership drifted")
    path.unlink()


def _runner_lock_open_flags() -> int:
    """Require kernel no-follow and close-on-exec for the global lease inode."""

    nofollow = getattr(os, "O_NOFOLLOW", None)
    cloexec = getattr(os, "O_CLOEXEC", None)
    if not isinstance(nofollow, int) or not isinstance(cloexec, int):
        raise QixiTransactionCoreError("runner lock safe open is unavailable")
    return os.O_RDWR | os.O_CREAT | nofollow | cloexec


def _same_safe_runner_lock(path: Path, descriptor: int, opened: os.stat_result) -> None:
    try:
        observed = os.lstat(path)
        live = os.fstat(descriptor)
    except OSError as exc:
        raise QixiTransactionCoreError("runner lock cannot be revalidated") from exc
    if (
        not stat.S_ISREG(opened.st_mode)
        or stat.S_ISLNK(observed.st_mode)
        or not stat.S_ISREG(observed.st_mode)
        or (observed.st_dev, observed.st_ino) != (opened.st_dev, opened.st_ino)
        or (live.st_dev, live.st_ino) != (opened.st_dev, opened.st_ino)
        or not stat.S_ISREG(live.st_mode)
    ):
        raise QixiTransactionCoreError("runner lock identity unsafe")


