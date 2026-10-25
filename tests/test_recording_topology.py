"""Server acceptance: change only the topology, observe real consumers.

No network, service control, recorder, provider or credential is used. The Mac
implementation worker only compiles these tests; execution belongs to OCI3.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys

import pytest

from ops.recording.recording_topology import CONFIG_RELATIVE, RecordingTopology, load_topology
from scripts.check_recording_topology_literals import find_literals

ROOT = Path(__file__).resolve().parents[1]
COMPOSE = Path("ops/recording/docker-compose.oci3-bililive-recorder.yml")
INSTALLER = Path("scripts/install_oci3_recording_health.sh")


def _run(argv, *, cwd=None, env=None):
    result = subprocess.run(argv, cwd=cwd, env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout


def _copy_checkout(target: Path, document: dict) -> Path:
    # Only small code/config inputs; no media, secrets or other worktrees.
    for rel in ("ops/recording",):
        shutil.copytree(ROOT / rel, target / rel, ignore=shutil.ignore_patterns("__pycache__"))
    for rel in (INSTALLER, "scripts/oci3_no_upload_soak.sh", "scripts/run_oci3_shadow_date_once.sh",
                "scripts/deploy_oci3_shadow_adapter.sh", "scripts/release_clouddrive_maintenance_hold.py",
                "src/autoslice/clouddrive_hold_release.py", "src/autoslice/source_integrity.py",
                "src/autoslice/evidence_identity.py",
                "tests/test_source_integrity.py"):
        path = target / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / rel, path)
    for package in ("ops", "src", "src/autoslice", "tests"):
        (target / package / "__init__.py").touch()
    config = target / CONFIG_RELATIVE
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(json.dumps(document, ensure_ascii=False) + "\n")
    return target


def _seal(repo: Path) -> None:
    entries = {}
    for path in repo.rglob("*"):
        if "__pycache__" in path.parts:
            continue
        rel = path.relative_to(repo).as_posix()
        mode = stat.S_IMODE(path.stat().st_mode) & ~0o022
        entries[rel] = ({"type": "dir", "mode": mode} if path.is_dir() else {
            "type": "file", "mode": mode, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
    digest = hashlib.sha256(json.dumps(entries, ensure_ascii=False, sort_keys=True,
                                      separators=(",", ":")).encode()).hexdigest()
    (repo / "DEPLOYED_COMMIT").write_text("a" * 40 + "\n")
    (repo / "DEPLOYED_MANIFEST.json").write_text(json.dumps({
        "schema_version": "oci3-shadow-autoslice-repo-manifest.v1", "commit": "a" * 40,
        "entries": entries, "tree_sha256": digest}, ensure_ascii=False) + "\n")
    for name in ("DEPLOYED_COMMIT", "DEPLOYED_MANIFEST.json"):
        (repo / name).chmod(0o644)


CONSUMER_PROBE = r'''
import json
from pathlib import Path
import sys
import pytest
from ops.recording import bililive_recorder_adapter as adapter
from ops.recording import recording_archive as identity
from ops.recording import recording_spool_mover as mover
from ops.recording import record_health_audit as health
from ops.recording import sealed_archive_intake as intake
from ops.recording.recording_topology import load_topology
from src.autoslice import source_integrity
from tests.test_source_integrity import test_recording_inventory_blocks_same_size_successor_rewrite_with_restored_mtime

t = load_topology()
assert identity.SPOOL_ROOT == t.spool_root
assert identity.ARCHIVE_ROOT == t.archive_root
assert identity.ROOM == t.room
args = adapter.parse_args(["--once", "--local-first"])
assert args.record_root == t.spool_room
assert args.room == int(t.room)
assert args.state_path == t.state_dir / "adapter-state.json"
assert health.parse_args(["--local-first"]).spool_root == t.spool_room
assert mover.parse_args([]).spool == t.spool_room
assert intake.DEFAULT_COVERAGE_STATE_PATHS == t.coverage_states
date = t.spool_room / "2026-10-20"
date.mkdir(parents=True, exist_ok=True)
result = source_integrity.audit_finalized_recording_inventory(
    date, room_id=t.room, adapter_state_path=Path(sys.argv[1]) / "missing-ledger.json")
assert result["status"] == "BLOCKED", result
assert result["issues"][0]["code"] == "RECORDING_ARCHIVE_PENDING", result
# Reuse the exact original failure assertion after the topology has changed.
work = Path(sys.argv[1]) / "integrity-control"
work.mkdir()
with pytest.MonkeyPatch.context() as patch:
    test_recording_inventory_blocks_same_size_successor_rewrite_with_restored_mtime(work, patch)
print(json.dumps({"spool": str(t.spool_room), "archive": str(t.archive_room),
                  "same_size_rewrite": "BLOCKED"}))
'''


@pytest.mark.parametrize("name", ["capture-next", "a-different-root"])
def test_spool_only_edit_reaches_installer_scripts_adapter_and_downstream(tmp_path, name):
    document = json.loads((ROOT / CONFIG_RELATIVE).read_text())
    old = document["spool_root"]
    document["spool_root"] = str(tmp_path / name)  # the only topology edit
    topology = RecordingTopology(document)
    repo = _copy_checkout(tmp_path / "checkout", document)
    _seal(repo)
    install_root = tmp_path / "installed"
    install_root.mkdir()
    env = {**os.environ, "OCI3_RECORDING_HEALTH_TEST_MODE": "1",
           "OCI3_RECORDING_HEALTH_TEST_ROOT": str(install_root)}
    command = ["bash", str(repo / INSTALLER)]
    _run([*command, "--recording-root", document["spool_root"], "recording-host"], env=env)
    _run([*command, "--verify", "recording-host"], env=env)
    base = install_root / "opt/bilive"
    rendered = (base / "compose.yml").read_text()
    assert old not in rendered
    topology.validate_compose(rendered)
    for mount in topology.consumer_mounts.values():
        assert f"{topology.spool_root}:{mount}:rshared" in rendered
    derived = base / "autoslice/recording-health.env"
    assert _run(["bash", "-c", 'source "$1"; printf %s "$AUTOSLICE_RECORDING_SPOOL_ROOM"',
                 "probe", str(derived)]) == str(topology.spool_room)
    # Installed adapter/health/mover dependencies must load outside a checkout.
    for directory in (base / "ops", base / "recording"):
        assert (directory / "recording_topology.py").read_bytes() == (repo / "ops/recording/recording_topology.py").read_bytes()
        assert json.loads((directory / CONFIG_RELATIVE.name).read_text()) == document
    probe = repo / "consumer-probe.py"
    probe.write_text(CONSUMER_PROBE)
    result = json.loads(_run([sys.executable, str(probe), str(tmp_path)], cwd=repo,
                             env={**os.environ, "PYTHONPATH": str(repo)}))
    assert result["spool"] == str(topology.spool_room)
    assert result["same_size_rewrite"] == "BLOCKED"
    # Execute the actual initialization blocks. No launcher workload is run.
    for rel, stop, variable in (
        ("scripts/oci3_no_upload_soak.sh", "MODE=${1:-}", "SOURCE_PATH"),
        ("scripts/run_oci3_shadow_date_once.sh", "refuse() {", "SOURCE_ROOT_DEFAULT"),
    ):
        source = (repo / rel).read_text()
        prelude = source[:source.index(stop)]
        script = (repo / rel).with_name("topology-probe.sh")
        script.write_text(prelude + f'\nprintf "%s|%s" "$AUTOSLICE_RECORDING_SPOOL_ROOM" "${variable}"\n')
        assert _run(["bash", str(script)]) == f"{topology.spool_room}|{topology.archive_room}"
    # Run the deployed adapter script's same remote prelude against the local
    # fixture. This is a shell read, never SSH or service mutation.
    current = base / "autoslice/current"
    current.mkdir()
    (current / "repo").symlink_to(repo, target_is_directory=True)
    deploy = (repo / "scripts/deploy_oci3_shadow_adapter.sh").read_text()
    prelude = deploy.split("<<'REMOTE_SCRIPT'\n", 1)[1].split("txn=$base/", 1)[0]
    prelude = prelude.replace("base=/opt/bilive", "base=" + str(base))
    script = repo / "scripts/adapter-topology-probe.sh"
    script.write_text(prelude + '\nprintf %s "$spool_probe"\n')
    assert _run(["bash", str(script), "preflight", "test-owner"]) == str(topology.spool_room)
    # Exercise the installed boot gate with protocol-local command doubles.
    tools = tmp_path / "bin"
    tools.mkdir()
    for filename, body in {
        "findmnt": '#!/bin/sh\nprintf "ext4\\n"\n',
        "docker": '#!/bin/sh\nprintf "%s\\n" "$@" > "$DOCKER_ARGS"\n',
    }.items():
        tool = tools / filename
        tool.write_text(body)
        tool.chmod(0o755)
    start = repo / "start-test.sh"
    start.write_text((base / "ops/start_local_recording.sh").read_text().replace("/opt/bilive", str(base)))
    argv_path = tmp_path / "docker-args"
    _run(["bash", str(start)], env={**os.environ, "PATH": str(tools) + os.pathsep + os.environ["PATH"],
                                       "DOCKER_ARGS": str(argv_path)})
    assert topology.spool_room.is_dir()
    assert "--pull\nnever\n" in argv_path.read_text()
    # Installed topology drift is refused by the ordinary verification gate.
    (base / "ops" / CONFIG_RELATIVE.name).write_text("{}\n")
    drift = subprocess.run([*command, "--verify", "recording-host"], env=env, capture_output=True, text=True)
    assert drift.returncode == 2
    assert "installed bytes drifted" in drift.stderr


def test_definition_is_the_only_recording_root_literal_source():
    assert find_literals(ROOT) == []


@pytest.mark.parametrize("scope", ["src", "scripts", "ops", "config"])
@pytest.mark.parametrize("key", ["spool_root", "archive_root", "n100_archive_root", "backup_root"])
def test_literal_guard_rejects_new_hardcoded_root(tmp_path, scope, key):
    for folder in ("src", "scripts", "ops/recording", "config"):
        (tmp_path / folder).mkdir(parents=True, exist_ok=True)
    shutil.copy2(ROOT / CONFIG_RELATIVE, tmp_path / CONFIG_RELATIVE)
    injected = tmp_path / scope / "bad-consumer.py"
    injected.write_text("RECORDING_ROOT = " + repr(str(getattr(load_topology(), key))) + "\n")
    assert find_literals(tmp_path) == [f"{scope}/bad-consumer.py:1"]


@pytest.mark.parametrize("key", ["archive_root", "n100_archive_root", "backup_root", "cloud_mount"])
def test_topology_never_authorizes_capture_in_archive_or_backup(key):
    document = json.loads((ROOT / CONFIG_RELATIVE).read_text())
    document["spool_root"] = document[key] + "/capture"
    with pytest.raises(ValueError, match="spool"):
        RecordingTopology(document)


def test_room_and_container_mounts_derive_from_the_same_definition():
    document = json.loads((ROOT / CONFIG_RELATIVE).read_text())
    document["rooms"] = ["123456"]
    document["consumer_mounts"] = {name: "/capture/" + str(index) for index, name in
                                    enumerate(document["consumer_mounts"])}
    topology = RecordingTopology(document)
    compose = topology.render_compose((ROOT / COMPOSE).read_text())
    topology.validate_compose(compose)
    assert str(topology.consumer_mounts["bililive-adapter"] / "123456") in compose
    assert "22966160" not in compose


def test_ordinary_installer_rejects_unconfigured_requested_root(tmp_path):
    document = json.loads((ROOT / CONFIG_RELATIVE).read_text())
    repo = _copy_checkout(tmp_path / "checkout", document)
    _seal(repo)
    install_root = tmp_path / "installed"
    install_root.mkdir()
    result = subprocess.run(["bash", str(repo / INSTALLER), "--recording-root", "/unconfigured/capture", "recording-host"],
                            env={**os.environ, "OCI3_RECORDING_HEALTH_TEST_MODE": "1",
                                 "OCI3_RECORDING_HEALTH_TEST_ROOT": str(install_root)},
                            capture_output=True, text=True)
    assert result.returncode == 2
    assert "differs from the topology" in result.stderr
    assert not (install_root / "opt/bilive/compose.yml").exists()
