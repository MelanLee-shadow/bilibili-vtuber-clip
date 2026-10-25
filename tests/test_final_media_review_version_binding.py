"""Versioned configuration tests. No real sentinel, model, or media PASS."""
import copy
import hashlib
import json
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

from src.autoslice import final_media_review_gemini_api_runtime as api
from src.autoslice.final_media_review_code_binding import (
    SOURCE_FILES, ADAPTER, HELPER, RUNNER, build_code_binding, child_environment,
)
from src.autoslice.runtime_code_lease import VERSION_ENV
from src.autoslice.final_media_review_model_capability_sentinel import (
    SENTINEL_RUNTIME_FILENAME, load_sentinel_runtime, _adapter_environment,
)
from src.autoslice.final_media_review_capability_autobootstrap import ensure_runtime_raw_av_capability
from tests.test_final_media_review_gemini_api_runtime import _runtime_tree

REPO = Path(__file__).resolve().parents[1]


def seal(repo, commit):
    entries = {}
    for relative in SOURCE_FILES:
        p = repo / relative
        entries[relative] = {"type": "file", "mode": stat.S_IMODE(p.stat().st_mode),
                             "sha256": hashlib.sha256(p.read_bytes()).hexdigest()}
    payload = {"schema_version": "oci3-shadow-autoslice-repo-manifest.v1",
               "commit": commit, "entries": entries,
               "tree_sha256": hashlib.sha256(json.dumps(entries, ensure_ascii=False,
                    sort_keys=True, separators=(",", ":")).encode()).hexdigest()}
    (repo / "DEPLOYED_COMMIT").write_text(commit + "\n")
    (repo / "DEPLOYED_MANIFEST.json").write_text(json.dumps(payload))


def install(root, commit, *, legacy=False):
    repo = root / "repo" if legacy else root / "versions" / commit / "repo"
    for relative in SOURCE_FILES:
        target = repo / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(REPO / relative, target)
        target.chmod(0o755 if relative.startswith("scripts/") else 0o644)
    seal(repo, commit)
    return repo


def layout(tmp_path, monkeypatch, *, legacy=False):
    root, env = _runtime_tree(tmp_path)
    repo = install(root, "a" * 40, legacy=legacy)
    monkeypatch.setattr(api, "__file__", str(repo / "src/autoslice/final_media_review_gemini_api_runtime.py"))
    if not legacy:
        (root / "current").symlink_to(repo.parent)
        env[VERSION_ENV] = str(repo)
        # Distinguishable preserved legacy files.
        (root / "repo" / ADAPTER).write_text("# preserved legacy fixture\n")
    return root, repo, env


@pytest.mark.parametrize("legacy", [False, True])
def test_normal_constructor_binds_executing_deployment_and_canonical_state(tmp_path, monkeypatch, legacy):
    root, repo, env = layout(tmp_path, monkeypatch, legacy=legacy)
    legacy_bytes = (root / "repo" / ADAPTER).read_bytes()
    result = api.ensure_gemini_api_sentinel_runtime(root, environment=env)
    runtime = load_sentinel_runtime(root)
    assert result["provider_calls"] == 0
    assert runtime["code_binding"] == build_code_binding(root, repo)
    assert runtime["adapter"]["absolute_path"] == str(repo / ADAPTER)
    assert runtime["runner"]["absolute_path"] == str(repo / RUNNER)
    assert Path(result["path"]).parent == root
    assert not (repo.parent / SENTINEL_RUNTIME_FILENAME).exists() if not legacy else True
    assert (root / "repo" / ADAPTER).read_bytes() == legacy_bytes
    assert not (root / "final-media-review-raw-av-capability.json").exists()


def test_current_switch_keeps_inflight_runner_adapter_and_state_version(tmp_path, monkeypatch):
    root, first, env = layout(tmp_path, monkeypatch)
    api.ensure_gemini_api_sentinel_runtime(root, environment=env)
    runtime = load_sentinel_runtime(root)
    second = install(root, "b" * 40)
    (root / "current").unlink()
    (root / "current").symlink_to(second.parent)
    env[VERSION_ENV] = str(second)
    assert load_sentinel_runtime(root)["runtime_contract_sha256"] == runtime["runtime_contract_sha256"]
    seen = {}
    def explicit_runner_stub(command, **kwargs):
        seen.update(command=command, environment=kwargs["env"])
        return subprocess.CompletedProcess(command, 2, json.dumps({
            "status": "FAILED", "provider_calls": 0, "reason_code": "EXPLICIT_TEST_NO_SENTINEL"}), "")
    result = ensure_runtime_raw_av_capability(root, environment=env, run=explicit_runner_stub)
    assert result["status"] == "FAILED" and result["provider_calls"] == 0
    assert seen["command"][0] == str(first / RUNNER)
    assert seen["environment"][VERSION_ENV] == str(first)
    for selected in (
        child_environment(env, root=root, code_binding=runtime["code_binding"]),
        _adapter_environment(root, "fixture", env, runtime["code_binding"]),
    ):
        assert selected[VERSION_ENV] == str(first)
    assert seen["environment"]["AUTOSLICE_BASE"] == str(root)


