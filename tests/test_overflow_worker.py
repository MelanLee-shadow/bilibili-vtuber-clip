import json
import os
import time
from pathlib import Path

import pytest

from src.autoslice import overflow_worker as worker


@pytest.fixture(autouse=True)
def isolated_profile_selectors(monkeypatch):
    for name in ("AUTOSLICE_PROFILE", "AUTOSLICE_PROFILE_MANIFEST", "AUTOSLICE_PROFILE_BINDING"):
        monkeypatch.delenv(name, raising=False)


def _request(runtime: Path, *, attempt: str | None = None, item: dict | None = None,
             lane: str = "talk") -> dict:
    runtime.mkdir(parents=True, exist_ok=True)
    for name, content in [('mai.env', 'AZURE_ENDPOINT=https://resource.services.ai.azure.com\nAZURE_API_KEY=test-mai\n'),
                          ('moss.env', 'AUTOSLICE_MOSS_API_KEY=test-moss\n')]:
        (runtime/name).write_text(content)
        (runtime/name).chmod(0o600)
    repo = runtime / "repo"
    repo.mkdir(exist_ok=True)
    from src.autoslice.channel_profile import binding_for_profile, load_channel_profile
    profile_path = repo / "profiles/lidousha/profile.json"
    profile_path.parent.mkdir(parents=True, exist_ok=True)
    profile_path.write_bytes((Path(__file__).resolve().parents[1] / "profiles/lidousha/profile.json").read_bytes())
    profile_binding = binding_for_profile(load_channel_profile(repo, environ={}))
    (repo / "DEPLOYED_COMMIT").write_text("a" * 40)
    (repo / "DEPLOYED_AUTHORITY_MANIFEST.json").write_text("{}")
    from src.autoslice.overflow_release import seal

    release = seal(repo)
    request = {
        "schema_version": worker.REQUEST_SCHEMA,
        "profile_binding": profile_binding,
        "attempt_id": attempt or worker.new_attempt_id(),
        "release_sha256": release,
        "input_sha256": "b" * 64,
        "lane": lane,
        "date": "2026-10-05",
        "item": item or {"cid": "fixture"},
        "runtime_root": str(runtime),
        "controller_root": str(runtime),
        "estimated_peak_bytes": 1,
        "operational_reserve_bytes": 0,
    }
    with worker._slot(runtime.parent / "control"):
        request["worker_store_id"] = worker._store_id(runtime.parent / "control", initialize=True)
    request["job_id"] = worker.compute_job_id(request)
    return request


class _FakeProcess:
    pid = os.getpid()

    def terminate(self):
        return None


def _fake_popen(kept_fds):
    def spawn(_command, **kwargs):
        # Keep the gate's read end alive long enough for submit's parent write.
        kept_fds.append(os.dup(kwargs["pass_fds"][1]))
        return _FakeProcess()

    return spawn


def _submit(tmp_path, *, request=None, kept_fds=None):
    runtime = tmp_path / "runtime"
    runtime.mkdir(exist_ok=True)
    control = tmp_path / "control"
    kept_fds = [] if kept_fds is None else kept_fds
    request = request or _request(runtime)
    result = worker.submit_request(
        request,
        worker_root=control,
        allowed_runtime_parent=str(tmp_path),
        expected_runtime_root=str(runtime),
        popen_factory=_fake_popen(kept_fds),
    )
    return result, runtime, control, kept_fds


def test_submit_is_atomic_and_duplicate_or_conflict_never_relaunches(tmp_path):
    first, runtime, control, kept = _submit(tmp_path)
    assert first["accepted"] is True
    second, _, _, _ = _submit(tmp_path, request=_request(runtime, attempt=first["attempt_id"]), kept_fds=kept)
    assert second["duplicate"] is True
    conflicting = _request(runtime, attempt=first["attempt_id"])
    conflicting["produce_kwargs"] = {"prepare_only": True}
    with pytest.raises(worker.JobConflictError):
        worker.submit_request(
            conflicting,
            worker_root=control,
            allowed_runtime_parent=str(tmp_path),
            expected_runtime_root=str(runtime),
            popen_factory=_fake_popen(kept),
        )
    state = json.loads((Path(first["jobdir"]) / "state.json").read_text())
    assert state["status"] == "submitted"
    for fd in kept:
        os.close(fd)


def test_m60_same_request_new_attempt_is_duplicate_but_changed_request_conflicts(tmp_path):
    first, runtime, control, kept = _submit(tmp_path)
    request = _request(runtime)
    assert request["attempt_id"] != first["attempt_id"]
    try:
        with pytest.raises(worker.DuplicateJobError):
            worker.submit_request(request, worker_root=control,
                allowed_runtime_parent=str(tmp_path), expected_runtime_root=str(runtime),
                popen_factory=_fake_popen(kept))
        request["produce_kwargs"] = {"prepare_only": True}
        with pytest.raises(worker.JobConflictError):
            worker.submit_request(request, worker_root=control,
                allowed_runtime_parent=str(tmp_path), expected_runtime_root=str(runtime),
                popen_factory=_fake_popen(kept))
    finally:
        for fd in kept:
            os.close(fd)


