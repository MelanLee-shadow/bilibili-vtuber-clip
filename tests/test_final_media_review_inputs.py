from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import wave

import pytest

from src.autoslice.final_media_review_inputs import (
    JOB_SCHEMA_VERSION,
    RESULT_SCHEMA_VERSION,
    FinalMediaReviewInputError,
    assess_review_inputs,
    consume_review_job,
)
from src.autoslice.llm_client import LlmCallError
from src.autoslice.final_media_review_raw_av import (
    PACKAGE_BINDING_SCHEMA_VERSION,
)
from src.autoslice.final_media_review_transport_evidence import (
    FinalMediaReviewTransportEvidenceError,
    validate_transport_evidence,
)
from tests.final_media_review_test_support import (
    package_model_capability_fields,
)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _wav(path: Path, *, duration_us: int, sample_rate: int = 16_000) -> None:
    frames = round(duration_us * sample_rate / 1_000_000)
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(b"\0\0" * frames)


def _artifact(path: Path, **extra: object) -> dict[str, object]:
    return {
        "path": str(path.resolve()),
        "bytes": path.stat().st_size,
        "sha256": _sha(path),
        **extra,
    }


def _attach_transport_binding(
    root: Path,
    job: dict[str, object],
    *,
    provider_audio: bool,
    provider_video: bool,
    provider: str = "cpa",
) -> None:
    document = {
        "schema_version": PACKAGE_BINDING_SCHEMA_VERSION,
        "capability_id": "synthetic-test-capability",
        "provider": provider,
        "transport": "content_bound_command",
        "model": "synthetic-raw-av-model",
        "endpoint_family": "synthetic_raw_av",
        "accepts": {
            "raw_audio": provider_audio,
            "continuous_source_video": provider_video,
        },
        "runtime_capability_sha256": "1" * 64,
        "executable_sha256": "2" * 64,
        "command_contract_sha256": "3" * 64,
        **package_model_capability_fields(),
        "result_schema_version": RESULT_SCHEMA_VERSION,
    }
    path = root / "transport-capability.json"
    _write_json(path, document)
    job["transport_capability"] = _artifact(path)


def _job(
    tmp_path: Path,
    *,
    windows: list[tuple[str, int, int, int]],
    provider_audio: bool,
    provider_video: bool,
    duration_us: int = 2_000_000,
    provider: str = "cpa",
) -> tuple[Path, dict[str, object]]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    source = tmp_path / "source.mp4"
    source.write_bytes(b"exact synthetic final video bytes")
    artifacts: list[dict[str, object]] = []
    exact_full_audio = (
        provider_audio
        and len(windows) == 1
        and windows[0][1] == 0
        and windows[0][2] == duration_us
    )
    for name, start_us, declared_end_us, actual_duration_us in windows:
        audio = tmp_path / f"{name}.wav"
        _wav(audio, duration_us=actual_duration_us)
        artifacts.append(
            _artifact(
                audio,
                kind=(
                    "exact_full_audio_wav"
                    if exact_full_audio
                    else "diagnostic_wav"
                ),
                window=name,
                range_seconds=[start_us / 1_000_000, declared_end_us / 1_000_000],
            )
        )
    frame = tmp_path / "frame.jpg"
    frame.write_bytes(b"jpeg-like bytes")
    artifacts.append(
        _artifact(frame, window="sample", timestamp_seconds=0.5)
    )
    manifest = {
        "schema": "synthetic-review-assets.v1",
        "source_video": _artifact(source),
        "artifacts": artifacts,
    }
    manifest_path = tmp_path / "review-assets.json"
    _write_json(manifest_path, manifest)
    value: dict[str, object] = {
        "schema_version": JOB_SCHEMA_VERSION,
        "candidate_id": "auto_200000_1_2",
        "asset_manifest": {
            "path": str(manifest_path.resolve()),
            "sha256": _sha(manifest_path),
        },
        "media_clock": {
            "source_video_sha256": _sha(source),
            "source_video_bytes": source.stat().st_size,
            "duration_us": duration_us,
            "first_video_pts_us": 0,
            "last_video_pts_us": duration_us - 40_000,
            "video_frame_count": 50,
        },
        "requirements": {
            "continuous_audio_required": True,
            "continuous_visual_required": True,
            "content_review_required": True,
            "exact_media_clock_evidence_required": False,
            "provider_accepts_bound_source_video": provider_video,
            "provider_accepts_bound_audio": provider_audio,
        },
    }
    if provider_audio or provider_video:
        _attach_transport_binding(
            tmp_path,
            value,
            provider_audio=provider_audio,
            provider_video=provider_video,
            provider=provider,
        )
    path = tmp_path / "job.json"
    _write_json(path, value)
    return path, value


