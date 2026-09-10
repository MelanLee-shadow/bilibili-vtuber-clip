"""CPA image-edit transport and bounded evidence; never billing/model authority."""

from __future__ import annotations

import base64
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import time
import urllib.error
import urllib.request
from typing import Callable, Mapping, Sequence


_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_COUNTS = {"input_tokens", "output_tokens", "total_tokens"}
_DETAILS = {
    "input_tokens_details": {"text_tokens", "image_tokens", "cached_tokens"},
    "output_tokens_details": {"text_tokens", "image_tokens"},
}


def _safe_token(value: object) -> str | None:
    return value if isinstance(value, str) and _TOKEN.fullmatch(value) else None


def _usage_observation(value: object) -> tuple[dict | None, str]:
    """Never substitute zero, estimate money, or serialize arbitrary reply fields."""
    if value is None:
        return None, "NOT_REPORTED"
    if not isinstance(value, Mapping):
        return None, "REPORTED_INVALID"
    result: dict[str, object] = {}
    for key in _COUNTS:
        if key not in value:
            continue
        count = value[key]
        if type(count) is not int or count < 0:
            return None, "REPORTED_INVALID"
        result[key] = count
    for key, fields in _DETAILS.items():
        if key not in value:
            continue
        detail = value[key]
        if not isinstance(detail, Mapping):
            return None, "REPORTED_INVALID"
        clean = {}
        for field in fields:
            if field not in detail:
                continue
            count = detail[field]
            if type(count) is not int or count < 0:
                return None, "REPORTED_INVALID"
            clean[field] = count
        result[key] = clean
    if not result:
        return None, "REPORTED_INVALID"
    if not _COUNTS.issubset(result):
        return result, "REPORTED_PARTIAL"
    if result["input_tokens"] + result["output_tokens"] != result["total_tokens"]:
        return result, "REPORTED_INCONSISTENT"
    return result, "REPORTED_VALID"


def service_observation(
    payload: Mapping[str, object],
    *,
    requested_model: str,
    requested_size: str,
    headers: object,
    request_elapsed_seconds: float,
) -> dict[str, object]:
    """The service may omit/lie about model and usage; record that distinction."""
    usage, status = _usage_observation(payload.get("usage"))
    get_header = getattr(headers, "get", None)
    request_id = _safe_token(get_header("x-request-id")) if callable(get_header) else None
    quality = payload.get("quality")
    size = payload.get("size")
    output_format = payload.get("output_format")
    elapsed = request_elapsed_seconds
    return {
        "schema_version": "image-edit-service-observation.v1",
        "requested_model": requested_model,
        "requested_parameters": {"model": requested_model, "size": requested_size},
        "reported_model": _safe_token(payload.get("model")),
        "upstream_identity_verified": False,
        "request_id": request_id,
        "reported_quality": quality
        if quality in ("low", "medium", "high", "xhigh", "max", "auto")
        else None,
        "reported_size": size
        if isinstance(size, str) and re.fullmatch(r"[1-9][0-9]{0,4}x[1-9][0-9]{0,4}", size)
        else None,
        "reported_output_format": output_format
        if output_format in ("png", "jpeg", "webp")
        else None,
        "request_elapsed_seconds": round(elapsed, 6)
        if math.isfinite(elapsed) and elapsed >= 0
        else None,
        "usage": usage,
        "usage_status": status,
        "cost_usd": None,
        "cost_status": "NOT_MEASURED",
    }


def preserve_response_image(path: Path, response_path: Path) -> dict[str, object]:
    """Preserve exact returned bytes before the existing normalization mutates them."""
    data = path.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    raw_path = response_path.with_name(f"{response_path.stem}.raw-image-{digest}.bin")
    try:
        fd = os.open(raw_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        info = raw_path.lstat()
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or stat.S_IMODE(info.st_mode) != 0o600
            or raw_path.is_symlink()
            or raw_path.read_bytes() != data
        ):
            raise ValueError("raw image evidence path conflict") from None
    else:
        with os.fdopen(fd, "wb") as out:
            out.write(data)
            out.flush()
            os.fsync(out.fileno())
    dimensions, image_format = None, None
    try:
        from PIL import Image

        with Image.open(raw_path) as image:
            dimensions = list(image.size)
            image_format = image.format
    except (OSError, ValueError):
        # The existing real canvas normalizer still rejects broken images.
        # Keeping invalid bytes aids diagnosis, but never makes them acceptable.
        pass
    return {
        "path": str(raw_path),
        "sha256": "sha256:" + digest,
        "bytes": len(data),
        "dimensions": dimensions,
        "format": image_format,
    }


