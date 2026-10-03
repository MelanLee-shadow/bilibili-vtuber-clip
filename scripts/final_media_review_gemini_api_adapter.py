#!/usr/bin/env python3
"""Hash-bound Gemini Files API adapter for final-media sentinel and review.

The adapter never infers capability from a model name. Sentinel mode uploads
one hidden exact audio/video challenge and accepts only the observed eight-digit
token. Final-review mode uploads the exact final MP4 and exact full-media WAV
in two independent calls and intersects their strict verdicts fail-closed.

Only configured free Gemini API keys are considered. The paid backup key is
never read or spent by this adapter.
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

from src.autoslice.final_media_review_gemini_api_runtime import (
    ENDPOINT_FAMILY,
    MODEL_ENV,
    PROVIDER,
    VIDEO_FPS,
)
from src.autoslice.gemini_file_api import (
    GeminiFileApiError,
    GeminiFileCallOutcome,
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
GEMINI_FILE_HELPER_SHA256 = "7b19bbd4fd24bdf7dd0913a3a685318d23377b619bd3d7e96d2f6dfcde73344f"
_REQUEST_TIMEOUT_ENV = "AUTOSLICE_FINAL_MEDIA_GEMINI_API_TIMEOUT_SECONDS"
_POLL_TIMEOUT_ENV = "AUTOSLICE_FINAL_MEDIA_GEMINI_API_POLL_TIMEOUT_SECONDS"
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
_MAX_JSON_BYTES = 8_000_000
_MAX_RESPONSE_BYTES = 2_000_000
_MAX_MEDIA_BYTES = 2_000_000_000


class AdapterError(ValueError):
    """A typed direct API adapter rejection."""

    def __init__(
        self,
        safe_reason: str,
        message: str,
        *,
        provider_http_status: int | None = None,
        exit_code: int = 2,
        ambiguous: bool = False,
    ):
        super().__init__(message)
        self.safe_reason = safe_reason
        self.message = message
        self.provider_http_status = provider_http_status
        self.exit_code = exit_code
        self.ambiguous = ambiguous


@dataclass(frozen=True)
class ApiConfig:
    model: str
    video_fps: float
    timeout_seconds: float
    poll_timeout_seconds: float


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
        raise AdapterError(
            "GEMINI_API_INPUT_INVALID", f"{label} unavailable"
        ) from exc
    if (
        path.is_symlink()
        or not stat.S_ISREG(info.st_mode)
        or not resolved.is_file()
        or info.st_nlink != 1
    ):
        raise AdapterError(
            "GEMINI_API_INPUT_INVALID",
            f"{label} must be a regular single-link non-symlink file",
        )
    size = resolved.stat().st_size
    if size <= 0 or (maximum is not None and size > maximum):
        raise AdapterError(
            "GEMINI_API_INPUT_INVALID",
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
            "GEMINI_API_RUNTIME_INVALID", "final-media runtime root is missing"
        )
    path = Path(raw).expanduser()
    try:
        info = path.lstat()
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise AdapterError(
            "GEMINI_API_RUNTIME_INVALID", "final-media runtime root unavailable"
        ) from exc
    if path.is_symlink() or not stat.S_ISDIR(info.st_mode) or not resolved.is_dir():
        raise AdapterError(
            "GEMINI_API_RUNTIME_INVALID", "final-media runtime root is unsafe"
        )
    return resolved


def _read_json(path: Path, *, label: str) -> dict[str, object]:
    target = _regular_file(path, label=label, maximum=_MAX_JSON_BYTES)
    try:
        value = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise AdapterError(
            "GEMINI_API_INPUT_INVALID", f"{label} is not valid JSON"
        ) from exc
    if not isinstance(value, dict):
        raise AdapterError(
            "GEMINI_API_INPUT_INVALID", f"{label} root must be an object"
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
            "GEMINI_API_RESPONSE_INVALID", "response cannot be encoded"
        ) from exc
    if len(payload) > _MAX_JSON_BYTES:
        raise AdapterError(
            "GEMINI_API_RESPONSE_INVALID", "response exceeds size limit"
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
            "GEMINI_API_RESPONSE_CONFLICT", "response path already exists"
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
        raise AdapterError(
            "GEMINI_API_INPUT_INVALID", f"{label} SHA256 missing"
        )
    normalized = value.strip().lower().removeprefix("sha256:")
    if _SHA_RE.fullmatch(normalized) is None:
        raise AdapterError(
            "GEMINI_API_INPUT_INVALID", f"{label} SHA256 invalid"
        )
    return normalized


def _api_config(environment: Mapping[str, str]) -> ApiConfig:
    model = str(environment.get(MODEL_ENV) or "").strip()
    if _ID_RE.fullmatch(model) is None:
        raise AdapterError(
            "GEMINI_API_CONFIG_MISSING",
            f"{MODEL_ENV} must name the exact runtime model",
        )
    try:
        timeout = float(environment.get(_REQUEST_TIMEOUT_ENV) or "900")
        poll_timeout = float(environment.get(_POLL_TIMEOUT_ENV) or "900")
    except ValueError as exc:
        raise AdapterError(
            "GEMINI_API_CONFIG_INVALID", "Gemini API timeout is invalid"
        ) from exc
    if not 1 <= timeout <= 3600 or not 1 <= poll_timeout <= 3600:
        raise AdapterError(
            "GEMINI_API_CONFIG_INVALID", "Gemini API timeout is outside range"
        )
    return ApiConfig(
        model=model,
        video_fps=VIDEO_FPS,
        timeout_seconds=timeout,
        poll_timeout_seconds=poll_timeout,
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
            "GEMINI_API_RUNTIME_MISMATCH",
            "runtime capability identity differs from request",
        )
    raw_adapter = (
        manifest.get("adapter")
        if manifest_name == SENTINEL_RUNTIME_FILENAME
        else manifest.get("executable")
    )
    if not isinstance(raw_adapter, Mapping):
        raise AdapterError(
            "GEMINI_API_RUNTIME_INVALID", "runtime adapter binding missing"
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
            "GEMINI_API_RUNTIME_INVALID", "runtime adapter path invalid"
        )
    bound = _regular_file(root / relative, label="runtime adapter")
    actual = adapter_path.resolve(strict=True)
    if bound != actual or _sha256(bound) != expected_sha:
        raise AdapterError(
            "GEMINI_API_RUNTIME_MISMATCH", "runtime adapter bytes differ"
        )
    return manifest


def _file_runner() -> Callable[..., GeminiFileCallOutcome]:
    helper = _regular_file(
        REPO_ROOT / "src/autoslice/gemini_file_api.py",
        label="Gemini Files API helper",
        maximum=2_000_000,
    )
    if _sha256(helper) != GEMINI_FILE_HELPER_SHA256:
        raise AdapterError(
            "GEMINI_API_HELPER_DRIFT",
            "Gemini Files API helper differs from adapter-bound source",
        )
    from src.autoslice.gemini_file_api import run_free_key_file_call

    return run_free_key_file_call


def _evidence_directory(
    root: Path,
    *,
    request_sha256: str,
    identity: str,
) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "-", identity).strip("-.") or "request"
    parent = root / "final-media-review-gemini-api-runs"
    parent.mkdir(mode=0o700, exist_ok=True)
    parent.chmod(0o700)
    request_root = parent / f"{safe[:80]}-{request_sha256[:24]}"
    if request_root.exists() or request_root.is_symlink():
        if request_root.is_symlink() or not request_root.is_dir():
            raise AdapterError(
                "GEMINI_API_EVIDENCE_PATH_INVALID",
                "provider evidence request directory is unsafe",
            )
    else:
        request_root.mkdir(mode=0o700)
    for attempt in range(1, 1000):
        target = request_root / f"attempt-{attempt:03d}"
        try:
            target.mkdir(mode=0o700)
        except FileExistsError:
            continue
        return target.resolve(strict=True)
    raise AdapterError(
        "GEMINI_API_EVIDENCE_EXHAUSTED",
        "provider evidence attempt namespace is exhausted",
    )


def _write_text(path: Path, text: str) -> None:
    payload = text.encode("utf-8")
    if not payload or len(payload) > _MAX_RESPONSE_BYTES:
        raise AdapterError(
            "GEMINI_API_INPUT_INVALID", "provider prompt size outside range"
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


def _provider_error(exc: GeminiFileApiError) -> AdapterError:
    return AdapterError(
        exc.reason_code,
        exc.detail[:1000],
        provider_http_status=exc.http_status,
        exit_code=75 if exc.http_status == 429 else 2,
        ambiguous=exc.ambiguous,
    )


def _provider_call(
    *,
    media: Path,
    media_sha256: str,
    media_bytes: int,
    mime_type: str,
    prompt: str,
    config: ApiConfig,
    environment: Mapping[str, str],
    runner: Callable[..., GeminiFileCallOutcome],
) -> GeminiFileCallOutcome:
    try:
        free_key_environment = {
            name: value
            for name in ("GEMINI_API_KEY", "GEMINI_API_KEY_2", "GEMINI_API_KEY_3")
            if isinstance((value := environment.get(name)), str) and value
        }
        outcome = runner(
            media_path=media,
            expected_sha256=media_sha256,
            expected_bytes=media_bytes,
            mime_type=mime_type,
            prompt=prompt,
            model=config.model,
            video_fps=(config.video_fps if mime_type.startswith("video/") else None),
            environment=free_key_environment,
            timeout_seconds=config.timeout_seconds,
            poll_timeout_seconds=config.poll_timeout_seconds,
        )
    except GeminiFileApiError as exc:
        raise _provider_error(exc) from exc
    if not isinstance(outcome, GeminiFileCallOutcome):
        raise AdapterError(
            "GEMINI_API_RESPONSE_INVALID", "provider returned no typed outcome"
        )
    if (
        outcome.evidence.get("provider") != PROVIDER
        or outcome.evidence.get("model") != config.model
        or outcome.evidence.get("media_sha256") != media_sha256
        or outcome.evidence.get("media_bytes") != media_bytes
        or outcome.evidence.get("mime_type") != mime_type
        or outcome.evidence.get("video_fps")
        != (config.video_fps if mime_type.startswith("video/") else None)
        or outcome.evidence.get("paid_backup_used") is not False
    ):
        raise AdapterError(
            "GEMINI_API_RESPONSE_INVALID",
            "provider evidence does not bind exact free-key media consumption",
        )
    return outcome

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
        or request.get("endpoint_family") != ENDPOINT_FAMILY
        or not isinstance(request.get("challenge_id"), str)
        or not isinstance(request.get("model"), str)
        or request.get("answer_disclosed_to_adapter") is not False
        or _sha256(input_path)
        != _normalized_sha(request.get("input_sha256"), label="sentinel input")
        or _sha256(prompt_path)
        != _normalized_sha(request.get("prompt_sha256"), label="sentinel prompt")
    ):
        raise AdapterError(
            "GEMINI_API_SENTINEL_REQUEST_INVALID",
            "sentinel request or exact media binding is invalid",
        )
    return request, request_sha


def _sentinel_mime(coverage: object) -> str:
    if coverage == "RAW_AUDIO_ONLY_HIDDEN_NONCE":
        return "audio/wav"
    if coverage == "CONTINUOUS_SOURCE_VIDEO_HIDDEN_SEQUENCE":
        return "video/mp4"
    raise AdapterError(
        "GEMINI_API_SENTINEL_REQUEST_INVALID",
        "sentinel coverage is not an approved hidden modality",
    )


def _sentinel_response(
    *,
    request: Mapping[str, object],
    request_sha: str,
    raw: str,
) -> dict[str, object]:
    observed = raw.strip()
    if re.fullmatch(r"[0-9]{8}", observed) is None:
        raise AdapterError(
            "GEMINI_API_SENTINEL_RESPONSE_INVALID",
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
    file_runner: Callable[..., GeminiFileCallOutcome],
    adapter_path: Path,
) -> dict[str, object]:
    root = _safe_runtime_root(environment)
    media = _regular_file(input_path, label="sentinel media", maximum=_MAX_MEDIA_BYTES)
    prompt_path = _regular_file(
        prompt_path, label="sentinel prompt", maximum=1_000_000
    )
    request, request_sha = _sentinel_request(
        request_path, input_path=media, prompt_path=prompt_path
    )
    _verify_adapter_identity(
        root=root,
        manifest_name=SENTINEL_RUNTIME_FILENAME,
        adapter_path=adapter_path,
        provider=str(request["provider"]),
        model=str(request["model"]),
        endpoint_family=str(request["endpoint_family"]),
    )
    config = _api_config(environment)
    if request["model"] != config.model:
        raise AdapterError(
            "GEMINI_API_RUNTIME_MISMATCH",
            "sentinel model identity differs from explicit runtime model",
        )
    try:
        prompt = prompt_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise AdapterError(
            "GEMINI_API_INPUT_INVALID", "sentinel prompt is not UTF-8"
        ) from exc
    evidence_root = _evidence_directory(
        root,
        request_sha256=request_sha,
        identity=str(request["challenge_id"]),
    )
    outcome = _provider_call(
        media=media,
        media_sha256=_sha256(media),
        media_bytes=media.stat().st_size,
        mime_type=_sentinel_mime(request["coverage"]),
        prompt=prompt,
        config=config,
        environment=environment,
        runner=file_runner,
    )
    response = _sentinel_response(
        request=request, request_sha=request_sha, raw=outcome.text
    )
    _write_create_only(response_path, response)
    _write_create_only(
        evidence_root / "adapter-evidence.json",
        {
            "schema_version": "final-media-review-gemini-api-evidence.v1",
            "mode": "sentinel",
            "request_sha256": request_sha,
            "challenge_id": request["challenge_id"],
            "input_sha256": _sha256(media),
            "prompt_sha256": _sha256(prompt_path),
            "provider_evidence": dict(outcome.evidence),
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
                "GEMINI_API_RESPONSE_INVALID", f"{modality} response fence invalid"
            )
        stripped = match.group(1)
    try:
        value = json.loads(stripped)
    except json.JSONDecodeError as exc:
        raise AdapterError(
            "GEMINI_API_RESPONSE_INVALID", f"{modality} response is not JSON"
        ) from exc
    if not isinstance(value, dict) or set(value) != {"status", "observations"}:
        raise AdapterError(
            "GEMINI_API_RESPONSE_INVALID",
            f"{modality} response fields invalid",
        )
    status = value.get("status")
    observations = value.get("observations")
    if status not in {"PASS", "BLOCK"} or not isinstance(observations, list):
        raise AdapterError(
            "GEMINI_API_RESPONSE_INVALID",
            f"{modality} status or observations invalid",
        )
    if not 1 <= len(observations) <= 100:
        raise AdapterError(
            "GEMINI_API_RESPONSE_INVALID",
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
                "GEMINI_API_RESPONSE_INVALID",
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
                "GEMINI_API_RESPONSE_INVALID",
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
            "GEMINI_API_RESPONSE_INVALID",
            f"{modality} status contradicts observations",
        )
    return {"status": status, "observations": normalized}


def _review_prompt(request: Mapping[str, object], *, modality: str) -> str:
    plan = json.dumps(
        request.get("review_plan"),
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    )
    medium = (
        f"continuous exact final MP4 from 0 seconds through EOS, requested in "
        f"static {VIDEO_FPS:g} FPS processing ({1000 / VIDEO_FPS:g} ms cadence). "
        "Inspect the available video stream, scene transitions, burned subtitle "
        "intervals, subtitle-free intervals, opening and ending. BLOCK if any "
        "required event could fall between sampled frames or text/transition detail "
        "cannot be resolved. The independent WAV reviewer owns the full-audio "
        "verdict."
        if modality == "continuous_source_video"
        else "exact full-media WAV from 0 seconds through EOS. Listen across the "
        "complete duration, including every review-plan interval and all gaps. "
        "Judge spoken wording, speaker/source attribution, abrupt cuts, silence "
        "and ending completeness. Do not infer pixels from this audio."
    )
    source = request.get("source_video")
    audio = request.get("raw_audio")
    return f"""You are one fail-closed final-media reviewer. Review the attached
{medium}

