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
    mount_real: bool = False,
    permanently_non_fuse: str = "",
    fallback_file: bool = False,
    probe_only: bool = False,
) -> tuple[subprocess.CompletedProcess[str], str, Path]:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    mount = tmp_path / "mount"
    probe_dir = mount / "live-streaming"
    probe_dir.mkdir(parents=True)
    mount_state = tmp_path / "mount-real"
    if mount_real:
        mount_state.touch()
    if fallback_file:
        fallback = probe_dir / "record_health" / "health.json"
        fallback.parent.mkdir()
        fallback.write_text('{"fallback": true}\n', encoding="utf-8")

    docker_log = tmp_path / "docker.log"
    container_state = tmp_path / "container-state"
    container_state.mkdir()
    quarantine_root = tmp_path / "quarantine"
    _executable(
        fake_bin / "timeout",
        "#!/bin/sh\nshift\nexec \"$@\"\n",
    )
    _executable(
        fake_bin / "findmnt",
        """#!/bin/sh
field=""
mode=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    -T|-M)
      mode="$1"
      shift
      ;;
    -o)
      field="$2"
      shift 2
      continue
      ;;
  esac
  shift
done
if [ "$mode" = "-M" ]; then
  if [ -f "$WATCHDOG_TEST_MOUNT_STATE" ]; then
    echo "$WATCHDOG_TEST_MOUNT fuse CloudFS"
    exit 0
  fi
  exit 1
fi
if [ -f "$WATCHDOG_TEST_MOUNT_STATE" ]; then
  case "$field" in
    TARGET) echo "$WATCHDOG_TEST_MOUNT" ;;
    FSTYPE) echo "fuse" ;;
    SOURCE) echo "CloudFS" ;;
    *) echo "$WATCHDOG_TEST_MOUNT fuse CloudFS" ;;
  esac
else
  case "$field" in
    TARGET) echo "/" ;;
    FSTYPE) echo "ext4" ;;
    SOURCE) echo "/dev/test" ;;
    *) echo "/ ext4 /dev/test" ;;
  esac
fi
""",
    )
    _executable(
        fake_bin / "fusermount",
        "#!/bin/sh\nrm -f \"$WATCHDOG_TEST_MOUNT_STATE\"\n",
    )
    _executable(
        fake_bin / "umount",
        "#!/bin/sh\nrm -f \"$WATCHDOG_TEST_MOUNT_STATE\"\n",
    )
    _executable(
        fake_bin / "docker",
        """#!/bin/sh
echo "$*" >> "$WATCHDOG_TEST_DOCKER_LOG"
if [ "$1 $2" = "restart clouddrive2" ]; then
  : > "$WATCHDOG_TEST_MOUNT_STATE"
  mkdir -p "$WATCHDOG_TEST_PROBE_DIR"
  exit 0
fi
if [ "$1" = "stop" ]; then
  rm -f "$WATCHDOG_TEST_CONTAINER_STATE/$2.started"
  exit 0
fi
if [ "$1" = "compose" ]; then
  case " $* " in
    *" up "*)
      for container in bililive_adapter bililive_recorder bilive_record; do
        : > "$WATCHDOG_TEST_CONTAINER_STATE/$container.started"
      done
      exit 0
      ;;
  esac
fi
if [ "$1" = "inspect" ]; then
  for arg in "$@"; do container="$arg"; done
  if [ -f "$WATCHDOG_TEST_CONTAINER_STATE/$container.started" ]; then
    echo true
    exit 0
  fi
  echo false
  exit 0
fi
if [ "$1" = "exec" ]; then
  container="$2"
  if [ ! -f "$WATCHDOG_TEST_CONTAINER_STATE/$container.started" ]; then
    exit 1
  fi
  if [ "$container" = "$WATCHDOG_TEST_PERMANENTLY_NON_FUSE" ]; then
    echo ext2/ext3
  else
    echo fuseblk
  fi
  exit 0
fi
exit 1
""",
    )
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ.get('PATH', '')}",
        "AUTOSLICE_WATCHDOG_MOUNT": str(mount),
        "AUTOSLICE_WATCHDOG_PROBE_DIR": str(probe_dir),
        "AUTOSLICE_WATCHDOG_BASE": str(tmp_path / "autoslice"),
        "AUTOSLICE_WATCHDOG_COOLDOWN_S": "0",
        "AUTOSLICE_WATCHDOG_MOUNT_RETRIES": "2",
        "AUTOSLICE_WATCHDOG_RECORDER_RETRIES": "2",
        "AUTOSLICE_WATCHDOG_RETRY_SLEEP_S": "0",
        "AUTOSLICE_WATCHDOG_RECORDER_SETTLE_S": "0",
        "AUTOSLICE_WATCHDOG_COMPOSE_FILE": str(tmp_path / "compose.yml"),
        "AUTOSLICE_WATCHDOG_QUARANTINE_ROOT": str(quarantine_root),
        "AUTOSLICE_WATCHDOG_DOCKER_BIN": str(fake_bin / "docker"),
        "AUTOSLICE_WATCHDOG_FINDMNT_BIN": str(fake_bin / "findmnt"),
        "AUTOSLICE_WATCHDOG_FUSERMOUNT_BIN": str(fake_bin / "fusermount"),
        "AUTOSLICE_WATCHDOG_TIMEOUT_BIN": str(fake_bin / "timeout"),
        "AUTOSLICE_WATCHDOG_UMOUNT_BIN": str(fake_bin / "umount"),
        "WATCHDOG_TEST_DOCKER_LOG": str(docker_log),
        "WATCHDOG_TEST_MOUNT": str(mount),
        "WATCHDOG_TEST_MOUNT_STATE": str(mount_state),
        "WATCHDOG_TEST_PROBE_DIR": str(probe_dir),
        "WATCHDOG_TEST_CONTAINER_STATE": str(container_state),
        "WATCHDOG_TEST_PERMANENTLY_NON_FUSE": permanently_non_fuse,
    }
    command = ["bash", str(ROOT / "scripts" / "mount_watchdog.sh")]
    if probe_only:
        command.append("--probe-only")
    completed = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        env=env,
        timeout=15,
    )
    calls = docker_log.read_text(encoding="utf-8") if docker_log.exists() else ""
    return completed, calls, quarantine_root


