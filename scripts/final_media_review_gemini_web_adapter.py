#!/usr/bin/env python3
"""Hash-bound Gemini Web adapter for final-media sentinel and review jobs.

The adapter has two fail-closed modes:

* sentinel mode receives one hidden audio or continuous-video challenge;
* final-review mode submits the exact final MP4 and exact full-media WAV in two
  independent Gemini Web turns and locally intersects their verdicts.

The consumer Web UI label is a route label, not proof of the backend model ID.
Actual dual-media capability is established only by the independent hidden
sentinel and its short-lived seal.  This executable never decodes challenge
content itself and never fabricates a perceptual result from hashes or text.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
from typing import Callable, Mapping, Sequence


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.autoslice.final_media_review_gemini_web_runtime import (
    PROVIDER,
    gemini_web_route_model_id,
)

SENTINEL_RUNTIME_FILENAME = (
    "final-media-review-model-capability-sentinel-runtime.json"
)
RAW_AV_RUNTIME_FILENAME = "final-media-review-raw-av-capability.json"
SENTINEL_REQUEST_SCHEMA = (
    "final-media-review-model-capability-sentinel-request.v1"
)
SENTINEL_RESPONSE_SCHEMA = (
    "final-media-review-model-capability-sentinel-response.v1"
)
SENTINEL_ERROR_SCHEMA = (
    "final-media-review-model-capability-sentinel-adapter-error.v1"
)
RAW_AV_REQUEST_SCHEMA = "final-media-review-raw-av-request.v3"
RAW_AV_RESPONSE_SCHEMA = "final-media-review-raw-av-adapter-response.v3"
RAW_AV_RECEIPT_SCHEMA = "final-media-review-raw-av-adapter-receipt.v3"
RESULT_SCHEMA = "final-media-perceptual-review-result.v1"
GEMINI_HELPER_SHA256 = (
    "e867687beb31346bd67c3e8ed655ad77948c4d47f7128f36b98f134da51d5222"
)
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
_MAX_JSON_BYTES = 8_000_000
_MAX_RESPONSE_BYTES = 2_000_000
_MAX_MEDIA_BYTES = 2_000_000_000


class AdapterError(ValueError):
    """A typed explicit adapter rejection."""

    def __init__(
        self,
        safe_reason: str,
        message: str,
        *,
        provider_http_status: int | None = None,
        exit_code: int = 2,
    ):
        super().__init__(message)
        self.safe_reason = safe_reason
        self.message = message
        self.provider_http_status = provider_http_status
        self.exit_code = exit_code


@dataclass(frozen=True)
class WebConfig:
    profile_dir: Path
    model_label: str
    headless: bool
    direct_cdp: bool
    browser_executable: Path | None
    xvfb_bin: Path | None
    display: str | None
    cdp_port: int
    upload_timeout_seconds: float
    response_timeout_seconds: float
    stability_polls: int


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _regular_file(
    raw: str | Path,
    *,
    label: str,
    maximum: int | None = None,
) -> Path:
    path = Path(raw).expanduser()
    try:
        info = path.lstat()
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise AdapterError("GEMINI_WEB_INPUT_INVALID", f"{label} unavailable") from exc
    if path.is_symlink() or not stat.S_ISREG(info.st_mode) or not resolved.is_file():
        raise AdapterError(
            "GEMINI_WEB_INPUT_INVALID",
            f"{label} must be a regular non-symlink file",
        )
    size = resolved.stat().st_size
    if size <= 0 or (maximum is not None and size > maximum):
        raise AdapterError(
            "GEMINI_WEB_INPUT_INVALID",
            f"{label} size outside accepted range",
        )
    return resolved


def _safe_runtime_root(environment: Mapping[str, str]) -> Path:
    raw = (
        environment.get("AUTOSLICE_FINAL_REVIEW_RUNTIME_ROOT")
        or environment.get("AUTOSLICE_BASE")
    )
    if not raw:
        raise AdapterError(
            "GEMINI_WEB_RUNTIME_INVALID", "final-media runtime root is missing"
        )
    path = Path(raw).expanduser()
    try:
        info = path.lstat()
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise AdapterError(
            "GEMINI_WEB_RUNTIME_INVALID", "final-media runtime root unavailable"
        ) from exc
    if path.is_symlink() or not stat.S_ISDIR(info.st_mode) or not resolved.is_dir():
        raise AdapterError(
            "GEMINI_WEB_RUNTIME_INVALID", "final-media runtime root is unsafe"
        )
    return resolved


def _read_json(path: Path, *, label: str) -> dict[str, object]:
    target = _regular_file(path, label=label, maximum=_MAX_JSON_BYTES)
    try:
        value = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise AdapterError(
            "GEMINI_WEB_INPUT_INVALID", f"{label} is not valid JSON"
        ) from exc
    if not isinstance(value, dict):
        raise AdapterError(
            "GEMINI_WEB_INPUT_INVALID", f"{label} root must be an object"
        )
    return value


def _json_bytes(value: Mapping[str, object]) -> bytes:
    try:
        payload = (
            json.dumps(
                dict(value),
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
                allow_nan=False,
            )
            + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise AdapterError(
            "GEMINI_WEB_RESPONSE_INVALID", "response cannot be encoded"
        ) from exc
    if len(payload) > _MAX_JSON_BYTES:
        raise AdapterError(
            "GEMINI_WEB_RESPONSE_INVALID", "response exceeds size limit"
        )
    return payload


def _write_create_only(path: Path, value: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    payload = _json_bytes(value)
    try:
        descriptor = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
        )
    except FileExistsError as exc:
        raise AdapterError(
            "GEMINI_WEB_RESPONSE_CONFLICT", "response path already exists"
        ) from exc
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())


def _binding(path: Path) -> dict[str, object]:
    target = _regular_file(path, label="provider evidence", maximum=_MAX_JSON_BYTES)
    return {
        "path": str(target),
        "sha256": _sha256(target),
        "bytes": target.stat().st_size,
    }


def _normalized_sha(value: object, *, label: str) -> str:
    if not isinstance(value, str):
        raise AdapterError("GEMINI_WEB_INPUT_INVALID", f"{label} SHA256 missing")
    normalized = value.strip().lower().removeprefix("sha256:")
    if _SHA_RE.fullmatch(normalized) is None:
        raise AdapterError("GEMINI_WEB_INPUT_INVALID", f"{label} SHA256 invalid")
    return normalized


def _bool_env(value: str | None, *, default: bool = False) -> bool:
    if value is None or not value.strip():
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise AdapterError("GEMINI_WEB_CONFIG_INVALID", "boolean environment invalid")


def _first_env(environment: Mapping[str, str], *names: str) -> str | None:
    for name in names:
        value = environment.get(name)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _optional_path(raw: str | None) -> Path | None:
    return Path(raw).expanduser() if raw else None


def _web_config(environment: Mapping[str, str]) -> WebConfig:
    profile_raw = _first_env(
        environment,
        "AUTOSLICE_FINAL_MEDIA_GEMINI_WEB_PROFILE_DIR",
        "AUTOSLICE_ENTITY_AUDIO_GEMINI_WEB_PROFILE_DIR",
    )
    label = _first_env(
        environment,
        "AUTOSLICE_FINAL_MEDIA_GEMINI_WEB_MODEL_LABEL",
        "AUTOSLICE_ENTITY_AUDIO_GEMINI_WEB_MODEL_LABEL",
    )
    if not profile_raw or not label:
        raise AdapterError(
            "GEMINI_WEB_CONFIG_MISSING",
            "Gemini Web profile directory or visible model label is missing",
        )
    profile = Path(profile_raw).expanduser()
    if profile.is_symlink() or not profile.is_dir():
        raise AdapterError(
            "GEMINI_WEB_CONFIG_INVALID", "Gemini Web profile directory is unsafe"
        )
    try:
        cdp_port = int(
            _first_env(
                environment,
                "AUTOSLICE_FINAL_MEDIA_GEMINI_WEB_CDP_PORT",
                "AUTOSLICE_ENTITY_AUDIO_GEMINI_WEB_CDP_PORT",
            )
            or "0"
        )
        upload_timeout = float(
            _first_env(
                environment,
                "AUTOSLICE_FINAL_MEDIA_GEMINI_WEB_UPLOAD_TIMEOUT_SECONDS",
                "AUTOSLICE_ENTITY_AUDIO_GEMINI_WEB_UPLOAD_TIMEOUT_SECONDS",
            )
            or "180"
        )
        response_timeout = float(
            _first_env(
                environment,
                "AUTOSLICE_FINAL_MEDIA_GEMINI_WEB_RESPONSE_TIMEOUT_SECONDS",
                "AUTOSLICE_ENTITY_AUDIO_GEMINI_WEB_RESPONSE_TIMEOUT_SECONDS",
            )
            or "600"
        )
        stability_polls = int(
            _first_env(
                environment,
                "AUTOSLICE_FINAL_MEDIA_GEMINI_WEB_STABILITY_POLLS",
                "AUTOSLICE_ENTITY_AUDIO_GEMINI_WEB_STABILITY_POLLS",
            )
            or "3"
        )
    except ValueError as exc:
        raise AdapterError(
            "GEMINI_WEB_CONFIG_INVALID", "numeric Gemini Web config invalid"
        ) from exc
    if (
        not 0 <= cdp_port <= 65535
        or not 1 <= upload_timeout <= 3600
        or not 1 <= response_timeout <= 3600
        or not 2 <= stability_polls <= 20
    ):
        raise AdapterError(
            "GEMINI_WEB_CONFIG_INVALID", "Gemini Web timeouts or port invalid"
        )
    return WebConfig(
        profile_dir=profile.resolve(strict=True),
        model_label=label,
        headless=_bool_env(
            _first_env(
                environment,
                "AUTOSLICE_FINAL_MEDIA_GEMINI_WEB_HEADLESS",
                "AUTOSLICE_ENTITY_AUDIO_GEMINI_WEB_HEADLESS",
            ),
            default=False,
        ),
        direct_cdp=_bool_env(
            _first_env(
                environment,
                "AUTOSLICE_FINAL_MEDIA_GEMINI_WEB_DIRECT_CDP",
                "AUTOSLICE_ENTITY_AUDIO_GEMINI_WEB_DIRECT_CDP",
            ),
            default=False,
        ),
        browser_executable=_optional_path(
            _first_env(
                environment,
                "AUTOSLICE_FINAL_MEDIA_GEMINI_WEB_BROWSER",
                "AUTOSLICE_ENTITY_AUDIO_GEMINI_WEB_BROWSER",
            )
        ),
        xvfb_bin=_optional_path(
            _first_env(
                environment,
                "AUTOSLICE_FINAL_MEDIA_GEMINI_WEB_XVFB_BIN",
                "AUTOSLICE_ENTITY_AUDIO_GEMINI_WEB_XVFB_BIN",
            )
        ),
        display=_first_env(
            environment,
            "AUTOSLICE_FINAL_MEDIA_GEMINI_WEB_DISPLAY",
            "AUTOSLICE_ENTITY_AUDIO_GEMINI_WEB_DISPLAY",
        ),
        cdp_port=cdp_port,
        upload_timeout_seconds=upload_timeout,
        response_timeout_seconds=response_timeout,
        stability_polls=stability_polls,
    )


def _verify_adapter_identity(
    *,
    root: Path,
    manifest_name: str,
    adapter_path: Path,
    provider: str,
    model: str,
    endpoint_family: str,
    capability_id: str | None = None,
) -> dict[str, object]:
    manifest = _read_json(root / manifest_name, label="runtime capability manifest")
    if (
        manifest.get("provider") != provider
        or manifest.get("model") != model
        or manifest.get("endpoint_family") != endpoint_family
        or (capability_id is not None and manifest.get("capability_id") != capability_id)
    ):
        raise AdapterError(
            "GEMINI_WEB_RUNTIME_MISMATCH",
            "runtime capability identity differs from request",
        )
    raw_adapter = (
        manifest.get("adapter")
        if manifest_name == SENTINEL_RUNTIME_FILENAME
        else manifest.get("executable")
    )
    if not isinstance(raw_adapter, Mapping):
        raise AdapterError(
            "GEMINI_WEB_RUNTIME_INVALID", "runtime adapter binding missing"
        )
    relative = raw_adapter.get("path")
    expected_sha = _normalized_sha(
        raw_adapter.get("sha256"), label="runtime adapter"
    )
    if (
        not isinstance(relative, str)
        or not relative
        or Path(relative).is_absolute()
        or ".." in Path(relative).parts
    ):
        raise AdapterError(
            "GEMINI_WEB_RUNTIME_INVALID", "runtime adapter path invalid"
        )
    bound = _regular_file(root / relative, label="runtime adapter")
    actual = adapter_path.resolve(strict=True)
    if bound != actual or _sha256(bound) != expected_sha:
        raise AdapterError(
            "GEMINI_WEB_RUNTIME_MISMATCH", "runtime adapter bytes differ"
        )
    return manifest


def _helper_runner() -> Callable[..., Mapping[str, object]]:
    helper = _regular_file(
        REPO_ROOT / "scripts/gemini_web_subscription.py",
        label="Gemini Web helper",
        maximum=1_000_000,
    )
    if _sha256(helper) != GEMINI_HELPER_SHA256:
        raise AdapterError(
            "GEMINI_WEB_HELPER_DRIFT",
            "Gemini Web helper differs from the adapter-bound source",
        )
    from scripts.gemini_web_subscription import run_subscription

    return run_subscription


def _evidence_directory(
    root: Path,
    *,
    request_sha256: str,
    identity: str,
) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "-", identity).strip("-.")
    if not safe:
        safe = "request"
    parent = root / "final-media-review-gemini-web-runs"
    parent.mkdir(mode=0o700, exist_ok=True)
    parent.chmod(0o700)
    target = parent / f"{safe[:80]}-{request_sha256[:24]}"
    try:
        target.mkdir(mode=0o700)
    except FileExistsError as exc:
        raise AdapterError(
            "GEMINI_WEB_PROVIDER_REPLAY_BLOCKED",
            "provider evidence directory already exists",
        ) from exc
    return target.resolve(strict=True)


def _write_text(path: Path, text: str) -> None:
    payload = text.encode("utf-8")
    if not payload or len(payload) > _MAX_RESPONSE_BYTES:
        raise AdapterError(
            "GEMINI_WEB_INPUT_INVALID", "provider prompt size outside range"
        )
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
        0o600,
    )
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())


def _validate_provider_receipt(
    receipt_path: Path,
    returned: Mapping[str, object],
    *,
    media: Path,
    prompt: Path,
    raw_response: Path,
    config: WebConfig,
) -> dict[str, object]:
    saved = _read_json(receipt_path, label="Gemini Web receipt")
    if dict(returned) != saved:
        raise AdapterError(
            "GEMINI_WEB_RECEIPT_INVALID", "returned and saved receipts differ"
        )
    if (
        saved.get("provider") != PROVIDER
        or saved.get("mode") != "run"
        or saved.get("status") != "SUCCESS"
        or saved.get("requested_model_label") != config.model_label
        or " ".join(str(saved.get("observed_model_label") or "").split())
        != " ".join(config.model_label.split())
        or saved.get("video_sha256") != _sha256(media)
        or saved.get("prompt_sha256") != _sha256(prompt)
    ):
        raise AdapterError(
            "GEMINI_WEB_PROVIDER_FAILED",
            f"Gemini Web call did not complete successfully: {saved.get('status')}",
        )
    response_binding = saved.get("raw_response_path")
    if not isinstance(response_binding, str):
        raise AdapterError(
            "GEMINI_WEB_RECEIPT_INVALID", "provider raw response path missing"
        )
    actual_response = _regular_file(
        response_binding,
        label="Gemini Web raw response",
        maximum=_MAX_RESPONSE_BYTES,
    )
    if (
        actual_response != raw_response.resolve(strict=True)
        or saved.get("raw_response_sha256") != _sha256(actual_response)
    ):
        raise AdapterError(
            "GEMINI_WEB_RECEIPT_INVALID", "provider raw response binding drifted"
        )
    return saved


def _provider_call(
    *,
    media: Path,
    prompt: Path,
    call_root: Path,
    config: WebConfig,
    subscription_runner: Callable[..., Mapping[str, object]],
) -> tuple[str, dict[str, object]]:
    call_root.mkdir(mode=0o700)
    receipt_path = call_root / "receipt.json"
    response_path = call_root / "response.txt"
    screenshots = call_root / "screenshots"
    returned = subscription_runner(
        video_path=media,
        prompt_path=prompt,
        model_label=config.model_label,
        profile_dir=config.profile_dir,
        receipt_path=receipt_path,
        response_out=response_path,
        screenshot_dir=screenshots,
        headless=config.headless,
        direct_cdp=config.direct_cdp,
        browser_executable=config.browser_executable,
        xvfb_bin=config.xvfb_bin,
        display=config.display,
        cdp_port=config.cdp_port,
        upload_timeout_seconds=config.upload_timeout_seconds,
        response_timeout_seconds=config.response_timeout_seconds,
        stability_polls=config.stability_polls,
    )
    if not isinstance(returned, Mapping):
        raise AdapterError(
            "GEMINI_WEB_RECEIPT_INVALID", "provider returned no receipt object"
        )
    receipt = _validate_provider_receipt(
        receipt_path,
        returned,
        media=media,
        prompt=prompt,
        raw_response=response_path,
        config=config,
    )
    try:
        raw = response_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise AdapterError(
            "GEMINI_WEB_RESPONSE_INVALID", "provider response is not UTF-8"
        ) from exc
    return raw, {
        "receipt": _binding(receipt_path),
        "raw_response": _binding(response_path),
        "observed_model_label": receipt.get("observed_model_label"),
        "backend_model_status": receipt.get("backend_model_status"),
    }


def _sentinel_request(
    request_path: Path,
    *,
    input_path: Path,
    prompt_path: Path,
) -> tuple[dict[str, object], str]:
    request_bytes = _regular_file(
        request_path, label="sentinel request", maximum=_MAX_JSON_BYTES
    ).read_bytes()
    request_sha = hashlib.sha256(request_bytes).hexdigest()
    request = _read_json(request_path, label="sentinel request")
    expected = {
        "schema_version",
        "challenge_id",
        "provider",
        "model",
        "endpoint_family",
        "coverage",
        "input_sha256",
        "prompt_sha256",
        "answer_disclosed_to_adapter",
    }
    if (
        set(request) != expected
        or request.get("schema_version") != SENTINEL_REQUEST_SCHEMA
        or request.get("provider") != PROVIDER
        or not isinstance(request.get("challenge_id"), str)
        or not isinstance(request.get("model"), str)
        or not isinstance(request.get("endpoint_family"), str)
        or request.get("answer_disclosed_to_adapter") is not False
        or _sha256(input_path) != _normalized_sha(
            request.get("input_sha256"), label="sentinel input"
        )
        or _sha256(prompt_path) != _normalized_sha(
            request.get("prompt_sha256"), label="sentinel prompt"
        )
    ):
        raise AdapterError(
            "GEMINI_WEB_SENTINEL_REQUEST_INVALID",
            "sentinel request or exact media binding is invalid",
        )
    return request, request_sha


def _sentinel_response(
    *,
    request: Mapping[str, object],
    request_sha: str,
    raw: str,
) -> dict[str, object]:
    observed = raw.strip()
    if re.fullmatch(r"[0-9]{8}", observed) is None:
        raise AdapterError(
            "GEMINI_WEB_SENTINEL_RESPONSE_INVALID",
            "hidden challenge response is not exactly eight digits",
        )
    return {
        "schema_version": SENTINEL_RESPONSE_SCHEMA,
        "challenge_id": request["challenge_id"],
        "request_sha256": request_sha,
        "observed_answer": observed,
        "observed_answer_sha256": hashlib.sha256(
            observed.encode("utf-8")
        ).hexdigest(),
    }


def run_sentinel(
    *,
    request_path: Path,
    response_path: Path,
    input_path: Path,
    prompt_path: Path,
    environment: Mapping[str, str],
    subscription_runner: Callable[..., Mapping[str, object]],
    adapter_path: Path,
) -> dict[str, object]:
    root = _safe_runtime_root(environment)
    media = _regular_file(input_path, label="sentinel media", maximum=_MAX_MEDIA_BYTES)
    prompt = _regular_file(prompt_path, label="sentinel prompt", maximum=1_000_000)
    request, request_sha = _sentinel_request(
        request_path, input_path=media, prompt_path=prompt
    )
    _verify_adapter_identity(
        root=root,
        manifest_name=SENTINEL_RUNTIME_FILENAME,
        adapter_path=adapter_path,
        provider=str(request["provider"]),
        model=str(request["model"]),
        endpoint_family=str(request["endpoint_family"]),
    )
    config = _web_config(environment)
    if request["model"] != gemini_web_route_model_id(
        config.model_label, config.profile_dir
    ):
        raise AdapterError(
            "GEMINI_WEB_RUNTIME_MISMATCH",
            "sentinel model identity differs from the visible label/profile route",
        )
    evidence = _evidence_directory(
        root,
        request_sha256=request_sha,
        identity=str(request["challenge_id"]),
    )
    raw, provider_evidence = _provider_call(
        media=media,
        prompt=prompt,
        call_root=evidence / "provider",
        config=config,
        subscription_runner=subscription_runner,
    )
    response = _sentinel_response(
        request=request, request_sha=request_sha, raw=raw
    )
    _write_create_only(response_path, response)
    _write_create_only(
        evidence / "adapter-evidence.json",
        {
            "schema_version": "final-media-review-gemini-web-evidence.v1",
            "mode": "sentinel",
            "request_sha256": request_sha,
            "challenge_id": request["challenge_id"],
            "input_sha256": _sha256(media),
            "prompt_sha256": _sha256(prompt),
            "provider_evidence": provider_evidence,
            "response_sha256": _sha256(response_path),
        },
    )
    return response


def _strict_json_object(raw: str, *, modality: str) -> dict[str, object]:
    stripped = raw.strip()
    if stripped.startswith("```"):
        match = re.fullmatch(r"```(?:json)?\s*(\{.*\})\s*```", stripped, re.S)
        if match is None:
            raise AdapterError(
                "GEMINI_WEB_RESPONSE_INVALID", f"{modality} response fence invalid"
            )
        stripped = match.group(1)
    try:
        value = json.loads(stripped)
    except json.JSONDecodeError as exc:
        raise AdapterError(
            "GEMINI_WEB_RESPONSE_INVALID", f"{modality} response is not JSON"
        ) from exc
    if not isinstance(value, dict) or set(value) != {"status", "observations"}:
        raise AdapterError(
            "GEMINI_WEB_RESPONSE_INVALID",
            f"{modality} response fields invalid",
        )
    status = value.get("status")
    observations = value.get("observations")
    if status not in {"PASS", "BLOCK"} or not isinstance(observations, list):
        raise AdapterError(
            "GEMINI_WEB_RESPONSE_INVALID",
            f"{modality} status or observations invalid",
        )
    if not 1 <= len(observations) <= 100:
        raise AdapterError(
            "GEMINI_WEB_RESPONSE_INVALID",
            f"{modality} observations count invalid",
        )
    normalized: list[dict[str, str]] = []
    for row in observations:
        if not isinstance(row, Mapping) or set(row) != {
            "point_id",
            "verdict",
            "detail",
        }:
            raise AdapterError(
                "GEMINI_WEB_RESPONSE_INVALID",
                f"{modality} observation fields invalid",
            )
        point_id = row.get("point_id")
        verdict = row.get("verdict")
        detail = row.get("detail")
        if (
            not isinstance(point_id, str)
            or not 1 <= len(point_id) <= 128
            or verdict not in {"PASS", "BLOCK"}
            or not isinstance(detail, str)
            or not 1 <= len(detail) <= 2000
        ):
            raise AdapterError(
                "GEMINI_WEB_RESPONSE_INVALID",
                f"{modality} observation value invalid",
            )
        normalized.append(
            {"point_id": point_id, "verdict": str(verdict), "detail": detail}
        )
    verdicts = {row["verdict"] for row in normalized}
    if (status == "PASS" and verdicts != {"PASS"}) or (
        status == "BLOCK" and "BLOCK" not in verdicts
    ):
        raise AdapterError(
            "GEMINI_WEB_RESPONSE_INVALID",
            f"{modality} status contradicts observations",
        )
    return {"status": status, "observations": normalized}


def _review_prompt(
    request: Mapping[str, object], *, modality: str
) -> str:
    plan = json.dumps(
        request.get("review_plan"),
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    )
    medium = (
        "continuous exact final MP4 from 0 seconds through EOS. Inspect every "
        "scene transition, burned subtitle interval, subtitle-free interval, "
        "opening and ending. Use the attached video's own audio only as a "
        "secondary synchronization cue; the independent WAV reviewer owns the "
        "full-audio verdict."
        if modality == "continuous_source_video"
        else "exact full-media WAV from 0 seconds through EOS. Listen across "
        "the complete duration, including every review-plan interval and all "
        "gaps. Judge spoken wording, speaker/source attribution, abrupt cuts, "
        "silence and ending completeness. Do not infer pixels from this audio."
    )
    return f"""You are one fail-closed final-media reviewer. Review the attached
{medium}

