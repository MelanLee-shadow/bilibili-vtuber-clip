"""Guard the runner's SCRIPT entry point against circular-import regressions.

The rest of the suite imports the runner as the module
``scripts.session_autoslice``, but production (cron + systemd timers) runs
it as a script: ``python3 scripts/session_autoslice.py --once``. In that
case the runner is ``__main__``, and any submodule that did a plain
``import scripts.session_autoslice`` would re-execute the runner and
detonate the extraction cycle — an ImportError that never surfaces under
pytest's module import. This test runs the real script entry so that failure
mode can never ship again.
"""

import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "scripts" / "session_autoslice.py"
RECOVERY_PLANNER = ROOT / "scripts" / "plan_recovery_review_rerun.py"
RECOVERY_MANIFEST_BUILDER = ROOT / "scripts" / "build_recovery_review_manifest.py"
DEPLOY_SCRIPT = ROOT / "scripts" / "deploy_autoslice.sh"


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


def _run_embedded_status_validator(
    label: str,
    tmp_path: Path,
    payload: dict[str, object],
    *extra_args: str,
) -> subprocess.CompletedProcess[str]:
    status_path = tmp_path / f"{label}.json"
    status_path.write_text(json.dumps(payload), encoding="utf-8")
    return subprocess.run(
        [
            sys.executable,
            "-c",
            _embedded_deploy_python(label),
            str(status_path),
            *extra_args,
        ],
        capture_output=True,
        text=True,
        check=False,
    )


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


def test_deploy_adapter_repair_exception_is_changed_bytes_only_and_preinstall():
    source = DEPLOY_SCRIPT.read_text(encoding="utf-8")
    external = source.split("<<'REMOTE_EXTERNAL_INSTALL'\n", 1)[1].split(
        "\nREMOTE_EXTERNAL_INSTALL", 1
    )[0]

    changed = external.index("adapter_content_changed=0")
    clean_or_repair = external.index('if [ "$adapter_content_changed" -eq 0 ]; then', changed)
    clean_call = external.index('adapter_restart_safe "$old_adapter_sha"', clean_or_repair)
    repair_call = external.index('adapter_repair_restart_safe "$old_adapter_sha"', clean_call)
    child_gate_before_install = external.index(
        "adapter_identity_rebind_hash_child_absent", repair_call
    )
    install = external.index(
        'install_atomic \\\n    "$new_adapter_source"', child_gate_before_install
    )
    post_install_gate = external.index(
        'adapter_restart_environment_safe "$new_adapter_sha"', install
    )
    restart = external.index("docker restart bililive_adapter", post_install_gate)
    child_gate_before_restart = external.rindex(
        "adapter_identity_rebind_hash_child_absent", post_install_gate, restart
    )
    fresh_wait = external.index('wait_adapter_runtime "$restart_epoch" "$new_adapter_sha"', restart)

    assert (
        changed
        < clean_or_repair
        < clean_call
        < repair_call
        < child_gate_before_install
        < install
        < post_install_gate
        < child_gate_before_restart
        < restart
        < fresh_wait
    )
    assert "adapter_restart_safe" not in external[install:restart]
    assert 'cmp -s "$backup/external/recorder_adapter.file" "$host_adapter_path"' in external
    assert "docker exec bililive_adapter sha256sum /state/bililive_recorder_adapter.py" in external
    assert 'assert room.get("streaming") is False' in external
    assert 'assert room.get("recording") is False' in external
    assert 'assert payload.get("error") is None' in external
    assert "restart_epoch=$(python3 -c 'import time; print(time.time())')" in external


