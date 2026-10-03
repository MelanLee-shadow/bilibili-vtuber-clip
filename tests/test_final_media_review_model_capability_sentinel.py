from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import wave

import pytest

from scripts.run_final_media_review_model_capability_sentinel import (
    main as sentinel_cli_main,
)
from src.autoslice.final_media_review_model_capability_sentinel import (
    SENTINEL_ADAPTER_ERROR_SCHEMA_VERSION,
    SENTINEL_RUNTIME_FILENAME,
    SENTINEL_RUNTIME_SCHEMA_VERSION,
    FinalMediaReviewModelCapabilitySentinelError,
    run_model_capability_sentinel,
)
from src.autoslice.final_media_review_model_capability_seal import (
    SENTINEL_RESPONSE_SCHEMA_VERSION,
)
from src.autoslice.final_media_review_raw_av import (
    RUNTIME_CAPABILITY_FILENAME,
    load_runtime_raw_av_capability,
)


AUDIO_TOKEN = "12345678"
VIDEO_TOKEN = "87654321"
START = datetime.now(timezone.utc).replace(microsecond=0)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_json(path: Path, value: object, *, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    path.chmod(mode)


def _runtime(
    tmp_path: Path,
    *,
    provider: str = "cpa",
    model: str = "synthetic-dual-media-model",
) -> tuple[Path, Path, Path]:
    root = tmp_path / "runtime"
    root.mkdir(mode=0o700)
    runner = root / "repo" / "scripts" / "run-sentinel.py"
    adapter = root / "repo" / "scripts" / "dual-media-adapter.py"
    runner.parent.mkdir(parents=True, mode=0o700)
    runner.write_text("#!/usr/bin/env python3\n# bound runner identity\n", encoding="utf-8")
    adapter.write_text("#!/usr/bin/env python3\n# bound adapter identity\n", encoding="utf-8")
    runner.chmod(0o700)
    adapter.chmod(0o700)
    ffmpeg = Path(shutil.which("ffmpeg") or "").resolve(strict=True)
    ffprobe = Path(shutil.which("ffprobe") or "").resolve(strict=True)
    manifest = {
        "schema_version": SENTINEL_RUNTIME_SCHEMA_VERSION,
        "capability_id": "synthetic-dual-media",
        "provider": provider,
        "transport": "content_bound_command",
        "model": model,
        "endpoint_family": "synthetic_dual_media",
        "runner": {
            "path": runner.relative_to(root).as_posix(),
            "sha256": _sha(runner),
        },
        "adapter": {
            "path": adapter.relative_to(root).as_posix(),
            "sha256": _sha(adapter),
        },
        "sentinel_argv": [
            "{executable}",
            "--request",
            "{request_json}",
            "--response",
            "{response_json}",
            "--input",
            "{input_media}",
            "--prompt",
            "{prompt_file}",
        ],
        "final_review_argv": [
            "{executable}",
            "--request",
            "{request_json}",
            "--response",
            "{response_json}",
        ],
        "timeout_seconds": 30,
        "ffmpeg": {"path": str(ffmpeg), "sha256": _sha(ffmpeg)},
        "ffprobe": {"path": str(ffprobe), "sha256": _sha(ffprobe)},
        "backoff": {
            "base_seconds": 10,
            "max_seconds": 60,
            "max_rate_limit_attempts": 3,
        },
        "result_schema_version": "final-media-perceptual-review-result.v1",
    }
    _write_json(root / SENTINEL_RUNTIME_FILENAME, manifest)
    return root, runner, adapter


def _tokens(modality: str) -> str:
    return AUDIO_TOKEN if modality == "raw_audio" else VIDEO_TOKEN


class SyntheticAdapter:
    def __init__(self, modes: list[str] | None = None):
        self.modes = list(modes or [])
        self.calls: list[dict[str, object]] = []

    def __call__(self, argv: list[str], **_kwargs) -> subprocess.CompletedProcess[str]:
        options = {argv[index]: Path(argv[index + 1]) for index in range(1, len(argv) - 1, 2)}
        request_path = options["--request"]
        response_path = options["--response"]
        input_path = options["--input"]
        prompt_path = options["--prompt"]
        request = json.loads(request_path.read_text(encoding="utf-8"))
        mode = self.modes.pop(0) if self.modes else "pass"
        self.calls.append(
            {
                "coverage": request["coverage"],
                "mode": mode,
                "request": str(request_path),
                "response": str(response_path),
                "input": str(input_path),
                "prompt": str(prompt_path),
                "argv": list(argv),
            }
        )
        request_sha = _sha(request_path)
        if mode == "no-response":
            return subprocess.CompletedProcess(argv, 0, "", "")
        if mode == "rate-limit":
            _write_json(
                response_path,
                {
                    "schema_version": SENTINEL_ADAPTER_ERROR_SCHEMA_VERSION,
                    "request_sha256": request_sha,
                    "error": {
                        "safe_reason": "LLM_HTTP_429",
                        "provider_http_status": 429,
                        "message": "synthetic rate limit",
                    },
                },
            )
            return subprocess.CompletedProcess(argv, 75, "", "")
        token = (
            AUDIO_TOKEN
            if request["coverage"] == "RAW_AUDIO_ONLY_HIDDEN_NONCE"
            else VIDEO_TOKEN
        )
        if mode == "wrong-answer":
            token = "00000000"
        _write_json(
            response_path,
            {
                "schema_version": SENTINEL_RESPONSE_SCHEMA_VERSION,
                "challenge_id": request["challenge_id"],
                "request_sha256": request_sha,
                "observed_answer": token,
                "observed_answer_sha256": hashlib.sha256(
                    token.encode("utf-8")
                ).hexdigest(),
            },
        )
        return subprocess.CompletedProcess(argv, 0, "", "")


def _run(
    root: Path,
    runner: Path,
    adapter: SyntheticAdapter,
    *,
    run_id: str = "sentinel-run-1",
    now: datetime = START,
    failpoint: str | None = None,
    valid_for_seconds: int = 86_400,
) -> dict[str, object]:
    return run_model_capability_sentinel(
        runtime_root=root,
        run_id=run_id,
        valid_for_seconds=valid_for_seconds,
        runner_path=runner,
        now=now,
        environment={"PATH": str(Path(sys.executable).parent)},
        run=adapter,
        token_factory=_tokens,
        failpoint=failpoint,
    )


def test_runner_generates_real_media_seals_and_bootstraps(tmp_path: Path):
    root, runner, _adapter_path = _runtime(tmp_path)
    adapter = SyntheticAdapter()

    first = _run(root, runner, adapter)

    assert first["status"] == "CAPABILITY_BOOTSTRAPPED"
    assert first["provider_calls"] == 2
    assert [row["coverage"] for row in adapter.calls] == [
        "RAW_AUDIO_ONLY_HIDDEN_NONCE",
        "CONTINUOUS_SOURCE_VIDEO_HIDDEN_SEQUENCE",
    ]
    run_root = Path(first["run_root"])
    audio = run_root / "raw_audio" / "challenge.wav"
    video = run_root / "continuous_source_video" / "challenge.mp4"
    with wave.open(str(audio), "rb") as handle:
        assert handle.getnchannels() == 1
        assert handle.getframerate() == 16_000
        assert handle.getnframes() > 50_000
    assert video.read_bytes()[4:8] == b"ftyp"
    plan = json.loads((run_root / "plan.json").read_text(encoding="utf-8"))
    video_receipt = plan["challenges"]["continuous_source_video"][
        "media_receipt"
    ]
    assert video_receipt["panel_count"] == 4
    assert video_receipt["panel_seconds"] == pytest.approx(0.4)
    assert 1_400_000 <= video_receipt["duration_us"] <= 1_800_000
    assert video_receipt["video_frame_count"] is None or (
        video_receipt["video_frame_count"] >= 14
    )
    for modality, token in (
        ("raw_audio", AUDIO_TOKEN),
        ("continuous_source_video", VIDEO_TOKEN),
    ):
        challenge = run_root / modality
        for name in ("prompt.txt", "request.json", "secret-commitment.json"):
            assert token.encode("utf-8") not in (challenge / name).read_bytes()
    capability = load_runtime_raw_av_capability(root)
    assert capability is not None
    assert capability["model"] == "synthetic-dual-media-model"
    assert capability["accepts"] == {
        "raw_audio": True,
        "continuous_source_video": True,
    }
    assert Path(root / RUNTIME_CAPABILITY_FILENAME).is_file()

    second = _run(
        root,
        runner,
        adapter,
        now=START + timedelta(minutes=1),
    )
    assert second["status"] == "CAPABILITY_BOOTSTRAPPED"
    assert second["provider_calls"] == 0
    assert second["plan_reused"] is True
    assert second["result_reused"] is True
    assert second["seal_reused"] is True
    assert second["capability_reused"] is True
    assert len(adapter.calls) == 2


def test_known_unsupported_model_is_provider_free(tmp_path: Path):
    root, runner, _adapter_path = _runtime(tmp_path, model="gpt-6.1-sol")
    adapter = SyntheticAdapter()

    with pytest.raises(
        FinalMediaReviewModelCapabilitySentinelError,
        match="FINAL_MEDIA_REVIEW_MODEL_MODALITY_UNSUPPORTED",
    ):
        _run(root, runner, adapter)

    assert adapter.calls == []
    assert not (root / "model-capability-sentinel-runs").exists()
    assert not (root / RUNTIME_CAPABILITY_FILENAME).exists()


def test_rate_limit_backoff_resumes_without_repeating_success(tmp_path: Path):
    root, runner, _adapter_path = _runtime(tmp_path)
    adapter = SyntheticAdapter(["rate-limit", "pass", "pass"])

    first = _run(root, runner, adapter)
    assert first["status"] == "RETRY_WAIT"
    assert first["provider_calls"] == 1
    assert first["challenge_states"]["raw_audio"]["attempt_count"] == 1
    assert "continuous_source_video" not in first["challenge_states"]

    early = _run(root, runner, adapter, now=START + timedelta(seconds=5))
    assert early["status"] == "RETRY_WAIT"
    assert early["provider_calls"] == 0
    assert len(adapter.calls) == 1

    final = _run(root, runner, adapter, now=START + timedelta(seconds=11))
    assert final["status"] == "CAPABILITY_BOOTSTRAPPED"
    assert final["provider_calls"] == 2
    assert final["challenge_states"]["raw_audio"]["attempt_count"] == 2
    assert final["challenge_states"]["continuous_source_video"]["attempt_count"] == 1
    assert len(adapter.calls) == 3


def test_dispatching_crash_never_replays_unknown_call(tmp_path: Path):
    root, runner, _adapter_path = _runtime(tmp_path)
    adapter = SyntheticAdapter()

    with pytest.raises(RuntimeError, match="TEST_FAILPOINT_AFTER_DISPATCHING_raw_audio"):
        _run(root, runner, adapter, failpoint="after_dispatching:raw_audio")
    assert adapter.calls == []

    resumed = _run(root, runner, adapter, now=START + timedelta(seconds=1))
    assert resumed["status"] == "DISPATCH_AMBIGUOUS"
    assert resumed["provider_calls"] == 0
    assert resumed["reason_code"] == "SENTINEL_DISPATCH_OUTCOME_UNKNOWN"
    assert adapter.calls == []
    assert not (root / RUNTIME_CAPABILITY_FILENAME).exists()


def test_response_written_crash_recovers_without_duplicate_call(tmp_path: Path):
    root, runner, _adapter_path = _runtime(tmp_path)
    adapter = SyntheticAdapter()

    with pytest.raises(
        RuntimeError, match="TEST_FAILPOINT_AFTER_ADAPTER_RESPONSE_raw_audio"
    ):
        _run(root, runner, adapter, failpoint="after_adapter_response:raw_audio")
    assert len(adapter.calls) == 1

    resumed = _run(root, runner, adapter, now=START + timedelta(seconds=1))
    assert resumed["status"] == "CAPABILITY_BOOTSTRAPPED"
    assert resumed["provider_calls"] == 1
    assert len(adapter.calls) == 2
    assert [row["coverage"] for row in adapter.calls] == [
        "RAW_AUDIO_ONLY_HIDDEN_NONCE",
        "CONTINUOUS_SOURCE_VIDEO_HIDDEN_SEQUENCE",
    ]


def test_wrong_answer_blocks_seal_and_capability(tmp_path: Path):
    root, runner, _adapter_path = _runtime(tmp_path)
    adapter = SyntheticAdapter(["wrong-answer"])

    result = _run(root, runner, adapter)

    assert result["status"] == "FAILED"
    assert result["reason_code"] == "SENTINEL_ANSWER_MISMATCH"
    assert result["provider_calls"] == 1
    assert not (root / RUNTIME_CAPABILITY_FILENAME).exists()
    assert not (root / "model-capability-seals").exists()


def test_invoked_runner_mismatch_is_provider_free(tmp_path: Path):
    root, _runner, _adapter_path = _runtime(tmp_path)
    other = root / "repo" / "scripts" / "other-runner.py"
    other.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
    other.chmod(0o700)
    adapter = SyntheticAdapter()

    with pytest.raises(
        FinalMediaReviewModelCapabilitySentinelError,
        match="FINAL_MEDIA_REVIEW_SENTINEL_RUNNER_MISMATCH",
    ):
        _run(root, other, adapter)

    assert adapter.calls == []
    assert not (root / "model-capability-sentinel-runs").exists()


def test_missing_response_is_ambiguous_and_not_retried(tmp_path: Path):
    root, runner, _adapter_path = _runtime(tmp_path)
    adapter = SyntheticAdapter(["no-response"])

    first = _run(root, runner, adapter)
    assert first["status"] == "DISPATCH_AMBIGUOUS"
    assert first["provider_calls"] == 1

    second = _run(root, runner, adapter, now=START + timedelta(minutes=1))
    assert second["status"] == "DISPATCH_AMBIGUOUS"
    assert second["provider_calls"] == 0
    assert len(adapter.calls) == 1


def test_cli_rejects_known_unsupported_model_without_provider(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    root, _runner, _adapter_path = _runtime(tmp_path, model="gpt-6-sol")

    rc = sentinel_cli_main(
        [
            "--runtime-root",
            str(root),
            "--run-id",
            "unsupported-cli-run",
            "--valid-for-seconds",
            "86400",
        ]
    )
    captured = capsys.readouterr()

    assert rc == 2
    assert captured.out == ""
    error = json.loads(captured.err)
    assert error["reason_code"] == "FINAL_MEDIA_REVIEW_MODEL_MODALITY_UNSUPPORTED"
    assert not (root / "model-capability-sentinel-runs").exists()
    assert not (root / RUNTIME_CAPABILITY_FILENAME).exists()


def test_missing_runtime_cli_is_typed_retry_without_side_effect(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    root = tmp_path / "empty-runtime"
    root.mkdir(mode=0o700)

    rc = sentinel_cli_main(
        [
            "--runtime-root",
            str(root),
            "--run-id",
            "runtime-absent-run",
            "--valid-for-seconds",
            "86400",
        ]
    )
    captured = capsys.readouterr()
    result = json.loads(captured.out)

    assert rc == 75
    assert captured.err == ""
    assert result == {
        "provider_calls": 0,
        "run_id": "runtime-absent-run",
        "status": "SENTINEL_RUNTIME_ABSENT",
    }
    assert list(root.iterdir()) == [root / ".model-capability-sentinel.lock"]


def test_invalid_provider_slot_configuration_is_provider_free(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    root, runner, _adapter_path = _runtime(tmp_path)
    adapter = SyntheticAdapter()
    monkeypatch.setenv("AUTOSLICE_PROVIDER_CONCURRENCY", "0")

    result = _run(root, runner, adapter)

    assert result["status"] == "PREPARED"
    assert result["reason_code"] == "SENTINEL_PROVIDER_CAPACITY_UNAVAILABLE"
    assert result["provider_calls"] == 0
    assert adapter.calls == []
    assert not (root / RUNTIME_CAPABILITY_FILENAME).exists()


def test_runtime_rejects_embedded_placeholder_before_media_or_provider(
    tmp_path: Path
):
    root, runner, _adapter_path = _runtime(tmp_path)
    manifest_path = root / SENTINEL_RUNTIME_FILENAME
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["sentinel_argv"][2] = "--request={request_json}"
    _write_json(manifest_path, manifest)
    adapter = SyntheticAdapter()

    with pytest.raises(
        FinalMediaReviewModelCapabilitySentinelError,
        match="FINAL_MEDIA_REVIEW_SENTINEL_RUNTIME_INVALID",
    ):
        _run(root, runner, adapter)

    assert adapter.calls == []
    assert not (root / "model-capability-sentinel-runs").exists()


def test_new_successful_run_atomically_rotates_only_seal_binding(
    tmp_path: Path,
):
    root, runner, _adapter_path = _runtime(tmp_path)
    adapter = SyntheticAdapter()

    first = _run(
        root,
        runner,
        adapter,
        run_id="rotation-run-1",
        valid_for_seconds=60,
    )
    capability_path = root / RUNTIME_CAPABILITY_FILENAME
    first_bytes = capability_path.read_bytes()
    first_sha = hashlib.sha256(first_bytes).hexdigest()

    second = _run(
        root,
        runner,
        adapter,
        run_id="rotation-run-2",
        now=START + timedelta(seconds=61),
        valid_for_seconds=60,
    )

    assert first["status"] == second["status"] == "CAPABILITY_BOOTSTRAPPED"
    assert second["capability_reused"] is False
    archive = Path(second["archived_capability_path"])
    assert archive.name == f"{first_sha}.json"
    assert archive.read_bytes() == first_bytes
    assert capability_path.read_bytes() != first_bytes
    first_doc = json.loads(first_bytes)
    second_doc = json.loads(capability_path.read_text(encoding="utf-8"))
    first_identity = {**first_doc, "model_capability_seal": None}
    second_identity = {**second_doc, "model_capability_seal": None}
    assert first_identity == second_identity
    assert first_doc["model_capability_seal"] != second_doc["model_capability_seal"]
    assert load_runtime_raw_av_capability(
        root, now=START + timedelta(seconds=61)
    ) is not None


def test_sentinel_bootstraps_non_cpa_provider_identity(tmp_path: Path):
    root, runner, _adapter_path = _runtime(
        tmp_path, provider="gemini_web_subscription"
    )
    adapter = SyntheticAdapter()

    result = _run(root, runner, adapter)

    assert result["status"] == "CAPABILITY_BOOTSTRAPPED"
    capability = load_runtime_raw_av_capability(root)
    assert capability is not None
    assert capability["provider"] == "gemini_web_subscription"
    assert capability["accepts"] == {
        "raw_audio": True,
        "continuous_source_video": True,
    }