def perform_image_edit(
    *,
    base_url: str,
    api_key: str,
    reference_path: Path,
    output_path: Path,
    prompt: str,
    request_path: Path,
    response_path: Path,
    timeout_seconds: float,
    model_candidates: Sequence[str],
    request_size: str,
    normalize_canvas: Callable[[Path], tuple[int, int]],
    make_body: Callable,
    file_sha256: Callable[[Path], str],
    explicit_model_unavailable: Callable[[int, str], bool],
) -> dict[str, object]:
    """Use caller's unchanged model selection, canvas policy and provider slot."""
    endpoint = f"{base_url}/images/edits"
    candidates = tuple(
        dict.fromkeys(
            model.strip() for model in model_candidates if isinstance(model, str) and model.strip()
        )
    )
    if not candidates:
        return {
            "status": "FAILED",
            "reason_code": "CPA_IMAGE_EDIT_MODEL_CONFIG_INVALID",
            "detail": "no CPA image model candidate configured",
            "attempted_models": [],
        }
    request_attempts: list[dict[str, object]] = []
    response_attempts: list[dict[str, object]] = []
    reference_sha256 = "sha256:" + file_sha256(reference_path)
    reference_bytes = reference_path.read_bytes()
    request_path.parent.mkdir(parents=True, exist_ok=True)
    response_path.parent.mkdir(parents=True, exist_ok=True)

    def persist_evidence() -> None:
        request_path.write_text(
            json.dumps(
                {
                    "endpoint": endpoint,
                    "method": "images.edit",
                    "image_gen_model": "cpa",
                    "prompt": prompt,
                    "reference_image": str(reference_path),
                    "reference_sha256": reference_sha256,
                    "api_key": "<redacted>",
                    "attempts": request_attempts,
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        response_path.write_text(
            json.dumps(
                {"attempts": response_attempts},
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )

    attempted_models: list[str] = []
    for attempt_index, model in enumerate(candidates):
        attempted_models.append(model)
        request_attempts.append(
            {"attempt": attempt_index + 1, "model": model, "size": request_size}
        )
        persist_evidence()
        attempt_started = time.monotonic()
        response_headers = None
        try:
            body, content_type = make_body(
                fields={"model": model, "prompt": prompt, "size": request_size},
                files={"image": (reference_path.name, reference_bytes, "image/png")},
            )
            request = urllib.request.Request(
                endpoint,
                data=body,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": content_type,
                    # the CPA endpoint sits behind Cloudflare, which 403s the
                    # default Python-urllib user agent
                    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
                },
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
                status_code = response.status
                response_headers = getattr(response, "headers", None)
                raw_bytes = response.read()
                raw = raw_bytes.decode("utf-8", errors="replace")
                request_elapsed = time.monotonic() - attempt_started
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            unavailable = explicit_model_unavailable(exc.code, raw)
            response_attempts.append(
                {
                    "attempt": attempt_index + 1,
                    "model": model,
                    "status_code": exc.code,
                    "body_tail": raw[-4000:],
                    "explicit_model_unavailable": unavailable,
                    "attempt_elapsed_seconds": round(time.monotonic() - attempt_started, 6),
                }
            )
            persist_evidence()
            if unavailable and attempt_index + 1 < len(candidates):
                continue
            return {
                "status": "FAILED",
                "reason_code": "CPA_IMAGE_EDIT_HTTP_ERROR",
                "detail": f"HTTP {exc.code}: {raw[-500:]}",
                "attempted_models": attempted_models,
                "model_fallback_used": len(attempted_models) > 1,
            }
        except Exception as exc:
            response_attempts.append(
                {
                    "attempt": attempt_index + 1,
                    "model": model,
                    "error": type(exc).__name__,
                    "message": str(exc),
                    "attempt_elapsed_seconds": round(time.monotonic() - attempt_started, 6),
                }
            )
            persist_evidence()
            return {
                "status": "FAILED",
                "reason_code": "CPA_IMAGE_EDIT_REQUEST_FAILED",
                "detail": f"{type(exc).__name__}: {exc}",
                "attempted_models": attempted_models,
                "model_fallback_used": len(attempted_models) > 1,
            }

        try:
            payload = json.loads(raw)
            if not isinstance(payload, Mapping):
                raise ValueError("image response root is not an object")
        except (json.JSONDecodeError, ValueError):
            response_attempts.append(
                {
                    "attempt": attempt_index + 1,
                    "model": model,
                    "status_code": status_code,
                    "body_tail": raw[-4000:],
                    "attempt_elapsed_seconds": round(time.monotonic() - attempt_started, 6),
                }
            )
            persist_evidence()
            return {
                "status": "FAILED",
                "reason_code": "CPA_IMAGE_EDIT_BAD_JSON",
                "detail": raw[-500:],
                "attempted_models": attempted_models,
                "model_fallback_used": len(attempted_models) > 1,
            }
        image_record = (
            (payload.get("data") or [{}])[0] if isinstance(payload.get("data"), list) else {}
        )
        if not isinstance(image_record, Mapping):
            image_record = {}
        redacted_response: dict[str, object] = {
            "attempt": attempt_index + 1,
            "model": model,
            "status_code": status_code,
            "keys": sorted(payload.keys()),
            "data_keys": sorted(image_record.keys()),
            "response_body_sha256": "sha256:" + hashlib.sha256(raw_bytes).hexdigest(),
            "service_observation": service_observation(
                payload,
                requested_model=model,
                requested_size=request_size,
                headers=response_headers,
                request_elapsed_seconds=request_elapsed,
            ),
        }
        try:
            b64_json = image_record.get("b64_json")
            image_url = image_record.get("url")
            if isinstance(b64_json, str) and b64_json:
                output_path.parent.mkdir(parents=True, exist_ok=True)
                output_path.write_bytes(base64.b64decode(b64_json))
                redacted_response["b64_json_bytes"] = len(b64_json)
            elif isinstance(image_url, str) and image_url:
                with urllib.request.urlopen(image_url, timeout=timeout_seconds) as image_response:
                    output_path.parent.mkdir(parents=True, exist_ok=True)
                    output_path.write_bytes(image_response.read())
                redacted_response["url"] = image_url
            else:
                redacted_response["attempt_elapsed_seconds"] = round(
                    time.monotonic() - attempt_started, 6
                )
                response_attempts.append(redacted_response)
                persist_evidence()
                return {
                    "status": "FAILED",
                    "reason_code": "CPA_IMAGE_EDIT_NO_IMAGE",
                    "detail": "response had no b64_json/url image",
                    "attempted_models": attempted_models,
                    "model_fallback_used": len(attempted_models) > 1,
                }
            redacted_response["raw_image"] = preserve_response_image(output_path, response_path)
            redacted_response["canvas"] = list(normalize_canvas(output_path))
        except Exception as exc:  # noqa: BLE001 — broken images must block
            redacted_response["attempt_elapsed_seconds"] = round(
                time.monotonic() - attempt_started, 6
            )
            response_attempts.append(redacted_response)
            persist_evidence()
            return {
                "status": "FAILED",
                "reason_code": "CPA_IMAGE_EDIT_BAD_IMAGE",
                "detail": f"{type(exc).__name__}: {exc}",
                "attempted_models": attempted_models,
                "model_fallback_used": len(attempted_models) > 1,
            }
        redacted_response["attempt_elapsed_seconds"] = round(time.monotonic() - attempt_started, 6)
        redacted_response["output_path"] = str(output_path)
        redacted_response["output_sha256"] = "sha256:" + file_sha256(output_path)
        response_attempts.append(redacted_response)
        persist_evidence()
        return {
            "status": "AI_BACKGROUND_READY",
            "output_path": str(output_path),
            "selected_model": model,
            "attempted_models": attempted_models,
            "model_fallback_used": len(attempted_models) > 1,
        }

    raise AssertionError("CPA image model loop exhausted without a result")
