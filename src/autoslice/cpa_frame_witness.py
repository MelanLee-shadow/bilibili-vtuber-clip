"""Hash-bound CPA visual evidence over the Responses API.

CPA accepts ``input_image`` on the Responses route.  Visual receipts are
evidence inputs, not self-authorizing edits: callers still apply their own
closed verdict contract and bind it to the exact source pixels.
"""

from __future__ import annotations

import base64
import hashlib
import json
import subprocess
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

from src.autoslice.cpa_model_ladder import TOP_CPA_MODEL
from src.autoslice.completed_review_checkpoint import CompletedReviewCheckpointError
from src.autoslice.provider_slots import ProviderSlotTimeout, provider_wait_for_call, runtime_provider_slot
from src.autoslice.cpa_runtime import observed_cpa_request, observed_model_request
from src.autoslice.structured_output import output_contract, openai_format, contract_receipt
from src.autoslice.evidence_identity import sha256_hex


SCHEMA_VERSION = "cpa-frame-witness.v1"
DEFAULT_FRAME_MODEL = TOP_CPA_MODEL
_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"
_MAX_BATCH_IMAGES = 12


def _sha256_bytes(payload: bytes) -> str:
    # Keep the receipt schema's historical bare spelling for existing readers.
    return sha256_hex(hashlib.sha256(payload).hexdigest())


def _extract_frame_jpeg(
    media_path: Path,
    ms: int,
    *,
    max_width: int = 1280,
) -> bytes:
    with tempfile.NamedTemporaryFile(suffix=".jpg") as handle:
        subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-ss",
                f"{ms / 1000:.3f}",
                "-i",
                str(media_path),
                "-frames:v",
                "1",
                "-vf",
                f"scale=min({max_width}\\,iw):-2",
                "-q:v",
                "4",
                handle.name,
            ],
            capture_output=True,
            check=True,
        )
        return Path(handle.name).read_bytes()


def image_vision_probe(
    image_path: Path,
    question: str,
    *,
    api_base: str,
    api_key: str,
    model: str = TOP_CPA_MODEL,
    timeout_seconds: float = 90.0,
    max_tokens: int = 1024,
    max_width: int = 1280,
    reference_image_path: Path | None = None,
    response_schema=None,
    json_mode: bool | None = None,
) -> dict[str, object]:
    """Ask CPA one visual question about an on-disk image. Never raises."""

    receipt: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "image_path": str(image_path),
        "question": question,
        "model": model,
        "provider": "cpa",
    }
    if not api_base or not api_key:
        receipt.update(
            status="UNAVAILABLE",
            reason_code="CPA_CREDENTIALS_MISSING",
            error="CPA_BASE_URL/CPA_API_KEY missing",
        )
        return receipt
    def snapshot(path: Path) -> tuple[bytes, bytes]:
        source_bytes = path.read_bytes()
        # FFmpeg must decode the same bytes we hash, not reopen the mutable
        # caller path. Keep a seekable, private snapshot with the input suffix:
        # piping bytes instead breaks formats accepted by the existing reader.
        with tempfile.NamedTemporaryFile(suffix=path.suffix) as source_handle, tempfile.NamedTemporaryFile(suffix=".jpg") as handle:
            source_handle.write(source_bytes)
            source_handle.flush()
            subprocess.run(
                [
                    "ffmpeg",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-y",
                    "-i",
                    source_handle.name,
                    "-vf",
                    f"scale=min({max_width}\\,iw):-2",
                    "-q:v",
                    "4",
                    handle.name,
                ],
                capture_output=True,
                check=True,
            )
            frame_jpeg = Path(handle.name).read_bytes()
        return source_bytes, frame_jpeg

    reference_frames = ()
    try:
        source_bytes, frame_jpeg = snapshot(Path(image_path))
        if reference_image_path is not None:
            reference_bytes, reference_jpeg = snapshot(Path(reference_image_path))
            receipt["reference_image"] = {
                "image_path": str(reference_image_path),
                "image_sha256": _sha256_bytes(reference_bytes),
                "frame_sha256": _sha256_bytes(reference_jpeg),
                "role": "STYLE_ONLY",
            }
            reference_frames = (reference_jpeg,)
    except Exception as exc:
        receipt.update(
            status="UNAVAILABLE",
            reason_code="IMAGE_READ_FAILED",
            error=f"{type(exc).__name__}: {exc}",
        )
        return receipt
    receipt["image_sha256"] = _sha256_bytes(source_bytes)
    return _vision_qa(
        receipt,
        frame_jpeg,
        question,
        api_base=api_base,
        api_key=api_key,
        model=model,
        timeout_seconds=timeout_seconds,
        max_tokens=max_tokens,
        reference_frames=reference_frames,
        response_schema=response_schema,
        json_mode=json_mode,
    )


