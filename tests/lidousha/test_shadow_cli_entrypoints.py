from __future__ import annotations

import os
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


def test_host_occupancy_help_does_not_import_evaluator_dependencies(tmp_path: Path) -> None:
    """The smoke-only help path must not pay for diarization/profile imports."""

    guard = tmp_path / "import_guard"
    guard.mkdir()
    (guard / "sitecustomize.py").write_text(
        """import builtins
_original = builtins.__import__
def guarded(name, *args, **kwargs):
    if name in {
        'src.autoslice.host_occupancy',
        'src.autoslice.channel_profile',
        'scripts.run_cue_aligned_speaker_shadow',
    }:
        raise RuntimeError('heavy evaluator import during --help: ' + name)
    return _original(name, *args, **kwargs)
builtins.__import__ = guarded
""",
        encoding="utf-8",
    )
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(guard) + os.pathsep + environment.get("PYTHONPATH", "")
    completed = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts/evaluate_host_occupancy_challenge.py"), "--help"],
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
        env=environment,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.startswith("usage: evaluate_host_occupancy_challenge.py")
    assert "--occupancy-report" in completed.stdout