def _synthetic_transport_evidence(
    assessment: dict[str, object],
) -> dict[str, object]:
    capability = assessment["transport_capability"]
    audio = assessment["audio"]
    full_audio = next(
        row
        for row in audio["windows"]
        if row["kind"] == "exact_full_audio_wav"
    )
    return {
        "schema_version": "final-media-review-raw-av-evidence.v3",
        "provider": capability["provider"],
        "model": capability["model"],
        "endpoint_family": capability["endpoint_family"],
        "capability_id": capability["capability_id"],
        "runtime_capability_sha256": capability[
            "runtime_capability_sha256"
        ],
        "executable_sha256": capability["executable_sha256"],
        "model_capability_seal_receipt_sha256": capability[
            "model_capability_seal_receipt_sha256"
        ],
        "model_capability_attestation_sha256": capability[
            "model_capability_attestation_sha256"
        ],
        "model_capability_contract_sha256": capability[
            "model_capability_contract_sha256"
        ],
        "model_capability_sentinel_result_sha256": capability[
            "model_capability_sentinel_result_sha256"
        ],
        "model_capability_sentinel_result_self_sha256": capability[
            "model_capability_sentinel_result_self_sha256"
        ],
        "model_capability_sentinel_runner_sha256": capability[
            "model_capability_sentinel_runner_sha256"
        ],
        "request_sha256": "c" * 64,
        "response_sha256": "5" * 64,
        "source_video_sha256": assessment["exact_media_clock"][
            "source_video_sha256"
        ],
        "raw_audio_sha256": full_audio["sha256"],
        "asset_manifest_sha256": assessment["asset_manifest"]["sha256"],
        "consumed_continuous_source_video": True,
        "consumed_raw_audio": True,
    }


def test_assessment_reports_gaps_clock_overrun_and_sparse_visuals(tmp_path: Path):
    _path, job = _job(
        tmp_path,
        windows=[
            ("opening", 0, 1_000_000, 1_010_000),
            ("ending", 1_500_000, 2_000_500, 500_000),
        ],
        provider_audio=False,
        provider_video=False,
    )

    value = assess_review_inputs(job)

    assert value["status"] == "BLOCKED_INPUT"
    assert value["content_review_status"] == "UNASSESSED"
    assert value["visual"]["coverage"] == "SPARSE_DIAGNOSTIC_FRAMES"
    assert value["visual"]["frame_count"] == 1
    assert value["audio"]["declared_gaps"] == [
        {"start_us": 1_000_000, "end_us": 1_500_000, "duration_us": 500_000}
    ]
    assert value["audio"]["actual_gaps"] == [
        {"start_us": 1_010_000, "end_us": 1_500_000, "duration_us": 490_000}
    ]
    assert {
        "FINAL_MEDIA_REVIEW_AUDIO_COVERAGE_INCOMPLETE",
        "FINAL_MEDIA_REVIEW_AUDIO_DURATION_MISMATCH",
        "FINAL_MEDIA_REVIEW_AUDIO_TRANSPORT_MISSING",
        "FINAL_MEDIA_REVIEW_VISUAL_INPUT_NOT_CONTINUOUS",
        "FINAL_MEDIA_REVIEW_WINDOW_AFTER_EOF",
    } <= set(value["reason_codes"])


def test_independent_integrity_receipt_reuses_hash_validation_only(tmp_path: Path):
    path, job = _job(
        tmp_path,
        windows=[("all", 0, 2_000_000, 2_000_000)],
        provider_audio=True,
        provider_video=True,
    )
    manifest_path = Path(job["asset_manifest"]["path"])
    manifest = json.loads(manifest_path.read_text())
    receipt = {
        "source_manifest_sha256": _sha(manifest_path),
        "verified_assets": len(manifest["artifacts"]),
        "source_video_sha256": job["media_clock"]["source_video_sha256"],
        "duration_seconds": 2.0,
        "disposition": "ACCEPTED_INPUT_INTEGRITY_AND_TIMING_ONLY",
        "content_review_passed": False,
        "quality_release": False,
        "upload": False,
    }
    receipt_path = tmp_path / "integrity.json"
    _write_json(receipt_path, receipt)
    job["integrity_receipt"] = {
        "path": str(receipt_path.resolve()),
        "sha256": _sha(receipt_path),
    }
    _write_json(path, job)

    value = assess_review_inputs(job)

    assert value["status"] == "READY_FOR_PERCEPTUAL_REVIEW"
    assert value["integrity"] == {
        "status": "PASS",
        "independent_receipt_reused": True,
        "receipt": {
            "path": str(receipt_path.resolve()),
            "sha256": _sha(receipt_path),
            "verified_assets": len(manifest["artifacts"]),
            "disposition": "ACCEPTED_INPUT_INTEGRITY_AND_TIMING_ONLY",
            "authority": "INDEPENDENT_INPUT_VALIDATOR",
        },
        "exact_media_clock_evidence": None,
        "content_review_proved": False,
    }
    assert value["content_review_status"] == "UNASSESSED"