def frame_vision_probe(
    media_path: Path,
    ms: int,
    question: str,
    *,
    api_base: str,
    api_key: str,
    model: str = DEFAULT_FRAME_MODEL,
    timeout_seconds: float = 90.0,
    max_tokens: int = 1024,
    response_schema=None,
    json_mode: bool | None = None,
) -> dict[str, object]:
    """Ask CPA one visual question about a single video frame. Never raises."""

    receipt: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "media_path": str(media_path),
        "frame_ms": int(ms),
        "question": question,
        "model": model,
        "provider": "cpa",
    }
    if not api_base or not api_key:
        receipt.update(
            status="UNAVAILABLE",
            reason_code="CPA_CREDENTIALS_MISSING",
            error="CPA_BASE_URL/CPA_API_KEY missing",
        )
        return receipt
    try:
        frame_jpeg = _extract_frame_jpeg(Path(media_path), int(ms))
    except Exception as exc:
        receipt.update(
            status="UNAVAILABLE",
            reason_code="FRAME_EXTRACT_FAILED",
            error=f"{type(exc).__name__}: {exc}",
        )
        return receipt
    return _vision_qa(
        receipt,
        frame_jpeg,
        question,
        api_base=api_base,
        api_key=api_key,
        model=model,
        timeout_seconds=timeout_seconds,
        max_tokens=max_tokens,
        response_schema=response_schema,
        json_mode=json_mode,
    )


def _ruling_response_bytes(request, *, question, material, api_base, model, timeout_seconds, max_tokens):
    from .completed_review_checkpoint import ruling_response
    from .llm_client import CpaCompletion
    def send(_prompt):
        with runtime_provider_slot(provider='cpa', modality='vision', timeout_seconds=provider_wait_for_call(timeout_seconds)), observed_model_request(model=model,kind='vision',transport='http_responses'):
            with observed_cpa_request(request, timeout=timeout_seconds, model=model, kind='vision') as response:
                raw = response.read()
        # Preserve all wire bytes, including a malformed/non-UTF8 reply; the
        # current decoder below must reject the same original, never re-ask it.
        try:
            payload = json.loads(raw)
            answered = payload.get('model') if isinstance(payload, dict) else None
        except (ValueError, UnicodeError):
            answered = None
        return CpaCompletion(base64.b64encode(raw).decode('ascii'),
                             answered if isinstance(answered, str) and answered.strip() else model)
    send.cpa_cache_identity = {'transport': 'cpa_vision', 'models': [model], 'effort': 'low',
        'api_base_sha256': _sha256_bytes(api_base.encode()), 'max_tokens': max_tokens}
    raw, hit = ruling_response(stage='vision-ruling', prompt=question, llm_call=send,
        inputs=material, parser_path=Path(__file__))
    return base64.b64decode(raw, validate=True), hit


