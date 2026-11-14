#!/usr/bin/env python3
"""Render a 李豆沙 talk-clip cover in the house style: real frame (or redraw background) + RuiZi ZhenYan gradient text
with a stroke (white, dark or double), drop shadow and a tilt. The stroke, colours, tilt and size ratio come from a
style rotation table shared with vtuber-slice (assets/lidousha/cover_style_rotation.json). Produces the cover, a
phone-size preview and a render record.

usage: render_cover.py SPEC.json --styles PATH [--font PATH] [--out-dir DIR]   (render)
       render_cover.py SPEC.json --grid [--out-dir DIR]                         (crop + 100px grid, to read the face box)
font: --font, else env LIDOUSHA_COVER_FONT (锐字真言体 ZhenyanGB file). The font file is never committed.
styles: --styles, else env LIDOUSHA_COVER_STYLES. Required for render. A missing or invalid table exits with an error;
        there is no built-in default.

SPEC (pixel values are in the 1920x1080 output unless noted):
{
  "id": "jzq",
  "frame": "frames/f_0091.00.png",        # source image (a picked frame or a text-free redraw background);
                                          # omit it for a collage on a generated pastel background ("bg": ["#dff0ff", "#ffe3f1"])
  "stickers": [{"src": "emote/13_得意.png", "cutout": "dark", "x": 980, "y": 330, "h": 760, "rotate": -4}],
                                          # optional cut-out layers (emotes, AI chibi, other streamers, objects):
                                          # cutout auto (AI output)|mask:<small transparent original>|dark|white|green|alpha|none,
                                          # outline px (default 12), flip, crop
  "crop": [96, 130, 1150],                # x0, y0, width in SOURCE pixels; height = width * 9 / 16
  "layout": "auto",                       # auto (default: tries left/right/top/diagonal, keeps the biggest punchline)
                                          # or force left | right | top | diagonal (line 1 above the face, line 2 below)
  "lines": [{"text": "溜进舰长群偷窥"},     # 1-2 lines, <= 9 chars each; line 2 is the punchline.
            {"text": "kmx：快滚！"}],       # colour is chosen by the style unless "color" is written (blue pink orange purple green red yellow)
                                          # a column (layout left | right) may stack 3-4 short rows: the same words broken at
                                          # word boundaries; the rows of the punchline carry "punch": true (default: the last row)
  "style_id": "s3-blue-pink-t2-r85-double",  # optional: force one style from the table instead of rotating
  "blur": [[0, 0, 420, 1080]],            # optional boxes to blur (chat boxes, burned subtitles, UI)
  "inset": {"src": [1040, 440, 240, 240], "dst": [1440, 600, 420]},   # optional: her face from the SOURCE, placed at dst x,y,width
  "face": [1100, 300, 1600, 760],         # REQUIRED: face box in OUTPUT pixels, eyebrows to chin, cheek to cheek
                                          # (hair and ears may be covered by text, the face may not)
  "keep": [[400, 500, 900, 1000]],        # optional: other boxes the text may not cover (the other person, the event)
  "tilt": 4                                # optional, degrees (-4 -3 -2 2 3 4). Written tilt overrides the style.
}
Hand-written line "color", line "size" and top-level "tilt" always win over the style. Rotation never overrides them.
"""
import argparse, hashlib, importlib.util, json, os, sys
from collections import deque
from pathlib import Path
from PIL import Image, ImageDraw, ImageFilter, ImageEnhance, ImageFont

W, H = 1920, 1080
SIZE = [170, 250]          # default line sizes (px at 1080p): context line, punchline
MIN_PUNCH = 120            # production thumbnail rule: emphasis text >= 120px
SAFE_X0, SAFE_X1 = 260, 1660   # production feed-safe zone (70 step): every text pixel must stay inside
SCHEMA = 'lidousha-cover-style-rotation.v1'
# Filled by install_table(); no built-in palette or default style exists in this script.
PAL, STROKES, ALLOWED_TILTS, MIN_CONTRAST, STYLES, TABLE = {}, {}, (), 110, [], None
ROTATION = None

def _fail(code):
    raise SystemExit(code)

