"""Create-only runtime manifest for the explicit Gemini Web review route.

This module does not claim that Gemini Web supports either medium.  It only
materializes one hash-bound sentinel candidate when the final-media route is
explicitly enabled and all local runtime inputs are present.  The independent
hidden audio/video sentinel remains the sole authority that can create a
short-lived raw-AV capability.
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


PROVIDER = "gemini_web_subscription"
ENDPOINT_FAMILY = "consumer_web_ui"
CAPABILITY_ID = "gemini-web-subscription-dual-media"
ENABLED_ENV = "AUTOSLICE_FINAL_MEDIA_GEMINI_WEB_ENABLED"
PROFILE_ENV = "AUTOSLICE_FINAL_MEDIA_GEMINI_WEB_PROFILE_DIR"
MODEL_LABEL_ENV = "AUTOSLICE_FINAL_MEDIA_GEMINI_WEB_MODEL_LABEL"
ENTITY_PROFILE_ENV = "AUTOSLICE_ENTITY_AUDIO_GEMINI_WEB_PROFILE_DIR"
ENTITY_MODEL_LABEL_ENV = "AUTOSLICE_ENTITY_AUDIO_GEMINI_WEB_MODEL_LABEL"
FFMPEG_ENV = "AUTOSLICE_FINAL_MEDIA_REVIEW_FFMPEG"
FFPROBE_ENV = "AUTOSLICE_FINAL_MEDIA_REVIEW_FFPROBE"
RUNNER_RELATIVE = Path(
    "repo/scripts/run_final_media_review_model_capability_sentinel.py"
)
ADAPTER_RELATIVE = Path("repo/scripts/final_media_review_gemini_web_adapter.py")
_TIMEOUT_SECONDS = 1_200
_BACKOFF = {
    "base_seconds": 60,
    "max_seconds": 3_600,
    "max_rate_limit_attempts": 3,
}
_MAX_EXECUTABLE_BYTES = 32_000_000
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


class FinalMediaReviewGeminiWebRuntimeError(ValueError):
    """An explicit provider-free runtime provisioning failure."""

    def __init__(self, reason_code: str, detail: str):
        super().__init__(f"{reason_code}: {detail}")
        self.reason_code = reason_code
        self.detail = detail


def _error(
    reason_code: str, detail: str
) -> FinalMediaReviewGeminiWebRuntimeError:
    return FinalMediaReviewGeminiWebRuntimeError(reason_code, detail)


def _selected_environment(
    environment: Mapping[str, str] | None,
) -> dict[str, str]:
    return dict(os.environ if environment is None else environment)


def _enabled(environment: Mapping[str, str]) -> bool:
    raw = environment.get(ENABLED_ENV)
    if raw is None or not raw.strip():
        return False
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise _error(
        "FINAL_MEDIA_REVIEW_GEMINI_WEB_CONFIG_INVALID",
        f"{ENABLED_ENV} must be an explicit boolean",
    )


def gemini_web_runtime_enabled(
    environment: Mapping[str, str] | None,
) -> bool:
    """Return whether the explicit Web route is enabled."""

    return _enabled(_selected_environment(environment))


def _safe_root(value: str | Path) -> Path:
    raw = Path(value).expanduser()
    if not raw.is_absolute() or raw.is_symlink():
        raise _error(
            "FINAL_MEDIA_REVIEW_GEMINI_WEB_PATH_INVALID",
            "runtime root must be an absolute non-symlink directory",
        )
    try:
        resolved = raw.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise _error(
            "FINAL_MEDIA_REVIEW_GEMINI_WEB_PATH_INVALID",
            "runtime root is unavailable",
        ) from exc
    if not resolved.is_dir():
        raise _error(
            "FINAL_MEDIA_REVIEW_GEMINI_WEB_PATH_INVALID",
            "runtime root is not a directory",
        )
    return resolved


def _safe_profile(raw: str) -> Path:
    path = Path(raw).expanduser()
    try:
        info = path.lstat()
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise _error(
            "FINAL_MEDIA_REVIEW_GEMINI_WEB_CONFIG_INVALID",
            "Gemini Web profile directory is unavailable",
        ) from exc
    if (
        path.is_symlink()
        or not stat.S_ISDIR(info.st_mode)
        or not resolved.is_dir()
        or stat.S_IMODE(info.st_mode) & 0o022
    ):
        raise _error(
            "FINAL_MEDIA_REVIEW_GEMINI_WEB_CONFIG_INVALID",
            "Gemini Web profile directory is unsafe",
        )
    return resolved


def _first_environment(
    environment: Mapping[str, str], *names: str
) -> str | None:
    for name in names:
        value = environment.get(name)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def gemini_web_route_model_id(model_label: str, profile_dir: Path) -> str:
    """Return a stable ID that binds the visible route label and profile path."""

    label = " ".join(model_label.split())
    if not 1 <= len(label) <= 200:
        raise _error(
            "FINAL_MEDIA_REVIEW_GEMINI_WEB_CONFIG_INVALID",
            "Gemini Web visible model label is invalid",
        )
    slug = re.sub(r"[^a-z0-9_.-]+", "-", label.lower()).strip("-.")
    if not slug:
        slug = "visible-model"
    digest = hashlib.sha256(
        (label + "\0" + str(profile_dir)).encode("utf-8")
    ).hexdigest()[:12]
    value = f"gemini-web-{slug[:80]}-{digest}"
    if _ID_RE.fullmatch(value) is None:
        raise _error(
            "FINAL_MEDIA_REVIEW_GEMINI_WEB_CONFIG_INVALID",
            "derived Gemini Web route model ID is invalid",
        )
    return value


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _bound_executable(
    path: Path,
    *,
    label: str,
    contained_root: Path | None,
) -> tuple[Path, str]:
    try:
        info = path.lstat()
        resolved = path.resolve(strict=True)
        if contained_root is not None:
            resolved.relative_to(contained_root)
    except (OSError, RuntimeError, ValueError) as exc:
        raise _error(
            "FINAL_MEDIA_REVIEW_GEMINI_WEB_RUNTIME_UNAVAILABLE",
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
        or not mode & 0o111
    ):
        raise _error(
            "FINAL_MEDIA_REVIEW_GEMINI_WEB_RUNTIME_UNAVAILABLE",
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
            "FINAL_MEDIA_REVIEW_GEMINI_WEB_RUNTIME_UNAVAILABLE",
            f"{name} executable is unavailable",
        )
    return _bound_executable(
        located,
        label=name,
        contained_root=None,
    )


def _manifest(
    root: Path, environment: Mapping[str, str]
) -> dict[str, object]:
    profile_raw = _first_environment(
        environment, PROFILE_ENV, ENTITY_PROFILE_ENV
    )
    model_label = _first_environment(
        environment, MODEL_LABEL_ENV, ENTITY_MODEL_LABEL_ENV
    )
    if not profile_raw or not model_label:
        raise _error(
            "FINAL_MEDIA_REVIEW_GEMINI_WEB_CONFIG_MISSING",
            "explicit final-media Gemini Web profile and model label are required",
        )
    profile = _safe_profile(profile_raw)
    model = gemini_web_route_model_id(model_label, profile)
    runner, runner_sha = _bound_executable(
        root / RUNNER_RELATIVE,
        label="sentinel runner",
        contained_root=root,
    )
    adapter, adapter_sha = _bound_executable(
        root / ADAPTER_RELATIVE,
        label="Gemini Web adapter",
        contained_root=root,
    )
    ffmpeg, ffmpeg_sha = _external_tool(
        environment, name="ffmpeg", override=FFMPEG_ENV
    )
    ffprobe, ffprobe_sha = _external_tool(
        environment, name="ffprobe", override=FFPROBE_ENV
    )
    return {
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
    }


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


def _existing_matches(path: Path, payload: bytes, *, owner_uid: int) -> bool:
    try:
        info = path.lstat()
        data = path.read_bytes()
    except OSError as exc:
        raise _error(
            "FINAL_MEDIA_REVIEW_GEMINI_WEB_RUNTIME_CONFLICT",
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
            "FINAL_MEDIA_REVIEW_GEMINI_WEB_RUNTIME_CONFLICT",
            "existing sentinel runtime manifest differs from explicit config",
        )
    return True


def ensure_gemini_web_sentinel_runtime(
    runtime_root: str | Path,
    *,
    environment: Mapping[str, str] | None = None,
) -> dict[str, object]:
    """Create or replay one explicit provider-free sentinel runtime manifest."""

    selected = _selected_environment(environment)
    if not _enabled(selected):
        return {
            "status": "GEMINI_WEB_RUNTIME_DISABLED",
            "created": False,
            "provider_calls": 0,
        }
    root = _safe_root(runtime_root)
    value = _manifest(root, selected)
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
            "FINAL_MEDIA_REVIEW_GEMINI_WEB_RUNTIME_MISSING",
            "created sentinel runtime manifest could not be replayed",
        )
    return {
        "status": (
            "GEMINI_WEB_SENTINEL_RUNTIME_CREATED"
            if created
            else "GEMINI_WEB_SENTINEL_RUNTIME_REUSED"
        ),
        "created": created,
        "provider_calls": 0,
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
    "FinalMediaReviewGeminiWebRuntimeError",
    "MODEL_LABEL_ENV",
    "PROFILE_ENV",
    "PROVIDER",
    "RUNNER_RELATIVE",
    "ensure_gemini_web_sentinel_runtime",
    "gemini_web_route_model_id",
    "gemini_web_runtime_enabled",
]
