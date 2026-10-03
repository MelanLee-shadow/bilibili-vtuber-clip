"""Hash-bound raw-audio and continuous-video transport for final-media review.

The ordinary final-media consumer must not infer provider capability from two
booleans in a job.  This module makes the capability concrete and portable:

* a runtime-owned manifest binds one executable and argv contract;
* an independent, short-lived dual-media sentinel attests model modalities;
* a package-local receipt records only hashes and non-secret capability facts;
* a create-only successor job enables raw audio/video only after that receipt
  exists;
* the executable is invoked without a shell under the shared provider slot;
* a response is accepted only when its adapter receipt binds the exact request,
  source MP4 and full-media WAV.

No production capability is synthesized here.  Model lists, manifest booleans
and adapter self-reports are insufficient.  When either the runtime manifest or
its sentinel attestation is absent, the caller remains fail-closed without a
provider call.
"""
from __future__ import annotations

from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tempfile
from typing import Callable, Mapping

from src.autoslice.final_media_review_model_capability import (
    FinalMediaReviewModelCapabilityError,
    validate_model_capability_attestation,
)
from src.autoslice.llm_client import LlmCallError
from src.autoslice.provider_slots import (
    ProviderSlotError,
    ProviderSlotTimeout,
    provider_wait_for_call,
    runtime_provider_slot,
)


RUNTIME_CAPABILITY_SCHEMA_VERSION = (
    "final-media-review-raw-av-runtime-capability.v3"
)
PACKAGE_BINDING_SCHEMA_VERSION = "final-media-review-raw-av-package-binding.v3"
BINDING_RECEIPT_SCHEMA_VERSION = "final-media-review-raw-av-binding-receipt.v3"
REQUEST_SCHEMA_VERSION = "final-media-review-raw-av-request.v3"
ADAPTER_RESPONSE_SCHEMA_VERSION = "final-media-review-raw-av-adapter-response.v3"
ADAPTER_RECEIPT_SCHEMA_VERSION = "final-media-review-raw-av-adapter-receipt.v3"
TRANSPORT_EVIDENCE_SCHEMA_VERSION = "final-media-review-raw-av-evidence.v3"
RESULT_SCHEMA_VERSION = "final-media-perceptual-review-result.v1"
JOB_SCHEMA_VERSION = "final-media-review-input-job.v1"
RUNTIME_CAPABILITY_FILENAME = "final-media-review-raw-av-capability.json"

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_PLACEHOLDER_RE = re.compile(r"\{[^{}]+\}")
_ALLOWED_PLACEHOLDERS = frozenset(
    {"{executable}", "{request_json}", "{response_json}"}
)
_MAX_JSON_BYTES = 8_000_000
_MAX_CAPABILITY_BYTES = 1_000_000
_MAX_CAPTURE_BYTES = 1_000_000


class FinalMediaReviewRawAvError(ValueError):
    """A typed fail-closed raw-AV capability or binding error."""

    def __init__(self, reason_code: str, detail: str):
        super().__init__(f"{reason_code}: {detail}")
        self.reason_code = reason_code
        self.detail = detail


def _error(reason_code: str, detail: str) -> FinalMediaReviewRawAvError:
    return FinalMediaReviewRawAvError(reason_code, detail)


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
            "FINAL_MEDIA_REVIEW_RAW_AV_JSON_INVALID",
            "value is not canonical JSON",
        ) from exc


def _canonical_sha256(value: object) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _normalize_sha(value: object, *, label: str) -> str:
    if not isinstance(value, str):
        raise _error(
            "FINAL_MEDIA_REVIEW_RAW_AV_BINDING_INVALID",
            f"{label} sha256 missing",
        )
    normalized = value.strip().lower()
    if normalized.startswith("sha256:"):
        normalized = normalized[7:]
    if _SHA256_RE.fullmatch(normalized) is None:
        raise _error(
            "FINAL_MEDIA_REVIEW_RAW_AV_BINDING_INVALID",
            f"{label} sha256 invalid",
        )
    return normalized


def _safe_root(value: str | Path, *, label: str) -> Path:
    raw = Path(value).expanduser()
    if not raw.is_absolute() or raw.is_symlink():
        raise _error(
            "FINAL_MEDIA_REVIEW_RAW_AV_PATH_INVALID",
            f"{label} must be an absolute non-symlink directory",
        )
    try:
        resolved = raw.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise _error(
            "FINAL_MEDIA_REVIEW_RAW_AV_PATH_INVALID",
            f"{label} is unavailable: {raw}",
        ) from exc
    if not resolved.is_dir():
        raise _error(
            "FINAL_MEDIA_REVIEW_RAW_AV_PATH_INVALID",
            f"{label} is not a directory: {raw}",
        )
    return resolved


def _contained_regular_file(
    value: object,
    *,
    root: Path,
    label: str,
) -> Path:
    if not isinstance(value, (str, Path)) or not str(value):
        raise _error(
            "FINAL_MEDIA_REVIEW_RAW_AV_PATH_INVALID", f"{label} path missing"
        )
    raw = Path(value).expanduser()
    try:
        info = raw.lstat()
        resolved = raw.resolve(strict=True)
        resolved.relative_to(root)
    except (OSError, RuntimeError, ValueError) as exc:
        raise _error(
            "FINAL_MEDIA_REVIEW_RAW_AV_PATH_INVALID",
            f"{label} is unavailable or escapes its authority root: {raw}",
        ) from exc
    if raw.is_symlink() or not stat.S_ISREG(info.st_mode) or not resolved.is_file():
        raise _error(
            "FINAL_MEDIA_REVIEW_RAW_AV_PATH_INVALID",
            f"{label} must be a regular non-symlink file: {raw}",
        )
    return resolved


def _read_json(path: Path, *, label: str, maximum: int = _MAX_JSON_BYTES) -> dict[str, object]:
    try:
        info = path.stat()
        if not 0 < info.st_size <= maximum:
            raise ValueError("size outside accepted range")
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise _error(
            "FINAL_MEDIA_REVIEW_RAW_AV_JSON_INVALID",
            f"{label} is not a bounded JSON object: {path}: {exc}",
        ) from exc
    if not isinstance(value, dict):
        raise _error(
            "FINAL_MEDIA_REVIEW_RAW_AV_JSON_INVALID",
            f"{label} root must be an object",
        )
    return value