def test_consumer_persists_backoff_then_dedupes_terminal_result(tmp_path: Path):
    job_path, _job_value = _job(
        tmp_path,
        windows=[("all", 0, 2_000_000, 2_000_000)],
        provider_audio=True,
        provider_video=True,
    )
    state_path = tmp_path / "state.json"
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    env_path = runtime / "cpa.env"
    env_path.write_text(
        "CPA_BASE_URL=https://cpa.invalid/v1\nCPA_API_KEY=secret-for-test-only\n",
        encoding="utf-8",
    )
    env_path.chmod(0o600)

    calls = {"executor": 0, "builder": 0}

    def final_call_builder(**_kwargs):
        calls["builder"] += 1

        def call(_prompt: str) -> str:
            return "{}"

        call.provider_runtime_binding = {
            "provider_transport": "runtime_cpa",
            "provider_endpoint_host": "cpa.invalid",
            "provider_endpoint_path": "/v1",
            "provider_credential_source": str(env_path),
        }
        return call

    def executor(job, assessment, _llm_call, _semantic_command):
        calls["executor"] += 1
        if calls["executor"] == 1:
            raise LlmCallError(
                "rate limited 429",
                provider_diagnostics={
                    "provider_http_status": 429,
                    "provider_transport": "runtime_cpa",
                },
            )
        return {
            "schema_version": RESULT_SCHEMA_VERSION,
            "candidate_id": job["candidate_id"],
            "source_video_sha256": assessment["exact_media_clock"][
                "source_video_sha256"
            ],
            "asset_manifest_sha256": assessment["asset_manifest"]["sha256"],
            "status": "PASS",
            "content_review_status": "PASS",
            "observations": ["synthetic executor consumed bound inputs"],
            "transport_evidence": _synthetic_transport_evidence(assessment),
        }

    start = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)
    first = consume_review_job(
        job_path,
        state_path,
        runtime_root=runtime,
        now=start,
        executor=executor,
        final_review_call_builder=final_call_builder,
    )
    assert first["state"]["status"] == "RETRY_WAIT"
    assert first["state"]["attempt_count"] == 1
    assert first["provider_called"] is True

    before_due = consume_review_job(
        job_path,
        state_path,
        runtime_root=runtime,
        now=start + timedelta(seconds=30),
        executor=executor,
        final_review_call_builder=final_call_builder,
    )
    assert before_due["cache_reused"] is True
    assert before_due["provider_called"] is False
    assert calls["executor"] == 1

    after_due = consume_review_job(
        job_path,
        state_path,
        runtime_root=runtime,
        now=start + timedelta(seconds=61),
        executor=executor,
        final_review_call_builder=final_call_builder,
    )
    assert after_due["state"]["status"] == "COMPLETE"
    assert after_due["state"]["result"]["status"] == "PASS"
    assert after_due["state"]["attempt_count"] == 2
    assert calls["executor"] == 2

    replay = consume_review_job(
        job_path,
        state_path,
        runtime_root=runtime,
        now=start + timedelta(hours=1),
        executor=executor,
        final_review_call_builder=final_call_builder,
    )
    assert replay["cache_reused"] is True
    assert replay["provider_called"] is False
    assert calls["executor"] == 2
    assert calls["builder"] == 2


