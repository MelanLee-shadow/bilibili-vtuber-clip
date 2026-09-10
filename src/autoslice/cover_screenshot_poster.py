"""Source-led screenshot composition plus replay of explicit legacy poster styles."""

from __future__ import annotations

import math
from dataclasses import replace
from pathlib import Path

from .cover_generation import (
    LidoushaCoverArtDirection, _COVER_CANVAS, _COVER_LAYOUT_RENDER, _sha256,
    _COVER_BASE_FILL, _COVER_HOOK_COLORS, COVER_MIN_TALK_FONT_SIZE,
    _cover_font_for_text, _fit_cover_punch_lines,
)


_SCREENSHOT_POSTER_PALETTES = {
    "cobalt-comic-burst": {
        "background": (17, 38, 103),
        "secondary": (45, 126, 224),
        "accent": (255, 204, 45),
        "paper": (249, 245, 224),
        "rotation": -1.8,
    },
    "warm-scrapbook-collage": {
        "background": (224, 92, 79),
        "secondary": (248, 178, 137),
        "accent": (31, 88, 73),
        "paper": (255, 240, 207),
        "rotation": 1.5,
    },
    "violet-neon-stage": {
        "background": (39, 17, 68),
        "secondary": (125, 35, 160),
        "accent": (42, 228, 226),
        "paper": (245, 226, 255),
        "rotation": -1.0,
    },
    "mint-doodle-stickers": {
        "background": (123, 211, 193),
        "secondary": (228, 246, 212),
        "accent": (241, 123, 73),
        "paper": (255, 247, 220),
        "rotation": 1.2,
    },
    "mono-manga-panels": {
        "background": (34, 32, 31),
        "secondary": (232, 224, 198),
        "accent": (214, 53, 45),
        "paper": (255, 247, 219),
        "rotation": -1.4,
    },
    "coral-checker-pop": {
        "background": (207, 75, 75),
        "secondary": (158, 218, 213),
        "accent": (238, 183, 63),
        "paper": (255, 239, 202),
        "rotation": 1.0,
    },
}


def _is_source_led(direction):
    return not direction.is_song and direction.background_style not in (
        *_SCREENSHOT_POSTER_PALETTES, "pop-art-burst", "halftone-dots", "speed-lines",
        "soft-radial", "clean-scenic",
    )


def _source_region(source_size, title_zone):
    x0, y0, x1, y1 = title_zone
    regions = ((240, 0, x0 - 8, 1080), (x1 + 8, 0, 1680, 1080),
               (240, 0, 1680, y0 - 8), (240, y1 + 8, 1680, 1080))
    def scale(box):
        return min((box[2] - box[0]) / source_size[0], (box[3] - box[1]) / source_size[1])
    return max((box for box in regions if box[2] > box[0] and box[3] > box[1]), key=scale)


def _source_frame_layout(screenshot_path, direction):
    """Keep source pixels large, and fit a compact caption before compositing.

    A landscape frame cannot occupy a narrow side column at the same scale as
    a footer layout. Compare actual source dimensions, not the model's label.
    Explicit historical styles and full-text contracts retain their geometry.
    """
    from PIL import Image

    if not _is_source_led(direction) or not direction.cover_punch:
        return direction, None, None
    with Image.open(screenshot_path) as source:
        size = source.size
    font = _cover_font_for_text("\n".join(direction.cover_punch), title_style=direction.title_style)
    # Find the smallest band meeting the existing font floor, then allow eight
    # pixels of breathing room. Never allocate half the canvas merely because
    # a legacy layout has a large maximum title rectangle.
    lo, hi = 1, 486
    while lo < hi:
        height = (lo + hi) // 2
        lines = _fit_cover_punch_lines(
            direction.cover_punch, zone=(260, 0, 1660, height), font_path=font,
            hook_rgb=_COVER_HOOK_COLORS[direction.hook_color], base_fill=_COVER_BASE_FILL,
            max_size=COVER_MIN_TALK_FONT_SIZE,
        )
        if max(line["size"] for line in lines) >= COVER_MIN_TALK_FONT_SIZE:
            hi = height
        else:
            lo = height + 1
    height = min(486, hi + 8)
    footer_zone = (260, 1080 - height, 1660, 1080)
    requested = direction.layout
    if requested in ("banner", "footer"):
        zone = (260, 0, 1660, height) if requested == "banner" else footer_zone
    else:
        zone = _COVER_LAYOUT_RENDER[requested]["zone"]
        current = _source_region(size, zone)
        fallback = _source_region(size, footer_zone)
        scale = lambda box: min((box[2] - box[0]) / size[0], (box[3] - box[1]) / size[1])
        if scale(current) < scale(fallback):
            direction = replace(direction, layout="footer")
            zone = footer_zone
    return direction, zone, {
        "requested_layout": requested, "selected_layout": direction.layout,
        "source_size": list(size), "title_zone": list(zone),
        "reason": ("side column would shrink the complete source; use the larger footer area"
                   if direction.layout != requested else "compact caption preserves more source pixels"),
    }


