"""Screenshot polish materialization and the final-pixel face gate.

Extracted from publish_staging (2026-07-26 anti-屎山 guardrail): the polish
call, the target-bound compose loop with the whole-face contain retry, the
CPA face-integrity verdict and its hash binding all live together here.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Callable, Mapping

from .cover_generation import (
    _cover_screenshot_polish_prompt,
    _overlay_lidousha_cover_title,
)
from .cover_screenshot_poster import _compose_screenshot_poster_background


_POLISH_FACE_QUESTION = (
    "这是一张视频封面成品。请只判断画面中人物的脸部是否完整可见："
    "双眼、嘴巴、下巴都必须在画面内，且没有被画面边缘或卡片边框切断。"
    "同时报告人物是否吐舌头。只输出 JSON："
    '{"face_complete": true|false, "missing": ["eyes"|"mouth"|"chin"], '
    '"tongue_out": true|false, "reason": "简短中文说明"}'
)

def _verify_polish_face_integrity(
    final_cover_path: Path,
    *,
    base_url: str,
    api_key: str,
) -> dict[str, object]:
    """Final-pixel face-integrity verdict for AI-polished screenshot covers.

    The polish model may return a much larger face than the prompt asked for
    (2026-07-26 BV1E93L6rErV: mouth and chin cut by the fixed card crop went
    public). Polished pixels cannot inherit source-frame geometry, so the
    final bytes get an independent CPA vision verdict (Ivan 2026-07-25:
    看画面的任务交给 CPA). Failure here is fail-closed but repairable —
    cover-only maintenance retries on the next tick.
    """

    if not base_url or not api_key:
        return {
            "schema_version": "lidousha-cover-polish-face-verification.v1",
            "status": "FAIL",
            "reason_code": "VERIFIER_UNAVAILABLE",
            "detail": "CPA credentials unavailable for face verification",
        }
    from src.autoslice.cpa_frame_witness import image_vision_probe

    receipt = image_vision_probe(
        final_cover_path,
        _POLISH_FACE_QUESTION,
        api_base=base_url,
        api_key=api_key,
    )
    verification: dict[str, object] = {
        "schema_version": "lidousha-cover-polish-face-verification.v1",
        "witness": receipt,
    }
    if receipt.get("status") != "OBSERVED":
        verification.update(
            status="FAIL",
            reason_code="VERIFIER_UNAVAILABLE",
            detail=str(receipt.get("error") or receipt.get("status")),
        )
        return verification
    answer = str(receipt.get("answer") or "")
    try:
        verdict = json.loads(answer[answer.index("{"): answer.rindex("}") + 1])
        if not isinstance(verdict, dict):
            raise ValueError("verdict is not an object")
    except ValueError:
        verification.update(
            status="FAIL",
            reason_code="VERDICT_UNPARSEABLE",
            detail=answer[:200],
        )
        return verification
    verification["verdict"] = verdict
    if verdict.get("face_complete") is True and verdict.get("tongue_out") is not True:
        verification.update(status="PASS")
    else:
        verification.update(
            status="FAIL",
            reason_code=(
                "TONGUE_OUT"
                if verdict.get("tongue_out") is True
                else "FACE_INCOMPLETE"
            ),
            detail=str(verdict.get("reason") or verdict.get("missing") or ""),
        )
    return verification

def _polish_face_binding_failure(
    cover_generation: Mapping[str, object],
    face_verification: Mapping[str, object] | None,
    method: str,
) -> str | None:
    """A polished final needs a PASS face verdict bound to its exact hash."""

    if method != "screenshot_polish":
        return None
    bound_sha = str(cover_generation.get("final_cover_sha256") or "")
    witness = (
        face_verification.get("witness")
        if isinstance(face_verification, Mapping)
        else None
    )
    witness_sha = (
        "sha256:" + str(witness.get("image_sha256"))
        if isinstance(witness, Mapping) and witness.get("image_sha256")
        else None
    )
    if (
        isinstance(face_verification, Mapping)
        and face_verification.get("status") == "PASS"
        and witness_sha == bound_sha
    ):
        return None
    return (
        "polished cover lacks a PASS face-integrity verdict bound to the "
        "final cover hash: "
        + str(
            (face_verification or {}).get("reason_code")
            or "VERIFICATION_MISSING"
        )
    )

def _compose_screenshot_cover_with_face_gate(
    *,
    overlay_source: Path,
    crop_evidence: Mapping[str, object],
    candidate_id: str,
    ai_dir: Path,
    covers_dir: Path,
    cover_text: str,
    art_direction,
    relationship_visual_required: bool,
    method: str,
    base_url: str,
    api_key: str,
    verifier: Callable[..., dict[str, object]] | None = None,
) -> tuple[dict[str, object], dict[str, object], dict[str, object] | None]:
    """Compose the poster card + title and face-gate polished finals.

    Camera-window sources always take the whole-face contain card; a polished
    final must carry a PASS face-integrity verdict, with one contain re-compose
    before fail-closed (2026-07-26 BV1E93L6rErV).
    """

    poster_source = overlay_source
    face_safe_contain = bool(
        isinstance(crop_evidence, Mapping)
        and crop_evidence.get("camera_window_crop")
    )
    poster_path = ai_dir / f"{candidate_id}.screenshot-poster.png"
    final_cover_path = covers_dir / f"{candidate_id}.screenshot-title.cover.png"
    face_verification: dict[str, object] | None = None
    while True:
        poster_evidence = _compose_screenshot_poster_background(
            poster_source,
            poster_path,
            art_direction=art_direction,
            preserve_full_frame=relationship_visual_required,
            source_ai_modified=method == "screenshot_polish",
            face_safe_contain=(
                face_safe_contain and not relationship_visual_required
            ),
        )
        overlay = _overlay_lidousha_cover_title(
            poster_path,
            final_cover_path,
            cover_text=cover_text,
            art_direction=art_direction,
        )
        if method != "screenshot_polish":
            break
        face_verification = (verifier or _verify_polish_face_integrity)(
            final_cover_path,
            base_url=base_url,
            api_key=api_key,
        )
        if face_verification.get("status") == "PASS":
            break
        if (
            face_verification.get("reason_code") == "FACE_INCOMPLETE"
            and not face_safe_contain
            and not relationship_visual_required
        ):
            # 修复优先于 fail-close：换整脸 contain 卡重排一次再终判。
            face_safe_contain = True
            continue
        break
    return poster_evidence, overlay, face_verification


def _degrade_rejected_polish_to_direct(
    *,
    poster_evidence: dict[str, object],
    overlay: dict[str, object],
    face_verification: Mapping[str, object] | None,
    method: str,
    selected_model: str,
    screenshot_base: Path,
    crop_evidence: Mapping[str, object],
    candidate_id: str,
    ai_dir: Path,
    covers_dir: Path,
    cover_text: str,
    art_direction,
    relationship_visual_required: bool,
    base_url: str,
    api_key: str,
    cover_generation: dict[str, object],
    verifier: Callable[..., dict[str, object]] | None = None,
) -> tuple[
    dict[str, object],
    dict[str, object],
    dict[str, object] | None,
    str,
    str,
]:
    """Reject unsafe polish pixels and retain the source screenshot route."""

    if (
        method != "screenshot_polish"
        or not isinstance(face_verification, Mapping)
        or face_verification.get("status") == "PASS"
    ):
        return (
            poster_evidence,
            overlay,
            dict(face_verification) if face_verification is not None else None,
            method,
            selected_model,
        )
    rejected_verification = dict(face_verification)
    poster_evidence, overlay, _ = _compose_screenshot_cover_with_face_gate(
        overlay_source=screenshot_base,
        crop_evidence=crop_evidence,
        candidate_id=candidate_id,
        ai_dir=ai_dir,
        covers_dir=covers_dir,
        cover_text=cover_text,
        art_direction=art_direction,
        relationship_visual_required=relationship_visual_required,
        method="screenshot_direct",
        base_url=base_url,
        api_key=api_key,
        verifier=verifier,
    )
    cover_generation["rejected_polish_face_verification"] = (
        rejected_verification
    )
    cover_generation["screenshot_polish"] = {
        "status": "DEGRADED_TO_DIRECT",
        "reason_code": "POLISH_FACE_GATE_FAILED",
        "detail": (
            "generated polish rejected by final-pixel face gate; "
            "hash-bound source screenshot retained"
        ),
        "image_generation_attempted": True,
        "image_generation_used": False,
    }
    return poster_evidence, overlay, None, "screenshot_direct", "none"


def _materialize_screenshot_polish(
    *,
    screenshot_base: Path,
    candidate_id: str,
    ai_dir: Path,
    evidence_dir: Path | None,
    polish: bool,
    image_edit: Callable[..., dict[str, object]] | None,
    base_url: str,
    api_key: str,
    cover_generation: dict[str, object],
) -> tuple[Path, str, str, list[str], bool]:
    """Optionally polish a screenshot without changing the selected route."""

    overlay_source = screenshot_base
    method = "screenshot_direct"
    selected_model = "none"
    attempted_models: list[str] = []
    polish_attempted = False
    if (
        polish
        and image_edit is not None
        and base_url
        and api_key
        and evidence_dir is not None
    ):
        polished_path = ai_dir / f"{candidate_id}.screenshot-polished.png"
        polish_attempted = True
        try:
            cpa_result = image_edit(
                base_url=base_url,
                api_key=api_key,
                reference_path=screenshot_base,
                output_path=polished_path,
                prompt=_cover_screenshot_polish_prompt(),
                request_path=(
                    evidence_dir
                    / f"{candidate_id}.cover-polish-request.redacted.json"
                ),
                response_path=(
                    evidence_dir
                    / f"{candidate_id}.cover-polish-response.redacted.json"
                ),
            )
        except Exception as exc:
            cpa_result = {
                "status": "EXCEPTION",
                "detail": f"{type(exc).__name__}: {exc}",
            }
        attempted_models = list(cpa_result.get("attempted_models") or [])
        if (
            cpa_result.get("status") == "AI_BACKGROUND_READY"
            and polished_path.is_file()
        ):
            overlay_source = polished_path
            method = "screenshot_polish"
            selected_model = str(cpa_result.get("selected_model") or "cpa")
            cover_generation["screenshot_polish"] = {
                "status": "POLISHED",
                "image_generation_attempted": True,
                "image_generation_used": True,
            }
        else:
            cover_generation["screenshot_polish"] = {
                "status": "DEGRADED_TO_DIRECT",
                "reason_code": "SCREENSHOT_POLISH_FAILED",
                "detail": str(
                    cpa_result.get("detail")
                    or cpa_result.get("status")
                    or "polish failed"
                ),
                "image_generation_attempted": True,
                "image_generation_used": False,
            }
    elif polish:
        cover_generation["screenshot_polish"] = {
            "status": "DEGRADED_TO_DIRECT",
            "reason_code": "SCREENSHOT_POLISH_ADAPTER_UNAVAILABLE",
            "detail": "CPA credentials/adapter unavailable",
            "image_generation_attempted": False,
            "image_generation_used": False,
        }
    return (
        overlay_source,
        method,
        selected_model,
        attempted_models,
        polish_attempted,
    )
