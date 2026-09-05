"""Keep a repaired cover on the route it was originally produced under.

治 C7（截图优先）：通用 cover-only 修复是**重绘独占**的
（`cover_repair.py` 三处硬编码 `selected_treatment="cpa_redraw"`，
`scripts/regenerate_channel_cover.py` 自述 "real CPA image edit only"），于是任何
一次返修都把已经选定的截图路线单向换成 AI 重绘——「一旦降级或返修，没有任何路径
能走回截图」。

两条出路按证据分流：

* 那份 hash-bound 截图像素**还在盘上** → 返修的正确工具是
  ``scripts/repair_screenshot_cover.py``（同一份已证据化的 PNG 重排海报卡、重渲
  标题、重打整脸门，且不再打生图），通用重绘必须在**付费请求之前**让路；
* 像素已经不在了 → 没有可继承的截图，重绘是唯一出路，不阻断交付；但被顶替的
  路线必须 typed 披露，不能静默消失。`actual_treatment` 永远如实记
  ``cpa_redraw``——这批字节确实是重绘，把它记成截图就是伪造。
"""

from __future__ import annotations

import json
import copy
import re
from collections.abc import Mapping
from pathlib import Path

from src.autoslice.cover_polish_gate import _polish_face_binding_failure
from src.autoslice.cover_route_evidence import (
    validate_cover_route_decision,
    validate_rendered_text_pixel_evidence,
)
from src.autoslice.cover_maintenance import _atomic_write_json_file
from src.autoslice.story_contract import cover_story_contract_binding_matches
from src.autoslice.verified_io import _matches_sha256


SCREENSHOT_ROUTE_TREATMENTS = ("screenshot_direct", "screenshot_polish")
SCREENSHOT_REPAIR_TOOL = "scripts/repair_screenshot_cover.py"
DISPLACEMENT_SCHEMA = "cover-repair-route-displacement.v1"
SCREENSHOT_REPAIR_REQUIRED = "COVER_SCREENSHOT_ROUTE_REPAIR_REQUIRED"
_SCREENSHOT_IMAGE_MODELS = {"gpt-image-2", "gpt-image-1.5"}
_SHA256_RX = re.compile(r"sha256:[0-9a-f]{64}\Z")
_SCREENSHOT_FROZEN_GENERATION_KEYS = (
    "workflow",
    "method",
    "model",
    "image_gen_model",
    "attempted_models",
    "fallback_used",
    "model_fallback_used",
    "cover_origin",
    "cover_mode",
    "cover_diversity_slot",
    "cover_text",
    "cover_punch",
    "cover_punch_allowed",
    "art_direction",
    "relation_cover_mode",
    "reference_image",
    "reference_sha256",
    "reference_selection",
    "screenshot_frame",
    "source_composition_receipt",
    "source_composition_verification",
)
_SCREENSHOT_FROZEN_ROUTE_KEYS = (
    "schema_version",
    "selected_treatment",
    "reason",
    "selected_rationale",
    "selection_policy",
    "cover_mode",
    "subject_confident",
    "verified_stream_frame",
    "thumbnail_text_requires_punch",
    "required_participant_ids",
    "source_visible_participant_ids",
    "source_visibility_authority",
    "relationship_visual_required",
    "relationship_visual_safety_evidence",
    "relationship_semantic_evidence",
    "host_identity_required",
    "image_generation_planned",
    "alternatives",
    "rejected_alternatives",
)


def screenshot_polish_source_input_sha256(generation: object) -> str | None:
    """Return the hash-bound polished input carried by a screenshot poster."""

    poster = generation.get("screenshot_graphic_poster") if isinstance(generation, Mapping) else None
    transform = poster.get("source_frame_transform") if isinstance(poster, Mapping) else None
    value = transform.get("input_sha256") if isinstance(transform, Mapping) else None
    return value if isinstance(value, str) and _SHA256_RX.fullmatch(value) else None


