"""Create-only sealing for dual-media model capability evidence.

The ordinary raw-AV loader must not trust an operator-authored capability JSON.
This module consumes one completed, self-hashed sentinel run; replays the exact
audio/video challenge files; deterministically derives a short-lived
attestation; and commits a separate seal receipt last.  The receipt is the only
runtime binding accepted by the model-capability validator.

The sealer never calls a provider.  Sentinel execution remains a separate
runtime responsibility and its answers must have been withheld from the
adapter.  A crash after the attestation write is recoverable because a rerun
accepts only the exact deterministic attestation before creating the receipt.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat
from typing import Mapping


SENTINEL_RESULT_SCHEMA_VERSION = (
    "final-media-review-model-capability-sentinel-result.v1"
)
SENTINEL_SECRET_SCHEMA_VERSION = (
    "final-media-review-model-capability-secret-commitment.v2"
)
SENTINEL_REQUEST_SCHEMA_VERSION = (
    "final-media-review-model-capability-sentinel-request.v1"
)
SENTINEL_RESPONSE_SCHEMA_VERSION = (
    "final-media-review-model-capability-sentinel-response.v1"
)
MODEL_CAPABILITY_ATTESTATION_SCHEMA_VERSION = (
    "final-media-review-model-capability-attestation.v2"
)
MODEL_CAPABILITY_SEAL_RECEIPT_SCHEMA_VERSION = (
    "final-media-review-model-capability-seal-receipt.v1"
)
MODEL_CAPABILITY_AUTHORITY = "RUNTIME_DUAL_MEDIA_SENTINEL"
MODEL_CAPABILITY_SENTINEL_AUTHORITY = (
    "INDEPENDENT_DUAL_MEDIA_SENTINEL_RUNNER"
)
MODEL_CAPABILITY_SEAL_AUTHORITY = "RUNTIME_DUAL_MEDIA_SENTINEL_SEALER"
MODEL_CAPABILITY_VERIFICATION_METHOD = "sealed_dual_media_sentinel"
ATTESTATION_FILENAME = "model-capability-attestation.json"
SEAL_RECEIPT_FILENAME = "model-capability-seal-receipt.json"

_MAX_JSON_BYTES = 1_000_000
_MAX_CHALLENGE_BYTES = 64 * 1024 * 1024
_MAX_VALIDITY_SECONDS = 7 * 24 * 60 * 60
_MIN_VALIDITY_SECONDS = 60
_MAX_SENTINEL_DURATION = timedelta(hours=2)
_MAX_FUTURE_SKEW = timedelta(minutes=5)
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_CHALLENGE_COVERAGE = {
    "raw_audio": "RAW_AUDIO_ONLY_HIDDEN_NONCE",
    "continuous_source_video": "CONTINUOUS_SOURCE_VIDEO_HIDDEN_SEQUENCE",
}


class FinalMediaReviewModelCapabilitySealError(ValueError):
    """A typed fail-closed sentinel-result or sealing error."""

    def __init__(self, reason_code: str, detail: str):
        super().__init__(f"{reason_code}: {detail}")
        self.reason_code = reason_code
        self.detail = detail


def _error(
    reason_code: str, detail: str
) -> FinalMediaReviewModelCapabilitySealError:
    return FinalMediaReviewModelCapabilitySealError(reason_code, detail)


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
            "FINAL_MEDIA_REVIEW_MODEL_CAPABILITY_SEAL_INVALID",
            "value is not canonical JSON",
        ) from exc


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


def _canonical_sha256(value: object) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _normalize_sha(value: object, *, label: str) -> str:
    if not isinstance(value, str):
        raise _error(
            "FINAL_MEDIA_REVIEW_MODEL_CAPABILITY_SEAL_INVALID",
            f"{label} sha256 missing",
        )
    normalized = value.strip().lower()
    if normalized.startswith("sha256:"):
        normalized = normalized[7:]
    if _SHA256_RE.fullmatch(normalized) is None:
        raise _error(
            "FINAL_MEDIA_REVIEW_MODEL_CAPABILITY_SEAL_INVALID",
            f"{label} sha256 invalid",
        )
    return normalized


def _parse_time(value: object, *, label: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise _error(
            "FINAL_MEDIA_REVIEW_MODEL_CAPABILITY_SEAL_INVALID",
            f"{label} timestamp missing",
        )
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise _error(
            "FINAL_MEDIA_REVIEW_MODEL_CAPABILITY_SEAL_INVALID",
            f"{label} timestamp invalid",
        ) from exc
    if parsed.tzinfo is None:
        raise _error(
            "FINAL_MEDIA_REVIEW_MODEL_CAPABILITY_SEAL_INVALID",
            f"{label} timestamp must include a timezone",
        )
    return parsed.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _safe_runtime_root(value: str | Path) -> Path:
    raw = Path(value).expanduser()
    if not raw.is_absolute() or raw.is_symlink():
        raise _error(
            "FINAL_MEDIA_REVIEW_MODEL_CAPABILITY_SEAL_PATH_INVALID",
            "runtime root must be an absolute non-symlink directory",
        )
    try:
        resolved = raw.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise _error(
            "FINAL_MEDIA_REVIEW_MODEL_CAPABILITY_SEAL_PATH_INVALID",
            "runtime root is unavailable",
        ) from exc
    if not resolved.is_dir():
        raise _error(
            "FINAL_MEDIA_REVIEW_MODEL_CAPABILITY_SEAL_PATH_INVALID",
            "runtime root is not a directory",
        )
    return resolved


def _relative_path(value: object, *, label: str) -> Path:
    if not isinstance(value, str) or not value:
        raise _error(
            "FINAL_MEDIA_REVIEW_MODEL_CAPABILITY_SEAL_PATH_INVALID",
            f"{label} path missing",
        )
    relative = Path(value)
    if relative.is_absolute() or ".." in relative.parts or relative == Path("."):
        raise _error(
            "FINAL_MEDIA_REVIEW_MODEL_CAPABILITY_SEAL_PATH_INVALID",
            f"{label} path must be a contained relative path",
        )
    return relative


def _stable_file(
    path: Path,
    *,
    root: Path,
    label: str,
    maximum: int,
    authority_uid: int | None,
    executable: bool = False,
    private: bool = False,
) -> tuple[bytes, dict[str, object]]:
    try:
        info = path.lstat()
        resolved = path.resolve(strict=True)
        resolved.relative_to(root)
    except (OSError, RuntimeError, ValueError) as exc:
        raise _error(
            "FINAL_MEDIA_REVIEW_MODEL_CAPABILITY_SEAL_PATH_INVALID",
            f"{label} is unavailable or escapes the runtime root",
        ) from exc
    mode = stat.S_IMODE(info.st_mode)
    if (
        path.is_symlink()
        or not stat.S_ISREG(info.st_mode)
        or not resolved.is_file()
        or info.st_nlink != 1
        or not 0 < info.st_size <= maximum
        or mode & 0o022
        or (authority_uid is not None and info.st_uid != authority_uid)
        or (executable and not mode & 0o111)
        or (private and mode & 0o077)
    ):
        raise _error(
            "FINAL_MEDIA_REVIEW_MODEL_CAPABILITY_SEAL_PATH_INVALID",
            f"{label} is not a stable file with the required ownership/mode",
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
        payload = bytearray()
        while len(payload) <= maximum:
            chunk = os.read(descriptor, min(1024 * 1024, maximum + 1 - len(payload)))
            if not chunk:
                break
            payload.extend(chunk)
        after_descriptor = os.fstat(descriptor)
    except OSError as exc:
        raise _error(
            "FINAL_MEDIA_REVIEW_MODEL_CAPABILITY_SEAL_PATH_INVALID",
            f"{label} could not be read through a stable descriptor",
        ) from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    try:
        after_path = os.lstat(resolved)
    except OSError as exc:
        raise _error(
            "FINAL_MEDIA_REVIEW_MODEL_CAPABILITY_SEAL_PATH_INVALID",
            f"{label} disappeared after reading",
        ) from exc
    observed = bytes(payload)
    opened_identity = (
        opened.st_dev,
        opened.st_ino,
        stat.S_IMODE(opened.st_mode),
        opened.st_size,
        opened.st_mtime_ns,
        opened.st_ctime_ns,
    )
    after_descriptor_identity = (
        after_descriptor.st_dev,
        after_descriptor.st_ino,
        stat.S_IMODE(after_descriptor.st_mode),
        after_descriptor.st_size,
        after_descriptor.st_mtime_ns,
        after_descriptor.st_ctime_ns,
    )
    after_path_identity = (
        after_path.st_dev,
        after_path.st_ino,
        stat.S_IMODE(after_path.st_mode),
        after_path.st_size,
        after_path.st_mtime_ns,
        after_path.st_ctime_ns,
    )
    if (
        opened_identity != identity
        or after_descriptor_identity != identity
        or after_path_identity != identity
        or len(observed) != info.st_size
        or len(observed) > maximum
    ):
        raise _error(
            "FINAL_MEDIA_REVIEW_MODEL_CAPABILITY_SEAL_PATH_INVALID",
            f"{label} identity changed while reading",
        )
    return observed, {
        "path": resolved.relative_to(root).as_posix(),
        "sha256": _sha256_bytes(observed),
        "bytes": len(observed),
    }


def _bound_file(
    raw: object,
    *,
    root: Path,
    label: str,
    maximum: int,
    authority_uid: int | None,
    executable: bool = False,
    private: bool = False,
) -> tuple[bytes, dict[str, object]]:
    if not isinstance(raw, Mapping) or set(raw) != {"path", "sha256", "bytes"}:
        raise _error(
            "FINAL_MEDIA_REVIEW_MODEL_CAPABILITY_SEAL_BINDING_INVALID",
            f"{label} binding fields differ from the canonical schema",
        )
    relative = _relative_path(raw.get("path"), label=label)
    payload, binding = _stable_file(
        root / relative,
        root=root,
        label=label,
        maximum=maximum,
        authority_uid=authority_uid,
        executable=executable,
        private=private,
    )
    expected_bytes = raw.get("bytes")
    if (
        isinstance(expected_bytes, bool)
        or not isinstance(expected_bytes, int)
        or expected_bytes != binding["bytes"]
        or _normalize_sha(raw.get("sha256"), label=label) != binding["sha256"]
    ):
        raise _error(
            "FINAL_MEDIA_REVIEW_MODEL_CAPABILITY_SEAL_BINDING_INVALID",
            f"{label} binding drifted",
        )
    return payload, binding


def _read_json(payload: bytes, *, label: str) -> dict[str, object]:
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise _error(
            "FINAL_MEDIA_REVIEW_MODEL_CAPABILITY_SEAL_INVALID",
            f"{label} is not JSON",
        ) from exc
    if not isinstance(value, dict):
        raise _error(
            "FINAL_MEDIA_REVIEW_MODEL_CAPABILITY_SEAL_INVALID",
            f"{label} root must be an object",
        )
    return value


def _validate_challenge(
    raw: object,
    *,
    modality: str,
    root: Path,
    authority_uid: int | None,
    provider: str,
    model: str,
    endpoint_family: str,
) -> dict[str, object]:
    expected_fields = {
        "challenge_id",
        "coverage",
        "input",
        "prompt",
        "request",
        "response",
        "secret",
        "expected_answer_sha256",
        "observed_answer_sha256",
        "matched",
        "answer_disclosed_to_adapter",
    }
    if not isinstance(raw, Mapping) or set(raw) != expected_fields:
        raise _error(
            "FINAL_MEDIA_REVIEW_MODEL_CAPABILITY_SENTINEL_INVALID",
            f"{modality} challenge fields differ from the canonical schema",
        )
    challenge_id = raw.get("challenge_id")
    if (
        not isinstance(challenge_id, str)
        or _ID_RE.fullmatch(challenge_id) is None
        or raw.get("coverage") != _CHALLENGE_COVERAGE[modality]
        or raw.get("matched") is not True
        or raw.get("answer_disclosed_to_adapter") is not False
    ):
        raise _error(
            "FINAL_MEDIA_REVIEW_MODEL_CAPABILITY_SENTINEL_INVALID",
            f"{modality} challenge did not prove withheld-answer perception",
        )
    _input_bytes, input_binding = _bound_file(
        raw.get("input"),
        root=root,
        label=f"{modality} input",
        maximum=_MAX_CHALLENGE_BYTES,
        authority_uid=authority_uid,
    )
    prompt_bytes, prompt_binding = _bound_file(
        raw.get("prompt"),
        root=root,
        label=f"{modality} prompt",
        maximum=_MAX_JSON_BYTES,
        authority_uid=authority_uid,
    )
    request_bytes, request_binding = _bound_file(
        raw.get("request"),
        root=root,
        label=f"{modality} request",
        maximum=_MAX_JSON_BYTES,
        authority_uid=authority_uid,
    )
    response_bytes, response_binding = _bound_file(
        raw.get("response"),
        root=root,
        label=f"{modality} response",
        maximum=_MAX_JSON_BYTES,
        authority_uid=authority_uid,
    )
    secret_bytes, secret_binding = _bound_file(
        raw.get("secret"),
        root=root,
        label=f"{modality} secret commitment",
        maximum=_MAX_JSON_BYTES,
        authority_uid=authority_uid,
        private=True,
    )
    secret = _read_json(secret_bytes, label=f"{modality} secret commitment")
    request = _read_json(request_bytes, label=f"{modality} request")
    response = _read_json(response_bytes, label=f"{modality} response")
    secret_fields = {
        "schema_version",
        "challenge_id",
        "input_sha256",
        "prompt_sha256",
        "expected_answer_sha256",
        "created_at",
        "commitment_sha256",
    }
    request_fields = {
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
    response_fields = {
        "schema_version",
        "challenge_id",
        "request_sha256",
        "observed_answer",
        "observed_answer_sha256",
    }
    if set(secret) != secret_fields:
        raise _error(
            "FINAL_MEDIA_REVIEW_MODEL_CAPABILITY_SENTINEL_INVALID",
            f"{modality} secret commitment fields are invalid",
        )
    unsigned_secret = dict(secret)
    supplied_commitment = _normalize_sha(
        unsigned_secret.pop("commitment_sha256", None),
        label=f"{modality} secret commitment",
    )
    expected_sha = _normalize_sha(
        secret.get("expected_answer_sha256"),
        label=f"{modality} secret expected answer",
    )
    _parse_time(secret.get("created_at"), label=f"{modality} commitment created_at")
    observed = response.get("observed_answer") if isinstance(response, Mapping) else None
    observed_sha = (
        _sha256_bytes(observed.encode("utf-8"))
        if isinstance(observed, str)
        else ""
    )
    if (
        secret.get("schema_version") != SENTINEL_SECRET_SCHEMA_VERSION
        or secret.get("challenge_id") != challenge_id
        or _normalize_sha(
            secret.get("input_sha256"), label=f"{modality} secret input"
        )
        != input_binding["sha256"]
        or _normalize_sha(
            secret.get("prompt_sha256"), label=f"{modality} secret prompt"
        )
        != prompt_binding["sha256"]
        or _canonical_sha256(unsigned_secret) != supplied_commitment
        or set(request) != request_fields
        or request.get("schema_version") != SENTINEL_REQUEST_SCHEMA_VERSION
        or request.get("challenge_id") != challenge_id
        or request.get("provider") != provider
        or request.get("model") != model
        or request.get("endpoint_family") != endpoint_family
        or request.get("coverage") != _CHALLENGE_COVERAGE[modality]
        or request.get("input_sha256") != input_binding["sha256"]
        or request.get("prompt_sha256") != prompt_binding["sha256"]
        or request.get("answer_disclosed_to_adapter") is not False
        or set(response) != response_fields
        or response.get("schema_version") != SENTINEL_RESPONSE_SCHEMA_VERSION
        or response.get("challenge_id") != challenge_id
        or response.get("request_sha256") != request_binding["sha256"]
        or not isinstance(observed, str)
        or not observed
        or response.get("observed_answer_sha256") != observed_sha
    ):
        raise _error(
            "FINAL_MEDIA_REVIEW_MODEL_CAPABILITY_SENTINEL_INVALID",
            f"{modality} challenge files are inconsistent",
        )
    if (
        expected_sha != observed_sha
        or _normalize_sha(
            raw.get("expected_answer_sha256"), label=f"{modality} expected answer"
        )
        != expected_sha
        or _normalize_sha(
            raw.get("observed_answer_sha256"), label=f"{modality} observed answer"
        )
        != observed_sha
        or expected_sha.encode("ascii") in prompt_bytes.lower()
        or expected_sha.encode("ascii") in request_bytes.lower()
        or supplied_commitment.encode("ascii") in request_bytes.lower()
        or secret_binding["path"].encode("utf-8") in request_bytes
    ):
        raise _error(
            "FINAL_MEDIA_REVIEW_MODEL_CAPABILITY_SENTINEL_INVALID",
            f"{modality} challenge commitment was disclosed or did not match",
        )
    return {
        "challenge_id": challenge_id,
        "coverage": raw["coverage"],
        "input_sha256": input_binding["sha256"],
        "prompt_sha256": prompt_binding["sha256"],
        "request_sha256": request_binding["sha256"],
        "response_sha256": response_binding["sha256"],
        "secret_sha256": secret_binding["sha256"],
        "secret_commitment_sha256": supplied_commitment,
        "expected_answer_sha256": expected_sha,
        "observed_answer_sha256": observed_sha,
        "matched": True,
        "answer_disclosed_to_adapter": False,
    }


def _result_document(
    payload: bytes,
    *,
    binding: Mapping[str, object],
    root: Path,
    authority_uid: int | None,
    provider: str,
    model: str,
    endpoint_family: str,
    target_executable_sha256: str,
    now: datetime,
) -> dict[str, object]:
    value = _read_json(payload, label="sentinel result")
    expected_fields = {
        "schema_version",
        "status",
        "authority",
        "run_id",
        "provider",
        "model",
        "endpoint_family",
        "target_executable_sha256",
        "runner",
        "started_at",
        "completed_at",
        "challenges",
        "result_sha256",
    }
    run_id = value.get("run_id")
    challenges = value.get("challenges")
    if (
        set(value) != expected_fields
        or value.get("schema_version") != SENTINEL_RESULT_SCHEMA_VERSION
        or value.get("status") != "COMPLETE"
        or value.get("authority") != MODEL_CAPABILITY_SENTINEL_AUTHORITY
        or not isinstance(run_id, str)
        or _ID_RE.fullmatch(run_id) is None
        or value.get("provider") != provider
        or value.get("model") != model
        or value.get("endpoint_family") != endpoint_family
        or _normalize_sha(
            value.get("target_executable_sha256"), label="target executable"
        )
        != target_executable_sha256
        or not isinstance(challenges, Mapping)
        or set(challenges) != {"raw_audio", "continuous_source_video"}
    ):
        raise _error(
            "FINAL_MEDIA_REVIEW_MODEL_CAPABILITY_SENTINEL_INVALID",
            "sentinel result identity or fields are invalid",
        )
    unsigned = dict(value)
    supplied_result_sha = _normalize_sha(
        unsigned.pop("result_sha256", None), label="sentinel result self-seal"
    )
    if _canonical_sha256(unsigned) != supplied_result_sha:
        raise _error(
            "FINAL_MEDIA_REVIEW_MODEL_CAPABILITY_SENTINEL_INVALID",
            "sentinel result self-hash changed",
        )
    started = _parse_time(value.get("started_at"), label="started_at")
    completed = _parse_time(value.get("completed_at"), label="completed_at")
    if (
        completed < started
        or completed - started > _MAX_SENTINEL_DURATION
        or completed > now + _MAX_FUTURE_SKEW
    ):
        raise _error(
            "FINAL_MEDIA_REVIEW_MODEL_CAPABILITY_SENTINEL_INVALID",
            "sentinel run time window is invalid",
        )
    runner_bytes, runner_binding = _bound_file(
        value.get("runner"),
        root=root,
        label="sentinel runner",
        maximum=_MAX_JSON_BYTES,
        authority_uid=authority_uid,
        executable=True,
    )
    del runner_bytes
    normalized_challenges = {
        modality: _validate_challenge(
            challenges[modality],
            modality=modality,
            root=root,
            authority_uid=authority_uid,
            provider=provider,
            model=model,
            endpoint_family=endpoint_family,
        )
        for modality in ("raw_audio", "continuous_source_video")
    }
    audio = normalized_challenges["raw_audio"]
    video = normalized_challenges["continuous_source_video"]
    if (
        audio["challenge_id"] == video["challenge_id"]
        or audio["input_sha256"] == video["input_sha256"]
        or audio["secret_sha256"] == video["secret_sha256"]
    ):
        raise _error(
            "FINAL_MEDIA_REVIEW_MODEL_CAPABILITY_SENTINEL_INVALID",
            "audio and video challenges are not independent",
        )
    return {
        "binding": dict(binding),
        "result_sha256": supplied_result_sha,
        "run_id": run_id,
        "provider": provider,
        "model": model,
        "endpoint_family": endpoint_family,
        "target_executable_sha256": target_executable_sha256,
        "runner": runner_binding,
        "started_at": _iso(started),
        "completed_at": _iso(completed),
        "completed_datetime": completed,
        "challenges": normalized_challenges,
    }


def _source_binding(root: Path) -> dict[str, object]:
    source = Path(__file__).resolve(strict=True)
    payload, binding = _stable_file(
        source,
        root=source.parents[2],
        label="capability sealer source",
        maximum=_MAX_JSON_BYTES,
        authority_uid=None,
    )
    del payload
    return {
        "path": str(source),
        "sha256": binding["sha256"],
        "bytes": binding["bytes"],
    }


def _attestation_document(
    result: Mapping[str, object],
    *,
    valid_for_seconds: int,
    sealer_source_sha256: str,
) -> dict[str, object]:
    completed = result["completed_datetime"]
    if not isinstance(completed, datetime):
        raise AssertionError("normalized sentinel completion is not a datetime")
    expires = completed + timedelta(seconds=valid_for_seconds)
    value: dict[str, object] = {
        "schema_version": MODEL_CAPABILITY_ATTESTATION_SCHEMA_VERSION,
        "status": "VERIFIED",
        "authority": MODEL_CAPABILITY_AUTHORITY,
        "provider": result["provider"],
        "model": result["model"],
        "endpoint_family": result["endpoint_family"],
        "accepts": {
            "raw_audio": True,
            "continuous_source_video": True,
        },
        "executable_sha256": result["target_executable_sha256"],
        "verification_method": MODEL_CAPABILITY_VERIFICATION_METHOD,
        "verified_at": result["completed_at"],
        "expires_at": _iso(expires),
        "valid_for_seconds": valid_for_seconds,
        "sentinel_result_sha256": result["binding"]["sha256"],
        "sentinel_result_self_sha256": result["result_sha256"],
        "sentinel_runner_sha256": result["runner"]["sha256"],
        "sealer_source_sha256": sealer_source_sha256,
        "challenges": result["challenges"],
    }
    value["attestation_sha256"] = _canonical_sha256(value)
    return value


def _receipt_document(
    *,
    result: Mapping[str, object],
    attestation_binding: Mapping[str, object],
    attestation_sha256: str,
    target_binding: Mapping[str, object],
    source_binding: Mapping[str, object],
    valid_for_seconds: int,
) -> dict[str, object]:
    value: dict[str, object] = {
        "schema_version": MODEL_CAPABILITY_SEAL_RECEIPT_SCHEMA_VERSION,
        "status": "SEALED",
        "authority": MODEL_CAPABILITY_SEAL_AUTHORITY,
        "provider": result["provider"],
        "model": result["model"],
        "endpoint_family": result["endpoint_family"],
        "valid_for_seconds": valid_for_seconds,
        "sentinel_result": result["binding"],
        "sentinel_result_self_sha256": result["result_sha256"],
        "sentinel_runner": result["runner"],
        "target_executable": dict(target_binding),
        "sealer_source": dict(source_binding),
        "attestation": dict(attestation_binding),
        "attestation_sha256": attestation_sha256,
    }
    value["receipt_sha256"] = _canonical_sha256(value)
    return value


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _output_directory(path: Path, *, root: Path, authority_uid: int | None) -> Path:
    raw = path.expanduser()
    if not raw.is_absolute() or raw.is_symlink():
        raise _error(
            "FINAL_MEDIA_REVIEW_MODEL_CAPABILITY_SEAL_PATH_INVALID",
            "seal output must be an absolute non-symlink directory path",
        )
    try:
        raw.parent.resolve(strict=True).relative_to(root)
    except (OSError, RuntimeError, ValueError) as exc:
        raise _error(
            "FINAL_MEDIA_REVIEW_MODEL_CAPABILITY_SEAL_PATH_INVALID",
            "seal output parent escapes the runtime root",
        ) from exc
    if not raw.exists():
        try:
            os.mkdir(raw, 0o700)
            _fsync_directory(raw.parent)
        except OSError as exc:
            raise _error(
                "FINAL_MEDIA_REVIEW_MODEL_CAPABILITY_SEAL_PATH_INVALID",
                "seal output directory could not be created",
            ) from exc
    try:
        info = raw.lstat()
        resolved = raw.resolve(strict=True)
        resolved.relative_to(root)
    except (OSError, RuntimeError, ValueError) as exc:
        raise _error(
            "FINAL_MEDIA_REVIEW_MODEL_CAPABILITY_SEAL_PATH_INVALID",
            "seal output directory is unavailable or unsafe",
        ) from exc
    if (
        raw.is_symlink()
        or not resolved.is_dir()
        or stat.S_IMODE(info.st_mode) != 0o700
        or (authority_uid is not None and info.st_uid != authority_uid)
    ):
        raise _error(
            "FINAL_MEDIA_REVIEW_MODEL_CAPABILITY_SEAL_PATH_INVALID",
            "seal output directory mode or ownership is invalid",
        )
    allowed = {ATTESTATION_FILENAME, SEAL_RECEIPT_FILENAME}
    unexpected = {child.name for child in resolved.iterdir()} - allowed
    if unexpected:
        raise _error(
            "FINAL_MEDIA_REVIEW_MODEL_CAPABILITY_SEAL_CONFLICT",
            f"seal output contains unexpected entries: {sorted(unexpected)}",
        )
    return resolved


def _write_or_match(path: Path, payload: bytes, *, label: str) -> bool:
    if path.exists() or path.is_symlink():
        if path.is_symlink() or not path.is_file() or path.stat().st_nlink != 1:
            raise _error(
                "FINAL_MEDIA_REVIEW_MODEL_CAPABILITY_SEAL_CONFLICT",
                f"existing {label} is not a safe regular file",
            )
        if path.read_bytes() != payload:
            raise _error(
                "FINAL_MEDIA_REVIEW_MODEL_CAPABILITY_SEAL_CONFLICT",
                f"existing {label} differs from the deterministic successor",
            )
        return True
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptor = -1
    try:
        descriptor = os.open(path, flags, 0o600)
        offset = 0
        while offset < len(payload):
            written = os.write(descriptor, payload[offset:])
            if written <= 0:
                raise OSError("short write")
            offset += written
        os.fsync(descriptor)
    except OSError as exc:
        raise _error(
            "FINAL_MEDIA_REVIEW_MODEL_CAPABILITY_SEAL_WRITE_FAILED",
            f"failed to create {label}",
        ) from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    _fsync_directory(path.parent)
    return False


def _binding_from_payload(path: Path, *, root: Path, payload: bytes) -> dict[str, object]:
    return {
        "path": path.relative_to(root).as_posix(),
        "sha256": _sha256_bytes(payload),
        "bytes": len(payload),
    }


def seal_model_capability_attestation(
    *,
    runtime_root: str | Path,
    sentinel_result_path: str | Path,
    target_executable_path: str | Path,
    output_directory: str | Path,
    valid_for_seconds: int,
    now: datetime | None = None,
    failpoint: str | None = None,
) -> dict[str, object]:
    """Seal one completed sentinel run without any provider call."""

    if (
        isinstance(valid_for_seconds, bool)
        or not isinstance(valid_for_seconds, int)
        or not _MIN_VALIDITY_SECONDS
        <= valid_for_seconds
        <= _MAX_VALIDITY_SECONDS
    ):
        raise _error(
            "FINAL_MEDIA_REVIEW_MODEL_CAPABILITY_SEAL_INVALID",
            "valid_for_seconds is outside the accepted range",
        )
    root = _safe_runtime_root(runtime_root)
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    authority_uid = root.stat().st_uid
    result_bytes, result_binding = _stable_file(
        Path(sentinel_result_path).expanduser(),
        root=root,
        label="sentinel result",
        maximum=_MAX_JSON_BYTES,
        authority_uid=authority_uid,
    )
    target_bytes, target_binding = _stable_file(
        Path(target_executable_path).expanduser(),
        root=root,
        label="target raw-AV executable",
        maximum=_MAX_CHALLENGE_BYTES,
        authority_uid=authority_uid,
        executable=True,
    )
    del target_bytes
    raw_result = _read_json(result_bytes, label="sentinel result")
    provider = raw_result.get("provider")
    model = raw_result.get("model")
    endpoint_family = raw_result.get("endpoint_family")
    if not all(isinstance(item, str) and item for item in (provider, model, endpoint_family)):
        raise _error(
            "FINAL_MEDIA_REVIEW_MODEL_CAPABILITY_SENTINEL_INVALID",
            "sentinel provider/model/endpoint identity is missing",
        )
    result = _result_document(
        result_bytes,
        binding=result_binding,
        root=root,
        authority_uid=authority_uid,
        provider=provider,
        model=model,
        endpoint_family=endpoint_family,
        target_executable_sha256=target_binding["sha256"],
        now=current,
    )
    completed = result["completed_datetime"]
    if not isinstance(completed, datetime) or completed + timedelta(
        seconds=valid_for_seconds
    ) <= current:
        raise _error(
            "FINAL_MEDIA_REVIEW_MODEL_CAPABILITY_ATTESTATION_EXPIRED",
            "sentinel result is too old for the requested validity window",
        )
    source = _source_binding(root)
    attestation = _attestation_document(
        result,
        valid_for_seconds=valid_for_seconds,
        sealer_source_sha256=source["sha256"],
    )
    attestation_payload = _json_bytes(attestation)
    output = _output_directory(
        Path(output_directory), root=root, authority_uid=authority_uid
    )
    attestation_path = output / ATTESTATION_FILENAME
    receipt_path = output / SEAL_RECEIPT_FILENAME
    attestation_binding = _binding_from_payload(
        attestation_path, root=root, payload=attestation_payload
    )
    receipt = _receipt_document(
        result=result,
        attestation_binding=attestation_binding,
        attestation_sha256=attestation["attestation_sha256"],
        target_binding=target_binding,
        source_binding=source,
        valid_for_seconds=valid_for_seconds,
    )
    receipt_payload = _json_bytes(receipt)
    attestation_reused = _write_or_match(
        attestation_path, attestation_payload, label="model capability attestation"
    )
    if failpoint == "after_attestation":
        raise RuntimeError("TEST_FAILPOINT_AFTER_ATTESTATION")
    receipt_reused = _write_or_match(
        receipt_path, receipt_payload, label="model capability seal receipt"
    )
    validated = validate_model_capability_seal_receipt(
        {
            "path": receipt_path.relative_to(root).as_posix(),
            "sha256": _sha256_bytes(receipt_payload),
            "bytes": len(receipt_payload),
        },
        runtime_root=root,
        provider=provider,
        model=model,
        endpoint_family=endpoint_family,
        executable_sha256=target_binding["sha256"],
        authority_uid=authority_uid,
        now=current,
    )
    return {
        "status": "SEALED_MODEL_CAPABILITY",
        "cache_reused": attestation_reused and receipt_reused,
        "recovered_after_attestation_only": attestation_reused and not receipt_reused,
        "seal_receipt": {
            "path": receipt_path.relative_to(root).as_posix(),
            "sha256": _sha256_bytes(receipt_payload),
            "bytes": len(receipt_payload),
        },
        "attestation": attestation_binding,
        "validated": validated,
        "provider_calls": 0,
    }


def validate_model_capability_seal_receipt(
    raw_binding: object,
    *,
    runtime_root: str | Path,
    provider: str,
    model: str,
    endpoint_family: str,
    executable_sha256: str,
    authority_uid: int | None = None,
    now: datetime | None = None,
) -> dict[str, object]:
    """Recompute a seal receipt, result and deterministic attestation."""

    root = _safe_runtime_root(runtime_root)
    receipt_bytes, receipt_binding = _bound_file(
        raw_binding,
        root=root,
        label="model capability seal receipt",
        maximum=_MAX_JSON_BYTES,
        authority_uid=authority_uid,
        private=True,
    )
    receipt = _read_json(receipt_bytes, label="model capability seal receipt")
    expected_fields = {
        "schema_version",
        "status",
        "authority",
        "provider",
        "model",
        "endpoint_family",
        "valid_for_seconds",
        "sentinel_result",
        "sentinel_result_self_sha256",
        "sentinel_runner",
        "target_executable",
        "sealer_source",
        "attestation",
        "attestation_sha256",
        "receipt_sha256",
    }
    valid_for_seconds = receipt.get("valid_for_seconds")
    if (
        set(receipt) != expected_fields
        or receipt.get("schema_version")
        != MODEL_CAPABILITY_SEAL_RECEIPT_SCHEMA_VERSION
        or receipt.get("status") != "SEALED"
        or receipt.get("authority") != MODEL_CAPABILITY_SEAL_AUTHORITY
        or receipt.get("provider") != provider
        or receipt.get("model") != model
        or receipt.get("endpoint_family") != endpoint_family
        or isinstance(valid_for_seconds, bool)
        or not isinstance(valid_for_seconds, int)
        or not _MIN_VALIDITY_SECONDS
        <= valid_for_seconds
        <= _MAX_VALIDITY_SECONDS
    ):
        raise _error(
            "FINAL_MEDIA_REVIEW_MODEL_CAPABILITY_SEAL_INVALID",
            "model capability seal receipt fields are invalid",
        )
    unsigned_receipt = dict(receipt)
    supplied_receipt_sha = _normalize_sha(
        unsigned_receipt.pop("receipt_sha256", None), label="seal receipt self-hash"
    )
    if _canonical_sha256(unsigned_receipt) != supplied_receipt_sha:
        raise _error(
            "FINAL_MEDIA_REVIEW_MODEL_CAPABILITY_SEAL_INVALID",
            "model capability seal receipt self-hash changed",
        )
    current_source = _source_binding(root)
    if receipt.get("sealer_source") != current_source:
        raise _error(
            "FINAL_MEDIA_REVIEW_MODEL_CAPABILITY_SEAL_INVALID",
            "model capability sealer source changed",
        )
    target_bytes, target_binding = _bound_file(
        receipt.get("target_executable"),
        root=root,
        label="sealed target executable",
        maximum=_MAX_CHALLENGE_BYTES,
        authority_uid=authority_uid,
        executable=True,
    )
    del target_bytes
    expected_executable_sha = _normalize_sha(
        executable_sha256, label="expected target executable"
    )
    if target_binding["sha256"] != expected_executable_sha:
        raise _error(
            "FINAL_MEDIA_REVIEW_MODEL_CAPABILITY_SEAL_INVALID",
            "sealed target executable differs from the runtime adapter",
        )
    result_bytes, result_binding = _bound_file(
        receipt.get("sentinel_result"),
        root=root,
        label="sealed sentinel result",
        maximum=_MAX_JSON_BYTES,
        authority_uid=authority_uid,
    )
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    result = _result_document(
        result_bytes,
        binding=result_binding,
        root=root,
        authority_uid=authority_uid,
        provider=provider,
        model=model,
        endpoint_family=endpoint_family,
        target_executable_sha256=expected_executable_sha,
        now=current,
    )
    if (
        receipt.get("sentinel_result_self_sha256") != result["result_sha256"]
        or receipt.get("sentinel_runner") != result["runner"]
    ):
        raise _error(
            "FINAL_MEDIA_REVIEW_MODEL_CAPABILITY_SEAL_INVALID",
            "seal receipt differs from the sentinel result",
        )
    attestation_bytes, attestation_binding = _bound_file(
        receipt.get("attestation"),
        root=root,
        label="sealed model capability attestation",
        maximum=_MAX_JSON_BYTES,
        authority_uid=authority_uid,
        private=True,
    )
    expected_attestation = _attestation_document(
        result,
        valid_for_seconds=valid_for_seconds,
        sealer_source_sha256=current_source["sha256"],
    )
    expected_attestation_bytes = _json_bytes(expected_attestation)
    if (
        attestation_bytes != expected_attestation_bytes
        or attestation_binding != receipt.get("attestation")
        or receipt.get("attestation_sha256")
        != expected_attestation["attestation_sha256"]
    ):
        raise _error(
            "FINAL_MEDIA_REVIEW_MODEL_CAPABILITY_SEAL_INVALID",
            "sealed attestation differs from the deterministic projection",
        )
    expires = _parse_time(expected_attestation["expires_at"], label="expires_at")
    if current >= expires:
        raise _error(
            "FINAL_MEDIA_REVIEW_MODEL_CAPABILITY_ATTESTATION_EXPIRED",
            "model-modality attestation has expired",
        )
    contract = {
        "provider": provider,
        "model": model,
        "endpoint_family": endpoint_family,
        "accepts": {
            "raw_audio": True,
            "continuous_source_video": True,
        },
        "executable_sha256": expected_executable_sha,
        "seal_receipt_file_sha256": receipt_binding["sha256"],
        "seal_receipt_sha256": supplied_receipt_sha,
        "attestation_file_sha256": attestation_binding["sha256"],
        "attestation_sha256": expected_attestation["attestation_sha256"],
        "sentinel_result_sha256": result_binding["sha256"],
        "sentinel_result_self_sha256": result["result_sha256"],
        "sentinel_runner_sha256": result["runner"]["sha256"],
        "verification_method": MODEL_CAPABILITY_VERIFICATION_METHOD,
        "verified_at": expected_attestation["verified_at"],
        "expires_at": expected_attestation["expires_at"],
        "challenges": result["challenges"],
    }
    return {
        "seal_receipt_path": str(root / receipt_binding["path"]),
        "seal_receipt_file_sha256": receipt_binding["sha256"],
        "seal_receipt_bytes": receipt_binding["bytes"],
        "seal_receipt_sha256": supplied_receipt_sha,
        "attestation_path": str(root / attestation_binding["path"]),
        "attestation_file_sha256": attestation_binding["sha256"],
        "attestation_bytes": attestation_binding["bytes"],
        "attestation_sha256": expected_attestation["attestation_sha256"],
        "sentinel_result_sha256": result_binding["sha256"],
        "sentinel_result_self_sha256": result["result_sha256"],
        "sentinel_runner_sha256": result["runner"]["sha256"],
        "contract_sha256": _canonical_sha256(contract),
        "provider": provider,
        "model": model,
        "endpoint_family": endpoint_family,
        "accepts": dict(contract["accepts"]),
        "verification_method": MODEL_CAPABILITY_VERIFICATION_METHOD,
        "verified_at": expected_attestation["verified_at"],
        "expires_at": expected_attestation["expires_at"],
        "challenges": result["challenges"],
    }


__all__ = [
    "ATTESTATION_FILENAME",
    "FinalMediaReviewModelCapabilitySealError",
    "MODEL_CAPABILITY_ATTESTATION_SCHEMA_VERSION",
    "MODEL_CAPABILITY_AUTHORITY",
    "MODEL_CAPABILITY_SEAL_AUTHORITY",
    "MODEL_CAPABILITY_SEAL_RECEIPT_SCHEMA_VERSION",
    "MODEL_CAPABILITY_SENTINEL_AUTHORITY",
    "MODEL_CAPABILITY_VERIFICATION_METHOD",
    "SEAL_RECEIPT_FILENAME",
    "SENTINEL_REQUEST_SCHEMA_VERSION",
    "SENTINEL_RESPONSE_SCHEMA_VERSION",
    "SENTINEL_RESULT_SCHEMA_VERSION",
    "SENTINEL_SECRET_SCHEMA_VERSION",
    "seal_model_capability_attestation",
    "validate_model_capability_seal_receipt",
]
