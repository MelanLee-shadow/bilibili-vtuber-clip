from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess

import pytest

import scripts.final_media_review_gemini_api_adapter as adapter
from src.autoslice.final_media_review_inputs import (
    RESULT_SCHEMA_VERSION,
    consume_review_job,
)
from src.autoslice.final_media_review_raw_av import (
    RUNTIME_CAPABILITY_FILENAME,
    RUNTIME_CAPABILITY_SCHEMA_VERSION,
    bind_review_job_to_runtime_capability,
    build_runtime_raw_av_executor,
)
from src.autoslice.gemini_file_api import (
    GeminiFileApiError,
    GeminiFileCallOutcome,
)
from tests.final_media_review_test_support import (
    write_model_capability_attestation,
)
from tests.test_final_media_review_raw_av import _package as raw_av_package


MODEL = "gemini-3.8-flash"
ENDPOINT = adapter.ENDPOINT_FAMILY
CAPABILITY_ID = "gemini-api-files-dual-media"


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_json(path: Path, value: object, *, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    path.chmod(mode)


def _binding(path: Path, root: Path) -> dict[str, object]:
    return {
        "path": path.relative_to(root).as_posix(),
        "sha256": _sha(path),
        "bytes": path.stat().st_size,
    }


def _runtime(
    tmp_path: Path, *, sentinel: bool
) -> tuple[Path, Path, dict[str, str]]:
    root = tmp_path / "runtime"
    script = root / "repo/scripts/final_media_review_gemini_api_adapter.py"
    helper = root / "repo/src/autoslice/gemini_file_api.py"
    script.parent.mkdir(parents=True)
    helper.parent.mkdir(parents=True)
    script.write_bytes(Path(adapter.__file__).resolve().read_bytes())
    helper.write_bytes(
        Path(__file__).resolve().parents[1]
        .joinpath("src/autoslice/gemini_file_api.py")
        .read_bytes()
    )
    script.chmod(0o700)
    helper.chmod(0o600)
    manifest = {
        "schema_version": (
            "final-media-review-model-capability-sentinel-runtime.v1"
            if sentinel
            else "final-media-review-raw-av-runtime-capability.v3"
        ),
        "capability_id": CAPABILITY_ID,
        "provider": adapter.PROVIDER,
        "transport": "content_bound_command",
        "model": MODEL,
        "endpoint_family": ENDPOINT,
        ("adapter" if sentinel else "executable"): _binding(script, root),
    }
    _write_json(
        root
        / (
            adapter.SENTINEL_RUNTIME_FILENAME
            if sentinel
            else adapter.RAW_AV_RUNTIME_FILENAME
        ),
        manifest,
    )
    return root, script, {
        "AUTOSLICE_BASE": str(root),
        "AUTOSLICE_FINAL_MEDIA_GEMINI_API_MODEL": MODEL,
        "GEMINI_API_KEY": "free-key-for-test",
        "GEMINI_KEY_BACKUP": "paid-must-remain-unused",
    }


class FakeFileRunner:
    def __init__(
        self,
        responses: list[str],
        *,
        error: GeminiFileApiError | None = None,
    ):
        self.responses = list(responses)
        self.error = error
        self.calls: list[dict[str, object]] = []

    def __call__(self, **kwargs: object) -> GeminiFileCallOutcome:
        self.calls.append(dict(kwargs))
        if self.error is not None:
            raise self.error
        if not self.responses:
            raise AssertionError("unexpected provider replay")
        text = self.responses.pop(0)
        return GeminiFileCallOutcome(
            text=text,
            evidence={
                "provider": adapter.PROVIDER,
                "model": kwargs["model"],
                "media_sha256": kwargs["expected_sha256"],
                "media_bytes": kwargs["expected_bytes"],
                "mime_type": kwargs["mime_type"],
                "video_fps": kwargs["video_fps"],
                "resource_name_sha256": "1" * 64,
                "file_uri_sha256": "2" * 64,
                "active_state": "ACTIVE",
                "poll_count": 0,
                "http_request_count": 4,
                "response_payload_sha256": "3" * 64,
                "response_text_sha256": hashlib.sha256(
                    text.encode("utf-8")
                ).hexdigest(),
                "cleanup_status": "DELETED",
                "key_tier": "free",
                "key_name": "GEMINI_API_KEY",
                "key_ordinal": 1,
                "configured_free_key_count": 1,
                "prior_failures": [],
                "paid_backup_used": False,
            },
        )


def _sentinel_fixture(
    tmp_path: Path,
) -> tuple[Path, Path, Path, Path, Path, Path, dict[str, str]]:
    root, script, environment = _runtime(tmp_path, sentinel=True)
    media = tmp_path / "challenge.wav"
    media.write_bytes(b"RIFF exact hidden challenge bytes")
    prompt = tmp_path / "prompt.txt"
    prompt.write_text("Return only the eight digits.\n", encoding="utf-8")
    request = {
        "schema_version": adapter.SENTINEL_REQUEST_SCHEMA,
        "challenge_id": "raw-audio-challenge-1",
        "provider": adapter.PROVIDER,
        "model": MODEL,
        "endpoint_family": ENDPOINT,
        "coverage": "RAW_AUDIO_ONLY_HIDDEN_NONCE",
        "input_sha256": _sha(media),
        "prompt_sha256": _sha(prompt),
        "answer_disclosed_to_adapter": False,
    }
    request_path = tmp_path / "request.json"
    _write_json(request_path, request)
    response_path = tmp_path / "response.json"
    return root, script, request_path, response_path, media, prompt, environment


def _final_fixture(
    tmp_path: Path,
) -> tuple[Path, Path, Path, Path, Path, Path, dict[str, str]]:
    root, script, environment = _runtime(tmp_path, sentinel=False)
    video = tmp_path / "final.mp4"
    video.write_bytes(b"exact continuous source video bytes")
    audio = tmp_path / "full-final-media.wav"
    audio.write_bytes(b"RIFF exact full media audio bytes")
    request = {
        "schema_version": adapter.RAW_AV_REQUEST_SCHEMA,
        "candidate_id": "auto_120029_1409_1578",
        "provider": adapter.PROVIDER,
        "model": MODEL,
        "endpoint_family": ENDPOINT,
        "capability_id": CAPABILITY_ID,
        "runtime_capability_sha256": "1" * 64,
        "command_contract_sha256": "2" * 64,
        "model_capability_seal_receipt_sha256": "3" * 64,
        "model_capability_attestation_sha256": "4" * 64,
        "model_capability_contract_sha256": "5" * 64,
        "model_capability_sentinel_result_sha256": "6" * 64,
        "model_capability_sentinel_result_self_sha256": "7" * 64,
        "model_capability_sentinel_runner_sha256": "8" * 64,
        "source_video": {
            "path": str(video.resolve()),
            "sha256": _sha(video),
            "bytes": video.stat().st_size,
            "duration_us": 2_000_000,
            "first_video_pts_us": 0,
            "last_video_pts_us": 1_960_000,
            "video_frame_count": 50,
            "coverage": "CONTINUOUS_EXACT_SOURCE_VIDEO",
        },
        "raw_audio": {
            "path": str(audio.resolve()),
            "sha256": _sha(audio),
            "bytes": audio.stat().st_size,
            "sample_rate": 16_000,
            "sample_frames": 32_000,
            "declared_start_us": 0,
            "declared_end_us": 2_000_000,
            "coverage": "CONTINUOUS_EXACT_FULL_MEDIA_WAV",
        },
        "asset_manifest_sha256": "9" * 64,
        "review_plan": {
            "authority": "SYNTHETIC_TEST_ONLY",
            "review_points": [
                {
                    "point_id": "whole-media",
                    "final_video_start_ms": 0,
                    "final_video_end_ms": 2000,
                    "expectation": "inspect the whole exact medium",
                }
            ],
        },
        "expected_result_schema_version": adapter.RESULT_SCHEMA,
        "semantic_command_sha256": "a" * 64,
    }
    request_path = tmp_path / "request.json"
    _write_json(request_path, request)
    response_path = tmp_path / "response.json"
    environment["AUTOSLICE_RAW_AV_REQUEST_SHA256"] = _sha(request_path)
    return root, script, request_path, response_path, video, audio, environment


def _review(status: str, point: str) -> str:
    verdict = "PASS" if status == "PASS" else "BLOCK"
    return json.dumps(
        {
            "status": status,
            "observations": [
                {
                    "point_id": point,
                    "verdict": verdict,
                    "detail": f"synthetic {status.lower()} observation",
                }
            ],
        },
        sort_keys=True,
    )


def test_sentinel_upload_is_hash_bound_and_free_key_only(tmp_path: Path):
    root, script, request, response, media, prompt, environment = (
        _sentinel_fixture(tmp_path)
    )
    fake = FakeFileRunner(["12345678\n"])

    result = adapter.run_sentinel(
        request_path=request,
        response_path=response,
        input_path=media,
        prompt_path=prompt,
        environment=environment,
        file_runner=fake,
        adapter_path=script,
    )

    assert result["observed_answer"] == "12345678"
    assert json.loads(response.read_text()) == result
    assert len(fake.calls) == 1
    assert fake.calls[0]["mime_type"] == "audio/wav"
    evidence = list(
        (root / "final-media-review-gemini-api-runs").glob(
            "*/*/adapter-evidence.json"
        )
    )
    assert len(evidence) == 1
    saved = json.loads(evidence[0].read_text())
    assert saved["input_sha256"] == _sha(media)
    assert saved["prompt_sha256"] == _sha(prompt)
    assert saved["provider_evidence"]["paid_backup_used"] is False


def test_sentinel_rejects_prose_instead_of_hidden_token(tmp_path: Path):
    _root, script, request, response, media, prompt, environment = (
        _sentinel_fixture(tmp_path)
    )
    fake = FakeFileRunner(["The answer is 12345678"])

    with pytest.raises(
        adapter.AdapterError, match="not exactly eight digits"
    ):
        adapter.run_sentinel(
            request_path=request,
            response_path=response,
            input_path=media,
            prompt_path=prompt,
            environment=environment,
            file_runner=fake,
            adapter_path=script,
        )

    assert not response.exists()
    assert len(fake.calls) == 1


def test_final_review_intersects_exact_video_and_audio_passes(tmp_path: Path):
    root, script, request, response, video, audio, environment = _final_fixture(
        tmp_path
    )
    fake = FakeFileRunner(
        [_review("PASS", "video-whole"), _review("PASS", "audio-whole")]
    )

    envelope = adapter.run_final_review(
        request_path=request,
        response_path=response,
        environment=environment,
        file_runner=fake,
        adapter_path=script,
    )

    assert envelope["result"]["status"] == "PASS"
    assert envelope["transport_receipt"]["provider"] == adapter.PROVIDER
    assert [Path(str(row["media_path"])).resolve() for row in fake.calls] == [
        video.resolve(),
        audio.resolve(),
    ]
    assert [row["mime_type"] for row in fake.calls] == [
        "video/mp4",
        "audio/wav",
    ]
    assert [row["video_fps"] for row in fake.calls] == [
        adapter.VIDEO_FPS,
        None,
    ]
    modalities = {
        row["modality"] for row in envelope["result"]["observations"]
    }
    assert modalities == {"continuous_source_video", "raw_audio", "transport"}
    evidence_path = Path(
        envelope["result"]["observations"][-1]["provider_evidence"]["path"]
    )
    assert evidence_path.is_relative_to(
        root / "final-media-review-gemini-api-runs"
    )
    assert json.loads(response.read_text()) == envelope


def test_final_review_blocks_when_either_modality_blocks(tmp_path: Path):
    _root, script, request, response, _video, _audio, environment = (
        _final_fixture(tmp_path)
    )
    fake = FakeFileRunner(
        [_review("PASS", "video-whole"), _review("BLOCK", "audio-whole")]
    )

    envelope = adapter.run_final_review(
        request_path=request,
        response_path=response,
        environment=environment,
        file_runner=fake,
        adapter_path=script,
    )

    assert envelope["result"]["status"] == "BLOCK"
    assert envelope["result"]["content_review_status"] == "BLOCK"


def test_final_review_rejects_binding_drift_before_provider_call(tmp_path: Path):
    _root, script, request, response, _video, audio, environment = _final_fixture(
        tmp_path
    )
    audio.write_bytes(audio.read_bytes() + b"drift")
    fake = FakeFileRunner([_review("PASS", "unused")])

    with pytest.raises(adapter.AdapterError, match="media bytes differ"):
        adapter.run_final_review(
            request_path=request,
            response_path=response,
            environment=environment,
            file_runner=fake,
            adapter_path=script,
        )

    assert fake.calls == []
    assert not response.exists()


def test_explicit_429_writes_typed_retry_response(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    _root, script, request, response, media, prompt, environment = (
        _sentinel_fixture(tmp_path)
    )
    fake = FakeFileRunner(
        [],
        error=GeminiFileApiError(
            "GEMINI_API_QUOTA_EXHAUSTED",
            "synthetic quota",
            phase="upload_start",
            http_status=429,
            ambiguous=False,
        ),
    )
    monkeypatch.setattr(adapter, "_file_runner", lambda: fake)
    monkeypatch.setattr(adapter, "__file__", str(script))
    monkeypatch.setattr(os, "environ", environment)

    rc = adapter.main(
        [
            "--request",
            str(request),
            "--response",
            str(response),
            "--input",
            str(media),
            "--prompt",
            str(prompt),
        ]
    )

    assert rc == 75
    value = json.loads(response.read_text())
    assert value["error"]["safe_reason"] == "GEMINI_API_QUOTA_EXHAUSTED"
    assert value["error"]["provider_http_status"] == 429


def test_ambiguous_api_failure_writes_no_response_for_outer_dedupe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    _root, script, request, response, media, prompt, environment = (
        _sentinel_fixture(tmp_path)
    )
    fake = FakeFileRunner(
        [],
        error=GeminiFileApiError(
            "GEMINI_API_TIMEOUT",
            "synthetic ambiguous timeout",
            phase="generate_content",
            ambiguous=True,
        ),
    )
    monkeypatch.setattr(adapter, "_file_runner", lambda: fake)
    monkeypatch.setattr(adapter, "__file__", str(script))
    monkeypatch.setattr(os, "environ", environment)

    rc = adapter.main(
        [
            "--request",
            str(request),
            "--response",
            str(response),
            "--input",
            str(media),
            "--prompt",
            str(prompt),
        ]
    )

    assert rc == 2
    assert not response.exists()


def test_actual_adapter_envelope_completes_existing_raw_av_consumer(
    tmp_path: Path,
):
    package_root, job_path, _job = raw_av_package(tmp_path)
    runtime = tmp_path / "e2e-runtime"
    script = runtime / "repo/scripts/final_media_review_gemini_api_adapter.py"
    helper = runtime / "repo/src/autoslice/gemini_file_api.py"
    script.parent.mkdir(parents=True)
    helper.parent.mkdir(parents=True)
    script.write_bytes(Path(adapter.__file__).resolve().read_bytes())
    helper.write_bytes(
        Path(__file__).resolve().parents[1]
        .joinpath("src/autoslice/gemini_file_api.py")
        .read_bytes()
    )
    script.chmod(0o700)
    helper.chmod(0o600)
    seal = write_model_capability_attestation(
        runtime,
        script,
        provider=adapter.PROVIDER,
        model=MODEL,
        endpoint_family=ENDPOINT,
    )
    capability = {
        "schema_version": RUNTIME_CAPABILITY_SCHEMA_VERSION,
        "capability_id": CAPABILITY_ID,
        "provider": adapter.PROVIDER,
        "transport": "content_bound_command",
        "model": MODEL,
        "endpoint_family": ENDPOINT,
        "accepts": {
            "raw_audio": True,
            "continuous_source_video": True,
        },
        "executable": {
            "path": "repo/scripts/final_media_review_gemini_api_adapter.py",
            "sha256": _sha(script),
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
        "model_capability_seal": seal,
    }
    _write_json(runtime / RUNTIME_CAPABILITY_FILENAME, capability)
    bound = bind_review_job_to_runtime_capability(
        job_path,
        allowed_root=package_root,
        runtime_root=runtime,
    )
    package_binding = json.loads(
        Path(bound["binding_path"]).read_text(encoding="utf-8")
    )
    fake = FakeFileRunner(
        [_review("PASS", "video-whole"), _review("PASS", "audio-whole")]
    )
    child_environment = {
        "PATH": os.environ.get("PATH", ""),
        "AUTOSLICE_FINAL_MEDIA_GEMINI_API_MODEL": MODEL,
        "GEMINI_API_KEY": "free-test-key",
        "GEMINI_KEY_BACKUP": "paid-must-remain-unused",
        "AUTOSLICE_PROVIDER_CONCURRENCY": "1",
        "AUTOSLICE_PROVIDER_WAIT_SECONDS": "1",
    }

    def run_adapter(
        command: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        request_path = Path(command[command.index("--request") + 1])
        response_path = Path(command[command.index("--response") + 1])
        raw_env = kwargs.get("env")
        assert isinstance(raw_env, dict)
        adapter.run_final_review(
            request_path=request_path,
            response_path=response_path,
            environment={str(key): str(value) for key, value in raw_env.items()},
            file_runner=fake,
            adapter_path=script,
        )
        return subprocess.CompletedProcess(command, 0, "", "")

    executor = build_runtime_raw_av_executor(
        runtime_root=runtime,
        package_binding=package_binding,
        environment=child_environment,
        run=run_adapter,
    )
    state_path = package_root / "verification/e2e-gemini-api-state.json"
    outcome = consume_review_job(
        bound["active_job_path"],
        state_path,
        runtime_root=runtime,
        allowed_root=package_root,
        executor=executor,
        environment=child_environment,
    )

    assert outcome["state"]["status"] == "COMPLETE", outcome
    assert outcome["state"]["result"]["status"] == "PASS"
    evidence = outcome["state"]["result"]["transport_evidence"]
    assert evidence["provider"] == adapter.PROVIDER
    assert evidence["consumed_continuous_source_video"] is True
    assert evidence["consumed_raw_audio"] is True
    assert len(fake.calls) == 2
    assert all(
        set(call["environment"]) == {"GEMINI_API_KEY"}
        and "GEMINI_KEY_BACKUP" not in call["environment"]
        for call in fake.calls
    )

    second = consume_review_job(
        bound["active_job_path"],
        state_path,
        runtime_root=runtime,
        allowed_root=package_root,
        executor=executor,
        environment=child_environment,
    )
    assert second["cache_reused"] is True
    assert len(fake.calls) == 2


def test_bound_helper_source_is_loadable_without_network():
    runner = adapter._file_runner()
    assert callable(runner)


def test_provider_evidence_cannot_downgrade_bound_video_fps(tmp_path: Path):
    _root, script, request, response, _video, _audio, environment = (
        _final_fixture(tmp_path)
    )

    class WrongFpsRunner(FakeFileRunner):
        def __call__(self, **kwargs: object) -> GeminiFileCallOutcome:
            outcome = super().__call__(**kwargs)
            evidence = dict(outcome.evidence)
            evidence["video_fps"] = 1.0
            return GeminiFileCallOutcome(text=outcome.text, evidence=evidence)

    fake = WrongFpsRunner([_review("PASS", "video-whole")])
    with pytest.raises(
        adapter.AdapterError,
        match="provider evidence does not bind exact",
    ):
        adapter.run_final_review(
            request_path=request,
            response_path=response,
            environment=environment,
            file_runner=fake,
            adapter_path=script,
        )

    assert len(fake.calls) == 1
    assert not response.exists()