def batch_jpeg_vision_probe(
    image_paths: list[Path],
    question: str,
    *,
    api_base: str,
    api_key: str,
    model: str = TOP_CPA_MODEL,
    timeout_seconds: float = 90.0,
    max_tokens: int = 4096,
    raw_response_path: Path | None = None,
    response_schema=None,
    json_mode: bool | None = None,
) -> dict[str, object]:
    """Ask CPA one visual question about 1--12 exact on-disk JPEG snapshots.

    The bytes sent to CPA are the bytes read from each path.  This route is
    deliberately separate from :func:`image_vision_probe`, whose compatibility
    contract converts ordinary image formats through FFmpeg before dispatch.
    """

    receipt: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "question": question,
        "model": model,
        "provider": "cpa",
    }
    if not api_base or not api_key:
        receipt.update(
            status="UNAVAILABLE",
            reason_code="CPA_CREDENTIALS_MISSING",
            error="CPA_BASE_URL/CPA_API_KEY missing",
        )
        return receipt
    if not isinstance(image_paths, list) or not 1 <= len(image_paths) <= _MAX_BATCH_IMAGES:
        receipt.update(
            status="UNAVAILABLE",
            reason_code="BATCH_IMAGE_COUNT_INVALID",
            error=f"batch requires 1 to {_MAX_BATCH_IMAGES} images",
        )
        return receipt
    if not isinstance(question, str) or not question.strip():
        receipt.update(
            status="UNAVAILABLE",
            reason_code="BATCH_QUESTION_INVALID",
            error="question must be a non-empty string",
        )
        return receipt

    images: list[dict[str, object]] = []
    image_bytes: list[bytes] = []
    try:
        for image_path in image_paths:
            path = Path(image_path).absolute()
            payload = path.read_bytes()
            image_bytes.append(payload)
            images.append(
                {
                    "image_path": str(path),
                    "image_sha256": _sha256_bytes(payload),
                }
            )
    except Exception as exc:
        receipt.update(
            status="UNAVAILABLE",
            reason_code="IMAGE_READ_FAILED",
            error=f"{type(exc).__name__}: {exc}",
        )
        return receipt
    receipt["images"] = images

    content: list[dict[str, object]] = [
        {"type": "input_text", "text": question},
    ]
    content.extend(
        {
            "type": "input_image",
            "image_url": "data:image/jpeg;base64," + base64.b64encode(payload).decode("ascii"),
        }
        for payload in image_bytes
    )
    body = {
        "model": model,
        "input": [
            {
                "role": "user",
                "content": content,
            }
        ],
        "reasoning": {"effort": "low"},
        "max_output_tokens": max_tokens,
    }
    contract = output_contract(response_schema, json_mode=('json' in question.lower() if json_mode is None else json_mode))
    response_format = openai_format(contract, prompt=question)
    if response_format:
        body['text'] = {'format': response_format}
    receipt['structured_output'] = contract_receipt(contract, prompt=question)
    request_bytes = json.dumps(body).encode("utf-8")
    receipt["prompt_sha256"] = _sha256_bytes(
        json.dumps(
            {
                "question": question,
                "image_sha256s": [image["image_sha256"] for image in images],
                "model": model,
            },
            sort_keys=True,
        ).encode("utf-8")
    )
    request = urllib.request.Request(
        api_base.rstrip("/") + "/responses",
        data=request_bytes,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
            "User-Agent": _UA,
        },
    )
    raw = None
    try:
        raw, hit = _ruling_response_bytes(request, question=question,
            material={'image_sha256s': [image['image_sha256'] for image in images]},
            api_base=api_base, model=model, timeout_seconds=timeout_seconds, max_tokens=max_tokens)
        if hit:
            receipt['served_from_cache'] = True
        if raw_response_path is not None:
            # Caller-owned evidence path; never persist request headers/credentials.
            if raw_response_path.is_symlink():
                raise ValueError("unsafe raw vision response path")
            raw_response_path.parent.mkdir(parents=True, exist_ok=True)
            with raw_response_path.open("xb") as evidence:
                evidence.write(raw)
            receipt["raw_response_path"] = str(raw_response_path)
        payload = json.loads(raw.decode("utf-8", errors="replace"))
        if isinstance(payload, dict) and isinstance(payload.get('model'), str) and payload['model'].strip():
            if payload['model'] != model:
                receipt['requested_model'] = model
                receipt['model'] = payload['model']
        answer = payload.get("output_text")
        if not answer:
            parts: list[str] = []
            for item in payload.get("output") or []:
                if isinstance(item, dict) and item.get("type") == "message":
                    for chunk in item.get("content") or []:
                        if (
                            isinstance(chunk, dict)
                            and chunk.get("type") in ("output_text", "text")
                            and chunk.get("text")
                        ):
                            parts.append(str(chunk["text"]))
            answer = "".join(parts)
        if not isinstance(answer, str) or not answer.strip():
            raise ValueError(f"empty vision completion (status={payload.get('status')})")
    except ProviderSlotTimeout:
        receipt.update(
            status="UNAVAILABLE",
            reason_code="VISION_PROVIDER_CAPACITY",
            error="provider capacity wait timed out",
        )
        return receipt
    except urllib.error.HTTPError as exc:
        receipt.update(
            status="UNAVAILABLE", reason_code="VISION_CALL_FAILED",
            provider_status_code=exc.code, dispatch_outcome="HTTP_ERROR_RESPONSE",
            error=f"{type(exc).__name__}: {exc}",
        )
        return receipt
    except Exception as exc:
        receipt.update(
            status="UNAVAILABLE",
            reason_code="VISION_CALL_FAILED",
            error=f"{type(exc).__name__}: {exc}",
        )
        if raw is not None or isinstance(exc, CompletedReviewCheckpointError):
            receipt['failure_layer'] = 'CONTRACT_ERROR'
        return receipt
    receipt.update(
        status="OBSERVED",
        answer=answer.strip(),
        response_sha256=_sha256_bytes(raw),
    )
    return receipt


