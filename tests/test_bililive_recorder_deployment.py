from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_recording_consumers_cannot_race_clouddrive_at_daemon_boot():
    compose = (
        ROOT / "ops" / "recording" / "docker-compose.bililive-recorder.yml"
    ).read_text(encoding="utf-8")

    assert compose.count('restart: "on-failure:5"') == 3
    assert "restart: unless-stopped" not in compose
    assert "restart: always" not in compose


def test_recorder_http_stays_private_and_authenticated():
    compose = (
        ROOT / "ops" / "recording" / "docker-compose.bililive-recorder.yml"
    ).read_text(encoding="utf-8")

    assert '"127.0.0.1:23566:2356"' in compose
    assert "BREC_HTTP_OPEN_ACCESS" not in compose


def test_systemd_boot_gate_tracks_docker_restart():
    unit = (
        ROOT / "ops" / "recording" / "bilive-recording-consumers.service"
    ).read_text(encoding="utf-8")

    assert "After=docker.service" in unit
    assert "Requires=docker.service" in unit
    assert "PartOf=docker.service" in unit
    assert "mount_watchdog.sh" in unit
    assert "EnvironmentFile=-/opt/bilive/autoslice/recording-health.env" not in unit
    assert "Restart=on-failure" in unit
    watchdog = (ROOT / "scripts" / "mount_watchdog.sh").read_text(encoding="utf-8")
    assert "WATCHDOG_ENV_FILE=/opt/bilive/autoslice/recording-health.env" in watchdog
    assert "WATCHDOG_ENV_OWNER_MODE" in watchdog
    assert '"0:600"' in watchdog


def test_health_cron_uses_the_strict_mount_probe():
    cron = (
        ROOT / "ops" / "recording" / "bilive-record-health.cron"
    ).read_text(encoding="utf-8")

    probe = "mount_watchdog.sh --probe-only"
    audit = "record_health_audit.py --lookback-hours 96"
    assert probe in cron
    assert audit in cron
    assert cron.index(probe) < cron.index(audit)
    assert "if [ -e /opt/bilive/autoslice/recording-health.env ]" in cron
    assert cron.startswith("# Refuse")
    assert "2-52/10 * * * * root" in cron


def test_oci3_recording_authority_and_cron_targets_are_present():
    env_path = ROOT / "ops" / "recording" / "recording-health.env"
    if env_path.is_file():
        env = env_path.read_text(encoding="utf-8")
        assert (
            "AUTOSLICE_WATCHDOG_PROBE_DIR=/path/to/cloud-drive/云盘/"
            "live-streaming-oci3test/22966160"
        ) in env
        assert (
            "AUTOSLICE_WATCHDOG_EXPECTED_RECORDING_BIND_ROOT=/path/to/cloud-drive/"
            "云盘/live-streaming-oci3test"
        ) in env
    assert not (ROOT / "ops" / "recording" / "bilive-record-watchdog.cron").exists()
    assert (ROOT / "ops" / "recording" / "bilive-record-health.cron").is_file()
    assert (ROOT / "ops" / "recording" / "record_health_audit.py").is_file()


def test_health_cron_log_parent_is_existing_autoslice_path():
    cron = (
        ROOT / "ops" / "recording" / "bilive-record-health.cron"
    ).read_text(encoding="utf-8")

    assert ">> /opt/bilive/autoslice/logs/record-health-cron.log 2>&1" in cron
    assert "/opt/bilive/logs/runtime/record-health-cron.log" not in cron