def test_consumer_backoff_survives_fresh_processes_and_terminal_dedupes(
    tmp_path: Path,
) -> None:
    job_path, _job_value = _job(
        tmp_path / "package",
        windows=[("all", 0, 2_000_000, 2_000_000)],
        provider_audio=True,
        provider_video=True,
    )
    state_path = tmp_path / "process-review-state.json"
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    env_path = runtime / "cpa.env"
    env_path.write_text(
        "CPA_BASE_URL=https://cpa.invalid/v1\n"
        "CPA_API_KEY=process-test-only\n",
        encoding="utf-8",
    )
    env_path.chmod(0o600)
    executor_ledger = tmp_path / "executor-calls.jsonl"
    builder_ledger = tmp_path / "builder-calls.jsonl"
    worker = tmp_path / "consumer-process-worker.py"
    worker.write_text(
        r'''from __future__ import annotations

from datetime import datetime
import json
import os
from pathlib import Path
import sys

from src.autoslice.final_media_review_inputs import (
    RESULT_SCHEMA_VERSION,
    consume_review_job,
)
from src.autoslice.llm_client import LlmCallError
from tests.test_final_media_review_inputs import _synthetic_transport_evidence

job_path = Path(sys.argv[1])
state_path = Path(sys.argv[2])
runtime = Path(sys.argv[3])
phase = sys.argv[4]
now = datetime.fromisoformat(sys.argv[5])
executor_ledger = Path(sys.argv[6])
builder_ledger = Path(sys.argv[7])
env_path = runtime / "cpa.env"


def append(path: Path, kind: str) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {"kind": kind, "phase": phase, "pid": os.getpid()},
                sort_keys=True,
            )
            + "\n"
        )


def final_call_builder(**_kwargs):
    append(builder_ledger, "builder")

    def call(_prompt: str) -> str:
        raise AssertionError("synthetic executor must not call text transport")

    call.provider_runtime_binding = {
        "provider_transport": "runtime_cpa",
        "provider_endpoint_host": "cpa.invalid",
        "provider_endpoint_path": "/v1",
        "provider_credential_source": str(env_path),
    }
    return call


def executor(job, assessment, _llm_call, _semantic_command):
    append(executor_ledger, "executor")
    if phase == "rate-limit":
        raise LlmCallError(
            "synthetic process-bound rate limit",
            provider_diagnostics={
                "provider_http_status": 429,
                "provider_transport": "runtime_cpa",
            },
        )
    if phase != "pass":
        raise AssertionError(f"executor replayed during {phase}")
    return {
        "schema_version": RESULT_SCHEMA_VERSION,
        "candidate_id": job["candidate_id"],
        "source_video_sha256": assessment["exact_media_clock"][
            "source_video_sha256"
        ],
        "asset_manifest_sha256": assessment["asset_manifest"]["sha256"],
        "status": "PASS",
        "content_review_status": "PASS",
        "observations": ["fresh process consumed exact synthetic inputs"],
        "transport_evidence": _synthetic_transport_evidence(assessment),
    }


outcome = consume_review_job(
    job_path,
    state_path,
    runtime_root=runtime,
    now=now,
    executor=executor,
    final_review_call_builder=final_call_builder,
)
print(
    json.dumps(
        {"outcome": outcome, "phase": phase, "pid": os.getpid()},
        ensure_ascii=False,
        sort_keys=True,
    )
)
''',
        encoding="utf-8",
    )
    repository = Path(__file__).resolve().parents[1]
    process_environment = {
        "PATH": os.environ.get("PATH", ""),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONPATH": str(repository),
    }

    def run_process(phase: str, when: datetime) -> dict[str, object]:
        completed = subprocess.run(
            [
                sys.executable,
                str(worker),
                str(job_path),
                str(state_path),
                str(runtime),
                phase,
                when.isoformat(),
                str(executor_ledger),
                str(builder_ledger),
            ],
            cwd=repository,
            env=process_environment,
            text=True,
            capture_output=True,
            check=False,
            timeout=30,
        )
        assert completed.returncode == 0, completed.stderr
        assert completed.stderr == ""
        return json.loads(completed.stdout)

    start = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)
    first = run_process("rate-limit", start)
    first_outcome = first["outcome"]
    assert first_outcome["state"]["status"] == "RETRY_WAIT"
    assert first_outcome["state"]["attempt_count"] == 1
    assert first_outcome["provider_called"] is True
    assert first_outcome["provider_call_status"] == "RATE_LIMITED_RESPONSE"
    first_state_sha = first_outcome["state"]["state_sha256"]

    before_due = run_process("forbidden-before-due", start + timedelta(seconds=30))
    before_due_outcome = before_due["outcome"]
    assert before_due_outcome["cache_reused"] is True
    assert before_due_outcome["provider_called"] is False
    assert before_due_outcome["state"]["status"] == "RETRY_WAIT"
    assert before_due_outcome["state"]["state_sha256"] == first_state_sha

    after_due = run_process("pass", start + timedelta(seconds=61))
    after_due_outcome = after_due["outcome"]
    assert after_due_outcome["state"]["status"] == "COMPLETE"
    assert after_due_outcome["state"]["attempt_count"] == 2
    assert after_due_outcome["state"]["result"]["status"] == "PASS"
    assert (
        after_due_outcome["state"]["result"]["content_review_status"]
        == "PASS"
    )
    assert after_due_outcome["provider_called"] is True
    assert after_due_outcome["provider_call_status"] == "RESPONSE_ACCEPTED"
    terminal_state_sha = after_due_outcome["state"]["state_sha256"]
    terminal_bytes = state_path.read_bytes()

    replay = run_process("forbidden-terminal", start + timedelta(hours=1))
    replay_outcome = replay["outcome"]
    assert replay_outcome["cache_reused"] is True
    assert replay_outcome["provider_called"] is False
    assert replay_outcome["state"]["status"] == "COMPLETE"
    assert replay_outcome["state"]["state_sha256"] == terminal_state_sha
    assert state_path.read_bytes() == terminal_bytes

    executor_calls = [
        json.loads(line)
        for line in executor_ledger.read_text(encoding="utf-8").splitlines()
    ]
    builder_calls = [
        json.loads(line)
        for line in builder_ledger.read_text(encoding="utf-8").splitlines()
    ]
    assert [row["phase"] for row in executor_calls] == ["rate-limit", "pass"]
    assert [row["phase"] for row in builder_calls] == ["rate-limit", "pass"]
    assert all(row["pid"] > 0 for row in executor_calls + builder_calls)
    assert [row["phase"] for row in (first, before_due, after_due, replay)] == [
        "rate-limit",
        "forbidden-before-due",
        "pass",
        "forbidden-terminal",
    ]
    assert all(
        row["pid"] != os.getpid()
        for row in (first, before_due, after_due, replay)
    )


def test_bound_transport_result_without_evidence_is_rejected(
    tmp_path: Path,
):
    job_path, _job_value = _job(
        tmp_path,
        windows=[("all", 0, 2_000_000, 2_000_000)],
        provider_audio=True,
        provider_video=True,
    )
    runtime = tmp_path / "runtime-missing-evidence"
    runtime.mkdir()
    env_path = runtime / "cpa.env"
    env_path.write_text(
        "CPA_BASE_URL=https://cpa.invalid/v1\nCPA_API_KEY=test-only\n",
        encoding="utf-8",
    )
    env_path.chmod(0o600)

    def final_call_builder(**_kwargs):
        def call(_prompt: str) -> str:
            return "{}"

        call.provider_runtime_binding = {
            "provider_transport": "runtime_cpa",
            "provider_endpoint_host": "cpa.invalid",
        }
        return call

    def executor(job, assessment, _llm_call, _semantic_command):
        return {
            "schema_version": RESULT_SCHEMA_VERSION,
            "candidate_id": job["candidate_id"],
            "source_video_sha256": assessment["exact_media_clock"][
                "source_video_sha256"
            ],
            "asset_manifest_sha256": assessment["asset_manifest"]["sha256"],
            "status": "PASS",
            "content_review_status": "PASS",
            "observations": ["claims exact inputs but has no transport receipt"],
        }

    outcome = consume_review_job(
        job_path,
        tmp_path / "missing-evidence-state.json",
        runtime_root=runtime,
        executor=executor,
        final_review_call_builder=final_call_builder,
    )

    assert outcome["state"]["status"] == "FAILED"
    assert outcome["provider_called"] is True
    assert outcome["provider_call_status"] == "RESPONSE_INVALID"
    assert outcome["state"]["reason_codes"] == [
        "FINAL_MEDIA_REVIEW_RESULT_INVALID"
    ]
    assert outcome["state"]["provider_diagnostics"]["provider_error_code"] == (
        "FINAL_MEDIA_REVIEW_RESULT_INVALID"
    )


