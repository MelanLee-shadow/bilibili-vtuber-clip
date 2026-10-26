"""Fixed producer adapter used by the durable worker, never a publisher."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path


def _load_runner():
    try:
        from scripts import session_autoslice as runner
    except (ImportError, OSError, ValueError) as exc:
        from src.autoslice.overflow_worker import RuntimePrerequisiteError

        raise RuntimePrerequisiteError(
            "free-session producer runner import failed", startup_stage="producer_import"
        ) from exc
    return runner


def execute_request(request: dict, job_dir: Path) -> dict:
    root = Path(request["runtime_root"])
    from src.autoslice.overflow_worker import validate_runtime_prerequisites, pin_request_profile

    selected_profile = pin_request_profile(request)

    # The durable worker repeats the controller/probe gate immediately before
    # source validation and producer import.  Source media remains a content
    # concern below and is intentionally not folded into startup readiness.
    validate_runtime_prerequisites(
        root,
        request["release_sha256"],
        controller_root=request["controller_root"],
        check_adapter=False,
        lane=request["lane"],
        expected_profile_binding=request["profile_binding"],
    )
    for row in request.get("source_files", []):
        path = Path(row["path"])
        if path.is_symlink() or not path.is_file() or path.stat().st_size != row["bytes"]:
            raise ValueError("worker source identity mismatch")
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        if digest.hexdigest() != row["sha256"]:
            raise ValueError("worker source bytes mismatch")
    # Import only after binding this job's runtime. No tick/main/queue entrypoint
    # is called, and the node receives no uploader cookies or credentials.
    os.environ.pop("AUTOSLICE_EXECUTION_CONFIG", None)
    os.environ["AUTOSLICE_BASE"] = str(root)
    os.environ["AUTOSLICE_ENABLE_UPLOAD"] = "0"
    os.environ["AUTOSLICE_ENABLE_AUTHORIZED_UPLOAD_QUEUE"] = "0"
    os.environ["GEMINI_PAID_BACKUP_DAILY_CAP"] = "0"
    os.environ.setdefault("OMP_NUM_THREADS", "2")
    os.environ.setdefault("OPENBLAS_NUM_THREADS", "2")
    agy = Path("/data/autoslice-worker/producer-tools/agy")
    if agy.is_file():
        os.environ["AGY_BIN"] = str(agy)
        os.environ["AUTOSLICE_AGY_BIN"] = str(agy)
    os.environ["AGY_CLI_DISABLE_AUTO_UPDATE"] = "1"
    runner = _load_runner()
    from src.autoslice.channel_profile import binding_for_profile, require_profile_binding
    from src.autoslice.overflow_worker import RuntimePrerequisiteError
    try:
        require_profile_binding(task_binding=request["profile_binding"],
                                current_binding=binding_for_profile(runner.CHANNEL_PROFILE),
                                repo_root=selected_profile.repo_root)
    except ValueError as exc:
        raise RuntimePrerequisiteError(f"channel_profile: {exc}", startup_stage="producer_import") from exc
    credentials = runner.load_env_file(root / "cpa.env")
    os.environ.update({key: value for key, value in credentials.items() if key in {"CPA_API_KEY", "CPA_BASE_URL"}})
    for name in ("logs", "out", "cache", "reports", "runtime-scratch"):
        (root / name).mkdir(parents=True, exist_ok=True)
    item = dict(request["item"])
    recovery = item.get("prepared_recovery")
    if recovery is not None and (
        request["lane"] != "talk" or not isinstance(recovery, dict)
        or recovery.get("worker_store_id") != request.get("worker_store_id")
    ):
        raise ValueError("prepared recovery worker store differs from durable request")
    from src.autoslice.runtime_storage import create_ephemeral_workspace, collect_ephemeral
    collect_ephemeral(root, reason="overflow-stage-start")
    lease = create_ephemeral_workspace(root, owner=item["cid"])
    item["runtime_scratch_dir"] = str(lease.path)
    kwargs = dict(request.get("produce_kwargs", {}))
    if set(kwargs) - {"prepare_only", "reuse_cover"} or kwargs.get("prepare_only") is not True:
        raise ValueError("overflow worker accepts prepare-only producer calls")
    produce = runner.produce_talk if request["lane"] == "talk" else runner.produce_song
    result = produce(request["date"], item, **kwargs)
    # The patched real runner projects its verified selection. Missing result
    # provenance must not be synthesized from the request by the transport.
    from src.autoslice.channel_profile import require_record_profile_binding
    require_record_profile_binding(record_binding=result.get("profile_binding"),
        task_binding=binding_for_profile(selected_profile), repo_root=selected_profile.repo_root)
    if result.get("session_id") is None and item.get("session_id"):
        result["session_id"] = item["session_id"]
    from src.autoslice.overflow_bundle import export_bundle
    export_bundle(result=result, runtime_root=root, bundle_dir=job_dir / "bundle",
        job_id=request["job_id"], attempt_id=request["attempt_id"],
        release_sha256=request["release_sha256"], input_sha256=request["input_sha256"],
        source_files=request.get("source_files", []),
        expected_profile_binding=request["profile_binding"],
        date=request["date"], lane=request["lane"])
    # Prepared artifacts remain on the worker until the controller consumes
    # them; only its bounded ephemeral scratch can be retired now.
    lease.release(failed=result.get("status") != "delivery_prepared_no_target")
    collect_ephemeral(root, reason="overflow-result-exported")
    return result