def _compose_source_frame(screenshot_path, output_path, *, art_direction, source_ai_modified, title_zone=None):
    """Contain the complete input outside the actual title reservation.

    Geometry, not a decorative family, determines the remaining image area.
    The exact transformed source stays inside the center 4:3 crop and cannot
    be touched by the title layer; relationship proof consumes this same box.
    """
    from PIL import Image, ImageEnhance, ImageOps

    source = Image.open(screenshot_path).convert("RGB")
    x0, y0, x1, y1 = title_zone or _COVER_LAYOUT_RENDER[art_direction.layout]["zone"]
    safe = [240, 0, 1680, 1080]
    region = _source_region(source.size, (x0, y0, x1, y1))
    contained = ImageOps.contain(
        source, (region[2] - region[0], region[3] - region[1]), Image.Resampling.LANCZOS
    )
    offset = ((region[0] + region[2] - contained.width) // 2,
              (region[1] + region[3] - contained.height) // 2)
    # Extend only the source's broad color gradients into the surrounding area,
    # without repeating a blurred face, adding motifs or changing source pixels.
    wash = source.resize((1, 16), Image.Resampling.BOX).resize(_COVER_CANVAS, Image.Resampling.BICUBIC)
    canvas = ImageEnhance.Brightness(wash).enhance(0.38)
    canvas.paste(contained, offset)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(output_path)
    content_box = [offset[0], offset[1], offset[0] + contained.width, offset[1] + contained.height]
    return {
        "schema_version": "screenshot-graphic-poster.v2", "status": "COMPOSED",
        "background_style": art_direction.background_style,
        "composition": "source_frame_with_title_reservation", "palette": {},
        "title_zone": [x0, y0, x1, y1],
        "screenshot_card": {"size": list(contained.size), "rotation_degrees": 0.0},
        "source_frame_transform": {
            "input_sha256": "sha256:" + _sha256(screenshot_path),
            "source_size": list(source.size), "rendered_content_box": content_box,
            "crop_applied": False, "full_frame_preserved": True,
            "face_safe_contain": True, "card_fit": "full_frame",
            "ai_modified": bool(source_ai_modified),
            "center_4_3_box": safe, "center_4_3_safe": True,
        },
        "output_path": str(output_path), "output_sha256": "sha256:" + _sha256(output_path),
    }


def _compose_screenshot_poster_background(
    screenshot_path: Path,
    output_path: Path,
    *,
    art_direction: LidoushaCoverArtDirection,
    preserve_full_frame: bool = False,
    source_ai_modified: bool = False,
    face_safe_contain: bool = False,
    source_title_zone: tuple[int, int, int, int] | None = None,
) -> dict[str, object]:
    """Compose a real frame; explicit historic styles retain their old pixels."""
    from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageOps

    if _is_source_led(art_direction):
        return _compose_source_frame(
            screenshot_path, output_path, art_direction=art_direction,
            source_ai_modified=source_ai_modified, title_zone=source_title_zone,
        )

    if (art_direction.visual_brief and art_direction.title_style == "clean"
            and art_direction.layout == "footer" and not preserve_full_frame):
        # A real frame can carry the cover without another decorative card.
        # Contain the complete authorized crop; only darken the caption area.
        source = Image.open(screenshot_path).convert("RGB")
        contained = ImageOps.contain(source, _COVER_CANVAS, Image.Resampling.LANCZOS)
        offset = ((_COVER_CANVAS[0] - contained.width) // 2,
                  (_COVER_CANVAS[1] - contained.height) // 2)
        canvas = Image.new("RGB", _COVER_CANVAS, (16, 20, 27))
        canvas.paste(contained, offset)
        shade = Image.new("RGBA", _COVER_CANVAS)
        draw = ImageDraw.Draw(shade)
        for y in range(640, 1080):
            draw.line((0, y, 1920, y), fill=(8, 12, 20, round(235 * (y - 640) / 439)))
        canvas = Image.alpha_composite(canvas.convert("RGBA"), shade).convert("RGB")
        output_path.parent.mkdir(parents=True, exist_ok=True)
        canvas.save(output_path)
        box = [offset[0], offset[1], offset[0] + contained.width, offset[1] + contained.height]
        return {
            "schema_version": "screenshot-graphic-poster.v2", "status": "COMPOSED",
            "background_style": art_direction.background_style,
            "composition": "source_frame_with_footer", "palette": {},
            "screenshot_card": {"size": list(contained.size), "rotation_degrees": 0.0},
            "source_frame_transform": {
                "input_sha256": "sha256:" + _sha256(screenshot_path),
                "source_size": list(source.size), "rendered_content_box": box,
                "crop_applied": False, "full_frame_preserved": True,
                "face_safe_contain": True, "card_fit": "full_frame",
                "ai_modified": bool(source_ai_modified),
                "center_4_3_box": [240, 0, 1680, 1080],
                "center_4_3_safe": box[0] >= 240 and box[2] <= 1680,
                "caption_shade_box": [0, 640, 1920, 1080],
            },
            "output_path": str(output_path), "output_sha256": "sha256:" + _sha256(output_path),
        }

    style = art_direction.background_style
    palette = _SCREENSHOT_POSTER_PALETTES.get(
        style, _SCREENSHOT_POSTER_PALETTES["cobalt-comic-burst"]
    )
    background = tuple(palette["background"])
    secondary = tuple(palette["secondary"])
    accent = tuple(palette["accent"])
    paper = tuple(palette["paper"])

    canvas = Image.new("RGBA", _COVER_CANVAS, background + (255,))
    draw = ImageDraw.Draw(canvas, "RGBA")

    # A quiet vertical gradient gives the flat palettes depth without diluting
    # their identity.  All following motifs stay outside the title renderer and
    # contain no glyphs.
    for y in range(_COVER_CANVAS[1]):
        ratio = y / max(1, _COVER_CANVAS[1] - 1)
        color = tuple(
            int(round(background[i] * (1.0 - 0.28 * ratio) + secondary[i] * 0.28 * ratio))
            for i in range(3)
        )
        draw.line((0, y, _COVER_CANVAS[0], y), fill=color + (255,))

    if style == "cobalt-comic-burst":
        center = (960, 730)
        radius = 1250
        for degrees in range(0, 360, 12):
            angle = math.radians(degrees)
            end = (
                center[0] + int(math.cos(angle) * radius),
                center[1] + int(math.sin(angle) * radius),
            )
            draw.line((center, end), fill=accent + (130,), width=9)
        for y in range(36, 330, 34):
            for x in range((y // 34 % 2) * 17, 1920, 34):
                draw.ellipse((x, y, x + 9, y + 9), fill=paper + (150,))
    elif style == "warm-scrapbook-collage":
        draw.polygon([(0, 0), (690, 0), (515, 430), (0, 350)], fill=paper + (215,))
        draw.polygon([(1310, 0), (1920, 0), (1920, 405), (1515, 300)], fill=accent + (215,))
        for x, y, w, h in ((90, 175, 280, 46), (1490, 160, 300, 48), (785, 40, 245, 42)):
            draw.rounded_rectangle((x, y, x + w, y + h), radius=13, fill=(246, 207, 121, 190))
        for x, y, r in ((430, 115, 38), (1190, 155, 55), (1810, 520, 42)):
            draw.ellipse((x - r, y - r, x + r, y + r), outline=paper + (220,), width=15)
    elif style == "violet-neon-stage":
        for inset, color, width in (
            (70, accent, 15),
            (155, (244, 61, 181), 12),
            (250, secondary, 10),
        ):
            draw.arc((inset, -300 + inset, 1920 - inset, 850 + inset), 188, 352, fill=color + (215,), width=width)
        for x, y, r, color in (
            (185, 170, 70, accent),
            (1660, 120, 92, (244, 61, 181)),
            (1450, 430, 45, paper),
        ):
            draw.ellipse((x - r, y - r, x + r, y + r), fill=color + (80,))
    elif style == "mint-doodle-stickers":
        blobs = ((-110, 10, 520, 330), (1330, -80, 2020, 330), (1540, 650, 2020, 1110))
        for box in blobs:
            draw.ellipse(box, fill=paper + (205,), outline=accent + (190,), width=12)
        for x, y in ((170, 190), (530, 95), (1195, 145), (1730, 250)):
            draw.ellipse((x - 18, y - 18, x + 18, y + 18), fill=accent + (220,))
            for dx, dy in ((-32, -25), (32, -25), (-38, 21), (38, 21)):
                draw.ellipse((x + dx - 13, y + dy - 13, x + dx + 13, y + dy + 13), fill=paper + (220,))
    elif style == "mono-manga-panels":
        draw.polygon([(0, 0), (670, 0), (515, 430), (0, 315)], fill=paper + (245,))
        draw.polygon([(1240, 0), (1920, 0), (1920, 395), (1455, 320)], fill=secondary + (240,))
        draw.polygon([(780, 0), (1150, 0), (1040, 330), (690, 310)], fill=accent + (225,))
        for x in range(-250, 2050, 42):
            draw.line((x, 0, x + 430, 430), fill=background + (165,), width=5)
    elif style == "coral-checker-pop":
        cell = 105
        for row in range(4):
            for col in range(20):
                if (row + col) % 2 == 0:
                    draw.rectangle(
                        (col * cell, row * cell, (col + 1) * cell, (row + 1) * cell),
                        fill=secondary + (220,),
                    )
        draw.ellipse((-180, 510, 390, 1080), fill=accent + (225,))
        draw.ellipse((1600, 470, 2110, 980), fill=paper + (210,))
        draw.arc((1370, -80, 1880, 430), 0, 180, fill=accent + (240,), width=36)

    # Relationship covers may inherit participant identity only when the exact
    # hash-bound source frame is transferred in full.  ``ImageOps.fit`` crops
    # by design, so it is reserved for non-relationship aesthetic routes.
    # The no-crop branch keeps the complete 16:9 frame inside the central 4:3
    # feed-safe region and leaves a separate title band above it.
    source_original = Image.open(screenshot_path).convert("RGB")
    source_size = list(source_original.size)
    if preserve_full_frame:
        card_inner_size = (1400, 520)
        contained = ImageOps.contain(
            source_original,
            card_inner_size,
            method=Image.Resampling.LANCZOS,
        )
        source = Image.new("RGB", card_inner_size, paper)
        contained_offset = (
            (card_inner_size[0] - contained.width) // 2,
            (card_inner_size[1] - contained.height) // 2,
        )
        source.paste(contained, contained_offset)
        angle = 0.0
        card_y = 505
    elif face_safe_contain:
        # Camera-window sources are near-full-face by construction: a fixed
        # 1640×700 fit-crop decapitates any close-up whose mouth sits low in
        # the frame (BV1E93L6rErV case — cover shipped with the
        # face cut at the eyes). Contain keeps the whole face; paper bands
        # absorb the aspect difference exactly like the relationship card.
        # The face-safe source is already the CPA-authorized 16:9 identity
        # crop.  Keep it unrotated at exactly 1440×810 so the full image lands
        # inside the central 4:3 feed-safe window (x=240..1680).  The previous
        # 1640×700 tilted card pushed identity pixels outside that crop and
        # created large decorative bands around a short image.
        card_inner_size = (1440, 810)
        contained = ImageOps.contain(
            source_original,
            card_inner_size,
            method=Image.Resampling.LANCZOS,
        )
        source = Image.new("RGB", card_inner_size, paper)
        contained_offset = (
            (card_inner_size[0] - contained.width) // 2,
            (card_inner_size[1] - contained.height) // 2,
        )
        source.paste(
            ImageEnhance.Color(
                ImageEnhance.Contrast(contained).enhance(1.04)
            ).enhance(1.05),
            contained_offset,
        )
        angle = 0.0
        card_y = 117
    else:
        card_inner_size = (1640, 700)
        contained_offset = (0, 0)
        contained = ImageOps.fit(
            source_original,
            card_inner_size,
            method=Image.Resampling.LANCZOS,
            centering=(0.58, 0.38),
        )
        source = ImageEnhance.Contrast(contained).enhance(1.04)
        source = ImageEnhance.Color(source).enhance(1.05)
        angle = float(palette["rotation"])
        card_y = 315
    card = ImageOps.expand(source, border=18, fill=paper).convert("RGBA")
    card = card.rotate(angle, resample=Image.Resampling.BICUBIC, expand=True)
    alpha = card.getchannel("A")
    shadow = Image.new("RGBA", card.size, (6, 9, 22, 0))
    shadow.putalpha(alpha.filter(ImageFilter.GaussianBlur(24)).point(lambda value: int(value * 0.48)))
    x = int((_COVER_CANVAS[0] - card.width) / 2)
    y = card_y
    canvas.alpha_composite(shadow, (x + 18, y + 24))
    canvas.alpha_composite(card, (x, y))

    if preserve_full_frame or face_safe_contain:
        content_box = [
            x + 18 + contained_offset[0],
            y + 18 + contained_offset[1],
            x + 18 + contained_offset[0] + contained.width,
            y + 18 + contained_offset[1] + contained.height,
        ]
    else:
        content_box = [x + 18, y + 18, x + 18 + source.width, y + 18 + source.height]
    center_4_3_box = [240, 0, 1680, 1080]
    center_4_3_safe = bool(
        content_box[0] >= center_4_3_box[0]
        and content_box[1] >= center_4_3_box[1]
        and content_box[2] <= center_4_3_box[2]
        and content_box[3] <= center_4_3_box[3]
    )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    canvas.convert("RGB").save(output_path)
    return {
        "schema_version": "screenshot-graphic-poster.v2",
        "status": "COMPOSED",
        "background_style": style,
        "palette": {
            "background": "#%02x%02x%02x" % background,
            "secondary": "#%02x%02x%02x" % secondary,
            "accent": "#%02x%02x%02x" % accent,
            "paper": "#%02x%02x%02x" % paper,
        },
        "screenshot_card": {
            "size": list(card_inner_size),
            "rotation_degrees": angle,
        },
        "source_frame_transform": {
            "input_sha256": "sha256:" + _sha256(screenshot_path),
            "source_size": source_size,
            "rendered_content_box": content_box,
            "crop_applied": not (preserve_full_frame or face_safe_contain),
            "full_frame_preserved": preserve_full_frame,
            "face_safe_contain": bool(face_safe_contain),
            "card_fit": (
                "full_frame"
                if preserve_full_frame
                else ("contain_face_safe" if face_safe_contain else "fit_crop")
            ),
            "ai_modified": bool(source_ai_modified),
            "center_4_3_box": center_4_3_box,
            "center_4_3_safe": center_4_3_safe,
        },
        "output_path": str(output_path),
        "output_sha256": "sha256:" + _sha256(output_path),
    }
