"""Retry checkpoints for successful ordinary transcription observations.

These are fallible draft observations, never release approvals or human truth.
The caller reruns preparation, fidelity, strict parsers and downstream review. Legacy
SRT files without this binding are deliberately not imported into the cache.
"""

from __future__ import annotations

from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import stat
import time


def _no_links(path: Path) -> None:
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise OSError("unsafe checkpoint path")


def _directory(path: Path) -> None:
    _no_links(path)
    path.mkdir(mode=0o700, exist_ok=True)
    st = path.lstat()
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.geteuid() or st.st_mode & 0o022:
        raise OSError("unsafe checkpoint directory")


@contextmanager
def _locked(root: Path, stage: str, key: str):
    fd = None
    try:
        _directory(root)
        folder = root / stage
        _directory(folder)
        lock = folder / (key + ".lock")
        _no_links(lock)
        fd = os.open(lock, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        st = os.fstat(fd)
        if (
            not stat.S_ISREG(st.st_mode)
            or st.st_nlink != 1
            or st.st_uid != os.geteuid()
            or st.st_mode & 0o077
            or st.st_size != 0
        ):
            raise OSError("unsafe checkpoint lock")
        deadline = time.monotonic() + 600
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise OSError("checkpoint lock timeout")
                time.sleep(0.025)
        path = folder / (key + ".json")
    except OSError:
        if fd is not None:
            os.close(fd)
            fd = None
        path = None
    try:
        yield path
    finally:
        if fd is not None:
            os.close(fd)


