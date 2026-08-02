from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest
from PIL import Image

from src.autoslice.cover_route_evidence import (
    validate_rendered_text_pixel_evidence,
)
from src.autoslice.cover_text_pixel_evidence import (
    materialize_rendered_text_pixel_evidence,
    verify_pre_overlay_route_background,
    verify_rendered_text_pixel_artifacts,
)
from src.autoslice.cover_title_rendering import (
    CoverTitleRenderError,
    SCHEMA_VERSION,
    render_title_layer,
    sha256_file,
)


ROOT = Path(__file__).resolve().parents[2]
FONT = ROOT / "assets/lidousha/fonts/ZCOOLKuaiLe-Regular.ttf"


def _spec(text: str = "真实标题") -> dict[str, object]:
    return {
        "schema_version": SCHEMA_VERSION,
        "font_file_name": FONT.name,
        "font_file_sha256": sha256_file(FONT),
        "font_face_index": 0,
        "layer_size": [1000, 300],
        "rendered_layer_size": [1000, 300],
        "angle_degrees": 0,
        "lines": [
            {
                "x": 30,
                "y": 30,
                "font_size": 180,
                "segment_pad": 15,
                "segments": [
                    {"text": text, "fill": [255, 198, 41]}
                ],
                "outlines": [
                    {"width": 15, "color": [18, 36, 79]},
                    {"width": 8, "color": [255, 255, 255]},
                ],
            }
        ],
    }


def _materialize(
    tmp_path: Path,
) -> tuple[Path, Path, Path, dict[str, object]]:
    route_background = tmp_path / "route.png"
    pre_overlay = tmp_path / "pre.png"
    final_cover = tmp_path / "final.png"
    Image.new("RGB", (1920, 1080), (230, 220, 200)).save(
        route_background
    )
    Image.open(route_background).save(pre_overlay)
    spec = _spec()
    layer = render_title_layer(spec, font_path=FONT)
    with Image.open(pre_overlay) as source:
        final = source.convert("RGB")
    final.paste(layer, (300, 100), layer)
    final.save(final_cover)
    evidence = materialize_rendered_text_pixel_evidence(
        final_cover_path=final_cover,
        pre_overlay_path=pre_overlay,
        font_path=FONT,
        render_spec=spec,
        paste_xy=(300, 100),
        font_size=180,
        rendered_text="真实标题",
    )
    return route_background, pre_overlay, final_cover, evidence


def test_v3_replays_exact_visible_title_and_route_background(
    tmp_path: Path,
) -> None:
    route, pre, final, evidence = _materialize(tmp_path)
    mask = Path(str(evidence["mask_path"]))
    assert verify_rendered_text_pixel_artifacts(
        evidence,
        final_cover_path=final,
        pre_overlay_path=pre,
        mask_path=mask,
        font_path=FONT,
        expected_pre_overlay_sha256=evidence["pre_overlay_sha256"],
    )
    assert verify_pre_overlay_route_background(
        route_background_path=route,
        pre_overlay_path=pre,
        expected_route_background_sha256=sha256_file(route),
        text_backing="outline",
        scrim=False,
    )


def test_nonfinite_and_clipped_glyph_specs_fail_closed() -> None:
    infinite = _spec()
    infinite["lines"][0]["y"] = float("inf")
    with pytest.raises(CoverTitleRenderError):
        render_title_layer(infinite, font_path=FONT)

    clipped = _spec()
    clipped["lines"][0]["x"] = -670
    with pytest.raises(
        CoverTitleRenderError, match="COVER_TITLE_RENDER_GLYPH_CLIPPED"
    ):
        render_title_layer(clipped, font_path=FONT)


def test_untrusted_invisible_palette_is_rejected() -> None:
    invisible = _spec()
    invisible["lines"][0]["segments"][0]["fill"] = [230, 220, 200]
    invisible["lines"][0]["outlines"] = [
        {"width": 15, "color": [230, 220, 200]},
        {"width": 8, "color": [230, 220, 200]},
    ]
    with pytest.raises(
        CoverTitleRenderError,
        match="COVER_TITLE_RENDER_OUTLINE_POLICY_INVALID",
    ):
        render_title_layer(invisible, font_path=FONT)


def test_feed_crop_and_route_background_drift_are_rejected(
    tmp_path: Path,
) -> None:
    route, pre, final, evidence = _materialize(tmp_path)
    generation = {
        "final_cover_sha256": evidence["final_cover_sha256"],
        "font_size": 180,
        "font_selection": {"font": FONT.name, "glyph_risk": []},
        "angle_degrees": 0,
        "overlay_position": evidence["overlay_position"],
        "rendered_lines": ["真实标题"],
        "rendered_text_pixels": evidence,
    }
    assert validate_rendered_text_pixel_evidence(generation)
    unsafe = deepcopy(generation)
    unsafe_evidence = unsafe["rendered_text_pixels"]
    unsafe_evidence["text_pixel_bbox"] = [0, 100, 500, 300]
    unsafe_evidence["text_pixel_width"] = 500
    assert not validate_rendered_text_pixel_evidence(unsafe)

    Image.new("RGB", (1920, 1080), (1, 2, 3)).save(route)
    assert not verify_pre_overlay_route_background(
        route_background_path=route,
        pre_overlay_path=pre,
        expected_route_background_sha256=sha256_file(route),
        text_backing="outline",
        scrim=False,
    )
