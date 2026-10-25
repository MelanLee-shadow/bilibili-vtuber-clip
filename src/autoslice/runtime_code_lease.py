"""Shared lease for readers of the mutable deployed repository.

Development worktrees do not use this lease. A deployment takes the exclusive
side only while replacing the repository. Candidate output/state locks are
independent. The wrapper uses only the standard library before acquiring the
lease and loads the target program afresh under it.
"""
from __future__ import annotations

import atexit
import fcntl
import os
from pathlib import Path
import re
import runpy
import stat
import sys

LOCK_NAME = '.oci3-runtime-code.lock'
LEASE_ENV = 'AUTOSLICE_RUNTIME_CODE_LEASE_FD'
VERSION_ENV = 'AUTOSLICE_RUNTIME_VERSION_ROOT'


def repository_runtime_root(repo: Path) -> Path:
    """Keep shared credentials/cache outside the immutable code versions."""
    repo = repo.resolve()
    return repo.parent.parent.parent if immutable_version(repo) else repo.parent


def immutable_version(repo: Path) -> bool:
    """Completed version directories are never overwritten by deployment."""
    repo = repo.resolve()
    version = repo.parent
    if (repo.name != 'repo' or version.parent.name != 'versions'
            or re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', version.name) is None):
        return False
    commit = (repo / 'DEPLOYED_COMMIT').read_text().strip()
    identity = commit if len(version.name) == 40 else (repo / 'OVERFLOW_RELEASE_SHA256').read_text().strip()
    if identity != version.name or not (repo / 'DEPLOYED_MANIFEST.json').is_file():
        raise RuntimeError('RUNTIME_VERSION_INCOMPLETE')
    return True


def pin_version(repo: Path) -> None:
    previous = os.environ.get(VERSION_ENV)
    if previous and Path(previous).resolve() != repo.resolve():
        raise RuntimeError('RUNTIME_VERSION_CHANGED_WITHIN_TASK')
    os.environ[VERSION_ENV] = str(repo.resolve())


def _open_lock(path: Path) -> int:
    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        os.close(fd)
        raise RuntimeError('RUNTIME_CODE_LEASE_UNSAFE')
    return fd


def _inherited_lease(path: Path) -> int | None:
    value = os.environ.get(LEASE_ENV, '')
    if not value.isdecimal():
        return None
    fd = int(value)
    try:
        actual, expected = os.fstat(fd), path.stat(follow_symlinks=False)
        if not stat.S_ISREG(expected.st_mode) or expected.st_nlink != 1:
            return None
        if (actual.st_dev, actual.st_ino) != (expected.st_dev, expected.st_ino):
            return None
        # Do not trust an environment marker without taking the actual lock.
        mode = fcntl.LOCK_EX if os.environ.get("AUTOSLICE_RUNTIME_CODE_LEASE_MODE") == "exclusive" else fcntl.LOCK_SH
        fcntl.flock(fd, mode)
        return fd
    except OSError:
        return None


def ensure_cli_code_lease(repo_root: Path, script: Path) -> None:
    """Re-enter a deployed CLI through the protected, fresh-code wrapper."""
    repo_root = repo_root.resolve()
    if not (repo_root / 'DEPLOYED_COMMIT').is_file():
        return
    if immutable_version(repo_root):
        pin_version(repo_root)
        return
    lock = repo_root.parent / LOCK_NAME
    if not lock.exists():
        raise RuntimeError('RUNTIME_CODE_LEASE_NOT_INSTALLED')
    if _inherited_lease(lock) is not None:
        return
    # The deployment also installs this stdlib-only bootstrap outside repo, so
    # it remains available while repo components are swapped or rolled back.
    wrapper = repo_root.parent / 'runtime-code-lease.py'
    if not wrapper.is_file() or wrapper.is_symlink():
        raise RuntimeError('RUNTIME_CODE_LEASE_BOOTSTRAP_MISSING')
    os.execv(sys.executable, [sys.executable, str(wrapper), str(repo_root),
                             str(script.absolute()), *sys.argv[1:]])



def ensure_import_code_lease(repo_root: Path) -> None:
    """Protect normal library consumers, including private drivers and -c."""
    repo_root = repo_root.resolve()
    if not (repo_root / 'DEPLOYED_COMMIT').is_file():
        return
    if immutable_version(repo_root):
        pin_version(repo_root)
        return
    lock = repo_root.parent / LOCK_NAME
    if _inherited_lease(lock) is not None:
        return
    main_file = getattr(sys.modules.get('__main__'), '__file__', None)
    if main_file and Path(main_file).absolute().is_relative_to(repo_root):
        ensure_cli_code_lease(repo_root, Path(main_file))
        return
    fd = _open_lock(lock)
    try:
        fcntl.flock(fd, fcntl.LOCK_SH)
        if any(repo_root.parent.glob('.oci3-shadow-autoslice-*')):
            raise RuntimeError('RUNTIME_CODE_RECOVERY_REQUIRED')
        os.environ[LEASE_ENV] = str(fd)
        os.set_inheritable(fd, True)
        atexit.register(os.close, fd)
    except BaseException:
        os.close(fd)
        raise


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) < 2:
        raise RuntimeError('RUNTIME_CODE_LEASE_ARGUMENTS_INVALID')
    logical = Path(args[0]).absolute()
    repo = logical.resolve()
    script = Path(args[1]).absolute()
    try:
        relative = script.relative_to(logical)
    except ValueError:
        try:
            relative = script.relative_to(repo)
        except ValueError as exc:
            raise RuntimeError('RUNTIME_CODE_LEASE_SCRIPT_OUTSIDE_REPOSITORY') from exc
    # Resolve the repository once. A concurrent current-link switch must not
    # resolve the script against a different version after selecting the repo.
    pinned = os.environ.get(VERSION_ENV)
    if pinned:
        prior = Path(pinned).resolve()
        if immutable_version(prior) and prior.parent.parent == repo.parent.parent:
            repo = prior
    script = repo / relative
    if not script.resolve().is_relative_to(repo):
        raise RuntimeError('RUNTIME_CODE_LEASE_SCRIPT_OUTSIDE_REPOSITORY')
    if immutable_version(repo):
        pin_version(repo)
        sys.argv = [str(script), *args[2:]]
        sys.path.insert(0, str(script.parent))
        runpy.run_path(str(script), run_name='__main__')
        return 0
    fd = _open_lock(repo.parent / LOCK_NAME)
    try:
        fcntl.flock(fd, fcntl.LOCK_SH)
        # A killed deployment releases its kernel lock, but its incomplete
        # transaction must still prevent mixed-code execution after reboot.
        if any(repo.parent.glob('.oci3-shadow-autoslice-*')):
            print('RUNTIME_CODE_RECOVERY_REQUIRED: resume the saved deployment transaction', file=sys.stderr)
            return 75
        if not (repo / 'DEPLOYED_COMMIT').is_file():
            raise RuntimeError('RUNTIME_CODE_DEPLOYED_IDENTITY_MISSING')
        os.environ[LEASE_ENV] = str(fd)
        os.set_inheritable(fd, True)
        sys.argv = [str(script), *args[2:]]
        sys.path.insert(0, str(script.parent))
        runpy.run_path(str(script), run_name='__main__')
        return 0
    finally:
        os.close(fd)


if __name__ == '__main__':
    raise SystemExit(main())
