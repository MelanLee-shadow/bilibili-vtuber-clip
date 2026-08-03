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
    assert "Restart=on-failure" in unit


def test_health_cron_uses_the_strict_mount_probe():
    cron = (
        ROOT / "ops" / "recording" / "bilive-record-health.cron"
    ).read_text(encoding="utf-8")

    probe = "mount_watchdog.sh --probe-only"
    audit = "record_health_audit.py --lookback-hours 96"
    assert probe in cron
    assert audit in cron
    assert cron.index(probe) < cron.index(audit)