def _json_bytes(value: Mapping[str, object]) -> bytes:
    try:
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
    except (TypeError, ValueError) as exc:
        raise _error(
            "FINAL_MEDIA_REVIEW_RAW_AV_JSON_INVALID",
            "document cannot be encoded as canonical JSON",
        ) from exc


def _write_private(path: Path, payload: bytes) -> None:
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _binding(path: Path) -> dict[str, object]:
    return {
        "path": str(path),
        "sha256": _sha256(path),
        "bytes": path.stat().st_size,
    }


def _planned_binding(path: Path, payload: bytes) -> dict[str, object]:
    return {
        "path": str(path),
        "sha256": hashlib.sha256(payload).hexdigest(),
        "bytes": len(payload),
    }


def _validate_argv(raw: object) -> list[str]:
    if (
        not isinstance(raw, list)
        or not 3 <= len(raw) <= 32
        or not all(isinstance(token, str) and token for token in raw)
    ):
        raise _error(
            "FINAL_MEDIA_REVIEW_RAW_AV_CAPABILITY_INVALID",
            "argv must be a bounded non-empty string array",
        )
    argv = list(raw)
    if argv[0] != "{executable}":
        raise _error(
            "FINAL_MEDIA_REVIEW_RAW_AV_CAPABILITY_INVALID",
            "argv[0] must be {executable}",
        )
    for token in argv:
        if len(token) > 4096 or "\x00" in token or "\n" in token or "\r" in token:
            raise _error(
                "FINAL_MEDIA_REVIEW_RAW_AV_CAPABILITY_INVALID",
                "argv contains an unsafe token",
            )
        placeholders = _PLACEHOLDER_RE.findall(token)
        if placeholders and (token not in _ALLOWED_PLACEHOLDERS or len(placeholders) != 1):
            raise _error(
                "FINAL_MEDIA_REVIEW_RAW_AV_CAPABILITY_INVALID",
                "argv placeholders must be standalone approved tokens",
            )
    if argv.count("{request_json}") != 1 or argv.count("{response_json}") != 1:
        raise _error(
            "FINAL_MEDIA_REVIEW_RAW_AV_CAPABILITY_INVALID",
            "argv must bind request_json and response_json exactly once",
        )
    return argv


def _command_contract(capability: Mapping[str, object]) -> dict[str, object]:
    executable = capability["executable"]
    model_capability = capability.get("model_capability")
    return {
        "transport": capability["transport"],
        "model": capability["model"],
        "endpoint_family": capability["endpoint_family"],
        "executable_path": executable["path"],
        "executable_sha256": executable["sha256"],
        "argv": capability["argv"],
        "timeout_seconds": capability["timeout_seconds"],
        "result_schema_version": capability["result_schema_version"],
        "model_capability_seal": capability["model_capability_seal"],
        "model_capability_contract_sha256": (
            model_capability.get("contract_sha256")
            if isinstance(model_capability, Mapping)
            else None
        ),
    }


def load_runtime_raw_av_capability(
    runtime_root: str | Path,
    *,
    now: datetime | None = None,
) -> dict[str, object] | None:
    """Load the fixed runtime capability manifest, or return None when absent."""

    root = _safe_root(runtime_root, label="runtime root")
    path = root / RUNTIME_CAPABILITY_FILENAME
    if not path.exists() and not path.is_symlink():
        return None
    target = _contained_regular_file(path, root=root, label="runtime capability")
    mode = stat.S_IMODE(target.stat().st_mode)
    if mode & 0o022:
        raise _error(
            "FINAL_MEDIA_REVIEW_RAW_AV_CAPABILITY_INVALID",
            "runtime capability manifest is group/world writable",
        )
    value = _read_json(
        target, label="runtime capability", maximum=_MAX_CAPABILITY_BYTES
    )
    expected_fields = {
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
        "model_capability_seal",
    }
    if set(value) != expected_fields:
        raise _error(
            "FINAL_MEDIA_REVIEW_RAW_AV_CAPABILITY_INVALID",
            "runtime capability fields differ from the canonical schema",
        )
    capability_id = value.get("capability_id")
    accepts = value.get("accepts")
    executable = value.get("executable")
    model = value.get("model")
    endpoint_family = value.get("endpoint_family")
    timeout = value.get("timeout_seconds")
    if (
        value.get("schema_version") != RUNTIME_CAPABILITY_SCHEMA_VERSION
        or not isinstance(capability_id, str)
        or _ID_RE.fullmatch(capability_id) is None
        or not isinstance(value.get("provider"), str)
        or _ID_RE.fullmatch(str(value.get("provider"))) is None
        or value.get("transport") != "content_bound_command"
        or not isinstance(model, str)
        or _ID_RE.fullmatch(model) is None
        or not isinstance(endpoint_family, str)
        or _ID_RE.fullmatch(endpoint_family) is None
        or not isinstance(accepts, Mapping)
        or set(accepts) != {"raw_audio", "continuous_source_video"}
        or accepts.get("raw_audio") is not True
        or accepts.get("continuous_source_video") is not True
        or not isinstance(executable, Mapping)
        or set(executable) != {"path", "sha256"}
        or isinstance(timeout, bool)
        or not isinstance(timeout, int)
        or not 1 <= timeout <= 3600
        or value.get("result_schema_version") != RESULT_SCHEMA_VERSION
    ):
        raise _error(
            "FINAL_MEDIA_REVIEW_RAW_AV_CAPABILITY_INVALID",
            "runtime capability values are invalid or incomplete",
        )
    relative = executable.get("path")
    if (
        not isinstance(relative, str)
        or not relative
        or Path(relative).is_absolute()
        or ".." in Path(relative).parts
    ):
        raise _error(
            "FINAL_MEDIA_REVIEW_RAW_AV_CAPABILITY_INVALID",
            "executable path must be a contained relative path",
        )
    executable_path = _contained_regular_file(
        root / relative, root=root, label="raw-AV executable"
    )
    executable_mode = stat.S_IMODE(executable_path.stat().st_mode)
    if not executable_mode & 0o111 or executable_mode & 0o022:
        raise _error(
            "FINAL_MEDIA_REVIEW_RAW_AV_CAPABILITY_INVALID",
            "raw-AV executable must be executable and not group/world writable",
        )
    executable_sha = _normalize_sha(
        executable.get("sha256"), label="raw-AV executable"
    )
    if _sha256(executable_path) != executable_sha:
        raise _error(
            "FINAL_MEDIA_REVIEW_RAW_AV_CAPABILITY_INVALID",
            "raw-AV executable hash changed",
        )
    try:
        model_capability = validate_model_capability_attestation(
            value.get("model_capability_seal"),
            runtime_root=root,
            provider=str(value["provider"]),
            model=model,
            endpoint_family=endpoint_family,
            executable_sha256=executable_sha,
            authority_uid=target.stat().st_uid,
            now=now,
        )
    except FinalMediaReviewModelCapabilityError as exc:
        raise _error(exc.reason_code, exc.detail) from exc
    argv = _validate_argv(value.get("argv"))
    normalized = {
        **value,
        "argv": argv,
        "accepts": dict(accepts),
        "executable": {"path": relative, "sha256": executable_sha},
        "runtime_root": str(root),
        "runtime_capability_path": str(target),
        "runtime_capability_sha256": _sha256(target),
        "executable_path": str(executable_path),
        "model_capability": model_capability,
    }
    normalized["command_contract_sha256"] = _canonical_sha256(
        _command_contract(normalized)
    )
    return normalized


