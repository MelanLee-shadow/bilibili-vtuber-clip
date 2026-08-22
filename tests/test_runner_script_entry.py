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
import io
import json
import os
import subprocess
import sys
import time
import tarfile
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


def _canonical_sha256(payload: object) -> str:
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _bootstrap_state_material(payload: dict[str, object]) -> dict[str, object]:
    material = {key: value for key, value in payload.items() if key != "last_room_status_epoch"}
    cookie_health = material.get("cookie_health")
    assert isinstance(cookie_health, dict)
    material["cookie_health"] = {
        key: value
        for key, value in cookie_health.items()
        if key not in {"checked_at", "checked_at_epoch"}
    }
    return material


def _bootstrap_status_projection(payload: dict[str, object]) -> dict[str, object]:
    projected = json.loads(json.dumps(payload))
    projected.pop("generated_at", None)
    projected.pop("generated_at_epoch", None)
    cookie_status = projected.get("bilibili_cookie")
    assert isinstance(cookie_status, dict)
    projected["bilibili_cookie"] = {
        key: value
        for key, value in cookie_status.items()
        if key not in {"checked_at", "checked_at_epoch"}
    }
    errors = projected.get("finalize_errors")
    if isinstance(errors, list):
        for entry in errors:
            if isinstance(entry, dict) and isinstance(entry.get("error"), str):
                entry["error"] = __import__("re").sub(
                    r"0x[0-9a-fA-F]+", "0x<address>", entry["error"]
                )
    return projected


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
    ownership = source.index("DISABLED_TOUCHED=1")
    touch = source.index('ssh "$HOST" "touch \'$DISABLED\'"', ownership)
    staging = source.index(
        'ssh "$HOST" "test ! -e \'$STAGE\'',
        ownership,
    )

    assert ownership < touch < staging


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


def test_deploy_adapter_repair_idle_is_gated_before_external_install():
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
    strict_external_install = external.index(
        'if [ "$external_payload_unchanged" -eq 0 ]; then', repair_call
    )
    install = external.index('"$new_adapter_source"', child_gate_before_install)
    post_install_gate = external.index(
        'adapter_restart_environment_safe "$new_adapter_sha"', install
    )
    restart = external.index("docker restart bililive_adapter", post_install_gate)
    child_gate_before_restart = external.rindex(
        "adapter_identity_rebind_hash_child_absent", post_install_gate, restart
    )
    bootstrap_marker_activation = external.rindex(
        "activate_connection_stub_bootstrap_marker", post_install_gate, restart
    )
    fresh_wait = external.index('wait_adapter_runtime "$restart_epoch" "$new_adapter_sha"', restart)

    assert (
        changed
        < clean_or_repair
        < clean_call
        < repair_call
        < strict_external_install
        < child_gate_before_install
        < install
        < post_install_gate
        < child_gate_before_restart
        < bootstrap_marker_activation
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


def test_deploy_unchanged_adapter_external_drift_accepts_supported_repair_idle(
    tmp_path: Path,
) -> None:
    """A cron-only drift may install while the unchanged adapter is repair-idle."""

    source = DEPLOY_SCRIPT.read_text(encoding="utf-8")
    external = source.split("<<'REMOTE_EXTERNAL_INSTALL'\n", 1)[1].split(
        "\nREMOTE_EXTERNAL_INSTALL", 1
    )[0]
    status_validator = _embedded_shell_function(
        external, "adapter_status_supported_repair_idle"
    )
    selection_start = external.index("external_payload_unchanged=0")
    selection_end = external.index(
        'if [ "$external_payload_unchanged" -eq 0 ]; then\n    printf',
        selection_start,
    )
    selection = external[selection_start:selection_end]
    status_path = tmp_path / "status.json"
    external_drift = tmp_path / "runner-cron-drift"
    selected = tmp_path / "repair-idle-selected"
    status_path.write_text(
        json.dumps(
            {
                "generated_at_epoch": time.time(),
                "service_reachable": False,
                "streaming": False,
                "recording": False,
                "finalizing": False,
                "error": "source disposition drift: finalized source fingerprint changed",
            }
        ),
        encoding="utf-8",
    )
    external_drift.write_text("runner cron differs\n", encoding="utf-8")
    status_validator = status_validator.replace(
        "/opt/bilive/recording/status.json", str(status_path)
    )
    script = "\n".join(
        (
            "set -euo pipefail",
            status_validator,
            "adapter_restart_safe() { return 1; }",
            "adapter_repair_restart_safe() { adapter_status_supported_repair_idle && touch \"$REPAIR_SELECTED\"; }",
            "adapter_connection_stub_bootstrap_safe() { return 1; }",
            "external_payload_unchanged_safe() { test ! -e \"$EXTERNAL_DRIFT\"; }",
            "adapter_content_changed=0",
            "old_adapter_sha=unchanged",
            "new_adapter_sha=unchanged",
            "connection_stub_bootstrap=0",
            selection,
            'test -f "$REPAIR_SELECTED"',
        )
    )

    def run() -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["/bin/bash", "-c", script],
            capture_output=True,
            text=True,
            check=False,
            env={
                **os.environ,
                "EXTERNAL_DRIFT": str(external_drift),
                "REPAIR_SELECTED": str(selected),
            },
        )

    accepted = run()
    assert accepted.returncode == 0, accepted.stderr
    assert selected.exists()

    selected.unlink()
    status_path.write_text(
        json.dumps(
            {
                "generated_at_epoch": time.time(),
                "service_reachable": False,
                "streaming": False,
                "recording": False,
                "finalizing": False,
                "error": "graphql unavailable",
            }
        ),
        encoding="utf-8",
    )
    rejected = run()
    assert rejected.returncode != 0
    assert not selected.exists()


def test_deploy_connection_stub_bootstrap_is_receipt_bound_and_exact():
    source = DEPLOY_SCRIPT.read_text(encoding="utf-8")
    external = source.split("<<'REMOTE_EXTERNAL_INSTALL'\n", 1)[1].split(
        "\nREMOTE_EXTERNAL_INSTALL", 1
    )[0]
    outer_rollback = source.split("<<'REMOTE_ROLLBACK'\n", 1)[1].split(
        "\nREMOTE_ROLLBACK", 1
    )[0]
    switch = source.split("<<'REMOTE_SWITCH'\n", 1)[1].split("\nREMOTE_SWITCH", 1)[0]

    assert '"$BACKUP" "$COMMIT" <<\'REMOTE_EXTERNAL_INSTALL\'' in source
    assert "adapter_connection_stub_bootstrap_safe" in external
    assert "adapter_connection_stub_bootstrap_postcondition" in external
    assert "--prepare-connection-stub-bootstrap" in external
    assert "--bootstrap-receipt-root /state/connection-stub-bootstrap-receipts" in external
    assert "2026-08-20/22966160_20260820-21-00-20.flv" in external
    assert "2026-08-20/22966160_20260820-21-56-14.flv" in external
    assert 'assert isinstance(paths, list) and len(paths) == 2' in external
    assert 'payload.get("error") == f"{len(paths)} closed recording(s) failed finalization"' in external
    assert "capture_connection_stub_bootstrap_preimage" in external
    assert "activate_connection_stub_bootstrap_marker" in external
    assert "restore_connection_stub_bootstrap_preimage" in source
    assert "restore_connection_stub_bootstrap_preimage" in outer_rollback
    assert "restore_connection_stub_bootstrap_preimage" in switch
    assert "capture_file recorder_adapter_state" not in source
    assert "capture_file recorder_adapter_status" not in source
    assert "restore_file recorder_adapter_state" not in source
    assert "restore_file recorder_adapter_status" not in source
    assert "assert all(dispositions.get(path) == rows[path] for path in paths)" in external


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


