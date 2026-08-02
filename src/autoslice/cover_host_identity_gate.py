"""Final-pixel host identity and subject-prominence gate.

The image generator may copy the wrong person from a multi-person reference
and then add one or two panda-like details. It may also preserve the host's
identity while shrinking her into a corner, leaving dead space, or adding
meaningless graphic bars. A prompt is not evidence that the result is a useful
thumbnail. This module builds a hash-bound SOURCE/FINAL comparison image and
asks CPA for a distinct strict identity plus composition verdict. AGY is a
fallback witness only when CPA vision is unavailable.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Mapping

from PIL import Image, ImageDraw, ImageOps

from src.autoslice.surface_canon import CHANNEL_PROFILE


SCHEMA_VERSION = "lidousha-cover-final-host-identity-verification.v3"
AUTHORITY = (
    "CPA_PRIMARY_HASH_BOUND_SOURCE_FINAL_IDENTITY_AND_PROMINENCE_COMPARISON"
)
# 冻结出版结转条款（2026-08-01，1013 r18 案）：字节等同复用一张**已发布**
# 封面时，其发布时点的身份见证是该字节的既成证据；对同一字节按今日更严的
# v3 门重考属于对冻结资产做 live 政策重算（7/27 裁定禁止），且视觉裁判对
# 边界样本非确定（r17 PASS / r18 FAIL 同字节）。仅当 bundle 显式声明
# carried_forward_from_published_record 且见证恰为下列历史世代对时才放行，
# 其余（哈希绑定、PASS、provider 路由）与 v3 同标准。
PUBLISHED_CARRY_SCHEMA_VERSION = (
    "lidousha-cover-final-host-identity-verification.v2"
)
PUBLISHED_CARRY_AUTHORITY = (
    "CPA_PRIMARY_HASH_BOUND_SOURCE_FINAL_IDENTITY_COMPARISON"
)
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


_BOOLEAN_VERDICT_FIELDS = (
    "source_lidousha_located",
    "primary_subject_is_lidousha",
    "primary_subject_matches_other_source_participant",
    "primary_subject_is_visually_dominant",
    "primary_subject_face_is_large_and_clear",
    "primary_subject_carries_story_reaction",
    "excessive_dead_space",
    "meaningless_dominant_decoration",
    "thumbnail_has_clear_click_hook",
)


def _identity_answer_valid(answer: str) -> bool:
    try:
        verdict = _extract_json_object(answer)
    except (ValueError, json.JSONDecodeError):
        return False
    return bool(
        all(isinstance(verdict.get(key), bool) for key in _BOOLEAN_VERDICT_FIELDS)
        and isinstance(verdict.get("identity_conflicts"), list)
        and isinstance(verdict.get("composition_conflicts"), list)
        and all(
            isinstance(value, str) and value.strip()
            for key in ("identity_conflicts", "composition_conflicts")
            for value in verdict[key]
        )
        and isinstance(verdict.get("reason"), str)
        and str(verdict.get("reason") or "").strip()
    )


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
    f"请先在左图用可见名牌、服装和外形定位{CHANNEL_PROFILE.display_name}；若名牌可见，必须以名牌为准。"
    f"{CHANNEL_PROFILE.display_name}是{CHANNEL_PROFILE.cover_identity.gate_appearance_zh}。特别注意：不要把左图其他人物"
    f"{CHANNEL_PROFILE.cover_identity.gate_rival_note_zh}误认成{CHANNEL_PROFILE.display_name}；给别的角色"
    f"{CHANNEL_PROFILE.cover_identity.gate_imitation_zh}也不算身份正确。再判断右图最大、最显眼、承担故事反应的主角是否确实"
    f"延续左图{CHANNEL_PROFILE.display_name}，而不是其他参与者。次要人物可以存在，但主角必须是{CHANNEL_PROFILE.display_name}。"
    f"这是信息流缩略图终检，不只验身份：{CHANNEL_PROFILE.display_name}不能缩在角落或小到需要寻找；脸部必须"
    "足够大、完整、清楚，且她的表情/动作必须承担标题所讲事件的反应。通常脸或上半身"
    "应形成第一视觉焦点；仅仅能认出她不算通过。专门留给已渲染标题的干净文字区是合理"
    "留白，但标题以外不得有大片死空白、无意义纯色红条/色块、装饰噪声或与故事无关的"
    "强元素压过人物。陌生观众只看右图时应立即知道看谁、看到一个明确点击钩子。"
    "不确定就 FAIL。只输出 JSON："
    '{"source_lidousha_located":true|false,'
    '"primary_subject_is_lidousha":true|false,'
    '"primary_subject_matches_other_source_participant":true|false,'
    '"primary_subject_is_visually_dominant":true|false,'
    '"primary_subject_face_is_large_and_clear":true|false,'
    '"primary_subject_carries_story_reaction":true|false,'
    '"excessive_dead_space":true|false,'
    '"meaningless_dominant_decoration":true|false,'
    '"thumbnail_has_clear_click_hook":true|false,'
    '"primary_subject_identity":"简短身份",'
    '"identity_conflicts":["冲突特征"],'
    '"composition_conflicts":["主体过小/角落小人/死空白/无意义装饰等"],'
    '"reason":"简短中文说明"}'
)


def verify_lidousha_final_host_identity(
    *,
    final_cover_path: Path,
    final_cover_sha256: str,
    reference_path: Path,
    base_url: str = "",
    api_key: str = "",
) -> dict[str, object]:
    """Return a fail-closed CPA-primary verdict bound to source/final bytes."""

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

    from src.autoslice.visual_witness import image_vision_probe

    comparison_sha = _sha256(comparison_path)
    witness = image_vision_probe(
        comparison_path,
        _QUESTION,
        api_base=base_url,
        api_key=api_key,
        answer_validator=_identity_answer_valid,
    )
    verification.update(
        comparison_path=str(comparison_path),
        comparison_sha256=comparison_sha,
        witness=witness,
        preferred_witness_provider="cpa",
        selected_witness_provider=witness.get("provider"),
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
    identity_conflicts = verdict.get("identity_conflicts")
    composition_conflicts = verdict.get("composition_conflicts")
    identity_passed = bool(
        verdict.get("source_lidousha_located") is True
        and verdict.get("primary_subject_is_lidousha") is True
        and verdict.get("primary_subject_matches_other_source_participant")
        is False
        and isinstance(identity_conflicts, list)
        and not identity_conflicts
    )
    composition_passed = bool(
        verdict.get("primary_subject_is_visually_dominant") is True
        and verdict.get("primary_subject_face_is_large_and_clear") is True
        and verdict.get("primary_subject_carries_story_reaction") is True
        and verdict.get("excessive_dead_space") is False
        and verdict.get("meaningless_dominant_decoration") is False
        and verdict.get("thumbnail_has_clear_click_hook") is True
        and isinstance(composition_conflicts, list)
        and not composition_conflicts
    )
    if identity_passed and composition_passed:
        verification["status"] = "PASS"
    elif identity_passed:
        verification.update(
            status="FAIL",
            reason_code="FINAL_COVER_SUBJECT_PROMINENCE_FAILED",
            detail=str(
                verdict.get("reason")
                or composition_conflicts
                or "host is identifiable but not a dominant clickworthy subject"
            ),
        )
    else:
        verification.update(
            status="FAIL",
            reason_code="FINAL_HOST_IDENTITY_MISMATCH",
            detail=str(
                verdict.get("reason")
                or identity_conflicts
                or "identity mismatch"
            ),
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
    provider = str(witness.get("provider") or "") if isinstance(witness, Mapping) else ""
    routing = witness.get("routing") if isinstance(witness, Mapping) else None
    primary_receipt = (
        routing.get("primary_receipt") if isinstance(routing, Mapping) else None
    )
    provider_route_valid = bool(
        provider == "cpa"
        or (
            provider == "agy"
            and isinstance(routing, Mapping)
            and routing.get("preferred_provider") == "cpa"
            and routing.get("fallback_used") is True
            and routing.get("primary_status") != "OBSERVED"
            and isinstance(primary_receipt, Mapping)
            and primary_receipt.get("provider") == "cpa"
        )
    )
    generation_pin_valid = (
        verification.get("schema_version") == SCHEMA_VERSION
        and verification.get("authority") == AUTHORITY
    ) or (
        # 冻结出版结转条款：仅字节等同结转 bundle + 恰为 v2 历史世代对。
        cover_generation.get("carried_forward_from_published_record") is True
        and verification.get("schema_version")
        == PUBLISHED_CARRY_SCHEMA_VERSION
        and verification.get("authority") == PUBLISHED_CARRY_AUTHORITY
    )
    return bool(
        generation_pin_valid
        and verification.get("status") == "PASS"
        and verification.get("final_cover_sha256")
        == cover_generation.get("final_cover_sha256")
        and comparison_sha.startswith("sha256:")
        and witness_sha == comparison_sha
        and provider_route_valid
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
