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

import hashlib
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "scripts" / "free_session_autoslice.py"
RECOVERY_PLANNER = ROOT / "scripts" / "plan_recovery_review_rerun.py"
RECOVERY_MANIFEST_BUILDER = ROOT / "scripts" / "build_lidousha_recovery_review_manifest.py"
DEPLOY_SCRIPT = ROOT / "scripts" / "deploy_free_autoslice.sh"


def _embedded_deploy_python(label: str) -> str:
    source = DEPLOY_SCRIPT.read_text(encoding="utf-8")
    opener = f"<<'{label}'\n"
    start = source.index(opener) + len(opener)
    end = source.index(f"\n{label}\n", start)
    return source[start:end]


def _embedded_shell_function(block: str, name: str) -> str:
    start = block.index(f"{name}() {{")
    end = block.index("\n}", start) + len("\n}")
    return block[start:end]


def test_runner_runs_as_script_without_import_cycle():
    result = subprocess.run(
        [sys.executable, str(RUNNER), "--help"],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, (
        "runner --help failed as a script (cron invocation path):\n" + result.stderr[-2000:]
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


def test_recovery_manifest_builder_bootstraps_repo_root_for_direct_execution():
    probe = (
        "import runpy,sys; "
        f"ns=runpy.run_path({str(RECOVERY_MANIFEST_BUILDER)!r}, "
        "run_name='recovery_manifest_builder_probe'); "
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


def test_deploy_owns_disabled_before_remote_wait_can_be_interrupted():
    source = DEPLOY_SCRIPT.read_text(encoding="utf-8")
    ownership = source.index(
        'DISABLED_TOUCHED=1\nssh "$HOST" "touch \'$DISABLED\'; /usr/bin/flock -w 7200'
    )
    staging = source.index(
        'ssh "$HOST" "test ! -e \'$STAGE\'',
        ownership,
    )

    assert ownership < staging


def test_runner_passes_default_adapter_state_to_recording_inventory():
    source = RUNNER.read_text(encoding="utf-8")

    assert '"/opt/bilive/recording/adapter-state.json"' in source
    assert "adapter_state_path=RECORDER_ADAPTER_STATE_PATH" in source


def test_deploy_commits_and_transactionally_installs_recorder_adapter():
    source = DEPLOY_SCRIPT.read_text(encoding="utf-8")
    external = source.split("<<'REMOTE_EXTERNAL_INSTALL'\n", 1)[1].split(
        "\nREMOTE_EXTERNAL_INSTALL", 1
    )[0]
    outer_rollback = source.split("<<'REMOTE_ROLLBACK'\n", 1)[1].split("\nREMOTE_ROLLBACK", 1)[0]

    assert "scripts src ops assets profiles" in source
    assert '"ops",' in source
    assert (
        "capture_file recorder_adapter /opt/bilive/recording/bililive_recorder_adapter.py"
    ) in source
    assert ("/opt/bilive/autoslice/repo/ops/recording/bililive_recorder_adapter.py") in external
    assert 'install -m "$mode" "$source" "$tmp"' in external
    assert 'cp "$source" "$tmp"' not in external
    assert "adapter_restart_safe" in external
    assert 'payload.get("streaming") is False' in external
    assert 'payload.get("recording") is False' in external
    assert 'payload.get("finalizing") is False' in external
    assert "recorder_adapter.restart-required" in external
    assert "docker restart bililive_adapter" in external
    assert "sha256sum /opt/bilive/recording/bililive_recorder_adapter.py" in external
    assert "docker exec bililive_adapter sha256sum /state/bililive_recorder_adapter.py" in external
    assert ".State.Health.Status" in external
    assert "generated >= float(sys.argv[2])" in external
    assert 'payload.get("error") is None' in external
    assert "query_room_status" in external
    assert "adapter_content_changed" in external
    assert "timeout 15 find /adapter/Videos" in external
    assert "restore_adapter_atomic" in outer_rollback
    assert "docker restart bililive_adapter" in outer_rollback
    assert outer_rollback.index('crontab "$backup/external/crontab.file"') < outer_rollback.index(
        "docker restart bililive_adapter"
    )
    assert 'cmp -s "$backup/DEPLOYED_COMMIT.old" "$repo/DEPLOYED_COMMIT"' in (outer_rollback)


def test_deploy_authority_manifest_is_canonical_and_exact_byte_bound(tmp_path):
    repo = tmp_path / "repo"
    registry = repo / "assets/lidousha/publication_registry.v1.json"
    authority = repo / "assets/lidousha/authorities/story.json"
    nested_authority = repo / "assets/lidousha/authorities/nested/solo.json"
    baseline_manifest = (
        repo / "assets/lidousha/reviewed_subtitle_baselines/story.subtitle-baseline.v1.json"
    )
    reviewed_srt = repo / "assets/lidousha/reviewed_subtitle_baselines/story.reviewed.srt"
    exact_interval = (
        repo / "assets/lidousha/reviewed_exact_source_intervals/"
        "story.reviewed-exact-source-interval.v1.json"
    )
    registry.parent.mkdir(parents=True)
    authority.parent.mkdir(parents=True)
    nested_authority.parent.mkdir(parents=True)
    baseline_manifest.parent.mkdir(parents=True)
    exact_interval.parent.mkdir(parents=True)
    registry.write_bytes(b'{"registry":"truth"}\n')
    authority.write_bytes('{"name":"莉娅"}\n'.encode())
    nested_authority.write_bytes(b'{"candidate":"solo"}\n')
    baseline_manifest.write_bytes(b'{"candidate":"story"}\n')
    reviewed_srt.write_bytes(b"1\n00:00:00,000 --> 00:00:01,000\nstory\n")
    exact_interval.write_bytes(b'{"interval":"reviewed"}\n')
    (authority.parent / "README.txt").write_text("not an authority document")
    commit = "a" * 40
    output = repo / ".manifest.tmp"

    generated = subprocess.run(
        [
            sys.executable,
            "-c",
            _embedded_deploy_python("REMOTE_AUTHORITY_MANIFEST_PY"),
            str(repo),
            commit,
            str(output),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert generated.returncode == 0, generated.stderr
    assert output.read_bytes().endswith(b"\n")
    manifest = json.loads(output.read_text(encoding="utf-8"))
    declared_hash = manifest.pop("manifest_sha256")
    canonical = json.dumps(
        manifest,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    assert declared_hash == "sha256:" + hashlib.sha256(canonical).hexdigest()
    assert manifest["schema_version"] == "deployed-authority-manifest.v1"
    assert manifest["deployed_commit"] == commit
    expected_paths = {
        "assets/lidousha/publication_registry.v1.json": registry,
        "assets/lidousha/authorities/story.json": authority,
        "assets/lidousha/authorities/nested/solo.json": nested_authority,
        (
            "assets/lidousha/reviewed_subtitle_baselines/story.subtitle-baseline.v1.json"
        ): baseline_manifest,
        ("assets/lidousha/reviewed_subtitle_baselines/story.reviewed.srt"): reviewed_srt,
        (
            "assets/lidousha/reviewed_exact_source_intervals/"
            "story.reviewed-exact-source-interval.v1.json"
        ): exact_interval,
    }
    assert set(manifest["entries"]) == set(expected_paths)
    for relative, path in expected_paths.items():
        payload = path.read_bytes()
        assert manifest["entries"][relative] == {
            "bytes": len(payload),
            "sha256": "sha256:" + hashlib.sha256(payload).hexdigest(),
        }

    output.replace(repo / "DEPLOYED_AUTHORITY_MANIFEST.json")
    (repo / "DEPLOYED_COMMIT").write_text(
        f"{commit}  deployed 2026-08-11T00:00:00Z\n",
        encoding="utf-8",
    )
    verifier = _embedded_deploy_python("REMOTE_VERIFY_DEPLOYMENT_IDENTITY_PY")
    verified = subprocess.run(
        [sys.executable, "-c", verifier, str(repo), commit],
        capture_output=True,
        text=True,
        check=False,
    )
    assert verified.returncode == 0, verified.stderr

    authority.write_bytes(b'{"name":"drifted"}\n')
    drifted = subprocess.run(
        [sys.executable, "-c", verifier, str(repo), commit],
        capture_output=True,
        text=True,
        check=False,
    )
    assert drifted.returncode != 0
    assert "readback mismatch" in drifted.stderr


def test_deploy_authority_identity_is_captured_and_restored_on_every_path(tmp_path):
    source = DEPLOY_SCRIPT.read_text(encoding="utf-8")
    outer_rollback = source.split("<<'REMOTE_ROLLBACK'\n", 1)[1].split("\nREMOTE_ROLLBACK", 1)[0]
    remote_switch = source.split("<<'REMOTE_SWITCH'\n", 1)[1].split("\nREMOTE_SWITCH", 1)[0]
    identity_seal = source.split("<<'REMOTE_SEAL_DEPLOYMENT_IDENTITY'\n", 1)[1].split(
        "\nREMOTE_SEAL_DEPLOYMENT_IDENTITY", 1
    )[0]

    assert "capture_repository_file" in remote_switch
    assert "deployed_authority_manifest" in remote_switch
    for rollback_path in (outer_rollback, remote_switch, identity_seal):
        assert "restore_repository_file" in rollback_path
        assert "DEPLOYED_AUTHORITY_MANIFEST.json" in rollback_path
        assert 'rm -f "$destination"' in rollback_path
        assert 'cmp -s "$backup/repository/$label.file" "$destination"' in rollback_path

    tree_verified = source.index('if [ "$LOCAL_MANIFEST" != "$REMOTE_DEPLOYED_MANIFEST" ]')
    seal_started = source.index("REMOTE_SEAL_DEPLOYMENT_IDENTITY", tree_verified)
    committed = source.index("COMMITTED=1", seal_started)
    assert tree_verified < seal_started < committed
    assert identity_seal.index('mv -f "$manifest_tmp" "$manifest_path"') < (
        identity_seal.index('mv -f "$commit_tmp" "$commit_path"')
    )
    assert identity_seal.count("PYTHONDONTWRITEBYTECODE=1 python3") == 2

    for index, rollback_path in enumerate((outer_rollback, remote_switch, identity_seal)):
        restore_function = _embedded_shell_function(rollback_path, "restore_repository_file")
        harness = (
            "set -euo pipefail\n"
            "repo=$1\n"
            "backup=$2\n"
            f"{restore_function}\n"
            "restore_repository_file deployed_authority_manifest "
            '"$repo/DEPLOYED_AUTHORITY_MANIFEST.json"\n'
        )

        present = tmp_path / f"present-{index}"
        present_repo = present / "repo"
        present_backup = present / "backup/repository"
        present_repo.mkdir(parents=True)
        present_backup.mkdir(parents=True)
        destination = present_repo / "DEPLOYED_AUTHORITY_MANIFEST.json"
        destination.write_bytes(b"new identity\n")
        old = present_backup / "deployed_authority_manifest.file"
        old.write_bytes(b"old identity\n")
        old.chmod(0o640)
        (present_backup / "deployed_authority_manifest.present").touch()
        restored = subprocess.run(
            ["bash", "-c", harness, "restore-test", str(present_repo), str(present / "backup")],
            capture_output=True,
            text=True,
            check=False,
        )
        assert restored.returncode == 0, restored.stderr
        assert destination.read_bytes() == b"old identity\n"
        assert destination.stat().st_mode & 0o777 == 0o640

        absent = tmp_path / f"absent-{index}"
        absent_repo = absent / "repo"
        absent_backup = absent / "backup/repository"
        absent_repo.mkdir(parents=True)
        absent_backup.mkdir(parents=True)
        absent_destination = absent_repo / "DEPLOYED_AUTHORITY_MANIFEST.json"
        absent_destination.write_bytes(b"new identity\n")
        (absent_backup / "deployed_authority_manifest.absent").touch()
        removed = subprocess.run(
            ["bash", "-c", harness, "restore-test", str(absent_repo), str(absent / "backup")],
            capture_output=True,
            text=True,
            check=False,
        )
        assert removed.returncode == 0, removed.stderr
        assert not absent_destination.exists()