def package_binding_document(
    capability: Mapping[str, object],
) -> dict[str, object]:
    """Return the non-secret package-local projection of a runtime capability."""

    model_capability = capability["model_capability"]
    return {
        "schema_version": PACKAGE_BINDING_SCHEMA_VERSION,
        "capability_id": capability["capability_id"],
        "provider": capability["provider"],
        "transport": capability["transport"],
        "model": capability["model"],
        "endpoint_family": capability["endpoint_family"],
        "accepts": dict(capability["accepts"]),
        "runtime_capability_sha256": capability["runtime_capability_sha256"],
        "executable_sha256": capability["executable"]["sha256"],
        "command_contract_sha256": capability["command_contract_sha256"],
        "model_capability_seal_receipt_file_sha256": model_capability[
            "seal_receipt_file_sha256"
        ],
        "model_capability_seal_receipt_sha256": model_capability[
            "seal_receipt_sha256"
        ],
        "model_capability_attestation_file_sha256": model_capability[
            "file_sha256"
        ],
        "model_capability_attestation_sha256": model_capability[
            "attestation_sha256"
        ],
        "model_capability_contract_sha256": model_capability[
            "contract_sha256"
        ],
        "model_capability_sentinel_result_sha256": model_capability[
            "sentinel_result_sha256"
        ],
        "model_capability_sentinel_result_self_sha256": model_capability[
            "sentinel_result_self_sha256"
        ],
        "model_capability_sentinel_runner_sha256": model_capability[
            "sentinel_runner_sha256"
        ],
        "model_capability_verification_method": model_capability[
            "verification_method"
        ],
        "model_capability_expires_at": model_capability["expires_at"],
        "result_schema_version": capability["result_schema_version"],
    }