def test_deploy_adapter_hash_child_gate_is_strict_and_used_by_rollback(tmp_path):
    source = DEPLOY_SCRIPT.read_text(encoding="utf-8")
    external = source.split("<<'REMOTE_EXTERNAL_INSTALL'\n", 1)[1].split(
        "\nREMOTE_EXTERNAL_INSTALL", 1
    )[0]
    rollback = source.split("<<'REMOTE_ROLLBACK'\n", 1)[1].split("\nREMOTE_ROLLBACK", 1)[0]
    function = _embedded_shell_function(external, "adapter_identity_rebind_hash_child_absent")
    rollback_function = _embedded_shell_function(
        rollback, "adapter_identity_rebind_hash_child_absent"
    )
    assert rollback_function == function
    fake_docker = tmp_path / "docker"
    fake_docker.write_text(
        "#!/bin/sh\n"
        'test "${DOCKER_TOP_FAIL:-0}" != 1 || exit 1\n'
        'printf "%s\\n" "${DOCKER_TOP_OUTPUT:-}"\n',
        encoding="utf-8",
    )
    fake_docker.chmod(0o755)

    def run_gate(output: str = "", *, fail: bool = False) -> subprocess.CompletedProcess[str]:
        environment = os.environ.copy()
        environment["PATH"] = f"{tmp_path}:{environment['PATH']}"
        environment["DOCKER_TOP_OUTPUT"] = output
        environment["DOCKER_TOP_FAIL"] = "1" if fail else "0"
        return subprocess.run(
            ["/bin/bash", "-c", f"set -euo pipefail\n{function}\n{function.split('()', 1)[0]}"],
            capture_output=True,
            text=True,
            check=False,
            env=environment,
        )

    assert run_gate("COMMAND\npython3 /state/bililive_recorder_adapter.py").returncode == 0
    assert (
        run_gate(
            "COMMAND\npython3 /state/bililive_recorder_adapter.py "
            "--identity-rebind-hash-child request.json"
        ).returncode
        != 0
    )
    assert run_gate().returncode != 0
    assert run_gate("COMMAND", fail=True).returncode != 0
    assert "docker top bililive_adapter -eo pid,args" in function

    rollback_restart = rollback.index("docker restart bililive_adapter")
    rollback_child_gate = rollback.rindex(
        "adapter_identity_rebind_hash_child_absent", 0, rollback_restart
    )
    assert rollback_child_gate < rollback_restart


def test_deploy_clean_adapter_status_rejects_any_error(tmp_path):
    now = time.time()
    clean = {
        "generated_at_epoch": now,
        "service_reachable": True,
        "streaming": False,
        "recording": False,
        "finalizing": False,
        "error": None,
    }
    accepted = _run_embedded_status_validator("PY_CLEAN_ADAPTER_IDLE", tmp_path, clean)
    assert accepted.returncode == 0, accepted.stderr

    dirty = dict(clean)
    dirty.update(
        service_reachable=False,
        error="source disposition drift: finalized source fingerprint changed",
    )
    rejected = _run_embedded_status_validator("PY_CLEAN_ADAPTER_IDLE", tmp_path, dirty)
    assert rejected.returncode != 0


def test_deploy_adapter_repair_status_accepts_only_exact_idle_defect(tmp_path):
    now = time.time()
    repairable = {
        "generated_at_epoch": now,
        "service_reachable": False,
        "streaming": False,
        "recording": False,
        "finalizing": False,
        "error": "source disposition drift: finalized source fingerprint changed",
    }

    def _fresh_repairable(error=None):
        # The validator enforces a 90s freshness window on generated_at_epoch.
        # ~28 subprocess spawns run in this test; under load that can exceed
        # 90s of wall-clock time, so every accept-path payload must carry an
        # epoch computed right before its own subprocess call rather than
        # reusing the epoch captured once at the top of the test.
        payload = dict(repairable)
        payload["generated_at_epoch"] = time.time()
        if error is not None:
            payload["error"] = error
        return payload

    for label in (
        "PY_SUPPORTED_ADAPTER_REPAIR_IDLE",
        "PY_ROLLBACK_SUPPORTED_ADAPTER_REPAIR_IDLE",
    ):
        accepted = _run_embedded_status_validator(label, tmp_path, _fresh_repairable())
        assert accepted.returncode == 0, (label, accepted.stderr)

        for exact_error in (
            "source disposition identity rebind hash retry is pending",
            "source disposition identity rebind hash retry exhausted",
        ):
            typed = _fresh_repairable(exact_error)
            accepted = _run_embedded_status_validator(label, tmp_path, typed)
            assert accepted.returncode == 0, (label, exact_error, accepted.stderr)

    rejected_payloads = []
    for update in (
        {"error": "source disposition drift"},
        {"error": "SOURCE DISPOSITION DRIFT: mismatch"},
        {"error": "graphql unavailable"},
        {"error": "source disposition identity rebind hash is pending"},
        {"error": "source disposition identity rebind timed-out child is still pending"},
        {"error": "source disposition identity rebind hash retry is pending: details"},
        {"error": "source disposition identity rebind hash retry exhausted "},
        {"error": None},
        {"service_reachable": True},
        {"streaming": True},
        {"recording": True},
        {"finalizing": True},
        {"generated_at_epoch": now - 91},
        # A future timestamp must be rejected regardless of how far in the
        # future it is (the validator asserts `0 <= age`, so any positive
        # offset fails identically). Use a large offset (1h) instead of +1s
        # so the assertion can never be defeated by wall-clock drift during
        # this test's ~28 subprocess spawns racing past the +1s mark.
        {"generated_at_epoch": now + 3600},
    ):
        payload = dict(repairable)
        payload.update(update)
        rejected_payloads.append(payload)

    for label in (
        "PY_SUPPORTED_ADAPTER_REPAIR_IDLE",
        "PY_ROLLBACK_SUPPORTED_ADAPTER_REPAIR_IDLE",
    ):
        for payload in rejected_payloads:
            rejected = _run_embedded_status_validator(label, tmp_path, payload)
            assert rejected.returncode != 0, (label, payload)


