"""Replayable glyph and exact-composition evidence for final cover pixels."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Mapping

from PIL import Image, ImageChops, ImageOps

from src.autoslice.cover_title_rendering import (
    CoverTitleRenderError,
    render_spec_sha256,
    render_title_layer,
    sha256_file,
)


SCHEMA_VERSION = "lidousha-cover-rendered-text-pixels.v3"


def verify_pre_overlay_route_background(
    *,
    route_background_path: Path,
    pre_overlay_path: Path,
    expected_route_background_sha256: object,
    text_backing: object,
    scrim: object,
) -> bool:
    """Replay the current no-card background normalization exactly."""

    if (
        text_backing != "outline"
        or scrim is not False
        or not route_background_path.is_file()
        or not pre_overlay_path.is_file()
    ):
        return False
    try:
        if (
            expected_route_background_sha256
            != sha256_file(route_background_path)
        ):
            return False
        with (
            Image.open(route_background_path) as route_background,
            Image.open(pre_overlay_path) as pre_overlay,
        ):
            expected = ImageOps.fit(
                route_background.convert("RGB"),
                (1920, 1080),
                method=Image.Resampling.LANCZOS,
            )
            actual = pre_overlay.convert("RGB")
    except (OSError, Image.DecompressionBombError):
        return False
    return ImageChops.difference(expected, actual).getbbox() is None


def _masked_final_sha256(final_image: Image.Image, mask: Image.Image) -> str:
    rgb = final_image.convert("RGB")
    black = Image.new("RGB", rgb.size)
    masked = Image.composite(rgb, black, mask)
    return "sha256:" + hashlib.sha256(masked.tobytes()).hexdigest()


def _changed_pixel_count(
    final_image: Image.Image,
    pre_overlay: Image.Image,
    mask: Image.Image,
) -> int:
    difference = ImageChops.difference(
        final_image.convert("RGB"), pre_overlay.convert("RGB")
    )
    red, green, blue = difference.split()
    any_channel = ImageChops.lighter(ImageChops.lighter(red, green), blue)
    changed_under_mask = Image.composite(
        any_channel,
        Image.new("L", any_channel.size),
        mask.convert("L"),
    )
    return sum(changed_under_mask.histogram()[1:])


def materialize_rendered_text_pixel_evidence(
    *,
    final_cover_path: Path,
    pre_overlay_path: Path,
    font_path: Path,
    render_spec: Mapping[str, object],
    paste_xy: tuple[int, int],
    font_size: int,
    rendered_text: str,
) -> dict[str, object]:
    """Regenerate glyphs, persist their mask, and prove exact recomposition."""

    canonical_spec = json.loads(
        json.dumps(
            render_spec,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    title_layer = render_title_layer(canonical_spec, font_path=font_path)
    raw_lines = canonical_spec.get("lines")
    spec_text = "".join(
        str(segment.get("text") or "")
        for line in raw_lines
        if isinstance(line, Mapping)
        for segment in line.get("segments") or []
        if isinstance(segment, Mapping)
    )
    spec_font_sizes = [
        line.get("font_size")
        for line in raw_lines
        if isinstance(line, Mapping)
    ]
    if (
        spec_text != rendered_text
        or not spec_font_sizes
        or any(
            isinstance(value, bool) or not isinstance(value, int)
            for value in spec_font_sizes
        )
        or max(spec_font_sizes) != font_size
    ):
        raise ValueError("COVER_RENDERED_TEXT_SPEC_BINDING_INVALID")
    with Image.open(pre_overlay_path) as pre_overlay:
        pre_overlay.load()
        canvas_size = pre_overlay.size
    with Image.open(final_cover_path) as final_image:
        final_image.load()
        if final_image.size != canvas_size:
            raise ValueError("COVER_RENDERED_TEXT_CANVAS_MISMATCH")
    if (
        canvas_size != (1920, 1080)
        or any(
            isinstance(value, bool) or not isinstance(value, int)
            for value in paste_xy
        )
    ):
        raise ValueError("COVER_RENDERED_TEXT_POSITION_INVALID")
    alpha_bbox = title_layer.getchannel("A").getbbox()
    if (
        alpha_bbox is None
        or paste_xy[0] + alpha_bbox[0] < 0
        or paste_xy[1] + alpha_bbox[1] < 0
        or paste_xy[0] + alpha_bbox[2] > canvas_size[0]
        or paste_xy[1] + alpha_bbox[3] > canvas_size[1]
    ):
        raise ValueError("COVER_RENDERED_TEXT_POSITION_INVALID")
    mask = Image.new("L", canvas_size, 0)
    mask.paste(title_layer.getchannel("A"), paste_xy)
    mask_path = final_cover_path.with_name(
        final_cover_path.name.removesuffix(".png") + ".title-mask.png"
    )
    mask.save(mask_path)
    bbox = mask.getbbox()
    bbox_list = list(bbox) if bbox is not None else None
    nonzero = sum(mask.histogram()[1:])
    with (
        Image.open(final_cover_path) as final_image,
        Image.open(pre_overlay_path) as pre_overlay,
    ):
        final_image.load()
        pre_overlay.load()
        masked_final_sha256 = _masked_final_sha256(final_image, mask)
        changed_pixel_count = _changed_pixel_count(
            final_image, pre_overlay, mask
        )
    changed_ratio = changed_pixel_count / nonzero if nonzero else 0.0
    evidence = {
        "schema_version": SCHEMA_VERSION,
        "status": "PASS" if bbox is not None and nonzero > 0 else "FAIL",
        "final_cover_sha256": sha256_file(final_cover_path),
        "pre_overlay_path": str(pre_overlay_path),
        "pre_overlay_sha256": sha256_file(pre_overlay_path),
        "canvas_size": list(canvas_size),
        "text_pixel_bbox": bbox_list,
        "text_pixel_width": (
            bbox_list[2] - bbox_list[0] if bbox_list is not None else 0
        ),
        "text_pixel_height": (
            bbox_list[3] - bbox_list[1] if bbox_list is not None else 0
        ),
        "font_size": font_size,
        "font_file_name": font_path.name,
        "font_file_sha256": sha256_file(font_path),
        "rendered_text": rendered_text,
        "render_spec": canonical_spec,
        "render_spec_sha256": render_spec_sha256(canonical_spec),
        "overlay_position": {"x": paste_xy[0], "y": paste_xy[1]},
        "mask_path": str(mask_path),
        "mask_sha256": sha256_file(mask_path),
        "mask_nonzero_pixels": nonzero,
        "changed_pixel_count": changed_pixel_count,
        "changed_pixel_ratio": changed_ratio,
        "masked_final_pixels_sha256": masked_final_sha256,
    }
    if not verify_rendered_text_pixel_artifacts(
        evidence,
        final_cover_path=final_cover_path,
        pre_overlay_path=pre_overlay_path,
        mask_path=mask_path,
        font_path=font_path,
        expected_pre_overlay_sha256=evidence["pre_overlay_sha256"],
    ):
        raise ValueError("COVER_RENDERED_TEXT_PIXEL_RECOMPOSITION_FAILED")
    return evidence


def verify_rendered_text_pixel_artifacts(
    evidence: Mapping[str, object],
    *,
    final_cover_path: Path,
    pre_overlay_path: Path,
    mask_path: Path,
    font_path: Path,
    expected_pre_overlay_sha256: object,
) -> bool:
    """Replay trusted glyph rendering and exactly recompose the final cover."""

    render_spec = evidence.get("render_spec")
    if (
        evidence.get("schema_version") != SCHEMA_VERSION
        or evidence.get("status") != "PASS"
        or not isinstance(render_spec, Mapping)
        or not final_cover_path.is_file()
        or not pre_overlay_path.is_file()
        or not mask_path.is_file()
        or not isinstance(expected_pre_overlay_sha256, str)
    ):
        return False
    try:
        if (
            evidence.get("final_cover_sha256")
            != sha256_file(final_cover_path)
            or evidence.get("mask_sha256") != sha256_file(mask_path)
            or evidence.get("font_file_name") != font_path.name
            or evidence.get("font_file_sha256") != sha256_file(font_path)
            or evidence.get("render_spec_sha256")
            != render_spec_sha256(render_spec)
            or evidence.get("pre_overlay_sha256")
            != sha256_file(pre_overlay_path)
            or expected_pre_overlay_sha256
            != evidence.get("pre_overlay_sha256")
        ):
            return False
        title_layer = render_title_layer(render_spec, font_path=font_path)
        raw_lines = render_spec.get("lines")
        spec_text = "".join(
            str(segment.get("text") or "")
            for line in raw_lines
            if isinstance(line, Mapping)
            for segment in line.get("segments") or []
            if isinstance(segment, Mapping)
        )
        spec_font_sizes = [
            line.get("font_size")
            for line in raw_lines
            if isinstance(line, Mapping)
        ]
        with (
            Image.open(final_cover_path) as final_image,
            Image.open(pre_overlay_path) as pre_overlay,
            Image.open(mask_path) as mask_image,
        ):
            final_image.load()
            pre_overlay.load()
            mask_image.load()
            final_rgb = final_image.convert("RGB")
            pre_rgb = pre_overlay.convert("RGB")
            mask = mask_image.convert("L")
    except (
        OSError,
        OverflowError,
        ValueError,
        CoverTitleRenderError,
        Image.DecompressionBombError,
    ):
        return False
    if (
        final_rgb.size != (1920, 1080)
        or pre_rgb.size != final_rgb.size
        or mask.size != final_rgb.size
    ):
        return False
    position = evidence.get("overlay_position")
    if (
        not isinstance(position, Mapping)
        or isinstance(position.get("x"), bool)
        or not isinstance(position.get("x"), int)
        or isinstance(position.get("y"), bool)
        or not isinstance(position.get("y"), int)
    ):
        return False
    paste_xy = (int(position["x"]), int(position["y"]))
    alpha_bbox = title_layer.getchannel("A").getbbox()
    if (
        alpha_bbox is None
        or paste_xy[0] + alpha_bbox[0] < 0
        or paste_xy[1] + alpha_bbox[1] < 0
        or paste_xy[0] + alpha_bbox[2] > final_rgb.width
        or paste_xy[1] + alpha_bbox[3] > final_rgb.height
        or spec_text != evidence.get("rendered_text")
        or not spec_font_sizes
        or any(
            isinstance(value, bool) or not isinstance(value, int)
            for value in spec_font_sizes
        )
        or max(spec_font_sizes) != evidence.get("font_size")
    ):
        return False
    expected_mask = Image.new("L", final_rgb.size, 0)
    expected_mask.paste(title_layer.getchannel("A"), paste_xy)
    if ImageChops.difference(mask, expected_mask).getbbox() is not None:
        return False
    bbox = mask.getbbox()
    bbox_list = list(bbox) if bbox is not None else None
    nonzero = sum(mask.histogram()[1:])
    changed_pixel_count = _changed_pixel_count(final_rgb, pre_rgb, mask)
    changed_ratio = changed_pixel_count / nonzero if nonzero else 0.0
    if (
        bbox_list != evidence.get("text_pixel_bbox")
        or evidence.get("canvas_size") != list(final_rgb.size)
        or evidence.get("text_pixel_width")
        != (bbox_list[2] - bbox_list[0] if bbox_list is not None else 0)
        or evidence.get("text_pixel_height")
        != (bbox_list[3] - bbox_list[1] if bbox_list is not None else 0)
        or nonzero != evidence.get("mask_nonzero_pixels")
        or nonzero < 100
        or changed_pixel_count != evidence.get("changed_pixel_count")
        or not isinstance(evidence.get("changed_pixel_ratio"), (int, float))
        or isinstance(evidence.get("changed_pixel_ratio"), bool)
        or abs(
            float(evidence.get("changed_pixel_ratio")) - changed_ratio
        )
        > 1e-12
        or changed_pixel_count < 100
        or changed_ratio < 0.10
        or evidence.get("masked_final_pixels_sha256")
        != _masked_final_sha256(final_rgb, mask)
    ):
        return False
    expected_final = pre_rgb.copy()
    expected_final.paste(title_layer, paste_xy, title_layer)
    return ImageChops.difference(expected_final, final_rgb).getbbox() is None