def test_submit_refuses_busy_slot_and_low_disk_without_job_state(tmp_path, monkeypatch):
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    control = tmp_path / "control"
    request = _request(runtime)
    with worker._slot(control):
        with pytest.raises(worker.SlotBusyError):
            worker.submit_request(
                request,
                worker_root=control,
                allowed_runtime_parent=str(tmp_path),
                expected_runtime_root=str(runtime),
                popen_factory=_fake_popen([]),
            )
    monkeypatch.setattr(worker, "_free", lambda _path: 1)
    with pytest.raises(worker.InsufficientDiskError):
        worker.submit_request(
            request,
            worker_root=control,
            allowed_runtime_parent=str(tmp_path),
            expected_runtime_root=str(runtime),
            popen_factory=_fake_popen([]),
        )
    assert not (control / "jobs" / request["job_id"]).exists()


def test_status_reports_unknown_after_restart_without_rewriting_failed(tmp_path):
    submitted, runtime, control, kept = _submit(tmp_path)
    state_path = Path(submitted["jobdir"]) / "state.json"
    state = json.loads(state_path.read_text())
    state.update(status="running", pid=999_999_999, revision=state["revision"] + 1)
    state_path.write_text(json.dumps(state))
    status = worker.status_job(
        worker_root=control,
        job_id=submitted["job_id"],
        attempt_id=submitted["attempt_id"],
        allowed_runtime_parent=str(tmp_path),
        expected_runtime_root=str(runtime),
    )
    assert status["status"] == "unknown"
    assert status["lifecycle_status"] == "running"
    assert json.loads(state_path.read_text())["status"] == "running"
    for fd in kept:
        os.close(fd)


def test_completed_result_hash_is_verified_and_tamper_is_rejected(tmp_path):
    submitted, runtime, control, kept = _submit(tmp_path)
    completed = worker.run_job(
        worker_root=control,
        job_id=submitted["job_id"],
        attempt_id=submitted["attempt_id"],
        allowed_runtime_parent=str(tmp_path),
        expected_runtime_root=str(runtime),
        executor=lambda _request, _jobdir: {"candidate": "fixture", "status": "prepared", "profile_binding": _request["profile_binding"]},
    )
    assert completed["status"] == "completed"
    description = worker.fetch_description(
        worker_root=control,
        job_id=submitted["job_id"],
        attempt_id=submitted["attempt_id"],
        allowed_runtime_parent=str(tmp_path),
        expected_runtime_root=str(runtime),
    )
    assert description["result_sha256"] == completed["result_sha256"]
    Path(description["result_path"]).write_text("{}\n")
    with pytest.raises(worker.ResultIntegrityError):
        worker.fetch_description(
            worker_root=control,
            job_id=submitted["job_id"],
            attempt_id=submitted["attempt_id"],
            allowed_runtime_parent=str(tmp_path),
            expected_runtime_root=str(runtime),
        )
    for fd in kept:
        os.close(fd)


def test_runtime_path_escape_is_rejected_before_admission(tmp_path):
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    request = _request(runtime)
    with pytest.raises(worker.PathValidationError):
        worker.validate_request(request, allowed_runtime_parent=str(tmp_path / "other"))


def test_valid_marker_without_runtime_release_is_not_startup_ready(tmp_path):
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    control = tmp_path / "control"
    marker = tmp_path / "release.sha256"
    marker.write_text("a" * 64 + "\n")

    result = worker.probe(
        worker_root=control,
        runtime_root=runtime,
        release_marker=marker,
    )

    assert result["ok"] is True
    assert result["release_marker_status"] == "valid"
    assert result["startup_ok"] is False
    assert result["startup_error_code"] == "runtime_prerequisite_failed"


def test_probe_uses_common_media_preflight_with_uniform_host_defaults(tmp_path, monkeypatch):
    runtime = tmp_path / "runtime"
    _request(runtime)
    marker = runtime / "repo" / "OVERFLOW_RELEASE_SHA256"
    seen = []
    monkeypatch.setenv("AUTOSLICE_SPEAKER_MODE", "required")
    monkeypatch.setenv("AUTOSLICE_SPEAKER_PYTHON", "/missing/selected-python")

    def checker(**kwargs):
        seen.append(kwargs)

    from src.autoslice import producer_request
    monkeypatch.setattr(producer_request, "validate_producer_runtime_prerequisites", checker)
    result = worker.probe(
        worker_root=tmp_path / "control",
        runtime_root=runtime,
        release_marker=marker,
    )
    assert result["startup_ok"] is True
    assert len(seen) == 1
    assert seen[0]["lane"] == "talk"
    assert seen[0]["speaker_mode"] == "uniform_host"
    assert seen[0]["check_media"] is True


def test_selected_lane_reads_runner_environment_names(tmp_path, monkeypatch):
    from src.autoslice import producer_request

    request = _request(tmp_path / "runtime")
    monkeypatch.setenv("AUTOSLICE_SPEAKER_MODE", "required")
    monkeypatch.setenv("AUTOSLICE_SPEAKER_PYTHON", "/selected/speaker-python")
    monkeypatch.setenv("AUTOSLICE_HOST_VOCAL_PYTHON", "/selected/vocal-python")
    monkeypatch.setenv("SPEAKER_MODE", "uniform_host")
    monkeypatch.setenv("SPEAKER_PYTHON", "/unused/python")
    monkeypatch.setenv("HOST_VOCAL_PYTHON", "/unused/python")
    seen = []
    monkeypatch.setattr(producer_request, "validate_producer_runtime_prerequisites", lambda **kw: seen.append(kw))
    worker.validate_runtime_prerequisites(request["runtime_root"], request["release_sha256"], lane="song")
    assert seen[0]["speaker_mode"] == "required"
    assert seen[0]["speaker_python"] == "/selected/speaker-python"
    assert seen[0]["host_vocal_python"] == "/selected/vocal-python"
    assert seen[0]["lane"] == "song"