def test_consumer_does_not_prepare_cpa_for_blocked_inputs(tmp_path: Path):
    job_path, _job_value = _job(
        tmp_path,
        windows=[("partial", 0, 1_000_000, 1_000_000)],
        provider_audio=True,
        provider_video=True,
    )
    called = {"builder": 0}

    def forbidden_builder(**_kwargs):
        called["builder"] += 1
        raise AssertionError("CPA transport must not be built for incomplete inputs")

    value = consume_review_job(
        job_path,
        tmp_path / "state.json",
        runtime_root=tmp_path / "missing-runtime",
        final_review_call_builder=forbidden_builder,
    )

    assert value["state"]["status"] == "BLOCKED_INPUT"
    assert value["provider_called"] is False
    assert called["builder"] == 0


def test_state_path_refuses_different_job_binding(tmp_path: Path):
    first_path, _ = _job(
        tmp_path / "first",
        windows=[("partial", 0, 1_000_000, 1_000_000)],
        provider_audio=False,
        provider_video=False,
    )
    state = tmp_path / "state.json"
    consume_review_job(first_path, state)

    second_path, second = _job(
        tmp_path / "second",
        windows=[("partial", 0, 1_000_000, 1_000_000)],
        provider_audio=False,
        provider_video=False,
    )
    second["candidate_id"] = "auto_200000_2_3"
    _write_json(second_path, second)
    with pytest.raises(
        FinalMediaReviewInputError,
        match="FINAL_MEDIA_REVIEW_STATE_BINDING_MISMATCH",
    ):
        consume_review_job(second_path, state)


def test_same_candidate_successor_job_archives_prior_state_without_deletion(
    tmp_path: Path,
):
    first_path, _ = _job(
        tmp_path / "first",
        windows=[("partial", 0, 1_000_000, 1_000_000)],
        provider_audio=False,
        provider_video=False,
    )
    state_path = tmp_path / "state.json"
    first = consume_review_job(first_path, state_path)
    first_state = dict(first["state"])
    assert first_state["status"] == "BLOCKED_INPUT"

    successor_path, _ = _job(
        tmp_path / "successor",
        windows=[("different-partial", 0, 1_250_000, 1_250_000)],
        provider_audio=False,
        provider_video=False,
    )
    successor = consume_review_job(successor_path, state_path)

    assert successor["state"]["candidate_id"] == first_state["candidate_id"]
    assert successor["state"]["binding_sha256"] != first_state["binding_sha256"]
    assert successor["cache_reused"] is False
    assert successor["provider_called"] is False
    archive_path = Path(successor["superseded_state_path"])
    assert archive_path.is_file()
    assert json.loads(archive_path.read_text(encoding="utf-8")) == first_state
    assert json.loads(state_path.read_text(encoding="utf-8")) == successor["state"]


def test_runtime_root_is_bootstrapped_from_normal_runner_environment(tmp_path: Path):
    job_path, _ = _job(
        tmp_path,
        windows=[("all", 0, 2_000_000, 2_000_000)],
        provider_audio=True,
        provider_video=True,
    )
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    env_path = runtime / "cpa.env"
    env_path.write_text(
        "CPA_BASE_URL=https://cpa.invalid/v1\nCPA_API_KEY=secret-for-test-only\n",
        encoding="utf-8",
    )
    env_path.chmod(0o600)
    calls = {"builder": 0}

    def final_call_builder(**_kwargs):
        calls["builder"] += 1

        def call(_prompt: str) -> str:
            return "{}"

        call.provider_runtime_binding = {
            "provider_transport": "runtime_cpa",
            "provider_endpoint_host": "cpa.invalid",
            "provider_endpoint_path": "/v1",
            "provider_credential_source": str(env_path),
        }
        return call

    outcome = consume_review_job(
        job_path,
        tmp_path / "state.json",
        environment={"AUTOSLICE_BASE": str(runtime)},
        executor=None,
        final_review_call_builder=final_call_builder,
    )

    assert outcome["state"]["status"] == "WAITING_CAPABILITY"
    assert outcome["state"]["reason_codes"] == [
        "FINAL_MEDIA_REVIEW_RAW_AV_CAPABILITY_MISSING"
    ]
    assert outcome["provider_called"] is False
    assert outcome["state"]["runtime_binding"] is None
    assert calls["builder"] == 0


