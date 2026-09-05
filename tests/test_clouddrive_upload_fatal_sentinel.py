from __future__ import annotations

from datetime import date
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

CLOUD_PATH = (
    "/live-streaming/123456/2026-07-25/123456_20260725-19-20-00.flv"
)
FATAL_LOG = (
    "\x1b[2m2026-07-25 14:48:36.308\x1b[0m \x1b[31mERROR\x1b[0m "
    "\x1b[2mcloudapi::transfers\x1b[0m\x1b[2m:\x1b[0m upload error for "
    f"{CLOUD_PATH}: UploadError(Fatal(\"upload_complete error: etag mismatch\"))"
)
HEALTHY_LOG = (
    "2026-07-25 14:00:00.000 INFO cloudapi::transfers: upload finished for "
    "/live-streaming/123456/2026-07-25/other.mp4"
)


def _executable(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    path.chmod(0o755)
    return path


def _run_sentinel(
    tmp_path: Path,
    *,
    docker_log: str,
    persisted_log: str | None = None,
    victim_bytes: bytes | None = b"recording-bytes",
    min_free_kb: int = 1,
) -> tuple[subprocess.CompletedProcess[str], Path, Path, Path]:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir(exist_ok=True)
    mount = tmp_path / "mount"
    rescue_root = tmp_path / "rescue"
    base = tmp_path / "base"
    alert = base / "reports" / "ALERT_UPLOAD_FATAL.txt"
    log_file = tmp_path / "docker-canned.log"
    log_file.write_text(docker_log + "\n", encoding="utf-8")
    persisted_log_dir = tmp_path / "persisted-logs"
    if persisted_log is not None:
        persisted_log_dir.mkdir()
        (persisted_log_dir / f"{date.today():%Y-%m-%d}.log").write_text(
            persisted_log + "\n", encoding="utf-8"
        )

    victim = mount / CLOUD_PATH.lstrip("/")
    if victim_bytes is not None:
        victim.parent.mkdir(parents=True, exist_ok=True)
        victim.write_bytes(victim_bytes)
    else:
        mount.mkdir(parents=True, exist_ok=True)

    _executable(
        fake_bin / "docker",
        '#!/bin/sh\ncat "$SENTINEL_TEST_DOCKER_LOG"\n',
    )
    _executable(
        fake_bin / "stat",
        (
            "#!/bin/sh\n"
            '# emulate GNU `stat -c %s <path>` portably\n'
            'python3 -c "import os,sys;print(os.path.getsize(sys.argv[1]))" "$3"\n'
        ),
    )
    _executable(
        fake_bin / "df",
        '#!/bin/sh\necho Avail\necho 99999999\n',
    )

    env = {
        **os.environ,
        "UPLOAD_SENTINEL_MOUNT": str(mount),
        "UPLOAD_SENTINEL_BASE": str(base),
        "UPLOAD_SENTINEL_RESCUE_ROOT": str(rescue_root),
        "UPLOAD_SENTINEL_ALERT": str(alert),
        "UPLOAD_SENTINEL_SEEN": str(base / "sentinel.seen"),
        "UPLOAD_SENTINEL_LOG_DIR": str(persisted_log_dir),
        "UPLOAD_SENTINEL_MIN_FREE_KB": str(min_free_kb),
        "UPLOAD_SENTINEL_DOCKER_BIN": str(fake_bin / "docker"),
        "UPLOAD_SENTINEL_STAT_BIN": str(fake_bin / "stat"),
        "UPLOAD_SENTINEL_DF_BIN": str(fake_bin / "df"),
        "SENTINEL_TEST_DOCKER_LOG": str(log_file),
    }
    completed = subprocess.run(
        ["bash", str(ROOT / "scripts" / "clouddrive_upload_fatal_sentinel.sh")],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )
    rescue_path = rescue_root / CLOUD_PATH.lstrip("/")
    return completed, alert, rescue_path, victim


def test_fatal_upload_rescues_bytes_out_of_the_cache_view(tmp_path):
    completed, alert, rescue_path, victim = _run_sentinel(
        tmp_path, docker_log=FATAL_LOG
    )

    assert completed.returncode == 0, completed.stderr
    assert rescue_path.is_file()
    assert rescue_path.read_bytes() == victim.read_bytes()
    body = alert.read_text(encoding="utf-8")
    assert "upload FATAL for " + CLOUD_PATH in body
    assert "rescued " + CLOUD_PATH in body
    assert "NEEDS HUMAN" in body


def test_persisted_log_only_rescues_bytes_out_of_the_cache_view(tmp_path):
    completed, alert, rescue_path, victim = _run_sentinel(
        tmp_path, docker_log="", persisted_log=FATAL_LOG
    )

    assert completed.returncode == 0, completed.stderr
    assert rescue_path.is_file()
    assert rescue_path.read_bytes() == victim.read_bytes()
    assert "rescued " + CLOUD_PATH in alert.read_text(encoding="utf-8")


def test_docker_and_persisted_log_duplicate_rescues_once(tmp_path):
    completed, alert, rescue_path, _ = _run_sentinel(
        tmp_path, docker_log=FATAL_LOG, persisted_log=FATAL_LOG
    )

    assert completed.returncode == 0, completed.stderr
    assert rescue_path.is_file()
    body = alert.read_text(encoding="utf-8")
    assert body.count("upload FATAL for " + CLOUD_PATH) == 1
    assert body.count("rescued " + CLOUD_PATH) == 1


def test_second_run_does_not_duplicate_rescue_or_alert(tmp_path):
    first, alert, rescue_path, _ = _run_sentinel(tmp_path, docker_log=FATAL_LOG)
    assert first.returncode == 0, first.stderr
    original = rescue_path.read_bytes()

    env_home = tmp_path  # same tree: seen-file persists between runs
    second = subprocess.run(
        ["bash", str(ROOT / "scripts" / "clouddrive_upload_fatal_sentinel.sh")],
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "UPLOAD_SENTINEL_MOUNT": str(env_home / "mount"),
            "UPLOAD_SENTINEL_BASE": str(env_home / "base"),
            "UPLOAD_SENTINEL_RESCUE_ROOT": str(env_home / "rescue"),
            "UPLOAD_SENTINEL_ALERT": str(alert),
            "UPLOAD_SENTINEL_SEEN": str(env_home / "base" / "sentinel.seen"),
            "UPLOAD_SENTINEL_MIN_FREE_KB": "1",
            "UPLOAD_SENTINEL_DOCKER_BIN": str(env_home / "bin" / "docker"),
            "UPLOAD_SENTINEL_STAT_BIN": str(env_home / "bin" / "stat"),
            "UPLOAD_SENTINEL_DF_BIN": str(env_home / "bin" / "df"),
            "SENTINEL_TEST_DOCKER_LOG": str(env_home / "docker-canned.log"),
        },
        timeout=60,
    )

    assert second.returncode == 0, second.stderr
    assert rescue_path.read_bytes() == original
    body = alert.read_text(encoding="utf-8")
    assert body.count("rescued " + CLOUD_PATH) == 1