def test_deploy_fresh_post_restart_status_never_accepts_repair_preimage(tmp_path):
    now = time.time()
    clean = {
        "generated_at_epoch": now,
        "service_reachable": True,
        "streaming": False,
        "recording": False,
        "finalizing": False,
        "error": None,
    }
    accepted = _run_embedded_status_validator("PY_FRESH", tmp_path, clean, str(now - 1))
    assert accepted.returncode == 0, accepted.stderr

    for error in (
        "source disposition drift: finalized source fingerprint changed",
        "source disposition identity rebind hash retry is pending",
        "source disposition identity rebind hash retry exhausted",
    ):
        repair_preimage = dict(clean)
        repair_preimage.update(service_reachable=False, error=error)
        rejected = _run_embedded_status_validator(
            "PY_FRESH", tmp_path, repair_preimage, str(now - 1)
        )
        assert rejected.returncode != 0, error


def test_deploy_rollback_reaccepts_only_clean_or_exact_supported_preimage(tmp_path):
    now = time.time()
    repairable = {
        "generated_at_epoch": now,
        "service_reachable": False,
        "streaming": False,
        "recording": False,
        "finalizing": False,
        "error": "source disposition drift: finalized source fingerprint changed",
    }
    rollback = _run_embedded_status_validator(
        "PY_ROLLBACK_FRESH", tmp_path, repairable, str(now - 1), "0"
    )
    assert rollback.returncode == 0, rollback.stderr

    for exact_error in (
        "source disposition identity rebind hash retry is pending",
        "source disposition identity rebind hash retry exhausted",
    ):
        typed = dict(repairable)
        typed["error"] = exact_error
        rollback = _run_embedded_status_validator(
            "PY_ROLLBACK_FRESH", tmp_path, typed, str(now - 1), "0"
        )
        assert rollback.returncode == 0, (exact_error, rollback.stderr)

        new_deploy = _run_embedded_status_validator(
            "PY_ROLLBACK_FRESH", tmp_path, typed, str(now - 1), "1"
        )
        assert new_deploy.returncode != 0

    new_deploy = _run_embedded_status_validator(
        "PY_ROLLBACK_FRESH", tmp_path, repairable, str(now - 1), "1"
    )
    assert new_deploy.returncode != 0

    for unknown_error in (
        "graphql unavailable",
        "source disposition identity rebind hash is pending",
        "source disposition identity rebind timed-out child is still pending",
        "source disposition identity rebind hash retry is pending: details",
    ):
        unknown = dict(repairable)
        unknown["error"] = unknown_error
        rejected = _run_embedded_status_validator(
            "PY_ROLLBACK_FRESH", tmp_path, unknown, str(now - 1), "0"
        )
        assert rejected.returncode != 0, unknown_error


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
