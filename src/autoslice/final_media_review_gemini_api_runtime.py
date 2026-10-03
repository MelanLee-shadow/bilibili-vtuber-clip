"""Create-only runtime manifest for direct Gemini Files API final review.

The route is explicit and provider-free at provisioning time.  It binds the
normal hidden-sentinel runner, the direct API adapter, ffmpeg/ffprobe and one
configured model ID.  At least one free Gemini API key must already be present
in the ordinary runner environment, but no credential value is persisted.

Provisioning never claims audio/video capability.  Only the independent
hidden dual-media sentinel may seal the short-lived raw-AV capability.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
from typing import Mapping

from src.autoslice.final_media_review_inputs import RESULT_SCHEMA_VERSION
from src.autoslice.final_media_review_model_capability_sentinel import (
    SENTINEL_RUNTIME_FILENAME,
    SENTINEL_RUNTIME_SCHEMA_VERSION,
    FinalMediaReviewModelCapabilitySentinelError,
    load_sentinel_runtime,
)
from src.autoslice.gemini_file_api import configured_free_keys


PROVIDER = "gemini_api"
VIDEO_FPS = 5.0
ENDPOINT_FAMILY = "files_api_generate_content_static_5fps"
CAPABILITY_ID = "gemini-api-files-dual-media"
ENABLED_ENV = "AUTOSLICE_FINAL_MEDIA_GEMINI_API_ENABLED"
MODEL_ENV = "AUTOSLICE_FINAL_MEDIA_GEMINI_API_MODEL"
FFMPEG_ENV = "AUTOSLICE_FINAL_MEDIA_REVIEW_FFMPEG"
FFPROBE_ENV = "AUTOSLICE_FINAL_MEDIA_REVIEW_FFPROBE"
RUNNER_RELATIVE = Path(
    "repo/scripts/run_final_media_review_model_capability_sentinel.py"
)
ADAPTER_RELATIVE = Path("repo/scripts/final_media_review_gemini_api_adapter.py")
HELPER_RELATIVE = Path("repo/src/autoslice/gemini_file_api.py")
_TIMEOUT_SECONDS = 1_800
_BACKOFF = {
    "base_seconds": 60,
    "max_seconds": 3_600,
    "max_rate_limit_attempts": 3,
}
_MAX_EXECUTABLE_BYTES = 32_000_000
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


class FinalMediaReviewGeminiApiRuntimeError(ValueError):
    """An explicit provider-free direct API runtime provisioning failure."""

    def __init__(self, reason_code: str, detail: str):
        super().__init__(f"{reason_code}: {detail}")
        self.reason_code = reason_code
        self.detail = detail


def _error(
    reason_code: str, detail: str
) -> FinalMediaReviewGeminiApiRuntimeError:
    return FinalMediaReviewGeminiApiRuntimeError(reason_code, detail)


def _selected_environment(
    environment: Mapping[str, str] | None,
) -> dict[str, str]:
    return dict(os.environ if environment is None else environment)


def _bool_value(raw: str | None, *, name: str) -> bool:
    if raw is None or not raw.strip():
        return False
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise _error(
        "FINAL_MEDIA_REVIEW_GEMINI_API_CONFIG_INVALID",
        f"{name} must be an explicit boolean",
    )


def gemini_api_runtime_enabled(
    environment: Mapping[str, str] | None,
) -> bool:
    selected = _selected_environment(environment)
    return _bool_value(selected.get(ENABLED_ENV), name=ENABLED_ENV)


def _safe_root(value: str | Path) -> Path:
    raw = Path(value).expanduser()
    if not raw.is_absolute() or raw.is_symlink():
        raise _error(
            "FINAL_MEDIA_REVIEW_GEMINI_API_PATH_INVALID",
            "runtime root must be an absolute non-symlink directory",
        )
    try:
        resolved = raw.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise _error(
            "FINAL_MEDIA_REVIEW_GEMINI_API_PATH_INVALID",
            "runtime root is unavailable",
        ) from exc
    if not resolved.is_dir():
        raise _error(
            "FINAL_MEDIA_REVIEW_GEMINI_API_PATH_INVALID",
            "runtime root is not a directory",
        )
    return resolved


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _bound_regular(
    path: Path,
    *,
    label: str,
    contained_root: Path | None,
    executable: bool,
) -> tuple[Path, str]:
    try:
        info = path.lstat()
        resolved = path.resolve(strict=True)
        if contained_root is not None:
            resolved.relative_to(contained_root)
    except (OSError, RuntimeError, ValueError) as exc:
        raise _error(
            "FINAL_MEDIA_REVIEW_GEMINI_API_RUNTIME_UNAVAILABLE",
            f"{label} is unavailable or escapes the runtime",
        ) from exc
    mode = stat.S_IMODE(info.st_mode)
    if (
        path.is_symlink()
        or not stat.S_ISREG(info.st_mode)
        or not resolved.is_file()
        or info.st_nlink != 1
        or not 0 < info.st_size <= _MAX_EXECUTABLE_BYTES
        or mode & 0o022
        or (executable and not mode & 0o111)
    ):
        raise _error(
            "FINAL_MEDIA_REVIEW_GEMINI_API_RUNTIME_UNAVAILABLE",
            f"{label} mode, type, or size is invalid",
        )
    return resolved, _sha256(resolved)


def _external_tool(
    environment: Mapping[str, str], *, name: str, override: str
) -> tuple[Path, str]:
    raw = environment.get(override)
    located = Path(raw).expanduser() if raw else None
    if located is None:
        command = shutil.which(name, path=environment.get("PATH"))
        if command:
            located = Path(command)
    if located is None:
        raise _error(
            "FINAL_MEDIA_REVIEW_GEMINI_API_RUNTIME_UNAVAILABLE",
            f"{name} executable is unavailable",
        )
    return _bound_regular(
        located,
        label=name,
        contained_root=None,
        executable=True,
    )


def _model(environment: Mapping[str, str]) -> str:
    raw = environment.get(MODEL_ENV)
    model = raw.strip() if isinstance(raw, str) else ""
    if not model or _ID_RE.fullmatch(model) is None:
        raise _error(
            "FINAL_MEDIA_REVIEW_GEMINI_API_CONFIG_MISSING",
            f"{MODEL_ENV} must name one exact Gemini API model",
        )
    return model


def _manifest(
    root: Path, environment: Mapping[str, str]
) -> tuple[dict[str, object], int]:
    model = _model(environment)
    keys = configured_free_keys(environment)
    if not keys:
        raise _error(
            "FINAL_MEDIA_REVIEW_GEMINI_API_KEYS_MISSING",
            "at least one configured free Gemini API key is required",
        )
    runner, runner_sha = _bound_regular(
        root / RUNNER_RELATIVE,
        label="sentinel runner",
        contained_root=root,
        executable=True,
    )
    adapter, adapter_sha = _bound_regular(
        root / ADAPTER_RELATIVE,
        label="Gemini API adapter",
        contained_root=root,
        executable=True,
    )
    _bound_regular(
        root / HELPER_RELATIVE,
        label="Gemini Files API helper",
        contained_root=root,
        executable=False,
    )
    ffmpeg, ffmpeg_sha = _external_tool(
        environment, name="ffmpeg", override=FFMPEG_ENV
    )
    ffprobe, ffprobe_sha = _external_tool(
        environment, name="ffprobe", override=FFPROBE_ENV
    )
    return (
        {
            "schema_version": SENTINEL_RUNTIME_SCHEMA_VERSION,
            "capability_id": CAPABILITY_ID,
            "provider": PROVIDER,
            "transport": "content_bound_command",
            "model": model,
            "endpoint_family": ENDPOINT_FAMILY,
            "runner": {
                "path": runner.relative_to(root).as_posix(),
                "sha256": runner_sha,
            },
            "adapter": {
                "path": adapter.relative_to(root).as_posix(),
                "sha256": adapter_sha,
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
            "timeout_seconds": _TIMEOUT_SECONDS,
            "ffmpeg": {"path": str(ffmpeg), "sha256": ffmpeg_sha},
            "ffprobe": {"path": str(ffprobe), "sha256": ffprobe_sha},
            "backoff": dict(_BACKOFF),
            "result_schema_version": RESULT_SCHEMA_VERSION,
        },
        len(keys),
    )


def _encoded(value: Mapping[str, object]) -> bytes:
    return (
        json.dumps(
            dict(value),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def _existing_matches(path: Path, payload: bytes, *, owner_uid: int) -> None:
    try:
        info = path.lstat()
        data = path.read_bytes()
    except OSError as exc:
        raise _error(
            "FINAL_MEDIA_REVIEW_GEMINI_API_RUNTIME_CONFLICT",
            "existing sentinel runtime manifest cannot be read",
        ) from exc
    if (
        path.is_symlink()
        or not stat.S_ISREG(info.st_mode)
        or info.st_nlink != 1
        or info.st_uid != owner_uid
        or stat.S_IMODE(info.st_mode) & 0o077
        or data != payload
    ):
        raise _error(
            "FINAL_MEDIA_REVIEW_GEMINI_API_RUNTIME_CONFLICT",
            "existing sentinel runtime manifest differs from explicit config",
        )


def ensure_gemini_api_sentinel_runtime(
    runtime_root: str | Path,
    *,
    environment: Mapping[str, str] | None = None,
) -> dict[str, object]:
    """Create or replay one explicit provider-free direct API runtime."""

    selected = _selected_environment(environment)
    if not gemini_api_runtime_enabled(selected):
        return {
            "status": "GEMINI_API_RUNTIME_DISABLED",
            "created": False,
            "provider_calls": 0,
        }
    root = _safe_root(runtime_root)
    value, key_count = _manifest(root, selected)
    payload = _encoded(value)
    path = root / SENTINEL_RUNTIME_FILENAME
    created = False
    try:
        descriptor = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
        )
    except FileExistsError:
        _existing_matches(path, payload, owner_uid=root.stat().st_uid)
    else:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        directory = os.open(root, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        created = True
    try:
        replayed = load_sentinel_runtime(root)
    except FinalMediaReviewModelCapabilitySentinelError as exc:
        raise _error(exc.reason_code, exc.detail) from exc
    if replayed is None:
        raise _error(
            "FINAL_MEDIA_REVIEW_GEMINI_API_RUNTIME_MISSING",
            "created sentinel runtime manifest could not be replayed",
        )
    return {
        "status": (
            "GEMINI_API_SENTINEL_RUNTIME_CREATED"
            if created
            else "GEMINI_API_SENTINEL_RUNTIME_REUSED"
        ),
        "created": created,
        "provider_calls": 0,
        "configured_free_key_count": key_count,
        "paid_backup_allowed": False,
        "path": str(path),
        "sha256": _sha256(path),
        "bytes": path.stat().st_size,
        "provider": replayed["provider"],
        "model": replayed["model"],
        "endpoint_family": replayed["endpoint_family"],
        "runtime_contract_sha256": replayed["runtime_contract_sha256"],
    }


__all__ = [
    "ADAPTER_RELATIVE",
    "CAPABILITY_ID",
    "ENABLED_ENV",
    "ENDPOINT_FAMILY",
    "FFMPEG_ENV",
    "FFPROBE_ENV",
    "FinalMediaReviewGeminiApiRuntimeError",
    "HELPER_RELATIVE",
    "MODEL_ENV",
    "PROVIDER",
    "RUNNER_RELATIVE",
    "VIDEO_FPS",
    "ensure_gemini_api_sentinel_runtime",
    "gemini_api_runtime_enabled",
]