def _active_cover_generations(
    documents: list[tuple[Path, dict]], prior_generation: object
) -> list[Mapping[str, object]]:
    generations: list[Mapping[str, object]] = []
    if isinstance(prior_generation, Mapping) and prior_generation.get("method"):
        generations.append(prior_generation)
    for _path, document in documents:
        publish_view = (
            document
            if document.get("schema_version") == "shadow-publish-draft.v1"
            else document.get("publish_staging")
        )
        generation = publish_view.get("cover_generation") if isinstance(publish_view, Mapping) else None
        if isinstance(generation, Mapping) and generation.get("method"):
            generations.append(generation)
    return generations


def validate_screenshot_route_authority(
    generation: Mapping[str, object],
    *,
    documents: list[tuple[Path, dict]],
    prior_generation: object = None,
    title: str,
) -> None:
    """Keep a new screenshot repair bound to the active production authority.

    Route execution state and new pixels are intentionally mutable.  The source
    frame/reference, model chain, selected route, and host requirement are not.
    """

    if generation.get("method") != "screenshot_polish":
        return
    authorities = _active_cover_generations(documents, prior_generation)
    if not authorities:
        raise ValueError("screenshot repair has no active screenshot_polish authority")
    if any(candidate.get("method") != "screenshot_polish" for candidate in authorities):
        raise ValueError("active cover authority mixes screenshot and non-screenshot routes")
    if any(
        isinstance(candidate.get("route_decision"), Mapping)
        and candidate["route_decision"].get("actual_treatment") == "cpa_redraw"
        for candidate in authorities
    ):
        raise ValueError("active screenshot authority was already produced by cpa_redraw")
    authority = authorities[0]
    for candidate in authorities[1:]:
        for key in _SCREENSHOT_FROZEN_GENERATION_KEYS:
            if candidate.get(key) != authority.get(key):
                raise ValueError(f"active screenshot authority disagrees on {key}")
        authority_route = authority.get("route_decision")
        candidate_route = candidate.get("route_decision")
        if not isinstance(authority_route, Mapping) or not isinstance(candidate_route, Mapping):
            raise ValueError("active screenshot authority has incomplete route evidence")
        for key in _SCREENSHOT_FROZEN_ROUTE_KEYS:
            if candidate_route.get(key) != authority_route.get(key):
                raise ValueError(f"active screenshot route authority disagrees on {key}")
    for key in _SCREENSHOT_FROZEN_GENERATION_KEYS:
        if generation.get(key) != authority.get(key):
            raise ValueError(f"screenshot repair changed frozen authority {key}")
    if generation.get("title") != title or authority.get("title") != title:
        raise ValueError("screenshot repair changed frozen title authority")
    source_hash = screenshot_polish_source_input_sha256(generation)
    authority_source_hash = screenshot_polish_source_input_sha256(authority)
    if source_hash is None or source_hash != authority_source_hash:
        raise ValueError("screenshot repair changed frozen polished source input")
    generation_route = generation.get("route_decision")
    authority_route = authority.get("route_decision")
    if not isinstance(generation_route, Mapping) or not isinstance(authority_route, Mapping):
        raise ValueError("screenshot repair has incomplete route evidence")
    for key in _SCREENSHOT_FROZEN_ROUTE_KEYS:
        if generation_route.get(key) != authority_route.get(key):
            raise ValueError(f"screenshot repair changed frozen route authority {key}")