def _rgb(value):
    if not isinstance(value, list) or len(value) != 3 or any(isinstance(v, bool) or not isinstance(v, int) or not 0 <= v <= 255 for v in value):
        _fail('COVER_STYLE_TABLE_COLOR_INVALID')
    return tuple(value)

def load_styles(path):
    """Read and validate the shared rotation table. Any problem exits with an error; there is no fallback."""
    global ROTATION
    try:
        raw = open(path, 'rb').read()
    except OSError:
        _fail(f'COVER_STYLE_TABLE_UNREADABLE: {path}')
    try:
        table = json.loads(raw.decode('utf-8'))
    except ValueError:
        _fail('COVER_STYLE_TABLE_JSON_INVALID')
    if not isinstance(table, dict) or table.get('schema_version') != SCHEMA:
        _fail('COVER_STYLE_TABLE_SCHEMA_INVALID')
    palette = table.get('palette')
    if not isinstance(palette, dict) or not palette:
        _fail('COVER_STYLE_TABLE_PALETTE_INVALID')
    for pair in palette.values():
        if not isinstance(pair, dict):
            _fail('COVER_STYLE_TABLE_PALETTE_INVALID')
        _rgb(pair.get('top')); _rgb(pair.get('bottom'))
    strokes = table.get('strokes')
    if not isinstance(strokes, dict) or not strokes:
        _fail('COVER_STYLE_TABLE_STROKES_INVALID')
    for recipe in strokes.values():
        if not isinstance(recipe, dict) or (recipe.get('inner') is None and recipe.get('outer') is None):
            _fail('COVER_STYLE_TABLE_STROKES_INVALID')
        for key in ('inner', 'outer'):
            if recipe.get(key) is not None:
                _rgb(recipe[key])
        ratio = recipe.get('outer_extra_ratio')
        if isinstance(ratio, bool) or not isinstance(ratio, (int, float)) or ratio < 0:
            _fail('COVER_STYLE_TABLE_STROKES_INVALID')
    tilts = table.get('allowed_tilts')
    if not isinstance(tilts, list) or not tilts or any(isinstance(t, bool) or not isinstance(t, int) or t == 0 or abs(t) > 6 for t in tilts):
        _fail('COVER_STYLE_TABLE_TILT_INVALID')
    contrast = table.get('contrast') or {}
    if isinstance(contrast.get('min_distance'), bool) or not isinstance(contrast.get('min_distance'), int) or contrast['min_distance'] <= 0:
        _fail('COVER_STYLE_TABLE_CONTRAST_INVALID')
    styles = table.get('styles')
    if not isinstance(styles, list) or not styles:
        _fail('COVER_STYLE_TABLE_STYLES_INVALID')
    seen = set()
    for style in styles:
        if not isinstance(style, dict):
            _fail('COVER_STYLE_TABLE_STYLES_INVALID')
        sid = style.get('style_id')
        if not isinstance(sid, str) or not sid or sid in seen:
            _fail('COVER_STYLE_TABLE_STYLES_INVALID')
        seen.add(sid)
        colors = style.get('colors')
        if not isinstance(colors, list) or len(colors) != 2 or any(c not in palette for c in colors):
            _fail('COVER_STYLE_TABLE_STYLES_INVALID')
        if isinstance(style.get('tilt'), bool) or style.get('tilt') not in tilts:
            _fail('COVER_STYLE_TABLE_STYLES_INVALID')
        ratio = style.get('size_ratio')
        if isinstance(ratio, bool) or not isinstance(ratio, (int, float)) or not 0.3 <= ratio <= 1:
            _fail('COVER_STYLE_TABLE_STYLES_INVALID')
        if style.get('stroke') not in strokes:
            _fail('COVER_STYLE_TABLE_STYLES_INVALID')
    table = dict(table)
    table['table_sha256'] = hashlib.sha256(raw).hexdigest()
    # A repo table locates its own authority; the portable renderer receives the same source beside the script.
    styles_path = Path(path).resolve()
    sources = [Path(__file__).resolve().with_name('cover_style_rotation.py')]
    if styles_path.parent.name == 'lidousha' and styles_path.parent.parent.name == 'assets':
        sources.insert(0, styles_path.parent.parent.parent / 'src/autoslice/cover_style_rotation.py')
    source = next((p for p in sources if p.is_file()), None)
    if source is None:
        raise SystemExit('COVER_STYLE_SELECTOR_UNREADABLE: use the vtuber-slice repo table or provide cover_style_rotation.py beside the renderer')
    module_spec = importlib.util.spec_from_file_location('cover_style_rotation', source)
    rotation = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(rotation)
    ROTATION = rotation
    return table

