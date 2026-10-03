"""Durable dual-media sentinel runner for autonomous raw-AV capability bootstrap.

This runner is the missing producer for the E581 seal contract.  It creates
real audio/video challenges, commits answer hashes before dispatch, invokes one
runtime-owned adapter under the shared provider slot, and persists restart-safe
challenge states.  A successful two-challenge run is sealed and projected into
the ordinary final-media raw-AV capability manifest.

It does not infer support from model names or adapter booleans.  Known
unsupported models are rejected without media generation or provider calls.
Timeouts and response loss become DISPATCH_AMBIGUOUS and are never replayed.
Only an explicit 429 response enters durable bounded backoff.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat
import subprocess
import tempfile
from typing import Callable, Iterator, Mapping

from src.autoslice.final_media_review_inputs import RESULT_SCHEMA_VERSION
from src.autoslice.final_media_review_model_capability import (
    KNOWN_UNSUPPORTED_DUAL_RAW_AV_MODELS,
)
from src.autoslice.final_media_review_model_capability_seal import (
    MODEL_CAPABILITY_SENTINEL_AUTHORITY,
    SENTINEL_REQUEST_SCHEMA_VERSION,
    SENTINEL_RESPONSE_SCHEMA_VERSION,
    SENTINEL_RESULT_SCHEMA_VERSION,
    SENTINEL_SECRET_SCHEMA_VERSION,
    seal_model_capability_attestation,
)
from src.autoslice.final_media_review_model_capability_sentinel_media import (
    AUDIO_TOKEN_DIGITS,
    VIDEO_TOKEN_DIGITS,
    generate_dtmf_wav,
    generate_temporal_video,
)
from src.autoslice.final_media_review_raw_av import (
    RUNTIME_CAPABILITY_FILENAME,
    RUNTIME_CAPABILITY_SCHEMA_VERSION,
)
from src.autoslice.provider_slots import (
    ProviderSlotError,
    ProviderSlotTimeout,
    provider_wait_for_call,
    runtime_provider_slot,
)


SENTINEL_RUNTIME_FILENAME = (
    "final-media-review-model-capability-sentinel-runtime.json"
)
SENTINEL_RUNTIME_SCHEMA_VERSION = (
    "final-media-review-model-capability-sentinel-runtime.v1"
)
SENTINEL_PLAN_SCHEMA_VERSION = (
    "final-media-review-model-capability-sentinel-plan.v1"
)
SENTINEL_STATE_SCHEMA_VERSION = (
    "final-media-review-model-capability-sentinel-state.v1"
)
SENTINEL_ADAPTER_ERROR_SCHEMA_VERSION = (
    "final-media-review-model-capability-sentinel-adapter-error.v1"
)
SENTINEL_RUNS_DIRECTORY = "model-capability-sentinel-runs"
SENTINEL_SEALS_DIRECTORY = "model-capability-seals"
SENTINEL_LOCK_FILENAME = ".model-capability-sentinel.lock"

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_PLACEHOLDER_RE = re.compile(r"\{[^{}]+\}")
_SENTINEL_PLACEHOLDERS = frozenset(
    {
        "{executable}",
        "{request_json}",
        "{response_json}",
        "{input_media}",
        "{prompt_file}",
    }
)
_FINAL_REVIEW_PLACEHOLDERS = frozenset(
    {"{executable}", "{request_json}", "{response_json}"}
)
_MAX_JSON_BYTES = 4_000_000
_MAX_EXECUTABLE_BYTES = 128 * 1024 * 1024
_MIN_VALIDITY_SECONDS = 60
_MAX_VALIDITY_SECONDS = 7 * 24 * 60 * 60


class FinalMediaReviewModelCapabilitySentinelError(ValueError):
    """Typed runner/configuration/state rejection."""

    def __init__(self, reason_code: str, detail: str):
        super().__init__(f"{reason_code}: {detail}")
        self.reason_code = reason_code
        self.detail = detail


def _error(
    reason_code: str, detail: str
) -> FinalMediaReviewModelCapabilitySentinelError:
    return FinalMediaReviewModelCapabilitySentinelError(reason_code, detail)


def _canonical_bytes(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise _error(
            "FINAL_MEDIA_REVIEW_SENTINEL_JSON_INVALID",
            "value is not canonical JSON",
        ) from exc


def _canonical_sha256(value: object) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _json_bytes(value: Mapping[str, object]) -> bytes:
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


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _normalize_sha(value: object, *, label: str) -> str:
    if not isinstance(value, str):
        raise _error(
            "FINAL_MEDIA_REVIEW_SENTINEL_BINDING_INVALID",
            f"{label} sha256 missing",
        )
    normalized = value.strip().lower()
    if normalized.startswith("sha256:"):
        normalized = normalized[7:]
    if _SHA256_RE.fullmatch(normalized) is None:
        raise _error(
            "FINAL_MEDIA_REVIEW_SENTINEL_BINDING_INVALID",
            f"{label} sha256 invalid",
        )
    return normalized


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_time(value: object, *, label: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise _error(
            "FINAL_MEDIA_REVIEW_SENTINEL_STATE_INVALID",
            f"{label} timestamp missing",
        )
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise _error(
            "FINAL_MEDIA_REVIEW_SENTINEL_STATE_INVALID",
            f"{label} timestamp invalid",
        ) from exc
    if parsed.tzinfo is None:
        raise _error(
            "FINAL_MEDIA_REVIEW_SENTINEL_STATE_INVALID",
            f"{label} timestamp must include timezone",
        )
    return parsed.astimezone(timezone.utc)


def _safe_root(value: str | Path) -> Path:
    raw = Path(value).expanduser()
    if not raw.is_absolute() or raw.is_symlink():
        raise _error(
            "FINAL_MEDIA_REVIEW_SENTINEL_PATH_INVALID",
            "runtime root must be an absolute non-symlink directory",
        )
    try:
        resolved = raw.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise _error(
            "FINAL_MEDIA_REVIEW_SENTINEL_PATH_INVALID",
            "runtime root is unavailable",
        ) from exc
    if not resolved.is_dir():
        raise _error(
            "FINAL_MEDIA_REVIEW_SENTINEL_PATH_INVALID",
            "runtime root is not a directory",
        )
    return resolved


def _relative_path(value: object, *, label: str) -> Path:
    if not isinstance(value, str) or not value:
        raise _error(
            "FINAL_MEDIA_REVIEW_SENTINEL_PATH_INVALID",
            f"{label} path missing",
        )
    path = Path(value)
    if path.is_absolute() or ".." in path.parts or path == Path("."):
        raise _error(
            "FINAL_MEDIA_REVIEW_SENTINEL_PATH_INVALID",
            f"{label} must be a contained relative path",
        )
    return path


def _stable_regular(
    path: Path,
    *,
    label: str,
    maximum: int,
    expected_uid: int | None,
    executable: bool = False,
    private: bool = False,
) -> dict[str, object]:
    try:
        info = path.lstat()
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise _error(
            "FINAL_MEDIA_REVIEW_SENTINEL_PATH_INVALID",
            f"{label} is unavailable",
        ) from exc
    mode = stat.S_IMODE(info.st_mode)
    if (
        path.is_symlink()
        or not stat.S_ISREG(info.st_mode)
        or not resolved.is_file()
        or info.st_nlink != 1
        or not 0 < info.st_size <= maximum
        or mode & 0o022
        or (expected_uid is not None and info.st_uid != expected_uid)
        or (executable and not mode & 0o111)
        or (private and mode & 0o077)
    ):
        raise _error(
            "FINAL_MEDIA_REVIEW_SENTINEL_PATH_INVALID",
            f"{label} mode, ownership, type, or size is invalid",
        )
    identity = (
        info.st_dev,
        info.st_ino,
        mode,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )
    descriptor = -1
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(
        os, "O_NOFOLLOW", 0
    )
    try:
        descriptor = os.open(resolved, flags)
        opened = os.fstat(descriptor)
        digest = hashlib.sha256()
        total = 0
        while total <= maximum:
            chunk = os.read(descriptor, min(1024 * 1024, maximum + 1 - total))
            if not chunk:
                break
            total += len(chunk)
            digest.update(chunk)
        after = os.fstat(descriptor)
    except OSError as exc:
        raise _error(
            "FINAL_MEDIA_REVIEW_SENTINEL_PATH_INVALID",
            f"{label} could not be read stably",
        ) from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    try:
        final = os.lstat(resolved)
    except OSError as exc:
        raise _error(
            "FINAL_MEDIA_REVIEW_SENTINEL_PATH_INVALID",
            f"{label} disappeared after reading",
        ) from exc
    observed = (
        opened.st_dev,
        opened.st_ino,
        stat.S_IMODE(opened.st_mode),
        opened.st_size,
        opened.st_mtime_ns,
        opened.st_ctime_ns,
    )
    after_identity = (
        after.st_dev,
        after.st_ino,
        stat.S_IMODE(after.st_mode),
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
    )
    final_identity = (
        final.st_dev,
        final.st_ino,
        stat.S_IMODE(final.st_mode),
        final.st_size,
        final.st_mtime_ns,
        final.st_ctime_ns,
    )
    if observed != identity or after_identity != identity or final_identity != identity or total != info.st_size:
        raise _error(
            "FINAL_MEDIA_REVIEW_SENTINEL_PATH_INVALID",
            f"{label} identity changed while reading",
        )
    return {
        "path": str(resolved),
        "sha256": digest.hexdigest(),
        "bytes": total,
        "mode": mode,
        "uid": info.st_uid,
    }


def _binding(path: Path, *, root: Path) -> dict[str, object]:
    resolved = path.resolve(strict=True)
    try:
        relative = resolved.relative_to(root).as_posix()
    except ValueError as exc:
        raise _error(
            "FINAL_MEDIA_REVIEW_SENTINEL_PATH_INVALID",
            f"binding escapes runtime root: {path}",
        ) from exc
    return {
        "path": relative,
        "sha256": _sha256(resolved),
        "bytes": resolved.stat().st_size,
    }


def _read_json(path: Path, *, label: str) -> dict[str, object]:
    stable = _stable_regular(
        path,
        label=label,
        maximum=_MAX_JSON_BYTES,
        expected_uid=None,
    )
    if stable["bytes"] > _MAX_JSON_BYTES:
        raise _error(
            "FINAL_MEDIA_REVIEW_SENTINEL_JSON_INVALID", f"{label} too large"
        )
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise _error(
            "FINAL_MEDIA_REVIEW_SENTINEL_JSON_INVALID",
            f"{label} is not JSON",
        ) from exc
    if not isinstance(value, dict):
        raise _error(
            "FINAL_MEDIA_REVIEW_SENTINEL_JSON_INVALID",
            f"{label} root must be an object",
        )
    return value


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_create_only(path: Path, payload: bytes, *, mode: int = 0o600) -> None:
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptor = -1
    try:
        descriptor = os.open(path, flags, mode)
        offset = 0
        while offset < len(payload):
            written = os.write(descriptor, payload[offset:])
            if written <= 0:
                raise OSError("short write")
            offset += written
        os.fsync(descriptor)
    except OSError as exc:
        raise _error(
            "FINAL_MEDIA_REVIEW_SENTINEL_WRITE_FAILED",
            f"create-only write failed: {path}",
        ) from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    _fsync_directory(path.parent)


def _write_or_match(path: Path, value: Mapping[str, object]) -> bool:
    payload = _json_bytes(value)
    if path.exists() or path.is_symlink():
        if path.is_symlink() or not path.is_file() or path.stat().st_nlink != 1:
            raise _error(
                "FINAL_MEDIA_REVIEW_SENTINEL_CONFLICT",
                f"existing target is unsafe: {path}",
            )
        if path.read_bytes() != payload:
            raise _error(
                "FINAL_MEDIA_REVIEW_SENTINEL_CONFLICT",
                f"existing target differs from deterministic projection: {path}",
            )
        return True
    _write_create_only(path, payload)
    return False


def _atomic_json(path: Path, value: Mapping[str, object]) -> None:
    payload = _json_bytes(value)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        offset = 0
        while offset < len(payload):
            written = os.write(descriptor, payload[offset:])
            if written <= 0:
                raise OSError("short write")
            offset += written
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    except OSError as exc:
        raise _error(
            "FINAL_MEDIA_REVIEW_SENTINEL_WRITE_FAILED",
            f"atomic state write failed: {path}",
        ) from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary.exists() and not temporary.is_symlink():
            temporary.unlink()


def _self_hashed(value: Mapping[str, object], *, field: str) -> dict[str, object]:
    result = dict(value)
    result[field] = _canonical_sha256(result)
    return result


def _validate_self_hash(
    value: Mapping[str, object], *, field: str, label: str
) -> None:
    supplied = _normalize_sha(value.get(field), label=f"{label} self-hash")
    unsigned = dict(value)
    unsigned.pop(field, None)
    if _canonical_sha256(unsigned) != supplied:
        raise _error(
            "FINAL_MEDIA_REVIEW_SENTINEL_STATE_INVALID",
            f"{label} self-hash changed",
        )


def _validate_argv(
    value: object, *, allowed: frozenset[str], label: str
) -> list[str]:
    if not isinstance(value, list) or not 3 <= len(value) <= 64:
        raise _error(
            "FINAL_MEDIA_REVIEW_SENTINEL_RUNTIME_INVALID",
            f"{label} argv is invalid",
        )
    argv: list[str] = []
    counts = {placeholder: 0 for placeholder in allowed}
    for item in value:
        if (
            not isinstance(item, str)
            or not item
            or len(item) > 4096
            or "\x00" in item
            or "\n" in item
            or "\r" in item
        ):
            raise _error(
                "FINAL_MEDIA_REVIEW_SENTINEL_RUNTIME_INVALID",
                f"{label} argv item is invalid",
            )
        placeholders = _PLACEHOLDER_RE.findall(item)
        if placeholders and (
            item not in allowed or len(placeholders) != 1
        ):
            raise _error(
                "FINAL_MEDIA_REVIEW_SENTINEL_RUNTIME_INVALID",
                f"{label} argv placeholders must be standalone approved tokens",
            )
        for placeholder in placeholders:
            counts[placeholder] += 1
        argv.append(item)
    if argv[0] != "{executable}":
        raise _error(
            "FINAL_MEDIA_REVIEW_SENTINEL_RUNTIME_INVALID",
            f"{label} argv[0] must be {{executable}}",
        )
    if any(count != 1 for count in counts.values()):
        raise _error(
            "FINAL_MEDIA_REVIEW_SENTINEL_RUNTIME_INVALID",
            f"{label} argv must bind every required placeholder exactly once",
        )
    return argv


def _bound_manifest_file(
    raw: object,
    *,
    root: Path,
    label: str,
    contained: bool,
    executable: bool,
) -> dict[str, object]:
    if not isinstance(raw, Mapping) or set(raw) != {"path", "sha256"}:
        raise _error(
            "FINAL_MEDIA_REVIEW_SENTINEL_RUNTIME_INVALID",
            f"{label} binding is malformed",
        )
    raw_path = raw.get("path")
    if not isinstance(raw_path, str) or not raw_path:
        raise _error(
            "FINAL_MEDIA_REVIEW_SENTINEL_RUNTIME_INVALID",
            f"{label} path missing",
        )
    if contained:
        relative = _relative_path(raw_path, label=label)
        path = root / relative
    else:
        path = Path(raw_path).expanduser()
        if not path.is_absolute():
            raise _error(
                "FINAL_MEDIA_REVIEW_SENTINEL_RUNTIME_INVALID",
                f"{label} path must be absolute",
            )
    stable = _stable_regular(
        path,
        label=label,
        maximum=_MAX_EXECUTABLE_BYTES,
        expected_uid=None,
        executable=executable,
    )
    expected_sha = _normalize_sha(raw.get("sha256"), label=label)
    if stable["sha256"] != expected_sha:
        raise _error(
            "FINAL_MEDIA_REVIEW_SENTINEL_RUNTIME_INVALID",
            f"{label} hash changed",
        )
    return {
        "path": (
            Path(str(stable["path"])).relative_to(root).as_posix()
            if contained
            else str(stable["path"])
        ),
        "sha256": expected_sha,
        "bytes": stable["bytes"],
        "absolute_path": str(stable["path"]),
    }


def load_sentinel_runtime(runtime_root: str | Path) -> dict[str, object] | None:
    """Load one runtime-owned candidate transport without claiming capability."""

    root = _safe_root(runtime_root)
    path = root / SENTINEL_RUNTIME_FILENAME
    if not path.exists() and not path.is_symlink():
        return None
    stable = _stable_regular(
        path,
        label="sentinel runtime manifest",
        maximum=_MAX_JSON_BYTES,
        expected_uid=root.stat().st_uid,
        private=True,
    )
    value = _read_json(path, label="sentinel runtime manifest")
    expected_fields = {
        "schema_version",
        "capability_id",
        "provider",
        "transport",
        "model",
        "endpoint_family",
        "runner",
        "adapter",
        "sentinel_argv",
        "final_review_argv",
        "timeout_seconds",
        "ffmpeg",
        "ffprobe",
        "backoff",
        "result_schema_version",
    }
    capability_id = value.get("capability_id")
    model = value.get("model")
    endpoint_family = value.get("endpoint_family")
    timeout = value.get("timeout_seconds")
    backoff = value.get("backoff")
    if (
        set(value) != expected_fields
        or value.get("schema_version") != SENTINEL_RUNTIME_SCHEMA_VERSION
        or not isinstance(capability_id, str)
        or _ID_RE.fullmatch(capability_id) is None
        or not isinstance(value.get("provider"), str)
        or _ID_RE.fullmatch(str(value.get("provider"))) is None
        or value.get("transport") != "content_bound_command"
        or not isinstance(model, str)
        or _ID_RE.fullmatch(model) is None
        or not isinstance(endpoint_family, str)
        or _ID_RE.fullmatch(endpoint_family) is None
        or isinstance(timeout, bool)
        or not isinstance(timeout, int)
        or not 1 <= timeout <= 3600
        or value.get("result_schema_version") != RESULT_SCHEMA_VERSION
        or not isinstance(backoff, Mapping)
        or set(backoff) != {
            "base_seconds",
            "max_seconds",
            "max_rate_limit_attempts",
        }
    ):
        raise _error(
            "FINAL_MEDIA_REVIEW_SENTINEL_RUNTIME_INVALID",
            "sentinel runtime manifest values are invalid",
        )
    base = backoff.get("base_seconds")
    maximum = backoff.get("max_seconds")
    attempts = backoff.get("max_rate_limit_attempts")
    if (
        any(isinstance(item, bool) or not isinstance(item, int) for item in (base, maximum, attempts))
        or not 1 <= int(base) <= int(maximum) <= 86_400
        or not 1 <= int(attempts) <= 8
    ):
        raise _error(
            "FINAL_MEDIA_REVIEW_SENTINEL_RUNTIME_INVALID",
            "sentinel backoff policy is invalid",
        )
    if model in KNOWN_UNSUPPORTED_DUAL_RAW_AV_MODELS:
        raise _error(
            "FINAL_MEDIA_REVIEW_MODEL_MODALITY_UNSUPPORTED",
            f"{model} has no raw-audio plus continuous-video input contract",
        )
    runner = _bound_manifest_file(
        value.get("runner"),
        root=root,
        label="sentinel runner",
        contained=True,
        executable=True,
    )
    adapter = _bound_manifest_file(
        value.get("adapter"),
        root=root,
        label="sentinel adapter",
        contained=True,
        executable=True,
    )
    ffmpeg = _bound_manifest_file(
        value.get("ffmpeg"),
        root=root,
        label="sentinel ffmpeg",
        contained=False,
        executable=True,
    )
    ffprobe = _bound_manifest_file(
        value.get("ffprobe"),
        root=root,
        label="sentinel ffprobe",
        contained=False,
        executable=True,
    )
    normalized = {
        **value,
        "runner": runner,
        "adapter": adapter,
        "ffmpeg": ffmpeg,
        "ffprobe": ffprobe,
        "sentinel_argv": _validate_argv(
            value.get("sentinel_argv"),
            allowed=_SENTINEL_PLACEHOLDERS,
            label="sentinel",
        ),
        "final_review_argv": _validate_argv(
            value.get("final_review_argv"),
            allowed=_FINAL_REVIEW_PLACEHOLDERS,
            label="final review",
        ),
        "backoff": {
            "base_seconds": int(base),
            "max_seconds": int(maximum),
            "max_rate_limit_attempts": int(attempts),
        },
        "timeout_seconds": int(timeout),
        "runtime_root": str(root),
        "runtime_manifest_path": str(path.resolve()),
        "runtime_manifest_sha256": str(stable["sha256"]),
    }
    normalized["runtime_contract_sha256"] = _canonical_sha256(
        {
            key: normalized[key]
            for key in (
                "capability_id",
                "provider",
                "transport",
                "model",
                "endpoint_family",
                "runner",
                "adapter",
                "sentinel_argv",
                "final_review_argv",
                "timeout_seconds",
                "ffmpeg",
                "ffprobe",
                "backoff",
                "result_schema_version",
            )
        }
    )
    return normalized


@contextmanager
def _runner_lock(root: Path) -> Iterator[None]:
    path = root / SENTINEL_LOCK_FILENAME
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != root.stat().st_uid:
            raise _error(
                "FINAL_MEDIA_REVIEW_SENTINEL_LOCK_INVALID",
                "sentinel lock ownership/type is invalid",
            )
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise _error(
                "FINAL_MEDIA_REVIEW_SENTINEL_BUSY",
                "another sentinel transaction owns the runtime lock",
            ) from exc
        yield
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def _token(digits: int) -> str:
    return "".join(secrets.choice("0123456789") for _ in range(digits))


def _challenge_prompt(modality: str) -> str:
    if modality == "raw_audio":
        return (
            "The attached audio contains exactly eight standard DTMF keypad "
            "digits separated by silence. Decode them in temporal order and "
            "return only the eight decimal digits, with no spaces or prose.\n"
        )
    return (
        "The attached video contains four consecutive short panels, each lasting "
        "about 0.4 seconds. Each panel shows exactly two decimal digits. "
        "Concatenate the panels in temporal "
        "order and return only the eight digits, with no spaces or prose.\n"
    )


def _challenge_paths(root: Path, modality: str) -> dict[str, Path]:
    challenge = root / modality
    suffix = ".wav" if modality == "raw_audio" else ".mp4"
    return {
        "root": challenge,
        "input": challenge / f"challenge{suffix}",
        "prompt": challenge / "prompt.txt",
        "secret": challenge / "secret-commitment.json",
        "request": challenge / "request.json",
        "state": challenge / "state.json",
        "responses": challenge / "responses",
    }


def _prepare_challenge(
    staging: Path,
    *,
    modality: str,
    token: str,
    runtime: Mapping[str, object],
    created_at: datetime,
) -> dict[str, object]:
    paths = _challenge_paths(staging, modality)
    paths["root"].mkdir(mode=0o700)
    paths["responses"].mkdir(mode=0o700)
    prompt = _challenge_prompt(modality)
    paths["prompt"].write_text(prompt, encoding="utf-8")
    paths["prompt"].chmod(0o600)
    try:
        if modality == "raw_audio":
            media = generate_dtmf_wav(paths["input"], token)
            coverage = "RAW_AUDIO_ONLY_HIDDEN_NONCE"
        else:
            media = generate_temporal_video(
                paths["input"],
                token,
                ffmpeg_path=Path(str(runtime["ffmpeg"]["absolute_path"])),
                ffprobe_path=Path(str(runtime["ffprobe"]["absolute_path"])),
            )
            coverage = "CONTINUOUS_SOURCE_VIDEO_HIDDEN_SEQUENCE"
    except ValueError as exc:
        raise _error(
            "FINAL_MEDIA_REVIEW_SENTINEL_MEDIA_INVALID",
            f"{modality} sentinel media could not be generated: {exc}",
        ) from exc
    media = dict(media)
    media["path"] = paths["input"].relative_to(staging).as_posix()
    if isinstance(media.get("frame_sources"), list):
        media["frame_sources"] = [
            {
                **row,
                "path": Path(str(row["path"])).relative_to(staging).as_posix(),
            }
            for row in media["frame_sources"]
        ]
    expected_sha = hashlib.sha256(token.encode("utf-8")).hexdigest()
    commitment: dict[str, object] = {
        "schema_version": SENTINEL_SECRET_SCHEMA_VERSION,
        "challenge_id": f"{modality}-{runtime['capability_id']}",
        "input_sha256": media["sha256"],
        "prompt_sha256": _sha256(paths["prompt"]),
        "expected_answer_sha256": expected_sha,
        "created_at": _iso(created_at),
    }
    commitment["commitment_sha256"] = _canonical_sha256(commitment)
    _write_create_only(paths["secret"], _json_bytes(commitment))
    request = {
        "schema_version": SENTINEL_REQUEST_SCHEMA_VERSION,
        "challenge_id": commitment["challenge_id"],
        "provider": runtime["provider"],
        "model": runtime["model"],
        "endpoint_family": runtime["endpoint_family"],
        "coverage": coverage,
        "input_sha256": media["sha256"],
        "prompt_sha256": _sha256(paths["prompt"]),
        "answer_disclosed_to_adapter": False,
    }
    _write_create_only(paths["request"], _json_bytes(request))
    state = _self_hashed(
        {
            "schema_version": SENTINEL_STATE_SCHEMA_VERSION,
            "challenge_id": commitment["challenge_id"],
            "modality": modality,
            "status": "PREPARED",
            "attempt_count": 0,
            "request_sha256": _sha256(paths["request"]),
            "current_response": None,
            "next_retry_at": None,
            "reason_code": None,
            "updated_at": _iso(created_at),
        },
        field="state_sha256",
    )
    _write_create_only(paths["state"], _json_bytes(state))
    return {
        "challenge_id": commitment["challenge_id"],
        "coverage": coverage,
        "input": _binding(paths["input"], root=staging),
        "prompt": _binding(paths["prompt"], root=staging),
        "secret": _binding(paths["secret"], root=staging),
        "request": _binding(paths["request"], root=staging),
        "state_path": paths["state"].relative_to(staging).as_posix(),
        "responses_path": paths["responses"].relative_to(staging).as_posix(),
        "media_receipt": media,
        "expected_answer_sha256": expected_sha,
    }


def _prepare_run(
    root: Path,
    *,
    run_id: str,
    runtime: Mapping[str, object],
    now: datetime,
    token_factory: Callable[[str], str] | None,
) -> tuple[Path, dict[str, object], bool]:
    if _ID_RE.fullmatch(run_id) is None:
        raise _error(
            "FINAL_MEDIA_REVIEW_SENTINEL_RUN_INVALID",
            "run_id is invalid",
        )
    parent = root / SENTINEL_RUNS_DIRECTORY
    if parent.exists() or parent.is_symlink():
        if parent.is_symlink() or not parent.is_dir():
            raise _error(
                "FINAL_MEDIA_REVIEW_SENTINEL_PATH_INVALID",
                "sentinel run namespace is unsafe",
            )
    else:
        parent.mkdir(mode=0o700)
        _fsync_directory(parent.parent)
    parent.chmod(0o700)
    run_root = parent / run_id
    plan_path = run_root / "plan.json"
    if run_root.exists() or run_root.is_symlink():
        if run_root.is_symlink() or not run_root.is_dir():
            raise _error(
                "FINAL_MEDIA_REVIEW_SENTINEL_PATH_INVALID",
                "sentinel run root is unsafe",
            )
        plan = _read_json(plan_path, label="sentinel plan")
        _validate_self_hash(plan, field="plan_sha256", label="sentinel plan")
        if (
            plan.get("schema_version") != SENTINEL_PLAN_SCHEMA_VERSION
            or plan.get("run_id") != run_id
            or plan.get("runtime_contract_sha256")
            != runtime["runtime_contract_sha256"]
        ):
            raise _error(
                "FINAL_MEDIA_REVIEW_SENTINEL_CONFLICT",
                "existing sentinel run differs from current runtime contract",
            )
        return run_root.resolve(), plan, True
    temporary = Path(
        tempfile.mkdtemp(prefix=f".{run_id}.prepare.", dir=parent)
    )
    temporary.chmod(0o700)
    try:
        factory = token_factory or (
            lambda modality: _token(
                AUDIO_TOKEN_DIGITS if modality == "raw_audio" else VIDEO_TOKEN_DIGITS
            )
        )
        audio_token = factory("raw_audio")
        video_token = factory("continuous_source_video")
        if audio_token == video_token:
            raise _error(
                "FINAL_MEDIA_REVIEW_SENTINEL_RUN_INVALID",
                "audio and video challenge tokens must differ",
            )
        challenges = {
            "raw_audio": _prepare_challenge(
                temporary,
                modality="raw_audio",
                token=audio_token,
                runtime=runtime,
                created_at=now,
            ),
            "continuous_source_video": _prepare_challenge(
                temporary,
                modality="continuous_source_video",
                token=video_token,
                runtime=runtime,
                created_at=now,
            ),
        }
        plan = _self_hashed(
            {
                "schema_version": SENTINEL_PLAN_SCHEMA_VERSION,
                "run_id": run_id,
                "status": "PREPARED",
                "created_at": _iso(now),
                "provider": runtime["provider"],
                "model": runtime["model"],
                "endpoint_family": runtime["endpoint_family"],
                "capability_id": runtime["capability_id"],
                "runtime_manifest_sha256": runtime[
                    "runtime_manifest_sha256"
                ],
                "runtime_contract_sha256": runtime[
                    "runtime_contract_sha256"
                ],
                "runner": {
                    key: runtime["runner"][key]
                    for key in ("path", "sha256", "bytes")
                },
                "adapter": {
                    key: runtime["adapter"][key]
                    for key in ("path", "sha256", "bytes")
                },
                "ffmpeg_sha256": runtime["ffmpeg"]["sha256"],
                "ffprobe_sha256": runtime["ffprobe"]["sha256"],
                "challenges": challenges,
            },
            field="plan_sha256",
        )
        _write_create_only(temporary / "plan.json", _json_bytes(plan))
        _fsync_directory(temporary)
        try:
            os.rename(temporary, run_root)
        except OSError as exc:
            raise _error(
                "FINAL_MEDIA_REVIEW_SENTINEL_CONFLICT",
                "sentinel run root appeared during prepare",
            ) from exc
        _fsync_directory(parent)
        return run_root.resolve(), plan, False
    except Exception:
        # A partial private prepare tree has never crossed the run-root commit
        # boundary.  Preserve it for diagnosis; it is not considered resumable.
        raise


def _state(path: Path, *, challenge_id: str, modality: str) -> dict[str, object]:
    value = _read_json(path, label=f"{modality} sentinel state")
    _validate_self_hash(value, field="state_sha256", label=f"{modality} state")
    if (
        value.get("schema_version") != SENTINEL_STATE_SCHEMA_VERSION
        or value.get("challenge_id") != challenge_id
        or value.get("modality") != modality
        or value.get("status")
        not in {
            "PREPARED",
            "DISPATCHING",
            "RETRY_WAIT",
            "COMPLETE",
            "FAILED",
            "DISPATCH_AMBIGUOUS",
        }
        or isinstance(value.get("attempt_count"), bool)
        or not isinstance(value.get("attempt_count"), int)
        or value.get("attempt_count") < 0
    ):
        raise _error(
            "FINAL_MEDIA_REVIEW_SENTINEL_STATE_INVALID",
            f"{modality} sentinel state fields are invalid",
        )
    return value


def _write_state(path: Path, value: Mapping[str, object]) -> dict[str, object]:
    unsigned = dict(value)
    unsigned.pop("state_sha256", None)
    sealed = _self_hashed(unsigned, field="state_sha256")
    _atomic_json(path, sealed)
    return sealed


def _expanded_argv(
    runtime: Mapping[str, object],
    *,
    request_path: Path,
    response_path: Path,
    input_path: Path,
    prompt_path: Path,
) -> list[str]:
    replacements = {
        "{executable}": str(runtime["adapter"]["absolute_path"]),
        "{request_json}": str(request_path),
        "{response_json}": str(response_path),
        "{input_media}": str(input_path),
        "{prompt_file}": str(prompt_path),
    }
    return [
        replacements.get(item, item)
        for item in runtime["sentinel_argv"]
    ]


def _adapter_environment(
    root: Path, run_id: str, environment: Mapping[str, str] | None
) -> dict[str, str]:
    selected = dict(os.environ if environment is None else environment)
    for key in list(selected):
        if key.startswith("CPA_"):
            selected.pop(key, None)
    selected["AUTOSLICE_BASE"] = str(root)
    selected["AUTOSLICE_FINAL_REVIEW_RUNTIME_ROOT"] = str(root)
    selected["AUTOSLICE_SENTINEL_RUN_ID"] = run_id
    return selected


def _response_binding(path: Path, *, run_root: Path) -> dict[str, object]:
    stable = _stable_regular(
        path,
        label="sentinel adapter response",
        maximum=_MAX_JSON_BYTES,
        expected_uid=run_root.stat().st_uid,
        private=True,
    )
    return {
        "path": path.resolve().relative_to(run_root).as_posix(),
        "sha256": stable["sha256"],
        "bytes": stable["bytes"],
    }


def _classify_response(
    path: Path,
    *,
    run_root: Path,
    challenge: Mapping[str, object],
) -> tuple[str, dict[str, object]]:
    value = _read_json(path, label="sentinel adapter response")
    request_sha = challenge["request"]["sha256"]
    if value.get("schema_version") == SENTINEL_RESPONSE_SCHEMA_VERSION:
        observed = value.get("observed_answer")
        observed_sha = (
            hashlib.sha256(observed.encode("utf-8")).hexdigest()
            if isinstance(observed, str)
            else None
        )
        if (
            set(value)
            != {
                "schema_version",
                "challenge_id",
                "request_sha256",
                "observed_answer",
                "observed_answer_sha256",
            }
            or value.get("challenge_id") != challenge["challenge_id"]
            or value.get("request_sha256") != request_sha
            or not isinstance(observed, str)
            or not observed
            or value.get("observed_answer_sha256") != observed_sha
        ):
            raise _error(
                "FINAL_MEDIA_REVIEW_SENTINEL_RESPONSE_INVALID",
                "sentinel success response is malformed",
            )
        expected = challenge["expected_answer_sha256"]
        return (
            "PASS" if observed_sha == expected else "ANSWER_MISMATCH",
            {
                "binding": _response_binding(path, run_root=run_root),
                "observed_answer_sha256": observed_sha,
            },
        )
    if value.get("schema_version") == SENTINEL_ADAPTER_ERROR_SCHEMA_VERSION:
        error = value.get("error")
        if (
            set(value) != {"schema_version", "request_sha256", "error"}
            or value.get("request_sha256") != request_sha
            or not isinstance(error, Mapping)
            or set(error) != {
                "safe_reason",
                "provider_http_status",
                "message",
            }
            or not isinstance(error.get("message"), str)
        ):
            raise _error(
                "FINAL_MEDIA_REVIEW_SENTINEL_RESPONSE_INVALID",
                "sentinel error response is malformed",
            )
        status = error.get("provider_http_status")
        return (
            "RATE_LIMIT" if status == 429 else "EXPLICIT_ERROR",
            {
                "binding": _response_binding(path, run_root=run_root),
                "safe_reason": error.get("safe_reason"),
                "provider_http_status": status,
                "message": str(error.get("message"))[:1000],
            },
        )
    raise _error(
        "FINAL_MEDIA_REVIEW_SENTINEL_RESPONSE_INVALID",
        "sentinel adapter response schema is unknown",
    )


def _backoff_seconds(runtime: Mapping[str, object], attempt_count: int) -> int:
    base = int(runtime["backoff"]["base_seconds"])
    maximum = int(runtime["backoff"]["max_seconds"])
    return min(maximum, base * (2 ** max(0, attempt_count - 1)))


def _resume_dispatching(
    state: Mapping[str, object],
    *,
    state_path: Path,
    run_root: Path,
    challenge: Mapping[str, object],
    modality: str,
    runtime: Mapping[str, object],
    now: datetime,
) -> dict[str, object]:
    relative = state.get("current_response")
    if not isinstance(relative, str) or not relative:
        return _write_state(
            state_path,
            {
                **state,
                "status": "DISPATCH_AMBIGUOUS",
                "reason_code": "SENTINEL_DISPATCH_OUTCOME_UNKNOWN",
                "updated_at": _iso(now),
            },
        )
    response_path = run_root / relative
    if not response_path.exists() or response_path.is_symlink():
        return _write_state(
            state_path,
            {
                **state,
                "status": "DISPATCH_AMBIGUOUS",
                "reason_code": "SENTINEL_DISPATCH_OUTCOME_UNKNOWN",
                "updated_at": _iso(now),
            },
        )
    try:
        classification, details = _classify_response(
            response_path, run_root=run_root, challenge=challenge
        )
    except FinalMediaReviewModelCapabilitySentinelError:
        return _write_state(
            state_path,
            {
                **state,
                "status": "DISPATCH_AMBIGUOUS",
                "reason_code": "SENTINEL_RESPONSE_UNRECOVERABLE",
                "updated_at": _iso(now),
            },
        )
    return _transition_from_response(
        state,
        state_path=state_path,
        classification=classification,
        details=details,
        modality=modality,
        runtime=runtime,
        now=now,
    )


def _transition_from_response(
    state: Mapping[str, object],
    *,
    state_path: Path,
    classification: str,
    details: Mapping[str, object],
    modality: str,
    runtime: Mapping[str, object],
    now: datetime,
) -> dict[str, object]:
    attempt_count = int(state["attempt_count"])
    base = {
        **state,
        "response": dict(details.get("binding") or {}),
        "updated_at": _iso(now),
    }
    if classification == "PASS":
        return _write_state(
            state_path,
            {
                **base,
                "status": "COMPLETE",
                "observed_answer_sha256": details["observed_answer_sha256"],
                "next_retry_at": None,
                "reason_code": None,
            },
        )
    if classification == "ANSWER_MISMATCH":
        return _write_state(
            state_path,
            {
                **base,
                "status": "FAILED",
                "observed_answer_sha256": details["observed_answer_sha256"],
                "next_retry_at": None,
                "reason_code": "SENTINEL_ANSWER_MISMATCH",
            },
        )
    if classification == "RATE_LIMIT":
        maximum = int(runtime["backoff"]["max_rate_limit_attempts"])
        if attempt_count >= maximum:
            return _write_state(
                state_path,
                {
                    **base,
                    "status": "FAILED",
                    "next_retry_at": None,
                    "reason_code": "SENTINEL_RATE_LIMIT_EXHAUSTED",
                    "provider_http_status": 429,
                },
            )
        retry_at = now + timedelta(
            seconds=_backoff_seconds(runtime, attempt_count)
        )
        return _write_state(
            state_path,
            {
                **base,
                "status": "RETRY_WAIT",
                "next_retry_at": _iso(retry_at),
                "reason_code": "SENTINEL_PROVIDER_RATE_LIMITED",
                "provider_http_status": 429,
            },
        )
    return _write_state(
        state_path,
        {
            **base,
            "status": "FAILED",
            "next_retry_at": None,
            "reason_code": "SENTINEL_PROVIDER_EXPLICIT_ERROR",
            "provider_http_status": details.get("provider_http_status"),
        },
    )


def _process_challenge(
    *,
    run_root: Path,
    run_id: str,
    modality: str,
    challenge: Mapping[str, object],
    runtime: Mapping[str, object],
    now: datetime,
    environment: Mapping[str, str] | None,
    run: Callable[..., subprocess.CompletedProcess[str]],
    failpoint: str | None,
) -> tuple[dict[str, object], bool]:
    state_path = run_root / str(challenge["state_path"])
    state = _state(
        state_path,
        challenge_id=str(challenge["challenge_id"]),
        modality=modality,
    )
    status = state["status"]
    if status == "DISPATCHING":
        return (
            _resume_dispatching(
                state,
                state_path=state_path,
                run_root=run_root,
                challenge=challenge,
                modality=modality,
                runtime=runtime,
                now=now,
            ),
            False,
        )
    if status in {"COMPLETE", "FAILED", "DISPATCH_AMBIGUOUS"}:
        return state, False
    if status == "RETRY_WAIT":
        due = _parse_time(state.get("next_retry_at"), label="next_retry_at")
        if now < due:
            return state, False
    try:
        wait = provider_wait_for_call(float(runtime["timeout_seconds"]))
        slot = runtime_provider_slot(
            runtime_root=Path(str(runtime["runtime_root"])),
            timeout_seconds=wait,
        )
        slot.__enter__()
    except (ProviderSlotError, ProviderSlotTimeout):
        return (
            _write_state(
                state_path,
                {
                    **state,
                    "status": "PREPARED",
                    "next_retry_at": None,
                    "reason_code": "SENTINEL_PROVIDER_CAPACITY_UNAVAILABLE",
                    "updated_at": _iso(now),
                },
            ),
            False,
        )
    try:
        attempt = int(state["attempt_count"]) + 1
        responses = run_root / str(challenge["responses_path"])
        response_path = responses / f"attempt-{attempt:02d}.json"
        if response_path.exists() or response_path.is_symlink():
            raise _error(
                "FINAL_MEDIA_REVIEW_SENTINEL_CONFLICT",
                "next sentinel response path already exists",
            )
        dispatching = _write_state(
            state_path,
            {
                **state,
                "status": "DISPATCHING",
                "attempt_count": attempt,
                "current_response": response_path.relative_to(run_root).as_posix(),
                "next_retry_at": None,
                "reason_code": None,
                "updated_at": _iso(now),
            },
        )
        if failpoint == f"after_dispatching:{modality}":
            raise RuntimeError(f"TEST_FAILPOINT_AFTER_DISPATCHING_{modality}")
        request_path = run_root / str(challenge["request"]["path"])
        input_path = run_root / str(challenge["input"]["path"])
        prompt_path = run_root / str(challenge["prompt"]["path"])
        argv = _expanded_argv(
            runtime,
            request_path=request_path,
            response_path=response_path,
            input_path=input_path,
            prompt_path=prompt_path,
        )
        try:
            completed = run(
                argv,
                check=False,
                capture_output=True,
                text=True,
                stdin=subprocess.DEVNULL,
                timeout=int(runtime["timeout_seconds"]),
                env=_adapter_environment(
                    Path(str(runtime["runtime_root"])), run_id, environment
                ),
            )
            if failpoint == f"after_adapter_response:{modality}":
                raise RuntimeError(
                    f"TEST_FAILPOINT_AFTER_ADAPTER_RESPONSE_{modality}"
                )
        except (OSError, subprocess.TimeoutExpired):
            return (
                _write_state(
                    state_path,
                    {
                        **dispatching,
                        "status": "DISPATCH_AMBIGUOUS",
                        "reason_code": "SENTINEL_DISPATCH_OUTCOME_UNKNOWN",
                        "updated_at": _iso(now),
                    },
                ),
                True,
            )
        if not response_path.exists() or response_path.is_symlink():
            return (
                _write_state(
                    state_path,
                    {
                        **dispatching,
                        "status": "DISPATCH_AMBIGUOUS",
                        "reason_code": "SENTINEL_RESPONSE_MISSING",
                        "adapter_returncode": completed.returncode,
                        "updated_at": _iso(now),
                    },
                ),
                True,
            )
        try:
            classification, details = _classify_response(
                response_path, run_root=run_root, challenge=challenge
            )
        except FinalMediaReviewModelCapabilitySentinelError:
            return (
                _write_state(
                    state_path,
                    {
                        **dispatching,
                        "status": "DISPATCH_AMBIGUOUS",
                        "reason_code": "SENTINEL_RESPONSE_INVALID",
                        "adapter_returncode": completed.returncode,
                        "updated_at": _iso(now),
                    },
                ),
                True,
            )
        if completed.returncode == 0 and classification not in {
            "PASS",
            "ANSWER_MISMATCH",
        }:
            classification = "EXPLICIT_ERROR"
        elif completed.returncode != 0 and classification in {
            "PASS",
            "ANSWER_MISMATCH",
        }:
            return (
                _write_state(
                    state_path,
                    {
                        **dispatching,
                        "status": "DISPATCH_AMBIGUOUS",
                        "reason_code": "SENTINEL_RESPONSE_RETURN_CODE_CONFLICT",
                        "adapter_returncode": completed.returncode,
                        "updated_at": _iso(now),
                    },
                ),
                True,
            )
        return (
            _transition_from_response(
                dispatching,
                state_path=state_path,
                classification=classification,
                details=details,
                modality=modality,
                runtime=runtime,
                now=now,
            ),
            True,
        )
    finally:
        slot.__exit__(None, None, None)


def _result_challenge(
    run_root: Path,
    runtime_root: Path,
    *,
    challenge: Mapping[str, object],
    state: Mapping[str, object],
) -> dict[str, object]:
    if state.get("status") != "COMPLETE":
        raise _error(
            "FINAL_MEDIA_REVIEW_SENTINEL_STATE_INVALID",
            "cannot build result from a non-complete challenge",
        )
    response = state.get("response")
    if not isinstance(response, Mapping):
        raise _error(
            "FINAL_MEDIA_REVIEW_SENTINEL_STATE_INVALID",
            "complete challenge has no response binding",
        )
    for key in ("input", "prompt", "request", "secret"):
        path = run_root / str(challenge[key]["path"])
        if _sha256(path) != challenge[key]["sha256"]:
            raise _error(
                "FINAL_MEDIA_REVIEW_SENTINEL_BINDING_INVALID",
                f"challenge {key} drifted before result",
            )
    response_path = run_root / str(response["path"])
    if _sha256(response_path) != response.get("sha256"):
        raise _error(
            "FINAL_MEDIA_REVIEW_SENTINEL_BINDING_INVALID",
            "challenge response drifted before result",
        )
    def runtime_binding(raw: Mapping[str, object]) -> dict[str, object]:
        path = run_root / str(raw["path"])
        return _binding(path, root=runtime_root)

    return {
        "challenge_id": challenge["challenge_id"],
        "coverage": challenge["coverage"],
        "input": runtime_binding(challenge["input"]),
        "prompt": runtime_binding(challenge["prompt"]),
        "request": runtime_binding(challenge["request"]),
        "response": runtime_binding(response),
        "secret": runtime_binding(challenge["secret"]),
        "expected_answer_sha256": challenge["expected_answer_sha256"],
        "observed_answer_sha256": state["observed_answer_sha256"],
        "matched": True,
        "answer_disclosed_to_adapter": False,
    }


def _build_result(
    run_root: Path,
    runtime_root: Path,
    *,
    plan: Mapping[str, object],
    states: Mapping[str, Mapping[str, object]],
    now: datetime,
) -> tuple[Path, dict[str, object], bool]:
    challenges = plan["challenges"]
    path = run_root / "result.json"
    if path.exists() or path.is_symlink():
        if path.is_symlink() or not path.is_file():
            raise _error(
                "FINAL_MEDIA_REVIEW_SENTINEL_CONFLICT",
                "existing sentinel result is unsafe",
            )
        existing = _read_json(path, label="sentinel result")
        _validate_self_hash(
            existing, field="result_sha256", label="sentinel result"
        )
        if not (
            existing.get("schema_version") == SENTINEL_RESULT_SCHEMA_VERSION
            and existing.get("status") == "COMPLETE"
            and existing.get("run_id") == plan["run_id"]
            and existing.get("provider") == plan["provider"]
            and existing.get("model") == plan["model"]
            and existing.get("endpoint_family") == plan["endpoint_family"]
            and existing.get("target_executable_sha256")
            == plan["adapter"]["sha256"]
            and existing.get("runner") == plan["runner"]
        ):
            raise _error(
                "FINAL_MEDIA_REVIEW_SENTINEL_CONFLICT",
                "existing sentinel result differs from the active plan",
            )
        return path.resolve(), existing, True
    result: dict[str, object] = {
        "schema_version": SENTINEL_RESULT_SCHEMA_VERSION,
        "status": "COMPLETE",
        "authority": MODEL_CAPABILITY_SENTINEL_AUTHORITY,
        "run_id": plan["run_id"],
        "provider": plan["provider"],
        "model": plan["model"],
        "endpoint_family": plan["endpoint_family"],
        "target_executable_sha256": plan["adapter"]["sha256"],
        "runner": plan["runner"],
        "started_at": plan["created_at"],
        "completed_at": _iso(now),
        "challenges": {
            modality: _result_challenge(
                run_root,
                runtime_root,
                challenge=challenges[modality],
                state=states[modality],
            )
            for modality in ("raw_audio", "continuous_source_video")
        },
    }
    result["result_sha256"] = _canonical_sha256(result)
    _write_create_only(path, _json_bytes(result))
    return path.resolve(), result, False


def _runtime_capability_identity(
    value: Mapping[str, object],
) -> dict[str, object]:
    return {
        key: value.get(key)
        for key in (
            "schema_version",
            "capability_id",
            "provider",
            "transport",
            "model",
            "endpoint_family",
            "accepts",
            "executable",
            "argv",
            "timeout_seconds",
            "result_schema_version",
        )
    }


def _install_runtime_capability(
    path: Path,
    *,
    root: Path,
    capability: Mapping[str, object],
) -> tuple[bool, str | None]:
    payload = _json_bytes(capability)
    if not path.exists() and not path.is_symlink():
        _write_create_only(path, payload)
        return False, None
    if path.is_symlink() or not path.is_file() or path.stat().st_nlink != 1:
        raise _error(
            "FINAL_MEDIA_REVIEW_SENTINEL_CONFLICT",
            "existing raw-AV capability path is unsafe",
        )
    previous_payload = path.read_bytes()
    if previous_payload == payload:
        return True, None
    try:
        previous = json.loads(previous_payload.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise _error(
            "FINAL_MEDIA_REVIEW_SENTINEL_CONFLICT",
            "existing raw-AV capability is not valid JSON",
        ) from exc
    if (
        not isinstance(previous, Mapping)
        or _runtime_capability_identity(previous)
        != _runtime_capability_identity(capability)
    ):
        raise _error(
            "FINAL_MEDIA_REVIEW_SENTINEL_CONFLICT",
            "raw-AV capability identity changed during seal rotation",
        )
    previous_sha = hashlib.sha256(previous_payload).hexdigest()
    history = root / "final-media-review-raw-av-capability-history"
    if history.exists() or history.is_symlink():
        if history.is_symlink() or not history.is_dir():
            raise _error(
                "FINAL_MEDIA_REVIEW_SENTINEL_PATH_INVALID",
                "raw-AV capability history namespace is unsafe",
            )
    else:
        history.mkdir(mode=0o700)
        _fsync_directory(history.parent)
    history.chmod(0o700)
    archive = history / f"{previous_sha}.json"
    if archive.exists() or archive.is_symlink():
        if (
            archive.is_symlink()
            or not archive.is_file()
            or archive.stat().st_nlink != 1
            or archive.read_bytes() != previous_payload
        ):
            raise _error(
                "FINAL_MEDIA_REVIEW_SENTINEL_CONFLICT",
                "raw-AV capability history entry differs from its hash",
            )
    else:
        _write_create_only(archive, previous_payload)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".rotate", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        offset = 0
        while offset < len(payload):
            written = os.write(descriptor, payload[offset:])
            if written <= 0:
                raise OSError("short write")
            offset += written
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        if path.read_bytes() != previous_payload:
            raise _error(
                "FINAL_MEDIA_REVIEW_SENTINEL_CONFLICT",
                "raw-AV capability changed before atomic rotation",
            )
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    except FinalMediaReviewModelCapabilitySentinelError:
        raise
    except OSError as exc:
        raise _error(
            "FINAL_MEDIA_REVIEW_SENTINEL_WRITE_FAILED",
            "raw-AV capability rotation failed",
        ) from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary.exists() and not temporary.is_symlink():
            temporary.unlink()
    return False, str(archive.resolve())


def _project_runtime_capability(
    root: Path,
    *,
    runtime: Mapping[str, object],
    seal_binding: Mapping[str, object],
) -> tuple[Path, dict[str, object], bool, str | None]:
    capability = {
        "schema_version": RUNTIME_CAPABILITY_SCHEMA_VERSION,
        "capability_id": runtime["capability_id"],
        "provider": runtime["provider"],
        "transport": runtime["transport"],
        "model": runtime["model"],
        "endpoint_family": runtime["endpoint_family"],
        "accepts": {
            "raw_audio": True,
            "continuous_source_video": True,
        },
        "executable": {
            "path": runtime["adapter"]["path"],
            "sha256": runtime["adapter"]["sha256"],
        },
        "argv": list(runtime["final_review_argv"]),
        "timeout_seconds": runtime["timeout_seconds"],
        "result_schema_version": runtime["result_schema_version"],
        "model_capability_seal": dict(seal_binding),
    }
    path = root / RUNTIME_CAPABILITY_FILENAME
    reused, archived = _install_runtime_capability(
        path, root=root, capability=capability
    )
    return path, capability, reused, archived


def run_model_capability_sentinel(
    *,
    runtime_root: str | Path,
    run_id: str,
    valid_for_seconds: int,
    runner_path: str | Path,
    now: datetime | None = None,
    environment: Mapping[str, str] | None = None,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    token_factory: Callable[[str], str] | None = None,
    failpoint: str | None = None,
) -> dict[str, object]:
    """Run/resume one sentinel and bootstrap the ordinary raw-AV capability."""

    if (
        isinstance(valid_for_seconds, bool)
        or not isinstance(valid_for_seconds, int)
        or not _MIN_VALIDITY_SECONDS
        <= valid_for_seconds
        <= _MAX_VALIDITY_SECONDS
    ):
        raise _error(
            "FINAL_MEDIA_REVIEW_SENTINEL_RUN_INVALID",
            "valid_for_seconds is outside the accepted range",
        )
    root = _safe_root(runtime_root)
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    with _runner_lock(root):
        runtime = load_sentinel_runtime(root)
        if runtime is None:
            return {
                "status": "SENTINEL_RUNTIME_ABSENT",
                "provider_calls": 0,
                "run_id": run_id,
            }
        invoked = _stable_regular(
            Path(runner_path).expanduser(),
            label="invoked sentinel runner",
            maximum=_MAX_EXECUTABLE_BYTES,
            expected_uid=root.stat().st_uid,
            executable=True,
        )
        if (
            invoked["path"] != runtime["runner"]["absolute_path"]
            or invoked["sha256"] != runtime["runner"]["sha256"]
        ):
            raise _error(
                "FINAL_MEDIA_REVIEW_SENTINEL_RUNNER_MISMATCH",
                "invoked sentinel runner differs from the runtime authority",
            )
        run_root, plan, plan_reused = _prepare_run(
            root,
            run_id=run_id,
            runtime=runtime,
            now=current,
            token_factory=token_factory,
        )
        states: dict[str, dict[str, object]] = {}
        provider_calls = 0
        for modality in ("raw_audio", "continuous_source_video"):
            state, called = _process_challenge(
                run_root=run_root,
                run_id=run_id,
                modality=modality,
                challenge=plan["challenges"][modality],
                runtime=runtime,
                now=current,
                environment=environment,
                run=run,
                failpoint=failpoint,
            )
            states[modality] = state
            provider_calls += int(called)
            if state["status"] != "COMPLETE":
                return {
                    "status": state["status"],
                    "reason_code": state.get("reason_code"),
                    "run_id": run_id,
                    "run_root": str(run_root),
                    "plan_reused": plan_reused,
                    "provider_calls": provider_calls,
                    "challenge_states": states,
                    "capability_created": False,
                }
        result_path, result, result_reused = _build_result(
            run_root,
            root,
            plan=plan,
            states=states,
            now=current,
        )
        seal_root = root / SENTINEL_SEALS_DIRECTORY / run_id
        seal_root.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        seal_root.parent.chmod(0o700)
        try:
            sealed = seal_model_capability_attestation(
                runtime_root=root,
                sentinel_result_path=result_path,
                target_executable_path=Path(
                    str(runtime["adapter"]["absolute_path"])
                ),
                output_directory=seal_root,
                valid_for_seconds=valid_for_seconds,
                now=current,
            )
        except ValueError as exc:
            reason = getattr(
                exc, "reason_code", "FINAL_MEDIA_REVIEW_SENTINEL_SEAL_FAILED"
            )
            detail = getattr(exc, "detail", str(exc))
            raise _error(str(reason), str(detail)) from exc
        (
            capability_path,
            capability,
            capability_reused,
            archived_capability_path,
        ) = _project_runtime_capability(
            root,
            runtime=runtime,
            seal_binding=sealed["seal_receipt"],
        )
        return {
            "status": "CAPABILITY_BOOTSTRAPPED",
            "run_id": run_id,
            "run_root": str(run_root),
            "plan_reused": plan_reused,
            "result_reused": result_reused,
            "seal_reused": sealed["cache_reused"],
            "capability_reused": capability_reused,
            "archived_capability_path": archived_capability_path,
            "provider_calls": provider_calls,
            "result": {
                "path": str(result_path),
                "sha256": _sha256(result_path),
                "bytes": result_path.stat().st_size,
                "self_sha256": result["result_sha256"],
            },
            "seal": sealed,
            "capability": {
                "path": str(capability_path),
                "sha256": _sha256(capability_path),
                "bytes": capability_path.stat().st_size,
                "model": capability["model"],
                "endpoint_family": capability["endpoint_family"],
            },
            "challenge_states": states,
        }


__all__ = [
    "FinalMediaReviewModelCapabilitySentinelError",
    "SENTINEL_ADAPTER_ERROR_SCHEMA_VERSION",
    "SENTINEL_RUNTIME_FILENAME",
    "SENTINEL_RUNTIME_SCHEMA_VERSION",
    "load_sentinel_runtime",
    "run_model_capability_sentinel",
]
