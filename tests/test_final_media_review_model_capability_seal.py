from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from scripts.seal_final_media_review_model_capability import main as seal_cli_main
from src.autoslice.final_media_review_model_capability_seal import (
    ATTESTATION_FILENAME,
    SEAL_RECEIPT_FILENAME,
    FinalMediaReviewModelCapabilitySealError,
    seal_model_capability_attestation,
    validate_model_capability_seal_receipt,
)
from tests.final_media_review_test_support import write_synthetic_sentinel_run


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _fixture(tmp_path: Path) -> tuple[Path, Path, dict[str, object]]:
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    adapter = runtime / "raw-av-adapter.py"
    adapter.write_text("#!/usr/bin/env python3\n# synthetic raw-av adapter\n", encoding="utf-8")
    adapter.chmod(0o700)
    run = write_synthetic_sentinel_run(runtime, adapter)
    return runtime, adapter, run


def _seal(runtime: Path, adapter: Path, run: dict[str, object], **extra):
    return seal_model_capability_attestation(
        runtime_root=runtime,
        sentinel_result_path=run["result_path"],
        target_executable_path=adapter,
        output_directory=runtime / "sealed-capability",
        valid_for_seconds=86_400,
        **extra,
    )


def test_sealer_is_create_only_and_idempotent(tmp_path: Path):
    runtime, adapter, run = _fixture(tmp_path)

    first = _seal(runtime, adapter, run)
    receipt = runtime / "sealed-capability" / SEAL_RECEIPT_FILENAME
    attestation = runtime / "sealed-capability" / ATTESTATION_FILENAME
    before = (_sha(receipt), _sha(attestation), receipt.read_bytes(), attestation.read_bytes())
    second = _seal(runtime, adapter, run)

    assert first["status"] == "SEALED_MODEL_CAPABILITY"
    assert first["cache_reused"] is False
    assert first["provider_calls"] == 0
    assert second["cache_reused"] is True
    assert second["provider_calls"] == 0
    assert (_sha(receipt), _sha(attestation), receipt.read_bytes(), attestation.read_bytes()) == before
    assert receipt.stat().st_mode & 0o777 == 0o600
    assert attestation.stat().st_mode & 0o777 == 0o600


def test_sealer_recovers_exact_attestation_only_crash(tmp_path: Path):
    runtime, adapter, run = _fixture(tmp_path)

    with pytest.raises(RuntimeError, match="TEST_FAILPOINT_AFTER_ATTESTATION"):
        _seal(runtime, adapter, run, failpoint="after_attestation")
    output = runtime / "sealed-capability"
    attestation = output / ATTESTATION_FILENAME
    receipt = output / SEAL_RECEIPT_FILENAME
    attestation_before = attestation.read_bytes()
    assert not receipt.exists()

    recovered = _seal(runtime, adapter, run)

    assert recovered["recovered_after_attestation_only"] is True
    assert recovered["cache_reused"] is False
    assert attestation.read_bytes() == attestation_before
    assert receipt.is_file()


def test_sealer_rejects_expected_hash_leak_before_output(tmp_path: Path):
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    adapter = runtime / "adapter.py"
    adapter.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
    adapter.chmod(0o700)
    run = write_synthetic_sentinel_run(runtime, adapter, leak_answer=True)

    with pytest.raises(
        FinalMediaReviewModelCapabilitySealError,
        match="FINAL_MEDIA_REVIEW_MODEL_CAPABILITY_SENTINEL_INVALID",
    ):
        _seal(runtime, adapter, run)

    assert not (runtime / "sealed-capability").exists()


def test_sealer_rejects_result_self_hash_drift(tmp_path: Path):
    runtime, adapter, run = _fixture(tmp_path)
    result_path = Path(run["result_path"])
    value = json.loads(result_path.read_text(encoding="utf-8"))
    value["run_id"] = "drifted-run"
    result_path.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")
    result_path.chmod(0o600)

    with pytest.raises(
        FinalMediaReviewModelCapabilitySealError,
        match="FINAL_MEDIA_REVIEW_MODEL_CAPABILITY_SENTINEL_INVALID",
    ):
        _seal(runtime, adapter, run)


def test_sealer_refuses_different_bytes_at_existing_output(tmp_path: Path):
    runtime, adapter, run = _fixture(tmp_path)
    _seal(runtime, adapter, run)
    attestation = runtime / "sealed-capability" / ATTESTATION_FILENAME
    attestation.write_bytes(attestation.read_bytes() + b" ")
    attestation.chmod(0o600)

    with pytest.raises(
        FinalMediaReviewModelCapabilitySealError,
        match="FINAL_MEDIA_REVIEW_MODEL_CAPABILITY_SEAL_CONFLICT",
    ):
        _seal(runtime, adapter, run)


def test_validator_rejects_direct_attestation_binding(tmp_path: Path):
    runtime, adapter, run = _fixture(tmp_path)
    sealed = _seal(runtime, adapter, run)
    attestation = sealed["attestation"]

    with pytest.raises(
        FinalMediaReviewModelCapabilitySealError,
        match="FINAL_MEDIA_REVIEW_MODEL_CAPABILITY_SEAL_INVALID",
    ):
        validate_model_capability_seal_receipt(
            attestation,
            runtime_root=runtime,
            provider="cpa",
            model="synthetic-raw-av-model",
            endpoint_family="synthetic_raw_av",
            executable_sha256=_sha(adapter),
            authority_uid=runtime.stat().st_uid,
        )


def test_seal_cli_uses_normal_create_only_entrypoint(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    runtime, adapter, run = _fixture(tmp_path)
    output = runtime / "cli-sealed-capability"

    first_rc = seal_cli_main(
        [
            "--runtime-root",
            str(runtime),
            "--sentinel-result",
            str(run["result_path"]),
            "--target-executable",
            str(adapter),
            "--output-directory",
            str(output),
            "--valid-for-seconds",
            "86400",
        ]
    )
    first_capture = capsys.readouterr()
    first = json.loads(first_capture.out)
    second_rc = seal_cli_main(
        [
            "--runtime-root",
            str(runtime),
            "--sentinel-result",
            str(run["result_path"]),
            "--target-executable",
            str(adapter),
            "--output-directory",
            str(output),
            "--valid-for-seconds",
            "86400",
        ]
    )
    second_capture = capsys.readouterr()
    second = json.loads(second_capture.out)

    assert first_rc == 0
    assert second_rc == 0
    assert first_capture.err == second_capture.err == ""
    assert first["provider_calls"] == second["provider_calls"] == 0
    assert first["cache_reused"] is False
    assert second["cache_reused"] is True
