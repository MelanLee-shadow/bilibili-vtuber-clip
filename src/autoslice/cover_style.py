"""Production rendering port of lidousha-title-cover/scripts/render_cover.py.

Editorial authority remains the shared skill . This port adds
strict numeric validation and replayable full-alpha layers for package audit.
Source SHA256: 207cace8728113166e3c7c0a4aca1215e330e8a325ae1d45c4d14c09ec0cce2b
"""

import sys
from PIL import Image, ImageDraw, ImageFilter, ImageEnhance, ImageFont

W, H = 1920, 1080
PAL = {  # top -> bottom gradient. Default pair: blue (context line) + pink (punchline); others for occasional variety
    "pink": ((255, 138, 196), (255, 40, 130)),
    "blue": ((110, 205, 255), (30, 100, 255)),
    "orange": ((255, 230, 90), (255, 130, 20)),
    "purple": ((205, 150, 255), (125, 60, 235)),
    "green": ((150, 240, 120), (20, 170, 90)),
    "red": ((255, 150, 120), (230, 30, 50)),
    "yellow": ((255, 248, 140), (255, 196, 0)),
}
SIZE = [170, 250]  # default line sizes (px at 1080p): context line, punchline
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


def line_layer(path, text, size, colorway):
    f = font_at(path, size)
    w1 = max(6, size // 9)
    pad = w1 + 12
    tw = int(f.getlength(text)) + 2 * pad
    th = int(size * 1.35) + 2 * pad
    org = (pad, pad - int(size * 0.12))

    def mask(stroke):
        m = Image.new("L", (tw, th), 0)
        ImageDraw.Draw(m).text(org, text, font=f, fill=255, stroke_width=stroke, stroke_fill=255)
        return m.point(lambda v: 255 if v > 110 else 0)

    white = mask(w1)
    ext = white.copy()
    ImageDraw.floodfill(ext, (0, 0), 128)
    white = ext.point(
        lambda v: 0 if v == 128 else 255
    )  # fill enclosed holes of the stroke, not of the glyphs
    lay = Image.new("RGBA", (tw, th), (0, 0, 0, 0))
    lay.paste(Image.new("RGBA", lay.size, (255, 255, 255, 255)), (0, 0), white)
    top, bot = PAL[colorway]
    grad = Image.new("RGBA", lay.size)
    gd = ImageDraw.Draw(grad)
    y0, y1 = org[1] + int(size * 0.15), org[1] + int(size * 1.25)
    for y in range(th):
        t = min(1, max(0, (y - y0) / max(1, y1 - y0)))
        gd.line(
            [(0, y), (tw, y)],
            fill=tuple(int(top[i] + (bot[i] - top[i]) * t) for i in range(3)) + (255,),
        )
    lay.paste(grad, (0, 0), mask(0))
    return lay


def cutout(img, mode):
    """Return an RGBA cut-out. dark: sticker on a dark/flat backdrop (official emotes), white: on white,
    green: AI output on a flat green backdrop, alpha: keep existing alpha, none: keep the rectangle."""
    im = img.convert("RGBA")
    if mode == "auto":  # AI output: real alpha if present, else a flat green backdrop
        mode = "alpha" if im.getchannel("A").getextrema()[0] < 128 else "green"
    if mode == "none":
        return im
    if mode == "alpha":
        return im
    if mode.startswith(
        "mask:"
    ):  # alpha taken from a smaller transparent original (official emotes)
        a = Image.open(mode[5:]).convert("RGBA").getchannel("A").resize(im.size, Image.LANCZOS)
        im.putalpha(
            a.filter(ImageFilter.GaussianBlur(1.2)).point(
                lambda v: 0 if v < 70 else 255 if v > 190 else v
            )
        )
        return im
    rgb = im.convert("RGB")
    w, h = rgb.size
    if mode == "green":
        px = rgb.load()
        a = Image.new("L", (w, h), 255)
        pa = a.load()
        for y in range(h):
            for x in range(w):
                r, g, b = px[x, y]
                if g > 120 and g > r * 1.35 and g > b * 1.35:
                    pa[x, y] = 0
        im.putalpha(a.filter(ImageFilter.MinFilter(3)))
        return im
    # flood from the border through pixels that are NOT part of the sticker's white outline
    key = (lambda c: sum(c) < 600) if mode == "dark" else (lambda c: sum(c) > 720)
    m = Image.new("L", (w, h), 0)
    pm = m.load()
    px = rgb.load()
    from collections import deque

    q = deque((x, y) for x in range(w) for y in (0, h - 1))
    q.extend((x, y) for y in range(h) for x in (0, w - 1))
    while q:
        x, y = q.popleft()
        if pm[x, y] or not key(px[x, y]):
            continue
        pm[x, y] = 255
        if x > 0:
            q.append((x - 1, y))
        if x < w - 1:
            q.append((x + 1, y))
        if y > 0:
            q.append((x, y - 1))
        if y < h - 1:
            q.append((x, y + 1))
    alpha = (
        m.point(lambda v: 255 - v)
        .filter(ImageFilter.MinFilter(3))
        .filter(ImageFilter.GaussianBlur(0.8))
    )
    im.putalpha(alpha)
    return im


def paste_sticker(can, st):
    img = cutout(Image.open(st["src"]), st.get("cutout", "dark"))
    if st.get("crop"):
        img = img.crop(tuple(st["crop"]))
    bbox = img.getchannel("A").getbbox()
    if bbox:
        img = img.crop(bbox)
    hgt = int(st["h"])
    img = img.resize((max(1, round(img.width * hgt / img.height)), hgt), Image.LANCZOS)
    if st.get("flip"):
        img = img.transpose(Image.FLIP_LEFT_RIGHT)
    ol = int(st.get("outline", 12))
    if ol:
        pad = ol + 4
        big = Image.new("RGBA", (img.width + 2 * pad, img.height + 2 * pad), (0, 0, 0, 0))
        big.alpha_composite(img, (pad, pad))
        ring = big.getchannel("A").filter(ImageFilter.MaxFilter(2 * (ol // 2) + 1))
        base = Image.new("RGBA", big.size, (255, 255, 255, 0))
        base.putalpha(ring)
        base.alpha_composite(big)
        img = base
    if st.get("rotate"):
        img = img.rotate(st["rotate"], resample=Image.BICUBIC, expand=True)
    sh = Image.new("RGBA", img.size, (0, 0, 0, 0))
    sh.putalpha(img.getchannel("A").point(lambda a: int(a * 0.35)))
    x, y = int(st["x"]), int(st["y"])
    can.alpha_composite(sh.filter(ImageFilter.GaussianBlur(9)), (x + 8, y + 12))
    can.alpha_composite(img, (x, y))


def pastel(spec):
    c0, c1 = spec.get("bg", ["#dff0ff", "#ffe3f1"])
    c0 = tuple(int(c0[i : i + 2], 16) for i in (1, 3, 5))
    c1 = tuple(int(c1[i : i + 2], 16) for i in (1, 3, 5))
    can = Image.new("RGB", (W, H))
    d = ImageDraw.Draw(can)
    for y in range(H):
        t = y / H
        d.line([(0, y), (W, y)], fill=tuple(int(c0[i] + (c1[i] - c0[i]) * t) for i in range(3)))
    dots = Image.new("L", (W, H), 0)
    dd = ImageDraw.Draw(dots)
    for yy in range(0, H, 36):
        for xx in range((yy // 36 % 2) * 18, W, 36):
            dd.ellipse((xx - 5, yy - 5, xx + 5, yy + 5), fill=40)
    can.paste(Image.new("RGB", (W, H), (255, 255, 255)), (0, 0), dots)
    return can


def background(spec):
    if "background_image" in spec:
        from PIL import ImageOps

        with Image.open(spec["background_image"]) as source:
            return ImageOps.fit(source.convert("RGBA"), (W, H), method=Image.Resampling.LANCZOS)
    if "frame" not in spec:
        can = pastel(spec).convert("RGBA")
        for st in spec.get("stickers", []):
            paste_sticker(can, st)
        return can
    im = Image.open(spec["frame"]).convert("RGB")
    x0, y0, w = spec["crop"]
    h = round(w * H / W)
    if x0 < 0 or y0 < 0 or x0 + w > im.width or y0 + h > im.height:
        raise SystemExit(
            f"crop {spec['crop']} (height {h}) falls outside the {im.width}x{im.height} source"
        )
    can = ImageEnhance.Color(
        im.crop((x0, y0, x0 + w, y0 + h)).resize((W, H), Image.LANCZOS)
    ).enhance(1.12)
    for bx0, by0, bx1, by1 in spec.get("blur", []):
        m = Image.new("L", (W, H), 0)
        ImageDraw.Draw(m).rectangle((bx0, by0, bx1, by1), fill=255)
        can = Image.composite(
            can.filter(ImageFilter.GaussianBlur(22)), can, m.filter(ImageFilter.GaussianBlur(30))
        )
    can = can.convert("RGBA")
    if spec.get("inset"):
        sx, sy, sw, sh = spec["inset"]["src"]
        dx, dy, dw = spec["inset"]["dst"]
        face = (
            im.crop((sx, sy, sx + sw, sy + sh))
            .resize((dw, round(dw * sh / sw)), Image.LANCZOS)
            .convert("RGBA")
        )
        r = max(20, dw // 10)
        b = max(10, dw // 28)
        frame = Image.new("RGBA", (face.width + 2 * b, face.height + 2 * b), (0, 0, 0, 0))
        ImageDraw.Draw(frame).rounded_rectangle(
            (0, 0, frame.width - 1, frame.height - 1), r + b, fill=(255, 255, 255, 255)
        )
        m = Image.new("L", face.size, 0)
        ImageDraw.Draw(m).rounded_rectangle((0, 0, face.width - 1, face.height - 1), r, fill=255)
        face.putalpha(m)
        can.alpha_composite(frame, (dx - b, dy - b))
        can.alpha_composite(face, (dx, dy))
    for st in spec.get("stickers", []):
        paste_sticker(can, st)
    return can


def background_mean(img, box):
    x0, y0, x1, y1 = [int(v) for v in box]
    x0, y0 = max(0, x0), max(0, y0)
    x1, y1 = min(W, x1), min(H, y1)
    small = img.crop((x0, y0, x1, y1)).resize((1, 1), Image.BOX)
    return small.getpixel((0, 0))


def _paint_line(font, text, size, colorway, tilt):
    """Measure stroke AND shadow, so both must fit the feed-safe area."""
    glyph = line_layer(font, text, size, colorway).rotate(
        tilt, resample=Image.Resampling.BICUBIC, expand=True
    )
    pad = 48
    painted = Image.new("RGBA", (glyph.width + 2 * pad, glyph.height + 2 * pad))
    shadow = Image.new("RGBA", painted.size)
    mask = Image.new("L", painted.size)
    mask.paste(glyph.getchannel("A").point(lambda a: int(a * 0.4)), (pad + 9, pad + 13))
    shadow.putalpha(mask.filter(ImageFilter.GaussianBlur(10)))
    painted.alpha_composite(shadow)
    painted.alpha_composite(glyph, (pad, pad))
    return painted.crop(painted.getbbox())


def _box(value):
    if (
        not isinstance(value, (list, tuple))
        or len(value) != 4
        or any(isinstance(v, bool) or not isinstance(v, int) for v in value)
    ):
        raise ValueError("COVER_SKILL_BOX_INVALID")
    x0, y0, x1, y1 = value
    if not 0 <= x0 < x1 <= W or not 0 <= y0 < y1 <= H:
        raise ValueError("COVER_SKILL_BOX_INVALID")
    return list(value)


def validate_spec(spec):
    if not isinstance(spec, dict):
        raise ValueError("COVER_SKILL_SPEC_INVALID")
    _box(spec.get("face"))
    for box in spec.get("keep", []):
        _box(box)
    if spec.get("layout", "auto") not in ("auto", "left", "right", "top", "diagonal"):
        raise ValueError("COVER_SKILL_LAYOUT_INVALID")
    tilt = spec.get("tilt", 4)
    if isinstance(tilt, bool) or tilt not in (3, 4):
        raise ValueError("COVER_SKILL_TILT_INVALID")
    lines = spec.get("lines")
    if not isinstance(lines, list) or not 1 <= len(lines) <= 2:
        raise ValueError("COVER_SKILL_LINES_INVALID")
    for line in lines:
        text = line.get("text") if isinstance(line, dict) else None
        if (
            not isinstance(text, str)
            or not text.strip()
            or len(text) > 9
            or any(ord(c) < 32 for c in text)
            or any(c in text for c in "，,")
        ):
            raise ValueError("COVER_SKILL_LINES_INVALID")
        if line.get("color", "blue") not in PAL:
            raise ValueError("COVER_SKILL_PALETTE_INVALID")
        if "size" in line:
            raise ValueError("COVER_SKILL_SIZE_IS_MEASURED_NOT_AUTHORED")
    return lines


def render(spec, font):
    lines = validate_spec(spec)
    if "face" not in spec:
        raise SystemExit("spec 缺 face（脸在成品里的位置 [x0,y0,x1,y1]）")
    if not 1 <= len(lines) <= 2:
        raise SystemExit("cover text must be 1-2 lines")
    for ln in lines:
        if len(ln["text"]) > 9:
            raise SystemExit(f"「{ln['text']}」超过 9 字：缩短文字，不要缩小字号")
    bad = [ln.get("color") for ln in lines if ln.get("color") and ln["color"] not in PAL]
    if bad:
        raise SystemExit(f"未知配色 {bad}：可用 {' '.join(PAL)}")
    miss = missing_glyphs(font, "".join(ln["text"] for ln in lines))
    if miss:
        raise SystemExit(f"字体缺字 {''.join(miss)}：改写措辞，不要混用其他字体")
    can = background(spec)
    spec_bg = can.convert("RGB")
    title_layer = Image.new("RGBA", (W, H))
    fx0, fy0, fx1, fy1 = spec["face"]

    def zone(layout):  # text area that keeps clear of the face and inside the feed-safe zone
        if layout == "left":
            return (SAFE_X0, min(SAFE_X1, fx0 - 30), 70, H - 20)
        if layout == "right":
            return (max(SAFE_X0, fx1 + 30), SAFE_X1, 70, H - 20)
        return (SAFE_X0, SAFE_X1, 30, fy0 - 10)  # top: above the face

    def plan(layout):
        x0, x1, y0, y1 = zone(layout)
        tilt = spec.get("tilt", 3 if layout == "top" else 4)
        if x1 - x0 < 300 or y1 - y0 < 150:
            return None
        n = len(lines)
        budget = y1 - y0

        def fit(i, size, max_h):
            ln = lines[i]
            while True:
                lay = _paint_line(
                    font, ln["text"], size, ln.get("color", "pink" if i else "blue"), tilt
                )
                ink = lay.getbbox()  # actual painted pixels (stroke included)
                if (
                    ink[2] - ink[0] <= x1 - x0 - (int(W * 0.03) if (n == 2 and i == 0) else 0)
                    and ink[3] - ink[1] <= max_h
                ) or size <= 60:
                    return size, lay, ink
                size -= 6

        share = [0.42, 0.58] if n == 2 else [1.0]
        fitted = [
            fit(i, ln.get("size") or (SIZE[1] if i == n - 1 else SIZE[0]), budget * share[i])
            for i, ln in enumerate(lines)
        ]
        if n == 2 and not lines[0].get("size"):  # the context line stays smaller than the punchline
            want = max(80, int(fitted[1][0] * 0.7))
            if fitted[0][0] > want:
                fitted[0] = fit(0, want, budget * share[0])
        if sum(ink[3] - ink[1] for _, _, ink in fitted) > budget:
            return None
        return dict(layout=layout, zone=(x0, x1, y0, y1), fitted=fitted, punch=fitted[-1][0])

    def plan_diagonal():  # line 1 above the face (left-aligned), line 2 below the face (right-aligned)
        if len(lines) != 2:
            return None
        za = (SAFE_X0, SAFE_X1, 30, fy0 - 10)
        zb = (SAFE_X0, SAFE_X1, fy1 + 10, H - 20)
        if za[3] - za[2] < 110 or zb[3] - zb[2] < 140:
            return None
        out = []
        for i, (x0_, x1_, y0_, y1_) in enumerate((za, zb)):
            ln = lines[i]
            tilt = spec.get("tilt", 4)
            size = ln.get("size") or (SIZE[0] if i == 0 else SIZE[1] + 20)
            while True:
                lay = _paint_line(
                    font, ln["text"], size, ln.get("color", "pink" if i else "blue"), tilt
                )
                ink = lay.getbbox()
                if (ink[2] - ink[0] <= x1_ - x0_ and ink[3] - ink[1] <= y1_ - y0_) or size <= 60:
                    break
                size -= 6
            out.append((size, lay, ink))
        if not lines[0].get("size") and out[0][0] > out[1][0] * 0.8:
            ln = lines[0]
            size = int(out[1][0] * 0.75)
            lay = _paint_line(font, ln["text"], size, ln.get("color", "blue"), spec.get("tilt", 4))
            out[0] = (size, lay, lay.getbbox())
        return dict(layout="diagonal", zone=(za, zb), fitted=out, punch=out[1][0])

    wanted = spec.get("layout", "auto")
    names = ["left", "right", "top", "diagonal"] if wanted == "auto" else [wanted]
    plans = [
        p for p in ((plan_diagonal() if name == "diagonal" else plan(name)) for name in names) if p
    ]
    if not plans:
        raise SystemExit(f"脸周围放不下文字（版式 {wanted}）：重新裁切让脸偏到一侧，或缩短文字")
    best = max(plans, key=lambda p: p["punch"])
    layout = best["layout"]
    if layout == "diagonal":
        (x0, x1, y0, y1), zb = best["zone"]
    else:
        x0, x1, y0, y1 = best["zone"]
    record = []
    y = y0
    for i, (size, lay, ink) in enumerate(best["fitted"]):
        ln = lines[i]
        iw, ih = ink[2] - ink[0], ink[3] - ink[1]
        if i == len(lines) - 1 and size < MIN_PUNCH:
            raise SystemExit(
                f"「{ln['text']}」最多只能放到 {size}px（下限 {MIN_PUNCH}px）：缩短文字，或重新裁切给文字留出地方"
            )
        stagger = int(W * 0.03) if (len(lines) == 2 and i == 0) else 0
        if layout == "diagonal" and i == 1:
            y = zb[3] - ih
            x = zb[1] - iw
        elif layout == "diagonal":
            x = x0
        else:
            x = {
                "left": x0 + stagger,
                "right": x1 - iw - stagger,
                "top": (x0 if i == 0 else x1 - iw) if len(lines) == 2 else (x0 + x1 - iw) // 2,
            }[layout]
        x = max(SAFE_X0, min(x, SAFE_X1 - iw))
        px, py = x - ink[0], y - ink[1]  # paste so the ink box starts at (x, y)
        title_layer.alpha_composite(lay, (px, py))
        bg_mean = background_mean(spec_bg, (x, y, x + iw, y + ih))
        col = PAL[ln.get("color", "pink" if i else "blue")][1]
        if sum((a - b) ** 2 for a, b in zip(bg_mean, col)) ** 0.5 < 110:
            print(
                f"WARN 「{ln['text']}」的颜色和背景太接近（背景平均 {bg_mean}）：换一套配色",
                file=sys.stderr,
            )
        record.append(
            dict(
                text=ln["text"],
                size=size,
                x=x,
                y=y,
                w=iw,
                h=ih,
                color=ln.get("color", "pink" if i else "blue"),
                layout=layout,
                tilt=spec.get("tilt", 3 if layout == "top" else 4),
            )
        )
        y += ih + int(size * 0.02)
    for rr in record:
        for kx0, ky0, kx1, ky1 in [spec["face"]] + spec.get("keep", []):
            ox = min(rr["x"] + rr["w"], kx1) - max(rr["x"], kx0)
            oy = min(rr["y"] + rr["h"], ky1) - max(rr["y"], ky0)
            if ox > 0 and oy > 0:
                raise SystemExit(
                    f"「{rr['text']}」压到脸或要保留的画面上了：换版式、换裁切或缩短文字"
                )
    if len(record) == 2 and record[1]["size"] < record[0]["size"]:
        print(
            f"WARN 笑点行「{record[1]['text']}」({record[1]['size']}px) 比第一行小：缩短笑点行，或把第一行写短",
            file=sys.stderr,
        )
    print(f"layout={layout} punch={record[-1]['size']}px", file=sys.stderr)
    bbox = title_layer.getchannel("A").getbbox()
    if not bbox or bbox[0] < SAFE_X0 or bbox[2] > SAFE_X1 or bbox[1] < 0 or bbox[3] > H:
        raise ValueError("COVER_TITLE_EXCEEDS_FEED_SAFE_ZONE")
    return spec_bg, title_layer, record


STYLE = "lidousha-zhenyan-gradient.v1"


def replay_title_layer(spec, font_path):
    """Replay the fixed recipe without re-running the layout chooser."""
    from .cover_title_rendering import _basic_layout_engine, sha256_file

    if (
        spec.get("style") != STYLE
        or spec.get("layer_size") != [W, H]
        or spec.get("rendered_layer_size") != [W, H]
        or spec.get("angle_degrees") != 0
        or spec.get("layout_engine") != "basic"
        or spec.get("font_face_index") != 0
        or spec.get("font_file_name") != font_path.name
        or spec.get("font_file_sha256") != sha256_file(font_path)
    ):
        raise ValueError("COVER_SKILL_RENDER_SPEC_INVALID")
    del _basic_layout_engine  # font_at selects this same engine for every glyph.
    face = _box(spec.get("face"))
    keep = [_box(box) for box in spec.get("keep", [])]
    lines = spec.get("lines")
    if not isinstance(lines, list) or not 1 <= len(lines) <= 2:
        raise ValueError("COVER_SKILL_LINES_INVALID")
    layer = Image.new("RGBA", (W, H))
    for row in lines:
        seg = row.get("segments")
        if not isinstance(seg, list) or len(seg) != 1:
            raise ValueError("COVER_SKILL_SEGMENTS_INVALID")
        text = seg[0].get("text")
        if (
            not isinstance(text, str)
            or not text.strip()
            or len(text) > 9
            or any(ord(c) < 32 for c in text)
            or any(c in text for c in "，,")
        ):
            raise ValueError("COVER_SKILL_LINES_INVALID")
        size, tilt = row.get("font_size"), row.get("tilt")
        if (
            isinstance(size, bool)
            or not isinstance(size, int)
            or not 60 <= size <= 300
            or isinstance(tilt, bool)
            or tilt not in (3, 4)
            or row.get("colorway") not in PAL
        ):
            raise ValueError("COVER_SKILL_RENDER_SPEC_INVALID")
        if missing_glyphs(str(font_path), text):
            raise ValueError("COVER_FONT_GLYPH_COVERAGE_MISSING")
        painted = _paint_line(str(font_path), text, size, row["colorway"], tilt)
        x, y = row.get("x"), row.get("y")
        if (
            isinstance(x, bool)
            or not isinstance(x, int)
            or isinstance(y, bool)
            or not isinstance(y, int)
            or x < SAFE_X0
            or x + painted.width > SAFE_X1
            or y < 0
            or y + painted.height > H
        ):
            raise ValueError("COVER_TITLE_EXCEEDS_FEED_SAFE_ZONE")
        if row.get("painted_size") != [painted.width, painted.height]:
            raise ValueError("COVER_SKILL_PAINTED_SIZE_MISMATCH")
        for x0, y0, x1, y1 in [face, *keep]:
            if min(x + painted.width, x1) > max(x, x0) and min(y + painted.height, y1) > max(y, y0):
                raise ValueError("COVER_SKILL_TEXT_OCCLUDES_FACE_OR_EVENT")
        layer.alpha_composite(painted, (x, y))
    if lines[-1]["font_size"] < MIN_PUNCH:
        raise ValueError("COVER_TITLE_TOO_SMALL")
    return layer


def materialize_cover(spec, final_path, *, font_path, font_authority):
    """Write the same artifacts consumed by the native package pixel auditor."""
    from pathlib import Path
    from .cover_title_rendering import sha256_file
    from .cover_text_pixel_evidence import materialize_rendered_text_pixel_evidence

    final_path, font_path = Path(final_path), Path(font_path)
    pre, layer, rows = render(spec, str(font_path))
    render_spec = {
        "schema_version": "lidousha-cover-title-render-spec.v1",
        "style": STYLE,
        "layout_engine": "basic",
        "font_file_name": font_path.name,
        "font_file_sha256": sha256_file(font_path),
        "font_face_index": 0,
        "layer_size": [W, H],
        "rendered_layer_size": [W, H],
        "angle_degrees": 0,
        "face": list(spec["face"]),
        "keep": spec.get("keep", []),
        "lines": [
            {
                "x": int(r["x"]),
                "y": int(r["y"]),
                "font_size": r["size"],
                "segments": [{"text": r["text"]}],
                "colorway": r["color"],
                "tilt": r["tilt"],
                "painted_size": [r["w"], r["h"]],
            }
            for r in rows
        ],
    }
    replayed = replay_title_layer(render_spec, font_path)
    if replayed.tobytes() != layer.tobytes():
        raise ValueError("COVER_SKILL_LAYOUT_REPLAY_MISMATCH")
    final_path.parent.mkdir(parents=True, exist_ok=True)
    base = final_path.with_suffix("")
    pre_path = base.with_name(base.name + ".pre-overlay.png")
    background_path = base.with_name(base.name + ".route-background.png")
    pre.save(pre_path)
    pre.save(background_path)
    final = pre.copy()
    final.paste(layer, (0, 0), layer)
    final.save(final_path)
    phone = final.resize((180, 101), Image.Resampling.LANCZOS)
    phone_path = base.with_name(base.name + ".phone-x3.png")
    phone.resize((540, 303), Image.Resampling.NEAREST).save(phone_path)
    text = "".join(r["text"] for r in rows)
    pixels = materialize_rendered_text_pixel_evidence(
        final_cover_path=final_path,
        pre_overlay_path=pre_path,
        font_path=font_path,
        render_spec=render_spec,
        paste_xy=(0, 0),
        font_size=max(r["size"] for r in rows),
        rendered_text=text,
    )
    return {
        "font": font_path.name,
        "font_selection": {"font": font_path.name, "glyph_risk": [], "authority": font_authority},
        "font_size": max(r["size"] for r in rows),
        "punch_font_size": rows[-1]["size"],
        "min_talk_font_size": MIN_PUNCH,
        "angle_degrees": 0,
        "overlay_position": {"x": 0, "y": 0},
        "layout": rows[-1]["layout"],
        "title_style": STYLE,
        "cover_text_mode": "punch",
        "cover_punch": [r["text"] for r in rows],
        "rendered_lines": [r["text"] for r in rows],
        "rendered_text_pixels": pixels,
        "pre_overlay_path": str(pre_path),
        "pre_overlay_sha256": sha256_file(pre_path),
        "ai_background": str(background_path),
        "ai_background_sha256": sha256_file(background_path),
        "text_backing": "outline",
        "scrim": False,
        "phone_preview": str(phone_path),
        "phone_preview_sha256": sha256_file(phone_path),
        "measured_lines": rows,
    }
