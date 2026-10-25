from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_oci3_boot_and_health_paths_do_not_depend_on_cd2():
    unit = (ROOT / "ops/recording/bilive-recording-consumers.oci3.service").read_text()
    start = (ROOT / "ops/recording/start_local_recording.sh").read_text()
    cron = (ROOT / "ops/recording/bilive-record-health.oci3.cron").read_text()
    assert "start_local_recording.sh" in unit
    assert "mount_watchdog" not in unit + start + cron
    assert "clouddrive2" not in start
    assert "spool=$AUTOSLICE_RECORDING_BIND_ROOT" in start
    assert "--local-first" in cron
    assert "PartOf=docker.service" in unit


def test_single_mover_service_and_independent_existing_disk_guard():
    mover = (ROOT / "ops/recording/bilive-recording-spool-mover.service").read_text()
    guard = (ROOT / "ops/recording/oci3-disk-guard-recording-spool.conf").read_text()
    assert "ExecStart=/usr/bin/python3 -B /opt/bilive/ops/recording_spool_mover.py" in mover
    assert "--disk-guard-only" in guard
    assert "ExecStartPre=-" in guard
    assert "bilive-recording-consumers" not in mover  # no failure/stop coupling
    stub = (ROOT / "ops/recording/oci3_retired_mount_watchdog.sh").read_text()
    executable_lines = [line for line in stub.splitlines() if line and not line.startswith("#")]
    assert executable_lines[-1] == "exit 75"
    assert all(not line.startswith(("docker", "systemctl")) for line in executable_lines)