def test_vanished_victim_alerts_without_crashing(tmp_path):
    completed, alert, rescue_path, _ = _run_sentinel(
        tmp_path, docker_log=FATAL_LOG, victim_bytes=None
    )

    assert completed.returncode == 0, completed.stderr
    assert not rescue_path.exists()
    body = alert.read_text(encoding="utf-8")
    assert "already vanished" in body


def test_free_disk_floor_refuses_rescue_copy(tmp_path):
    completed, alert, rescue_path, _ = _run_sentinel(
        tmp_path, docker_log=FATAL_LOG, min_free_kb=10**12
    )

    assert completed.returncode == 0, completed.stderr
    assert not rescue_path.exists()
    body = alert.read_text(encoding="utf-8")
    assert "rescue skipped" in body
    assert "NEEDS HUMAN" in body


def test_healthy_logs_produce_no_alert(tmp_path):
    completed, alert, rescue_path, _ = _run_sentinel(
        tmp_path, docker_log=HEALTHY_LOG
    )

    assert completed.returncode == 0, completed.stderr
    assert not alert.exists()
    assert not rescue_path.exists()


def test_missing_persisted_log_directory_is_compatible(tmp_path):
    completed, alert, rescue_path, _ = _run_sentinel(tmp_path, docker_log="")

    assert completed.returncode == 0, completed.stderr
    assert not alert.exists()
    assert not rescue_path.exists()


def test_default_log_directory_is_a_public_placeholder() -> None:
    source = (
        ROOT / "scripts" / "clouddrive_upload_fatal_sentinel.sh"
    ).read_text(encoding="utf-8")
    default_line = next(line for line in source.splitlines() if line.startswith("LOG_DIR="))

    assert default_line == 'LOG_DIR="${UPLOAD_SENTINEL_LOG_DIR:-/path/to/clouddrive2/config/log}"'


def test_path_traversal_is_ignored(tmp_path):
    unsafe_log = FATAL_LOG.replace(CLOUD_PATH, "/../../outside.flv")
    completed, alert, rescue_path, _ = _run_sentinel(
        tmp_path, docker_log=unsafe_log, victim_bytes=None
    )

    assert completed.returncode == 0, completed.stderr
    assert not alert.exists()
    assert not rescue_path.exists()
