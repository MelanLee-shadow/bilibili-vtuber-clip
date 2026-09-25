"""Offline contracts for the Mac-side Li Dousha slice monitor."""
from __future__ import annotations

import importlib.util
import itertools
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/slice_monitor.py"
_COUNTER = itertools.count()
_ENV_KEYS = (
    "AUTOSLICE_MONITOR_PROFILE",
    "AUTOSLICE_MONITOR_SSH_HOST",
    "AUTOSLICE_MONITOR_CONTAINER",
    "AUTOSLICE_MONITOR_ROOM",
    "AUTOSLICE_MONITOR_ALLOW_RECORDER_RESTART",
    "AUTOSLICE_MONITOR_ALLOW_UPLOAD_KILL",
)


def _load(monkeypatch: pytest.MonkeyPatch, **env: str):
    for key in _ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    name = f"slice_monitor_test_{next(_COUNTER)}"
    spec = importlib.util.spec_from_file_location(name, SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _healthy_probe(module, *, upload: int = 0, rec_total: int = 200) -> dict[str, object]:
    return {
        "ok": True,
        "now": 2_000.0,
        "procs": {
            "recorder": 1,
            "scan": 0,
            "local_prepare": 0,
            "upload": upload,
            "auto_review_shadow": 0,
        },
        "rooms": {
            module.PRIMARY_ROOM: {
                "latest_date_dir": "2026-09-25",
                "recording": {"active": True, "age_sec": 2, "mtime": 1_998.0},
                "slices": {
                    "age_sec": 60,
                    "count_latest_dir": 0,
                    "cover_count_latest_dir": 0,
                    "publish_json_latest_dir": 0,
                },
                "dirty_backlog": [],
                "recorder_status": {
                    "live_status": 1,
                    "finalizing": False,
                    "running_status": 1,
                    "total_output_bytes": rec_total,
                    "real_stream_format": "flv",
                    "requested_quality_number": 10000,
                    "active_media": {"width": 1920, "height": 1080},
                    "cookie_login_valid": True,
                },
            }
        },
        "flags": {"delete_source": "never"},
        "disk": {"free": 100 * 1024**3},
        "audio": {},
        "jingting": {"ok": True, "skipped": True, "reason": "production profile"},
        "auto_review_shadow": {},
        "record_health": {
            "schema_version": "record-health-audit.v1",
            "status": "PASS",
            "healthy": True,
            "service_reachable": True,
            "status_age_seconds": 2,
            "adapter_state_age_seconds": 2,
            "unresolved": [],
        },
        "autoslice": {
            "heartbeat": "2026-09-25T08:55:00+0000 2026-09-25:review_ready",
            "age_sec": 10,
            "alerts": [],
        },
    }


def test_production_defaults_use_oci3_and_pinned_host_key_check(monkeypatch):
    monitor = _load(monkeypatch)
    expected_host = "localhost" if SCRIPT.name == "slice_monitor.py" else "oci3"
    assert monitor.MONITOR_PROFILE == "production"
    assert monitor._DEFAULT_SSH_HOST == expected_host
    assert monitor.SSH_HOST == expected_host
    assert monitor.ALLOW_RECORDER_RESTART is False
    assert monitor.ALLOW_UPLOAD_KILL is True
    argv = monitor._ssh_argv("true")
    assert argv[:2] == ["ssh", "-o"]
    assert "BatchMode=yes" in argv
    assert "StrictHostKeyChecking=yes" in argv
    assert argv[-2:] == [expected_host, "true"]


def test_legacy_profile_requires_an_explicit_host(monkeypatch):
    with pytest.raises(RuntimeError, match="legacy monitor profile requires"):
        _load(monkeypatch, AUTOSLICE_MONITOR_PROFILE="legacy")
    monitor = _load(
        monkeypatch,
        AUTOSLICE_MONITOR_PROFILE="legacy",
        AUTOSLICE_MONITOR_SSH_HOST="historical-host",
    )
    assert monitor.SSH_HOST == "historical-host"


def test_production_skips_legacy_jingting_without_subprocess(monkeypatch):
    monitor = _load(monkeypatch)
    monkeypatch.setattr(
        monitor.subprocess,
        "run",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("subprocess called")),
    )
    result = monitor.run_jingting_probe()
    assert result["ok"] is True
    assert result["skipped"] is True


def test_production_probe_uses_host_side_python_not_legacy_container(monkeypatch):
    monitor = _load(monkeypatch)
    payload = _healthy_probe(monitor)
    calls = []

    class Completed:
        returncode = 0
        stdout = "noise\n" + __import__("json").dumps(payload)
        stderr = ""

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        return Completed()

    monkeypatch.setattr(monitor.subprocess, "run", fake_run)
    result = monitor.run_production_probe()
    assert result["ok"] is True
    argv, kwargs = calls[0]
    assert argv[-3:] == [monitor.SSH_HOST, "/usr/bin/python3", "-"]
    assert "/opt/bilive/autoslice/repo/ops/recording/record_health_audit.py" in kwargs["input"]
    assert '["/usr/bin/sudo", "-n", "/usr/bin/python3", AUDIT_PATH' in kwargs["input"]
    assert "/opt/bilive/ops/record_health_audit.py" not in kwargs["input"]
    assert "docker exec" not in kwargs["input"]