Candidate: {request.get('candidate_id')}
Exact source video SHA256: {source.get('sha256') if isinstance(source, Mapping) else None}
Exact full audio SHA256: {audio.get('sha256') if isinstance(audio, Mapping) else None}

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
            "GEMINI_API_REQUEST_BINDING_MISMATCH",
            "raw-AV request differs from executor binding",
        )
    request = _read_json(target, label="raw-AV request")
    if (
        request.get("schema_version") != RAW_AV_REQUEST_SCHEMA
        or request.get("provider") != PROVIDER
        or request.get("endpoint_family") != ENDPOINT_FAMILY
        or not isinstance(request.get("candidate_id"), str)
        or not isinstance(request.get("model"), str)
        or not isinstance(request.get("capability_id"), str)
        or request.get("expected_result_schema_version") != RESULT_SCHEMA
    ):
        raise AdapterError(
            "GEMINI_API_REQUEST_INVALID", "raw-AV request identity invalid"
        )
    source = request.get("source_video")
    audio = request.get("raw_audio")
    if not isinstance(source, Mapping) or not isinstance(audio, Mapping):
        raise AdapterError(
            "GEMINI_API_REQUEST_INVALID", "raw-AV media bindings missing"
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
            "GEMINI_API_REQUEST_BINDING_MISMATCH",
            "raw-AV media bytes differ from request",
        )
    return request, request_sha, video, wav


