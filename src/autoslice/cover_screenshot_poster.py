"""Deterministic graphic-poster treatment for strong real-moment covers.

This module keeps the live screenshot intact while materializing the selected
batch diversity family as actual pixels around it.
"""

from __future__ import annotations

import math
from pathlib import Path

from .cover_generation import LidoushaCoverArtDirection, _COVER_CANVAS, _sha256


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


def _compose_screenshot_poster_background(
    screenshot_path: Path,
    output_path: Path,
    *,
    art_direction: LidoushaCoverArtDirection,
) -> dict[str, object]:
    """Put a faithful real-moment screenshot on a visibly rotating poster.

    The July 22 cover incident exposed a routing hole: ``background_style`` was
    assigned correctly, but strong frames took the screenshot-direct lane and
    never rendered that style.  This deterministic layer keeps the real facial
    expression (the useful part of the screenshot route) while making palette,
    motif, card angle and surrounding graphic field materially different across
    a batch.  No segmentation or generative redraw is involved.
    """

    from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageOps

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

    # Preserve the selected real moment verbatim inside a big photographic card.
    # The top graphic field remains exposed for the 2-12-character punch text.
    source = Image.open(screenshot_path).convert("RGB")
    source = ImageOps.fit(
        source,
        (1640, 700),
        method=Image.Resampling.LANCZOS,
        centering=(0.58, 0.38),
    )
    source = ImageEnhance.Contrast(source).enhance(1.04)
    source = ImageEnhance.Color(source).enhance(1.05)
    card = ImageOps.expand(source, border=18, fill=paper).convert("RGBA")
    angle = float(palette["rotation"])
    card = card.rotate(angle, resample=Image.Resampling.BICUBIC, expand=True)
    alpha = card.getchannel("A")
    shadow = Image.new("RGBA", card.size, (6, 9, 22, 0))
    shadow.putalpha(alpha.filter(ImageFilter.GaussianBlur(24)).point(lambda value: int(value * 0.48)))
    x = int((_COVER_CANVAS[0] - card.width) / 2)
    y = 315
    canvas.alpha_composite(shadow, (x + 18, y + 24))
    canvas.alpha_composite(card, (x, y))

    # One family-color rule under the card makes the batch palette readable even
    # in a tiny feed thumbnail.
    draw = ImageDraw.Draw(canvas, "RGBA")
    draw.rounded_rectangle((120, 1020, 1800, 1062), radius=20, fill=accent + (245,))

    output_path.parent.mkdir(parents=True, exist_ok=True)
    canvas.convert("RGB").save(output_path)
    return {
        "schema_version": "screenshot-graphic-poster.v1",
        "status": "COMPOSED",
        "background_style": style,
        "palette": {
            "background": "#%02x%02x%02x" % background,
            "secondary": "#%02x%02x%02x" % secondary,
            "accent": "#%02x%02x%02x" % accent,
            "paper": "#%02x%02x%02x" % paper,
        },
        "screenshot_card": {"size": [1640, 700], "rotation_degrees": angle},
        "output_path": str(output_path),
        "output_sha256": "sha256:" + _sha256(output_path),
    }
