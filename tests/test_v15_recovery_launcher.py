from pathlib import Path


def test_recovery_refresh_excludes_live_python_bytecode() -> None:
    source = Path("scripts/launch_v15_recovery_once.sh").read_text(encoding="utf-8")

    assert "--exclude='*/__pycache__'" in source
    assert "--exclude='*/__pycache__/**'" in source
    assert "--exclude='*.pyc'" in source
    assert "--exclude='*.pyo'" in source