def validate_screenshot_polish_generation(
    *, cover: Path, title: str, candidate_id: str
) -> tuple[dict, Path]:
    """Validate one locally recomposed screenshot-polish generation.

    This is deliberately separate from the paid ``images.edit`` validator: a
    screenshot repair may only carry forward its existing route evidence and
    bind the newly rendered bytes to the existing face/host witnesses.
    """

    manifest_path = cover.with_suffix(".cover_generation.json")
    try:
        document = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"screenshot cover generation manifest is invalid: {exc}") from exc
    if not isinstance(document, dict):
        raise ValueError("screenshot cover generation manifest must be an object")
    if (
        document.get("status") != "AI_COVER_READY"
        or document.get("candidate_id") != candidate_id
        or document.get("title") != title
    ):
        raise ValueError("screenshot cover candidate/title/status binding mismatch")
    model = str(document.get("model") or "")
    attempted_models = document.get("attempted_models")
    model_fallback_used = document.get("model_fallback_used")
    model_chain_valid = bool(
        model in _SCREENSHOT_IMAGE_MODELS
        and document.get("image_gen_model") == model
        and isinstance(attempted_models, list)
        and attempted_models
        and all(isinstance(value, str) for value in attempted_models)
        and len(attempted_models) == len(set(attempted_models))
        and attempted_models[-1] == model
        and isinstance(model_fallback_used, bool)
        and model_fallback_used == (len(attempted_models) > 1)
        and (
            not model_fallback_used
            or attempted_models == ["gpt-image-2", "gpt-image-1.5"]
        )
    )
    if (
        document.get("method") != "screenshot_polish"
        or not model_chain_valid
        or document.get("cover_origin") != "SOURCE_SCREENSHOT_AI_POLISH"
        or document.get("fallback_used") is not False
        or document.get("image_generation_used") is not True
    ):
        raise ValueError("cover generation is not a preserved screenshot_polish result")
    if screenshot_polish_source_input_sha256(document) is None:
        raise ValueError("screenshot polished source input hash is missing or invalid")
    try:
        if Path(str(document.get("final_cover") or "")).resolve(strict=True) != cover.resolve(
            strict=True
        ):
            raise ValueError("screenshot generation final_cover path mismatch")
    except OSError as exc:
        raise ValueError(f"screenshot generation final artifact is missing: {exc}") from exc
    if not _matches_sha256(cover, str(document.get("final_cover_sha256") or "")):
        raise ValueError("screenshot generation final_cover hash mismatch")
    if not validate_cover_route_decision(document, allow_legacy_v1=False):
        raise ValueError("screenshot generation route evidence is invalid")
    rendered_lines = document.get("rendered_lines")
    frame = document.get("screenshot_frame")
    selection = document.get("reference_selection")
    try:
        reference = Path(str(document.get("reference_image") or "")).resolve(strict=True)
    except OSError as exc:
        raise ValueError(f"screenshot reference image is missing: {exc}") from exc
    if not (
        isinstance(rendered_lines, list)
        and bool("".join(str(value) for value in rendered_lines).strip())
        and isinstance(frame, Mapping)
        and isinstance(frame.get("frame_ms"), int)
        and not isinstance(frame.get("frame_ms"), bool)
        and frame.get("frame_ms") >= 0
        and isinstance(selection, Mapping)
        and selection.get("best_ms") == frame.get("frame_ms")
        and _matches_sha256(reference, str(document.get("reference_sha256") or ""))
    ):
        raise ValueError("screenshot frame/reference evidence is incomplete")
    background = Path(str(document.get("ai_background") or ""))
    if not background.is_file() or not _matches_sha256(
        background, str(document.get("ai_background_sha256") or "")
    ):
        raise ValueError("screenshot polished background hash mismatch")
    text_pixels = document.get("rendered_text_pixels")
    pre_overlay = Path(str(document.get("pre_overlay_path") or ""))
    mask_path = (
        Path(str(text_pixels.get("mask_path") or ""))
        if isinstance(text_pixels, Mapping)
        else Path()
    )
    if (
        not pre_overlay.is_file()
        or not _matches_sha256(pre_overlay, str(document.get("pre_overlay_sha256") or ""))
        or not mask_path.is_file()
        or not validate_rendered_text_pixel_evidence(document)
    ):
        raise ValueError("screenshot title-render evidence is incomplete")
    face_detail = _polish_face_binding_failure(
        document, document.get("polish_face_verification"), "screenshot_polish"
    )
    if face_detail is not None:
        raise ValueError(f"screenshot face witness is invalid: {face_detail}")
    return document, manifest_path


