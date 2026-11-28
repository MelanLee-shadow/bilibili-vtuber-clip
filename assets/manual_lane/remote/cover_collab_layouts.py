"""Free collaboration compositions using only the already bound host and guest pictures.

The existing renderer still places and checks the words.  This module prepares its picture and the actual face boxes;
it never draws or invents a person.  Pack pictures have already passed the hash/readability gate.
"""
import math
import os

from PIL import Image, ImageChops, ImageDraw, ImageEnhance, ImageFilter, ImageOps

W, H = 1920, 1080


def portrait(canvas, person, center, diameter, colour):
    """Paste a round portrait, retaining the measured face and returning its box on the canvas."""
    fx0, fy0, fx1, fy1 = person["face"]
    fw, fh = fx1 - fx0, fy1 - fy0
    # Keep even a wide chibi face inside the circle while its height stays above the host's 25% floor in the large cards.
    side = max(fh / 0.46, fw / 0.84)
    cx, cy = (fx0 + fx1) / 2, (fy0 + fy1) / 2
    x0, y0 = cx - side / 2, cy - side * 0.51
    with Image.open(person["src"]) as original:
        image = original.convert("RGBA")
        # PIL's crop may extend beyond a source edge: those pixels remain transparent in the round badge.
        crop = image.crop((math.floor(x0), math.floor(y0), math.ceil(x0 + side), math.ceil(y0 + side)))
    side_x, side_y = crop.size
    crop = crop.resize((diameter, diameter), Image.Resampling.LANCZOS)
    x, y = round(center[0] - diameter / 2), round(center[1] - diameter / 2)
    ring = ImageDraw.Draw(canvas)
    ring.ellipse((x - 9, y - 9, x + diameter + 9, y + diameter + 9), fill="white")
    badge = Image.new("RGBA", (diameter, diameter), colour)
    badge.alpha_composite(crop)
    mask = Image.new("L", (diameter, diameter), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, diameter - 1, diameter - 1), fill=255)
    canvas.paste(badge, (x, y), mask)
    return [round(x + (fx0 - math.floor(x0)) * diameter / side_x),
            round(y + (fy0 - math.floor(y0)) * diameter / side_y),
            round(x + (fx1 - math.floor(x0)) * diameter / side_x),
            round(y + (fy1 - math.floor(y0)) * diameter / side_y)]


def frame_background(host, face_x, face_top, face_height, blur):
    """Place the real frame by the host's measured face; extend exposed edges with the same frame blurred."""
    with Image.open(host["src"]) as image:
        source = image.convert("RGB")
    for box in blur or []:
        mask = Image.new("L", source.size, 0)
        ImageDraw.Draw(mask).rectangle(tuple(box), fill=255)
        source = Image.composite(source.filter(ImageFilter.GaussianBlur(22)), source,
                                 mask.filter(ImageFilter.GaussianBlur(30)))
    source = ImageEnhance.Color(source).enhance(1.10)
    face = host["face"]
    scale = face_height / (face[3] - face[1])
    dx, dy = face_x - (face[0] + face[2]) * scale / 2, face_top - face[1] * scale
    base = ImageOps.fit(source, (W, H)).filter(ImageFilter.GaussianBlur(40)).convert("RGBA")
    transformed = source.convert("RGBA").transform((W, H), Image.Transform.AFFINE,
        (1 / scale, 0, -dx / scale, 0, 1 / scale, -dy / scale), Image.Resampling.BICUBIC)
    # When the frame is shifted down to make room for words, feather its boundary into its own blurred extension.
    alpha = transformed.getchannel("A").filter(ImageFilter.GaussianBlur(28))
    if dy > 0:
        fade = Image.new("L", (W, H), 255)
        ImageDraw.Draw(fade).rectangle((0, 0, W, round(dy)), fill=0)
        fade.paste(Image.linear_gradient("L").resize((W, 150)), (0, round(dy)))
        alpha = ImageChops.multiply(alpha, fade)
    transformed.putalpha(alpha)
    base.alpha_composite(transformed)
    placed = [round(dx + face[0] * scale), round(dy + face[1] * scale),
              round(dx + face[2] * scale), round(dy + face[3] * scale)]
    return base, placed


def build(run, route, host, family, blur):
    """Return (path, faces, grounds, row) for a supported free scene; refuse if its prerequisites are absent."""
    guests = list(run.pack)
    if len(guests) not in ((1, 2) if family == "dialogue" else (2,)):
        raise ValueError("COLLAB_LAYOUT_REQUIRES_TWO_GUESTS")
    row = list(run.row)
    people = {run.host_name: dict(host, name=run.host_name), **{g["name"]: g for g in guests}}
    colours = ("#dceeff", "#f9dfed", "#fff0c8")
    if family == "dialogue":
        canvas = run.renderer.pastel({"bg": ["#dceeff", "#fce4ef"]}).convert("RGBA")
        draw = ImageDraw.Draw(canvas)
        for top, bottom in ((24, 185), (180, 390)):
            # A dark card supports the renderer's light text and its white outline as well as darker outlined type.
            draw.rounded_rectangle((175, top, 1745, bottom), radius=42, fill=(48, 53, 79, 235))
        centres = (600, 1320) if len(row) == 2 else (320, 960, 1600)
        faces = {name: portrait(canvas, people[name], (x, 760), 620, colours[i])
                 for i, (name, x) in enumerate(zip(row, centres, strict=True))}
    else:
        if host["kind"] != "frame":
            raise ValueError("COLLAB_LAYOUT_REQUIRES_SCREENSHOT")
        words = run.rules["face_top_default_px"]
        # The top two lines use the normal renderer; all faces stay below its measured text zone.
        face_top = max(600, words)
        # Leave room on the right for the second guest; the crop also avoids copying more of a busy stream sidebar.
        canvas, host_face = frame_background(host, 660 if family == "portraits" else 710, face_top, 380, blur)
        faces = {run.host_name: host_face}
        if family == "stacked":
            panel = run.renderer.pastel({"bg": ["#f1e5ff", "#dceeff"]}).convert("RGBA")
            canvas.paste(panel.crop((1250, 0, W, H)), (1250, 0))
            placements = ((1550, 650), (1550, 930))
        elif family == "portraits":
            placements = ((230, 865), (1690, 865))
        else:
            raise ValueError("COLLAB_LAYOUT_UNKNOWN")
        for i, (guest, center) in enumerate(zip(guests, placements, strict=True)):
            faces[guest["name"]] = portrait(canvas, guest, center, 280 if family == "stacked" else 340, colours[i + 1])
    out = os.path.join(run.out, f"{route}.{family}.png")
    canvas.convert("RGB").save(out)
    return out, faces, {}, row
