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


def _proc_locks_key(path: Path) -> str:
    """Reproduce the kernel's ``%02x:%02x:%lu`` MAJ:MIN:INODE key for a file."""
    info = path.stat()
    dev_hex = format(info.st_dev, "x").rjust(4, "0")
    return f"{dev_hex[:-2]}:{dev_hex[-2:]}:{info.st_ino}"


def _run_watchdog(
    tmp_path: Path,
    *,
    mount_real: bool = False,
    permanently_non_fuse: str = "",
    fallback_file: bool = False,
    probe_only: bool = False,
    heartbeat_age_s: int | None = None,
    disabled: bool = False,
    lock_holder_cmd: str = "",
    stall_after_s: int = 3600,
) -> tuple[subprocess.CompletedProcess[str], str, Path]:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    base = tmp_path / "autoslice"
    (base / "reports").mkdir(parents=True)
    proc_root = tmp_path / "proc"
    proc_root.mkdir()
    proc_locks = proc_root / "locks"
    proc_locks.write_text("", encoding="utf-8")
    if heartbeat_age_s is not None:
        heartbeat = base / "reports" / "heartbeat.txt"
        heartbeat.write_text("live=True source=ok\n", encoding="utf-8")
        stamp = heartbeat.stat().st_mtime - heartbeat_age_s
        os.utime(heartbeat, (stamp, stamp))
    if disabled:
        (base / "DISABLED").touch()
    if lock_holder_cmd:
        lock = base / "runner.lock"
        lock.touch()
        holder = proc_root / "424242"
        holder.mkdir()
        (holder / "cmdline").write_bytes(
            b"\0".join(part.encode() for part in lock_holder_cmd.split(" ")) + b"\0"
        )
        proc_locks.write_text(
            "1: POSIX  ADVISORY  READ 11 00:01:99 0 EOF\n"
            f"2: FLOCK  ADVISORY  WRITE 424242 {_proc_locks_key(lock)} 0 EOF\n",
            encoding="utf-8",
        )
    _executable(
        fake_bin / "fake-stat",
        """#!/usr/bin/env python3
import os
import sys

args = sys.argv[1:]
if args[0] != "-c":
    sys.exit(2)
fmt, path = args[1], args[2]
info = os.stat(path)
print(
    fmt.replace("%Y", str(int(info.st_mtime)))
    .replace("%D", format(info.st_dev, "x"))
    .replace("%i", str(info.st_ino))
)
""",
    )
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
        "AUTOSLICE_WATCHDOG_BASE": str(base),
        "AUTOSLICE_WATCHDOG_STAT_BIN": str(fake_bin / "fake-stat"),
        "AUTOSLICE_WATCHDOG_PROC_LOCKS": str(proc_locks),
        "AUTOSLICE_WATCHDOG_PROC_ROOT": str(proc_root),
        "AUTOSLICE_WATCHDOG_STALL_AFTER_S": str(stall_after_s),
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
    command = ["bash", str(ROOT / "scripts" / "free_mount_watchdog.sh")]
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


def _stall_alert(tmp_path: Path) -> Path:
    return tmp_path / "autoslice" / "reports" / "ALERT_RUNNER_STALLED.txt"


def test_watchdog_alerts_on_starved_runner_and_names_the_lock_holder(tmp_path):
    """2026-08-09 根因面：runner.lock 被外部进程占住七小时，每个 cron tick 都被
    `flock -n` 静默弹回——没有日志、没有心跳、没有告警。心跳年龄是饥饿时唯一
    还能说话的信号，而 /proc/locks 能把凶手的 pid/cmdline 当场记下来。"""

    completed, _, _ = _run_watchdog(
        tmp_path,
        mount_real=True,
        heartbeat_age_s=7 * 3600,
        lock_holder_cmd="/usr/bin/python3 rogue_deploy_step.py",
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    alert = _stall_alert(tmp_path).read_text(encoding="utf-8")
    assert "runner STALLED" in alert
    assert "25200s old" in alert
    assert "pid=424242" in alert
    assert "rogue_deploy_step.py" in alert
    assert "NEEDS HUMAN" in alert


def test_watchdog_stall_alert_still_fires_when_no_holder_is_identifiable(tmp_path):
    completed, _, _ = _run_watchdog(
        tmp_path,
        mount_real=True,
        heartbeat_age_s=7200,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    alert = _stall_alert(tmp_path).read_text(encoding="utf-8")
    assert "runner STALLED" in alert
    assert "cron itself may be dead" in alert


def test_watchdog_stays_quiet_while_the_runner_is_deliberately_paused(tmp_path):
    """部署/人工停拍会立 DISABLED——那不是饥饿，报警会变成狼来了。"""

    completed, _, _ = _run_watchdog(
        tmp_path,
        mount_real=True,
        heartbeat_age_s=7 * 3600,
        disabled=True,
        lock_holder_cmd="/usr/bin/python3 rogue_deploy_step.py",
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert not _stall_alert(tmp_path).exists()


def test_watchdog_stays_quiet_on_a_fresh_heartbeat(tmp_path):
    completed, _, _ = _run_watchdog(
        tmp_path,
        mount_real=True,
        heartbeat_age_s=120,
        lock_holder_cmd="/usr/bin/python3 legitimate_tick.py",
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert not _stall_alert(tmp_path).exists()


def test_watchdog_probe_only_never_touches_the_stall_lane(tmp_path):
    """--probe-only 是 record-health cron 的轻探针；它不该产生告警副作用。"""

    completed, _, _ = _run_watchdog(
        tmp_path,
        mount_real=True,
        probe_only=True,
        heartbeat_age_s=7 * 3600,
    )

    assert completed.returncode == 0
    assert not _stall_alert(tmp_path).exists()


def test_watchdog_does_not_cry_wolf_over_the_runners_own_marathon_tick(tmp_path):
    """心跳只在 tick 结束时写，而健康的马拉松批次能跑两小时（8/9 实况
    09:40→11:42）。锁在 runner 自己手里 = 在干活，不是饥饿。"""

    completed, _, _ = _run_watchdog(
        tmp_path,
        mount_real=True,
        heartbeat_age_s=2 * 3600,
        lock_holder_cmd="/usr/bin/python3 scripts/free_session_autoslice.py --once",
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "working, not starved" in completed.stdout
    assert not _stall_alert(tmp_path).exists()
