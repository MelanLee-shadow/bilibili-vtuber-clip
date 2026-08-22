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
import urllib.request
from pathlib import Path

from src.autoslice.provider_slots import ProviderSlotTimeout, provider_wait_for_call, runtime_provider_slot


SCHEMA_VERSION = "cpa-frame-witness.v1"
_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


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
    model: str = "gpt-5.6-sol",
    timeout_seconds: float = 90.0,
    max_tokens: int = 1024,
    max_width: int = 1280,
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
    try:
        source_bytes = Path(image_path).read_bytes()
        with tempfile.NamedTemporaryFile(suffix=".jpg") as handle:
            subprocess.run(
                [
                    "ffmpeg",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-y",
                    "-i",
                    str(image_path),
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
    )


def frame_vision_probe(
    media_path: Path,
    ms: int,
    question: str,
    *,
    api_base: str,
    api_key: str,
    model: str = "gpt-5.6-sol",
    timeout_seconds: float = 90.0,
    max_tokens: int = 1024,
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
    )


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
) -> dict[str, object]:
    receipt["frame_sha256"] = _sha256_bytes(frame_jpeg)
    data_uri = "data:image/jpeg;base64," + base64.b64encode(frame_jpeg).decode(
        "ascii"
    )
    body = {
        "model": model,
        "input": [
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": question},
                    {"type": "input_image", "image_url": data_uri},
                ],
            }
        ],
        "reasoning": {"effort": "low"},
        "max_output_tokens": max_tokens,
    }
    request_bytes = json.dumps(body).encode("utf-8")
    receipt["prompt_sha256"] = _sha256_bytes(
        json.dumps(
            {
                "question": question,
                "frame_sha256": receipt["frame_sha256"],
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
    try:
        with runtime_provider_slot(timeout_seconds=provider_wait_for_call(timeout_seconds)):
            with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
                raw = response.read()
        payload = json.loads(raw.decode("utf-8", errors="replace"))
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
        return receipt
    receipt.update(
        status="OBSERVED",
        answer=answer.strip(),
        response_sha256=_sha256_bytes(raw),
    )
    return receipt