def test_probe_only_rejects_a_readable_plain_directory(tmp_path):
    completed, calls, _ = _run_watchdog(tmp_path, probe_only=True)

    assert completed.returncode == 1
    assert calls == ""


def test_watchdog_recovers_mount_then_recreates_consumers(tmp_path):
    completed, calls, _ = _run_watchdog(tmp_path)

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "real CloudFS mount probe FAILED" in completed.stdout
    assert "restart clouddrive2" in calls
    assert "compose -f" in calls
    assert "up -d --force-recreate" in calls
    assert "exec bililive_recorder stat -f -c %T /rec/Videos" in calls
    assert "exec bililive_adapter stat -f -c %T /adapter/Videos" in calls
    assert "exec bilive_record stat -f -c %T /app/Videos" in calls


def test_watchdog_preserves_system_disk_fallback_before_mount(tmp_path):
    completed, _, quarantine_root = _run_watchdog(
        tmp_path,
        fallback_file=True,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    preserved = list(quarantine_root.rglob("health.json"))
    assert len(preserved) == 1
    assert preserved[0].read_text(encoding="utf-8") == '{"fallback": true}\n'
    assert "preserved system-disk fallback entries" in completed.stdout


def test_watchdog_bootstraps_stopped_consumers_on_healthy_mount(tmp_path):
    completed, calls, _ = _run_watchdog(tmp_path, mount_real=True)

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "bootstrapping" in completed.stdout
    assert "restart clouddrive2" not in calls
    assert "up -d --force-recreate" in calls


@pytest.mark.parametrize(
    "failed_consumer",
    ["bililive_recorder", "bililive_adapter", "bilive_record"],
)
def test_watchdog_fails_closed_when_a_consumer_sees_non_fuse_storage(
    tmp_path,
    failed_consumer,
):
    completed, calls, _ = _run_watchdog(
        tmp_path,
        permanently_non_fuse=failed_consumer,
    )

    assert completed.returncode == 1
    assert "one or more consumers do not see a FUSE recording path" in completed.stdout
    assert f"exec {failed_consumer} stat -f -c %T" in calls