def validate_package_transport_binding(
    raw_binding: object,
    *,
    allowed_root: str | Path | None,
) -> dict[str, object]:
    """Validate one package-local capability receipt and return safe facts."""

    if not isinstance(raw_binding, Mapping):
        raise _error(
            "FINAL_MEDIA_REVIEW_TRANSPORT_CAPABILITY_BINDING_MISSING",
            "provider capability booleans require a package-local binding",
        )
    if allowed_root is None:
        raw_path = raw_binding.get("path")
        if not isinstance(raw_path, (str, Path)) or not str(raw_path):
            raise _error(
                "FINAL_MEDIA_REVIEW_TRANSPORT_CAPABILITY_BINDING_INVALID",
                "transport capability binding path missing",
            )
        path = Path(raw_path).expanduser()
        try:
            info = path.lstat()
            path = path.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise _error(
                "FINAL_MEDIA_REVIEW_TRANSPORT_CAPABILITY_BINDING_INVALID",
                "transport capability binding is unavailable",
            ) from exc
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or not path.is_file():
            raise _error(
                "FINAL_MEDIA_REVIEW_TRANSPORT_CAPABILITY_BINDING_INVALID",
                "transport capability binding must be a regular non-symlink file",
            )
    else:
        root = _safe_root(allowed_root, label="package root")
        path = _contained_regular_file(
            raw_binding.get("path"),
            root=root,
            label="transport capability binding",
        )
    expected_sha = _normalize_sha(
        raw_binding.get("sha256"), label="transport capability binding"
    )
    expected_bytes = raw_binding.get("bytes")
    if (
        isinstance(expected_bytes, bool)
        or not isinstance(expected_bytes, int)
        or expected_bytes < 1
        or path.stat().st_size != expected_bytes
        or _sha256(path) != expected_sha
    ):
        raise _error(
            "FINAL_MEDIA_REVIEW_TRANSPORT_CAPABILITY_BINDING_INVALID",
            "package-local transport capability binding drifted",
        )
    value = _read_json(path, label="transport capability binding")
    expected_fields = {
        "schema_version",
        "capability_id",
        "provider",
        "transport",
        "model",
        "endpoint_family",
        "accepts",
        "runtime_capability_sha256",
        "executable_sha256",
        "command_contract_sha256",
        "model_capability_seal_receipt_file_sha256",
        "model_capability_seal_receipt_sha256",
        "model_capability_attestation_file_sha256",
        "model_capability_attestation_sha256",
        "model_capability_contract_sha256",
        "model_capability_sentinel_result_sha256",
        "model_capability_sentinel_result_self_sha256",
        "model_capability_sentinel_runner_sha256",
        "model_capability_verification_method",
        "model_capability_expires_at",
        "result_schema_version",
    }
    accepts = value.get("accepts")
    if (
        set(value) != expected_fields
        or value.get("schema_version") != PACKAGE_BINDING_SCHEMA_VERSION
        or not isinstance(value.get("capability_id"), str)
        or _ID_RE.fullmatch(str(value.get("capability_id"))) is None
        or not isinstance(value.get("provider"), str)
        or _ID_RE.fullmatch(str(value.get("provider"))) is None
        or value.get("transport") != "content_bound_command"
        or not isinstance(value.get("model"), str)
        or _ID_RE.fullmatch(str(value.get("model"))) is None
        or not isinstance(value.get("endpoint_family"), str)
        or _ID_RE.fullmatch(str(value.get("endpoint_family"))) is None
        or not isinstance(accepts, Mapping)
        or set(accepts) != {"raw_audio", "continuous_source_video"}
        or not all(isinstance(accepts.get(key), bool) for key in accepts)
        or value.get("model_capability_verification_method")
        != "sealed_dual_media_sentinel"
        or not isinstance(value.get("model_capability_expires_at"), str)
        or not str(value.get("model_capability_expires_at")).endswith("Z")
        or value.get("result_schema_version") != RESULT_SCHEMA_VERSION
    ):
        raise _error(
            "FINAL_MEDIA_REVIEW_TRANSPORT_CAPABILITY_BINDING_INVALID",
            "package-local transport capability document is invalid",
        )
    for key in (
        "runtime_capability_sha256",
        "executable_sha256",
        "command_contract_sha256",
        "model_capability_seal_receipt_file_sha256",
        "model_capability_seal_receipt_sha256",
        "model_capability_attestation_file_sha256",
        "model_capability_attestation_sha256",
        "model_capability_contract_sha256",
        "model_capability_sentinel_result_sha256",
        "model_capability_sentinel_result_self_sha256",
        "model_capability_sentinel_runner_sha256",
    ):
        _normalize_sha(value.get(key), label=key)
    return {
        "path": str(path),
        "sha256": expected_sha,
        "bytes": expected_bytes,
        "capability_id": value["capability_id"],
        "provider": value["provider"],
        "transport": value["transport"],
        "model": value["model"],
        "endpoint_family": value["endpoint_family"],
        "accepts": dict(accepts),
        "runtime_capability_sha256": value["runtime_capability_sha256"],
        "executable_sha256": value["executable_sha256"],
        "command_contract_sha256": value["command_contract_sha256"],
        "model_capability_seal_receipt_file_sha256": value[
            "model_capability_seal_receipt_file_sha256"
        ],
        "model_capability_seal_receipt_sha256": value[
            "model_capability_seal_receipt_sha256"
        ],
        "model_capability_attestation_file_sha256": value[
            "model_capability_attestation_file_sha256"
        ],
        "model_capability_attestation_sha256": value[
            "model_capability_attestation_sha256"
        ],
        "model_capability_contract_sha256": value[
            "model_capability_contract_sha256"
        ],
        "model_capability_sentinel_result_sha256": value[
            "model_capability_sentinel_result_sha256"
        ],
        "model_capability_sentinel_result_self_sha256": value[
            "model_capability_sentinel_result_self_sha256"
        ],
        "model_capability_sentinel_runner_sha256": value[
            "model_capability_sentinel_runner_sha256"
        ],
        "model_capability_verification_method": value[
            "model_capability_verification_method"
        ],
        "model_capability_expires_at": value[
            "model_capability_expires_at"
        ],
        "result_schema_version": value["result_schema_version"],
    }


def _safe_capability_parent(root: Path, candidate_id: str) -> Path:
    verification = root / "verification"
    if verification.is_symlink() or not verification.is_dir():
        raise _error(
            "FINAL_MEDIA_REVIEW_RAW_AV_PATH_INVALID",
            "package verification directory is missing or unsafe",
        )
    parent = verification / "final-media-review-capabilities"
    candidate = parent / candidate_id
    for directory in (parent, candidate):
        if directory.exists() or directory.is_symlink():
            if directory.is_symlink() or not directory.is_dir():
                raise _error(
                    "FINAL_MEDIA_REVIEW_RAW_AV_PATH_INVALID",
                    f"capability output parent is unsafe: {directory}",
                )
        else:
            directory.mkdir(mode=0o700)
        directory.chmod(0o700)
    return candidate


def _validate_existing_binding_target(
    *,
    target: Path,
    original_job_path: Path,
    original_job: Mapping[str, object],
    capability: Mapping[str, object],
    package_root: Path,
) -> dict[str, object]:
    from src.autoslice.final_media_review_inputs import assess_review_inputs

    receipt_path = target / "binding-receipt.json"
    binding_path = target / "transport-capability.json"
    job_path = target / "review-job.raw-av.json"
    receipt = _read_json(receipt_path, label="raw-AV binding receipt")
    supplied = receipt.get("receipt_sha256")
    unsigned = dict(receipt)
    unsigned.pop("receipt_sha256", None)
    if not (
        receipt.get("schema_version") == BINDING_RECEIPT_SCHEMA_VERSION
        and receipt.get("status") == "BOUND_RUNTIME_RAW_AV_CAPABILITY"
        and receipt.get("candidate_id") == original_job.get("candidate_id")
        and receipt.get("original_job_sha256") == _sha256(original_job_path)
        and receipt.get("original_job_binding_sha256")
        == _canonical_sha256(original_job)
        and receipt.get("runtime_capability_sha256")
        == capability["runtime_capability_sha256"]
        and supplied == _canonical_sha256(unsigned)
    ):
        raise _error(
            "FINAL_MEDIA_REVIEW_RAW_AV_TARGET_CONFLICT",
            "existing capability binding receipt identity differs",
        )
    for key, path in (
        ("transport_capability", binding_path),
        ("successor_job", job_path),
    ):
        binding = receipt.get(key)
        if not isinstance(binding, Mapping):
            raise _error(
                "FINAL_MEDIA_REVIEW_RAW_AV_TARGET_CONFLICT",
                f"existing {key} binding missing",
            )
        expected = _normalize_sha(binding.get("sha256"), label=key)
        expected_bytes = binding.get("bytes")
        if not (
            binding.get("path") == str(path)
            and path.is_file()
            and not path.is_symlink()
            and isinstance(expected_bytes, int)
            and not isinstance(expected_bytes, bool)
            and path.stat().st_size == expected_bytes
            and _sha256(path) == expected
        ):
            raise _error(
                "FINAL_MEDIA_REVIEW_RAW_AV_TARGET_CONFLICT",
                f"existing {key} binding drifted",
            )
    job = _read_json(job_path, label="raw-AV successor job")
    assessment = assess_review_inputs(job, allowed_root=package_root)
    return {
        "status": "BOUND_RUNTIME_RAW_AV_CAPABILITY",
        "cache_reused": True,
        "active_job_path": str(job_path),
        "binding_path": str(binding_path),
        "receipt_path": str(receipt_path),
        "assessment": assessment,
    }