def validate_cover_generation_for_binding(
    *,
    cover: Path,
    title: str,
    candidate_id: str,
    documents: list[tuple[Path, dict]] | None = None,
    prior_generation: object = None,
) -> tuple[dict, Path]:
    """Dispatch screenshot validation while retaining the legacy redraw gate."""

    manifest_path = cover.with_suffix(".cover_generation.json")
    try:
        method = json.loads(manifest_path.read_text(encoding="utf-8")).get("method")
    except (OSError, AttributeError, TypeError, ValueError) as exc:
        raise ValueError(f"cover generation manifest is missing or invalid: {exc}") from exc
    if method == "screenshot_polish":
        validated = validate_screenshot_polish_generation(
            cover=cover, title=title, candidate_id=candidate_id
        )
        if documents is not None:
            validate_screenshot_route_authority(
                validated[0],
                documents=documents,
                prior_generation=prior_generation,
                title=title,
            )
        return validated
    # Lazy import keeps this route-lineage helper independent of cover_repair's
    # module initialization while preserving its strict images.edit validator.
    from src.autoslice.cover_repair import _validate_repaired_cover_generation

    return _validate_repaired_cover_generation(
        cover=cover, title=title, candidate_id=candidate_id
    )


def enrich_screenshot_polish_generation(
    *,
    generation: dict,
    generation_path: Path,
    title: str,
    documents: list[tuple[Path, dict]],
    prior_generation: object = None,
) -> tuple[dict, Path]:
    """Carry active StoryContract authority without changing screenshot route evidence."""

    story_contract = active_story_contract(documents)
    enriched = copy.deepcopy(generation)
    if story_contract is not None:
        existing_contract = enriched.get("story_contract")
        if (
            existing_contract is not None
            and not cover_story_contract_binding_matches(story_contract, existing_contract)
        ):
            raise ValueError(
                "active cover StoryContract projection disagrees with screenshot generation"
            )
        enriched["story_contract"] = copy.deepcopy(story_contract)
    validate_screenshot_route_authority(
        enriched,
        documents=documents,
        prior_generation=prior_generation,
        title=title,
    )
    _atomic_write_json_file(generation_path, enriched)
    return validate_screenshot_polish_generation(
        cover=Path(str(enriched["final_cover"])),
        title=title,
        candidate_id=str(enriched["candidate_id"]),
    )


def active_story_contract(documents: list[tuple[Path, dict]]) -> dict | None:
    """Resolve one frozen story contract from the active publication surface."""

    authoritative_contracts: list[dict] = []
    cover_bindings: list[dict] = []
    for _path, document in documents:
        authoritative = document.get("story_contract")
        if isinstance(authoritative, dict) and authoritative not in authoritative_contracts:
            authoritative_contracts.append(authoritative)
        publish_view = (
            document
            if document.get("schema_version") == "shadow-publish-draft.v1"
            else document.get("publish_staging")
        )
        if isinstance(publish_view, dict):
            generation = publish_view.get("cover_generation")
            if isinstance(generation, dict):
                binding = generation.get("story_contract")
                if isinstance(binding, dict) and binding not in cover_bindings:
                    cover_bindings.append(binding)
    if not authoritative_contracts and not cover_bindings:
        return None
    if len(authoritative_contracts) > 1:
        raise ValueError("active cover documents disagree on story contract")
    if authoritative_contracts:
        authority = authoritative_contracts[0]
        if any(
            not cover_story_contract_binding_matches(authority, binding)
            for binding in cover_bindings
        ):
            raise ValueError("active cover StoryContract projection disagrees with authority")
        return copy.deepcopy(authority)
    if len(cover_bindings) != 1:
        raise ValueError("active cover documents disagree on story contract")
    return copy.deepcopy(cover_bindings[0])


def prior_cover_route_treatment(generation: object) -> str:
    """Read the route a cover's pixels were **actually produced under**.

    键在 ``actual_treatment`` 而不是 ``selected_treatment``：一条被降级过的封面
    选的是截图、产出的是重绘，那份字节里没有可保留的截图。``actual_treatment``
    缺席（初始 v1 证据、或执行态还是 PENDING/BLOCKED）时退回 selected，只有两者
    都指向截图路线才算数。
    """

    if not isinstance(generation, Mapping):
        return ""
    route = generation.get("route_decision")
    if not isinstance(route, Mapping):
        return ""
    actual = str(route.get("actual_treatment") or "")
    if actual:
        # polish 被拒后降级成 direct 的成品仍然是**真截图**，必须算数；
        # 只有 actual 本身是 cpa_redraw 才说明那份字节里没有截图可保留。
        return actual
    selected = str(route.get("selected_treatment") or "")
    return selected if route.get("execution_status") in (None, "READY") else ""


