"""The slice monitor's modules, report directory and wrapper follow one project root."""

from __future__ import annotations

import ast
import importlib.util
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"


def _load(env_dir, monkeypatch):
    if env_dir is None:
        monkeypatch.delenv("AUTOSLICE_MONITOR_REPORT_DIR", raising=False)
    else:
        monkeypatch.setenv("AUTOSLICE_MONITOR_REPORT_DIR", str(env_dir))
    spec = importlib.util.spec_from_file_location(
        "slice_monitor_layout_under_test", SCRIPTS / "slice_monitor.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_report_dir_defaults_to_this_checkout_and_env_redirects_it(monkeypatch, tmp_path):
    default = _load(None, monkeypatch)
    assert default.REPORT_DIR == str(ROOT / "reports" / "slice_monitor")
    assert default.STATE_FILE == str(ROOT / "reports" / "slice_monitor" / "state.json")

    redirected = _load(tmp_path, monkeypatch)
    assert redirected.REPORT_DIR == str(tmp_path)
    assert redirected.LATEST_JSON == str(tmp_path / "latest.json")


def test_probe_programs_are_valid_python_with_every_placeholder_substituted(monkeypatch):
    monitor = _load(None, monkeypatch)
    for source in (monitor.PROBE_PY, monitor.PRODUCTION_PROBE_PY, monitor.JINGTING_PROBE_PY):
        ast.parse(source)
    assert "__ROOMS__" not in monitor.PROBE_PY and monitor.PRIMARY_ROOM in monitor.PROBE_PY
    assert "__ROOM__" not in monitor.PRODUCTION_PROBE_PY
    assert f'ROOM = "{monitor.PRIMARY_ROOM}"' in monitor.PRODUCTION_PROBE_PY


def test_wrapper_derives_project_and_report_dir_without_a_hardcoded_checkout():
    path = SCRIPTS / "slice_monitor.sh"
    wrapper = path.read_text()
    assert "/Users/" not in wrapper
    assert "AUTOSLICE_MONITOR_REPORT_DIR" in wrapper
    assert subprocess.run(["bash", "-n", str(path)]).returncode == 0
    proj_line = next(line for line in wrapper.splitlines() if line.startswith("PROJ="))
    derived = subprocess.run(
        ["bash", "-c", proj_line + '; printf %s "$PROJ"', str(path)],
        capture_output=True, text=True, check=True,
    ).stdout
    assert Path(derived).resolve() == ROOT
