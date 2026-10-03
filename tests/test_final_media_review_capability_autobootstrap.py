from __future__ import annotations

from datetime import timedelta
import hashlib
import json
from pathlib import Path
import subprocess

import pytest

from src.autoslice.final_media_review_capability_autobootstrap import (
    FinalMediaReviewCapabilityAutobootstrapError,
    ensure_runtime_raw_av_capability,
)
from src.autoslice.final_media_review_gemini_api_runtime import (
    ENABLED_ENV as API_ENABLED_ENV,
    MODEL_ENV as API_MODEL_ENV,
)
from src.autoslice.final_media_review_gemini_web_runtime import (
    ENABLED_ENV as WEB_ENABLED_ENV,
)
from src.autoslice.final_media_review_model_capability_sentinel import (
    run_model_capability_sentinel,
)
from src.autoslice.final_media_review_raw_av import RUNTIME_CAPABILITY_FILENAME
from tests.test_final_media_review_model_capability_sentinel import (
    START,
    SyntheticAdapter,
    _run,
    _runtime,
    _tokens,
)


def _runtime_file_bytes(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file() and not path.is_symlink()
    }


def test_absent_sentinel_runtime_is_provider_free(tmp_path: Path):
    root = tmp_path / "runtime"
    root.mkdir(mode=0o700)
    calls = []

    def unexpected(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("runner must not be called")

    result = ensure_runtime_raw_av_capability(root, now=START, run=unexpected)

    assert result == {
        "status": "SENTINEL_RUNTIME_ABSENT",
        "runner_called": False,
        "provider_calls": 0,
    }
    assert calls == []
    assert list(root.iterdir()) == []


def test_valid_capability_is_reused_without_runner(tmp_path: Path):
    root, runner, _adapter_path = _runtime(
        tmp_path, provider="gemini_api", model="gemini-3.8-flash"
    )
    _run(root, runner, SyntheticAdapter(), now=START)

    def unexpected(*_args, **_kwargs):
        raise AssertionError("valid capability must not relaunch sentinel")

    result = ensure_runtime_raw_av_capability(
        root,
        now=START + timedelta(minutes=1),
        environment={
            API_ENABLED_ENV: "1",
            API_MODEL_ENV: "gemini-3.8-flash",
        },
        run=unexpected,
    )

    assert result["status"] == "RAW_AV_CAPABILITY_PRESENT"
    assert result["runner_called"] is False
    assert result["provider_calls"] == 0
    assert len(result["runtime_capability_sha256"]) == 64


@pytest.mark.parametrize("runtime_state", ["empty", "sentinel", "capability"])
def test_route_conflict_is_rejected_before_runtime_state_reuse(
    tmp_path: Path, runtime_state: str
):
    if runtime_state == "empty":
        root = tmp_path / "runtime"
        root.mkdir(mode=0o700)
    else:
        root, runner, _adapter_path = _runtime(tmp_path)
        if runtime_state == "capability":
            adapter = SyntheticAdapter()
            bootstrapped = _run(root, runner, adapter, now=START)
            assert bootstrapped["status"] == "CAPABILITY_BOOTSTRAPPED"
            assert len(adapter.calls) == 2
    before = _runtime_file_bytes(root)
    calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    def unexpected(*args: object, **kwargs: object):
        calls.append((args, kwargs))
        raise AssertionError("route conflict must stop before sentinel dispatch")

    with pytest.raises(FinalMediaReviewCapabilityAutobootstrapError) as caught:
        ensure_runtime_raw_av_capability(
            root,
            now=START + timedelta(minutes=1),
            environment={
                API_ENABLED_ENV: "1",
                API_MODEL_ENV: "gemini-3.8-flash",
                WEB_ENABLED_ENV: "1",
            },
            run=unexpected,
        )

    assert caught.value.reason_code == "FINAL_MEDIA_REVIEW_ROUTE_CONFLICT"
    assert caught.value.detail == (
        "direct Gemini API and Gemini Web routes cannot both be enabled"
    )
    assert calls == []
    assert _runtime_file_bytes(root) == before


def test_bootstrap_invokes_bound_runner_and_replays_capability(tmp_path: Path):
    root, runner, _adapter_path = _runtime(tmp_path)
    adapter = SyntheticAdapter()
    observed_commands: list[list[str]] = []

    def invoke(command: list[str], **_kwargs) -> subprocess.CompletedProcess[str]:
        observed_commands.append(command)
        run_id = command[command.index("--run-id") + 1]
        validity = int(command[command.index("--valid-for-seconds") + 1])
        value = run_model_capability_sentinel(
            runtime_root=root,
            run_id=run_id,
            valid_for_seconds=validity,
            runner_path=runner,
            now=START,
            run=adapter,
            token_factory=_tokens,
        )
        return subprocess.CompletedProcess(
            command, 0, json.dumps(value), ""
        )

    result = ensure_runtime_raw_av_capability(root, now=START, run=invoke)

    assert result["status"] == "CAPABILITY_BOOTSTRAPPED"
    assert result["runner_called"] is True
    assert result["runner_returncode"] == 0
    assert result["provider_calls"] == 2
    assert observed_commands[0][0] == str(runner.resolve())
    assert (root / RUNTIME_CAPABILITY_FILENAME).is_file()


def test_wait_state_is_returned_without_capability(tmp_path: Path):
    root, _runner, _adapter_path = _runtime(tmp_path)
    commands: list[list[str]] = []

    def wait(command: list[str], **_kwargs) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        value = {
            "status": "RETRY_WAIT",
            "provider_calls": 1,
            "reason_code": "SENTINEL_PROVIDER_RATE_LIMITED",
            "challenge_states": {"raw_audio": {"status": "RETRY_WAIT"}},
        }
        return subprocess.CompletedProcess(command, 75, json.dumps(value), "")

    first = ensure_runtime_raw_av_capability(root, now=START, run=wait)
    second = ensure_runtime_raw_av_capability(
        root, now=START + timedelta(minutes=1), run=wait
    )

    assert first["status"] == second["status"] == "RETRY_WAIT"
    assert first["runner_returncode"] == second["runner_returncode"] == 75
    assert first["provider_calls"] == second["provider_calls"] == 1
    assert commands[0][commands[0].index("--run-id") + 1] == commands[1][
        commands[1].index("--run-id") + 1
    ]
    assert not (root / RUNTIME_CAPABILITY_FILENAME).exists()


def test_ambiguous_state_is_preserved_without_retry_inference(tmp_path: Path):
    root, _runner, _adapter_path = _runtime(tmp_path)

    def ambiguous(command: list[str], **_kwargs) -> subprocess.CompletedProcess[str]:
        value = {
            "status": "DISPATCH_AMBIGUOUS",
            "provider_calls": 0,
            "reason_code": "SENTINEL_DISPATCH_OUTCOME_UNKNOWN",
            "challenge_states": {"raw_audio": {"status": "DISPATCH_AMBIGUOUS"}},
        }
        return subprocess.CompletedProcess(command, 2, json.dumps(value), "")

    result = ensure_runtime_raw_av_capability(root, now=START, run=ambiguous)

    assert result["status"] == "DISPATCH_AMBIGUOUS"
    assert result["runner_returncode"] == 2
    assert result["reason_code"] == "SENTINEL_DISPATCH_OUTCOME_UNKNOWN"
    assert not (root / RUNTIME_CAPABILITY_FILENAME).exists()


def test_runner_success_without_capability_is_rejected(tmp_path: Path):
    root, _runner, _adapter_path = _runtime(tmp_path)

    def lying(command: list[str], **_kwargs) -> subprocess.CompletedProcess[str]:
        value = {"status": "CAPABILITY_BOOTSTRAPPED", "provider_calls": 2}
        return subprocess.CompletedProcess(command, 0, json.dumps(value), "")

    with pytest.raises(
        FinalMediaReviewCapabilityAutobootstrapError,
        match="FINAL_MEDIA_REVIEW_SENTINEL_CAPABILITY_MISSING",
    ):
        ensure_runtime_raw_av_capability(root, now=START, run=lying)


def test_expired_capability_is_rotated_by_new_bucket_run(tmp_path: Path):
    root, runner, _adapter_path = _runtime(tmp_path)
    first_adapter = SyntheticAdapter()
    _run(
        root,
        runner,
        first_adapter,
        run_id="short-lived-capability",
        now=START,
        valid_for_seconds=60,
    )
    capability = root / RUNTIME_CAPABILITY_FILENAME
    first_bytes = capability.read_bytes()
    first_sha = hashlib.sha256(first_bytes).hexdigest()
    second_adapter = SyntheticAdapter()

    def renew(command: list[str], **_kwargs) -> subprocess.CompletedProcess[str]:
        run_id = command[command.index("--run-id") + 1]
        validity = int(command[command.index("--valid-for-seconds") + 1])
        value = run_model_capability_sentinel(
            runtime_root=root,
            run_id=run_id,
            valid_for_seconds=validity,
            runner_path=runner,
            now=START + timedelta(seconds=61),
            run=second_adapter,
            token_factory=_tokens,
        )
        return subprocess.CompletedProcess(command, 0, json.dumps(value), "")

    result = ensure_runtime_raw_av_capability(
        root,
        now=START + timedelta(seconds=61),
        run=renew,
        validity_seconds=60,
    )

    assert result["status"] == "CAPABILITY_BOOTSTRAPPED"
    assert result["provider_calls"] == 2
    archive = root / "final-media-review-raw-av-capability-history" / f"{first_sha}.json"
    assert archive.read_bytes() == first_bytes
    assert capability.read_bytes() != first_bytes