def bind_review_job_to_runtime_capability(
    job_path: str | Path,
    *,
    allowed_root: str | Path,
    runtime_root: str | Path,
) -> dict[str, object]:
    """Create or reuse a package-local successor bound to a real runtime manifest."""

    from src.autoslice.final_media_review_inputs import assess_review_inputs

    package_root = _safe_root(allowed_root, label="package root")
    original_job_path = _contained_regular_file(
        job_path, root=package_root, label="review job"
    )
    original_job = _read_json(original_job_path, label="review job")
    if original_job.get("schema_version") != JOB_SCHEMA_VERSION:
        raise _error(
            "FINAL_MEDIA_REVIEW_RAW_AV_JOB_INVALID",
            f"expected {JOB_SCHEMA_VERSION}",
        )
    capability = load_runtime_raw_av_capability(runtime_root)
    if capability is None:
        return {
            "status": "RUNTIME_RAW_AV_CAPABILITY_ABSENT",
            "cache_reused": True,
            "active_job_path": str(original_job_path),
            "binding_path": None,
            "receipt_path": None,
            "assessment": assess_review_inputs(
                original_job, allowed_root=package_root
            ),
        }
    candidate_id = original_job.get("candidate_id")
    if not isinstance(candidate_id, str) or _ID_RE.fullmatch(candidate_id) is None:
        raise _error(
            "FINAL_MEDIA_REVIEW_RAW_AV_JOB_INVALID", "candidate_id invalid"
        )
    requirements = original_job.get("requirements")
    if not isinstance(requirements, Mapping):
        raise _error(
            "FINAL_MEDIA_REVIEW_RAW_AV_JOB_INVALID", "requirements missing"
        )
    key = _canonical_sha256(
        {
            "job_binding_sha256": _canonical_sha256(original_job),
            "runtime_capability_sha256": capability[
                "runtime_capability_sha256"
            ],
        }
    )
    parent = _safe_capability_parent(package_root, candidate_id)
    target = parent / key[:24]
    if target.exists() or target.is_symlink():
        if target.is_symlink() or not target.is_dir():
            raise _error(
                "FINAL_MEDIA_REVIEW_RAW_AV_TARGET_CONFLICT",
                "deterministic capability target is unsafe",
            )
        return _validate_existing_binding_target(
            target=target,
            original_job_path=original_job_path,
            original_job=original_job,
            capability=capability,
            package_root=package_root,
        )

    stage = Path(tempfile.mkdtemp(prefix=".bind-raw-av-", dir=parent))
    stage.chmod(0o700)
    created = False
    try:
        final_binding = target / "transport-capability.json"
        final_job = target / "review-job.raw-av.json"
        final_receipt = target / "binding-receipt.json"
        binding_doc = package_binding_document(capability)
        binding_bytes = _json_bytes(binding_doc)
        successor_job = {
            **original_job,
            "requirements": {
                **dict(requirements),
                "provider_accepts_bound_source_video": True,
                "provider_accepts_bound_audio": True,
            },
            "transport_capability": _planned_binding(
                final_binding, binding_bytes
            ),
            "transport_capability_lineage": {
                "authority": "RUNTIME_RAW_AV_CAPABILITY_BINDER",
                "original_job_path": str(original_job_path),
                "original_job_sha256": _sha256(original_job_path),
                "original_job_binding_sha256": _canonical_sha256(original_job),
                "runtime_capability_sha256": capability[
                    "runtime_capability_sha256"
                ],
            },
        }
        job_bytes = _json_bytes(successor_job)
        receipt: dict[str, object] = {
            "schema_version": BINDING_RECEIPT_SCHEMA_VERSION,
            "status": "BOUND_RUNTIME_RAW_AV_CAPABILITY",
            "authority": "RUNTIME_RAW_AV_CAPABILITY_BINDER",
            "candidate_id": candidate_id,
            "original_job_path": str(original_job_path),
            "original_job_sha256": _sha256(original_job_path),
            "original_job_binding_sha256": _canonical_sha256(original_job),
            "runtime_capability_sha256": capability[
                "runtime_capability_sha256"
            ],
            "transport_capability": _planned_binding(
                final_binding, binding_bytes
            ),
            "successor_job": _planned_binding(final_job, job_bytes),
            "content_review_status": "UNASSESSED",
            "provider_calls": 0,
            "upload_calls": 0,
        }
        receipt["receipt_sha256"] = _canonical_sha256(receipt)
        _write_private(stage / final_binding.name, binding_bytes)
        _write_private(stage / final_job.name, job_bytes)
        _write_private(stage / final_receipt.name, _json_bytes(receipt))
        directory_fd = os.open(stage, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        os.rename(stage, target)
        stage = Path()
        created = True
    except FileExistsError:
        pass
    finally:
        if stage != Path() and stage.exists():
            shutil.rmtree(stage)

    result = _validate_existing_binding_target(
        target=target,
        original_job_path=original_job_path,
        original_job=original_job,
        capability=capability,
        package_root=package_root,
    )
    result["cache_reused"] = not created
    return result


def _bounded_text(value: str) -> str:
    encoded = value.encode("utf-8", errors="replace")
    return encoded[:_MAX_CAPTURE_BYTES].decode("utf-8", errors="replace")


def _adapter_error(
    envelope: Mapping[str, object] | None,
    *,
    completed: subprocess.CompletedProcess[str],
) -> LlmCallError:
    error = envelope.get("error") if isinstance(envelope, Mapping) else None
    diagnostics: dict[str, object] = {
        "provider_transport": "runtime_content_bound_raw_av",
        "provider_process_returncode": completed.returncode,
    }
    safe_reason = "LLM_COMMAND_FAILED"
    message = "raw-AV adapter did not return an accepted response"
    if isinstance(error, Mapping):
        http_status = error.get("provider_http_status")
        if isinstance(http_status, int) and not isinstance(http_status, bool):
            diagnostics["provider_http_status"] = http_status
        raw_reason = error.get("safe_reason")
        if isinstance(raw_reason, str) and raw_reason:
            diagnostics["provider_error_code"] = raw_reason[:128]
        raw_message = error.get("message")
        if isinstance(raw_message, str) and raw_message:
            message = raw_message[:512]
    if completed.stderr:
        diagnostics["provider_stderr_tail"] = _bounded_text(completed.stderr)[-512:]
    return LlmCallError(
        message,
        safe_reason=safe_reason,
        provider_diagnostics=diagnostics,
    )


def _expanded_argv(
    capability: Mapping[str, object],
    *,
    request_path: Path,
    response_path: Path,
) -> list[str]:
    substitutions = {
        "{executable}": str(capability["executable_path"]),
        "{request_json}": str(request_path),
        "{response_json}": str(response_path),
    }
    return [substitutions.get(token, token) for token in capability["argv"]]


def _raw_audio_binding(assessment: Mapping[str, object]) -> dict[str, object]:
    audio = assessment.get("audio")
    if not isinstance(audio, Mapping) or audio.get("actual_gaps") != []:
        raise _error(
            "FINAL_MEDIA_REVIEW_RAW_AV_INPUT_INVALID",
            "assessment does not have continuous exact audio",
        )
    windows = audio.get("windows")
    rows = (
        [
            row
            for row in windows
            if isinstance(row, Mapping)
            and row.get("kind") == "exact_full_audio_wav"
        ]
        if isinstance(windows, list)
        else []
    )
    if len(rows) != 1:
        raise _error(
            "FINAL_MEDIA_REVIEW_RAW_AV_INPUT_INVALID",
            "exactly one full-media WAV is required",
        )
    row = dict(rows[0])
    path = Path(str(row.get("path") or ""))
    if path.is_symlink() or not path.is_file():
        raise _error(
            "FINAL_MEDIA_REVIEW_RAW_AV_INPUT_INVALID",
            "full-media WAV is unavailable",
        )
    row["bytes"] = path.stat().st_size
    return row


def _validate_adapter_envelope(
    envelope: Mapping[str, object],
    *,
    request_sha256: str,
    capability: Mapping[str, object],
    job: Mapping[str, object],
    assessment: Mapping[str, object],
    raw_audio: Mapping[str, object],
    response_path: Path,
) -> dict[str, object]:
    receipt = envelope.get("transport_receipt")
    result = envelope.get("result")
    clock = assessment["exact_media_clock"]
    manifest = assessment["asset_manifest"]
    if (
        envelope.get("schema_version") != ADAPTER_RESPONSE_SCHEMA_VERSION
        or envelope.get("request_sha256") != request_sha256
        or not isinstance(receipt, Mapping)
        or not isinstance(result, Mapping)
    ):
        raise _error(
            "FINAL_MEDIA_REVIEW_RAW_AV_RESPONSE_INVALID",
            "adapter response envelope is malformed",
        )
    expected_receipt_fields = {
        "schema_version",
        "provider",
        "model",
        "endpoint_family",
        "capability_id",
        "runtime_capability_sha256",
        "executable_sha256",
        "model_capability_seal_receipt_sha256",
        "model_capability_attestation_sha256",
        "model_capability_contract_sha256",
        "model_capability_sentinel_result_sha256",
        "model_capability_sentinel_result_self_sha256",
        "model_capability_sentinel_runner_sha256",
        "request_sha256",
        "candidate_id",
        "source_video_sha256",
        "raw_audio_sha256",
        "consumed_continuous_source_video",
        "consumed_raw_audio",
    }
    if (
        set(receipt) != expected_receipt_fields
        or receipt.get("schema_version") != ADAPTER_RECEIPT_SCHEMA_VERSION
        or receipt.get("provider") != capability["provider"]
        or receipt.get("model") != capability["model"]
        or receipt.get("endpoint_family") != capability["endpoint_family"]
        or receipt.get("capability_id") != capability["capability_id"]
        or receipt.get("runtime_capability_sha256")
        != capability["runtime_capability_sha256"]
        or receipt.get("executable_sha256")
        != capability["executable"]["sha256"]
        or receipt.get("model_capability_seal_receipt_sha256")
        != capability["model_capability"]["seal_receipt_sha256"]
        or receipt.get("model_capability_attestation_sha256")
        != capability["model_capability"]["attestation_sha256"]
        or receipt.get("model_capability_contract_sha256")
        != capability["model_capability"]["contract_sha256"]
        or receipt.get("model_capability_sentinel_result_sha256")
        != capability["model_capability"]["sentinel_result_sha256"]
        or receipt.get("model_capability_sentinel_result_self_sha256")
        != capability["model_capability"]["sentinel_result_self_sha256"]
        or receipt.get("model_capability_sentinel_runner_sha256")
        != capability["model_capability"]["sentinel_runner_sha256"]
        or receipt.get("request_sha256") != request_sha256
        or receipt.get("candidate_id") != job["candidate_id"]
        or receipt.get("source_video_sha256")
        != clock["source_video_sha256"]
        or receipt.get("raw_audio_sha256") != raw_audio["sha256"]
        or receipt.get("consumed_continuous_source_video") is not True
        or receipt.get("consumed_raw_audio") is not True
    ):
        raise _error(
            "FINAL_MEDIA_REVIEW_RAW_AV_RESPONSE_INVALID",
            "adapter receipt does not prove exact raw-AV consumption",
        )
    output = dict(result)
    output["transport_evidence"] = {
        "schema_version": TRANSPORT_EVIDENCE_SCHEMA_VERSION,
        "provider": capability["provider"],
        "model": capability["model"],
        "endpoint_family": capability["endpoint_family"],
        "capability_id": capability["capability_id"],
        "runtime_capability_sha256": capability[
            "runtime_capability_sha256"
        ],
        "executable_sha256": capability["executable"]["sha256"],
        "model_capability_seal_receipt_sha256": capability[
            "model_capability"
        ]["seal_receipt_sha256"],
        "model_capability_attestation_sha256": capability[
            "model_capability"
        ]["attestation_sha256"],
        "model_capability_contract_sha256": capability[
            "model_capability"
        ]["contract_sha256"],
        "model_capability_sentinel_result_sha256": capability[
            "model_capability"
        ]["sentinel_result_sha256"],
        "model_capability_sentinel_result_self_sha256": capability[
            "model_capability"
        ]["sentinel_result_self_sha256"],
        "model_capability_sentinel_runner_sha256": capability[
            "model_capability"
        ]["sentinel_runner_sha256"],
        "request_sha256": request_sha256,
        "response_sha256": _sha256(response_path),
        "source_video_sha256": clock["source_video_sha256"],
        "raw_audio_sha256": raw_audio["sha256"],
        "asset_manifest_sha256": manifest["sha256"],
        "consumed_continuous_source_video": True,
        "consumed_raw_audio": True,
    }
    return output


def build_runtime_raw_av_executor(
    *,
    runtime_root: str | Path,
    package_binding: Mapping[str, object],
    environment: Mapping[str, str] | None = None,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> Callable[..., Mapping[str, object]]:
    """Build one no-shell executor from a validated runtime/package match."""

    root = _safe_root(runtime_root, label="runtime root")
    capability = load_runtime_raw_av_capability(root)
    if capability is None:
        raise _error(
            "FINAL_MEDIA_REVIEW_RAW_AV_CAPABILITY_MISSING",
            "runtime raw-AV capability manifest is absent",
        )
    expected = package_binding_document(capability)
    for key in (
        "capability_id",
        "provider",
        "transport",
        "model",
        "endpoint_family",
        "accepts",
        "runtime_capability_sha256",
        "executable_sha256",
        "command_contract_sha256",
        "model_capability_seal_receipt_file_sha256",
        "model_capability_seal_receipt_sha256",
        "model_capability_attestation_file_sha256",
        "model_capability_attestation_sha256",
        "model_capability_contract_sha256",
        "model_capability_sentinel_result_sha256",
        "model_capability_sentinel_result_self_sha256",
        "model_capability_sentinel_runner_sha256",
        "model_capability_verification_method",
        "model_capability_expires_at",
        "result_schema_version",
    ):
        if package_binding.get(key) != expected.get(key):
            raise _error(
                "FINAL_MEDIA_REVIEW_RAW_AV_CAPABILITY_MISMATCH",
                f"package binding differs from runtime capability at {key}",
            )
    base_environment = dict(os.environ if environment is None else environment)
    # The sealed adapter owns credential loading from the authoritative runtime.
    # Never forward an ambient key that could silently select another account.
    for key in list(base_environment):
        if key.startswith("CPA_"):
            base_environment.pop(key, None)
    base_environment["AUTOSLICE_BASE"] = str(root)
    base_environment["AUTOSLICE_FINAL_REVIEW_RUNTIME_ROOT"] = str(root)

    def executor(
        job: Mapping[str, object],
        assessment: Mapping[str, object],
        _llm_call: Callable[[str], str],
        semantic_command: str,
    ) -> Mapping[str, object]:
        raw_audio = _raw_audio_binding(assessment)
        clock = assessment["exact_media_clock"]
        manifest = assessment["asset_manifest"]
        request = {
            "schema_version": REQUEST_SCHEMA_VERSION,
            "candidate_id": job["candidate_id"],
            "provider": capability["provider"],
            "model": capability["model"],
            "endpoint_family": capability["endpoint_family"],
            "capability_id": capability["capability_id"],
            "runtime_capability_sha256": capability[
                "runtime_capability_sha256"
            ],
            "command_contract_sha256": capability[
                "command_contract_sha256"
            ],
            "model_capability_seal_receipt_sha256": capability[
                "model_capability"
            ]["seal_receipt_sha256"],
            "model_capability_attestation_sha256": capability[
                "model_capability"
            ]["attestation_sha256"],
            "model_capability_contract_sha256": capability[
                "model_capability"
            ]["contract_sha256"],
            "model_capability_sentinel_result_sha256": capability[
                "model_capability"
            ]["sentinel_result_sha256"],
            "model_capability_sentinel_result_self_sha256": capability[
                "model_capability"
            ]["sentinel_result_self_sha256"],
            "model_capability_sentinel_runner_sha256": capability[
                "model_capability"
            ]["sentinel_runner_sha256"],
            "source_video": {
                "path": clock["source_video_path"],
                "sha256": clock["source_video_sha256"],
                "bytes": clock["source_video_bytes"],
                "duration_us": clock["duration_us"],
                "first_video_pts_us": clock.get("first_video_pts_us"),
                "last_video_pts_us": clock.get("last_video_pts_us"),
                "video_frame_count": clock.get("video_frame_count"),
                "coverage": "CONTINUOUS_EXACT_SOURCE_VIDEO",
            },
            "raw_audio": {
                "path": raw_audio["path"],
                "sha256": raw_audio["sha256"],
                "bytes": raw_audio["bytes"],
                "sample_rate": raw_audio.get("sample_rate"),
                "sample_frames": raw_audio.get("sample_frames"),
                "declared_start_us": raw_audio.get("declared_start_us"),
                "declared_end_us": raw_audio.get("declared_end_us"),
                "coverage": "CONTINUOUS_EXACT_FULL_MEDIA_WAV",
            },
            "asset_manifest_sha256": manifest["sha256"],
            "review_plan": job.get("review_plan"),
            "expected_result_schema_version": RESULT_SCHEMA_VERSION,
            "semantic_command_sha256": hashlib.sha256(
                semantic_command.encode("utf-8")
            ).hexdigest(),
        }
        request_bytes = _json_bytes(request)
        request_sha = hashlib.sha256(request_bytes).hexdigest()
        with tempfile.TemporaryDirectory(prefix="final-media-raw-av-") as temporary:
            temporary_root = Path(temporary)
            request_path = temporary_root / "request.json"
            response_path = temporary_root / "response.json"
            _write_private(request_path, request_bytes)
            child_environment = dict(base_environment)
            child_environment["AUTOSLICE_RAW_AV_REQUEST_SHA256"] = request_sha
            argv = _expanded_argv(
                capability,
                request_path=request_path,
                response_path=response_path,
            )
            timeout = int(capability["timeout_seconds"])
            try:
                wait = provider_wait_for_call(float(timeout))
                with runtime_provider_slot(
                    runtime_root=root, timeout_seconds=wait
                ):
                    completed = run(
                        argv,
                        check=False,
                        capture_output=True,
                        text=True,
                        stdin=subprocess.DEVNULL,
                        timeout=timeout,
                        env=child_environment,
                    )
            except ProviderSlotTimeout as exc:
                raise LlmCallError(
                    "raw-AV provider capacity wait timed out",
                    safe_reason="LLM_PROVIDER_CAPACITY_TIMEOUT",
                ) from exc
            except ProviderSlotError as exc:
                raise LlmCallError(
                    "raw-AV provider capacity is unavailable",
                    safe_reason="LLM_PROVIDER_CAPACITY_UNAVAILABLE",
                ) from exc
            except subprocess.TimeoutExpired as exc:
                raise LlmCallError(
                    "raw-AV adapter timed out",
                    safe_reason="LLM_COMMAND_TIMEOUT",
                    provider_diagnostics={
                        "provider_transport": "runtime_content_bound_raw_av"
                    },
                ) from exc
            except OSError as exc:
                raise LlmCallError(
                    "raw-AV adapter could not start",
                    safe_reason="LLM_RUNTIME_CPA_BINDING_REQUIRED",
                ) from exc
            envelope: dict[str, object] | None = None
            if (
                response_path.is_file()
                and not response_path.is_symlink()
                and 0 < response_path.stat().st_size <= _MAX_JSON_BYTES
            ):
                try:
                    parsed = json.loads(response_path.read_text(encoding="utf-8"))
                except (OSError, UnicodeError, json.JSONDecodeError):
                    parsed = None
                if isinstance(parsed, dict):
                    envelope = parsed
            if completed.returncode != 0:
                raise _adapter_error(envelope, completed=completed)
            if envelope is None:
                raise LlmCallError(
                    "raw-AV adapter returned no valid response envelope",
                    safe_reason="LLM_COMMAND_COMPLETION_MISSING",
                    provider_diagnostics={
                        "provider_transport": "runtime_content_bound_raw_av",
                        "provider_error_code": (
                            "FINAL_MEDIA_REVIEW_RAW_AV_RESPONSE_INVALID"
                        ),
                    },
                )
            try:
                return _validate_adapter_envelope(
                    envelope,
                    request_sha256=request_sha,
                    capability=capability,
                    job=job,
                    assessment=assessment,
                    raw_audio=raw_audio,
                    response_path=response_path,
                )
            except FinalMediaReviewRawAvError as exc:
                raise LlmCallError(
                    exc.detail,
                    safe_reason="LLM_COMMAND_FAILED",
                    provider_diagnostics={
                        "provider_transport": "runtime_content_bound_raw_av",
                        "provider_error_code": exc.reason_code,
                    },
                ) from exc

    executor.raw_av_runtime_binding = {
        "adapter": "final_media_review_raw_av.build_runtime_raw_av_executor",
        "provider_transport": "runtime_content_bound_raw_av",
        "provider": capability["provider"],
        "transport": capability["transport"],
        "accepts": dict(capability["accepts"]),
        "result_schema_version": capability["result_schema_version"],
        "model": capability["model"],
        "endpoint_family": capability["endpoint_family"],
        "capability_id": capability["capability_id"],
        "runtime_capability_sha256": capability[
            "runtime_capability_sha256"
        ],
        "executable_sha256": capability["executable"]["sha256"],
        "command_contract_sha256": capability["command_contract_sha256"],
        "model_capability_seal_receipt_sha256": capability[
            "model_capability"
        ]["seal_receipt_sha256"],
        "model_capability_attestation_sha256": capability[
            "model_capability"
        ]["attestation_sha256"],
        "model_capability_contract_sha256": capability[
            "model_capability"
        ]["contract_sha256"],
        "model_capability_sentinel_result_sha256": capability[
            "model_capability"
        ]["sentinel_result_sha256"],
        "model_capability_sentinel_result_self_sha256": capability[
            "model_capability"
        ]["sentinel_result_self_sha256"],
        "model_capability_sentinel_runner_sha256": capability[
            "model_capability"
        ]["sentinel_runner_sha256"],
        "model_capability_expires_at": capability["model_capability"][
            "expires_at"
        ],
    }
    return executor


__all__ = [
    "ADAPTER_RECEIPT_SCHEMA_VERSION",
    "ADAPTER_RESPONSE_SCHEMA_VERSION",
    "BINDING_RECEIPT_SCHEMA_VERSION",
    "FinalMediaReviewRawAvError",
    "PACKAGE_BINDING_SCHEMA_VERSION",
    "REQUEST_SCHEMA_VERSION",
    "RUNTIME_CAPABILITY_FILENAME",
    "RUNTIME_CAPABILITY_SCHEMA_VERSION",
    "TRANSPORT_EVIDENCE_SCHEMA_VERSION",
    "bind_review_job_to_runtime_capability",
    "build_runtime_raw_av_executor",
    "load_runtime_raw_av_capability",
    "package_binding_document",
    "validate_package_transport_binding",
]