def test_song_selected_tool_failure_is_typed_before_worker_spawn(tmp_path, monkeypatch):
    runtime = tmp_path / "runtime"
    request = _request(runtime, lane="song")
    control = tmp_path / "control"
    spawn_calls = []

    from src.autoslice import producer_request
    monkeypatch.setattr(
        producer_request,
        "validate_producer_runtime_prerequisites",
        lambda **kwargs: (_ for _ in ()).throw(
            producer_request.ProducerRuntimePrerequisiteError(
                "host_vocal_python", "selected executable is unavailable"
            )
        ),
    )

    def forbidden_spawn(*args, **kwargs):
        spawn_calls.append((args, kwargs))
        raise AssertionError("selected tool preflight must precede worker spawn")

    with pytest.raises(worker.RuntimePrerequisiteError, match=r"^RUNTIME_PREREQUISITE:"):
        worker.submit_request(
            request,
            worker_root=control,
            allowed_runtime_parent=str(tmp_path),
            expected_runtime_root=str(runtime),
            popen_factory=forbidden_spawn,
        )
    assert spawn_calls == []
    assert not (control / "jobs" / request["job_id"]).exists()


def test_missing_runtime_release_fails_before_spawn_or_producer(tmp_path):
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    request = _request(runtime)
    (runtime / "repo" / "OVERFLOW_RELEASE_SHA256").unlink()
    control = tmp_path / "control"
    spawn_calls = []

    def forbidden_spawn(*args, **kwargs):
        spawn_calls.append((args, kwargs))
        raise AssertionError("startup failure must precede worker spawn")

    with pytest.raises(worker.RuntimePrerequisiteError, match="RUNTIME_PREREQUISITE:"):
        worker.submit_request(
            request,
            worker_root=control,
            allowed_runtime_parent=str(tmp_path),
            expected_runtime_root=str(runtime),
            popen_factory=forbidden_spawn,
        )

    assert spawn_calls == []
    assert not (control / "jobs" / request["job_id"]).exists()


def test_release_drift_fails_child_before_executor(tmp_path):
    submitted, runtime, control, kept = _submit(tmp_path)
    marker = runtime / "repo" / "OVERFLOW_RELEASE_SHA256"
    marker.write_text("f" * 64 + "\n")
    calls = []

    def forbidden_executor(*args):
        calls.append(args)
        raise AssertionError("release drift must stop before executor")

    result = worker.run_job(
        worker_root=control,
        job_id=submitted["job_id"],
        attempt_id=submitted["attempt_id"],
        allowed_runtime_parent=str(tmp_path),
        expected_runtime_root=str(runtime),
        executor=forbidden_executor,
    )

    assert result["status"] == "failed"
    assert result["error_code"] == "runtime_prerequisite_failed"
    assert calls == []
    state = json.loads((Path(submitted["jobdir"]) / "state.json").read_text())
    assert state["status"] == "failed"
    assert state["error_code"] == "runtime_prerequisite_failed"
    for fd in kept:
        os.close(fd)


@pytest.mark.parametrize("startup_stage", ["pre_executor", "producer_import"])
def test_startup_failure_restarts_same_attempt_after_gate_repair(tmp_path, startup_stage):
    submitted, runtime, control, kept = _submit(tmp_path)
    state_path = Path(submitted["jobdir"]) / "state.json"
    state = json.loads(state_path.read_text())
    state.update(
        status="failed", pid=999_999_999,
        error_code="runtime_prerequisite_failed", startup_stage=startup_stage,
        result_sha256=None, result_bytes=None, revision=state["revision"] + 1,
    )
    state_path.write_text(json.dumps(state))

    resumed = worker.submit_request(
        _request(runtime, attempt=submitted["attempt_id"], item={"cid": "fixture"}),
        worker_root=control,
        allowed_runtime_parent=str(tmp_path),
        expected_runtime_root=str(runtime),
        popen_factory=_fake_popen(kept),
    )
    assert resumed["accepted"] is True
    assert resumed["duplicate"] is False
    assert resumed["job_id"] == submitted["job_id"]
    assert resumed["attempt_id"] == submitted["attempt_id"]
    assert json.loads(state_path.read_text())["status"] == "submitted"

    launches = len(kept)
    duplicate = worker.submit_request(
        _request(runtime, attempt=submitted["attempt_id"], item={"cid": "fixture"}),
        worker_root=control,
        allowed_runtime_parent=str(tmp_path),
        expected_runtime_root=str(runtime),
        popen_factory=lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("submitted duplicate must not relaunch")
        ),
    )
    assert duplicate["duplicate"] is True
    assert len(kept) == launches
    for fd in kept:
        os.close(fd)


