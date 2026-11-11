"""Monitor recorder roster, atomic reports and history retention, with fake command output.

No ssh, docker, network or real report directory is used.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from ops.recording.recording_topology import CONFIG_RELATIVE, RecordingTopology
from scripts import slice_monitor_recorders as recorders
from scripts import slice_monitor_reports as reports

ROOT = Path(__file__).resolve().parents[1]
NOW = 1_800_000_000.0


def _monitor(monkeypatch, report_dir):
    monkeypatch.setenv("AUTOSLICE_MONITOR_REPORT_DIR", str(report_dir))
    spec = importlib.util.spec_from_file_location(
        "slice_monitor_recorders_under_test", ROOT / "scripts" / "slice_monitor.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _n100(*, running=True, live=0, since=None, newest_age=60.0, restarting=False):
    newest = None if newest_age is None else {
        "file": "2026-11-10/22966160_x.flv", "mtime": NOW - newest_age, "size": 1}
    return {"ok": True, "now": NOW,
            "container": {"running": running, "restarting": restarting,
                          "status": "running" if running else "exited"},
            "live": {"status": live, "since_epoch": since}, "newest": newest}


def _host_probe(recorder_probes):
    """Fake OCI3 host probe of a paused recorder: no status ledger at all."""
    return {
        "ok": True, "now": NOW,
        "procs": {"recorder": 0, "scan": 0, "local_prepare": 0, "upload": 0, "auto_review_shadow": 0},
        "rooms": {"22966160": {"latest_date_dir": None, "recording": {"active": False, "mtime": None},
                               "slices": {}, "dirty_backlog": [], "recorder_status": {}}},
        "flags": {"delete_source": "never"}, "disk": {"free": 100 * 1024**3},
        "audio": {}, "auto_review_shadow": {}, "record_health": None,
        "recorder_probes": recorder_probes,
        "autoslice": {"heartbeat": None, "age_sec": None, "alerts": [], "crawlers": {},
                      "runner_disabled": True},
    }


def _ids(problems):
    return {p["id"]: p["sev"] for p in problems}


def test_paused_oci3_is_a_note_with_basis_not_down(monkeypatch, tmp_path):
    monitor = _monitor(monkeypatch, tmp_path)
    assert monitor.HOST_RECORDER_ACTIVE is False  # decided by the topology, not by the script
    verdict, problems, _, notes = monitor.evaluate(_host_probe({"n100": _n100()}), {})
    assert verdict == "OK", problems
    assert any("oci3" in n and "已暂停" in n and "2026-10-26" in n and "OCI 3" in n for n in notes)
    assert not any(p["id"] in {"recorder_down", "record_health_audit"} for p in problems)


def test_ledger_path_and_room_come_from_the_topology(monkeypatch, tmp_path):
    monitor = _monitor(monkeypatch, tmp_path)
    assert f'STATUS_PATH = "{monitor.TOPOLOGY.state_dir}/status.json"' in monitor.PRODUCTION_PROBE_PY
    assert "/opt/bilive/recording/status.json" not in monitor.PRODUCTION_PROBE_PY
    assert "RECORDER_ACTIVE = False" in monitor.PRODUCTION_PROBE_PY
    assert monitor.PRIMARY_ROOM == monitor.TOPOLOGY.room


def test_n100_container_missing_is_down(monkeypatch, tmp_path):
    monitor = _monitor(monkeypatch, tmp_path)
    verdict, problems, _, _ = monitor.evaluate(_host_probe({"n100": _n100(running=False)}), {})
    assert verdict == "DOWN" and _ids(problems)["recorder_n100_down"] == "DOWN"


def test_n100_unreachable_is_down_even_when_host_probe_failed(monkeypatch, tmp_path):
    monitor = _monitor(monkeypatch, tmp_path)
    broken = {"ok": False, "fatal": "ssh down",
              "recorder_probes": {"n100": {"ok": False, "fatal": "ssh error"}}}
    verdict, problems, _, _ = monitor.evaluate(broken, {})
    assert verdict == "DOWN" and {"recorder_n100_probe", "probe"} <= set(_ids(problems))


@pytest.mark.parametrize("newest_age, expect_alert", [
    (900.0, True),    # live for an hour, file untouched 15 min: not recording
    (None, True),     # live, no recording file at all
    (60.0, False),    # live and growing
])
def test_live_requires_a_growing_recording(monkeypatch, tmp_path, newest_age, expect_alert):
    monitor = _monitor(monkeypatch, tmp_path)
    probe = _n100(live=1, since=NOW - 3600, newest_age=newest_age)
    verdict, problems, _, _ = monitor.evaluate(_host_probe({"n100": probe}), {})
    assert ("recorder_n100_not_growing" in _ids(problems)) is expect_alert
    assert (verdict == "DOWN") is expect_alert


def test_just_went_live_and_offline_wait_instead_of_alerting(monkeypatch, tmp_path):
    monitor = _monitor(monkeypatch, tmp_path)
    fresh = _n100(live=1, since=NOW - 120, newest_age=4000.0)  # inside the attach grace
    offline = _n100(live=0, newest_age=4000.0)                 # not live: waiting is healthy
    for probe in (fresh, offline):
        verdict, problems, _, _ = monitor.evaluate(_host_probe({"n100": probe}), {})
        assert verdict == "OK", problems


def test_container_files_probe_runs_against_fake_docker_and_files(tmp_path):
    bindir, videos = tmp_path / "bin", tmp_path / "videos"
    (videos / "22966160" / "2026-11-09").mkdir(parents=True)
    (videos / "22966160" / "2026-11-10").mkdir()
    old = videos / "22966160" / "2026-11-09" / "a.flv"
    new = videos / "22966160" / "2026-11-10" / "b.flv"
    for path, age in ((old, 5000), (new, 30)):
        path.write_bytes(b"x" * 10)
        os.utime(path, (time.time() - age, time.time() - age))
    (videos / "22966160" / "2026-11-10" / "b.xml").write_text("ignored")
    bindir.mkdir()
    docker = bindir / "docker"
    docker.write_text('#!/bin/sh\necho \'{"Running": true, "Restarting": false, "Status": "running"}\'\n')
    docker.chmod(0o755)
    spec = {"container": "fake_recorder", "videos_root": str(videos)}
    fake_api = (
        "import io, json, urllib.request\n"
        "urllib.request.urlopen = lambda *a, **k: io.BytesIO(json.dumps("
        "{'data': {'live_status': 1, 'live_time': '2026-11-10 20:00:00'}}).encode())\n"
    )
    script = tmp_path / "probe.py"
    script.write_text(fake_api + recorders.probe_script(spec, "22966160"))
    done = subprocess.run([sys.executable, "-I", str(script)], capture_output=True, text=True,
                          env={"PATH": f"{bindir}:/usr/bin:/bin"}, check=True)
    result = json.loads(done.stdout)
    assert result["container"]["running"] is True
    assert result["live"]["status"] == 1 and result["live"]["since_epoch"] > 0
    assert result["newest"]["file"] == "2026-11-10/b.flv"


def test_run_container_files_probe_parses_ssh_stdout_and_reports_failures(monkeypatch):
    spec = {"ssh_host": "h", "container": "c", "videos_root": "/v"}

    def fake(rc, out="", err=""):
        return lambda *a, **k: subprocess.CompletedProcess(a, rc, out, err)

    monkeypatch.setattr(subprocess, "run", fake(0, "noise\n" + json.dumps({"ok": True, "now": 1}) + "\n"))
    assert recorders.run_container_files_probe(spec, "1") == {"ok": True, "now": 1}
    monkeypatch.setattr(subprocess, "run", fake(255, "", "Connection refused"))
    assert recorders.run_container_files_probe(spec, "1")["ok"] is False
    monkeypatch.setattr(subprocess, "run", fake(0, "not json"))
    assert "unparseable" in recorders.run_container_files_probe(spec, "1")["fatal"]


def test_runner_heartbeat_checks_follow_the_disabled_switch(monkeypatch, tmp_path):
    monitor = _monitor(monkeypatch, tmp_path)
    probe = _host_probe({"n100": _n100()})
    _, problems, _, notes = monitor.evaluate(probe, {})
    assert not {"autoslice_heartbeat_missing", "autoslice_not_concluded"} & set(_ids(problems))
    assert any("DISABLED" in n for n in notes)
    probe["autoslice"]["runner_disabled"] = False  # switch off: the existing checks still fire
    _, problems, _, _ = monitor.evaluate(probe, {})
    assert _ids(problems)["autoslice_heartbeat_missing"] == "DEGRADED"


def test_autoslice_probe_reads_heartbeat_with_sudo_and_the_switch(monkeypatch, tmp_path):
    monitor = _monitor(monkeypatch, tmp_path)
    seen = []

    def fake(argv, **kwargs):
        seen.append(argv[-1])
        out = "2026-09-14T04:20:05+0000 live=False\n__SEP__\n__CRAWL__\n__DISABLED__\nyes\n"
        return subprocess.CompletedProcess(argv, 0, out, "")

    monkeypatch.setattr(subprocess, "run", fake)
    result = monitor.run_autoslice_probe()
    assert "sudo -n cat /opt/bilive/autoslice/reports/heartbeat.txt" in seen[0]
    assert result["runner_disabled"] is True and result["heartbeat"].startswith("2026-09-14")


# ---- topology contract ------------------------------------------------------
def _topology(mutate):
    document = json.loads((ROOT / CONFIG_RELATIVE).read_text())
    mutate(document["recorders"])
    return RecordingTopology(document)


def test_topology_recorders_contract():
    ok = _topology(lambda r: None)
    assert ok.recorders["oci3"]["state"] == "paused" and ok.recorders["n100"]["state"] == "active"
    for mutate in (
        lambda r: r["oci3"].pop("paused_basis"),               # a pause needs its basis
        lambda r: r["oci3"].update(state="active", extra="x"),  # no undeclared fields
        lambda r: r["n100"].update(state="paused"),             # at least one active recorder
        lambda r: r["n100"].update(probe="unknown"),
        lambda r: r["n100"].update(videos_root="relative/path"),
        lambda r: r["oci3"].update(paused_since="yesterday"),
    ):
        with pytest.raises(ValueError):
            _topology(mutate)


# ---- atomic writes and history ------------------------------------------------
def test_atomic_write_replaces_whole_file_and_survives_a_failed_write(monkeypatch, tmp_path):
    target = tmp_path / "state.json"
    reports.atomic_write_json(str(target), {"verdict": "OK"})
    reports.atomic_write_json(str(target), {"verdict": "DOWN"})
    assert json.loads(target.read_text()) == {"verdict": "DOWN"}

    def boom(fd):
        raise OSError("disk full while flushing")

    monkeypatch.setattr(os, "fsync", boom)
    with pytest.raises(OSError):
        reports.atomic_write_json(str(target), {"verdict": "NEVER"})
    assert json.loads(target.read_text()) == {"verdict": "DOWN"}   # old file still whole
    assert sorted(p.name for p in tmp_path.iterdir()) == ["state.json"]  # no temp litter


def test_failed_replace_keeps_old_report_and_removes_temp(monkeypatch, tmp_path):
    target = tmp_path / "latest.md"
    reports.atomic_write_text(str(target), "old\n")
    monkeypatch.setattr(os, "replace", lambda *a: (_ for _ in ()).throw(OSError("replace failed")))
    with pytest.raises(OSError):
        reports.atomic_write_text(str(target), "new but interrupted\n")
    assert target.read_text() == "old\n"
    assert [p.name for p in tmp_path.iterdir()] == ["latest.md"]


def test_write_reports_markdown_and_files(tmp_path):
    ts = datetime(2026, 11, 11, 4, 39, 5, tzinfo=timezone(timedelta(hours=8)))
    problems = [{"id": "x", "sev": "DOWN", "msg": "坏了", "fix": "修", "detail": ["d1"]}]
    md = reports.write_reports(str(tmp_path), {"p": 1}, "DOWN", problems, [("a", "act")], ["n1"], ts)
    assert (tmp_path / "latest.md").read_text() == md + "\n"
    assert "- **[DOWN] 坏了**\n  - 建议: 修\n    - `d1`" in md and "- ✅ act" in md and "- n1" in md
    latest = json.loads((tmp_path / "latest.json").read_text())
    assert latest["verdict"] == "DOWN" and latest["actions"] == [{"kind": "a", "msg": "act"}]
    assert [p.name for p in (tmp_path / "history").iterdir()] == ["20261111-043905-DOWN.json"]
    assert reports.write_reports(str(tmp_path), {}, "OK", [], [], ["n"], ts).endswith("_无问题。_")


def test_history_keeps_recent_days_only_and_touches_only_its_own_files(tmp_path):
    history = tmp_path / "history"
    history.mkdir()
    now = datetime(2026, 11, 11, 12, 0, 0, tzinfo=timezone(timedelta(hours=8)))
    old = [f"{(now - timedelta(days=d)).strftime('%Y%m%d-%H%M%S')}-OK.json" for d in (8, 30, 400)]
    keep = [f"{(now - timedelta(days=d)).strftime('%Y%m%d-%H%M%S')}-DOWN.json" for d in (0, 3, 6)]
    foreign = ["notes.txt", "20200101-000000-OK.json.bak"]
    for name in old + keep + foreign:
        (history / name).write_text("{}")
    assert reports.prune_history(str(tmp_path), now) == 3
    assert sorted(p.name for p in history.iterdir()) == sorted(keep + foreign)


def test_host_probe_of_a_paused_recorder_runs_without_a_ledger_and_ignores_ci_pytest(
        monkeypatch, tmp_path):
    """Run the real host-probe program with a fake `ps`: no status file is read, and a CI
    pytest command line that merely names an old-plane script is not an old-plane process."""
    monitor = _monitor(monkeypatch, tmp_path)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    ps = bindir / "ps"
    ps.write_text(
        "#!/bin/sh\n"
        "echo '/usr/bin/python3 -B /opt/bilive/autoslice/current/scripts/session_autoslice.py'\n"
        "echo '/venv/bin/python -B -m pytest -q tests/test_session_autoslice.py "
        "tests/lidousha/test_auto_review_shadow_daemon.py'\n")
    ps.chmod(0o755)
    script = tmp_path / "host_probe.py"
    script.write_text(monitor.PRODUCTION_PROBE_PY)
    done = subprocess.run([sys.executable, "-I", str(script)], capture_output=True, text=True,
                          env={"PATH": f"{bindir}:/usr/bin:/bin"}, check=True)
    result = json.loads(done.stdout)
    assert result["ok"] is True and result["record_health"] is None
    assert result["procs"]["scan"] == 1 and result["procs"]["auto_review_shadow"] == 0


def test_public_export_topology_stub_satisfies_the_exported_parser():
    """The exporter rewrites the private cloud-drive token inside the parser, so the public stub
    must carry the rewritten token; the monitor loads the topology at import time."""
    from scripts import export_oss_snapshot as exporter
    from scripts._export_oss_policy import STUB_FILES

    rel = "ops/recording/recording_topology.py"
    source = exporter._rewrite_content(rel, exporter._sanitize_text((ROOT / rel).read_text()))
    namespace: dict = {"__name__": "exported_recording_topology"}
    exec(compile(source, "exported_recording_topology.py", "exec"), namespace)
    exported_parser = namespace["RecordingTopology"]
    stub = json.loads(STUB_FILES["ops/recording/oci3-recording-topology.json"])
    assert exported_parser(stub).recorders
    stub["backup_root"] = "/path/to/cloud/云盘/backup"  # the "obvious" fix is wrong
    with pytest.raises(ValueError, match="backup must remain on 123"):
        exported_parser(stub)
