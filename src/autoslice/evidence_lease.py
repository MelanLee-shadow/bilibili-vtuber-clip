"""Structured, expiring protection for bound evidence and inputs.

The digest comes from the recorded input binding or its declaring document
(named in purpose). Readers never reopen media to hash it: a drifted or missing
input still needs its owner's attention, not GC.
"""

from __future__ import annotations

from datetime import datetime, timezone
import ctypes
import json
import os
from pathlib import Path
import re
import time
import sys
from typing import TypeAlias, TypedDict, cast


class Lease(TypedDict):
    schema: str
    path: str
    sha256: str
    holder: str
    purpose: str
    until_utc: str


Leases: TypeAlias = dict[str, Lease]

SCHEMA = "evidence-lease.v1"
FIELDS = frozenset({"schema", "path", "sha256", "holder", "purpose", "until_utc"})


def validate(lease: object) -> Lease:
    if not isinstance(lease, dict) or set(lease) != FIELDS or lease.get("schema") != SCHEMA:
        raise ValueError("unsupported evidence lease schema or fields")
    if any(not isinstance(lease[key], str) or not lease[key].strip() for key in FIELDS):
        raise ValueError("evidence lease fields must be nonempty strings")
    if not Path(lease["path"]).is_absolute():
        raise ValueError("evidence lease path must be absolute")
    if not re.fullmatch(r"[0-9a-f]{64}", lease["sha256"]):
        raise ValueError("evidence lease sha256 must be 64 lowercase hex digits")
    validated = cast(Lease, dict(lease))
    expires_at(validated)
    return validated


def expires_at(lease: Lease) -> float:
    stamp = datetime.fromisoformat(lease["until_utc"].replace("Z", "+00:00"))
    if stamp.tzinfo is None or stamp.utcoffset() != timezone.utc.utcoffset(stamp):
        raise ValueError("evidence lease until_utc must specify UTC")
    return stamp.timestamp()


def lease_dir(base: Path) -> Path:
    return base / "state" / "evidence-leases"


def read_leases(base: Path, *, now: float | None = None) -> Leases:
    """Read a validated active snapshot. Invalid records stop collection/apply."""
    now = time.time() if now is None else now
    directory = lease_dir(base)
    if directory.is_symlink():
        raise ValueError("evidence lease directory must not be a symlink")
    if not directory.exists():
        return {}
    if not directory.is_dir():
        raise ValueError("evidence lease directory must be a directory")
    leases: Leases = {}
    for path in sorted(path for path in directory.iterdir() if path.suffix == '.json'):
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"evidence lease must be a regular JSON file: {path}")
        lease = validate(json.loads(path.read_text(encoding="utf-8")))
        if expires_at(lease) > now:
            leases[path.stem] = lease
    return leases


class LeaseIndex:
    """Lexical ancestor lookup; GC never walks symlinks. No per-media stat/hash."""

    def __init__(self, leases: Leases) -> None:
        self.by_path: dict[Path, list[str]] = {}
        for key, lease in leases.items():
            self.by_path.setdefault(Path(os.path.abspath(lease['path'])), []).append(key)

    def covering(self, path: Path) -> list[str]:
        path = Path(os.path.abspath(path))
        return [key for ancestor in (path, *path.parents) for key in self.by_path.get(ancestor, ())]


class SnapshotGuard:
    """Revalidate before every unlink without rereading N leases N times.

    Watch both the directory and each inode (including hard-linked aliases).
    Changes, expiry or unavailable event coverage fall back to full reads.
    A watcher is local to one apply and is always closed by its caller.
    """

    def __init__(self, base: Path, expected: Leases) -> None:
        self.base, self.expected = base, expected
        self.fd: int | None = None
        self.directory = lease_dir(base)
        self.stamp: tuple[int, int] | None = None
        self.expiry = min((expires_at(row) for row in expected.values()), default=float('inf'))
        if sys.platform == 'linux' and self.directory.is_dir() and not self.directory.is_symlink():
            try:
                libc = ctypes.CDLL(None, use_errno=True)
                libc.inotify_init1.argtypes = [ctypes.c_int]
                libc.inotify_add_watch.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32]
                descriptor = int(libc.inotify_init1(os.O_NONBLOCK | os.O_CLOEXEC))
                if descriptor >= 0:
                    self.fd = descriptor
                    # MODIFY/ATTRIB/CLOSE_WRITE/MOVED_FROM/TO/CREATE/DELETE/
                    # DELETE_SELF/MOVE_SELF; overflow and IGNORED are automatic.
                    mask = 0xFCE
                    info = self.directory.lstat()
                    self.stamp = (info.st_dev, info.st_ino)
                    for path in (self.directory, *self.directory.glob('*.json')):
                        if libc.inotify_add_watch(descriptor, os.fsencode(path), mask) < 0:
                            self.close()
                            break
            except (AttributeError, OSError):
                self.close()
        try:
            if read_leases(base) != expected:
                raise RuntimeError('evidence leases changed before apply; review a new plan')
        except Exception:
            self.close()
            raise

    def unchanged(self) -> bool:
        if self.fd is not None:
            try:
                info = self.directory.lstat()
                if ((info.st_dev, info.st_ino) == self.stamp and time.time() < self.expiry):
                    try:
                        changed = bool(os.read(self.fd, 65536))
                    except BlockingIOError:
                        return True
                    if not changed:
                        self.close()
                else:
                    self.close()
            except OSError:
                self.close()
            # On any event, stop trusting this watcher (its directory or inode
            # may have been replaced). The ordinary validated reader is exact.
            self.close()
        return read_leases(self.base) == self.expected

    def close(self) -> None:
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None