@pytest.mark.parametrize("blocker", ["live_pid", "result", "bundle", "bundle_symlink"])
def test_startup_failure_with_live_process_or_artifact_never_relaunches(tmp_path, blocker):
    submitted, runtime, control, kept = _submit(tmp_path)
    jobdir = Path(submitted["jobdir"])
    state_path = jobdir / "state.json"
    state = json.loads(state_path.read_text())
    state.update(status="failed", pid=os.getpid() if blocker == "live_pid" else 999_999_999,
                 error_code="runtime_prerequisite_failed", startup_stage="producer_import",
                 result_sha256=None, revision=state["revision"] + 1)
    state_path.write_text(json.dumps(state))
    if blocker == "result":
        (jobdir / "result.json").write_text("{}")
    elif blocker == "bundle":
        (jobdir / "bundle").mkdir()
    elif blocker == "bundle_symlink":
        (jobdir / "bundle").symlink_to(tmp_path / "absent")
    duplicate = worker.submit_request(
        _request(runtime, attempt=submitted["attempt_id"]), worker_root=control,
        allowed_runtime_parent=str(tmp_path), expected_runtime_root=str(runtime),
        popen_factory=lambda *a, **kw: pytest.fail("unsafe startup retry must not spawn"),
    )
    assert duplicate["duplicate"] is True
    for fd in kept:
        os.close(fd)


def test_late_typed_failure_is_not_restarted_by_duplicate_submit(tmp_path):
    submitted, runtime, control, kept = _submit(tmp_path)
    state_path = Path(submitted["jobdir"]) / "state.json"
    state = json.loads(state_path.read_text())
    state.update(
        status="failed", pid=999_999_999,
        error_code="runtime_prerequisite_failed", startup_stage="executor_started",
        result_sha256=None, result_bytes=None, revision=state["revision"] + 1,
    )
    state_path.write_text(json.dumps(state))
    launches = len(kept)
    duplicate = worker.submit_request(
        _request(runtime, attempt=submitted["attempt_id"], item={"cid": "fixture"}),
        worker_root=control,
        allowed_runtime_parent=str(tmp_path),
        expected_runtime_root=str(runtime),
        popen_factory=lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("late producer failure must not relaunch")
        ),
    )
    assert duplicate["duplicate"] is True
    assert len(kept) == launches
    for fd in kept:
        os.close(fd)


def test_runtime_preflight_wraps_path_and_symlink_failures_as_typed_startup(tmp_path):
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    with pytest.raises(worker.RuntimePrerequisiteError, match=r"^RUNTIME_PREREQUISITE:"):
        worker.validate_runtime_prerequisites(str(runtime / "missing"), "a" * 64)

    repo = runtime / "repo"
    repo.mkdir()
    (repo / "DEPLOYED_COMMIT").write_text("a" * 40)
    (repo / "DEPLOYED_AUTHORITY_MANIFEST.json").write_text("{}")
    from src.autoslice.overflow_release import seal
    release = seal(repo)
    linked = tmp_path / "linked-runtime"
    linked.symlink_to(runtime, target_is_directory=True)
    with pytest.raises(worker.RuntimePrerequisiteError, match=r"^RUNTIME_PREREQUISITE:"):
        worker.validate_runtime_prerequisites(str(linked), release)


def test_missing_source_file_remains_content_failure_after_startup_gate(tmp_path):
    from src.autoslice.overflow_execution_worker import execute_request

    runtime = tmp_path / "runtime"
    request = _request(runtime)
    request["source_files"] = [{
        "path": str(tmp_path / "missing.mp4"),
        "bytes": 1,
        "sha256": "b" * 64,
    }]

    with pytest.raises(ValueError, match="source identity") as caught:
        execute_request(request, tmp_path / "job")

    assert not isinstance(caught.value, worker.RuntimePrerequisiteError)


def test_free_session_runner_import_failure_keeps_typed_prefix(monkeypatch):
    import builtins

    from src.autoslice import overflow_execution_worker

    original_import = builtins.__import__

    def blocked(name, *args, **kwargs):
        if name == "scripts":
            raise ModuleNotFoundError("scripts package unavailable")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocked)
    with pytest.raises(worker.RuntimePrerequisiteError, match=r"^RUNTIME_PREREQUISITE:") as caught:
        overflow_execution_worker._load_runner()
    assert caught.value.startup_stage == "producer_import"


def test_real_adapter_import_failure_records_safe_stage_before_producer(tmp_path, monkeypatch):
    import builtins
    from src.autoslice import overflow_execution_worker

    submitted, runtime, control, kept = _submit(tmp_path)
    # execute_request mutates only this copied environment in the fixture.
    monkeypatch.setattr(overflow_execution_worker.os, "environ", os.environ.copy())
    original_import = builtins.__import__

    def blocked(name, *args, **kwargs):
        if name == "scripts":
            raise ModuleNotFoundError("scripts unavailable")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocked)
    result = worker.run_job(
        worker_root=control, job_id=submitted["job_id"], attempt_id=submitted["attempt_id"],
        allowed_runtime_parent=str(tmp_path), expected_runtime_root=str(runtime),
        executor=overflow_execution_worker.execute_request,
    )
    state = json.loads((Path(submitted["jobdir"]) / "state.json").read_text())
    assert result["error_code"] == "runtime_prerequisite_failed"
    assert state["startup_stage"] == "producer_import"
    assert not (Path(submitted["jobdir"]) / "result.json").exists()
    assert not (Path(submitted["jobdir"]) / "bundle").exists()
    assert not (runtime / "runtime-scratch").exists()
    for fd in kept:
        os.close(fd)