def _attach_integrity_and_clock_evidence(
    tmp_path: Path,
    job: dict[str, object],
    *,
    decoded_frame_count: int = 50,
) -> None:
    manifest_path = Path(job["asset_manifest"]["path"])
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    source = manifest["source_video"]
    duration_us = job["media_clock"]["duration_us"]
    evidence = {
        "scope": {
            "manifest_sha256": _sha(manifest_path),
            "review_status": "input_integrity_and_timing_only",
            "perceptual_content_review": "not_performed",
        },
        "source_bindings": {
            "source_video": {
                "actual_sha256": source["sha256"],
                "actual_bytes": source["bytes"],
                "sha256_match": True,
                "bytes_match": True,
                "regular_nonlinked": True,
                "ffprobe_format": {
                    "duration": f"{duration_us / 1_000_000:.6f}",
                },
                "decoded_frame_bounds": {
                    "decoded_frame_count": decoded_frame_count,
                    "first_best_effort_timestamp_seconds": (
                        job["media_clock"]["first_video_pts_us"] / 1_000_000
                    ),
                    "last_best_effort_timestamp_seconds": (
                        job["media_clock"]["last_video_pts_us"] / 1_000_000
                    ),
                },
            }
        },
    }
    evidence_path = tmp_path / "clock-evidence.json"
    _write_json(evidence_path, evidence)
    receipt = {
        "source_manifest_sha256": _sha(manifest_path),
        "verified_assets": len(manifest["artifacts"]),
        "source_video_sha256": job["media_clock"]["source_video_sha256"],
        "duration_seconds": duration_us / 1_000_000,
        "validation_file": str(evidence_path.resolve()),
        "validation_sha256": _sha(evidence_path),
        "disposition": "ACCEPTED_INPUT_INTEGRITY_AND_TIMING_ONLY",
        "content_review_passed": False,
        "quality_release": False,
        "upload": False,
    }
    receipt_path = tmp_path / "integrity-with-clock.json"
    _write_json(receipt_path, receipt)
    job["integrity_receipt"] = {
        "path": str(receipt_path.resolve()),
        "sha256": _sha(receipt_path),
    }


def test_required_exact_media_clock_evidence_is_hash_bound(tmp_path: Path):
    _path, job = _job(
        tmp_path,
        windows=[("all", 0, 2_000_000, 2_000_000)],
        provider_audio=True,
        provider_video=True,
    )
    job["requirements"]["exact_media_clock_evidence_required"] = True
    _attach_integrity_and_clock_evidence(tmp_path, job)

    value = assess_review_inputs(job)

    assert value["status"] == "READY_FOR_PERCEPTUAL_REVIEW"
    clock = value["integrity"]["exact_media_clock_evidence"]
    assert clock["duration_us"] == 2_000_000
    assert clock["video_frame_count"] == 50
    assert clock["first_video_pts_us"] == 0
    assert clock["last_video_pts_us"] == 1_960_000
    assert clock["content_review_proved"] is False


def test_required_exact_media_clock_evidence_missing_blocks_without_provider(
    tmp_path: Path,
):
    _path, job = _job(
        tmp_path,
        windows=[("all", 0, 2_000_000, 2_000_000)],
        provider_audio=True,
        provider_video=True,
    )
    job["requirements"]["exact_media_clock_evidence_required"] = True

    value = assess_review_inputs(job)

    assert value["status"] == "BLOCKED_INPUT"
    assert value["content_review_status"] == "UNASSESSED"
    assert value["reason_codes"] == [
        "FINAL_MEDIA_REVIEW_CLOCK_EVIDENCE_MISSING"
    ]


def test_exact_media_clock_evidence_rejects_frame_count_drift(tmp_path: Path):
    _path, job = _job(
        tmp_path,
        windows=[("all", 0, 2_000_000, 2_000_000)],
        provider_audio=True,
        provider_video=True,
    )
    job["requirements"]["exact_media_clock_evidence_required"] = True
    _attach_integrity_and_clock_evidence(
        tmp_path, job, decoded_frame_count=51
    )

    with pytest.raises(
        FinalMediaReviewInputError,
        match="FINAL_MEDIA_REVIEW_CLOCK_EVIDENCE_INVALID",
    ):
        assess_review_inputs(job)


