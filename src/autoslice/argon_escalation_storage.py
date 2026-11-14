"""Private immutable artifacts and cooperative execution lock for Argon jobs."""

from __future__ import annotations

import os
from pathlib import Path
import stat
import tempfile

from .argon_escalation_contract import require, sha, stat_identity, valid_sha

MAX_ARTIFACT_BYTES = 32_000_000


def _private_directory(path: Path) -> None:
    info = path.lstat()
    require(
        stat.S_ISDIR(info.st_mode)
        and info.st_uid == os.geteuid()
        and not stat.S_IMODE(info.st_mode) & 0o077,
        "STORE_DIRECTORY_NOT_PRIVATE",
    )


def directory(path: Path) -> None:
    # A caller may place a new private store under an existing container. Do
    # not chmod that container, and never follow a parent/terminal symlink.
    path = Path(path).absolute()
    require(not any(p.is_symlink() for p in (path, *path.parents)), "STORE_PATH_UNSAFE")
    missing = []
    current = path
    while not os.path.lexists(current):
        missing.append(current)
        current = current.parent
    # mkdir(parents=True, mode=0700) applies mode only to the deepest path.
    # Create each missing component explicitly; no global umask mutation and
    # no permission repair of an existing (possibly raced-in) directory.
    for component in reversed(missing):
        require(not component.parent.is_symlink(), "STORE_PATH_UNSAFE")
        try:
            component.mkdir(mode=0o700)
        except FileExistsError:
            pass
        _private_directory(component)
    require(not any(p.is_symlink() for p in (path, *path.parents)), "STORE_PATH_UNSAFE")
    _private_directory(path)


def read(path: Path) -> bytes:
    raw = _read(path)
    from .validation_memo import note_hashed_file, capturing_validation
    if capturing_validation():
        note_hashed_file(path, sha(raw))
    return raw


def _read(path: Path, *, allow_replaced: bool = False) -> bytes:
    """Read one complete private file from an opened inode.

    Ordinary immutable artifacts keep the path identity check: a replacement
    while the descriptor is open is a concurrent mutation and must be
    rejected.  Content-addressed blobs are different.  Their pathname is the
    digest, so replacing that pathname with another complete inode carrying
    the same digest is a safe publication race; the descriptor and the final
    hash check still bind the bytes the reader consumed.
    """
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        require(
            stat.S_ISREG(info.st_mode)
            and info.st_nlink in ((0, 1) if allow_replaced else (1,))
            and info.st_uid == os.geteuid()
            and not stat.S_IMODE(info.st_mode) & 0o077
            and info.st_size <= MAX_ARTIFACT_BYTES,
            "ARTIFACT_UNSAFE",
        )
        with os.fdopen(fd, "rb", closefd=False) as handle:
            raw = handle.read(MAX_ARTIFACT_BYTES + 1)
        after = os.fstat(fd)
        same_inode = stat_identity(info) == stat_identity(after)
        # Removing the old pathname during os.replace changes that inode's
        # link count and ctime even though its bytes remain immutable.  Keep
        # the device/inode/size/mtime checks for that cold-reader case.
        replaced_inode_stable = (
            allow_replaced
            and after.st_nlink == 0
            and (
                info.st_dev,
                info.st_ino,
                info.st_size,
                info.st_mtime_ns,
                info.st_uid,
                stat.S_IMODE(info.st_mode),
            )
            == (
                after.st_dev,
                after.st_ino,
                after.st_size,
                after.st_mtime_ns,
                after.st_uid,
                stat.S_IMODE(after.st_mode),
            )
        )
        require(
            len(raw) == info.st_size
            and (same_inode or replaced_inode_stable)
            and (
                allow_replaced
                or path.stat(follow_symlinks=False).st_ino == info.st_ino
            ),
            "ARTIFACT_CHANGED_DURING_READ",
        )
        return raw
    finally:
        os.close(fd)


def _sync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_write(path: Path, raw: bytes, *, create_only: bool = False) -> bool:
    """Shared complete-file publisher; False means create-only lost a race.

    Domain owners retain validation, permissions and conflict policy.
    Publication itself never needs a reader or writer lock.
    """
    path = Path(path)
    require(not any(p.is_symlink() for p in (path, *path.parents)), "STORE_PATH_UNSAFE")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(prefix=".argon-", dir=path.parent)
    temp = Path(name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        if create_only:
            try:
                os.link(temp, path)
            except FileExistsError:
                return False
            temp.unlink()
        else:
            os.replace(temp, path)
        _sync_directory(path.parent)
        return True
    finally:
        temp.unlink(missing_ok=True)


def immutable(path: Path, raw: bytes) -> None:
    directory(path.parent)
    if os.path.lexists(path):
        require(read(path) == raw, "IMMUTABLE_ARTIFACT_CONFLICT")
        return
    require(len(raw) <= MAX_ARTIFACT_BYTES, "ARTIFACT_TOO_LARGE")
    if not atomic_write(path, raw, create_only=True):
        require(read(path) == raw, "IMMUTABLE_ARTIFACT_CONFLICT")


def blob(root: Path, raw: bytes) -> str:
    digest = sha(raw)
    # A blob name is its complete content identity.  Writers may therefore
    # publish a freshly fsynced inode with replace semantics; readers validate
    # the digest after reading and never observe a partial file.
    require(len(raw) <= MAX_ARTIFACT_BYTES, "ARTIFACT_TOO_LARGE")
    blobs = Path(root) / "blobs"
    directory(blobs)
    atomic_write(blobs / f"{digest}.bin", raw)
    return digest


def get_blob(root: Path, digest: str) -> bytes:
    require(valid_sha(digest), "ARTIFACT_SHA_INVALID")
    raw = _read(root / "blobs" / f"{digest}.bin", allow_replaced=True)
    require(sha(raw) == digest, "ARTIFACT_SHA_MISMATCH")
    from .validation_memo import note_hashed_file
    note_hashed_file(root / "blobs" / f"{digest}.bin", digest)
    return raw


