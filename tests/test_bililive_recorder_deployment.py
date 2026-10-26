from __future__ import annotations

from pathlib import Path
import json
from ops.recording.recording_topology import load_topology


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
        env = load_topology().shell()
        root = _oci3_recording_root()
        assert root == "/opt/bilive/recording-spool"
        assert f"AUTOSLICE_RECORDING_BIND_ROOT={root}\n" in env
        assert "AUTOSLICE_WATCHDOG" not in env
        assert "云盘" not in root
        assert (
            "AUTOSLICE_RECORD_HEALTH_STATE_PATH=/opt/bilive/recording/oci3-local-first/adapter-state.json"
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


OCI3_COMPOSE = ROOT / "ops" / "recording" / "docker-compose.oci3-bililive-recorder.yml"


def _oci3_compose() -> str:
    template = OCI3_COMPOSE.read_text(encoding="utf-8")
    rendered = load_topology().render_compose(template)
    lines = []
    for before, after in zip(template.splitlines(), rendered.splitlines()):
        if "${AUTOSLICE_RECORDING_" in before:
            prefix = after[:after.index("- ") + 2]
            after = prefix + json.loads(after.strip()[2:])
        lines.append(after)
    return "\n".join(lines) + "\n"


def _oci3_recording_root() -> str:
    import re

    sources = set(
        re.findall(
            r"^\s*-\s+(\S+):/(?:app|adapter|rec)/Videos:rshared\s*$",
            _oci3_compose(),
            re.MULTILINE,
        )
    )
    assert len(sources) == 1
    return sources.pop()


def test_oci3_compose_records_locally_with_fresh_ledger():
    """OCI3 compose: same services/images/gates as the Free locator, only the
    local spool, archive receipt argument and fresh adapter ledger differ."""
    import pytest

    if not OCI3_COMPOSE.is_file():
        pytest.skip("OCI3 deployment compose is private to the maintainer tree")
    free = (ROOT / "ops" / "recording" / "docker-compose.bililive-recorder.yml").read_text(
        encoding="utf-8"
    )
    oci3 = _oci3_compose()
    root = _oci3_recording_root()
    assert root == "/opt/bilive/recording-spool"
    n100_archive_root = "/path/to/cloud-drive/115open/李豆沙录播"
    assert "云盘" not in oci3
    assert f"{n100_archive_root}:" not in oci3  # never the N100 archive root
    for target in ("/app/Videos", "/adapter/Videos", "/rec/Videos"):
        assert f"      - {root}:{target}:rshared\n" in oci3
    assert "      - /opt/bilive/recording:/state:rw\n" in oci3
    for name in ("status.json", "adapter-state.json", "webhook-events.jsonl"):
        assert f"      - /state/oci3-local-first/{name}\n" in oci3
        assert f"      - /state/{name}\n" not in oci3
    normalized = oci3[oci3.index("services:"):]
    assert normalized.count("      - --local-first\n") == 1
    assert "      - --room\n      - " + load_topology().room + "\n" in normalized
    normalized = normalized.replace("      - --local-first\n", "")
    normalized = normalized.replace("      - --room\n      - " + load_topology().room + "\n", "")
    for target in ("/app/Videos", "/adapter/Videos", "/rec/Videos"):
        normalized = normalized.replace(
            f"{root}:{target}", f"/path/to/cloud-drive/live-streaming:{target}"
        )
    normalized = normalized.replace("/state/oci3-local-first/", "/state/")
    normalized = normalized.replace(
        "      - --archive-state-path\n      - /state/archive-state.json\n", ""
    )
    # Comments describe each host's different startup contract.
    normalized = "\n".join(line for line in normalized.splitlines() if not line.lstrip().startswith("#"))
    free = "\n".join(line for line in free.splitlines() if not line.lstrip().startswith("#"))
    assert normalized == free
