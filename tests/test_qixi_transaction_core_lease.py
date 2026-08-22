from __future__ import annotations

from pathlib import Path

import pytest

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
