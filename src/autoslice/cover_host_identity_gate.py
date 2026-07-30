"""Final-pixel Li Dousha identity gate for AI-modified covers.

The image generator may copy the wrong person from a multi-person reference
and then add one or two panda-like details.  A prompt is not evidence that the
result still depicts Li Dousha.  This module builds a hash-bound SOURCE/FINAL
comparison image and asks AGY (the image-capable lane) for a strict visual
verdict.  CPA is text-only and is deliberately not used here.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Mapping

from PIL import Image, ImageDraw, ImageOps


SCHEMA_VERSION = "lidousha-cover-final-host-identity-verification.v1"
AUTHORITY = "AGY_HASH_BOUND_SOURCE_FINAL_IDENTITY_COMPARISON"
UNAVAILABLE_REASON_CODES = frozenset(
    {
        "HOST_IDENTITY_WITNESS_UNAVAILABLE",
        "HOST_IDENTITY_VERDICT_UNPARSEABLE",
        "HOST_IDENTITY_VERIFIER_EXCEPTION",
        "HOST_IDENTITY_VERIFIER_MISSING",
    }
)


def _sha256(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _extract_json_object(answer: str) -> Mapping[str, object]:
    start = answer.index("{")
    end = answer.rindex("}") + 1
    value = json.loads(answer[start:end])
    if not isinstance(value, Mapping):
        raise ValueError("verdict is not a JSON object")
    return value


def _comparison_path(final_cover_path: Path) -> Path:
    return final_cover_path.with_name(
        final_cover_path.stem + ".host-identity-witness.png"
    )


def _build_comparison(
    *, reference_path: Path, final_cover_path: Path, output_path: Path
) -> None:
    """Render a deterministic source/final contact sheet for the witness."""

    canvas = Image.new("RGB", (1920, 620), (10, 16, 34))
    draw = ImageDraw.Draw(canvas)
    draw.rectangle((0, 0, 960, 72), fill=(22, 66, 124))
    draw.rectangle((960, 0, 1920, 72), fill=(116, 38, 68))
    draw.text((24, 22), "SOURCE REFERENCE", fill="white")
    draw.text((984, 22), "FINAL COVER", fill="white")
    with Image.open(reference_path) as source_image:
        source = ImageOps.contain(source_image.convert("RGB"), (940, 528))
    with Image.open(final_cover_path) as final_image:
        final = ImageOps.contain(final_image.convert("RGB"), (940, 528))
    canvas.paste(source, (10 + (940 - source.width) // 2, 82))
    canvas.paste(final, (970 + (940 - final.width) // 2, 82))
    output_path.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(output_path, format="PNG", optimize=False)


_QUESTION = (
    "左侧是同一切片的 SOURCE REFERENCE，右侧是待发布 FINAL COVER。"
    "请先在左图用可见名牌、服装和外形定位李豆沙；若名牌可见，必须以名牌为准。"
    "李豆沙是白发、蓝白熊猫帽/熊猫耳的小个子形象。特别注意：不要把左图其他人物"
    "（例如带红金角饰、白发长辫、红领结的伊索尔Sol）误认成李豆沙；给别的角色"
    "加熊猫耳也不算身份正确。再判断右图最大、最居中、承担故事反应的主角是否确实"
    "延续左图李豆沙，而不是其他参与者。次要人物可以存在，但主角必须是李豆沙。"
    "不确定就 FAIL。只输出 JSON："
    '{"source_lidousha_located":true|false,'
    '"primary_subject_is_lidousha":true|false,'
    '"primary_subject_matches_other_source_participant":true|false,'
    '"primary_subject_identity":"简短身份",'
    '"identity_conflicts":["冲突特征"],"reason":"简短中文说明"}'
)


def verify_lidousha_final_host_identity(
    *,
    final_cover_path: Path,
    final_cover_sha256: str,
    reference_path: Path,
    base_url: str = "",
    api_key: str = "",
) -> dict[str, object]:
    """Return a fail-closed AGY verdict bound to source and final bytes."""

    final_cover_path = Path(final_cover_path)
    reference_path = Path(reference_path)
    verification: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "authority": AUTHORITY,
        "final_cover_path": str(final_cover_path),
        "reference_path": str(reference_path),
    }
    try:
        actual_final_sha = _sha256(final_cover_path)
        reference_sha = _sha256(reference_path)
    except OSError as exc:
        verification.update(
            status="FAIL",
            reason_code="IDENTITY_INPUT_UNAVAILABLE",
            detail=f"{type(exc).__name__}: {exc}",
        )
        return verification
    verification.update(
        final_cover_sha256=actual_final_sha,
        reference_sha256=reference_sha,
    )
    if actual_final_sha != str(final_cover_sha256):
        verification.update(
            status="FAIL",
            reason_code="FINAL_COVER_HASH_MISMATCH",
            detail=f"expected {final_cover_sha256}, got {actual_final_sha}",
        )
        return verification

    comparison_path = _comparison_path(final_cover_path)
    try:
        _build_comparison(
            reference_path=reference_path,
            final_cover_path=final_cover_path,
            output_path=comparison_path,
        )
    except Exception as exc:
        verification.update(
            status="FAIL",
            reason_code="IDENTITY_COMPARISON_BUILD_FAILED",
            detail=f"{type(exc).__name__}: {exc}",
        )
        return verification

    from src.autoslice.agy_frame_witness import image_vision_probe

    comparison_sha = _sha256(comparison_path)
    witness = image_vision_probe(
        comparison_path,
        _QUESTION,
        api_base=base_url,
        api_key=api_key,
    )
    verification.update(
        comparison_path=str(comparison_path),
        comparison_sha256=comparison_sha,
        witness=witness,
    )
    witness_sha = (
        "sha256:" + str(witness.get("image_sha256"))
        if witness.get("image_sha256")
        else ""
    )
    if witness.get("status") != "OBSERVED" or witness_sha != comparison_sha:
        verification.update(
            status="FAIL",
            reason_code="HOST_IDENTITY_WITNESS_UNAVAILABLE",
            detail=str(witness.get("error") or witness.get("status") or ""),
        )
        return verification
    try:
        verdict = dict(_extract_json_object(str(witness.get("answer") or "")))
    except (ValueError, json.JSONDecodeError) as exc:
        verification.update(
            status="FAIL",
            reason_code="HOST_IDENTITY_VERDICT_UNPARSEABLE",
            detail=f"{type(exc).__name__}: {exc}",
        )
        return verification
    verification["verdict"] = verdict
    conflicts = verdict.get("identity_conflicts")
    passed = bool(
        verdict.get("source_lidousha_located") is True
        and verdict.get("primary_subject_is_lidousha") is True
        and verdict.get("primary_subject_matches_other_source_participant")
        is False
        and isinstance(conflicts, list)
        and not conflicts
    )
    if passed:
        verification["status"] = "PASS"
    else:
        verification.update(
            status="FAIL",
            reason_code="FINAL_HOST_IDENTITY_MISMATCH",
            detail=str(verdict.get("reason") or conflicts or "identity mismatch"),
        )
    return verification


def validate_final_host_identity_verification(
    cover_generation: Mapping[str, object],
) -> bool:
    """Validate receipt shape and exact final-cover hash binding."""

    verification = cover_generation.get("final_host_identity_verification")
    if not isinstance(verification, Mapping):
        return False
    witness = verification.get("witness")
    comparison_sha = str(verification.get("comparison_sha256") or "")
    witness_sha = (
        "sha256:" + str(witness.get("image_sha256"))
        if isinstance(witness, Mapping) and witness.get("image_sha256")
        else ""
    )
    return bool(
        verification.get("schema_version") == SCHEMA_VERSION
        and verification.get("authority") == AUTHORITY
        and verification.get("status") == "PASS"
        and verification.get("final_cover_sha256")
        == cover_generation.get("final_cover_sha256")
        and comparison_sha.startswith("sha256:")
        and witness_sha == comparison_sha
    )


def final_host_identity_witness_unavailable(
    cover_generation: Mapping[str, object],
) -> bool:
    """Distinguish missing identity evidence from an observed mismatch."""

    verification = cover_generation.get("final_host_identity_verification")
    return bool(
        isinstance(verification, Mapping)
        and verification.get("status") == "FAIL"
        and verification.get("reason_code") in UNAVAILABLE_REASON_CODES
    )
