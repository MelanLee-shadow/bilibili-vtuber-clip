"""Provider-aware bootstrap shim for the ordinary final-media consumers.

The shim never invents a capability.  Before reusing or dispatching any
validated runtime state, it rejects conflicting explicit route configuration.
It then accepts an already-valid runtime raw-AV capability.  Otherwise it accepts
an existing runtime-owned sentinel manifest, or create-only provisions the
selected explicit route, then invokes the manifest's hash-bound runner with a
stable time-bucket run id.  Typed wait/ambiguous/failure states are returned
without creating a package-local terminal review state; a successful runner must
leave a capability that the ordinary raw-AV loader can independently replay.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
from typing import Callable, Mapping

from src.autoslice.final_media_review_gemini_api_runtime import (
    FinalMediaReviewGeminiApiRuntimeError,
    ensure_gemini_api_sentinel_runtime,
    gemini_api_runtime_enabled,
)
from src.autoslice.final_media_review_gemini_web_runtime import (
    FinalMediaReviewGeminiWebRuntimeError,
    ensure_gemini_web_sentinel_runtime,
    gemini_web_runtime_enabled,
)
from src.autoslice.final_media_review_model_capability_sentinel import (
    FinalMediaReviewModelCapabilitySentinelError,
    load_sentinel_runtime,
)
from src.autoslice.final_media_review_raw_av import (
    FinalMediaReviewRawAvError,
    load_runtime_raw_av_capability,
)


DEFAULT_SENTINEL_VALIDITY_SECONDS = 24 * 60 * 60
_MAX_RUNNER_OUTPUT_BYTES = 4_000_000
CAPABILITY_BOOTSTRAP_WAIT_STATUSES = frozenset({"PREPARED", "RETRY_WAIT"})
_RUNNER_STOP_STATUSES = frozenset({"FAILED", "DISPATCH_AMBIGUOUS"})
CAPABILITY_BOOTSTRAP_STOP_STATUSES = frozenset(
    {*_RUNNER_STOP_STATUSES, "SENTINEL_RUNTIME_ABSENT"}
)
CAPABILITY_BOOTSTRAP_BLOCKING_STATUSES = frozenset(
    CAPABILITY_BOOTSTRAP_WAIT_STATUSES | CAPABILITY_BOOTSTRAP_STOP_STATUSES
)


class FinalMediaReviewCapabilityAutobootstrapError(ValueError):
    """A typed fail-closed sentinel-runner or output error."""

    def __init__(self, reason_code: str, detail: str):
        super().__init__(f"{reason_code}: {detail}")
        self.reason_code = reason_code
        self.detail = detail


def _error(
    reason_code: str, detail: str
) -> FinalMediaReviewCapabilityAutobootstrapError:
    return FinalMediaReviewCapabilityAutobootstrapError(reason_code, detail)


def _safe_environment(
    environment: Mapping[str, str] | None,
    *,
    runtime_root: Path,
) -> dict[str, str]:
    selected = dict(os.environ if environment is None else environment)
    for key in list(selected):
        if key.startswith("CPA_"):
            selected.pop(key, None)
    selected["AUTOSLICE_BASE"] = str(runtime_root)
    selected["AUTOSLICE_FINAL_REVIEW_RUNTIME_ROOT"] = str(runtime_root)
    return selected


def _run_id(
    runtime_contract_sha256: str,
    *,
    now: datetime,
    validity_seconds: int,
) -> str:
    window = int(now.timestamp()) // validity_seconds
    return f"auto-{runtime_contract_sha256[:16]}-{window:012x}"


def _parse_runner_output(
    completed: subprocess.CompletedProcess[str],
) -> dict[str, object]:
    stdout = completed.stdout or ""
    stderr = completed.stderr or ""
    if len(stdout.encode("utf-8", errors="replace")) > _MAX_RUNNER_OUTPUT_BYTES:
        raise _error(
            "FINAL_MEDIA_REVIEW_SENTINEL_RUNNER_OUTPUT_INVALID",
            "sentinel runner stdout exceeds the bounded result size",
        )
    try:
        value = json.loads(stdout)
    except (TypeError, json.JSONDecodeError) as exc:
        detail = stderr.strip()[:1000] or "sentinel runner returned no JSON result"
        raise _error(
            "FINAL_MEDIA_REVIEW_SENTINEL_RUNNER_OUTPUT_INVALID",
            detail,
        ) from exc
    if not isinstance(value, dict) or not isinstance(value.get("status"), str):
        raise _error(
            "FINAL_MEDIA_REVIEW_SENTINEL_RUNNER_OUTPUT_INVALID",
            "sentinel runner result is not a typed object",
        )
    return value


def _validated_present_capability(
    root: Path, *, now: datetime
) -> dict[str, object] | None:
    try:
        return load_runtime_raw_av_capability(root, now=now)
    except FinalMediaReviewRawAvError as exc:
        if exc.reason_code == "FINAL_MEDIA_REVIEW_MODEL_CAPABILITY_ATTESTATION_EXPIRED":
            return None
        raise _error(exc.reason_code, exc.detail) from exc


def ensure_runtime_raw_av_capability(
    runtime_root: str | Path,
    *,
    now: datetime | None = None,
    environment: Mapping[str, str] | None = None,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    validity_seconds: int = DEFAULT_SENTINEL_VALIDITY_SECONDS,
) -> dict[str, object]:
    """Accept, bootstrap, wait, or fail one runtime capability deterministically."""

    root = Path(runtime_root).expanduser().absolute()
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    existing = _validated_present_capability(root, now=current)
    try:
        api_enabled = gemini_api_runtime_enabled(environment)
        web_enabled = gemini_web_runtime_enabled(environment)
    except (
        FinalMediaReviewGeminiApiRuntimeError,
        FinalMediaReviewGeminiWebRuntimeError,
    ) as exc:
        raise _error(exc.reason_code, exc.detail) from exc
    if api_enabled and web_enabled:
        raise _error(
            "FINAL_MEDIA_REVIEW_ROUTE_CONFLICT",
            "direct Gemini API and Gemini Web routes cannot both be enabled",
        )
    if existing is not None:
        return {
            "status": "RAW_AV_CAPABILITY_PRESENT",
            "runner_called": False,
            "provider_calls": 0,
            "runtime_capability_sha256": existing["runtime_capability_sha256"],
            "model": existing["model"],
            "endpoint_family": existing["endpoint_family"],
        }
    try:
        sentinel = load_sentinel_runtime(root)
    except FinalMediaReviewModelCapabilitySentinelError as exc:
        raise _error(exc.reason_code, exc.detail) from exc
    runtime_provisioning: dict[str, object] | None = None
    if sentinel is None:
        try:
            if api_enabled:
                runtime_provisioning = ensure_gemini_api_sentinel_runtime(
                    root, environment=environment
                )
            elif web_enabled:
                runtime_provisioning = ensure_gemini_web_sentinel_runtime(
                    root, environment=environment
                )
            else:
                return {
                    "status": "SENTINEL_RUNTIME_ABSENT",
                    "runner_called": False,
                    "provider_calls": 0,
                }
        except (
            FinalMediaReviewGeminiApiRuntimeError,
            FinalMediaReviewGeminiWebRuntimeError,
        ) as exc:
            raise _error(exc.reason_code, exc.detail) from exc
        try:
            sentinel = load_sentinel_runtime(root)
        except FinalMediaReviewModelCapabilitySentinelError as exc:
            raise _error(exc.reason_code, exc.detail) from exc
        if sentinel is None:
            raise _error(
                "FINAL_MEDIA_REVIEW_SENTINEL_RUNTIME_MISSING",
                "explicit runtime provisioning did not leave a replayable manifest",
            )
    if (
        isinstance(validity_seconds, bool)
        or not isinstance(validity_seconds, int)
        or not 60 <= validity_seconds <= 7 * 24 * 60 * 60
    ):
        raise _error(
            "FINAL_MEDIA_REVIEW_SENTINEL_RUN_INVALID",
            "sentinel validity is outside the accepted range",
        )
    run_id = _run_id(
        str(sentinel["runtime_contract_sha256"]),
        now=current,
        validity_seconds=validity_seconds,
    )
    command = [
        str(sentinel["runner"]["absolute_path"]),
        "--runtime-root",
        str(root),
        "--run-id",
        run_id,
        "--valid-for-seconds",
        str(validity_seconds),
    ]
    timeout = min(
        18_000,
        10_800 + 2 * int(sentinel["timeout_seconds"]) + 300,
    )
    try:
        completed = run(
            command,
            check=False,
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=timeout,
            env=_safe_environment(environment, runtime_root=root),
        )
    except subprocess.TimeoutExpired as exc:
        raise _error(
            "FINAL_MEDIA_REVIEW_SENTINEL_RUNNER_TIMEOUT",
            "sentinel runner timed out; its durable state must be inspected before retry",
        ) from exc
    except OSError as exc:
        raise _error(
            "FINAL_MEDIA_REVIEW_SENTINEL_RUNNER_UNAVAILABLE",
            "sentinel runner could not start",
        ) from exc
    result = _parse_runner_output(completed)
    status = str(result["status"])
    provider_calls = result.get("provider_calls")
    if isinstance(provider_calls, bool) or not isinstance(provider_calls, int):
        raise _error(
            "FINAL_MEDIA_REVIEW_SENTINEL_RUNNER_OUTPUT_INVALID",
            "sentinel runner provider call count is invalid",
        )
    rendered = {
        "status": status,
        "runner_called": True,
        "runner_returncode": completed.returncode,
        "run_id": run_id,
        "provider_calls": provider_calls,
        "reason_code": result.get("reason_code"),
        "challenge_states": result.get("challenge_states"),
        "runtime_provisioning": runtime_provisioning,
    }
    if status == "CAPABILITY_BOOTSTRAPPED":
        if completed.returncode != 0:
            raise _error(
                "FINAL_MEDIA_REVIEW_SENTINEL_RUNNER_OUTPUT_INVALID",
                "sentinel runner reported success with a nonzero return code",
            )
        capability = _validated_present_capability(root, now=current)
        if capability is None:
            raise _error(
                "FINAL_MEDIA_REVIEW_SENTINEL_CAPABILITY_MISSING",
                "sentinel runner reported success without a replayable capability",
            )
        return {
            **rendered,
            "runtime_capability_sha256": capability[
                "runtime_capability_sha256"
            ],
            "model": capability["model"],
            "endpoint_family": capability["endpoint_family"],
        }
    if status in CAPABILITY_BOOTSTRAP_WAIT_STATUSES:
        if completed.returncode != 75:
            raise _error(
                "FINAL_MEDIA_REVIEW_SENTINEL_RUNNER_OUTPUT_INVALID",
                "sentinel wait state has the wrong return code",
            )
        return rendered
    if status in _RUNNER_STOP_STATUSES:
        if completed.returncode != 2:
            raise _error(
                "FINAL_MEDIA_REVIEW_SENTINEL_RUNNER_OUTPUT_INVALID",
                "sentinel terminal failure has the wrong return code",
            )
        return rendered
    raise _error(
        "FINAL_MEDIA_REVIEW_SENTINEL_RUNNER_OUTPUT_INVALID",
        f"sentinel runner returned unknown status {status}",
    )


__all__ = [
    "CAPABILITY_BOOTSTRAP_BLOCKING_STATUSES",
    "CAPABILITY_BOOTSTRAP_STOP_STATUSES",
    "CAPABILITY_BOOTSTRAP_WAIT_STATUSES",
    "DEFAULT_SENTINEL_VALIDITY_SECONDS",
    "FinalMediaReviewCapabilityAutobootstrapError",
    "ensure_runtime_raw_av_capability",
]