def run_final_review(
    *,
    request_path: Path,
    response_path: Path,
    environment: Mapping[str, str],
    file_runner: Callable[..., GeminiFileCallOutcome],
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
    config = _api_config(environment)
    if request["model"] != config.model:
        raise AdapterError(
            "GEMINI_API_RUNTIME_MISMATCH",
            "review model identity differs from explicit runtime model",
        )
    evidence_root = _evidence_directory(
        root,
        request_sha256=request_sha,
        identity=str(request["candidate_id"]),
    )
    video_prompt = _review_prompt(request, modality="continuous_source_video")
    audio_prompt = _review_prompt(request, modality="raw_audio")
    _write_text(evidence_root / "video-prompt.txt", video_prompt)
    _write_text(evidence_root / "audio-prompt.txt", audio_prompt)
    source = request["source_video"]
    audio = request["raw_audio"]
    assert isinstance(source, Mapping) and isinstance(audio, Mapping)
    video_outcome = _provider_call(
        media=video,
        media_sha256=str(source["sha256"]),
        media_bytes=int(source["bytes"]),
        mime_type="video/mp4",
        prompt=video_prompt,
        config=config,
        environment=environment,
        runner=file_runner,
    )
    audio_outcome = _provider_call(
        media=wav,
        media_sha256=str(audio["sha256"]),
        media_bytes=int(audio["bytes"]),
        mime_type="audio/wav",
        prompt=audio_prompt,
        config=config,
        environment=environment,
        runner=file_runner,
    )
    video_review = _strict_json_object(
        video_outcome.text, modality="continuous_source_video"
    )
    audio_review = _strict_json_object(
        audio_outcome.text, modality="raw_audio"
    )
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
        review_rows = review["observations"]
        assert isinstance(review_rows, list)
        for row in review_rows:
            assert isinstance(row, Mapping)
            observations.append({"modality": modality, **dict(row)})
    evidence_document = {
        "schema_version": "final-media-review-gemini-api-evidence.v1",
        "mode": "final_review",
        "request_sha256": request_sha,
        "candidate_id": request["candidate_id"],
        "source_video_sha256": source["sha256"],
        "raw_audio_sha256": audio["sha256"],
        "video_processing": {"mode": "static", "fps": config.video_fps},
        "video_provider_evidence": dict(video_outcome.evidence),
        "audio_provider_evidence": dict(audio_outcome.evidence),
        "video_status": video_review["status"],
        "audio_status": audio_review["status"],
        "combined_status": status,
    }
    evidence_path = evidence_root / "adapter-evidence.json"
    _write_create_only(evidence_path, evidence_document)
    observations.append(
        {
            "modality": "transport",
            "point_id": "gemini-api-provider-evidence",
            "verdict": "PASS",
            "detail": (
                "Two independent Gemini Files API turns consumed the exact bound "
                f"MP4 at static {config.video_fps:g} FPS and exact bound full-media "
                "WAV using free keys only; durable provider evidence is SHA256 "
                f"{_sha256(evidence_path)}."
            ),
            "provider_evidence": _binding(evidence_path),
        }
    )
    result = {
        "schema_version": RESULT_SCHEMA,
        "candidate_id": request["candidate_id"],
        "source_video_sha256": source["sha256"],
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
        "source_video_sha256": source["sha256"],
        "raw_audio_sha256": audio["sha256"],
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
    schema = SENTINEL_ERROR_SCHEMA if sentinel else RAW_AV_RESPONSE_SCHEMA
    return {
        "schema_version": schema,
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
                "GEMINI_API_INPUT_INVALID",
                "sentinel mode requires both --input and --prompt",
            )
        runner = _file_runner()
        if sentinel:
            run_sentinel(
                request_path=args.request,
                response_path=args.response,
                input_path=args.input,
                prompt_path=args.prompt,
                environment=os.environ,
                file_runner=runner,
                adapter_path=Path(__file__).resolve(),
            )
        else:
            run_final_review(
                request_path=args.request,
                response_path=args.response,
                environment=os.environ,
                file_runner=runner,
                adapter_path=Path(__file__).resolve(),
            )
        return 0
    except AdapterError as exc:
        if not exc.ambiguous:
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
