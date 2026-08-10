"""The out-of-band lock guard must never be able to strand production.

2026-08-09: an ad-hoc `flock -x 9; while [ ! -e release ]; do sleep 3; done`
guard outlived its operator and held ``runner.lock`` for seven hours.  Every
cron tick was rejected by ``flock -n``, which prints nothing — the pipeline
died in total silence.  These tests pin the two kill-switches that make that
outcome impossible: a wall-clock TTL and parent-death detection.
"""

from __future__ import annotations

import fcntl
import os
import subprocess
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
GUARD = ROOT / "scripts" / "runner_lock_hold_guard.sh"


# util-linux flock(1) is absent on macOS.  This shim is faithful because
# `flock -x -n <fd>` locks an INHERITED descriptor: the lock belongs to the open
# file description shared with the calling shell, so it outlives the shim.
_FLOCK_SHIM = """#!/usr/bin/env python3
import fcntl
import sys

fd = int(sys.argv[-1])
try:
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
except OSError:
    sys.exit(1)
"""


def _flock_bin(tmp_path: Path) -> Path:
    shim = tmp_path / "flock-shim"
    shim.write_text(_FLOCK_SHIM, encoding="utf-8")
    shim.chmod(0o755)
    return shim


def _env(tmp_path: Path, **overrides: str) -> dict[str, str]:
    return {
        **os.environ,
        "AUTOSLICE_GUARD_FLOCK_BIN": str(_flock_bin(tmp_path)),
        "AUTOSLICE_GUARD_LOCK": str(tmp_path / "runner.lock"),
        "AUTOSLICE_GUARD_POLL_S": "0.1",
        "AUTOSLICE_GUARD_ALERT": str(tmp_path / "ALERT_RUNNER_LOCK_GUARD.txt"),
        **overrides,
    }


def _lock_is_free(path: Path) -> bool:
    with path.open("a+") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return False
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return True


def test_ttl_releases_the_lock_when_the_sentinel_never_arrives(tmp_path):
    """金丝雀①：哨兵文件永远不来 → TTL 到点必须自己放锁退出。"""

    completed = subprocess.run(
        ["bash", str(GUARD), "surgery-token", str(tmp_path / "never-touched")],
        check=False,
        capture_output=True,
        text=True,
        env=_env(tmp_path, AUTOSLICE_GUARD_TTL_S="1"),
        timeout=30,
    )

    assert completed.returncode == 3, completed.stdout + completed.stderr
    assert "GUARD_TTL_EXPIRED" in completed.stderr
    alert = (tmp_path / "ALERT_RUNNER_LOCK_GUARD.txt").read_text(encoding="utf-8")
    assert "GUARD_TTL_EXPIRED" in alert and "surgery-token" in alert
    assert _lock_is_free(tmp_path / "runner.lock")


def test_orphaned_guard_releases_the_lock_when_its_parent_dies(tmp_path):
    """金丝雀①b：父进程死了（Codex-F 实况）→ 立刻放锁，不等 TTL。"""

    parent = subprocess.Popen(["sleep", "30"])
    try:
        guard = subprocess.Popen(
            ["bash", str(GUARD), "orphan-token", str(tmp_path / "never-touched")],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=_env(
                tmp_path,
                AUTOSLICE_GUARD_TTL_S="600",
                AUTOSLICE_GUARD_PARENT_PID=str(parent.pid),
            ),
        )
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and _lock_is_free(tmp_path / "runner.lock"):
            time.sleep(0.05)
        assert not _lock_is_free(tmp_path / "runner.lock"), "guard never took the lock"

        parent.kill()
        parent.wait(timeout=10)
        _stdout, stderr = guard.communicate(timeout=20)
    finally:
        if parent.poll() is None:  # pragma: no cover - defensive cleanup
            parent.kill()

    assert guard.returncode == 4, stderr
    assert "GUARD_PARENT_GONE" in stderr
    assert _lock_is_free(tmp_path / "runner.lock")


def test_normal_short_surgery_is_untouched(tmp_path):
    """金丝雀③：几十秒的正常手术照常工作——哨兵一到就干净退出。"""

    release = tmp_path / "release"
    guard = subprocess.Popen(
        ["bash", str(GUARD), "normal-token", str(release)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=_env(tmp_path, AUTOSLICE_GUARD_TTL_S="600"),
    )
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and _lock_is_free(tmp_path / "runner.lock"):
        time.sleep(0.05)
    assert not _lock_is_free(tmp_path / "runner.lock"), "guard never took the lock"

    release.touch()
    stdout, stderr = guard.communicate(timeout=20)

    assert guard.returncode == 0, stderr
    assert "GUARD_UP" in stdout and "GUARD_RELEASED" in stdout
    assert not (tmp_path / "ALERT_RUNNER_LOCK_GUARD.txt").exists()
    assert _lock_is_free(tmp_path / "runner.lock")


def test_guard_refuses_a_lock_somebody_else_already_holds(tmp_path):
    """绝不抢别人的锁：忙锁=立刻显式失败，不是等、不是破。"""

    lock = tmp_path / "runner.lock"
    with lock.open("a+") as holder:
        fcntl.flock(holder.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        completed = subprocess.run(
            ["bash", str(GUARD), "loser-token", str(tmp_path / "release")],
            check=False,
            capture_output=True,
            text=True,
            env=_env(tmp_path, AUTOSLICE_GUARD_TTL_S="600"),
            timeout=30,
        )
        fcntl.flock(holder.fileno(), fcntl.LOCK_UN)

    assert completed.returncode == 1
    assert "already held" in completed.stderr
