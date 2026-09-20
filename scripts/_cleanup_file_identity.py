"""Read and compare cleanup preimages inside the existing out-only namespace.

The caller must keep the existing cooperative maintenance/runner lock and obtain
all original cleanup approvals. This does not prove an artifact is unreferenced.
"""

from __future__ import annotations

from contextlib import contextmanager
import hashlib
import os
from pathlib import Path
import stat
from typing import Iterator

SCHEMA = "cleanup-file-preimage.v1"
META_KEYS = ("dev", "ino", "bytes", "mtime_ns", "ctime_ns", "mode", "uid", "gid", "nlink")


def _metadata(s: os.stat_result) -> dict[str, int]:
    return dict(
        zip(
            META_KEYS,
            (
                s.st_dev,
                s.st_ino,
                s.st_size,
                s.st_mtime_ns,
                s.st_ctime_ns,
                stat.S_IMODE(s.st_mode),
                s.st_uid,
                s.st_gid,
                s.st_nlink,
            ),
        )
    )


@contextmanager
def _open_target(base: str, path: str) -> Iterator[tuple[int, int, str]]:
    root = Path(os.path.abspath(base)) / "out"
    target = Path(path)
    if (
        not target.is_absolute()
        or ".." in target.parts
        or target == root
        or not target.is_relative_to(root)
    ):
        raise ValueError("cleanup target is outside the existing out namespace")
    # Traverse from / with directory FDs. Neither an ancestor nor the final file
    # may be a symlink, and a FIFO must not block the planner or delete worker.
    parent = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    fd = None
    try:
        for part in target.parent.parts[1:]:
            nxt = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
            os.close(parent)
            parent = nxt
        fd = os.open(target.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        s = os.fstat(fd)
        if not stat.S_ISREG(s.st_mode) or s.st_nlink != 1:
            raise ValueError("cleanup requires an independent regular file, not a shared inode")
        yield fd, parent, target.name
    finally:
        if fd is not None:
            os.close(fd)
        os.close(parent)


def _capture(fd: int, parent: int, name: str) -> dict[str, object]:
    before = os.fstat(fd)
    os.lseek(fd, 0, os.SEEK_SET)
    digest = hashlib.sha256()
    while chunk := os.read(fd, 1024 * 1024):
        digest.update(chunk)
    after = os.fstat(fd)
    current = os.stat(name, dir_fd=parent, follow_symlinks=False)
    if (
        not stat.S_ISREG(current.st_mode)
        or _metadata(before) != _metadata(after)
        or _metadata(after) != _metadata(current)
        or current.st_nlink != 1
    ):
        raise ValueError("cleanup file changed while its preimage was read")
    return {"schema_version": SCHEMA, "sha256": digest.hexdigest(), **_metadata(after)}


def capture_preimage(base: str, path: str) -> dict[str, object]:
    """Return a hash and exact file identity; never write or unlink the target."""
    with _open_target(base, path) as opened:
        return _capture(*opened)


def _validate_expected(expected: object) -> dict[str, object]:
    if not isinstance(expected, dict) or set(expected) != {"schema_version", "sha256", *META_KEYS}:
        raise ValueError("cleanup plan lacks a complete file preimage; rebuild the plan")
    if expected["schema_version"] != SCHEMA:
        raise ValueError("cleanup preimage schema is invalid")
    digest = expected["sha256"]
    if (
        not isinstance(digest, str)
        or len(digest) != 64
        or any(c not in "0123456789abcdef" for c in digest)
    ):
        raise ValueError("cleanup preimage SHA-256 is invalid")
    if any(type(expected[key]) is not int or expected[key] < 0 for key in META_KEYS):
        raise ValueError("cleanup preimage metadata is invalid")
    if expected["nlink"] != 1:
        raise ValueError("cleanup preimage names a shared inode")
    return expected


def verify_preimage(base: str, path: str, expected: object) -> None:
    expected = _validate_expected(expected)
    if capture_preimage(base, path) != expected:
        raise ValueError("cleanup file content or identity drifted since planning")


def remove_if_matches(base: str, path: str, expected: object, *, dry_run: bool) -> None:
    """Rehash at use time and unlink only the matching open-parent directory entry.

    The check and unlink are not an OS compare-and-unlink primitive. Cooperative
    writer exclusion by the existing runner lock is still required throughout.
    """
    expected = _validate_expected(expected)
    with _open_target(base, path) as (fd, parent, name):
        if _capture(fd, parent, name) != expected:
            raise ValueError("cleanup file content or identity drifted at application")
        if not dry_run:
            # Bind the pathname to the descriptor again immediately before use.
            if _metadata(os.stat(name, dir_fd=parent, follow_symlinks=False)) != _metadata(
                os.fstat(fd)
            ):
                raise ValueError("cleanup directory entry changed before unlink")
            os.unlink(name, dir_fd=parent)
