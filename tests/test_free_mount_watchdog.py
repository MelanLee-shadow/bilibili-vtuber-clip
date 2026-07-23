from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def _executable(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    path.chmod(0o755)
    return path


def _run_watchdog(
    tmp_path: Path,
    *,
    restart_partial: str = "",
    permanently_unreadable: str = "",
) -> tuple[subprocess.CompletedProcess[str], str]:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    probe_dir = tmp_path / "mount" / "live-streaming"
    docker_log = tmp_path / "docker.log"
    container_state = tmp_path / "container-state"
    container_state.mkdir()
    _executable(
        fake_bin / "timeout",
        "#!/bin/sh\nshift\nexec \"$@\"\n",
    )
    _executable(
        fake_bin / "docker",
        """#!/bin/sh
echo "$*" >> "$WATCHDOG_TEST_DOCKER_LOG"
if [ "$1 $2" = "restart clouddrive2" ]; then
  mkdir -p "$WATCHDOG_TEST_PROBE_DIR"
  exit 0
fi
if [ "$1" = "restart" ]; then
  if [ "$2" = "$WATCHDOG_TEST_RESTART_PARTIAL" ]; then
    exit 1
  fi
  : > "$WATCHDOG_TEST_CONTAINER_STATE/$2.started"
  exit 0
fi
if [ "$1" = "start" ]; then
  : > "$WATCHDOG_TEST_CONTAINER_STATE/$2.started"
  exit 0
fi
if [ "$1" = "exec" ]; then
  if [ "$2" = "$WATCHDOG_TEST_PERMANENTLY_UNREADABLE" ]; then
    exit 1
  fi
  if [ -f "$WATCHDOG_TEST_CONTAINER_STATE/$2.started" ]; then
    exit 0
  fi
fi
exit 1
""",
    )
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ.get('PATH', '')}",
        "AUTOSLICE_WATCHDOG_MOUNT": str(tmp_path / "mount"),
        "AUTOSLICE_WATCHDOG_PROBE_DIR": str(probe_dir),
        "AUTOSLICE_WATCHDOG_BASE": str(tmp_path / "autoslice"),
        "AUTOSLICE_WATCHDOG_COOLDOWN_S": "0",
        "AUTOSLICE_WATCHDOG_MOUNT_RETRIES": "2",
        "AUTOSLICE_WATCHDOG_RECORDER_RETRIES": "2",
        "AUTOSLICE_WATCHDOG_RETRY_SLEEP_S": "0",
        "AUTOSLICE_WATCHDOG_RECORDER_SETTLE_S": "0",
        "AUTOSLICE_WATCHDOG_DOCKER_BIN": str(fake_bin / "docker"),
        "AUTOSLICE_WATCHDOG_TIMEOUT_BIN": str(fake_bin / "timeout"),
        "WATCHDOG_TEST_DOCKER_LOG": str(docker_log),
        "WATCHDOG_TEST_PROBE_DIR": str(probe_dir),
        "WATCHDOG_TEST_CONTAINER_STATE": str(container_state),
        "WATCHDOG_TEST_RESTART_PARTIAL": restart_partial,
        "WATCHDOG_TEST_PERMANENTLY_UNREADABLE": permanently_unreadable,
    }

    completed = subprocess.run(
        ["bash", str(ROOT / "scripts" / "free_mount_watchdog.sh")],
        check=False,
        capture_output=True,
        text=True,
        env=env,
        timeout=15,
    )
    return completed, docker_log.read_text(encoding="utf-8")


def test_watchdog_falls_back_to_explicit_start_after_restart_partial(tmp_path):
    completed, calls = _run_watchdog(
        tmp_path,
        restart_partial="bilive_record",
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert (
        "bilive_record restart returned non-zero; entering bounded start fallback"
        in completed.stdout
    )
    assert (
        "repair COMPLETE: recorder, adapter and tooling all see the recovered write path"
        in completed.stdout
    )
    assert "restart bililive_recorder" in calls
    assert "restart bililive_adapter" in calls
    assert "restart bilive_record" in calls
    assert "start bilive_record" in calls
    assert "exec bililive_recorder ls /rec/Videos" in calls
    assert "exec bililive_adapter ls /adapter/Videos" in calls
    assert "exec bilive_record ls /app/Videos" in calls


@pytest.mark.parametrize(
    "failed_consumer",
    ["bililive_recorder", "bililive_adapter", "bilive_record"],
)
def test_watchdog_fails_closed_when_a_consumer_mount_stays_unreadable(
    tmp_path,
    failed_consumer,
):
    completed, calls = _run_watchdog(
        tmp_path,
        permanently_unreadable=failed_consumer,
    )

    assert completed.returncode == 1
    assert (
        "repair PARTIAL: host mount ok but one or more consumers lack a readable "
        "recording mount"
    ) in completed.stdout
    assert f"exec {failed_consumer} " in calls