def recoverable_screenshot_pixels(generation: Mapping[str, object]) -> Path | None:
    """The on-disk screenshot artifact a repair could recompose from.

    ``ai_background`` 是截图路线唯一被落进 generation 的路径（海报底板；
    `cover_polish_gate` 的 polish 回执只记 status，不记 polished 路径）。海报的
    同目录兄弟 ``*.screenshot-polished.png`` / ``*.screenshot-base.png`` 才是
    `scripts/repair_screenshot_cover.py --polished` 真正要吃的输入，所以优先报它们，
    都不在时退回海报——三者任一存在都证明这条截图的像素链还在盘上。
    """

    poster = Path(str(generation.get("ai_background") or ""))
    if not str(generation.get("ai_background") or ""):
        return None
    stem = poster.name.split(".")[0]
    for name in (
        f"{stem}.screenshot-polished.png",
        f"{stem}.screenshot-base.png",
    ):
        sibling = poster.with_name(name)
        if sibling.is_file():
            return sibling
    return poster if poster.is_file() else None


def recoverable_screenshot_route(rec: object) -> tuple[str, Path] | None:
    """Return (prior treatment, recomposable pixels) for a screenshot cover.

    权威是**活动记录自己的** ``cover_generation``（初始生产与历次返修都写在这里，
    见 `cover_repair._initial_cover_proof_valid` 读的同一处）。盘上的
    ``<cover>.cover_generation.json`` 只有 `scripts/regenerate_channel_cover.py`
    会写，是重绘 lane 的产物——拿它当权威，初次返修一条截图封面时它根本不存在，
    这道门就永远不会触发。
    """

    if not isinstance(rec, Mapping):
        return None
    generation = rec.get("cover_generation")
    treatment = prior_cover_route_treatment(generation)
    if treatment not in SCREENSHOT_ROUTE_TREATMENTS:
        return None
    assert isinstance(generation, Mapping)
    pixels = recoverable_screenshot_pixels(generation)
    return None if pixels is None else (treatment, pixels)


def refuse_recoverable_screenshot_route(rec: object) -> None:
    """Fail closed, cost-free, before a generic redraw displaces a screenshot."""

    recoverable = recoverable_screenshot_route(rec)
    if recoverable is None:
        return
    raise ValueError(
        f"{SCREENSHOT_REPAIR_REQUIRED}: the displaced route is "
        f"{recoverable[0]} and its hash-bound pixels are still at "
        f"{recoverable[1]}; screenshot repairs go through "
        f"{SCREENSHOT_REPAIR_TOOL}. A generic CPA redraw must not silently "
        "convert an already-selected screenshot cover into a redraw."
    )


def record_screenshot_route_displacement(
    enriched: dict, prior_generation: object
) -> str:
    """Disclose a displaced screenshot route; return the rationale suffix.

    ``prior_generation`` 必须是**返修之前**那份 cover generation（活动记录里的
    ``rec["cover_generation"]``）。传新生成的 manifest 没有意义：它是重绘 lane 刚
    写出来的，`route_decision` 永远是 cpa_redraw，披露永远不会触发。
    """

    prior_treatment = prior_cover_route_treatment(prior_generation)
    if prior_treatment not in SCREENSHOT_ROUTE_TREATMENTS:
        return ""
    enriched["cover_repair_route_displacement"] = {
        "schema_version": DISPLACEMENT_SCHEMA,
        "prior_selected_treatment": prior_treatment,
        "replaced_with": "cpa_redraw",
        "reason_code": "SCREENSHOT_SOURCE_PIXELS_UNAVAILABLE",
        "screenshot_repair_tool": SCREENSHOT_REPAIR_TOOL,
    }
    return (
        f"; displaced route {prior_treatment} could not be preserved because "
        "its screenshot pixels are no longer on disk"
    )
