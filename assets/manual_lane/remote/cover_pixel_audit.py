"""Repaint the skill's measured glyphs, outline and shadow onto the supplied background.

This is a local pixel check for the manual lane, not a CPA verdict or the portable package's independent audit.
"""

import hashlib

from PIL import Image, ImageChops, ImageDraw, ImageFilter


def _sha_pixels(image):
    return hashlib.sha256(image.tobytes()).hexdigest()


def audit(renderer, spec, rows, font, result):
    """Return the background, full glyph/shadow mask and a receipt, or refuse inconsistent or unsafe pixels."""
    width, height = renderer.W, renderer.H
    if result.size != (width, height) or len(rows) != len(spec["lines"]) or not rows:
        raise ValueError("PIXEL_AUDIT_INPUT_MISMATCH")
    background = renderer.background(spec).convert("RGBA")
    if rows[0]["layout"] == "bottom" and spec.get("banner"):
        band = Image.new("RGBA", (width, height))
        ImageDraw.Draw(band).rectangle((0, max(0, rows[0]["y"] - 28), width, height), fill=renderer.BANNER)
        background.alpha_composite(band)
    rebuilt = background.copy()
    text_layer = Image.new("RGBA", (width, height))
    for source, row in zip(spec["lines"], rows, strict=True):
        if source["text"] != row["text"]:
            raise ValueError("PIXEL_AUDIT_TEXT_MISMATCH")
        glyph = renderer.line_layer(font, row["text"], row["size"], row["color"], row["stroke"])
        glyph = glyph.rotate(row["tilt"], resample=Image.Resampling.BICUBIC, expand=True)
        ink = glyph.getbbox()
        if not ink or [ink[2] - ink[0], ink[3] - ink[1]] != [row["w"], row["h"]]:
            raise ValueError("PIXEL_AUDIT_GLYPH_MISMATCH")
        x, y = row["x"] - ink[0], row["y"] - ink[1]
        shadow = Image.new("RGBA", glyph.size)
        shadow.putalpha(glyph.getchannel("A").point(lambda a: int(a * 0.4)))
        shadow = shadow.filter(ImageFilter.GaussianBlur(10))
        for part, px, py in ((glyph, x, y), (shadow, x + 9, y + 13)):
            left, top, right, bottom = part.getbbox()
            if px + left < 0 or py + top < 0 or px + right > width or py + bottom > height:
                raise ValueError("PIXEL_AUDIT_OUTSIDE_CANVAS")
        for canvas in (rebuilt, text_layer):
            canvas.alpha_composite(shadow, (x + 9, y + 13))
            canvas.alpha_composite(glyph, (x, y))
    mask = text_layer.getchannel("A")
    box = mask.getbbox()
    if not box or box[0] < renderer.SAFE_X0 or box[2] > renderer.SAFE_X1:
        raise ValueError("PIXEL_AUDIT_OUTSIDE_SAFE_ZONE")
    if ImageChops.difference(rebuilt.convert("RGB"), result.convert("RGB")).getbbox() is not None:
        raise ValueError("PIXEL_AUDIT_RECOMPOSITION_MISMATCH")
    with open(font, "rb") as handle:
        font_sha256 = hashlib.sha256(handle.read()).hexdigest()
    return background, mask, {"status": "PASS", "canvas": [width, height], "text_pixel_bbox": list(box),
                              "font_sha256": font_sha256, "background_rgb_sha256": _sha_pixels(background.convert("RGB")),
                              "mask_pixel_sha256": _sha_pixels(mask), "final_rgb_sha256": _sha_pixels(result.convert("RGB")),
                              "scope": "manual-lane skill pixel replay; not the portable package audit"}
