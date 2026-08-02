"""Hash-bound source-composition authority for channel covers.

The final-host gate protects the generated pixels, but it is too late to decide
whether the selected livestream frame should have been redrawn in the first
place.  This module asks CPA (AGY only through the shared fallback router) to
locate Li Dousha in the exact extracted reference and to make that route choice
before any image generation is attempted.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

from PIL import Image

from src.autoslice.surface_canon import CHANNEL_PROFILE


SCHEMA_VERSION = "lidousha-cover-source-composition-verification.v1"
AUTHORITY = "CPA_PRIMARY_HASH_BOUND_SOURCE_COMPOSITION"

_BOOL_FIELDS = (
    "source_face_complete",
    "faithful_crop_can_make_dominant",
    "source_carries_story_reaction",
    "cpa_redraw_recommended",
)


def _sha256(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _canonical_sha256(value: object) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _extract_json_object(answer: str) -> Mapping[str, object]:
    start = answer.index("{")
    end = answer.rindex("}") + 1
    value = json.loads(answer[start:end])
    if not isinstance(value, Mapping):
        raise ValueError("source-composition verdict is not a JSON object")
    return value


def _valid_bbox(value: object) -> bool:
    if not isinstance(value, list) or len(value) != 4:
        return False
    if not all(
        isinstance(part, (int, float))
        and not isinstance(part, bool)
        and math.isfinite(float(part))
        for part in value
    ):
        return False
    x0, y0, x1, y1 = (float(part) for part in value)
    return bool(
        0.0 <= x0 < x1 <= 1.0
        and 0.0 <= y0 < y1 <= 1.0
        and (x1 - x0) >= 0.01
        and (y1 - y0) >= 0.01
    )


def _verdict_is_coherent(verdict: Mapping[str, object]) -> bool:
    if not _valid_bbox(verdict.get("lidousha_bbox_frac")):
        return False
    if not all(isinstance(verdict.get(key), bool) for key in _BOOL_FIELDS):
        return False
    if not isinstance(verdict.get("reason"), str) or not str(
        verdict.get("reason") or ""
    ).strip():
        return False

    screenshot_safe = bool(
        verdict.get("source_face_complete") is True
        and verdict.get("faithful_crop_can_make_dominant") is True
        and verdict.get("source_carries_story_reaction") is True
    )
    # The witness cannot recommend keeping source pixels while also saying
    # that the face is cut, cannot become dominant, or does not carry the
    # story reaction.  Such a contradiction is not a route decision.
    return verdict.get("cpa_redraw_recommended") is (not screenshot_safe)


def _answer_valid(answer: str) -> bool:
    try:
        return _verdict_is_coherent(_extract_json_object(answer))
    except (ValueError, json.JSONDecodeError):
        return False


def _routing_valid(witness: Mapping[str, object]) -> bool:
    routing = witness.get("routing")
    if not isinstance(routing, Mapping):
        return False
    if routing.get("preferred_provider") != "cpa":
        return False
    fallback = routing.get("fallback_used")
    selected = str(routing.get("selected_provider") or "")
    provider = str(witness.get("provider") or "")
    if fallback is False:
        return selected == provider == "cpa"
    if fallback is True:
        return bool(
            selected == provider == "agy"
            and str(routing.get("primary_status") or "")
            in {"UNAVAILABLE", "OBSERVED_UNUSABLE"}
            and str(routing.get("primary_reason_code") or "").strip()
            and isinstance(routing.get("primary_receipt"), Mapping)
        )
    return False


_QUESTION_PREFIX = (
    f"这是待制作{CHANNEL_PROFILE.display_name}切片封面的、已经 hash-bound 的 SOURCE REFERENCE。"
    "只判断这张源图本身，不假设后续生成器会修正构图。先按当场服装、"
    f"{CHANNEL_PROFILE.cover_identity.locator_zh}定位{CHANNEL_PROFILE.display_name}；"
    f"不要把{CHANNEL_PROFILE.cover_identity.composition_decoys_zh} 当成她。"
    f"给出{CHANNEL_PROFILE.display_name}完整可见区域的归一化 bbox=[x0,y0,x1,y1]，坐标必须在 0..1 且紧包住"
    "她的脸和承担反应的上半身。判断脸是否完整；在不生成、不补画、不扭曲身份且不裁掉"
    "关键反应的前提下，能否只靠 16:9 裁切让她成为大号第一主体；源图中的表情/动作是否"
    "确实承载给定故事反应。只要脸不完整、无法忠实裁成大主体、或源图不承载故事反应，"
    "就必须 cpa_redraw_recommended=true。不要因为运动框、弹幕或游戏画面显眼而放行。"
    "只输出 JSON："
    '{"lidousha_bbox_frac":[0.0,0.0,1.0,1.0],'
    '"source_face_complete":true|false,'
    '"faithful_crop_can_make_dominant":true|false,'
    '"source_carries_story_reaction":true|false,'
    '"cpa_redraw_recommended":true|false,'
    '"reason":"简短中文像素依据"}'
)


def verify_lidousha_source_composition(
    *,
    reference_path: Path,
    reference_sha256: str,
    story_hook: str,
    title: str,
    base_url: str = "",
    api_key: str = "",
    image_probe: Callable[..., dict[str, object]] | None = None,
) -> dict[str, object]:
    """Return a strict source-composition receipt bound to reference bytes."""

    reference_path = Path(reference_path)
    receipt: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "authority": AUTHORITY,
        "reference_path": str(reference_path),
        "expected_reference_sha256": str(reference_sha256),
        "story_hook": str(story_hook),
        "title": str(title),
    }
    try:
        actual_reference_sha256 = _sha256(reference_path)
    except OSError as exc:
        receipt.update(
            status="FAIL",
            reason_code="SOURCE_COMPOSITION_REFERENCE_UNAVAILABLE",
            detail=f"{type(exc).__name__}: {exc}",
        )
        return receipt
    receipt["reference_sha256"] = actual_reference_sha256
    if actual_reference_sha256 != str(reference_sha256):
        receipt.update(
            status="FAIL",
            reason_code="SOURCE_COMPOSITION_REFERENCE_HASH_MISMATCH",
            detail=(
                f"expected {reference_sha256}, got {actual_reference_sha256}"
            ),
        )
        return receipt

    if image_probe is None:
        from src.autoslice.visual_witness import image_vision_probe

        image_probe = image_vision_probe
    question = (
        _QUESTION_PREFIX
        + "\n故事钩子："
        + (str(story_hook).strip() or "未提供；只按标题的明确事件判断")
        + "\n投稿标题："
        + str(title).strip()
    )
    try:
        witness = image_probe(
            reference_path,
            question,
            api_base=base_url,
            api_key=api_key,
            answer_validator=_answer_valid,
        )
    except Exception as exc:
        receipt.update(
            status="FAIL",
            reason_code="SOURCE_COMPOSITION_WITNESS_EXCEPTION",
            detail=f"{type(exc).__name__}: {exc}",
        )
        return receipt
    receipt["witness"] = witness
    receipt["witness_receipt_sha256"] = _canonical_sha256(witness)
    witness_image_sha = (
        "sha256:" + str(witness.get("image_sha256") or "")
    )
    if (
        witness.get("status") != "OBSERVED"
        or witness_image_sha != actual_reference_sha256
        or not _routing_valid(witness)
    ):
        receipt.update(
            status="FAIL",
            reason_code="SOURCE_COMPOSITION_WITNESS_UNAVAILABLE",
            detail=str(
                witness.get("error")
                or witness.get("reason_code")
                or witness.get("status")
                or "invalid provider routing or image hash"
            ),
        )
        return receipt
    try:
        verdict = dict(
            _extract_json_object(str(witness.get("answer") or ""))
        )
    except (ValueError, json.JSONDecodeError) as exc:
        receipt.update(
            status="FAIL",
            reason_code="SOURCE_COMPOSITION_VERDICT_UNPARSEABLE",
            detail=f"{type(exc).__name__}: {exc}",
        )
        return receipt
    receipt["verdict"] = verdict
    if not _verdict_is_coherent(verdict):
        receipt.update(
            status="FAIL",
            reason_code="SOURCE_COMPOSITION_VERDICT_INVALID",
            detail="bbox, boolean fields, reason, or route recommendation is invalid",
        )
        return receipt
    receipt["status"] = "PASS"
    return receipt


def validate_source_composition_verification(
    verification: object,
    *,
    reference_sha256: str,
) -> bool:
    """Revalidate a source-composition receipt at its consumption point."""

    if not isinstance(verification, Mapping):
        return False
    witness = verification.get("witness")
    verdict = verification.get("verdict")
    if not isinstance(witness, Mapping) or not isinstance(verdict, Mapping):
        return False
    witness_image_sha = "sha256:" + str(witness.get("image_sha256") or "")
    return bool(
        verification.get("schema_version") == SCHEMA_VERSION
        and verification.get("authority") == AUTHORITY
        and verification.get("status") == "PASS"
        and verification.get("expected_reference_sha256")
        == reference_sha256
        and verification.get("reference_sha256") == reference_sha256
        and witness_image_sha == reference_sha256
        and verification.get("witness_receipt_sha256")
        == _canonical_sha256(witness)
        and _routing_valid(witness)
        and _verdict_is_coherent(verdict)
    )


def source_composition_recommends_redraw(verification: object) -> bool:
    """witness 的否决权收窄到两个**几何**布尔（2026-07-31）。

    此前只要 `cpa_redraw_recommended` 为真就整张重绘，而该字段是三个条件的或：
    脸不完整 / 无法忠实裁成大主体 / **源图不承载故事反应**。第三条是故事判断不是
    几何判断，而 70-cover.md 的分工是「源帧没拍到的故事由 narrative_presentation
    → COVER_TEXT 承担，不是像素义务」——反应缺失的正确出路是降级 polish 或换帧
    重选，不是单独触发整张重画。前两条则是真几何不可能（角落小豆沙 polish 完还是
    角落小豆沙，下游显著性门必死），保留否决。
    """

    if not isinstance(verification, Mapping):
        return False
    verdict = verification.get("verdict")
    if not isinstance(verdict, Mapping):
        return False
    return (
        verdict.get("source_face_complete") is False
        or verdict.get("faithful_crop_can_make_dominant") is False
    )


def source_composition_supports_subject(verification: object) -> bool:
    """witness 是否给出了可用作**置信来源**的主体证据。

    这才是 2026-07-21 memory 里那条「真脸部识别放大要接 CPA 视觉裁判」的本意：
    运动几何分不清竖版手游列和皮套（辣妹案）时，让 CPA 视觉找到真脸以便忠实放大。
    字段清单本身就是铁证——纯否决器不需要返回 `lidousha_bbox_frac`，更不需要
    `_bbox_crop_box` 那个带脸部余量的 16:9 裁切实现。
    """

    if not isinstance(verification, Mapping):
        return False
    verdict = verification.get("verdict")
    if not isinstance(verdict, Mapping):
        return False
    return bool(
        verdict.get("faithful_crop_can_make_dominant") is True
        and _valid_bbox(verdict.get("lidousha_bbox_frac"))
    )


def source_composition_bbox(verification: object) -> list[float] | None:
    if not isinstance(verification, Mapping):
        return None
    verdict = verification.get("verdict")
    bbox = verdict.get("lidousha_bbox_frac") if isinstance(verdict, Mapping) else None
    if not _valid_bbox(bbox):
        return None
    assert isinstance(bbox, list)
    return [float(value) for value in bbox]


def _bbox_crop_box(
    bbox_frac: Sequence[float], *, width: int, height: int
) -> tuple[int, int, int, int]:
    """Expand an identity bbox into a bounded 16:9 crop with face margin."""

    x0, y0, x1, y1 = (float(value) for value in bbox_frac)
    bbox_w = (x1 - x0) * width
    bbox_h = (y1 - y0) * height
    center_x = ((x0 + x1) / 2.0) * width
    center_y = ((y0 + y1) / 2.0) * height
    crop_w = max(bbox_w * 1.38, bbox_h * 1.38 * 16.0 / 9.0)
    crop_h = crop_w * 9.0 / 16.0
    if crop_h > height:
        crop_h = float(height)
        crop_w = crop_h * 16.0 / 9.0
    if crop_w > width:
        crop_w = float(width)
        crop_h = crop_w * 9.0 / 16.0
    left = min(max(0.0, center_x - crop_w / 2.0), width - crop_w)
    top = min(max(0.0, center_y - crop_h / 2.0), height - crop_h)
    right = left + crop_w
    bottom = top + crop_h
    return (
        int(round(left)),
        int(round(top)),
        int(round(right)),
        int(round(bottom)),
    )


def extract_authority_source_crop(
    *,
    reference_path: Path,
    output_path: Path,
    frame_ms: int,
    verification: Mapping[str, object],
    verification_receipt_path: Path,
    verification_receipt_sha256: str,
) -> dict[str, object]:
    """Crop exact reference pixels around the CPA identity bbox."""

    reference_sha256 = _sha256(Path(reference_path))
    if not validate_source_composition_verification(
        verification,
        reference_sha256=reference_sha256,
    ):
        raise ValueError("SOURCE_COMPOSITION_VERIFICATION_INVALID")
    verdict = verification["verdict"]
    assert isinstance(verdict, Mapping)
    if (
        verdict.get("source_face_complete") is not True
        or verdict.get("faithful_crop_can_make_dominant") is not True
        or verdict.get("source_carries_story_reaction") is not True
        or verdict.get("cpa_redraw_recommended") is not False
    ):
        raise ValueError("SOURCE_COMPOSITION_CROP_NOT_AUTHORIZED")
    bbox = source_composition_bbox(verification)
    if bbox is None:
        raise ValueError("SOURCE_COMPOSITION_BBOX_INVALID")
    with Image.open(reference_path) as raw_image:
        source = raw_image.convert("RGB")
    crop_box = _bbox_crop_box(
        bbox,
        width=source.width,
        height=source.height,
    )
    cropped = source.crop(crop_box).resize(
        (1920, 1080),
        Image.Resampling.LANCZOS,
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    cropped.save(output_path, format="PNG", optimize=False)
    witness_sha = str(verification.get("witness_receipt_sha256") or "")
    return {
        "schema": "cover-frame-transfer.v2",
        "status": "HASH_BOUND_CPA_IDENTITY_CROP",
        "frame_ms": int(frame_ms),
        "source_path": str(reference_path),
        "source_sha256": reference_sha256,
        "reference_sha256": reference_sha256,
        "crop_applied": True,
        "crop_box": list(crop_box),
        "source_size": [source.width, source.height],
        "zoom": round(source.width / max(1, crop_box[2] - crop_box[0]), 4),
        "camera_window_crop": True,
        "authority_identity_crop": True,
        "authority_bbox_frac": bbox,
        "motion_bbox_role": "CANDIDATE_ONLY_NOT_AUTHORITY",
        "source_composition_schema_version": SCHEMA_VERSION,
        "source_composition_witness_sha256": witness_sha,
        "source_composition_receipt_path": str(verification_receipt_path),
        "source_composition_receipt_sha256": verification_receipt_sha256,
        "crop_output_sha256": _sha256(output_path),
    }
