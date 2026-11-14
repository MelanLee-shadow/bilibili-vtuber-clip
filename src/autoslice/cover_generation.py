"""Profile-driven AI cover generation, art direction, and title overlay."""

from __future__ import annotations

from src.autoslice.evidence_identity import file_sha256 as _evidence_file_sha256
import json
import os
import re
from pathlib import Path
from typing import Callable, Mapping, Sequence

from src.autoslice.cover_image_models import (
    PRIMARY_MODEL as _COVER_PRIMARY_MODEL,
    COMPATIBILITY_MODEL as _COVER_COMPATIBILITY_MODEL,
)
from src.autoslice.provider_slots import provider_transport
from src.autoslice.cpa_image_edit import perform_image_edit
from src.autoslice.cover_canvas import _COVER_CANVAS, _COVER_REQUEST_SIZE


def _sha256(path: Path) -> str:
    digest = _evidence_file_sha256(path)
    return digest


def _cpa_image_model_candidates() -> tuple[str, ...]:
    preferred = os.environ.get("CPA_IMAGE_MODEL", _COVER_PRIMARY_MODEL).strip()
    if not preferred:
        preferred = _COVER_PRIMARY_MODEL
    if preferred == _COVER_PRIMARY_MODEL:
        return (_COVER_PRIMARY_MODEL, _COVER_COMPATIBILITY_MODEL)
    return (preferred,)


def _explicit_cpa_model_unavailable(status_code: int, raw: str) -> bool:
    """Only explicit routing/model availability errors authorize fallback."""

    if status_code not in {400, 404}:
        return False
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        payload = None
    if isinstance(payload, Mapping):
        error = payload.get("error")
        if isinstance(error, Mapping) and error.get("code") == "client_model_unavailable":
            return True
        if isinstance(error, Mapping) and status_code == 400:
            message = error.get("message")
            if isinstance(message, str) and re.match(
                r"^Model\s+\S+\s+is not supported on /v1/images/generations or /v1/images/edits(?:\.|$)",
                message, re.IGNORECASE,
            ):
                return True
    lowered = raw.lower()
    return "client_model_unavailable" in lowered or "可用渠道不存在" in raw


def _normalize_cover_canvas(path: Path) -> tuple[int, int]:
    """Force the AI background onto the 1920x1080 canvas the text-overlay zones
    assume: scale-to-cover + center-crop (never letterbox/stretch)."""
    from PIL import Image

    with Image.open(path) as img:
        img = img.convert("RGB")
        if img.size != _COVER_CANVAS:
            scale = max(_COVER_CANVAS[0] / img.width, _COVER_CANVAS[1] / img.height)
            new_w = max(_COVER_CANVAS[0], int(round(img.width * scale)))
            new_h = max(_COVER_CANVAS[1], int(round(img.height * scale)))
            resized = img.resize((new_w, new_h), Image.LANCZOS)
            left = (new_w - _COVER_CANVAS[0]) // 2
            top = (new_h - _COVER_CANVAS[1]) // 2
            img = resized.crop((left, top, left + _COVER_CANVAS[0], top + _COVER_CANVAS[1]))
        img.save(path)
        return img.size


@provider_transport(timeout_result=lambda: {"status": "FAILED", "reason_code": "CPA_IMAGE_EDIT_PROVIDER_CAPACITY", "detail": "provider capacity wait timed out", "attempted_models": []})
def _call_cpa_image_edit(
    *,
    base_url: str,
    api_key: str,
    reference_path: Path,
    style_reference_paths: Sequence[Path] = (),
    output_path: Path,
    prompt: str,
    request_path: Path,
    response_path: Path,
    timeout_seconds: float = 600.0,
    model_candidates: Sequence[str] | None = None,
    normalize_canvas: Callable[[Path], tuple[int, int]] | None = None,
) -> dict[str, object]:
    return perform_image_edit(
        base_url=base_url, api_key=api_key, reference_path=reference_path,
        style_reference_paths=style_reference_paths,
        output_path=output_path, prompt=prompt, request_path=request_path,
        response_path=response_path, timeout_seconds=timeout_seconds,
        model_candidates=model_candidates or _cpa_image_model_candidates(),
        request_size=_COVER_REQUEST_SIZE,
        normalize_canvas=normalize_canvas or _normalize_cover_canvas,
        make_body=_multipart_form_data, file_sha256=_sha256,
        explicit_model_unavailable=_explicit_cpa_model_unavailable,
    )


def _multipart_form_data(
    *,
    fields: Mapping[str, str],
    files: Mapping[str, tuple[str, bytes, str]]
    | Sequence[tuple[str, tuple[str, bytes, str]]],
) -> tuple[bytes, str]:
    boundary = "----HermesVtuberSliceCoverBoundary"
    chunks: list[bytes] = []
    for name, value in fields.items():
        chunks.extend(
            [
                f"--{boundary}\r\n".encode(),
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode(),
                value.encode(),
                b"\r\n",
            ]
        )
    file_items = files.items() if isinstance(files, Mapping) else files
    for name, (filename, content, content_type) in file_items:
        chunks.extend(
            [
                f"--{boundary}\r\n".encode(),
                f'Content-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'.encode(),
                f"Content-Type: {content_type}\r\n\r\n".encode(),
                content,
                b"\r\n",
            ]
        )
    chunks.append(f"--{boundary}--\r\n".encode())
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