def _vision_qa(
    receipt: dict[str, object],
    frame_jpeg: bytes,
    question: str,
    *,
    api_base: str,
    api_key: str,
    model: str,
    timeout_seconds: float,
    max_tokens: int,
    reference_frames: tuple[bytes, ...] = (),
    response_schema=None,
    json_mode: bool | None = None,
) -> dict[str, object]:
    receipt["frame_sha256"] = _sha256_bytes(frame_jpeg)
    data_uri = "data:image/jpeg;base64," + base64.b64encode(frame_jpeg).decode(
        "ascii"
    )
    content = [
        {"type": "input_text", "text": question},
        {"type": "input_image", "image_url": data_uri},
    ]
    content.extend(
        {"type": "input_image", "image_url": "data:image/jpeg;base64," + base64.b64encode(frame).decode("ascii")}
        for frame in reference_frames
    )
    body = {
        "model": model,
        "input": [
            {
                "role": "user",
                "content": content,
            }
        ],
        "reasoning": {"effort": "low"},
        "max_output_tokens": max_tokens,
    }
    contract = output_contract(response_schema, json_mode=('json' in question.lower() if json_mode is None else json_mode))
    response_format = openai_format(contract, prompt=question)
    if response_format:
        body['text'] = {'format': response_format}
    receipt['structured_output'] = contract_receipt(contract, prompt=question)
    request_bytes = json.dumps(body).encode("utf-8")
    prompt_binding = {
                "question": question,
                "frame_sha256": receipt["frame_sha256"],
                "model": model,
    }
    if reference_frames:
        prompt_binding["reference_frame_sha256s"] = [_sha256_bytes(frame) for frame in reference_frames]
    receipt["prompt_sha256"] = _sha256_bytes(
        json.dumps(
            prompt_binding,
            sort_keys=True,
        ).encode("utf-8")
    )
    request = urllib.request.Request(
        api_base.rstrip("/") + "/responses",
        data=request_bytes,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
            "User-Agent": _UA,
        },
    )
    raw = None
    try:
        raw, hit = _ruling_response_bytes(request, question=question,
            material={'image_sha256s': [_sha256_bytes(frame_jpeg), *[_sha256_bytes(f) for f in reference_frames]]},
            api_base=api_base, model=model, timeout_seconds=timeout_seconds, max_tokens=max_tokens)
        if hit:
            receipt['served_from_cache'] = True
        payload = json.loads(raw.decode("utf-8", errors="replace"))
        if isinstance(payload, dict) and isinstance(payload.get('model'), str) and payload['model'].strip():
            if payload['model'] != model:
                receipt['requested_model'] = model
                receipt['model'] = payload['model']
        answer = payload.get("output_text")
        if not answer:
            parts: list[str] = []
            for item in payload.get("output") or []:
                if isinstance(item, dict) and item.get("type") == "message":
                    for chunk in item.get("content") or []:
                        if (
                            isinstance(chunk, dict)
                            and chunk.get("type") in ("output_text", "text")
                            and chunk.get("text")
                        ):
                            parts.append(str(chunk["text"]))
            answer = "".join(parts)
        if not isinstance(answer, str) or not answer.strip():
            raise ValueError(
                f"empty vision completion (status={payload.get('status')})"
            )
    except ProviderSlotTimeout:
        receipt.update(
            status="UNAVAILABLE",
            reason_code="VISION_PROVIDER_CAPACITY",
            error="provider capacity wait timed out",
        )
        return receipt
    except Exception as exc:
        receipt.update(
            status="UNAVAILABLE",
            reason_code="VISION_CALL_FAILED",
            error=f"{type(exc).__name__}: {exc}",
        )
        if raw is not None or isinstance(exc, CompletedReviewCheckpointError):
            receipt['failure_layer'] = 'CONTRACT_ERROR'
        return receipt
    receipt.update(
        status="OBSERVED",
        answer=answer.strip(),
        response_sha256=_sha256_bytes(raw),
    )
    return receipt
