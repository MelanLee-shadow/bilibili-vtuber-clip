from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path

from src.autoslice.final_media_review_model_capability import (
    MODEL_CAPABILITY_VERIFICATION_METHOD,
)
from src.autoslice.final_media_review_model_capability_seal import (
    MODEL_CAPABILITY_SENTINEL_AUTHORITY,
    SENTINEL_REQUEST_SCHEMA_VERSION,
    SENTINEL_RESPONSE_SCHEMA_VERSION,
    SENTINEL_RESULT_SCHEMA_VERSION,
    SENTINEL_SECRET_SCHEMA_VERSION,
    seal_model_capability_attestation,
)


def _canonical_sha256(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_json(path: Path, value: object, *, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    path.chmod(mode)


def _binding(path: Path, root: Path) -> dict[str, object]:
    return {
        "path": path.relative_to(root).as_posix(),
        "sha256": _file_sha256(path),
        "bytes": path.stat().st_size,
    }


def package_model_capability_fields() -> dict[str, object]:
    return {
        "model_capability_seal_receipt_file_sha256": "4" * 64,
        "model_capability_seal_receipt_sha256": "5" * 64,
        "model_capability_attestation_file_sha256": "6" * 64,
        "model_capability_attestation_sha256": "7" * 64,
        "model_capability_contract_sha256": "8" * 64,
        "model_capability_sentinel_result_sha256": "9" * 64,
        "model_capability_sentinel_result_self_sha256": "a" * 64,
        "model_capability_sentinel_runner_sha256": "b" * 64,
        "model_capability_verification_method": (
            MODEL_CAPABILITY_VERIFICATION_METHOD
        ),
        "model_capability_expires_at": "2099-01-01T00:00:00Z",
    }


def write_synthetic_sentinel_run(
    runtime_root: Path,
    executable_path: Path,
    *,
    provider: str = "cpa",
    model: str = "synthetic-raw-av-model",
    endpoint_family: str = "synthetic_raw_av",
    completed_at: datetime | None = None,
    audio_matched: bool = True,
    video_matched: bool = True,
    disclosed: bool = False,
    leak_answer: bool = False,
) -> dict[str, object]:
    runtime_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    completed = (completed_at or datetime.now(timezone.utc)).astimezone(timezone.utc)
    started = completed - timedelta(seconds=30)
    run_root = runtime_root / "sentinel-runs" / "synthetic-run"
    run_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    run_root.chmod(0o700)
    runner = runtime_root / "sentinel-runner.py"
    runner.write_text("#!/usr/bin/env python3\n# synthetic sentinel runner\n", encoding="utf-8")
    runner.chmod(0o700)

    def challenge(
        modality: str,
        *,
        answer: str,
        input_bytes: bytes,
        matched: bool,
    ) -> dict[str, object]:
        challenge_id = f"{modality}-hidden-challenge"
        challenge_root = run_root / modality
        challenge_root.mkdir(mode=0o700)
        coverage = {
            "raw_audio": "RAW_AUDIO_ONLY_HIDDEN_NONCE",
            "continuous_source_video": (
                "CONTINUOUS_SOURCE_VIDEO_HIDDEN_SEQUENCE"
            ),
        }[modality]
        input_path = challenge_root / (
            "challenge.wav" if modality == "raw_audio" else "challenge.mp4"
        )
        input_path.write_bytes(input_bytes)
        input_path.chmod(0o600)
        prompt_path = challenge_root / "prompt.txt"
        answer_sha = hashlib.sha256(answer.encode("utf-8")).hexdigest()
        prompt = "Return only the hidden token perceived in the supplied medium."
        if leak_answer:
            prompt += f" The expected token hash is {answer_sha}."
        prompt_path.write_text(prompt + "\n", encoding="utf-8")
        prompt_path.chmod(0o600)
        secret_path = challenge_root / "secret.json"
        secret = {
            "schema_version": SENTINEL_SECRET_SCHEMA_VERSION,
            "challenge_id": challenge_id,
            "input_sha256": _file_sha256(input_path),
            "prompt_sha256": _file_sha256(prompt_path),
            "expected_answer_sha256": answer_sha,
            "created_at": started.isoformat().replace("+00:00", "Z"),
        }
        secret["commitment_sha256"] = _canonical_sha256(secret)
        _write_json(secret_path, secret)
        request_path = challenge_root / "request.json"
        request = {
            "schema_version": SENTINEL_REQUEST_SCHEMA_VERSION,
            "challenge_id": challenge_id,
            "provider": provider,
            "model": model,
            "endpoint_family": endpoint_family,
            "coverage": coverage,
            "input_sha256": _file_sha256(input_path),
            "prompt_sha256": _file_sha256(prompt_path),
            "answer_disclosed_to_adapter": disclosed,
        }
        _write_json(request_path, request)
        observed = answer if matched else f"wrong-{answer}"
        response_path = challenge_root / "response.json"
        _write_json(
            response_path,
            {
                "schema_version": SENTINEL_RESPONSE_SCHEMA_VERSION,
                "challenge_id": challenge_id,
                "request_sha256": _file_sha256(request_path),
                "observed_answer": observed,
                "observed_answer_sha256": hashlib.sha256(
                    observed.encode("utf-8")
                ).hexdigest(),
            },
        )
        return {
            "challenge_id": challenge_id,
            "coverage": coverage,
            "input": _binding(input_path, runtime_root),
            "prompt": _binding(prompt_path, runtime_root),
            "request": _binding(request_path, runtime_root),
            "response": _binding(response_path, runtime_root),
            "secret": _binding(secret_path, runtime_root),
            "expected_answer_sha256": answer_sha,
            "observed_answer_sha256": hashlib.sha256(
                observed.encode("utf-8")
            ).hexdigest(),
            "matched": matched,
            "answer_disclosed_to_adapter": disclosed,
        }

    value: dict[str, object] = {
        "schema_version": SENTINEL_RESULT_SCHEMA_VERSION,
        "status": "COMPLETE",
        "authority": MODEL_CAPABILITY_SENTINEL_AUTHORITY,
        "run_id": "synthetic-run",
        "provider": provider,
        "model": model,
        "endpoint_family": endpoint_family,
        "target_executable_sha256": _file_sha256(executable_path),
        "runner": _binding(runner, runtime_root),
        "started_at": started.isoformat().replace("+00:00", "Z"),
        "completed_at": completed.isoformat().replace("+00:00", "Z"),
        "challenges": {
            "raw_audio": challenge(
                "raw_audio",
                answer="AUDIO-7Q9",
                input_bytes=b"RIFF synthetic hidden audio token AUDIO-7Q9",
                matched=audio_matched,
            ),
            "continuous_source_video": challenge(
                "continuous_source_video",
                answer="VIDEO-K4M",
                input_bytes=b"synthetic continuous frames encoding VIDEO-K4M",
                matched=video_matched,
            ),
        },
    }
    value["result_sha256"] = _canonical_sha256(value)
    result_path = run_root / "result.json"
    _write_json(result_path, value)
    return {
        "runtime_root": runtime_root,
        "run_root": run_root,
        "runner": runner,
        "result_path": result_path,
        "result": value,
    }


def write_model_capability_attestation(
    runtime_root: Path,
    executable_path: Path,
    *,
    provider: str = "cpa",
    model: str = "synthetic-raw-av-model",
    endpoint_family: str = "synthetic_raw_av",
    current_time: datetime | None = None,
    verified_offset: timedelta = -timedelta(minutes=1),
    expires_offset: timedelta = timedelta(days=1),
    audio_matched: bool = True,
    video_matched: bool = True,
    disclosed: bool = False,
    leak_answer: bool = False,
) -> dict[str, object]:
    now = (current_time or datetime.now(timezone.utc)).astimezone(timezone.utc)
    completed = now + verified_offset
    valid_for_seconds = int((now + expires_offset - completed).total_seconds())
    run = write_synthetic_sentinel_run(
        runtime_root,
        executable_path,
        provider=provider,
        model=model,
        endpoint_family=endpoint_family,
        completed_at=completed,
        audio_matched=audio_matched,
        video_matched=video_matched,
        disclosed=disclosed,
        leak_answer=leak_answer,
    )
    sealed = seal_model_capability_attestation(
        runtime_root=runtime_root,
        sentinel_result_path=run["result_path"],
        target_executable_path=executable_path,
        output_directory=runtime_root / "model-capability-seal",
        valid_for_seconds=valid_for_seconds,
        now=completed,
    )
    return dict(sealed["seal_receipt"])