def test_producer_runtime_prerequisite_prefix_is_typed_in_worker_state(tmp_path):
    submitted, runtime, control, kept = _submit(tmp_path)

    def prerequisite_failure(*args):
        raise ValueError("RUNTIME_PREREQUISITE: ffmpeg: executable is unavailable")

    result = worker.run_job(
        worker_root=control,
        job_id=submitted["job_id"],
        attempt_id=submitted["attempt_id"],
        allowed_runtime_parent=str(tmp_path),
        expected_runtime_root=str(runtime),
        executor=prerequisite_failure,
    )

    assert result["status"] == "failed"
    assert result["error_code"] == "runtime_prerequisite_failed"
    for fd in kept:
        os.close(fd)


def test_real_detached_child_crosses_start_gate_and_completes(tmp_path, monkeypatch):
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    control = tmp_path / "control"
    request = _request(runtime)
    repo = Path(__file__).resolve().parents[1]
    child_script = tmp_path / "child_worker.py"
    child_script.write_text(
        "import argparse, sys\n"
        "from pathlib import Path\n"
        f"sys.path.insert(0, {str(repo)!r})\n"
        "from src.autoslice.overflow_worker import run_job\n"
        "p = argparse.ArgumentParser()\n"
        "p.add_argument('--worker-root', type=Path, required=True)\n"
        "p.add_argument('--runtime-root', type=Path, required=True)\n"
        "p.add_argument('--allowed-runtime-parent', type=Path, required=True)\n"
        "p.add_argument('--slot-fd', type=int, required=True)\n"
        "p.add_argument('--start-fd', type=int, required=True)\n"
        "p.add_argument('action')\n"
        "p.add_argument('--job-id', required=True)\n"
        "p.add_argument('--attempt-id', required=True)\n"
        "a = p.parse_args()\n"
        "run_job(worker_root=a.worker_root, job_id=a.job_id, attempt_id=a.attempt_id, "
        "allowed_runtime_parent=a.allowed_runtime_parent, expected_runtime_root=a.runtime_root, "
        "slot_fd=a.slot_fd, start_fd=a.start_fd, executor=lambda _request, _jobdir: {'child': True, 'profile_binding': _request['profile_binding']})\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(worker, "_script", lambda: child_script)
    admitted = worker.submit_request(
        request,
        worker_root=control,
        allowed_runtime_parent=str(tmp_path),
        expected_runtime_root=str(runtime),
    )
    deadline = time.monotonic() + 5
    observed = None
    while time.monotonic() < deadline:
        observed = worker.status_job(
            worker_root=control,
            job_id=request["job_id"],
            attempt_id=request["attempt_id"],
            allowed_runtime_parent=str(tmp_path),
            expected_runtime_root=str(runtime),
        )
        if observed["status"] == "completed":
            break
        assert observed["status"] in {"submitted", "running"}
        time.sleep(0.02)
    assert admitted["accepted"] is True
    assert observed is not None and observed["status"] == "completed"
    description = worker.fetch_description(
        worker_root=control,
        job_id=request["job_id"],
        attempt_id=request["attempt_id"],
        allowed_runtime_parent=str(tmp_path),
        expected_runtime_root=str(runtime),
    )
    assert description["result_sha256"] == observed["result_sha256"]


def test_store_id_survives_probe_and_new_empty_store_has_different_id(tmp_path):
    request = _request(tmp_path / "runtime")
    control = tmp_path / "control"
    marker = control / "worker-store.json"
    original = marker.read_bytes()
    first = worker.probe(worker_root=control, runtime_root=tmp_path / "runtime")
    second = worker.probe(worker_root=control, runtime_root=tmp_path / "runtime")
    assert first["worker_store_id"] == second["worker_store_id"] == request["worker_store_id"]
    assert marker.read_bytes() == original
    control.rename(tmp_path / "original-store")
    replacement = worker.probe(worker_root=control, runtime_root=tmp_path / "runtime")
    assert replacement["worker_store_id"] != request["worker_store_id"]
    with pytest.raises(worker.WorkerAuthorityError):
        worker.submit_request(request, worker_root=control, allowed_runtime_parent=tmp_path,
            expected_runtime_root=tmp_path / "runtime",
            popen_factory=lambda *a, **kw: pytest.fail("replacement must not spawn"))
    assert not (control / "jobs" / request["job_id"]).exists()


@pytest.mark.parametrize("change", ["missing", "malformed", "symlink", "different"])
def test_store_authority_loss_or_drift_blocks_submit_and_run(tmp_path, change):
    admitted, runtime, control, kept = _submit(tmp_path)
    jobdir = Path(admitted["jobdir"])
    request = json.loads((jobdir / "request.json").read_text())
    before = (jobdir / "state.json").read_bytes()
    marker = control / "worker-store.json"
    if change == "missing":
        marker.unlink()
    if change == "malformed":
        marker.write_text("{}")
    if change == "symlink":
        marker.rename(control / "marker-preimage.json")
        marker.symlink_to(control / "marker-preimage.json")
    if change == "different":
        marker.write_text(json.dumps({"schema_version":"autoslice-worker-store.v1", "store_id":"f" * 32}))
    try:
        with pytest.raises(worker.OverflowWorkerError):
            worker.submit_request(request,worker_root=control,allowed_runtime_parent=tmp_path,
                expected_runtime_root=runtime,popen_factory=lambda *a, **kw: pytest.fail("must not spawn"))
        with pytest.raises(worker.OverflowWorkerError):
            worker.run_job(worker_root=control,job_id=request["job_id"],attempt_id=request["attempt_id"],
                allowed_runtime_parent=tmp_path,expected_runtime_root=runtime,
                executor=lambda *a: pytest.fail("must not execute"))
        assert (jobdir / "state.json").read_bytes() == before
    finally:
        for fd in kept:
            os.close(fd)


def test_probe_never_initializes_missing_authority_beside_existing_jobs(tmp_path):
    admitted, runtime, control, kept = _submit(tmp_path)
    (control / "worker-store.json").unlink()
    try:
        with pytest.raises(worker.WorkerAuthorityError,match="restore original authority"):
            worker.probe(worker_root=control,runtime_root=runtime)
        assert not (control / "worker-store.json").exists()
        assert Path(admitted["jobdir"]).exists()
    finally:
        for fd in kept:
            os.close(fd)


def test_unbound_request_is_readonly_and_cannot_restart(tmp_path):
    admitted, runtime, control, kept = _submit(tmp_path)
    jobdir = Path(admitted["jobdir"])
    request = json.loads((jobdir / "request.json").read_text())
    request.pop("worker_store_id")
    state = json.loads((jobdir / "state.json").read_text())
    state.update(status="completed",request_sha256=worker.request_sha256(request))
    (jobdir / "request.json").write_text(json.dumps(request))
    (jobdir / "state.json").write_text(json.dumps(state))
    (control / "worker-store.json").unlink()
    try:
        observed = worker.status_job(worker_root=control,job_id=request["job_id"],attempt_id=request["attempt_id"],
            allowed_runtime_parent=tmp_path,expected_runtime_root=runtime)
        assert observed["status"] == "completed" and observed["worker_store_id"] is None
        with pytest.raises(worker.WorkerAuthorityError):
            worker.submit_request(request,worker_root=control,allowed_runtime_parent=tmp_path,
                expected_runtime_root=runtime,popen_factory=lambda *a, **kw: pytest.fail("unbound must not spawn"))
        assert not (control / "worker-store.json").exists()
    finally:
        for fd in kept:
            os.close(fd)


def _legacy_adoption(tmp_path, *, version="v2"):
    import hashlib

    request = _request(tmp_path / "runtime")
    control = tmp_path / "control"
    (control / "worker-store.json").unlink()
    request.pop("worker_store_id")
    if version == "v1":
        request["schema_version"] = worker.LEGACY_REQUEST_SCHEMA
        request.pop("profile_binding")
        request["job_id"] = worker.compute_job_id(request)
    jobdir = control / "jobs" / request["job_id"] / request["attempt_id"]
    jobdir.mkdir(parents=True)
    result = {"status": "failed"}
    if "profile_binding" in request:
        result["profile_binding"] = request["profile_binding"]
    raw_result = (json.dumps(result) + "\n").encode()
    (jobdir / "result.json").write_bytes(raw_result)
    (jobdir / "request.json").write_text(json.dumps(request))
    state = {"schema_version": worker.STATE_SCHEMA, "status": "completed", "pid": 999999999,
             "request_sha256": worker.request_sha256(request), "revision": 4,
             "result_sha256": hashlib.sha256(raw_result).hexdigest(), "result_bytes": len(raw_result),
             "bundle_path": None, "bundle_bytes": None,
             **{k: request[k] for k in ("job_id", "attempt_id", "release_sha256", "input_sha256")}}
    (jobdir / "state.json").write_text(json.dumps(state))
    dispatch = tmp_path / "controller/overflow-dispatch"
    dispatch.mkdir(parents=True)
    assignment = {k: request[k] for k in ("job_id", "attempt_id", "release_sha256", "input_sha256", "lane", "date", "item")}
    assignment.update(node="original-worker", phase="submitting")
    if "profile_binding" in request:
        assignment["profile_binding"] = request["profile_binding"]
    ap = dispatch / (request["job_id"] + ".json")
    rp = dispatch / (request["job_id"] + ".imported.json")
    ap.write_text(json.dumps(assignment))
    rp.write_bytes(raw_result)
    def digest(p):
        return hashlib.sha256(p.read_bytes()).hexdigest()
    row = {"job_id": request["job_id"], "attempt_id": request["attempt_id"],
           "files": {f.name: digest(f) for f in jobdir.iterdir()},
           "controller_assignment": {"path": str(ap), "sha256": digest(ap)},
           "controller_result": {"path": str(rp), "sha256": digest(rp)}}
    plan = {"schema_version": worker.LEGACY_ADOPTION_SCHEMA, "worker_root": str(control), "jobs": [row]}
    return control, request, plan


def _adopt(control, plan, tmp_path):
    return worker.adopt_legacy_store(plan, worker_root=control, allowed_runtime_parent=tmp_path)


@pytest.mark.parametrize("version", ["v1", "v2"])
def test_explicit_legacy_adoption_preserves_old_bytes_and_execution_ban(tmp_path, version):
    import hashlib

    control, request, plan = _legacy_adoption(tmp_path, version=version)
    before = {str(p): p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    with pytest.raises(worker.WorkerAuthorityError):
        worker.probe(worker_root=control, runtime_root=tmp_path / "runtime")
    adopted = _adopt(control, plan, tmp_path)
    marker = (control / "worker-store.json").read_bytes()
    repeated = _adopt(control, plan, tmp_path)
    assert not adopted["already_adopted"] and repeated["already_adopted"]
    assert adopted["worker_store_id"] == repeated["worker_store_id"]
    assert (control / "worker-store.json").read_bytes() == marker
    assert json.loads(marker)["legacy_adoption_sha256"] == hashlib.sha256(worker._json_bytes(plan)).hexdigest()
    assert all(Path(p).read_bytes() == raw for p, raw in before.items())
    assert worker.status_job(worker_root=control, job_id=request["job_id"], attempt_id=request["attempt_id"],
        allowed_runtime_parent=tmp_path)["status"] == "completed"
    assert worker.fetch_description(worker_root=control, job_id=request["job_id"], attempt_id=request["attempt_id"],
        allowed_runtime_parent=tmp_path)["status"] == "completed"
    with pytest.raises(worker.OverflowWorkerError):
        worker.submit_request(request, worker_root=control, allowed_runtime_parent=tmp_path,
            popen_factory=lambda *a, **kw: pytest.fail("old request must not spawn"))
    with pytest.raises(worker.OverflowWorkerError):
        worker.run_job(worker_root=control, job_id=request["job_id"], attempt_id=request["attempt_id"],
            allowed_runtime_parent=tmp_path, executor=lambda *a: pytest.fail("old request must not execute"))
    if version == "v2":
        with pytest.raises(worker.JobConflictError):
            worker.submit_request({**request, "worker_store_id": adopted["worker_store_id"]},
                worker_root=control, allowed_runtime_parent=tmp_path,
                popen_factory=lambda *a, **kw: pytest.fail("backfill must not spawn"))
    assert all(Path(p).read_bytes() == raw for p, raw in before.items())


@pytest.mark.parametrize("change", ["submitted", "unknown", "live_pid", "bound", "missing_receipt",
    "wrong_result", "wrong_assignment", "extra_entry", "incomplete", "symlink", "file_drift"])
def test_legacy_adoption_refuses_uncertainty_without_marker(tmp_path, change):
    import hashlib

    control, request, plan = _legacy_adoption(tmp_path)
    jobdir = control / "jobs" / request["job_id"] / request["attempt_id"]
    row = plan["jobs"][0]
    if change in {"submitted", "unknown", "live_pid"}:
        state = json.loads((jobdir / "state.json").read_text())
        if change == "live_pid":
            state["pid"] = os.getpid()
        else:
            state["status"] = "submitted"
            state["pid"] = os.getpid() if change == "submitted" else 999999999
        (jobdir / "state.json").write_text(json.dumps(state))
        row["files"]["state.json"] = hashlib.sha256((jobdir / "state.json").read_bytes()).hexdigest()
    elif change == "bound":
        request["worker_store_id"] = "f" * 32
        (jobdir / "request.json").write_text(json.dumps(request))
    elif change == "missing_receipt":
        Path(row["controller_result"]["path"]).unlink()
    elif change in {"wrong_result", "wrong_assignment"}:
        evidence = row["controller_result" if change == "wrong_result" else "controller_assignment"]
        p = Path(evidence["path"])
        value = json.loads(p.read_text())
        value["status" if change == "wrong_result" else "attempt_id"] = "wrong"
        p.write_text(json.dumps(value))
        evidence["sha256"] = hashlib.sha256(p.read_bytes()).hexdigest()
    elif change == "extra_entry":
        (control / "jobs" / "unrecognized").mkdir()
    elif change == "incomplete":
        (control / "jobs" / ("c" * 64) / ("d" * 32)).mkdir(parents=True)
    elif change == "symlink":
        p = Path(row["controller_result"]["path"])
        original = p.with_suffix('.original')
        p.rename(original)
        p.symlink_to(original)
    elif change == "file_drift":
        with (jobdir / "state.json").open('a') as handle:
            handle.write('\n')
    before = {str(p): p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    with pytest.raises(worker.OverflowWorkerError):
        _adopt(control, plan, tmp_path)
    assert not (control / "worker-store.json").exists()
    assert all(Path(p).read_bytes() == raw for p, raw in before.items())


def test_legacy_adoption_rechecks_external_input_and_refuses_busy_slot(tmp_path, monkeypatch):
    control, request, plan = _legacy_adoption(tmp_path)
    with worker._slot(control):
        with pytest.raises(worker.SlotBusyError):
            _adopt(control, plan, tmp_path)
    original = worker._adoption_snapshot
    calls = 0

    def changing(*args):
        nonlocal calls
        snapshot = original(*args)
        calls += 1
        if calls == 1:
            with Path(plan["jobs"][0]["controller_result"]["path"]).open('a') as handle:
                handle.write('\n')
        return snapshot

    monkeypatch.setattr(worker, "_adoption_snapshot", changing)
    with pytest.raises(worker.ResultIntegrityError):
        _adopt(control, plan, tmp_path)
    assert not (control / "worker-store.json").exists()


@pytest.mark.parametrize("when", ["before_commit", "after_commit"])
def test_legacy_adoption_interruption_can_retry_without_replacing_authority(tmp_path, monkeypatch, when):
    control, request, plan = _legacy_adoption(tmp_path)
    original = worker._write

    def interrupted(path, value, **kwargs):
        if when == "after_commit":
            original(path, value, **kwargs)
        raise OSError("injected interruption")

    monkeypatch.setattr(worker, "_write", interrupted)
    with pytest.raises(OSError):
        _adopt(control, plan, tmp_path)
    marker = control / "worker-store.json"
    before = marker.read_bytes() if marker.exists() else None
    monkeypatch.setattr(worker, "_write", original)
    result = _adopt(control, plan, tmp_path)
    assert result["already_adopted"] == (when == "after_commit")
    if before is not None:
        assert marker.read_bytes() == before


def test_legacy_adoption_retry_cannot_change_inventory_or_adopt_normal_authority(tmp_path):
    control, request, plan = _legacy_adoption(tmp_path)
    _adopt(control, plan, tmp_path)
    marker = control / "worker-store.json"
    before = marker.read_bytes()
    plan["jobs"][0]["files"]["state.json"] = "f" * 64
    with pytest.raises(worker.WorkerAuthorityError):
        _adopt(control, plan, tmp_path)
    assert marker.read_bytes() == before
    existing = json.loads(before)
    existing.pop("legacy_adoption_sha256")
    marker.write_text(json.dumps(existing))
    normal = marker.read_bytes()
    with pytest.raises(worker.WorkerAuthorityError):
        _adopt(control, plan, tmp_path)
    assert marker.read_bytes() == normal


def test_sync_adopts_original_private_runtime_then_probes_current_runtime(tmp_path, monkeypatch):
    import shlex
    import subprocess
    import fcntl
    from src.autoslice import overflow_install as install
    from src.autoslice import execution_backend
    monkeypatch.setattr(install, '_sync_native_audio_nodes', lambda *_: [])

    control, request, inventory = _legacy_adoption(tmp_path)
    runtime = tmp_path / "controller"
    assert request["runtime_root"] != str(runtime)
    original = {str(p): p.read_bytes() for p in control.rglob("*") if p.is_file()}
    declaration = runtime / "legacy-inventory.json"
    declaration.write_bytes(worker._json_bytes(inventory))
    config = runtime / "execution-backends.json"
    config.write_text(json.dumps({"mode": "auto", "release_sha256": "f" * 64,
        "nodes": [{"host": "original-worker", "runtime_root": str(runtime),
                   "worker_root": str(control), "legacy_adoption_inventory": {
                       "path": str(declaration), "sha256": worker._sha(declaration.read_bytes())}}]}))
    release = "e" * 64
    monkeypatch.setattr(install, "snapshot_archive", lambda repo: (b"frozen-code", release, {}))
    probes = []

    def rpc(command, **kwargs):
        args = shlex.split(command[-1])
        if args[-1] == "adopt-legacy-store":
            expected = args[args.index("--runtime-root") + 1] if "--runtime-root" in args else None
            result = worker.adopt_legacy_store(json.loads(kwargs["input"]), worker_root=control,
                allowed_runtime_parent=tmp_path, expected_runtime_root=expected)
            return subprocess.CompletedProcess(command, 0, stdout=worker._json_bytes(result))
        if args[-1] == "probe":
            assert args[args.index("--runtime-root") + 1] == str(runtime)
            probes.append(str(runtime))
            store = worker._store_id(control)
            result = ({"ok": False, "error": "worker_authority_unknown"} if store is None else
                {"startup_ok": True, "release_sha256": release, "runtime_root": str(runtime),
                 "worker_root": str(control), "worker_store_id": store})
            return subprocess.CompletedProcess(command, 2 if store is None else 0,
                                                stdout=worker._json_bytes(result))
        return subprocess.CompletedProcess(command, 0, stdout=b'{"status":"INSTALLED"}')

    class Backend:
        def __init__(self, base, value, **kwargs):
            assert value["release_sha256"] == release
        def _rpc(self, node, action):
            return {"startup_ok": True}
        def _probe_authority(self, node, value):
            assert value["startup_ok"]

    monkeypatch.setattr(install.subprocess, "run", rpc)
    monkeypatch.setattr(execution_backend, "SSHBackend", Backend)
    lease_module = runtime / "repo/src/autoslice/runtime_code_lease.py"
    lease_module.parent.mkdir(parents=True)
    lease_module.touch()
    with (runtime / ".oci3-runtime-code.lock").open("wb+") as lease:
        fcntl.flock(lease, fcntl.LOCK_SH)
        monkeypatch.setenv("AUTOSLICE_RUNTIME_CODE_LEASE_FD", str(lease.fileno()))
        result = install.sync(runtime, config)
    assert result["status"] == "REGISTERED"
    assert len(probes) == 2
    assert json.loads(config.read_bytes())["release_sha256"] == release
    assert worker._store_id(control)
    assert all(Path(path).read_bytes() == data for path, data in original.items())
    with pytest.raises(worker.WorkerAuthorityError):
        worker.submit_request(request, worker_root=control, allowed_runtime_parent=tmp_path,
            popen_factory=lambda *a, **kw: pytest.fail("old unbound request must never execute"))


def test_startup_requires_actual_native_provider_configuration_without_dispatch(tmp_path, monkeypatch):
    from src.autoslice import producer_request
    request=_request(tmp_path/'runtime');runtime=Path(request['runtime_root'])
    monkeypatch.setattr(worker,'_adapter',lambda:None)
    monkeypatch.setattr(producer_request,'validate_producer_runtime_prerequisites',lambda **_:None)
    assert worker.validate_runtime_prerequisites(runtime,request['release_sha256'])['runtime_root']==str(runtime)
    (runtime/'moss.env').unlink()
    with pytest.raises(worker.RuntimePrerequisiteError,match='native_audio'):
        worker.validate_runtime_prerequisites(runtime,request['release_sha256'])
