"""Hash-bound AGY fallback witness for still images and video frames.

The production visual router shows pixels to CPA first.  This module is called
only when that CPA visual witness is unavailable or violates the caller's
answer contract; AGY then opens a sandboxed local JPEG with ``view_file`` and
returns a disclosed fallback observation.  The receipt is evidence input, not
an automatic semantic verdict.

(维护者 的「AGY->gemini 这条链全都复用一种接口」裁定)：这条腿此前
**完全没有兜底**——AGY 不在（wsl 产线上按 维护者 的长期指示就是不装）时，
CPA 的视觉兜底本身也就跟着没了。现在同一张 JPEG 在 AGY 缺席/失败后直接走
``agy_gemini_client`` 的 Gemini 视觉腿，回执用 ``provider``/``model``/
``key_tier`` 三元组说清楚到底是谁看的这张图。
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from src.autoslice import agy_gemini_client


SCHEMA_VERSION = "agy-frame-witness.v1"


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


_agy_bin = agy_gemini_client.resolve_local_agy_binary


def _agy_model() -> str:
    return os.environ.get("AGY_VISION_MODEL", "Gemini 3.6 Flash (High)")


def _vision_prompt(question: str) -> str:
    return (
        "You are a visual evidence witness. Answer only from the visible "
        "pixels of the attached image, and do not use audio, filenames, "
        "outside files, shell, terminal, or network.\n"
        f"Question:\n{question}\n"
    )


def _gemini_vision_fallback(
    receipt: dict[str, object],
    frame_jpeg: bytes,
    question: str,
    *,
    provider_failures: list[dict[str, object]],
    timeout_seconds: float,
) -> dict[str, object]:
    """Same question, same pixels, Gemini instead of a missing local AGY."""

    model = agy_gemini_client.gemini_vision_model()
    prompt = _vision_prompt(question)
    answer_box: dict[str, str] = {}

    def observe(key: str) -> str:
        raw = agy_gemini_client.generate_content(
            prompt=prompt,
            key=key,
            model=model,
            inline_data=frame_jpeg,
            mime_type="image/jpeg",
            response_mime_type=None,
            timeout_seconds=int(max(30, timeout_seconds)),
        ).strip()
        if not raw:
            raise ValueError("empty Gemini visual response")
        answer_box["answer"] = raw
        return raw

    def record(failure: agy_gemini_client.GeminiAttemptFailure) -> None:
        row: dict[str, Any] = {
            "provider": "gemini_api",
            "key_tier": failure.key_tier,
            "key_ordinal": failure.key_ordinal,
            "attempt_round": failure.attempt_round,
            "model": model,
            "category": failure.category,
            "error_type": failure.error_type,
        }
        if failure.http_status is not None:
            row["http_status"] = failure.http_status
        provider_failures.append(row)

    def record_skipped(key_ordinal: int, attempt_round: int, gate_reason: str) -> None:
        provider_failures.append(
            {
                "provider": "gemini_api",
                "key_tier": agy_gemini_client.gemini_backup_policy.PAID_KEY_TIER,
                "key_ordinal": key_ordinal,
                "attempt_round": attempt_round,
                "category": f"PAID_BACKUP_SKIPPED:{gate_reason}",
            }
        )

    outcome = agy_gemini_client.run_gemini_key_ladder(
        item_key=str(receipt.get("frame_sha256") or ""),
        observe=observe,
        purpose="agy_frame_visual_witness",
        record_failure=record,
        record_paid_skipped=record_skipped,
    )
    receipt["provider_failures"] = provider_failures
    if not outcome.accepted:
        receipt.update(
            status="UNAVAILABLE",
            reason_code="VISION_WITNESS_UNAVAILABLE",
            error=(
                "AGY absent/failed and the Gemini visual fallback did not "
                "return an answer"
            ),
        )
        return receipt
    answer = answer_box["answer"]
    receipt.update(
        status="OBSERVED",
        provider="gemini_api",
        model=model,
        key_tier=outcome.accepted_key_tier,
        answer=answer,
        response_sha256=_sha256_bytes(answer.encode("utf-8")),
    )
    if outcome.paid_policy_stamp is not None:
        receipt["paid_backup_policy"] = json.loads(
            json.dumps(dict(outcome.paid_policy_stamp), ensure_ascii=False)
        )
    return receipt


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
    provider_failures: list[dict[str, object]] = []
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
            run = agy_gemini_client.run_local_agy(
                agy_gemini_client.agy_argv(
                    _agy_bin(),
                    job_dir=job_dir,
                    model=model,
                    prompt=(
                        f"Open {prompt_path} and follow it exactly. Use only "
                        f"{prompt_path} and {image_path}. Do not inspect any "
                        "other file or directory. Do not use shell or terminal."
                    ),
                    print_timeout=os.environ.get("AGY_VISION_PRINT_TIMEOUT", "5m"),
                ),
                cwd=job_dir,
                timeout=timeout_seconds,
            )
            completed = run.completed
    except Exception as exc:
        completed = None
        run = agy_gemini_client.AgyRun(
            completed=None,
            failure_category="AGY_VISION_CALL_FAILED",
            launch_error_type=type(exc).__name__,
            launch_error=exc,
        )
    if completed is None:
        provider_failures.append(
            {
                "provider": "agy",
                "category": run.failure_category,
                "error_type": run.launch_error_type,
            }
        )
        return _gemini_vision_fallback(
            receipt,
            frame_jpeg,
            question,
            provider_failures=provider_failures,
            timeout_seconds=timeout_seconds,
        )
    answer = completed.stdout.strip()
    if completed.returncode != 0 or not answer:
        provider_failures.append(
            {
                "provider": "agy",
                "category": agy_gemini_client.classify_agy_failure(
                    completed.returncode, completed.stdout, completed.stderr
                ),
                "agy_rc": completed.returncode,
                "error": (completed.stderr or "empty AGY visual response")[-400:],
            }
        )
        receipt["agy_rc"] = completed.returncode
        return _gemini_vision_fallback(
            receipt,
            frame_jpeg,
            question,
            provider_failures=provider_failures,
            timeout_seconds=timeout_seconds,
        )
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
