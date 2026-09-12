from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest
from PIL import Image, ImageFont

import src.autoslice.cover_text_pixel_evidence as pixel_evidence
from src.autoslice.cover_route_evidence import (
    validate_rendered_text_pixel_evidence,
)
from src.autoslice.cover_text_pixel_evidence import (
    verify_pre_overlay_route_background,
    verify_rendered_text_pixel_artifacts,
)
from src.autoslice.cover_title_rendering import (
    CoverTitleRenderError,
    LAYOUT_ENGINE_BASIC,
    SCHEMA_VERSION,
    render_spec_sha256,
    render_title_layer,
    sha256_file,
)


ROOT = Path(__file__).resolve().parents[2]
FONT = ROOT / "assets/lidousha/fonts/ZCOOLKuaiLe-Regular.ttf"


def _spec(text: str = "真实标题") -> dict[str, object]:
    return {
        "schema_version": SCHEMA_VERSION,
        "layout_engine": LAYOUT_ENGINE_BASIC,
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
    layer = pixel_evidence.render_title_layer(spec, font_path=FONT)
    with Image.open(pre_overlay) as source:
        final = source.convert("RGB")
    final.paste(layer, (300, 100), layer)
    final.save(final_cover)
    evidence = pixel_evidence.materialize_rendered_text_pixel_evidence(
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


def test_v3_declares_basic_layout_engine_and_rejects_tampering(
    tmp_path: Path,
) -> None:
    _route, pre, final, evidence = _materialize(tmp_path)
    render_spec = evidence["render_spec"]
    assert isinstance(render_spec, dict)
    assert render_spec["layout_engine"] == LAYOUT_ENGINE_BASIC

    tampered = deepcopy(evidence)
    tampered_spec = tampered["render_spec"]
    assert isinstance(tampered_spec, dict)
    tampered_spec["layout_engine"] = "raqm"
    tampered["render_spec_sha256"] = render_spec_sha256(tampered_spec)
    assert not verify_rendered_text_pixel_artifacts(
        tampered,
        final_cover_path=final,
        pre_overlay_path=pre,
        mask_path=Path(str(evidence["mask_path"])),
        font_path=FONT,
        expected_pre_overlay_sha256=evidence["pre_overlay_sha256"],
    )


def test_legacy_v3_basic_artifact_remains_replayable(
    tmp_path: Path,
) -> None:
    _route, pre, final, evidence = _materialize(tmp_path)
    legacy = deepcopy(evidence)
    legacy_spec = legacy["render_spec"]
    assert isinstance(legacy_spec, dict)
    del legacy_spec["layout_engine"]
    legacy["render_spec_sha256"] = render_spec_sha256(legacy_spec)
    assert verify_rendered_text_pixel_artifacts(
        legacy,
        final_cover_path=final,
        pre_overlay_path=pre,
        mask_path=Path(str(evidence["mask_path"])),
        font_path=FONT,
        expected_pre_overlay_sha256=evidence["pre_overlay_sha256"],
    )


def test_declared_basic_layout_rejects_conflicting_override() -> None:
    with pytest.raises(
        CoverTitleRenderError,
        match="COVER_TITLE_RENDER_LAYOUT_ENGINE_MISMATCH",
    ):
        render_title_layer(
            _spec(),
            font_path=FONT,
            layout_engine=ImageFont.Layout.RAQM,
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
