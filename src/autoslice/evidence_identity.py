"""Identity of durable evidence: content plus its validated source binding.

A byte-identical restore or copy of an evidence file necessarily gets a new
inode/device placement and ctime; its size and mtime are restorable file
attributes. Whether stored evidence "is still the same piece of evidence" is
therefore decided by its content sha256 (with size and mtime as the recorded,
restorable attributes) and the already validated source binding, never by
placement (device, inode, ctime, nlink, owner). Placement keeps its legitimate
uses: a stable read within one process, hard-link alias/independence checks
between two *current* files, create-only/ownership safety, locks and GC.
41 records the rule.
"""
from __future__ import annotations

import hashlib
import os
import stat
import threading
from pathlib import Path
from time import time_ns
from typing import NamedTuple


class _HashMemoEntry(NamedTuple):
    digest: str
    read_started_ns: int
    cached_at_ns: int


# Native-write probes : Mac <= 1.37 ms, OCI3 <= 6.07 ms
# between observed mtime updates. Round their empirical upper bound to 10 ms;
# nanosecond stat/utime storage does not imply nanosecond write-clock updates.
_RACY_WINDOW_NS = 10_000_000
_MEMO: dict[tuple, _HashMemoEntry] = {}
_MEMO_LOCK = threading.Lock()
_PLACEMENT_LOCKS: dict[tuple, threading.Lock] = {}


def _placement(info: os.stat_result) -> tuple[int, ...]:
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def regular_file_sha256(path: Path, *, fresh: bool = False) -> str | None:
    """Bare sha256 of a regular non-symlink file.

    Like Git's "racy git" rule (https://git-scm.com/docs/racy-git), equal stat
    fields alone do not establish clean content when a write can share the
    cache's timestamp tick. Each entry records wall-clock times before reading
    and at insertion. Reuse requires mtime AND ctime to precede both times by
    more than the safety window; ctime also covers restored/backdated mtime.
    A racy entry must be rehashed even after time passes, never simply aged
    into trust. The read-start check keeps a slow hash from doing that too.
    Cold files still reuse the memo; ``fresh`` unconditionally bypasses it.
    """
    try:
        target = Path(path)
        info = os.lstat(target)
        if not stat.S_ISREG(info.st_mode):
            return None
        key = (str(target.absolute()), *_placement(info))
        with _MEMO_LOCK:
            lock = _PLACEMENT_LOCKS.setdefault(key, threading.Lock())
        with lock:
            if _placement(os.lstat(target)) != _placement(info):
                return None
            cached = None if fresh else _MEMO.get(key)
            read_started_ns = time_ns()
            if (cached is not None
                    and cached.cached_at_ns <= read_started_ns
                    and max(info.st_mtime_ns, info.st_ctime_ns) + _RACY_WINDOW_NS
                    < min(cached.read_started_ns, cached.cached_at_ns)):
                from .validation_memo import note_hashed_file
                note_hashed_file(target, cached.digest)
                return cached.digest
            digest = hashlib.sha256()
            with target.open("rb") as handle:
                for block in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(block)
            if _placement(os.lstat(target)) != _placement(info):
                return None  # changed while reading: not one stable piece of evidence
            value = digest.hexdigest()
            _MEMO[key] = _HashMemoEntry(value, read_started_ns, time_ns())
            from .validation_memo import note_hashed_file
            note_hashed_file(target, value)
            return value
    except OSError:
        return None


class EvidenceFileChangedError(OSError):
    """The named file changed while resolving or hashing its contents."""


def file_sha256(path: Path, *, fresh: bool = False) -> str:
    """Raising adapter for ordinary file readers, including permitted symlinks.

    Callers retain their own path/ownership guards. Resolve only here so old
    helpers which allowed a symlink still hash its actual regular target;
    ``regular_file_sha256`` remains the sole digest/cache implementation.
    """
    source = Path(path)
    before = source.stat()
    target = source.resolve(strict=True)
    value = regular_file_sha256(target, fresh=True) if fresh else regular_file_sha256(target)
    if _placement(source.stat()) != _placement(before):
        raise EvidenceFileChangedError("evidence file changed during hashing")
    if value is None:
        raise OSError("evidence file is unreadable, non-regular or changed during hashing")
    from .validation_memo import note_hashed_file
    note_hashed_file(source, value)
    return value