@pytest.mark.parametrize("damage", [
    "runner", "adapter", "helper", "runtime_source", "commit", "manifest",
    "symlink_source", "version_directory", "bound_hash", "missing_binding",
])
def test_load_rejects_source_or_version_drift_before_runner(tmp_path, monkeypatch, damage):
    root, repo, env = layout(tmp_path, monkeypatch)
    api.ensure_gemini_api_sentinel_runtime(root, environment=env)
    target = {"runner": RUNNER, "adapter": ADAPTER, "helper": HELPER,
              "runtime_source": "src/autoslice/final_media_review_gemini_api_runtime.py"}.get(damage)
    if target:
        (repo / target).write_bytes((repo / target).read_bytes() + b"\n# drift\n")
    elif damage == "commit":
        (repo / "DEPLOYED_COMMIT").write_text("c" * 40)
    elif damage == "manifest":
        p = repo / "DEPLOYED_MANIFEST.json"
        p.write_bytes(p.read_bytes() + b" ")
    elif damage == "symlink_source":
        p = repo / HELPER
        held = repo / "held-helper.py"
        p.rename(held)
        p.symlink_to(held)
    else:
        p = root / SENTINEL_RUNTIME_FILENAME
        value = json.loads(p.read_bytes())
        if damage == "version_directory":
            value["code_binding"]["repo"] = "current/repo"
        elif damage == "bound_hash":
            value["code_binding"]["source_files"][HELPER]["sha256"] = "0" * 64
        else:
            value.pop("code_binding")
        p.write_text(json.dumps(value))
    with pytest.raises((ValueError, RuntimeError)):
        load_sentinel_runtime(root)
    assert not (root / "final-media-review-raw-av-capability.json").exists()


def test_constructor_rejects_inherited_different_version(tmp_path, monkeypatch):
    root, repo, env = layout(tmp_path, monkeypatch)
    env[VERSION_ENV] = str(install(root, "b" * 40))
    with pytest.raises(ValueError, match="EXECUTING_VERSION_DRIFT"):
        api.ensure_gemini_api_sentinel_runtime(root, environment=env)
    assert not (root / SENTINEL_RUNTIME_FILENAME).exists()


def test_constructor_does_not_provision_deployed_legacy_from_development_code(tmp_path):
    root, env = _runtime_tree(tmp_path)
    install(root, "a" * 40, legacy=True)
    with pytest.raises(ValueError, match="EXECUTING_VERSION_REQUIRED"):
        api.ensure_gemini_api_sentinel_runtime(root, environment=env)


def test_exact_helper_pin_is_checked_even_against_resealed_manifest(tmp_path, monkeypatch):
    root, repo, env = layout(tmp_path, monkeypatch)
    (repo / HELPER).write_bytes((repo / HELPER).read_bytes() + b"\n# different helper\n")
    seal(repo, "a" * 40)
    with pytest.raises(ValueError, match="HELPER_PIN_DRIFT"):
        api.ensure_gemini_api_sentinel_runtime(root, environment=env)
    assert not (root / SENTINEL_RUNTIME_FILENAME).exists()


def test_new_version_reconfiguration_conflicts_with_existing_inflight_manifest(tmp_path, monkeypatch):
    root, repo, env = layout(tmp_path, monkeypatch)
    api.ensure_gemini_api_sentinel_runtime(root, environment=env)
    original = (root / SENTINEL_RUNTIME_FILENAME).read_bytes()
    second = install(root, "b" * 40)
    monkeypatch.setattr(api, "__file__", str(second / "src/autoslice/final_media_review_gemini_api_runtime.py"))
    env[VERSION_ENV] = str(second)
    with pytest.raises(ValueError, match="RUNTIME_CONFLICT"):
        api.ensure_gemini_api_sentinel_runtime(root, environment=env)
    assert (root / SENTINEL_RUNTIME_FILENAME).read_bytes() == original

def test_normal_version_lease_cli_configures_physical_repo_and_replays_after_current_move(tmp_path):
    import os
    import sys
    root, env = _runtime_tree(tmp_path)
    first = install(root, "a" * 40)
    # Complete committed-source fixture for the actual public CLI. No network.
    for name in ("src", "scripts"):
        shutil.copytree(REPO / name, first / name, dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    for relative in SOURCE_FILES:
        (first / relative).chmod(0o755 if relative.startswith("scripts/") else 0o644)
    seal(first, "a" * 40)
    wrapper = first.parent / "runtime-code-lease.py"
    shutil.copyfile(REPO / "src/autoslice/runtime_code_lease.py", wrapper)
    (root / "current").symlink_to(first.parent)
    command = [sys.executable, "-B", str(root / "current/runtime-code-lease.py"),
        str(root / "current/repo"), str(root / "current/repo/scripts/admit_final_media_review.py"),
        "configure-api", "--runtime-root", str(root), "--model", env[api.MODEL_ENV]]
    selected = {**os.environ, **env}
    selected.pop(VERSION_ENV, None)
    selected.pop("PYTHONPATH", None)
    first_run = subprocess.run(command, env=selected, capture_output=True, text=True, timeout=45)
    assert first_run.returncode == 0, first_run.stderr
    value = json.loads(first_run.stdout)
    assert value["provider_calls"] == 0 and value["content_review_status"] == "NOT_PERFORMED"
    captured = load_sentinel_runtime(root)
    assert captured["code_binding"]["repo"] == first.relative_to(root).as_posix()
    second = install(root, "b" * 40)
    shutil.copyfile(wrapper, second.parent / "runtime-code-lease.py")
    (root / "current").unlink()
    (root / "current").symlink_to(second.parent)
    selected[VERSION_ENV] = str(first)
    cold = subprocess.run(command, env=selected, capture_output=True, text=True, timeout=45)
    assert cold.returncode == 0, cold.stderr
    assert json.loads(cold.stdout)["status"] == "GEMINI_API_SENTINEL_RUNTIME_REUSED"
    assert load_sentinel_runtime(root)["code_binding"] == captured["code_binding"]