def test_crash_after_durable_dispatch_does_not_repeat_executor(tmp_path: Path):
    job_path, _ = _job(
        tmp_path,
        windows=[("all", 0, 2_000_000, 2_000_000)],
        provider_audio=True,
        provider_video=True,
    )
    runtime = tmp_path / "runtime-crash"
    runtime.mkdir()
    env_path = runtime / "cpa.env"
    env_path.write_text(
        "CPA_BASE_URL=https://cpa.invalid/v1\nCPA_API_KEY=secret-for-test-only\n",
        encoding="utf-8",
    )
    env_path.chmod(0o600)
    calls = {"executor": 0}

    def final_call_builder(**_kwargs):
        def call(_prompt: str) -> str:
            return "{}"

        call.provider_runtime_binding = {
            "provider_transport": "runtime_cpa",
            "provider_endpoint_host": "cpa.invalid",
            "provider_endpoint_path": "/v1",
            "provider_credential_source": str(env_path),
        }
        return call

    class SimulatedProcessDeath(BaseException):
        pass

    def crashing_executor(*_args):
        calls["executor"] += 1
        raise SimulatedProcessDeath()

    state_path = tmp_path / "crash-state.json"
    with pytest.raises(SimulatedProcessDeath):
        consume_review_job(
            job_path,
            state_path,
            runtime_root=runtime,
            executor=crashing_executor,
            final_review_call_builder=final_call_builder,
        )

    persisted = json.loads(state_path.read_text(encoding="utf-8"))
    assert persisted["status"] == "DISPATCHING"
    assert persisted["attempt_count"] == 1
    assert calls["executor"] == 1

    def forbidden_executor(*_args):
        calls["executor"] += 1
        raise AssertionError("ambiguous dispatch must not be repeated")

    replay = consume_review_job(
        job_path,
        state_path,
        runtime_root=runtime,
        executor=forbidden_executor,
        final_review_call_builder=final_call_builder,
    )

    assert replay["cache_reused"] is True
    assert replay["provider_called"] is None
    assert replay["provider_call_status"] == "AMBIGUOUS_PREVIOUS_DISPATCH"
    assert replay["state"]["status"] == "DISPATCHING"
    assert calls["executor"] == 1


def test_timeout_becomes_ambiguous_and_is_not_retried(tmp_path: Path):
    job_path, _ = _job(
        tmp_path,
        windows=[("all", 0, 2_000_000, 2_000_000)],
        provider_audio=True,
        provider_video=True,
    )
    runtime = tmp_path / "runtime-timeout"
    runtime.mkdir()
    env_path = runtime / "cpa.env"
    env_path.write_text(
        "CPA_BASE_URL=https://cpa.invalid/v1\nCPA_API_KEY=secret-for-test-only\n",
        encoding="utf-8",
    )
    env_path.chmod(0o600)
    calls = {"executor": 0}

    def final_call_builder(**_kwargs):
        def call(_prompt: str) -> str:
            return "{}"

        call.provider_runtime_binding = {
            "provider_transport": "runtime_cpa",
            "provider_endpoint_host": "cpa.invalid",
            "provider_endpoint_path": "/v1",
            "provider_credential_source": str(env_path),
        }
        return call

    def timeout_executor(*_args):
        calls["executor"] += 1
        raise LlmCallError(
            "provider response timed out",
            safe_reason="LLM_COMMAND_TIMEOUT",
            provider_diagnostics={"provider_transport": "runtime_cpa"},
        )

    state_path = tmp_path / "timeout-state.json"
    first = consume_review_job(
        job_path,
        state_path,
        runtime_root=runtime,
        executor=timeout_executor,
        final_review_call_builder=final_call_builder,
    )
    assert first["state"]["status"] == "DISPATCH_AMBIGUOUS"
    assert first["provider_called"] is None
    assert first["provider_call_status"] == "AMBIGUOUS"

    replay = consume_review_job(
        job_path,
        state_path,
        runtime_root=runtime,
        executor=timeout_executor,
        final_review_call_builder=final_call_builder,
    )
    assert replay["cache_reused"] is True
    assert replay["provider_called"] is None
    assert calls["executor"] == 1


def test_terminal_cache_revalidates_current_asset_binding(tmp_path: Path):
    job_path, job = _job(
        tmp_path,
        windows=[("all", 0, 2_000_000, 2_000_000)],
        provider_audio=True,
        provider_video=True,
    )
    runtime = tmp_path / "runtime-revalidate"
    runtime.mkdir()
    env_path = runtime / "cpa.env"
    env_path.write_text(
        "CPA_BASE_URL=https://cpa.invalid/v1\nCPA_API_KEY=secret-for-test-only\n",
        encoding="utf-8",
    )
    env_path.chmod(0o600)

    def final_call_builder(**_kwargs):
        def call(_prompt: str) -> str:
            return "{}"

        call.provider_runtime_binding = {
            "provider_transport": "runtime_cpa",
            "provider_endpoint_host": "cpa.invalid",
            "provider_endpoint_path": "/v1",
            "provider_credential_source": str(env_path),
        }
        return call

    def executor(bound_job, assessment, _llm_call, _semantic_command):
        return {
            "schema_version": RESULT_SCHEMA_VERSION,
            "candidate_id": bound_job["candidate_id"],
            "source_video_sha256": assessment["exact_media_clock"][
                "source_video_sha256"
            ],
            "asset_manifest_sha256": assessment["asset_manifest"]["sha256"],
            "status": "PASS",
            "content_review_status": "PASS",
            "observations": ["synthetic exact-input pass"],
            "transport_evidence": _synthetic_transport_evidence(assessment),
        }

    state_path = tmp_path / "terminal-state.json"
    first = consume_review_job(
        job_path,
        state_path,
        runtime_root=runtime,
        executor=executor,
        final_review_call_builder=final_call_builder,
    )
    assert first["state"]["status"] == "COMPLETE"

    manifest = json.loads(Path(job["asset_manifest"]["path"]).read_text())
    wav_path = Path(
        next(
            row["path"]
            for row in manifest["artifacts"]
            if row.get("kind")
            in {"diagnostic_wav", "exact_full_audio_wav"}
        )
    )
    wav_path.write_bytes(wav_path.read_bytes() + b"drift")

    with pytest.raises(
        FinalMediaReviewInputError,
        match="FINAL_MEDIA_REVIEW_BINDING_DRIFT",
    ):
        consume_review_job(
            job_path,
            state_path,
            runtime_root=runtime,
            executor=executor,
            final_review_call_builder=final_call_builder,
        )


