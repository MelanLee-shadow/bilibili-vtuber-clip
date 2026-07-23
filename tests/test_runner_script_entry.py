"""Guard the runner's SCRIPT entry point against circular-import regressions.

The rest of the suite imports the runner as the module
``scripts.free_session_autoslice``, but production (cron + systemd timers) runs
it as a script: ``python3 scripts/free_session_autoslice.py --once``. In that
case the runner is ``__main__``, and any submodule that did a plain
``import scripts.free_session_autoslice`` would re-execute the runner and
detonate the extraction cycle — an ImportError that never surfaces under
pytest's module import. This test runs the real script entry so that failure
mode can never ship again.
"""

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "scripts" / "free_session_autoslice.py"
RECOVERY_PLANNER = ROOT / "scripts" / "plan_recovery_review_rerun.py"


def test_runner_runs_as_script_without_import_cycle():
    result = subprocess.run(
        [sys.executable, str(RUNNER), "--help"],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, (
        "runner --help failed as a script (cron invocation path):\n"
        + result.stderr[-2000:]
    )
    # argparse prints the usage banner; a circular-import crash would not.
    assert "usage" in (result.stdout + result.stderr).lower()


def test_recovery_planner_bootstraps_repo_root_for_direct_execution():
    probe = (
        "import runpy,sys; "
        f"ns=runpy.run_path({str(RECOVERY_PLANNER)!r}, "
        "run_name='recovery_planner_probe'); "
        "assert str(ns['ROOT']) in sys.path"
    )
    result = subprocess.run(
        [sys.executable, "-I", "-c", probe],
        cwd="/",
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr[-2000:]