Candidate: {request.get('candidate_id')}
Exact source video SHA256: {request.get('source_video', {}).get('sha256') if isinstance(request.get('source_video'), Mapping) else None}
Exact full audio SHA256: {request.get('raw_audio', {}).get('sha256') if isinstance(request.get('raw_audio'), Mapping) else None}

Review plan (authoritative requested checks):
{plan}

Return JSON only, with exactly:
{{"status":"PASS|BLOCK","observations":[
  {{"point_id":"review point or whole-media id","verdict":"PASS|BLOCK","detail":"specific observed evidence"}}
]}}

PASS only when the complete attached medium supports every applicable review
point and no unlisted whole-media defect is observed. BLOCK on any uncertainty,
unreviewed interval, contradiction, truncation, duplicate/incorrect subtitle,
or modality limitation. Do not claim the other attachment was inspected.
"""


def _final_request(
    request_path: Path,
    *,
    environment: Mapping[str, str],
) -> tuple[dict[str, object], str, Path, Path]:
    target = _regular_file(
        request_path, label="raw-AV request", maximum=_MAX_JSON_BYTES
    )
    payload = target.read_bytes()
    request_sha = hashlib.sha256(payload).hexdigest()
    expected_env = environment.get("AUTOSLICE_RAW_AV_REQUEST_SHA256")
    if expected_env and expected_env != request_sha:
        raise AdapterError(
            "GEMINI_WEB_REQUEST_BINDING_MISMATCH",
            "raw-AV request differs from executor binding",
        )
    request = _read_json(target, label="raw-AV request")
    if (
        request.get("schema_version") != RAW_AV_REQUEST_SCHEMA
        or request.get("provider") != PROVIDER
        or not isinstance(request.get("candidate_id"), str)
        or not isinstance(request.get("model"), str)
        or not isinstance(request.get("endpoint_family"), str)
        or not isinstance(request.get("capability_id"), str)
        or request.get("expected_result_schema_version") != RESULT_SCHEMA
    ):
        raise AdapterError(
            "GEMINI_WEB_REQUEST_INVALID", "raw-AV request identity invalid"
        )
    source = request.get("source_video")
    audio = request.get("raw_audio")
    if not isinstance(source, Mapping) or not isinstance(audio, Mapping):
        raise AdapterError(
            "GEMINI_WEB_REQUEST_INVALID", "raw-AV media bindings missing"
        )
    video = _regular_file(
        str(source.get("path") or ""),
        label="exact source video",
        maximum=_MAX_MEDIA_BYTES,
    )
    wav = _regular_file(
        str(audio.get("path") or ""),
        label="exact full audio",
        maximum=_MAX_MEDIA_BYTES,
    )
    if (
        video.stat().st_size != source.get("bytes")
        or _sha256(video)
        != _normalized_sha(source.get("sha256"), label="source video")
        or wav.stat().st_size != audio.get("bytes")
        or _sha256(wav)
        != _normalized_sha(audio.get("sha256"), label="full audio")
    ):
        raise AdapterError(
            "GEMINI_WEB_REQUEST_BINDING_MISMATCH",
            "raw-AV media bytes differ from request",
        )
    return request, request_sha, video, wav


def run_final_review(
    *,
    request_path: Path,
    response_path: Path,
    environment: Mapping[str, str],
    subscription_runner: Callable[..., Mapping[str, object]],
    adapter_path: Path,
) -> dict[str, object]:
    root = _safe_runtime_root(environment)
    request, request_sha, video, wav = _final_request(
        request_path, environment=environment
    )
    capability = _verify_adapter_identity(
        root=root,
        manifest_name=RAW_AV_RUNTIME_FILENAME,
        adapter_path=adapter_path,
        provider=str(request["provider"]),
        model=str(request["model"]),
        endpoint_family=str(request["endpoint_family"]),
        capability_id=str(request["capability_id"]),
    )
    config = _web_config(environment)
    if request["model"] != gemini_web_route_model_id(
        config.model_label, config.profile_dir
    ):
        raise AdapterError(
            "GEMINI_WEB_RUNTIME_MISMATCH",
            "review model identity differs from the visible label/profile route",
        )
    evidence = _evidence_directory(
        root,
        request_sha256=request_sha,
        identity=str(request["candidate_id"]),
    )
    video_prompt = evidence / "video-prompt.txt"
    audio_prompt = evidence / "audio-prompt.txt"
    _write_text(
        video_prompt,
        _review_prompt(request, modality="continuous_source_video"),
    )
    _write_text(audio_prompt, _review_prompt(request, modality="raw_audio"))
    video_raw, video_evidence = _provider_call(
        media=video,
        prompt=video_prompt,
        call_root=evidence / "video",
        config=config,
        subscription_runner=subscription_runner,
    )
    audio_raw, audio_evidence = _provider_call(
        media=wav,
        prompt=audio_prompt,
        call_root=evidence / "audio",
        config=config,
        subscription_runner=subscription_runner,
    )
    video_review = _strict_json_object(
        video_raw, modality="continuous_source_video"
    )
    audio_review = _strict_json_object(audio_raw, modality="raw_audio")
    status = (
        "PASS"
        if video_review["status"] == audio_review["status"] == "PASS"
        else "BLOCK"
    )
    observations: list[dict[str, object]] = []
    for modality, review in (
        ("continuous_source_video", video_review),
        ("raw_audio", audio_review),
    ):
        for row in review["observations"]:
            observations.append({"modality": modality, **dict(row)})
    evidence_document = {
        "schema_version": "final-media-review-gemini-web-evidence.v1",
        "mode": "final_review",
        "request_sha256": request_sha,
        "candidate_id": request["candidate_id"],
        "source_video_sha256": request["source_video"]["sha256"],
        "raw_audio_sha256": request["raw_audio"]["sha256"],
        "video_provider_evidence": video_evidence,
        "audio_provider_evidence": audio_evidence,
        "video_status": video_review["status"],
        "audio_status": audio_review["status"],
        "combined_status": status,
    }
    evidence_path = evidence / "adapter-evidence.json"
    _write_create_only(evidence_path, evidence_document)
    observations.append(
        {
            "modality": "transport",
            "point_id": "gemini-web-provider-evidence",
            "verdict": "PASS",
            "detail": (
                "Two independent Gemini Web turns consumed the exact bound MP4 "
                "and exact bound full-media WAV; durable provider evidence is "
                f"SHA256 {_sha256(evidence_path)}."
            ),
            "provider_evidence": _binding(evidence_path),
        }
    )
    result = {
        "schema_version": RESULT_SCHEMA,
        "candidate_id": request["candidate_id"],
        "source_video_sha256": request["source_video"]["sha256"],
        "asset_manifest_sha256": request["asset_manifest_sha256"],
        "status": status,
        "content_review_status": status,
        "observations": observations,
    }
    receipt = {
        "schema_version": RAW_AV_RECEIPT_SCHEMA,
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
    envelope = {
        "schema_version": RAW_AV_RESPONSE_SCHEMA,
        "request_sha256": request_sha,
        "transport_receipt": receipt,
        "result": result,
    }
    _write_create_only(response_path, envelope)
    return envelope


def _error_document(
    exc: AdapterError,
    *,
    request_path: Path,
    sentinel: bool,
) -> dict[str, object]:
    request_sha = (
        _sha256(request_path)
        if request_path.is_file() and not request_path.is_symlink()
        else "0" * 64
    )
    if sentinel:
        return {
            "schema_version": SENTINEL_ERROR_SCHEMA,
            "request_sha256": request_sha,
            "error": {
                "safe_reason": exc.safe_reason,
                "provider_http_status": exc.provider_http_status,
                "message": exc.message[:1000],
            },
        }
    return {
        "schema_version": RAW_AV_RESPONSE_SCHEMA,
        "request_sha256": request_sha,
        "error": {
            "safe_reason": exc.safe_reason,
            "provider_http_status": exc.provider_http_status,
            "message": exc.message[:1000],
        },
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", required=True, type=Path)
    parser.add_argument("--response", required=True, type=Path)
    parser.add_argument("--input", type=Path)
    parser.add_argument("--prompt", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    sentinel = args.input is not None or args.prompt is not None
    try:
        if (args.input is None) != (args.prompt is None):
            raise AdapterError(
                "GEMINI_WEB_INPUT_INVALID",
                "sentinel mode requires both --input and --prompt",
            )
        runner = _helper_runner()
        if sentinel:
            run_sentinel(
                request_path=args.request,
                response_path=args.response,
                input_path=args.input,
                prompt_path=args.prompt,
                environment=os.environ,
                subscription_runner=runner,
                adapter_path=Path(__file__).resolve(),
            )
        else:
            run_final_review(
                request_path=args.request,
                response_path=args.response,
                environment=os.environ,
                subscription_runner=runner,
                adapter_path=Path(__file__).resolve(),
            )
        return 0
    except AdapterError as exc:
        try:
            _write_create_only(
                args.response,
                _error_document(
                    exc, request_path=args.request, sentinel=sentinel
                ),
            )
        except AdapterError:
            pass
        print(f"{exc.safe_reason}: {exc.message}", file=sys.stderr)
        return exc.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