def install_table(table):
    global PAL, STROKES, ALLOWED_TILTS, MIN_CONTRAST, STYLES, TABLE
    TABLE = table
    PAL = {n: (tuple(p['top']), tuple(p['bottom'])) for n, p in table['palette'].items()}
    STROKES = table['strokes']
    ALLOWED_TILTS = tuple(table['allowed_tilts'])
    MIN_CONTRAST = table['contrast']['min_distance']
    STYLES = table['styles']

def default_content_sha256(spec):
    return ROTATION.default_content_sha256(spec)

def font_at(path, size): return ImageFont.truetype(path, size)

def missing_glyphs(path, text):
    f = font_at(path, 64)
    def m(c):
        im = Image.new('L', (80, 80), 0); ImageDraw.Draw(im).text((4, 0), c, font=f, fill=255); return im.tobytes()
    notdef = {m(''), m('\U0010fffd')}
    return [c for c in dict.fromkeys(text) if not c.isspace() and m(c) in notdef]

def _outer_extra(recipe, size):
    if recipe['outer'] is None or not recipe['outer_extra_ratio']:
        return 0
    return max(recipe['outer_extra_min'], int(size * recipe['outer_extra_ratio']))

def line_layer(path, text, size, colorway, stroke='white'):
    f = font_at(path, size); recipe = STROKES[stroke]
    w1 = max(6, size // 9); extra = _outer_extra(recipe, size); pad = w1 + extra + 12
    tw = int(f.getlength(text)) + 2 * pad; th = int(size * 1.35) + 2 * pad
    org = (pad, pad - int(size * 0.12))
    def mask(stroke_width):
        m = Image.new('L', (tw, th), 0)
        ImageDraw.Draw(m).text(org, text, font=f, fill=255, stroke_width=stroke_width, stroke_fill=255)
        return m.point(lambda v: 255 if v > 110 else 0)
    def ring(stroke_width):
        m = mask(stroke_width); ext = m.copy(); ImageDraw.floodfill(ext, (0, 0), 128)
        return ext.point(lambda v: 0 if v == 128 else 255)     # fill enclosed holes of the stroke, not of the glyphs
    lay = Image.new('RGBA', (tw, th), (0, 0, 0, 0))
    if recipe['outer'] is not None:
        lay.paste(Image.new('RGBA', lay.size, tuple(recipe['outer']) + (255,)), (0, 0), ring(w1 + extra))
    if recipe['inner'] is not None:
        lay.paste(Image.new('RGBA', lay.size, tuple(recipe['inner']) + (255,)), (0, 0), ring(w1))
    top, bot = PAL[colorway]; grad = Image.new('RGBA', lay.size); gd = ImageDraw.Draw(grad)
    y0, y1 = org[1] + int(size * 0.15), org[1] + int(size * 1.25)
    for y in range(th):
        t = min(1, max(0, (y - y0) / max(1, y1 - y0)))
        gd.line([(0, y), (tw, y)], fill=tuple(int(top[i] + (bot[i] - top[i]) * t) for i in range(3)) + (255,))
    lay.paste(grad, (0, 0), mask(0))
    return lay

def cutout(img, mode):
    """Return an RGBA cut-out. dark: sticker on a dark/flat backdrop (official emotes), white: on white,
    green: AI output on a flat green backdrop, alpha: keep existing alpha, none: keep the rectangle."""
    im = img.convert('RGBA')
    if mode == 'auto':                               # AI output: real alpha if present, else a flat green backdrop
        mode = 'alpha' if im.getchannel('A').getextrema()[0] < 128 else 'green'
    if mode == 'none': return im
    if mode == 'alpha': return im
    if mode.startswith('mask:'):                     # alpha taken from a smaller transparent original (official emotes)
        a = Image.open(mode[5:]).convert('RGBA').getchannel('A').resize(im.size, Image.LANCZOS)
        im.putalpha(a.filter(ImageFilter.GaussianBlur(1.2)).point(lambda v: 0 if v < 70 else 255 if v > 190 else v)); return im
    rgb = im.convert('RGB'); w, h = rgb.size
    if mode == 'green':
        px = rgb.load(); a = Image.new('L', (w, h), 255); pa = a.load()
        for y in range(h):
            for x in range(w):
                r, g, b = px[x, y]
                if g > 120 and g > r * 1.35 and g > b * 1.35: pa[x, y] = 0
        im.putalpha(a.filter(ImageFilter.MinFilter(3))); return im
    # flood from the border through pixels that are NOT part of the sticker's white outline
    key = (lambda c: sum(c) < 600) if mode == 'dark' else (lambda c: sum(c) > 720)
    m = Image.new('L', (w, h), 0); pm = m.load(); px = rgb.load()
    q = deque((x, y) for x in range(w) for y in (0, h - 1)); q.extend((x, y) for y in range(h) for x in (0, w - 1))
    while q:
        x, y = q.popleft()
        if pm[x, y] or not key(px[x, y]): continue
        pm[x, y] = 255
        if x > 0: q.append((x - 1, y))
        if x < w - 1: q.append((x + 1, y))
        if y > 0: q.append((x, y - 1))
        if y < h - 1: q.append((x, y + 1))
    alpha = m.point(lambda v: 255 - v).filter(ImageFilter.MinFilter(3)).filter(ImageFilter.GaussianBlur(0.8))
    im.putalpha(alpha); return im

def paste_sticker(can, st):
    img = cutout(Image.open(st['src']), st.get('cutout', 'dark'))
    if st.get('crop'): img = img.crop(tuple(st['crop']))
    bbox = img.getchannel('A').getbbox()
    if bbox: img = img.crop(bbox)
    hgt = int(st['h']); img = img.resize((max(1, round(img.width * hgt / img.height)), hgt), Image.LANCZOS)
    if st.get('flip'): img = img.transpose(Image.FLIP_LEFT_RIGHT)
    ol = int(st.get('outline', 12))
    if ol:
        pad = ol + 4; big = Image.new('RGBA', (img.width + 2 * pad, img.height + 2 * pad), (0, 0, 0, 0)); big.alpha_composite(img, (pad, pad))
        ring = big.getchannel('A').filter(ImageFilter.MaxFilter(2 * (ol // 2) + 1))
        base = Image.new('RGBA', big.size, (255, 255, 255, 0)); base.putalpha(ring)
        base.alpha_composite(big); img = base
    if st.get('rotate'): img = img.rotate(st['rotate'], resample=Image.BICUBIC, expand=True)
    sh = Image.new('RGBA', img.size, (0, 0, 0, 0)); sh.putalpha(img.getchannel('A').point(lambda a: int(a * 0.35)))
    x, y = int(st['x']), int(st['y'])
    can.alpha_composite(sh.filter(ImageFilter.GaussianBlur(9)), (x + 8, y + 12)); can.alpha_composite(img, (x, y))

def pastel(spec):
    c0, c1 = spec.get('bg', ['#dff0ff', '#ffe3f1'])
    c0 = tuple(int(c0[i:i + 2], 16) for i in (1, 3, 5)); c1 = tuple(int(c1[i:i + 2], 16) for i in (1, 3, 5))
    can = Image.new('RGB', (W, H)); d = ImageDraw.Draw(can)
    for y in range(H):
        t = y / H; d.line([(0, y), (W, y)], fill=tuple(int(c0[i] + (c1[i] - c0[i]) * t) for i in range(3)))
    dots = Image.new('L', (W, H), 0); dd = ImageDraw.Draw(dots)
    for yy in range(0, H, 36):
        for xx in range((yy // 36 % 2) * 18, W, 36): dd.ellipse((xx - 5, yy - 5, xx + 5, yy + 5), fill=40)
    can.paste(Image.new('RGB', (W, H), (255, 255, 255)), (0, 0), dots)
    return can

def background(spec):
    if 'frame' not in spec:
        can = pastel(spec).convert('RGBA')
        for st in spec.get('stickers', []): paste_sticker(can, st)
        return can
    im = Image.open(spec['frame']).convert('RGB')
    x0, y0, w = spec['crop']; h = round(w * H / W)
    if x0 < 0 or y0 < 0 or x0 + w > im.width or y0 + h > im.height:
        raise SystemExit(f'crop {spec["crop"]} (height {h}) falls outside the {im.width}x{im.height} source')
    can = ImageEnhance.Color(im.crop((x0, y0, x0 + w, y0 + h)).resize((W, H), Image.LANCZOS)).enhance(1.12)
    for (bx0, by0, bx1, by1) in spec.get('blur', []):
        m = Image.new('L', (W, H), 0); ImageDraw.Draw(m).rectangle((bx0, by0, bx1, by1), fill=255)
        can = Image.composite(can.filter(ImageFilter.GaussianBlur(22)), can, m.filter(ImageFilter.GaussianBlur(30)))
    can = can.convert('RGBA')
    if spec.get('inset'):
        sx, sy, sw, sh = spec['inset']['src']; dx, dy, dw = spec['inset']['dst']
        face = im.crop((sx, sy, sx + sw, sy + sh)).resize((dw, round(dw * sh / sw)), Image.LANCZOS).convert('RGBA')
        r = max(20, dw // 10); b = max(10, dw // 28)
        frame = Image.new('RGBA', (face.width + 2 * b, face.height + 2 * b), (0, 0, 0, 0))
        ImageDraw.Draw(frame).rounded_rectangle((0, 0, frame.width - 1, frame.height - 1), r + b, fill=(255, 255, 255, 255))
        m = Image.new('L', face.size, 0); ImageDraw.Draw(m).rounded_rectangle((0, 0, face.width - 1, face.height - 1), r, fill=255)
        face.putalpha(m); can.alpha_composite(frame, (dx - b, dy - b)); can.alpha_composite(face, (dx, dy))
    for st in spec.get('stickers', []): paste_sticker(can, st)
    return can

def background_mean(img, box):
    x0, y0, x1, y1 = [int(v) for v in box]
    x0, y0 = max(0, x0), max(0, y0); x1, y1 = min(W, x1), min(H, y1)
    small = img.crop((x0, y0, x1, y1)).resize((1, 1), Image.BOX)
    return small.getpixel((0, 0))

def _line_color(line, index, style):
    return line['color'] if 'color' in line else style['colors'][index]

def _attempt(spec, lines, font, base_rgba, spec_bg, style):
    """Lay out and paint the cover with one style. Exits with SystemExit when that style does not fit."""
    tilt = spec['tilt'] if 'tilt' in spec else style['tilt']
    ratio = style['size_ratio']; stroke = style['stroke']
    punch = [i for i, ln in enumerate(lines) if ln.get('punch')] or [len(lines) - 1]     # the rows of the punchline
    # two lines take the style's two colours in turn; stacked rows take them by part (rows before the punchline, punchline)
    colors = [_line_color(ln, i if len(lines) <= 2 else (len(style['colors']) - 1 if i in punch else 0), style) for i, ln in enumerate(lines)]
    overridden = (['tilt'] if 'tilt' in spec else []) + [f'line{i}.{k}' for i, ln in enumerate(lines) for k in ('color', 'size') if k in ln]
    fx0, fy0, fx1, fy1 = spec['face']
    can = base_rgba.copy()
    def zone(layout):   # text area that keeps clear of the face and inside the feed-safe zone
        if layout == 'left': return (SAFE_X0, min(SAFE_X1, fx0 - 30), 70, H - 20)
        if layout == 'right': return (max(SAFE_X0, fx1 + 30), SAFE_X1, 70, H - 20)
        return (SAFE_X0, SAFE_X1, 30, fy0 - 10)                           # top: above the face
    def paint(i, size):
        return line_layer(font, lines[i]['text'], size, colors[i], stroke).rotate(tilt, resample=Image.BICUBIC, expand=True)
    def plan(layout):
        x0, x1, y0, y1 = zone(layout)
        if x1 - x0 < 300 or y1 - y0 < 150: return None
        n = len(lines); budget = y1 - y0
        if n > 2 and layout not in ('left', 'right'): return None     # stacked rows are a column's
        def fit(i, size, max_h):
            while True:
                lay = paint(i, size)
                ink = lay.getbbox()                                  # actual painted pixels (stroke included)
                if (ink[2] - ink[0] <= x1 - x0 - (int(W * 0.03) if (n == 2 and i == 0) else 0) and ink[3] - ink[1] <= max_h) or size <= 60:
                    return size, lay, ink
                size -= 6
        if n > 2:   # a column: the punchline's rows share 58 % of the height, the rows before them 42 %, each group at one size
            k = len(punch); share = [(0.58 if k < n else 1.0) / k if i in punch else 0.42 / (n - k) for i in range(n)]
        else: share = [0.42, 0.58] if n == 2 else [1.0]
        fitted = [fit(i, ln.get('size') or (SIZE[1] if i in punch else SIZE[0]), budget * share[i]) for i, ln in enumerate(lines)]
        if n == 2 and not lines[0].get('size'):          # the context line stays at most size_ratio of the punchline
            want = max(80, int(fitted[1][0] * ratio))
            if fitted[0][0] > want: fitted[0] = fit(0, want, budget * share[0])
        if n > 2:
            big = min(fitted[i][0] for i in punch)
            small = min([fitted[i][0] for i in range(n) if i not in punch] + [max(80, int(big * ratio))])
            fitted = [fit(i, big if i in punch else small, budget * share[i]) for i in range(n)]
        if sum(ink[3] - ink[1] for _, _, ink in fitted) > budget: return None
        return dict(layout=layout, zone=(x0, x1, y0, y1), fitted=fitted, punch=min(fitted[i][0] for i in punch))
    def plan_diagonal():   # line 1 above the face (left-aligned), line 2 below the face (right-aligned)
        if len(lines) != 2: return None
        za = (SAFE_X0, SAFE_X1, 30, fy0 - 10); zb = (SAFE_X0, SAFE_X1, fy1 + 10, H - 20)
        if za[3] - za[2] < 110 or zb[3] - zb[2] < 140: return None
        out = []
        for i, (x0_, x1_, y0_, y1_) in enumerate((za, zb)):
            ln = lines[i]; size = ln.get('size') or (SIZE[0] if i == 0 else SIZE[1] + 20)
            while True:
                lay = line_layer(font, ln['text'], size, colors[i], stroke).rotate(tilt, resample=Image.BICUBIC, expand=True)
                ink = lay.getbbox()
                if (ink[2] - ink[0] <= x1_ - x0_ and ink[3] - ink[1] <= y1_ - y0_) or size <= 60: break
                size -= 6
            out.append((size, lay, ink))
        if not lines[0].get('size') and out[0][0] > out[1][0] * ratio:
            ln = lines[0]; size = int(out[1][0] * ratio)
            lay = line_layer(font, ln['text'], size, colors[0], stroke).rotate(tilt, resample=Image.BICUBIC, expand=True)
            out[0] = (size, lay, lay.getbbox())
        return dict(layout='diagonal', zone=(za, zb), fitted=out, punch=out[1][0])
    wanted = spec.get('layout', 'auto')
    names = ['left', 'right', 'top', 'diagonal'] if wanted == 'auto' else [wanted]
    plans = [p for p in ((plan_diagonal() if l == 'diagonal' else plan(l)) for l in names) if p]
    if not plans: raise SystemExit(f'脸周围放不下文字（版式 {wanted}）：重新裁切让脸偏到一侧，或缩短文字')
    best = max(plans, key=lambda p: p['punch']); layout = best['layout']
    if layout == 'diagonal': (x0, x1, y0, y1), zb = best['zone']
    else: x0, x1, y0, y1 = best['zone']
    record = []; warnings = []; low_contrast = False; y = y0
    for i, (size, lay, ink) in enumerate(best['fitted']):
        ln = lines[i]; iw, ih = ink[2] - ink[0], ink[3] - ink[1]
        if i in punch and size < MIN_PUNCH:
            raise SystemExit(f'「{ln["text"]}」最多只能放到 {size}px（下限 {MIN_PUNCH}px）：缩短文字，或重新裁切给文字留出地方')
        stagger = int(W * 0.03) if (len(lines) == 2 and i == 0) else 0
        if layout == 'diagonal' and i == 1: y = zb[3] - ih; x = zb[1] - iw
        elif layout == 'diagonal': x = x0
        else: x = {'left': x0 + stagger, 'right': x1 - iw - stagger, 'top': (x0 if i == 0 else x1 - iw) if len(lines) == 2 else (x0 + x1 - iw) // 2}[layout]
        x = max(SAFE_X0, min(x, SAFE_X1 - iw))
        px, py = x - ink[0], y - ink[1]                     # paste so the ink box starts at (x, y)
        sh = Image.new('RGBA', lay.size, (0, 0, 0, 0)); sh.putalpha(lay.split()[3].point(lambda a: int(a * 0.4)))
        can.alpha_composite(sh.filter(ImageFilter.GaussianBlur(10)), (px + 9, py + 13)); can.alpha_composite(lay, (px, py))
        bg_mean = background_mean(spec_bg, (x, y, x + iw, y + ih))
        col = PAL[colors[i]][1]
        if sum((a - b) ** 2 for a, b in zip(bg_mean, col)) ** 0.5 < MIN_CONTRAST:
            low_contrast = True
            warnings.append(f'WARN 「{ln["text"]}」的颜色和背景太接近（背景平均 {bg_mean}）：换一套配色')
        record.append(dict(text=ln['text'], size=size, x=x, y=y, w=iw, h=ih, color=colors[i], layout=layout, tilt=tilt, stroke=stroke))
        y += ih + int(size * 0.02)
    for rr in record:
        for (kx0, ky0, kx1, ky1) in [spec['face']] + spec.get('keep', []):
            ox = min(rr['x'] + rr['w'], kx1) - max(rr['x'], kx0); oy = min(rr['y'] + rr['h'], ky1) - max(rr['y'], ky0)
            if ox > 0 and oy > 0 and ox * oy > 0.04 * (kx1 - kx0) * (ky1 - ky0):
                raise SystemExit(f'「{rr["text"]}」压到脸或要保留的画面上了：换版式、换裁切或缩短文字')
    if len(record) == 2 and record[1]['size'] < record[0]['size']:
        warnings.append(f'WARN 笑点行「{record[1]["text"]}」({record[1]["size"]}px) 比第一行小：缩短笑点行，或把第一行写短')
    return dict(style=style, layout=layout, canvas=can, record=record, low_contrast=low_contrast, warnings=warnings, overridden=overridden)

def select_style(table, content_sha256, attempt, recent_style_ids=()):
    res, selection = ROTATION.select_style(table, content_sha256, attempt=attempt, recent_style_ids=recent_style_ids)
    selection['overridden'] = res['overridden']
    return res, selection

def render(spec, font, recent_style_ids=(), content_sha256=None):
    """Return (cover RGB, record, selection)."""
    lines = spec['lines']
    if 'face' not in spec: raise SystemExit('spec 缺 face（脸在成品里的位置 [x0,y0,x1,y1]）')
    if not 1 <= len(lines) <= (4 if spec.get('layout') in ('left', 'right') else 2):
        raise SystemExit('cover text must be 1-2 lines (a left or right column: up to 4 rows)')
    for ln in lines:
        if len(ln['text']) > 9: raise SystemExit(f'「{ln["text"]}」超过 9 字：缩短文字，不要缩小字号')
    bad = [ln['color'] for ln in lines if 'color' in ln and ln['color'] not in PAL]
    if bad: raise SystemExit(f'未知配色 {bad}：可用 {" ".join(PAL)}')
    if 'tilt' in spec and (isinstance(spec['tilt'], bool) or spec['tilt'] not in ALLOWED_TILTS):
        raise SystemExit(f'tilt {spec["tilt"]} 不在 {list(ALLOWED_TILTS)} 之内')
    miss = missing_glyphs(font, ''.join(ln['text'] for ln in lines))
    if miss: raise SystemExit(f'字体缺字 {"".join(miss)}：改写措辞，不要混用其他字体')
    base = background(spec); spec_bg = base.convert('RGB')
    forced = spec.get('style_id')
    if forced is not None:
        matches = [s for s in STYLES if s['style_id'] == forced]
        if not matches: raise SystemExit(f'style_id {forced!r} 不在风格表里')
        table = {**TABLE, 'styles': matches}
        recent_style_ids = ()
    else:
        table = TABLE
    sha = content_sha256 or default_content_sha256(spec)
    res, selection = select_style(table, sha, lambda style: _attempt(spec, lines, font, base, spec_bg, style), recent_style_ids)
    if forced is not None: selection['selection'] = 'spec_style_id'
    for message in res['warnings']: print(message, file=sys.stderr)
    print(f'layout={res["layout"]} punch={res["record"][-1]["size"]}px style={selection["style_id"]}', file=sys.stderr)
    return res['canvas'].convert('RGB'), res['record'], selection

def main():
    ap = argparse.ArgumentParser(); ap.add_argument('spec'); ap.add_argument('--font', default=os.environ.get('LIDOUSHA_COVER_FONT'))
    ap.add_argument('--styles', default=None, help='path to lidousha/cover_style_rotation.json (or env LIDOUSHA_COVER_STYLES)')
    ap.add_argument('--out-dir', default='.'); ap.add_argument('--grid', action='store_true', help='only write <id>.grid.png to read the face box')
    ap.add_argument('--recent', default='', help='comma-separated style_ids of recent covers, oldest first')
    a = ap.parse_args()
    if a.grid:
        spec = json.load(open(a.spec, encoding='utf-8')); os.makedirs(a.out_dir, exist_ok=True)
        g = background(spec).convert('RGB'); d = ImageDraw.Draw(g); f = ImageFont.load_default(size=28) if hasattr(ImageFont, 'load_default') else None
        for x in range(0, W, 100): d.line([(x, 0), (x, H)], fill=(255, 0, 0) if x % 500 == 0 else (255, 170, 170), width=2); d.text((x + 4, 4), str(x), fill='red', font=f)
        for y in range(0, H, 100): d.line([(0, y), (W, y)], fill=(255, 0, 0) if y % 500 == 0 else (255, 170, 170), width=2); d.text((4, y + 4), str(y), fill='red', font=f)
        d.rectangle((SAFE_X0, 0, SAFE_X1, H - 1), outline=(0, 200, 0), width=4)
        out = os.path.join(a.out_dir, spec.get('id', 'cover') + '.grid.png'); g.save(out); print(out); return
    styles_path = a.styles or os.environ.get('LIDOUSHA_COVER_STYLES')
    if not styles_path:
        raise SystemExit('need --styles or LIDOUSHA_COVER_STYLES pointing at lidousha/cover_style_rotation.json (no built-in default)')
    install_table(load_styles(styles_path))
    if not a.font or not os.path.exists(a.font): raise SystemExit('need --font or LIDOUSHA_COVER_FONT pointing at 锐字真言体')
    spec = json.load(open(a.spec, encoding='utf-8')); os.makedirs(a.out_dir, exist_ok=True)
    recent = [s for s in a.recent.split(',') if s.strip()]
    cover, rec, selection = render(spec, a.font, recent_style_ids=recent); base = os.path.join(a.out_dir, spec.get('id', 'cover'))
    cover.save(base + '.png')
    phone = cover.resize((180, 101), Image.LANCZOS); phone.save(base + '.phone.png')
    phone.resize((540, 303), Image.NEAREST).save(base + '.phone-x3.png')
    json.dump(dict(spec=spec, font=a.font, font_sha256=hashlib.sha256(open(a.font, 'rb').read()).hexdigest(), lines=rec, size=[W, H],
                   cover_style=selection), open(base + '.render.json', 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
    print(base + '.png'); print(base + '.phone-x3.png')

if __name__ == '__main__':
    main()
