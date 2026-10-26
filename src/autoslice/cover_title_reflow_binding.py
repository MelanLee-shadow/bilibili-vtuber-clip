"""Bind deterministic title reflow to the actual rejected pixels and witness."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path

from .cover_generation import LidoushaCoverArtDirection, _CoverFontChoice, _cover_font_for_text
from .cover_text_pixel_evidence import (
    verify_pre_overlay_route_background,
    verify_rendered_text_pixel_artifacts,
)
from .normal_cover_generation_checkpoint import file_identity


def _bound(path, digest):
    path = Path(str(path or ""))
    if file_identity(path).get("sha256") != digest or not digest:
        raise ValueError("COVER_TITLE_REFLOW_INPUT_DRIFT")
    return path


def reflow_title_occlusion(
    generation: dict, *, output_path: Path, full_text_cover_contract=None
) -> bool:
    """Propose different title pixels, never a new identity/content verdict.

    False means the witness did not identify this narrowly supported class.
    An admitted but unrenderable title remains blocked without redrawing a
    background which the witness already found suitable.
    """
    from .cover_title_reflow import title_only_occlusion, render_title_reflow

    old = generation.get("final_host_identity_verification") or {}
    if not title_only_occlusion(old):
        return False
    final = _bound(generation.get("final_cover"), generation.get("final_cover_sha256"))
    if old.get("final_cover_sha256") != generation["final_cover_sha256"] or old.get(
        "final_cover_path"
    ) != str(final):
        raise ValueError("COVER_TITLE_REFLOW_WITNESS_DRIFT")
    _bound(old.get("reference_path"), old.get("reference_sha256"))
    _bound(old.get("comparison_path"), old.get("comparison_sha256"))
    background = _bound(generation.get("ai_background"), generation.get("ai_background_sha256"))
    pre = _bound(generation.get("pre_overlay_path"), generation.get("pre_overlay_sha256"))
    pixels = generation.get("rendered_text_pixels") or {}
    mask = _bound(pixels.get("mask_path"), pixels.get("mask_sha256"))
    direction = LidoushaCoverArtDirection(**generation["art_direction"])
    font = _cover_font_for_text(pixels.get("rendered_text", ""), title_style=direction.title_style)
    if not verify_pre_overlay_route_background(
        route_background_path=background,
        pre_overlay_path=pre,
        expected_route_background_sha256=generation["ai_background_sha256"],
        text_backing=generation.get("text_backing"),
        scrim=generation.get("scrim"),
    ) or not verify_rendered_text_pixel_artifacts(
        pixels,
        final_cover_path=final,
        pre_overlay_path=pre,
        mask_path=mask,
        font_path=font.path if isinstance(font, _CoverFontChoice) else font,
        expected_pre_overlay_sha256=generation["pre_overlay_sha256"],
    ):
        raise ValueError("COVER_TITLE_REFLOW_PIXEL_PROOF_INVALID")
    prior = {
        key: deepcopy(generation.get(key))
        for key in (
            "final_cover",
            "final_cover_sha256",
            "pre_overlay_path",
            "pre_overlay_sha256",
            "rendered_text_pixels",
            "final_host_identity_verification",
        )
    }
    try:
        overlay = render_title_reflow(
            background,
            output_path,
            cover_text=generation["cover_text"],
            art_direction=direction,
            full_text_cover_contract=full_text_cover_contract,
        )
    except ValueError as exc:
        generation["title_occlusion_reflow"] = {
            "status": "BLOCKED",
            "reason_code": str(exc),
            "new_image_calls": 0,
            "previous": prior,
            "background_sha256": generation["ai_background_sha256"],
        }
        return True
    generation.update(overlay)
    generation.update(
        final_cover=str(output_path), final_cover_sha256=file_identity(output_path)["sha256"]
    )
    generation["title_occlusion_reflow"] = {
        "status": "PENDING_CURRENT_IDENTITY",
        "new_image_calls": 0,
        "background_sha256": generation["ai_background_sha256"],
        "previous": prior,
        "proposal": deepcopy(overlay.get("title_reflow_proposal")),
    }
    # Keeping the old FAIL as the active verdict would confuse it with the new
    # pixels. Preserve it above, and require a fresh real witness next.
    generation["final_host_identity_verification"] = {
        "status": "PENDING",
        "reason_code": "CURRENT_IDENTITY_PROVIDER_REQUIRED",
        "final_cover_sha256": generation["final_cover_sha256"],
    }
    return True