def test_legacy_container_probe_is_refused_in_production(monkeypatch):
    monitor = _load(monkeypatch)
    with pytest.raises(RuntimeError, match="outside legacy profile"):
        monitor.run_probe()


def test_failed_normalized_record_health_is_reported(monkeypatch):
    monitor = _load(monkeypatch)
    probe = _healthy_probe(monitor)
    probe["procs"]["recorder"] = 0
    probe["record_health"] = {
        "schema_version": "record-health-audit.v1",
        "status": "FAIL",
        "healthy": False,
        "service_reachable": False,
        "status_age_seconds": 600,
        "adapter_state_age_seconds": 600,
        "unresolved": ["status stale: 600s", "recorder service is unreachable"],
    }
    verdict, problems, _actions, notes = monitor.evaluate(probe, {})
    assert verdict == "DOWN"
    problem = next(row for row in problems if row["id"] == "record_health_audit")
    assert problem["sev"] == "DOWN"
    assert "status stale: 600s" in problem["detail"]
    assert any("record-health status=FAIL" in note for note in notes)


def test_probe_failure_names_the_configured_host(monkeypatch):
    monitor = _load(monkeypatch, AUTOSLICE_MONITOR_SSH_HOST="current-authority")
    verdict, problems, actions, notes = monitor.evaluate(
        {"ok": False, "fatal": "connection failed"}, {}
    )
    assert verdict == "DOWN"
    assert actions == [] and notes == []
    assert "current-authority" in problems[0]["msg"]
    assert "ssh current-authority" in problems[0]["fix"]
    assert "ssh recording-host" not in str(problems[0])


def test_no_growth_does_not_restart_recorder_without_explicit_opt_in(monkeypatch):
    monitor = _load(monkeypatch)
    monkeypatch.setattr(
        monitor,
        "restart_recorder",
        lambda _room: (_ for _ in ()).throw(AssertionError("restart attempted")),
    )
    probe = _healthy_probe(monitor, rec_total=200)
    state = {"rec_total_seen": {monitor.PRIMARY_ROOM: 200}}
    verdict, problems, actions, notes = monitor.evaluate(probe, state)
    assert verdict == "DOWN"
    assert not any(kind == "restarted_recorder" for kind, _ in actions)
    problem = next(row for row in problems if row["id"] == "live_not_recording")
    assert "默认不重启" in problem["msg"]
    assert any("recorder_restart=disabled" in note for note in notes)


def test_no_growth_restarts_only_with_explicit_opt_in(monkeypatch):
    monitor = _load(monkeypatch, AUTOSLICE_MONITOR_ALLOW_RECORDER_RESTART="1")
    calls = []
    monkeypatch.setattr(
        monitor, "restart_recorder", lambda room: calls.append(room) or True
    )
    probe = _healthy_probe(monitor, rec_total=200)
    state = {"rec_total_seen": {monitor.PRIMARY_ROOM: 200}}
    verdict, _problems, actions, notes = monitor.evaluate(probe, state)
    assert verdict == "DOWN"
    assert calls == [monitor.PRIMARY_ROOM]
    assert any(kind == "restarted_recorder" for kind, _ in actions)
    assert any("recorder_restart=enabled" in note for note in notes)


def test_upload_safety_stop_is_independent(monkeypatch):
    monitor = _load(monkeypatch)
    calls = []
    monkeypatch.setattr(monitor, "kill_upload", lambda: calls.append(True) or True)
    probe = _healthy_probe(monitor, upload=1)
    verdict, problems, actions, _notes = monitor.evaluate(probe, {})
    assert verdict == "DEGRADED"
    assert calls == [True]
    assert any(kind == "killed_upload" for kind, _ in actions)
    assert any(row["id"] == "upload_running" for row in problems)


def test_upload_safety_stop_can_be_disabled(monkeypatch):
    monitor = _load(monkeypatch, AUTOSLICE_MONITOR_ALLOW_UPLOAD_KILL="0")
    monkeypatch.setattr(
        monitor,
        "kill_upload",
        lambda: (_ for _ in ()).throw(AssertionError("kill attempted")),
    )
    probe = _healthy_probe(monitor, upload=1)
    verdict, problems, actions, _notes = monitor.evaluate(probe, {})
    assert verdict == "DEGRADED"
    assert not any(kind == "killed_upload" for kind, _ in actions)
    problem = next(row for row in problems if row["id"] == "upload_running")
    assert "监控未作远端修改" in problem["msg"]
