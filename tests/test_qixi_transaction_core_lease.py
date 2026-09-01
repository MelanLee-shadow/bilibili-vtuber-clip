from __future__ import annotations

import os
from pathlib import Path

import pytest

from src.autoslice import qixi_transaction_core as core
from src.autoslice.qixi_transaction_core import (
    QixiTransactionCoreError,
    RunnerCommitLease,
    exclusive_runner_commit,
    exclusive_runner_lock,
    require_runner_commit_lease,
)


def test_runner_commit_lease_is_runtime_bound_and_non_reentrant(tmp_path: Path) -> None:
    other = tmp_path / "other"
    other.mkdir()
    with exclusive_runner_commit(tmp_path) as lease:
        require_runner_commit_lease(lease, runtime_root=tmp_path)
        with pytest.raises(QixiTransactionCoreError, match="invalid"):
            require_runner_commit_lease(lease, runtime_root=other)
        with pytest.raises(QixiTransactionCoreError, match="nesting"):
            with exclusive_runner_commit(tmp_path):
                pass
    with pytest.raises(QixiTransactionCoreError, match="invalid"):
        require_runner_commit_lease(lease, runtime_root=tmp_path)


def test_runner_commit_lease_cannot_be_constructed_and_legacy_wrapper_survives(tmp_path: Path) -> None:
    with pytest.raises(QixiTransactionCoreError, match="cannot be constructed"):
        RunnerCommitLease(
            runtime_root=tmp_path,
            lock_path=tmp_path / "runner.lock",
            device=1,
            inode=1,
            capability=object(),
        )
    with exclusive_runner_lock(tmp_path):
        assert (tmp_path / "runner.lock").is_file()


def test_runner_commit_lease_refuses_symlinked_lock_path(tmp_path: Path) -> None:
    foreign = tmp_path / "foreign.lock"
    foreign.write_bytes(b"")
    os.chmod(foreign, 0o600)
    (tmp_path / "runner.lock").symlink_to(foreign)
    with pytest.raises(QixiTransactionCoreError, match="opened safely"):
        with exclusive_runner_commit(tmp_path):
            pass


def test_runner_commit_lease_refuses_post_flock_path_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lock = tmp_path / "runner.lock"
    real_flock = core.fcntl.flock
    replaced = False

    def replace_after_lock(descriptor: int, operation: int) -> None:
        nonlocal replaced
        real_flock(descriptor, operation)
        if not replaced and operation & core.fcntl.LOCK_EX:
            replaced = True
            lock.unlink()
            lock.write_bytes(b"replacement")
            os.chmod(lock, 0o600)

    monkeypatch.setattr(core.fcntl, "flock", replace_after_lock)
    with pytest.raises(QixiTransactionCoreError, match="identity unsafe"):
        with exclusive_runner_commit(tmp_path):
            pass
