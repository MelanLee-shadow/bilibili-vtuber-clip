"""The F2 old-generator guards must work in a Git-free exported test tree."""
import json
import shutil
import subprocess

import pytest

from tests.f2_baseline import BASELINE, BASELINE_MODULES, FIXTURE_DIR, baseline_module


@pytest.fixture
def exported_baseline(tmp_path):
    return shutil.copytree(FIXTURE_DIR, tmp_path / "export/tests/fixtures/f2_baseline")


def test_baseline_manifest_covers_the_frozen_generators():
    manifest = json.loads((FIXTURE_DIR / "MANIFEST.json").read_text(encoding="utf-8"))
    assert manifest["source_commit"] == BASELINE
    assert sorted(row["source_path"] for row in manifest["files"]) == sorted(
        f"src/autoslice/{name}.py" for name in BASELINE_MODULES)
    assert sorted(path.name for path in FIXTURE_DIR.iterdir()) == sorted(
        ["MANIFEST.json", *(f"{name}.py.txt" for name in BASELINE_MODULES)])


@pytest.mark.parametrize("name", BASELINE_MODULES)
def test_baseline_module_loads_without_git(name, exported_baseline, monkeypatch):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("F2 baseline loading must not invoke Git or a subprocess")
    monkeypatch.setattr(subprocess, "run", forbidden)
    module = baseline_module(name, fixture_dir=exported_baseline)
    assert module.__package__ == "src.autoslice"
    assert module.__name__ == f"src.autoslice._f2_main_{name}"


@pytest.mark.parametrize("name", BASELINE_MODULES)
def test_baseline_sha_mismatch_fails_before_execution(name, exported_baseline):
    path = exported_baseline / f"{name}.py.txt"
    # Invalid code must fail hash validation, never reach compile/exec.
    path.write_bytes(path.read_bytes() + b"\nthis is not valid Python !!!\n")
    with pytest.raises(AssertionError, match="F2_BASELINE_SHA256_MISMATCH"):
        baseline_module(name, fixture_dir=exported_baseline)


@pytest.mark.parametrize("name", BASELINE_MODULES)
def test_missing_baseline_fixture_fails_without_skipping(name, exported_baseline):
    (exported_baseline / f"{name}.py.txt").unlink()
    with pytest.raises(FileNotFoundError):
        baseline_module(name, fixture_dir=exported_baseline)


def test_baseline_commit_mismatch_fails_without_skipping(exported_baseline):
    path = exported_baseline / "MANIFEST.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["source_commit"] = "0" * 40
    path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(AssertionError, match="F2_BASELINE_COMMIT_MISMATCH"):
        baseline_module("nonsemantic_sound", fixture_dir=exported_baseline)