def test_deploy_unchanged_external_route_preserves_a_live_adapter(tmp_path):
    source = DEPLOY_SCRIPT.read_text(encoding="utf-8")
    external = source.split("<<'REMOTE_EXTERNAL_INSTALL'\n", 1)[1].split(
        "\nREMOTE_EXTERNAL_INSTALL", 1
    )[0]
    outer_rollback = source.split("<<'REMOTE_ROLLBACK'\n", 1)[1].split(
        "\nREMOTE_ROLLBACK", 1
    )[0]
    cron_start = external.index("watchdog_cron=")
    cron_end = external.index("external_payload_exact() {")
    cron_definitions = external[cron_start:cron_end]
    names = (
        "adapter_status_zero_touch_fresh",
        "adapter_environment_healthy",
        "external_payload_exact",
        "external_payload_unchanged_safe",
    )
    function_definitions = "\n".join(
        _embedded_shell_function(external, name) for name in names
    )
    route_start = external.index('test -f "$backup/external/recorder_adapter.present"')
    # Include the final fast-route closure, not merely the selection gate.
    route = external[route_start:]

    root = tmp_path / "root"
    repo = root / "repo"
    backup = root / "backup"
    recording = root / "recording"
    cloud_root = root / "cloud-root"
    cloud = cloud_root / "123云盘/live-streaming"
    watchdog = root / "free_mount_watchdog.sh"
    sentinel = root / "upload_fatal_sentinel.sh"
    uploader = root / "do_upload.sh"
    cloud.mkdir(parents=True)
    recording.mkdir()
    adapter_state_path = recording / "adapter-state.json"
    adapter_state_path.write_bytes(b"adapter state remains untouched\n")
    adapter_state_path.chmod(0o600)
    entries = (
        ("watchdog", repo / "scripts/free_mount_watchdog.sh", watchdog, 0o755, 0o755),
        ("upload_sentinel", repo / "scripts/clouddrive_upload_fatal_sentinel.sh", sentinel, 0o755, 0o755),
        ("uploader", repo / "scripts/free_do_upload.sh", uploader, 0o755, 0o700),
        (
            "recorder_adapter",
            repo / "ops/recording/bililive_recorder_adapter.py",
            recording / "bililive_recorder_adapter.py",
            0o644,
            0o755,
        ),
    )
    originals: dict[Path, tuple[bytes, int]] = {}
    for label, staged, target, staged_mode, target_mode in entries:
        payload = f"{label} committed payload\n".encode()
        staged.parent.mkdir(parents=True, exist_ok=True)
        target.parent.mkdir(parents=True, exist_ok=True)
        staged.write_bytes(payload)
        target.write_bytes(payload)
        staged.chmod(staged_mode)
        target.chmod(target_mode)
        originals[target] = (payload, target_mode)
        preimage = backup / f"external/{label}.file"
        preimage.parent.mkdir(parents=True, exist_ok=True)
        preimage.write_bytes(payload)
        preimage.chmod(target_mode)
        (backup / f"external/{label}.present").touch()
    now = time.time()
    (recording / "status.json").write_text(
        json.dumps(
            {
                "generated_at_epoch": now,
                "service_reachable": False,
                "streaming": True,
                "recording": True,
                "finalizing": False,
                "error": "source disposition drift: finalized source fingerprint changed",
            }
        ),
        encoding="utf-8",
    )
    expected_sha = hashlib.sha256(originals[recording / "bililive_recorder_adapter.py"][0]).hexdigest()

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    (fake_bin / "findmnt").write_text(
        "#!/bin/sh\n"
        'case "$*" in *"-o TARGET") printf "%s\\n" "$TEST_CLOUD_ROOT";; '
        '*"-o FSTYPE") printf "%s\\n" fuseblk;; *"-o SOURCE") printf "%s\\n" CloudFS;; esac\n',
        encoding="utf-8",
    )
    (fake_bin / "timeout").write_text("#!/bin/sh\nshift\nexec \"$@\"\n", encoding="utf-8")
    (fake_bin / "sha256sum").write_text(
        "#!/bin/sh\nexec /usr/bin/shasum -a 256 \"$@\"\n", encoding="utf-8"
    )
    (fake_bin / "stat").write_text(
        "#!/bin/sh\n"
        'test "$1" = -c && test "$2" = %a || exit 2\n'
        'exec /usr/bin/stat -f %Lp "$3"\n',
        encoding="utf-8",
    )
    (fake_bin / "crontab").write_text(
        "#!/bin/sh\n"
        'if [ "$1" = -l ]; then\n'
        '  printf "read\\n" >> "$CRONTAB_READ_LOG"\n'
        '  reads=$(wc -l < "$CRONTAB_READ_LOG")\n'
        '  if [ "$reads" -eq 1 ] && [ -n "${CRON_DRIFT_TARGET:-}" ]; then\n'
        '    printf "%s\\n" "post-selection payload drift" > "$CRON_DRIFT_TARGET"\n'
        '    chmod 755 "$CRON_DRIFT_TARGET"\n'
        '  fi\n'
        '  if [ "$reads" -eq 1 ] && [ -n "${CRON_DRIFT_STATUS:-}" ]; then\n'
        '    printf "{\\\"generated_at_epoch\\\":%s,\\\"service_reachable\\\":1,\\\"streaming\\\":true,\\\"recording\\\":true,\\\"finalizing\\\":false,\\\"error\\\":[]}\\n" "$(date +%s)" > "$CRON_DRIFT_STATUS"\n'
        '  fi\n'
        '  case "${CRONTAB_MODE:-exact}" in\n'
        '    missing) exit 0;;\n'
        '    duplicate) printf "%s\\n%s\\n" "$CRONTAB_CONTENT" "$CRONTAB_CONTENT";;\n'
        '    stale) printf "%s\\n" "$CRONTAB_CONTENT"; printf "%s\\n" "* * * * * /opt/bilive/autoslice/free_mount_watchdog.sh";;\n'
        '    post-stale) if [ "$reads" -gt 1 ]; then printf "%s\\n" "* * * * * /opt/bilive/autoslice/free_mount_watchdog.sh"; else printf "%s\\n" "$CRONTAB_CONTENT"; fi;;\n'
        '    *) printf "%s\\n" "$CRONTAB_CONTENT";;\n'
        '  esac\n'
        '  exit 0\n'
        'fi\n'
        'printf write >> "$CRONTAB_WRITE_LOG"\n'
        'exit 0\n',
        encoding="utf-8",
    )
    (fake_bin / "docker").write_text(
        "#!/bin/sh\n"
        'if [ "$1" = inspect ]; then\n'
        '  format=$3; name=$4\n'
        '  case "$format" in\n'
        '    *".State.Status"*) printf "%s\\n" "${DOCKER_STATE:-running}";;\n'
        '    *".State.StartedAt"*) cat "$STARTED_AT_PATH";;\n'
        '    *".State.Health"*) printf "%s\\n" "${DOCKER_HEALTH:-healthy}";;\n'
        '    *".Config.Cmd"*) printf "%s\\n" "${DOCKER_CMD:-python3|/state/bililive_recorder_adapter.py}";;\n'
        '    *".Mounts"*) printf "%s\\n" "${DOCKER_BIND:-$TEST_RECORDING|bind|true}";;\n'
        '  esac\n'
        '  exit 0\n'
        'fi\n'
        'if [ "$1" = exec ]; then\n'
        '  container=$2; shift 2\n'
        '  case "$1" in\n'
        '    stat) printf "%s\\n" fuseblk;;\n'
        '    timeout) exit 0;;\n'
        '    sha256sum) if [ "$container" = bililive_adapter ]; then\n'
        '      if [ -n "${DOCKER_ADAPTER_SHA:-}" ]; then printf "%s  /state/bililive_recorder_adapter.py\\n" "$DOCKER_ADAPTER_SHA";\n'
        '      else /usr/bin/shasum -a 256 "$TEST_RECORDING/bililive_recorder_adapter.py"; fi\n'
        '    else exit 1; fi;;\n'
        '  esac\n'
        '  exit 0\n'
        'fi\n'
        'if [ "$1" = restart ]; then printf restart >> "$RESTART_LOG"; printf restarted > "$STARTED_AT_PATH"; exit 0; fi\n'
        'exit 1\n',
        encoding="utf-8",
    )
    for command in fake_bin.iterdir():
        command.chmod(0o755)

    replacements = (
        ("/root/clouddrive2/CloudNAS/CloudDrive/123云盘/live-streaming", "$TEST_CLOUD"),
        ("/root/clouddrive2/CloudNAS/CloudDrive", "$TEST_CLOUD_ROOT"),
        ("/opt/bilive/autoslice/repo", "$TEST_REPO"),
        ("/opt/bilive/autoslice/free_mount_watchdog.sh", "$TEST_WATCHDOG"),
        ("/opt/bilive/autoslice/upload_fatal_sentinel.sh", "$TEST_SENTINEL"),
        ("/opt/bilive/app/tmp_manual_upload/do_upload.sh", "$TEST_UPLOADER"),
        ("/opt/bilive/recording", "$TEST_RECORDING"),
    )
    for before, after in replacements:
        function_definitions = function_definitions.replace(before, after)
        route = route.replace(before, after)
    function_definitions = function_definitions.replace(
        "= '$TEST_RECORDING|bind|true'", '= "$TEST_RECORDING|bind|true"'
    )
    route = route.replace("= '$TEST_RECORDING|bind|true'", '= "$TEST_RECORDING|bind|true"')
    functions = cron_definitions + "\n" + function_definitions
    harness = (
        "set -euo pipefail\n"
        + functions
        + "\nbackup=$TEST_BACKUP\n"
        + "new_adapter_source=$TEST_REPO/ops/recording/bililive_recorder_adapter.py\n"
        + "host_adapter_path=$TEST_RECORDING/bililive_recorder_adapter.py\n"
        + "default_crontab=$(printf '%s\\n' \"$runner_cron\" \"$watchdog_cron\" \"$upload_fatal_cron\" \"$streamer_registry_cron\" \"$psplive_roster_cron\" \"$timely_terms_cron\" \"$community_names_cron\" \"$topic_entity_cron\" \"$streamer_dynamics_cron\")\n"
        + 'export CRONTAB_CONTENT="${TEST_CRONTAB_CONTENT-$default_crontab}"\n'
        + "adapter_restart_safe() { printf strict >> \"$STRICT_LOG\"; return 1; }\n"
        + "adapter_connection_stub_bootstrap_safe() { printf strict >> \"$STRICT_LOG\"; return 1; }\n"
        + "adapter_repair_restart_safe() { printf strict >> \"$STRICT_LOG\"; return 1; }\n"
        + route
    )

    def run(**extra: str) -> subprocess.CompletedProcess[str]:
        environment = os.environ.copy()
        environment.update(
            {
                "PATH": f"{fake_bin}:{environment['PATH']}",
                "TEST_REPO": str(repo),
                "TEST_BACKUP": str(backup),
                "TEST_RECORDING": str(recording),
                "TEST_CLOUD": str(cloud),
                "TEST_CLOUD_ROOT": str(cloud_root),
                "TEST_WATCHDOG": str(watchdog),
                "TEST_SENTINEL": str(sentinel),
                "TEST_UPLOADER": str(uploader),
                "EXPECTED_SHA": expected_sha,
                "RESTART_LOG": str(tmp_path / "restart.log"),
                "CRONTAB_READ_LOG": str(tmp_path / "crontab.read.log"),
                "CRONTAB_WRITE_LOG": str(tmp_path / "crontab.write.log"),
                "STRICT_LOG": str(tmp_path / "strict.log"),
                "STARTED_AT_PATH": str(tmp_path / "adapter.started-at"),
            }
        )
        environment.update(extra)
        return subprocess.run(
            ["/bin/bash", "-c", harness], capture_output=True, text=True, check=False, env=environment
        )

    started_at_path = tmp_path / "adapter.started-at"
    started_at_path.write_text("2026-08-21T00:00:00Z\n", encoding="utf-8")
    before = {
        path: (path.stat().st_ino, path.stat().st_mtime_ns, hashlib.sha256(path.read_bytes()).hexdigest())
        for path in originals
    }
    state_before = (
        adapter_state_path.stat().st_ino,
        adapter_state_path.stat().st_mtime_ns,
        hashlib.sha256(adapter_state_path.read_bytes()).hexdigest(),
    )
    started_before = started_at_path.read_bytes()
    accepted = run()
    assert accepted.returncode == 0, accepted.stderr
    assert before == {
        path: (path.stat().st_ino, path.stat().st_mtime_ns, hashlib.sha256(path.read_bytes()).hexdigest())
        for path in originals
    }
    assert state_before == (
        adapter_state_path.stat().st_ino,
        adapter_state_path.stat().st_mtime_ns,
        hashlib.sha256(adapter_state_path.read_bytes()).hexdigest(),
    )
    assert started_at_path.read_bytes() == started_before
    assert not (tmp_path / "restart.log").exists()
    assert not (tmp_path / "strict.log").exists()
    assert (tmp_path / "crontab.read.log").read_text(encoding="utf-8") == "read\nread\n"
    assert not (tmp_path / "crontab.write.log").exists()

    for _label, _staged, target, _staged_mode, target_mode in entries:
        payload, _ = originals[target]
        target.write_bytes(b"external drift\n")
        target.chmod(target_mode)
        assert run().returncode != 0
        target.write_bytes(payload)
        target.chmod(target_mode)
    adapter_target = recording / "bililive_recorder_adapter.py"
    adapter_target.chmod(0o700)
    assert adapter_target.stat().st_mode & 0o777 == 0o700
    mode_rejected = run()
    assert mode_rejected.returncode != 0, mode_rejected.stderr
    adapter_target.chmod(0o755)
    adapter_target.unlink()
    adapter_target.symlink_to(repo / "ops/recording/bililive_recorder_adapter.py")
    assert run().returncode != 0
    adapter_target.unlink()
    adapter_target.write_bytes(originals[adapter_target][0])
    adapter_target.chmod(0o755)
    adapter_source = repo / "ops/recording/bililive_recorder_adapter.py"
    for source_mode in (0o755, 0o600):
        adapter_source.chmod(source_mode)
        assert run().returncode != 0
    adapter_source.chmod(0o644)
    assert run(DOCKER_ADAPTER_SHA="0" * 64).returncode != 0
    assert run(DOCKER_STATE="exited").returncode != 0
    assert run(DOCKER_HEALTH="unhealthy").returncode != 0
    assert run(DOCKER_CMD="python3|/state/other.py").returncode != 0
    assert run(DOCKER_BIND=f"{recording}|bind|false").returncode != 0

    for mode in ("missing", "duplicate", "stale"):
        (tmp_path / "crontab.read.log").unlink(missing_ok=True)
        rejected = run(CRONTAB_MODE=mode)
        assert rejected.returncode != 0
        assert (tmp_path / "strict.log").exists()
        (tmp_path / "strict.log").unlink()
    (tmp_path / "crontab.read.log").unlink(missing_ok=True)
    assert run(CRONTAB_MODE="post-stale").returncode != 0
    (tmp_path / "crontab.read.log").unlink(missing_ok=True)
    assert run(CRON_DRIFT_TARGET=str(watchdog)).returncode != 0
    watchdog.write_bytes(originals[watchdog][0])
    watchdog.chmod(0o755)

    status_path = recording / "status.json"
    (tmp_path / "crontab.read.log").unlink(missing_ok=True)
    assert run(CRON_DRIFT_STATUS=str(status_path)).returncode != 0
    status_path.write_text(
        json.dumps(
            {
                "generated_at_epoch": time.time(),
                "service_reachable": True,
                "streaming": True,
                "recording": True,
                "finalizing": False,
                "error": None,
            }
        ),
        encoding="utf-8",
    )
    assert run().returncode == 0
    clean_status = json.loads(status_path.read_text(encoding="utf-8"))
    for missing in ("service_reachable", "error"):
        payload = dict(clean_status)
        payload.pop(missing)
        status_path.write_text(json.dumps(payload), encoding="utf-8")
        assert run().returncode != 0
    for update in (
        {"service_reachable": 1},
        {"error": {}},
        {"error": []},
        {"generated_at_epoch": now - 91},
        {"streaming": 1},
    ):
        payload = dict(clean_status)
        payload.update(update)
        status_path.write_text(json.dumps(payload), encoding="utf-8")
        assert run().returncode != 0
    marker = outer_rollback.index("external_mutation_started=0")
    assert marker < outer_rollback.index("restore_file watchdog")
    assert 'if [ "$external_mutation_started" -eq 1 ]; then\n    restore_file watchdog' in outer_rollback
    assert 'if [ "$external_mutation_started" -eq 1 ] && [ -f "$backup/external/crontab.present" ]' in outer_rollback
    assert 'if [ "$external_mutation_started" -eq 1 ] && [ -f "$backup/external/recorder_adapter.restart-required" ]' in outer_rollback
    assert 'if [ "$external_payload_unchanged" -eq 0 ]; then\n    printf' in external
    assert "adapter_status_zero_touch_fresh" in external
    zero_touch_status = _embedded_shell_function(external, "adapter_status_zero_touch_fresh")
    assert 'payload.get("service_reachable") is True' not in zero_touch_status
    assert 'payload.get("error") is None' not in zero_touch_status
    assert 'type(payload.get("service_reachable")) is bool' in zero_touch_status
    assert '"error" in payload and (payload["error"] is None or type(payload["error"]) is str)' in zero_touch_status
    assert external.index("watchdog_cron=") < external.index("external_payload_exact() {")
    assert '"$new_adapter_source" "$host_adapter_path" 644 755' in external
    assert "managed_crontab_exact()" in external
    assert 'if [ "$external_payload_unchanged" -eq 0 ]; then\nexisting_crontab=' in external
    assert 'if [ "$external_payload_unchanged" -eq 1 ]; then\n    # Close the read-only fast-route interval' in external
    switch = source.split("<<'REMOTE_SWITCH'\n", 1)[1].split("\nREMOTE_SWITCH", 1)[0]
    switch_rollback = _embedded_shell_function(switch, "rollback")
    # The repository switch happens before REMOTE_EXTERNAL_INSTALL.  Its
    # rollback must not replay a preimage over a concurrently-live recorder.
    assert "External targets,\n    # cron, and recorder state" in switch_rollback
    assert "restore_file watchdog" not in switch_rollback
    assert "restore_connection_stub_bootstrap_preimage" not in switch_rollback
    assert "crontab \"$backup/external/crontab.file\"" not in switch_rollback


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


