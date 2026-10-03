from __future__ import annotations

from datetime import timedelta
import hashlib
import json
import os
from pathlib import Path
import wave

import pytest

import scripts.consume_final_media_review_inputs as consumer_cli
from scripts.consume_final_media_review_inputs import main as consume_cli_main
from src.autoslice.final_media_review_inputs import (
    JOB_SCHEMA_VERSION,
    RESULT_SCHEMA_VERSION,
    FinalMediaReviewInputError,
    assess_review_inputs,
    consume_review_job,
)
from tests.final_media_review_test_support import (
    write_model_capability_attestation,
)
from src.autoslice.final_media_review_model_capability_seal import (
    FinalMediaReviewModelCapabilitySealError,
)
from src.autoslice.final_media_review_raw_av import (
    FinalMediaReviewRawAvError,
    RUNTIME_CAPABILITY_FILENAME,
    RUNTIME_CAPABILITY_SCHEMA_VERSION,
    bind_review_job_to_runtime_capability,
    load_runtime_raw_av_capability,
    validate_package_transport_binding,
)


CANDIDATE_ID = "auto_200000_1_2"


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_json(path: Path, value: object, *, mode: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    if mode is not None:
        path.chmod(mode)


def _wav(path: Path, *, duration_us: int = 2_000_000) -> None:
    frames = round(duration_us * 16_000 / 1_000_000)
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(16_000)
        handle.writeframes(b"\0\0" * frames)


def _artifact(path: Path, **extra: object) -> dict[str, object]:
    return {
        "path": str(path.resolve()),
        "sha256": _sha(path),
        "bytes": path.stat().st_size,
        **extra,
    }


def _package(tmp_path: Path) -> tuple[Path, Path, dict[str, object]]:
    root = tmp_path / "package"
    verification = root / "verification"
    verification.mkdir(parents=True)
    video = root / "final.mp4"
    video.write_bytes(b"exact synthetic continuous source video bytes")
    audio = verification / "full-final-media.wav"
    _wav(audio)
    manifest = {
        "schema": "synthetic-review-assets.v1",
        "source_video": _artifact(video),
        "artifacts": [
            _artifact(
                audio,
                kind="exact_full_audio_wav",
                window="full-final-media",
                range_seconds=[0.0, 2.0],
                actual_duration_us=2_000_000,
                sample_rate=16_000,
                sample_frames=32_000,
                source_video_sha256=_sha(video),
            )
        ],
    }
    manifest_path = verification / "review-assets.json"
    _write_json(manifest_path, manifest)
    job: dict[str, object] = {
        "schema_version": JOB_SCHEMA_VERSION,
        "candidate_id": CANDIDATE_ID,
        "asset_manifest": {
            "path": str(manifest_path.resolve()),
            "sha256": _sha(manifest_path),
        },
        "media_clock": {
            "source_video_sha256": _sha(video),
            "source_video_bytes": video.stat().st_size,
            "duration_us": 2_000_000,
            "first_video_pts_us": 0,
            "last_video_pts_us": 1_960_000,
            "video_frame_count": 50,
        },
        "requirements": {
            "continuous_audio_required": True,
            "continuous_visual_required": True,
            "content_review_required": True,
            "exact_media_clock_evidence_required": False,
            "provider_accepts_bound_source_video": False,
            "provider_accepts_bound_audio": False,
        },
        "review_plan": {
            "authority": "SYNTHETIC_TEST_ONLY",
            "review_points": [
                {
                    "point_id": "whole-final-media",
                    "final_video_start_ms": 0,
                    "final_video_end_ms": 2_000,
                    "expectation": "inspect exact final audio and video",
                }
            ],
        },
    }
    job_path = verification / f"{CANDIDATE_ID}.final-media-review-job.json"
    _write_json(job_path, job)
    return root, job_path, job


def _adapter_script() -> str:
    return r'''#!/usr/bin/env python3
import argparse
import hashlib
import json
import os
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("--request", required=True)
parser.add_argument("--response", required=True)
args = parser.parse_args()
request_path = Path(args.request)
response_path = Path(args.response)
request_bytes = request_path.read_bytes()
request = json.loads(request_bytes)
root = Path(os.environ["AUTOSLICE_BASE"])
capability = json.loads(
    (root / "final-media-review-raw-av-capability.json").read_text()
)

def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()

if sha(request["source_video"]["path"]) != request["source_video"]["sha256"]:
    raise SystemExit(31)
if sha(request["raw_audio"]["path"]) != request["raw_audio"]["sha256"]:
    raise SystemExit(32)
(root / "adapter-calls.txt").open("a", encoding="utf-8").write("call\n")
request_sha = hashlib.sha256(request_bytes).hexdigest()
mode = os.environ.get("RAW_AV_TEST_MODE", "pass")
if mode == "rate-limit":
    response = {
        "schema_version": "final-media-review-raw-av-adapter-response.v3",
        "request_sha256": request_sha,
        "error": {
            "safe_reason": "LLM_HTTP_429",
            "provider_http_status": 429,
            "message": "synthetic rate limit",
        },
    }
    response_path.write_text(json.dumps(response, sort_keys=True) + "\n")
    raise SystemExit(75)
result = {
    "schema_version": "final-media-perceptual-review-result.v1",
    "candidate_id": request["candidate_id"],
    "source_video_sha256": request["source_video"]["sha256"],
    "asset_manifest_sha256": request["asset_manifest_sha256"],
    "status": "PASS",
    "content_review_status": "PASS",
    "observations": [
        {
            "scope": "synthetic-test-only",
            "verdict": "exact request binding observed",
        }
    ],
}
receipt = {
    "schema_version": "final-media-review-raw-av-adapter-receipt.v3",
    "provider": request["provider"],
    "model": request["model"],
    "endpoint_family": request["endpoint_family"],
    "capability_id": request["capability_id"],
    "runtime_capability_sha256": request["runtime_capability_sha256"],
    "executable_sha256": capability["executable"]["sha256"],
    "model_capability_seal_receipt_sha256": request[
        "model_capability_seal_receipt_sha256"
    ],
    "model_capability_attestation_sha256": request[
        "model_capability_attestation_sha256"
    ],
    "model_capability_contract_sha256": request[
        "model_capability_contract_sha256"
    ],
    "model_capability_sentinel_result_sha256": request[
        "model_capability_sentinel_result_sha256"
    ],
    "model_capability_sentinel_result_self_sha256": request[
        "model_capability_sentinel_result_self_sha256"
    ],
    "model_capability_sentinel_runner_sha256": request[
        "model_capability_sentinel_runner_sha256"
    ],
    "request_sha256": request_sha,
    "candidate_id": request["candidate_id"],
    "source_video_sha256": request["source_video"]["sha256"],
    "raw_audio_sha256": request["raw_audio"]["sha256"],
    "consumed_continuous_source_video": True,
    "consumed_raw_audio": True,
}
if mode == "bad-capability-receipt":
    receipt["model_capability_seal_receipt_sha256"] = "0" * 64
response = {
    "schema_version": "final-media-review-raw-av-adapter-response.v3",
    "request_sha256": request_sha,
    "transport_receipt": receipt,
    "result": result,
}
response_path.write_text(json.dumps(response, sort_keys=True) + "\n")
'''


def _runtime(
    tmp_path: Path,
    *,
    provider: str = "cpa",
    model: str = "synthetic-raw-av-model",
    with_attestation: bool = True,
    attestation_kwargs: dict[str, object] | None = None,
) -> Path:
    root = tmp_path / "runtime"
    adapter = root / "repo" / "scripts" / "synthetic_raw_av_adapter.py"
    adapter.parent.mkdir(parents=True)
    adapter.write_text(_adapter_script(), encoding="utf-8")
    adapter.chmod(0o700)
    seal_binding = (
        write_model_capability_attestation(
            root,
            adapter,
            provider=provider,
            model=model,
            **dict(attestation_kwargs or {}),
        )
        if with_attestation
        else None
    )
    capability = {
        "schema_version": RUNTIME_CAPABILITY_SCHEMA_VERSION,
        "capability_id": "synthetic-cpa-raw-av",
        "provider": provider,
        "transport": "content_bound_command",
        "model": model,
        "endpoint_family": "synthetic_raw_av",
        "accepts": {
            "raw_audio": True,
            "continuous_source_video": True,
        },
        "executable": {
            "path": "repo/scripts/synthetic_raw_av_adapter.py",
            "sha256": _sha(adapter),
        },
        "argv": [
            "{executable}",
            "--request",
            "{request_json}",
            "--response",
            "{response_json}",
        ],
        "timeout_seconds": 30,
        "result_schema_version": RESULT_SCHEMA_VERSION,
        "model_capability_seal": seal_binding,
    }
    _write_json(root / RUNTIME_CAPABILITY_FILENAME, capability, mode=0o600)
    return root


def test_known_text_image_model_cannot_self_attest_raw_av(
    tmp_path: Path,
):
    package_root, job_path, _job = _package(tmp_path)
    runtime = _runtime(tmp_path, model="gpt-6-sol")

    with pytest.raises(
        FinalMediaReviewRawAvError,
        match="FINAL_MEDIA_REVIEW_MODEL_MODALITY_UNSUPPORTED",
    ):
        bind_review_job_to_runtime_capability(
            job_path, allowed_root=package_root, runtime_root=runtime
        )

    assert not (runtime / "adapter-calls.txt").exists()
    assert not (
        package_root / "verification" / "final-media-review-capabilities"
    ).exists()


def test_standalone_cli_rejects_unsupported_model_before_state_or_provider(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
):
    package_root, job_path, _job = _package(tmp_path)
    runtime = _runtime(tmp_path, model="gpt-6.1-sol")
    state_path = package_root / "verification" / "unsupported-state.json"

    rc = consume_cli_main(
        [
            "--job",
            str(job_path),
            "--state",
            str(state_path),
            "--runtime-root",
            str(runtime),
        ]
    )
    captured = capsys.readouterr()

    assert rc == 2
    assert captured.out == ""
    assert json.loads(captured.err)["reason_code"] == (
        "FINAL_MEDIA_REVIEW_MODEL_MODALITY_UNSUPPORTED"
    )
    assert not state_path.exists()
    assert not (runtime / "adapter-calls.txt").exists()


def test_unknown_model_without_independent_attestation_is_unverified(
    tmp_path: Path,
):
    package_root, job_path, _job = _package(tmp_path)
    runtime = _runtime(tmp_path, with_attestation=False)

    with pytest.raises(
        FinalMediaReviewRawAvError,
        match="FINAL_MEDIA_REVIEW_MODEL_MODALITY_UNVERIFIED",
    ):
        bind_review_job_to_runtime_capability(
            job_path, allowed_root=package_root, runtime_root=runtime
        )

    assert not (runtime / "adapter-calls.txt").exists()


def test_attestation_requires_both_hidden_sentinels_to_match(
    tmp_path: Path,
):
    with pytest.raises(
        FinalMediaReviewModelCapabilitySealError,
        match="FINAL_MEDIA_REVIEW_MODEL_CAPABILITY_SENTINEL_INVALID",
    ):
        _runtime(tmp_path, attestation_kwargs={"video_matched": False})


def test_expired_model_capability_attestation_is_provider_free(
    tmp_path: Path,
):
    package_root, job_path, _job = _package(tmp_path)
    runtime = _runtime(
        tmp_path,
        attestation_kwargs={
            "verified_offset": -timedelta(days=2),
            "expires_offset": -timedelta(days=1),
        },
    )

    with pytest.raises(
        FinalMediaReviewRawAvError,
        match="FINAL_MEDIA_REVIEW_MODEL_CAPABILITY_ATTESTATION_EXPIRED",
    ):
        bind_review_job_to_runtime_capability(
            job_path, allowed_root=package_root, runtime_root=runtime
        )

    assert not (runtime / "adapter-calls.txt").exists()


def test_model_capability_seal_receipt_hardlink_is_rejected(
    tmp_path: Path,
):
    package_root, job_path, _job = _package(tmp_path)
    runtime = _runtime(tmp_path)
    receipt = (
        runtime
        / "model-capability-seal"
        / "model-capability-seal-receipt.json"
    )
    os.link(receipt, runtime / "seal-receipt-alias.json")

    with pytest.raises(
        FinalMediaReviewRawAvError,
        match="FINAL_MEDIA_REVIEW_MODEL_CAPABILITY_SEAL_PATH_INVALID",
    ):
        bind_review_job_to_runtime_capability(
            job_path, allowed_root=package_root, runtime_root=runtime
        )

    assert not (runtime / "adapter-calls.txt").exists()


def test_model_capability_attestation_hash_drift_is_rejected(
    tmp_path: Path,
):
    package_root, job_path, _job = _package(tmp_path)
    runtime = _runtime(tmp_path)
    attestation = (
        runtime
        / "model-capability-seal"
        / "model-capability-attestation.json"
    )
    attestation.write_text(
        attestation.read_text(encoding="utf-8") + " ", encoding="utf-8"
    )
    attestation.chmod(0o600)

    with pytest.raises(
        FinalMediaReviewRawAvError,
        match="FINAL_MEDIA_REVIEW_MODEL_CAPABILITY_SEAL_BINDING_INVALID",
    ):
        bind_review_job_to_runtime_capability(
            job_path, allowed_root=package_root, runtime_root=runtime
        )

    assert not (runtime / "adapter-calls.txt").exists()


def test_provider_booleans_without_hash_bound_capability_are_rejected(
    tmp_path: Path,
):
    root, _job_path, job = _package(tmp_path)
    job["requirements"]["provider_accepts_bound_source_video"] = True
    job["requirements"]["provider_accepts_bound_audio"] = True

    with pytest.raises(
        FinalMediaReviewInputError,
        match="FINAL_MEDIA_REVIEW_TRANSPORT_CAPABILITY_BINDING_MISSING",
    ):
        assess_review_inputs(job, allowed_root=root)


def test_missing_runtime_capability_is_a_noop_without_package_mutation(
    tmp_path: Path,
):
    package_root, job_path, _job = _package(tmp_path)
    runtime = tmp_path / "empty-runtime"
    runtime.mkdir()

    result = bind_review_job_to_runtime_capability(
        job_path, allowed_root=package_root, runtime_root=runtime
    )

    assert result["status"] == "RUNTIME_RAW_AV_CAPABILITY_ABSENT"
    assert result["active_job_path"] == str(job_path.resolve())
    assert result["binding_path"] is None
    assert not (package_root / "verification" / "final-media-review-capabilities").exists()


def test_unscoped_transport_binding_rejects_symlink(tmp_path: Path):
    package_root, job_path, _job = _package(tmp_path)
    runtime = _runtime(tmp_path)
    bound = bind_review_job_to_runtime_capability(
        job_path, allowed_root=package_root, runtime_root=runtime
    )
    successor = json.loads(Path(bound["active_job_path"]).read_text())
    binding = dict(successor["transport_capability"])
    target = Path(binding["path"])
    alias = package_root / "verification" / "transport-capability-alias.json"
    alias.symlink_to(target)
    binding["path"] = str(alias)

    with pytest.raises(
        FinalMediaReviewRawAvError,
        match="FINAL_MEDIA_REVIEW_TRANSPORT_CAPABILITY_BINDING_INVALID",
    ):
        validate_package_transport_binding(binding, allowed_root=None)


def test_runtime_executable_hash_drift_is_rejected_before_binding(tmp_path: Path):
    package_root, job_path, _job = _package(tmp_path)
    runtime = _runtime(tmp_path)
    adapter = runtime / "repo" / "scripts" / "synthetic_raw_av_adapter.py"
    adapter.write_text(adapter.read_text() + "\n# drift\n", encoding="utf-8")
    adapter.chmod(0o700)

    with pytest.raises(
        FinalMediaReviewRawAvError,
        match="FINAL_MEDIA_REVIEW_RAW_AV_CAPABILITY_INVALID",
    ):
        bind_review_job_to_runtime_capability(
            job_path, allowed_root=package_root, runtime_root=runtime
        )
    assert not (package_root / "verification" / "final-media-review-capabilities").exists()


def test_bound_runtime_consumes_exact_raw_av_and_restart_deduplicates(
    tmp_path: Path,
):
    package_root, job_path, _job = _package(tmp_path)
    runtime = _runtime(tmp_path)
    bound = bind_review_job_to_runtime_capability(
        job_path, allowed_root=package_root, runtime_root=runtime
    )
    assert bound["status"] == "BOUND_RUNTIME_RAW_AV_CAPABILITY"
    assert bound["cache_reused"] is False
    assert bound["assessment"]["status"] == "READY_FOR_PERCEPTUAL_REVIEW"
    state_path = package_root / "verification" / "review-state.json"

    first = consume_review_job(
        bound["active_job_path"],
        state_path,
        runtime_root=runtime,
        allowed_root=package_root,
        environment={
            "PATH": os.environ.get("PATH", ""),
            "AUTOSLICE_PROVIDER_CONCURRENCY": "1",
            "AUTOSLICE_PROVIDER_WAIT_SECONDS": "1",
        },
    )

    assert first["state"]["status"] == "COMPLETE"
    assert first["state"]["attempt_count"] == 1
    assert first["state"]["result"]["status"] == "PASS"
    evidence = first["state"]["result"]["transport_evidence"]
    assert evidence["consumed_raw_audio"] is True
    assert evidence["consumed_continuous_source_video"] is True
    assert first["provider_called"] is True
    assert (runtime / "adapter-calls.txt").read_text().splitlines() == ["call"]

    second = consume_review_job(
        bound["active_job_path"],
        state_path,
        runtime_root=runtime,
        allowed_root=package_root,
    )
    assert second["cache_reused"] is True
    assert second["state"]["state_sha256"] == first["state"]["state_sha256"]
    assert (runtime / "adapter-calls.txt").read_text().splitlines() == ["call"]


def test_standalone_consumer_cli_binds_runtime_and_deduplicates(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
):
    package_root, job_path, _job = _package(tmp_path)
    runtime = _runtime(tmp_path)
    state_path = package_root / "verification" / "cli-review-state.json"
    args = [
        "--job",
        str(job_path),
        "--state",
        str(state_path),
        "--runtime-root",
        str(runtime),
    ]

    first_rc = consume_cli_main(args)
    first_capture = capsys.readouterr()
    first = json.loads(first_capture.out)

    assert first_rc == 0
    assert first_capture.err == ""
    assert first["capability_binding"]["status"] == (
        "BOUND_RUNTIME_RAW_AV_CAPABILITY"
    )
    assert first["state"]["status"] == "COMPLETE"
    assert first["state"]["result"]["status"] == "PASS"
    assert (runtime / "adapter-calls.txt").read_text().splitlines() == ["call"]

    second_rc = consume_cli_main(args)
    second_capture = capsys.readouterr()
    second = json.loads(second_capture.out)

    assert second_rc == 0
    assert second_capture.err == ""
    assert second["cache_reused"] is True
    assert second["state"]["state_sha256"] == first["state"]["state_sha256"]
    assert (runtime / "adapter-calls.txt").read_text().splitlines() == ["call"]


def test_adapter_cannot_replace_model_capability_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    package_root, job_path, _job = _package(tmp_path)
    runtime = _runtime(tmp_path)
    bound = bind_review_job_to_runtime_capability(
        job_path, allowed_root=package_root, runtime_root=runtime
    )
    monkeypatch.setenv("RAW_AV_TEST_MODE", "bad-capability-receipt")

    outcome = consume_review_job(
        bound["active_job_path"],
        package_root / "verification" / "bad-capability-state.json",
        runtime_root=runtime,
        allowed_root=package_root,
    )

    assert outcome["state"]["status"] == "DISPATCH_AMBIGUOUS"
    assert outcome["state"]["attempt_count"] == 1
    assert outcome["state"]["reason_codes"] == [
        "FINAL_MEDIA_REVIEW_PROVIDER_DISPATCH_AMBIGUOUS"
    ]
    assert outcome["provider_called"] is None
    assert outcome["provider_call_status"] == "AMBIGUOUS"
    assert (runtime / "adapter-calls.txt").read_text().splitlines() == ["call"]


def test_raw_av_rate_limit_enters_existing_retry_backoff(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    package_root, job_path, _job = _package(tmp_path)
    runtime = _runtime(tmp_path)
    bound = bind_review_job_to_runtime_capability(
        job_path, allowed_root=package_root, runtime_root=runtime
    )
    monkeypatch.setenv("RAW_AV_TEST_MODE", "rate-limit")

    outcome = consume_review_job(
        bound["active_job_path"],
        package_root / "verification" / "review-state.json",
        runtime_root=runtime,
        allowed_root=package_root,
    )

    assert outcome["state"]["status"] == "RETRY_WAIT"
    assert outcome["state"]["attempt_count"] == 1
    assert outcome["state"]["reason_codes"] == [
        "FINAL_MEDIA_REVIEW_PROVIDER_RATE_LIMITED"
    ]
    assert outcome["state"]["provider_diagnostics"]["provider_http_status"] == 429
    assert outcome["provider_called"] is True
    assert outcome["provider_call_status"] == "RATE_LIMITED_RESPONSE"


def test_capability_loader_returns_exact_nonsecret_identity(tmp_path: Path):
    runtime = _runtime(tmp_path)
    value = load_runtime_raw_av_capability(runtime)
    assert value is not None
    assert value["capability_id"] == "synthetic-cpa-raw-av"
    assert value["accepts"] == {
        "raw_audio": True,
        "continuous_source_video": True,
    }
    assert len(value["runtime_capability_sha256"]) == 64
    assert len(value["command_contract_sha256"]) == 64
    assert len(value["model_capability"]["seal_receipt_sha256"]) == 64
    assert len(value["model_capability"]["sentinel_result_sha256"]) == 64


@pytest.mark.parametrize(
    ("bootstrap_status", "provider_calls", "expected_rc", "provider_call_status"),
    [
        ("RETRY_WAIT", 1, 4, "CAPABILITY_BOOTSTRAP_WAIT"),
        ("DISPATCH_AMBIGUOUS", 1, 3, "CAPABILITY_BOOTSTRAP_STOPPED"),
        ("SENTINEL_RUNTIME_ABSENT", 0, 3, "CAPABILITY_BOOTSTRAP_STOPPED"),
    ],
)
def test_standalone_consumer_stops_before_package_state_when_bootstrap_is_not_ready(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    bootstrap_status: str,
    provider_calls: int,
    expected_rc: int,
    provider_call_status: str,
):
    package_root, job_path, _job = _package(tmp_path)
    runtime = tmp_path / "autobootstrap-runtime"
    runtime.mkdir(mode=0o700)
    state_path = package_root / "verification" / "bootstrap-wait-state.json"

    monkeypatch.setattr(
        consumer_cli,
        "ensure_runtime_raw_av_capability",
        lambda _root: {
            "status": bootstrap_status,
            "provider_calls": provider_calls,
            "reason_code": "SYNTHETIC_BOOTSTRAP_STATUS",
        },
    )

    def unexpected_bind(*_args, **_kwargs):
        raise AssertionError("package binding must wait for capability bootstrap")

    monkeypatch.setattr(
        consumer_cli, "bind_review_job_to_runtime_capability", unexpected_bind
    )
    rc = consumer_cli.main(
        [
            "--job",
            str(job_path),
            "--state",
            str(state_path),
            "--runtime-root",
            str(runtime),
        ]
    )
    captured = capsys.readouterr()
    result = json.loads(captured.out)

    assert rc == expected_rc
    assert captured.err == ""
    assert result["capability_bootstrap"]["status"] == bootstrap_status
    assert result["capability_binding"] is None
    assert result["state"] is None
    assert result["provider_call_status"] == provider_call_status
    assert not state_path.exists()
    assert not (
        package_root / "verification" / "final-media-review-capabilities"
    ).exists()


def test_non_cpa_provider_identity_uses_same_sealed_raw_av_contract(
    tmp_path: Path,
):
    package_root, job_path, _job = _package(tmp_path)
    runtime = _runtime(tmp_path, provider="gemini_web_subscription")

    capability = load_runtime_raw_av_capability(runtime)
    assert capability is not None
    assert capability["provider"] == "gemini_web_subscription"
    bound = bind_review_job_to_runtime_capability(
        job_path, allowed_root=package_root, runtime_root=runtime
    )
    assert bound["assessment"]["status"] == "READY_FOR_PERCEPTUAL_REVIEW"
    state_path = package_root / "verification" / "generic-provider-state.json"
    outcome = consume_review_job(
        bound["active_job_path"],
        state_path,
        runtime_root=runtime,
        allowed_root=package_root,
        environment={
            "PATH": os.environ.get("PATH", ""),
            "AUTOSLICE_PROVIDER_CONCURRENCY": "1",
            "AUTOSLICE_PROVIDER_WAIT_SECONDS": "1",
        },
    )

    assert outcome["state"]["status"] == "COMPLETE"
    evidence = outcome["state"]["result"]["transport_evidence"]
    assert evidence["provider"] == "gemini_web_subscription"
    assert evidence["consumed_raw_audio"] is True
    assert evidence["consumed_continuous_source_video"] is True


def test_astra_is_rejected_before_any_raw_av_provider_call(tmp_path: Path):
    package_root, job_path, _job = _package(tmp_path)
    runtime = _runtime(tmp_path, model="gpt-6-astra")

    with pytest.raises(
        FinalMediaReviewRawAvError,
        match="FINAL_MEDIA_REVIEW_MODEL_MODALITY_UNSUPPORTED",
    ):
        bind_review_job_to_runtime_capability(
            job_path, allowed_root=package_root, runtime_root=runtime
        )

    assert not (runtime / "adapter-calls.txt").exists()
