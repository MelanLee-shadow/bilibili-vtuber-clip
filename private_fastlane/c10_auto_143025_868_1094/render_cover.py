#!/usr/bin/env python3
"""Deterministically materialize C10's private, textless-background cover.

This helper is deliberately package-local: it is not a production cover route,
does not read state, and cannot upload or mutate a remote host.
"""
from __future__ import annotations

import json
from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from PIL import Image, ImageDraw, ImageFont, ImageOps

from src.autoslice.cover_text_pixel_evidence import (
    materialize_rendered_text_pixel_evidence,
    verify_rendered_text_pixel_artifacts,
)
from src.autoslice.cover_title_rendering import (
    TITLE_HOOK_FILLS,
    TITLE_INNER_STROKE,
    TITLE_OUTER_STROKE,
    materialize_title_layer_spec,
)


ROOT = Path(__file__).resolve().parent
FONT = ROOT.parents[1] / "assets/lidousha/fonts/ZCOOLKuaiLe-Regular.ttf"
BACKGROUND = ROOT / "cover/route-background-v2.png"
FINAL = ROOT / "cover/auto_143025_868_1094.private.cover-v2.png"
EVIDENCE = ROOT / "cover/rendered-text-pixels.v3.json"
LINES = ("前辈写的", "现在写不出")


def main() -> None:
    background = ImageOps.fit(
        Image.open(BACKGROUND).convert("RGB"),
        (1920, 1080),
        method=Image.Resampling.LANCZOS,
    )
    font_size = 180
    font = ImageFont.truetype(str(FONT), font_size)
    probe = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    outlines = [
        {"width": max(8, round(font_size * 0.085)), "color": list(TITLE_OUTER_STROKE)},
        {"width": max(4, round(font_size * 0.042)), "color": list(TITLE_INNER_STROKE)},
    ]
    rows = []
    for position, text in enumerate(LINES):
        box = probe.textbbox((0, 0), text, font=font, stroke_width=outlines[0]["width"])
        rows.append(
            {
                "font_size": font_size,
                "gap": 18 if position == 0 else 0,
                "width": box[2] - box[0],
                "height": sum(font.getmetrics()),
                "segments": [{"text": text, "fill": list(TITLE_HOOK_FILLS[position])}],
                "outlines": outlines,
            }
        )
    layer, spec = materialize_title_layer_spec(
        font_path=FONT,
        font_face_index=0,
        layer_size=(900, 520),
        angle_degrees=0.0,
        render_lines=rows,
        top_pad=42,
    )
    pre_overlay = FINAL.with_name(FINAL.name.removesuffix(".png") + ".pre-overlay.png")
    background.save(pre_overlay)
    paste_xy = ((1920 - layer.width) // 2, 92)
    final = background.convert("RGBA")
    final.paste(layer, paste_xy, layer)
    final.convert("RGB").save(FINAL)
    evidence = materialize_rendered_text_pixel_evidence(
        final_cover_path=FINAL,
        pre_overlay_path=pre_overlay,
        font_path=FONT,
        render_spec=spec,
        paste_xy=paste_xy,
        font_size=font_size,
        rendered_text="".join(LINES),
    )
    mask = Path(str(evidence["mask_path"]))
    if not verify_rendered_text_pixel_artifacts(
        evidence,
        final_cover_path=FINAL,
        pre_overlay_path=pre_overlay,
        mask_path=mask,
        font_path=FONT,
        expected_pre_overlay_sha256=evidence["pre_overlay_sha256"],
    ):
        raise ValueError("private cover replay failed")
    EVIDENCE.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