def test_deploy_rollback_connection_stub_receipt_binds_preimages_and_status(tmp_path):
    now = time.time()
    paths = [
        "2026-08-20/22966160_20260820-21-00-20.flv",
        "2026-08-20/22966160_20260820-21-56-14.flv",
    ]
    sources = [f"/adapter/Videos/22966160/{path}" for path in paths]
    payload = {
        "generated_at_epoch": now,
        "service_reachable": True,
        "streaming": False,
        "recording": False,
        "finalizing": False,
        "error": "2 closed recording(s) failed finalization",
        "finalize_errors": [{"source": source} for source in sources],
    }
    state_preimage = tmp_path / "adapter-state.json"
    state_preimage.write_text('{"schema_version":"recording-adapter-state.v1"}\n', encoding="utf-8")
    status_preimage = tmp_path / "status-preimage.json"
    status_preimage.write_text(json.dumps(payload), encoding="utf-8")
    receipt_path = tmp_path / ("a" * 40 + ".json")
    marker_path = tmp_path / "connection-stub-bootstrap.marker"

    def write_receipt() -> None:
        projection = {
            key: payload.get(key)
            for key in (
                "service_reachable",
                "streaming",
                "recording",
                "finalizing",
                "error",
                "finalize_errors",
            )
        }
        receipt = {
            "schema_version": "recording-connection-stub-bootstrap.v1",
            "receipt_id": receipt_path.stem,
            "adapter_state_sha256": hashlib.sha256(state_preimage.read_bytes()).hexdigest(),
            "adapter_status_preimage_sha256": hashlib.sha256(
                json.dumps(projection, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest(),
            "source_relative_paths": paths,
            "rows": {path: {"source_relative_path": path} for path in paths},
        }
        receipt["canonical_integrity"] = {
            "algorithm": "sha256",
            "canonical_json_sha256": hashlib.sha256(
                json.dumps(receipt, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest(),
        }
        receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
        marker = {
            "schema_version": "recording-connection-stub-bootstrap-rollback-marker.v1",
            "receipt_id": receipt_path.stem,
            "receipt_path": str(receipt_path),
            "receipt_sha256": hashlib.sha256(receipt_path.read_bytes()).hexdigest(),
            "state_sha256": hashlib.sha256(state_preimage.read_bytes()).hexdigest(),
            "status_sha256": hashlib.sha256(status_preimage.read_bytes()).hexdigest(),
            "status_projection_sha256": receipt["adapter_status_preimage_sha256"],
        }
        marker["canonical_integrity"] = {
            "algorithm": "sha256",
            "canonical_json_sha256": hashlib.sha256(
                json.dumps(marker, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest(),
        }
        marker_path.write_text(json.dumps(marker), encoding="utf-8")

    write_receipt()
    accepted = _run_embedded_status_validator(
        "PY_ROLLBACK_FRESH",
        tmp_path,
        payload,
        str(now - 1),
        "0",
        str(receipt_path),
        str(marker_path),
        str(state_preimage),
        str(status_preimage),
    )
    assert accepted.returncode == 0, accepted.stderr

    receipt_path.write_text("{}", encoding="utf-8")
    assert _run_embedded_status_validator(
        "PY_ROLLBACK_FRESH", tmp_path, payload, str(now - 1), "0", str(receipt_path), str(marker_path), str(state_preimage), str(status_preimage)
    ).returncode != 0
    write_receipt()

    state_preimage.write_text("changed", encoding="utf-8")
    assert _run_embedded_status_validator(
        "PY_ROLLBACK_FRESH", tmp_path, payload, str(now - 1), "0", str(receipt_path), str(marker_path), str(state_preimage), str(status_preimage)
    ).returncode != 0
    state_preimage.write_text('{"schema_version":"recording-adapter-state.v1"}\n', encoding="utf-8")
    write_receipt()

    changed_status = dict(payload)
    changed_status["finalize_errors"] = []
    status_preimage.write_text(json.dumps(changed_status), encoding="utf-8")
    assert _run_embedded_status_validator(
        "PY_ROLLBACK_FRESH", tmp_path, payload, str(now - 1), "0", str(receipt_path), str(marker_path), str(state_preimage), str(status_preimage)
    ).returncode != 0
    status_preimage.write_text(json.dumps(payload), encoding="utf-8")
    write_receipt()

    changed_runtime = dict(payload)
    changed_runtime["error"] = "other finalization error"
    assert _run_embedded_status_validator(
        "PY_ROLLBACK_FRESH", tmp_path, changed_runtime, str(now - 1), "0", str(receipt_path), str(marker_path), str(state_preimage), str(status_preimage)
    ).returncode != 0


def test_bootstrap_rollback_marker_is_the_only_state_restore_authority(tmp_path):
    source = DEPLOY_SCRIPT.read_text(encoding="utf-8")
    rollback = source.split("<<'REMOTE_ROLLBACK'\n", 1)[1].split("\nREMOTE_ROLLBACK", 1)[0]
    restore_start = rollback.index("restore_connection_stub_bootstrap_preimage() {")
    restore_end = rollback.index(
        '\nif [ "$external_mutation_started" -eq 1 ]; then\n    restore_connection_stub_bootstrap_preimage',
        restore_start,
    )
    restore = rollback[restore_start:restore_end]
    recording = tmp_path / "recording"
    backup = tmp_path / "backup"
    receipt_id = "b" * 40
    recording.mkdir()
    (recording / "connection-stub-bootstrap-receipts").mkdir()
    preimage = backup / "external/connection_stub_bootstrap_preimage"
    preimage.mkdir(parents=True)
    live_state = recording / "adapter-state.json"
    live_status = recording / "status.json"
    state_preimage = preimage / "adapter-state.json"
    status_preimage = preimage / "status.json"
    marker = backup / "external/connection_stub_bootstrap.marker"
    receipt = recording / f"connection-stub-bootstrap-receipts/{receipt_id}.json"
    paths = [
        "2026-08-20/22966160_20260820-21-00-20.flv",
        "2026-08-20/22966160_20260820-21-56-14.flv",
    ]
    status_payload = {
        "generated_at": "2026-08-20T19:08:54+00:00",
        "generated_at_epoch": 1.0,
        "bilibili_cookie": {
            "checked_at": "2026-08-20T19:08:54+00:00",
            "checked_at_epoch": 1.0,
            "authenticated": True,
        },
        "service_reachable": True,
        "streaming": False,
        "recording": False,
        "finalizing": False,
        "error": "2 closed recording(s) failed finalization",
        "finalize_errors": [
            {"source": f"/adapter/Videos/22966160/{path}"} for path in paths
        ],
    }

    def write_bound_preimage() -> None:
        state_preimage.write_bytes(b"bound-state\n")
        status_preimage.write_text(json.dumps(status_payload), encoding="utf-8")
        projection = _bootstrap_status_projection(status_payload)
        receipt_payload = {
            "schema_version": "recording-connection-stub-bootstrap.v1",
            "receipt_id": receipt_id,
            "adapter_state_sha256": hashlib.sha256(state_preimage.read_bytes()).hexdigest(),
            "adapter_status_preimage_sha256": hashlib.sha256(
                json.dumps(projection, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest(),
            "source_relative_paths": paths,
        }
        receipt_payload["canonical_integrity"] = {
            "algorithm": "sha256",
            "canonical_json_sha256": hashlib.sha256(
                json.dumps(
                    receipt_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                ).encode()
            ).hexdigest(),
        }
        receipt.write_text(json.dumps(receipt_payload), encoding="utf-8")
        marker_payload = {
            "schema_version": "recording-connection-stub-bootstrap-rollback-marker.v1",
            "receipt_id": receipt_id,
            "receipt_path": str(receipt),
            "receipt_sha256": hashlib.sha256(receipt.read_bytes()).hexdigest(),
            "state_sha256": hashlib.sha256(state_preimage.read_bytes()).hexdigest(),
            "status_sha256": hashlib.sha256(status_preimage.read_bytes()).hexdigest(),
            "status_projection_sha256": receipt_payload["adapter_status_preimage_sha256"],
        }
        marker_payload["canonical_integrity"] = {
            "algorithm": "sha256",
            "canonical_json_sha256": hashlib.sha256(
                json.dumps(
                    marker_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                ).encode()
            ).hexdigest(),
        }
        marker.write_text(json.dumps(marker_payload), encoding="utf-8")

    script = "\n".join(
        (
            "set -eu",
            "backup=$1",
            "new_commit=$2",
            restore.replace("/opt/bilive/recording", str(recording)),
            "restore_connection_stub_bootstrap_preimage",
        )
    )

    def run_restore() -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", "-c", script, "bootstrap-rollback", str(backup), receipt_id],
            capture_output=True,
            text=True,
            check=False,
        )

    live_state.write_bytes(b"intervening-state\n")
    live_status.write_bytes(b"intervening-status\n")
    normal = run_restore()
    assert normal.returncode == 0, normal.stderr
    assert live_state.read_bytes() == b"intervening-state\n"
    assert live_status.read_bytes() == b"intervening-status\n"

    write_bound_preimage()
    restored = run_restore()
    assert restored.returncode == 0, restored.stderr
    assert live_state.read_bytes() == state_preimage.read_bytes()
    assert live_status.read_bytes() == status_preimage.read_bytes()

    live_state.write_bytes(b"must-not-rewind\n")
    live_status.write_bytes(b"must-not-rewind\n")
    marker.write_text("{}", encoding="utf-8")
    assert run_restore().returncode != 0
    assert live_state.read_bytes() == b"must-not-rewind\n"
    assert live_status.read_bytes() == b"must-not-rewind\n"

    write_bound_preimage()
    state_preimage.unlink()
    assert run_restore().returncode != 0

    write_bound_preimage()
    marker_target = tmp_path / "marker-target.json"
    marker_target.write_bytes(marker.read_bytes())
    marker.unlink()
    marker.symlink_to(marker_target)
    assert run_restore().returncode != 0


def test_connection_stub_bootstrap_preinstall_refuses_preimage_drift(tmp_path):
    paths = [
        "2026-08-20/22966160_20260820-21-00-20.flv",
        "2026-08-20/22966160_20260820-21-56-14.flv",
    ]
    state_path = tmp_path / "adapter-state.json"
    state_path.write_bytes(b"captured-state\n")
    status_path = tmp_path / "status.json"
    status = {
        "service_reachable": True,
        "streaming": False,
        "recording": False,
        "finalizing": False,
        "error": "2 closed recording(s) failed finalization",
        "finalize_errors": [
            {"source": f"/adapter/Videos/22966160/{path}"} for path in paths
        ],
    }
    status_path.write_text(json.dumps(status), encoding="utf-8")
    projection = {
        key: status.get(key)
        for key in (
            "service_reachable",
            "streaming",
            "recording",
            "finalizing",
            "error",
            "finalize_errors",
        )
    }
    receipt_path = tmp_path / "receipt.json"
    receipt = {
        "schema_version": "recording-connection-stub-bootstrap.v1",
        "receipt_id": "a" * 40,
        "candidate_adapter_sha256": "b" * 64,
        "installed_adapter_sha256": "c" * 64,
        "adapter_state_sha256": "0" * 64,
        "adapter_status_preimage_sha256": hashlib.sha256(
            json.dumps(projection, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
        "source_relative_paths": paths,
        "rows": {path: {"source_relative_path": path} for path in paths},
    }
    receipt["canonical_integrity"] = {
        "algorithm": "sha256",
        "canonical_json_sha256": hashlib.sha256(
            json.dumps(receipt, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
    }
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            _embedded_deploy_python("PY_BOOTSTRAP_PREIMAGE"),
            str(state_path),
            str(status_path),
            str(receipt_path),
            "b" * 64,
            "c" * 64,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0


def test_bootstrap_marker_activation_refuses_live_drift_without_mutation(tmp_path):
    source = DEPLOY_SCRIPT.read_text(encoding="utf-8")
    external = source.split("<<'REMOTE_EXTERNAL_INSTALL'\n", 1)[1].split(
        "\nREMOTE_EXTERNAL_INSTALL", 1
    )[0]
    start = external.index("activate_connection_stub_bootstrap_marker() {")
    end = external.index("\nadapter_connection_stub_bootstrap_safe", start)
    activate = external[start:end]
    recording = tmp_path / "recording"
    backup = tmp_path / "backup"
    receipt_id = "d" * 40
    recording.mkdir()
    receipts = recording / "connection-stub-bootstrap-receipts"
    receipts.mkdir()
    preimage = backup / "external/connection_stub_bootstrap_preimage"
    preimage.mkdir(parents=True)
    state_preimage = preimage / "adapter-state.json"
    status_preimage = preimage / "status.json"
    state_preimage.write_bytes(b"captured-state\n")
    status = {
        "service_reachable": True,
        "streaming": False,
        "recording": False,
        "finalizing": False,
        "error": "2 closed recording(s) failed finalization",
        "finalize_errors": [
            {"source": "/adapter/Videos/22966160/2026-08-20/22966160_20260820-21-00-20.flv"},
            {"source": "/adapter/Videos/22966160/2026-08-20/22966160_20260820-21-56-14.flv"},
        ],
    }
    status_preimage.write_text(json.dumps(status), encoding="utf-8")
    projection = {
        key: status.get(key)
        for key in (
            "service_reachable",
            "streaming",
            "recording",
            "finalizing",
            "error",
            "finalize_errors",
        )
    }
    receipt = receipts / f"{receipt_id}.json"
    receipt.write_text(
        json.dumps(
            {
                "schema_version": "recording-connection-stub-bootstrap.v1",
                "receipt_id": receipt_id,
                "adapter_state_sha256": hashlib.sha256(state_preimage.read_bytes()).hexdigest(),
                "adapter_status_preimage_sha256": hashlib.sha256(
                    json.dumps(projection, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
                ).hexdigest(),
                "source_relative_paths": [
                    "2026-08-20/22966160_20260820-21-00-20.flv",
                    "2026-08-20/22966160_20260820-21-56-14.flv",
                ],
            }
        ),
        encoding="utf-8",
    )
    (recording / "adapter-state.json").write_bytes(b"old-daemon-wrote-state\n")
    (recording / "status.json").write_bytes(b"old-daemon-wrote-status\n")
    script = "\n".join(
        (
            "set -eu",
            "backup=$1",
            "commit=$2",
            activate.replace("/opt/bilive/recording", str(recording)),
            "activate_connection_stub_bootstrap_marker",
        )
    )
    result = subprocess.run(
        ["bash", "-c", script, "bootstrap-activate", str(backup), receipt_id],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert not (backup / "external/connection_stub_bootstrap.marker").exists()
    assert (recording / "adapter-state.json").read_bytes() == b"old-daemon-wrote-state\n"
    assert (recording / "status.json").read_bytes() == b"old-daemon-wrote-status\n"


def test_bootstrap_marker_activation_accepts_only_known_heartbeats(tmp_path):
    source = DEPLOY_SCRIPT.read_text(encoding="utf-8")
    external = source.split("<<'REMOTE_EXTERNAL_INSTALL'\n", 1)[1].split(
        "\nREMOTE_EXTERNAL_INSTALL", 1
    )[0]
    start = external.index("activate_connection_stub_bootstrap_marker() {")
    end = external.index("\nadapter_connection_stub_bootstrap_safe", start)
    activate = external[start:end]
    recording = tmp_path / "recording"
    backup = tmp_path / "backup"
    receipt_id = "e" * 40
    recording.mkdir()
    (recording / "connection-stub-bootstrap-receipts").mkdir()
    preimage = backup / "external/connection_stub_bootstrap_preimage"
    preimage.mkdir(parents=True)
    paths = [
        "2026-08-20/22966160_20260820-21-00-20.flv",
        "2026-08-20/22966160_20260820-21-56-14.flv",
    ]
    pre_state: dict[str, object] = {
        "schema_version": "recording-adapter-state.v1",
        "last_room_status_epoch": 1.0,
        "cookie_health": {
            "checked_at": "2026-08-20T19:08:54+00:00",
            "checked_at_epoch": 1.0,
            "authenticated": True,
        },
        "webhook_files": {},
        "finalized": {},
        "source_dispositions": {},
    }
    pre_status: dict[str, object] = {
        "generated_at": "2026-08-20T19:08:54+00:00",
        "generated_at_epoch": 1.0,
        "backend": "bililive-recorder",
        "bilibili_cookie": {
            "checked_at": "2026-08-20T19:08:54+00:00",
            "checked_at_epoch": 1.0,
            "authenticated": True,
        },
        "service_reachable": True,
        "streaming": False,
        "recording": False,
        "finalizing": False,
        "error": "2 closed recording(s) failed finalization",
        "finalize_errors": [
            {
                "source": f"/adapter/Videos/22966160/{path}",
                "error": "ffmpeg failed at 0x123abc",
            }
            for path in paths
        ],
    }
    state_preimage = preimage / "adapter-state.json"
    status_preimage = preimage / "status.json"
    state_preimage.write_text(json.dumps(pre_state), encoding="utf-8")
    status_preimage.write_text(json.dumps(pre_status), encoding="utf-8")
    receipt_payload: dict[str, object] = {
        "schema_version": "recording-connection-stub-bootstrap.v1",
        "receipt_id": receipt_id,
        "adapter_state_sha256": hashlib.sha256(state_preimage.read_bytes()).hexdigest(),
        "adapter_state_material_sha256": _canonical_sha256(_bootstrap_state_material(pre_state)),
        "adapter_status_preimage_sha256": _canonical_sha256(
            _bootstrap_status_projection(pre_status)
        ),
        "source_relative_paths": paths,
        "rows": {path: {"source_relative_path": path} for path in paths},
    }
    receipt_payload["canonical_integrity"] = {
        "algorithm": "sha256",
        "canonical_json_sha256": _canonical_sha256(receipt_payload),
    }
    (recording / f"connection-stub-bootstrap-receipts/{receipt_id}.json").write_text(
        json.dumps(receipt_payload), encoding="utf-8"
    )
    script = "\n".join(
        (
            "set -eu",
            "backup=$1",
            "commit=$2",
            activate.replace("/opt/bilive/recording", str(recording)),
            "activate_connection_stub_bootstrap_marker",
        )
    )

    def activate_with(live_state: dict[str, object], live_status: dict[str, object]):
        (recording / "adapter-state.json").write_text(json.dumps(live_state), encoding="utf-8")
        (recording / "status.json").write_text(json.dumps(live_status), encoding="utf-8")
        return subprocess.run(
            ["bash", "-c", script, "bootstrap-activate", str(backup), receipt_id],
            capture_output=True,
            text=True,
            check=False,
        )

    heartbeat_state = dict(
        pre_state,
        last_room_status_epoch=2.0,
        cookie_health={
            "checked_at": "2026-08-20T19:16:54+00:00",
            "checked_at_epoch": 2.0,
            "authenticated": True,
        },
    )
    heartbeat_status = dict(
        pre_status,
        generated_at="2026-08-20T19:16:54+00:00",
        generated_at_epoch=2.0,
        bilibili_cookie={
            "checked_at": "2026-08-20T19:16:54+00:00",
            "checked_at_epoch": 2.0,
            "authenticated": True,
        },
    )
    heartbeat_status["finalize_errors"] = [
        dict(entry, error="ffmpeg failed at 0xfeed42")
        for entry in pre_status["finalize_errors"]  # type: ignore[index]
    ]
    accepted = activate_with(heartbeat_state, heartbeat_status)
    assert accepted.returncode == 0, accepted.stderr
    marker = backup / "external/connection_stub_bootstrap.marker"
    assert marker.is_file()

    for name, state, status in (
        (
            "state-disposition",
            dict(heartbeat_state, source_dispositions={"other": {}}),
            heartbeat_status,
        ),
        (
            "source-set",
            heartbeat_state,
            dict(
                heartbeat_status,
                finalize_errors=[dict(heartbeat_status["finalize_errors"][0])],  # type: ignore[index]
            ),
        ),
        (
            "error-count",
            heartbeat_state,
            dict(heartbeat_status, error="1 closed recording(s) failed finalization"),
        ),
        (
            "semantic-error",
            heartbeat_state,
            dict(
                heartbeat_status,
                finalize_errors=[
                    dict(entry, error="ffmpeg different codec failure at 0xfeed42")
                    for entry in heartbeat_status["finalize_errors"]  # type: ignore[index]
                ],
            ),
        ),
        (
            "top-level-drift",
            heartbeat_state,
            dict(heartbeat_status, backend="other-backend"),
        ),
        (
            "cookie-status-drift",
            heartbeat_state,
            dict(
                heartbeat_status,
                bilibili_cookie={
                    "checked_at": "2026-08-20T19:16:54+00:00",
                    "checked_at_epoch": 2.0,
                    "authenticated": False,
                },
            ),
        ),
        (
            "cookie-health-drift",
            dict(
                heartbeat_state,
                cookie_health={
                    "checked_at": "2026-08-20T19:16:54+00:00",
                    "checked_at_epoch": 2.0,
                    "authenticated": False,
                },
            ),
            heartbeat_status,
        ),
    ):
        marker.unlink(missing_ok=True)
        result = activate_with(state, status)
        assert result.returncode != 0, (name, result.stderr)
        assert not marker.exists(), name


def test_exact_pre_marker_guard_recovery_is_heartbeat_tolerant_and_fail_closed(tmp_path):
    source = DEPLOY_SCRIPT.read_text(encoding="utf-8")
    recovery = source.split("<<'REMOTE_GUARD_RECOVERY'\n", 1)[1].split(
        "\nREMOTE_GUARD_RECOVERY", 1
    )[0]
    commit = "a" * 40
    owner = f"{commit}-20260820T190925Z-86811"

    rollback = source.split("<<'REMOTE_ROLLBACK'\n", 1)[1].split("\nREMOTE_ROLLBACK", 1)[0]
    verifier_start = rollback.index("connection_stub_bootstrap_pre_marker_safe() {")
    verifier_end = rollback.index(
        '\nif [ "$external_mutation_started" -eq 1 ] && [ -f "$backup/external/crontab.present" ]',
        verifier_start,
    )
    outer_verifier = rollback[verifier_start:verifier_end]

    def build(
        root: Path,
        *,
        semantic_drift: bool,
        top_level_drift: bool = False,
        cookie_status_drift: bool = False,
        cookie_health_drift: bool = False,
    ):
        base = root / "autoslice"
        recording = root / "recording"
        uploader = root / "uploader"
        repo = base / "repo"
        backup = base / f"repo.rollback-{commit}"
        stage = base / f"repo.deploy-{commit}"
        guard = base / "deploy.guard"
        receipts = recording / "connection-stub-bootstrap-receipts"
        repo.mkdir(parents=True)
        recording.mkdir()
        receipts.mkdir()
        stage.mkdir(parents=True)
        guard.mkdir()
        (guard / "owner").write_text(owner + "\n", encoding="utf-8")
        (repo / "DEPLOYED_COMMIT").write_text("old\n", encoding="utf-8")
        backup.mkdir()
        (backup / "DEPLOYED_COMMIT.old").write_text("old\n", encoding="utf-8")
        (backup / "repo.manifest.old.json").write_text("{}", encoding="utf-8")
        external = backup / "external"
        external.mkdir()
        targets = {
            "watchdog": base / "free_mount_watchdog.sh",
            "upload_sentinel": base / "upload_fatal_sentinel.sh",
            "uploader": uploader,
            "recorder_adapter": recording / "bililive_recorder_adapter.py",
        }
        for label, target in targets.items():
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(f"{label}-old\n".encode())
            (external / f"{label}.file").write_bytes(target.read_bytes())
            (external / f"{label}.present").touch()
        (external / "recorder_adapter.restart-required").touch()
        preimage = external / "connection_stub_bootstrap_preimage"
        preimage.mkdir()
        paths = [
            "2026-08-20/22966160_20260820-21-00-20.flv",
            "2026-08-20/22966160_20260820-21-56-14.flv",
        ]
        pre_state: dict[str, object] = {
            "schema_version": "recording-adapter-state.v1",
            "last_room_status_epoch": 1.0,
            "cookie_health": {
                "checked_at": "2026-08-20T19:08:54+00:00",
                "checked_at_epoch": 1.0,
                "authenticated": True,
            },
            "webhook_files": {},
            "finalized": {},
            "source_dispositions": {},
        }
        pre_status: dict[str, object] = {
            "generated_at": "2026-08-20T19:08:54+00:00",
            "generated_at_epoch": 1.0,
            "backend": "bililive-recorder",
            "bilibili_cookie": {
                "checked_at": "2026-08-20T19:08:54+00:00",
                "checked_at_epoch": 1.0,
                "authenticated": True,
            },
            "service_reachable": True,
            "streaming": False,
            "recording": False,
            "finalizing": False,
            "error": "2 closed recording(s) failed finalization",
            "finalize_errors": [
                {
                    "source": f"/adapter/Videos/22966160/{path}",
                    "error": "ffmpeg failed at 0x123abc",
                }
                for path in paths
            ],
        }
        state_path = preimage / "adapter-state.json"
        status_path = preimage / "status.json"
        state_path.write_text(json.dumps(pre_state), encoding="utf-8")
        status_path.write_text(json.dumps(pre_status), encoding="utf-8")
        receipt: dict[str, object] = {
            "schema_version": "recording-connection-stub-bootstrap.v1",
            "receipt_id": commit,
            "adapter_state_sha256": hashlib.sha256(state_path.read_bytes()).hexdigest(),
            # The retained remote incident was created by 3f0f622, before
            # durable receipt projections; recovery must accept that exact
            # six-field receipt while separately comparing live/preimage
            # durable projections.
            "adapter_status_preimage_sha256": _canonical_sha256(
                {
                    key: pre_status.get(key)
                    for key in (
                        "service_reachable",
                        "streaming",
                        "recording",
                        "finalizing",
                        "error",
                        "finalize_errors",
                    )
                }
            ),
            "source_relative_paths": paths,
            "rows": {path: {"source_relative_path": path} for path in paths},
        }
        receipt["canonical_integrity"] = {
            "algorithm": "sha256",
            "canonical_json_sha256": _canonical_sha256(receipt),
        }
        (receipts / f"{commit}.json").write_text(json.dumps(receipt), encoding="utf-8")
        live_state = dict(
            pre_state,
            last_room_status_epoch=2.0,
            cookie_health={
                "checked_at": "2026-08-20T19:16:54+00:00",
                "checked_at_epoch": 2.0,
                "authenticated": True,
            },
        )
        live_status = dict(
            pre_status,
            generated_at="2026-08-20T19:16:54+00:00",
            generated_at_epoch=2.0,
            bilibili_cookie={
                "checked_at": "2026-08-20T19:16:54+00:00",
                "checked_at_epoch": 2.0,
                "authenticated": True,
            },
        )
        live_status["finalize_errors"] = [
            dict(entry, error="ffmpeg failed at 0xfeed42")
            for entry in pre_status["finalize_errors"]  # type: ignore[index]
        ]
        if semantic_drift:
            live_status["error"] = "1 closed recording(s) failed finalization"
        if top_level_drift:
            live_status["backend"] = "other-backend"
        if cookie_status_drift:
            live_status["bilibili_cookie"] = dict(
                live_status["bilibili_cookie"], authenticated=False  # type: ignore[arg-type,index]
            )
        if cookie_health_drift:
            live_state["cookie_health"] = dict(
                live_state["cookie_health"], authenticated=False  # type: ignore[arg-type,index]
            )
        (recording / "adapter-state.json").write_text(json.dumps(live_state), encoding="utf-8")
        (recording / "status.json").write_text(json.dumps(live_status), encoding="utf-8")
        verifier = subprocess.run(
            [
                "bash",
                "-c",
                "\n".join(
                    (
                        "set -eu",
                        "backup=$1",
                        "new_commit=$2",
                        outer_verifier.replace("/opt/bilive/recording", str(recording)),
                        "connection_stub_bootstrap_pre_marker_safe",
                    )
                ),
                "outer-pre-marker",
                str(backup),
                commit,
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        script = recovery.replace("/opt/bilive/autoslice", str(base)).replace(
            "/opt/bilive/recording", str(recording)
        ).replace("/opt/bilive/app/tmp_manual_upload/do_upload.sh", str(uploader))
        result = subprocess.run(
            ["bash", "-c", script, "guard-recovery", str(base), owner],
            capture_output=True,
            text=True,
            check=False,
        )
        return result, guard, verifier

    accepted, guard, outer = build(tmp_path / "accepted", semantic_drift=False)
    assert outer.returncode == 0, outer.stderr
    assert accepted.returncode == 0, accepted.stderr
    assert not guard.exists()
    assert not (tmp_path / "accepted" / "autoslice" / f"repo.rollback-{commit}").exists()

    rejected, guard, outer = build(tmp_path / "drift", semantic_drift=True)
    assert outer.returncode != 0
    assert rejected.returncode != 0
    assert guard.is_dir()

    rejected, guard, outer = build(
        tmp_path / "cookie-status-drift", semantic_drift=False, cookie_status_drift=True
    )
    assert outer.returncode != 0
    assert rejected.returncode != 0
    assert guard.is_dir()

    rejected, guard, outer = build(
        tmp_path / "cookie-health-drift", semantic_drift=False, cookie_health_drift=True
    )
    assert outer.returncode != 0
    assert rejected.returncode != 0
    assert guard.is_dir()
    assert (guard / "owner").read_text(encoding="utf-8").strip() == owner

    rejected, guard, outer = build(
        tmp_path / "top-level-drift", semantic_drift=False, top_level_drift=True
    )
    assert outer.returncode != 0
    assert rejected.returncode != 0
    assert guard.is_dir()


def test_prebackup_stage_guard_recovery_requires_bound_stage_and_old_authority(tmp_path):
    source = DEPLOY_SCRIPT.read_text(encoding="utf-8")
    receiver = source.split("<<'REMOTE_PREBACKUP_GUARD_RECOVERY'\n", 1)[1].split(
        "\nREMOTE_PREBACKUP_GUARD_RECOVERY", 1
    )[0]
    owner_commit = "c" * 40
    old_commit = "d" * 40
    owner = f"{owner_commit}-20260820T200852Z-16337"

    def stage_tree_sha(stage: Path) -> str:
        entries: dict[str, dict[str, object]] = {
            "": {"type": "dir", "mode": stage.stat().st_mode & 0o777}
        }
        for path in sorted(stage.rglob("*")):
            relative = path.relative_to(stage).as_posix()
            mode = path.lstat().st_mode & 0o777
            if path.is_dir():
                entries[relative] = {"type": "dir", "mode": mode}
            else:
                entries[relative] = {
                    "type": "file",
                    "mode": mode,
                    "size": path.stat().st_size,
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                }
        return _canonical_sha256(entries)

    def build(root: Path, *, external_drift: bool = False, backup: bool = False):
        base = root / "autoslice"
        repo = base / "repo"
        stage = base / f"repo.deploy-{owner_commit}"
        guard = base / "deploy.guard"
        recording = root / "recording"
        uploader = root / "uploader"
        repo.mkdir(parents=True)
        stage.mkdir()
        stage.chmod(0o755)
        (stage / ".agent").mkdir(mode=0o700)
        (stage / ".agent/partial.txt").write_bytes(b"partial")
        (stage / ".agent/partial.txt").chmod(0o600)
        guard.mkdir()
        (guard / "owner").write_text(owner + "\n", encoding="utf-8")
        (repo / "DEPLOYED_COMMIT").write_text(old_commit + " deployed\n", encoding="utf-8")
        registry = repo / "assets/lidousha/publication_registry.v1.json"
        registry.parent.mkdir(parents=True)
        registry.write_text("{}", encoding="utf-8")
        module = repo / "src/autoslice/repository_asset_authority.py"
        module.parent.mkdir(parents=True)
        (repo / "src/__init__.py").touch()
        (repo / "src/autoslice/__init__.py").touch()
        module.write_text(
            "def build_deployed_authority_manifest(*, repo_root, deployed_commit, relative_paths):\n"
            "    return {'commit': deployed_commit, 'paths': sorted(path.as_posix() for path in relative_paths)}\n",
            encoding="utf-8",
        )
        (repo / "DEPLOYED_AUTHORITY_MANIFEST.json").write_text(
            json.dumps(
                {
                    "commit": old_commit,
                    "paths": [
                        "assets/lidousha/publication_registry.v1.json",
                        "assets/lidousha/publication_registry.v1.json",
                    ],
                }
            ),
            encoding="utf-8",
        )
        targets = {
            "scripts/free_mount_watchdog.sh": base / "free_mount_watchdog.sh",
            "scripts/clouddrive_upload_fatal_sentinel.sh": base / "upload_fatal_sentinel.sh",
            "scripts/free_do_upload.sh": uploader,
            "ops/recording/bililive_recorder_adapter.py": recording / "bililive_recorder_adapter.py",
        }
        for relative, destination in targets.items():
            source_path = repo / relative
            source_path.parent.mkdir(parents=True, exist_ok=True)
            source_path.write_bytes(relative.encode())
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(b"drift" if external_drift and "adapter" in relative else relative.encode())
        if backup:
            (base / f"repo.rollback-{owner_commit}").mkdir()
        transformed = receiver.replace("/opt/bilive/autoslice", str(base)).replace(
            "/opt/bilive/recording", str(recording)
        ).replace("/opt/bilive/app/tmp_manual_upload/do_upload.sh", str(uploader))
        result = subprocess.run(
            ["bash", "-c", transformed, "prebackup-recovery", str(base), owner, stage_tree_sha(stage)],
            capture_output=True,
            text=True,
            check=False,
        )
        return result, stage, guard

    accepted, stage, guard = build(tmp_path / "accepted")
    assert accepted.returncode == 0, accepted.stderr
    assert not stage.exists()
    assert not guard.exists()

    rejected, stage, guard = build(tmp_path / "external-drift", external_drift=True)
    assert rejected.returncode != 0
    assert stage.is_dir() and guard.is_dir()

    rejected, stage, guard = build(tmp_path / "backup-present", backup=True)
    assert rejected.returncode != 0
    assert stage.is_dir() and guard.is_dir()

    assert "git archive" in source
    assert "unexpected archive member" in source
    assert "assert set(actual_files) == set(seen)" in source
    assert "assert seen == [item[0] for item in expected_files[:len(seen)]]" in source
    assert "assert partial is None" in source


def test_prebackup_stage_prefix_verifier_models_gnu_tar_interruption_modes():
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    archive = subprocess.check_output(
        [
            "git",
            "archive",
            "--format=tar",
            commit,
            "scripts",
            "src",
            "ops",
            "assets",
            "profiles",
            ".agent",
            "docs",
            "cleanup_manifests",
            "AGENTS.md",
            "README.md",
        ],
        cwd=ROOT,
    )
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as bundle:
        files = [
            (member.name, member.mode, bundle.extractfile(member).read())
            for member in bundle
            if member.isfile()
        ]
    partial_index = next(
        index
        for index, (name, mode, content) in enumerate(files)
        if index
        and len(name.split("/")) >= 3
        and len(content) > 1
        and any(previous_mode & 0o111 for _, previous_mode, _ in files[:index])
        and any(not (previous_mode & 0o111) for _, previous_mode, _ in files[:index])
    )
    completed = files[:partial_index]
    partial_name, partial_archive_mode, partial_bytes = files[partial_index]
    next_name, next_archive_mode, next_bytes = files[partial_index + 1]
    partial_size = min(len(partial_bytes) - 1, max(1, len(partial_bytes) // 2))
    assert partial_size > 0

    def inventory(*, partial_sha: str, partial_mode: int = 0o600, following: bool = False, extra: bool = False):
        entries: dict[str, dict[str, object]] = {"": {"type": "dir", "mode": 0o755}}
        active_ancestors = set()
        for name, _, _ in (*completed, (partial_name, partial_archive_mode, partial_bytes)):
            parts = name.split("/")[:-1]
            for index in range(1, len(parts) + 1):
                path = "/".join(parts[:index])
                entries[path] = {"type": "dir", "mode": 0o755}
        for index in range(1, len(partial_name.split("/"))):
            active_ancestors.add("/".join(partial_name.split("/")[:index]))
        for path in active_ancestors:
            entries[path]["mode"] = 0o700
        for name, mode, content in completed:
            entries[name] = {
                "type": "file",
                "mode": mode & ~0o022,
                "size": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
            }
        entries[partial_name] = {
            "type": "file",
            "mode": partial_mode,
            "size": partial_size,
            "sha256": partial_sha,
        }
        if following:
            entries[next_name] = {
                "type": "file",
                "mode": next_archive_mode & ~0o022,
                "size": len(next_bytes),
                "sha256": hashlib.sha256(next_bytes).hexdigest(),
            }
        if extra:
            entries["unknown"] = {
                "type": "file",
                "mode": 0o600,
                "size": 1,
                "sha256": hashlib.sha256(b"x").hexdigest(),
            }
        return {
            "schema_version": "deploy-prebackup-stage-inventory.v1",
            "entries": entries,
            "tree_sha256": "0" * 64,
        }

    verifier = _embedded_deploy_python("PY_LOCAL_PREBACKUP_STAGE")
    owner = f"{commit}-20260820T200852Z-16337"

    def run(payload: dict[str, object]):
        return subprocess.run(
            [sys.executable, "-c", verifier, owner, json.dumps(payload)],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )

    good = inventory(partial_sha=hashlib.sha256(partial_bytes[:partial_size]).hexdigest())
    assert run(good).returncode == 0
    assert any(entry["type"] == "file" and entry["mode"] == 0o644 for entry in good["entries"].values())
    assert any(entry["type"] == "file" and entry["mode"] == 0o755 for entry in good["entries"].values())
    active_ancestor = partial_name.rsplit("/", 1)[0]
    completed_directory = next(path for path, entry in good["entries"].items()
                               if path and entry["type"] == "dir" and path != active_ancestor
                               and entry["mode"] == 0o755)

    def changed(payload: dict[str, object], path: str, key: str, value: object):
        changed_payload = json.loads(json.dumps(payload))
        changed_payload["entries"][path][key] = value
        return changed_payload

    assert run(inventory(partial_sha="0" * 64)).returncode != 0
    assert run(inventory(partial_sha=good["entries"][partial_name]["sha256"], partial_mode=0o644)).returncode != 0
    completed_file = completed[0][0]
    assert run(changed(good, completed_file, "mode", 0o600)).returncode != 0
    assert run(changed(good, completed_directory, "mode", 0o700)).returncode != 0
    assert run(changed(good, active_ancestor, "mode", 0o755)).returncode != 0
    assert run(inventory(partial_sha=good["entries"][partial_name]["sha256"], following=True)).returncode != 0
    assert run(inventory(partial_sha=good["entries"][partial_name]["sha256"], extra=True)).returncode != 0


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
