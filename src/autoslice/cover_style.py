"""Production rendering port of lidousha-title-cover/scripts/render_cover.py.

Editorial authority remains the shared skill . This port adds
strict numeric validation and replayable full-alpha layers for package audit.
Source SHA256: 207cace8728113166e3c7c0a4aca1215e330e8a325ae1d45c4d14c09ec0cce2b
"""

from PIL import Image, ImageDraw, ImageFont


MIN_PUNCH = 120  # production thumbnail rule: emphasis text >= 120px
SAFE_X0, SAFE_X1 = (
    260,
    1660,
)  # production feed-safe zone (70 step): every text pixel must stay inside


def font_at(path, size):
    from .cover_title_rendering import _basic_layout_engine

    return ImageFont.truetype(path, size, layout_engine=_basic_layout_engine())


def missing_glyphs(path, text):
    f = font_at(path, 64)

    def m(c):
        im = Image.new("L", (80, 80), 0)
        ImageDraw.Draw(im).text((4, 0), c, font=f, fill=255)
        return im.tobytes()

    notdef = {m(""), m("\U0010fffd")}
    return [c for c in dict.fromkeys(text) if not c.isspace() and m(c) in notdef]


