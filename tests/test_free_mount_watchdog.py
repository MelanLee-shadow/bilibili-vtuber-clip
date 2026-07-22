from __future__ import annotations

import os
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _executable(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    path.chmod(0o755)
    return path


def test_watchdog_falls_back_to_explicit_start_after_restart_partial(tmp_path):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    probe_dir = tmp_path / "mount" / "live-streaming"
    docker_log = tmp_path / "docker.log"
    recorder_started = tmp_path / "recorder.started"
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
if [ "$1 $2" = "restart bilive_record" ]; then
  exit 1
fi
if [ "$1 $2" = "start bilive_record" ]; then
  : > "$WATCHDOG_TEST_RECORDER_STARTED"
  exit 0
fi
if [ "$1 $2" = "exec bilive_record" ] && [ -f "$WATCHDOG_TEST_RECORDER_STARTED" ]; then
  exit 0
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
        "WATCHDOG_TEST_RECORDER_STARTED": str(recorder_started),
    }

    completed = subprocess.run(
        ["bash", str(ROOT / "scripts" / "free_mount_watchdog.sh")],
        check=False,
        capture_output=True,
        text=True,
        env=env,
        timeout=15,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "entering bounded start fallback" in completed.stdout
    assert "recorder running and write path verified" in completed.stdout
    calls = docker_log.read_text(encoding="utf-8")
    assert "restart bilive_record" in calls
    assert "start bilive_record" in calls
    assert "exec bilive_record ls /app/Videos" in calls
