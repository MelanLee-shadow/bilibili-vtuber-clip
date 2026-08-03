"""Hash-bound AGY fallback witness for still images and video frames.

The production visual router shows pixels to CPA first.  This module is called
only when that CPA visual witness is unavailable or violates the caller's
answer contract; AGY then opens a sandboxed local JPEG with ``view_file`` and
returns a disclosed fallback observation.  The receipt is evidence input, not
an automatic semantic verdict.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import tempfile
from pathlib import Path


SCHEMA_VERSION = "agy-frame-witness.v1"


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _agy_bin() -> str:
    return os.environ.get(
        "AGY_BIN",
        str(Path.home() / ".local" / "bin" / "agy"),
    )


def _agy_model() -> str:
    return os.environ.get("AGY_VISION_MODEL", "Gemini 3.6 Flash (High)")


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


def _image_jpeg(image_path: Path, *, max_width: int = 1280) -> tuple[bytes, bytes]:
    source_bytes = image_path.read_bytes()
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
        return source_bytes, Path(handle.name).read_bytes()


def _vision_qa(
    receipt: dict[str, object],
    frame_jpeg: bytes,
    question: str,
    *,
    timeout_seconds: float,
) -> dict[str, object]:
    model = _agy_model()
    receipt.update(
        {
            "frame_sha256": _sha256_bytes(frame_jpeg),
            "model": model,
            "provider": "agy",
        }
    )
    receipt["prompt_sha256"] = _sha256_bytes(
        (
            f"{question}\nframe_sha256={receipt['frame_sha256']}\n"
            f"model={model}"
        ).encode("utf-8")
    )
    try:
        with tempfile.TemporaryDirectory(prefix="autoslice-agy-frame-") as raw_dir:
            job_dir = Path(raw_dir)
            image_path = job_dir / "input.jpg"
            prompt_path = job_dir / "prompt.md"
            image_path.write_bytes(frame_jpeg)
            prompt_path.write_text(
                "You are a visual evidence witness. Open input.jpg with "
                "view_file, answer only from visible pixels, and do not use "
                "audio, filenames, outside files, shell, terminal, or network.\n"
                f"Question:\n{question}\n",
                encoding="utf-8",
            )
            completed = subprocess.run(
                [
                    _agy_bin(),
                    "--sandbox",
                    "--dangerously-skip-permissions",
                    "--add-dir",
                    str(job_dir),
                    "--model",
                    model,
                    "-p",
                    (
                        f"Open {prompt_path} and follow it exactly. Use only "
                        f"{prompt_path} and {image_path}. Do not inspect any "
                        "other file or directory. Do not use shell or terminal."
                    ),
                    "--print-timeout",
                    os.environ.get("AGY_VISION_PRINT_TIMEOUT", "5m"),
                ],
                cwd=str(job_dir),
                capture_output=True,
                check=False,
                text=True,
                timeout=timeout_seconds,
            )
    except Exception as exc:
        receipt.update(
            status="UNAVAILABLE",
            reason_code="AGY_VISION_CALL_FAILED",
            error=f"{type(exc).__name__}: {exc}",
        )
        return receipt
    answer = completed.stdout.strip()
    if completed.returncode != 0 or not answer:
        receipt.update(
            status="UNAVAILABLE",
            reason_code="AGY_VISION_CALL_FAILED",
            agy_rc=completed.returncode,
            error=(completed.stderr or "empty AGY visual response")[-400:],
        )
        return receipt
    receipt.update(
        status="OBSERVED",
        answer=answer,
        agy_rc=completed.returncode,
        response_sha256=_sha256_bytes(completed.stdout.encode("utf-8")),
    )
    return receipt


def image_vision_probe(
    image_path: Path,
    question: str,
    *,
    api_base: str = "",
    api_key: str = "",
    timeout_seconds: float = 360.0,
    max_tokens: int = 1024,
    max_width: int = 1280,
) -> dict[str, object]:
    """Ask AGY one visual question about an on-disk image. Never raises.

    ``api_base``, ``api_key`` and ``max_tokens`` remain accepted only so older
    callers can migrate without passing provider credentials into a new API.
    They are deliberately ignored.
    """

    del api_base, api_key, max_tokens
    receipt: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "image_path": str(image_path),
        "question": question,
    }
    try:
        source_bytes, frame_jpeg = _image_jpeg(
            Path(image_path),
            max_width=max_width,
        )
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
        timeout_seconds=timeout_seconds,
    )


def frame_vision_probe(
    media_path: Path,
    ms: int,
    question: str,
    *,
    api_base: str = "",
    api_key: str = "",
    timeout_seconds: float = 360.0,
    max_tokens: int = 1024,
) -> dict[str, object]:
    """Ask AGY one visual question about a video frame. Never raises."""

    del api_base, api_key, max_tokens
    receipt: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "media_path": str(media_path),
        "frame_ms": int(ms),
        "question": question,
    }
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
        timeout_seconds=timeout_seconds,
    )