def test_materialization_actions_bind_missing_modalities_without_rebuilding_valid_wav(
    tmp_path: Path,
) -> None:
    _path, job = _job(
        tmp_path,
        windows=[("all", 0, 999_979, 1_000_000)],
        provider_audio=False,
        provider_video=False,
        duration_us=999_979,
    )
    manifest_path = Path(job["asset_manifest"]["path"])
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    audio_artifact = next(
        row
        for row in manifest["artifacts"]
        if row.get("kind") == "diagnostic_wav"
    )
    audio_artifact["kind"] = "exact_full_audio_wav"
    _write_json(manifest_path, manifest)
    job["asset_manifest"]["sha256"] = _sha(manifest_path)

    assessment = assess_review_inputs(job)

    assert assessment["status"] == "BLOCKED_INPUT"
    assert assessment["audio"]["continuous"] is True
    assert assessment["audio"]["actual_gaps"] == []
    assert assessment["audio"]["windows"][0]["delta_us"] == 21
    assert assessment["audio"]["windows"][0]["duration_tolerance_us"] == 63
    assert assessment["reason_codes"] == [
        "FINAL_MEDIA_REVIEW_AUDIO_TRANSPORT_MISSING",
        "FINAL_MEDIA_REVIEW_VISUAL_INPUT_NOT_CONTINUOUS",
    ]
    assert assessment["materialization_actions"] == [
        "BIND_A_TRANSPORT_THAT_CAN_CONSUME_EXACT_SOURCE_VIDEO",
        "BIND_A_TRANSPORT_THAT_CAN_CONSUME_EXACT_AUDIO",
    ]


def test_audio_duration_tolerance_is_exactly_one_output_sample(
    tmp_path: Path,
) -> None:
    _path, one_sample_job = _job(
        tmp_path / "one-sample",
        windows=[("all", 0, 2_000_000, 2_000_063)],
        provider_audio=True,
        provider_video=True,
    )
    one_sample = assess_review_inputs(one_sample_job)
    assert one_sample["status"] == "READY_FOR_PERCEPTUAL_REVIEW"
    assert (
        "FINAL_MEDIA_REVIEW_AUDIO_DURATION_MISMATCH"
        not in one_sample["reason_codes"]
    )
    assert one_sample["audio"]["windows"][0]["delta_us"] == 63
    assert (
        one_sample["audio"]["windows"][0]["duration_tolerance_us"]
        == 63
    )
    assert (
        "REBUILD_OR_REDECLARE_WAV_WINDOWS_FROM_EXACT_CLOCK"
        not in one_sample["materialization_actions"]
    )

    _path, two_sample_job = _job(
        tmp_path / "two-samples",
        windows=[("all", 0, 2_000_000, 2_000_125)],
        provider_audio=True,
        provider_video=True,
    )
    two_samples = assess_review_inputs(two_sample_job)
    assert two_samples["status"] == "BLOCKED_INPUT"
    assert (
        "FINAL_MEDIA_REVIEW_AUDIO_DURATION_MISMATCH"
        in two_samples["reason_codes"]
    )
    assert two_samples["audio"]["windows"][0]["delta_us"] == 125
    assert (
        two_samples["audio"]["windows"][0]["duration_tolerance_us"]
        == 63
    )
    assert (
        "REBUILD_OR_REDECLARE_WAV_WINDOWS_FROM_EXACT_CLOCK"
        in two_samples["materialization_actions"]
    )


def test_transport_evidence_accepts_nearest_sample_eos_for_generic_provider(
    tmp_path: Path,
) -> None:
    _path, one_sample_job = _job(
        tmp_path / "generic-one-sample",
        windows=[("all", 0, 2_000_000, 2_000_063)],
        provider_audio=True,
        provider_video=True,
        provider="gemini_web_subscription",
    )
    one_sample = assess_review_inputs(one_sample_job)
    assert one_sample["status"] == "READY_FOR_PERCEPTUAL_REVIEW"
    result = {
        "transport_evidence": _synthetic_transport_evidence(one_sample)
    }

    validate_transport_evidence(result, one_sample)
    assert result["transport_evidence"]["provider"] == (
        "gemini_web_subscription"
    )

    _path, two_sample_job = _job(
        tmp_path / "generic-two-samples",
        windows=[("all", 0, 2_000_000, 2_000_125)],
        provider_audio=True,
        provider_video=True,
        provider="gemini_web_subscription",
    )
    two_samples = assess_review_inputs(two_sample_job)
    assert two_samples["status"] == "BLOCKED_INPUT"
    with pytest.raises(
        FinalMediaReviewTransportEvidenceError,
        match="one exact full-media WAV binding",
    ):
        validate_transport_evidence(
            {"transport_evidence": _synthetic_transport_evidence(two_samples)},
            two_samples,
        )
