from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize(
    "relative_script",
    [
        "scripts/aggregate_cue_speaker_challenge.py",
        "scripts/build_reviewed_voiceprint_enrollment.py",
        "scripts/evaluate_host_occupancy_challenge.py",
        "scripts/run_cue_aligned_speaker_shadow.py",
        "scripts/run_host_occupancy_shadow.py",
    ],
)
def test_shadow_cli_imports_from_neutral_working_directory(
    tmp_path: Path, relative_script: str
) -> None:
    completed = subprocess.run(
        [sys.executable, str(REPO_ROOT / relative_script), "--help"],
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr
