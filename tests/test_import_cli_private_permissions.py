"""Exercise real import CLI creation permissions using synthetic local packages.

The fixture is intentionally not a publishable package. Later native audit or
manifest failure is expected; --skip-qc prevents all image-provider execution.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import subprocess
import sys

import pytest

from tests.test_import_external_package import (
    CANDIDATE,
    DATE,
    SRC_REPO,
    SRC_WORKSPACE,
    _build_external_package,
)

ROOT = Path(__file__).resolve().parents[1]


def invoke(fixture, *, apply=True, mask=0o022):
    command = [
        sys.executable,
        "-B",
        str(ROOT / "scripts/import_external_package.py"),
        "--source",
        str(fixture.staging_package),
        "--date",
        DATE,
        "--candidate",
        CANDIDATE,
        "--base",
        str(fixture.base),
        "--repo-root",
        str(fixture.repo_root),
        "--source-repo-root",
        SRC_REPO,
        "--source-workspace-root",
        SRC_WORKSPACE,
        "--allow-new-pick",
        "--skip-qc",
    ]
    if apply:
        command.append("--apply")
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", AUTOSLICE_BASE=str(fixture.base))
    completed = subprocess.run(
        command, cwd=ROOT, env=env, capture_output=True, text=True, timeout=30, umask=mask
    )
    receipt = json.loads(completed.stdout)
    assert completed.returncode in (0, 2), completed.stderr
    return receipt


@pytest.mark.parametrize("ambient_mask", [0o000, 0o022, 0o077])
def test_new_package_has_private_qc_parent_regardless_of_shell_mask(tmp_path, ambient_mask):
    fixture = _build_external_package(tmp_path)
    state = fixture.state_path.read_bytes()
    receipt = invoke(fixture, mask=ambient_mask)
    assert any(row["step"] == "COPY" and row["status"] == "PASS" for row in receipt["steps"])
    assert stat.S_IMODE(fixture.destination_package.stat().st_mode) == 0o700
    assert fixture.state_path.read_bytes() == state  # Incomplete fixture rolls back.
    assert receipt["status"] == "REFUSED"  # Not an end-to-end quality PASS.


def test_dry_run_never_creates_a_private_destination(tmp_path):
    fixture = _build_external_package(tmp_path)
    receipt = invoke(fixture, apply=False)
    assert receipt["status"] == "DRY_RUN_OK"
    assert not fixture.destination_package.exists()


def test_existing_directory_permissions_are_not_silently_changed(tmp_path):
    fixture = _build_external_package(tmp_path)
    fixture.destination_package.mkdir(parents=True, mode=0o755)
    fixture.destination_package.chmod(0o755)
    invoke(fixture)
    assert stat.S_IMODE(fixture.destination_package.stat().st_mode) == 0o755


def test_importing_cli_does_not_change_callers_creation_mask():
    code = "import os; os.umask(0o022); import scripts.import_external_package; assert os.umask(0o022) == 0o022"
    completed = subprocess.run(
        [sys.executable, "-B", "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=10
    )
    assert completed.returncode == 0, completed.stderr
