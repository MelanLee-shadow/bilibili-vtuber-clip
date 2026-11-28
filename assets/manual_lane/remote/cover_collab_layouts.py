"""Free collaboration compositions using only the already bound host and guest pictures.

The existing renderer still places and checks the words.  This module prepares its picture and the actual face boxes;
it never draws or invents a person.  Pack pictures have already passed the hash/readability gate.
"""
import math
import os

from PIL import Image, ImageChops, ImageDraw, ImageEnhance, ImageFilter, ImageOps

W, H = 1920, 1080


def portrait_crop_box(face, clean_rects, diameter, face_floor_px=270):
    """Square crop `(x0, y0, x1, y1, used_clean)` for `portrait`, constrained inside `clean_rects` when given."""
    fx0, fy0, fx1, fy1 = face
    fw, fh = fx1 - fx0, fy1 - fy0
    side_target = max(fh / 0.46, fw / 0.84)
    cx, cy = (fx0 + fx1) / 2, (fy0 + fy1) / 2
    if not clean_rects:
        x0, y0 = cx - side_target / 2, cy - side_target * 0.51
        return math.floor(x0), math.floor(y0), math.ceil(x0 + side_target), math.ceil(y0 + side_target), None
    best = None
    best_side = 0
    for clean in clean_rects:
        rx0, ry0, rx1, ry1 = clean
        if not (rx0 <= fx0 and fx1 <= rx1 and ry0 <= fy0 and fy1 <= ry1):
            continue
        max_side = min(rx1 - rx0, ry1 - ry0)
        hi_side = min(max_side, math.floor(diameter * fh / max(1, face_floor_px)))
        lo_side = max(1, math.ceil(math.hypot(fw, fh)))
        stop_side = max(lo_side, best_side + 1)
        if hi_side < stop_side:
            continue
        for iside in range(hi_side, stop_side - 1, -1):
            if diameter * fh / iside < face_floor_px - 1e-9:
                continue
            x_lo = max(rx0, math.ceil(fx1 - iside))
            x_hi = min(rx1 - iside, math.floor(fx0))
            y_lo = max(ry0, math.ceil(fy1 - iside))
            y_hi = min(ry1 - iside, math.floor(fy0))
            if x_lo > x_hi or y_lo > y_hi:
                continue
            r = iside / 2
            xs = [min(max(round(cx - r), x_lo), x_hi),
                  min(max(math.floor(cx - r), x_lo), x_hi),
                  min(max(math.ceil(cx - r), x_lo), x_hi)]
            ys = [min(max(round(cy - iside * 0.51), y_lo), y_hi),
                  min(max(round(cy - r), y_lo), y_hi),
                  min(max(math.floor(cy - r), y_lo), y_hi),
                  min(max(math.ceil(cy - r), y_lo), y_hi)]
            for ix0 in dict.fromkeys(xs):
                for iy0 in dict.fromkeys(ys):
                    ccx, ccy = ix0 + r, iy0 + r
                    if all((px - ccx) ** 2 + (py - ccy) ** 2 <= r * r + 1e-9 for px in (fx0, fx1) for py in (fy0, fy1)):
                        best = (ix0, iy0, ix0 + iside, iy0 + iside, list(clean))
                        best_side = iside
                        break
                if best_side == iside:
                    break
            if best_side == iside:
                break
    if best is not None:
        return best
    raise ValueError("COLLAB_CLEAN_CROP_TOO_SMALL")


def portrait(canvas, person, center, diameter, colour, face_floor_px=270):
    """Paste a round portrait, retaining the measured face and returning its box on the canvas."""
    fx0, fy0, fx1, fy1 = person["face"]
    cx0, cy0, cx1, cy1, used_clean = portrait_crop_box(person["face"], person.get("clean_rects"), diameter, face_floor_px)
    if used_clean is not None:
        person["clean_used"] = used_clean
    with Image.open(person["src"]) as original:
        image = original.convert("RGBA")
        # PIL's crop may extend beyond a source edge when clean_rects is None: transparent in the round badge.
        crop = image.crop((cx0, cy0, cx1, cy1))
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
    return [round(x + (fx0 - cx0) * diameter / side_x),
            round(y + (fy0 - cy0) * diameter / side_y),
            round(x + (fx1 - cx0) * diameter / side_x),
            round(y + (fy1 - cy0) * diameter / side_y)]


def frame_background(host, face_x, face_top, face_height, blur, visible_w=W):
    """Place the real frame by the host's measured face; extend exposed edges with the same frame blurred."""
    with Image.open(host["src"]) as image:
        source = image.convert("RGB")
    face = host["face"]
    scale = face_height / (face[3] - face[1])
    cleans = host.get("clean_rects")
    cx0, cy0, cx1, cy1 = 0, 0, source.width, source.height
    dx = face_x - (face[0] + face[2]) * scale / 2
    dy = face_top - face[1] * scale
    if cleans:
        chosen = None
        for cand in cleans:
            rx0, ry0, rx1, ry1 = cand
            if (rx1 - rx0) * scale < visible_w:
                continue
            cand_dx = min(max(dx, visible_w - rx1 * scale), -rx0 * scale)
            cand_dy = max(dy, H - ry1 * scale)
            placed_x0, placed_x1 = cand_dx + face[0] * scale, cand_dx + face[2] * scale
            placed_y0, placed_y1 = cand_dy + face[1] * scale, cand_dy + face[3] * scale
            if placed_x0 >= 30 and placed_x1 <= visible_w - 30 and placed_y0 >= face_top - 1 and placed_y1 <= H + 1:
                chosen = (cand, cand_dx, cand_dy)
                break
        if chosen is None:
            raise ValueError("COLLAB_CLEAN_CROP_TOO_SMALL")
        (cx0, cy0, cx1, cy1), dx, dy = chosen
        host["clean_used"] = [cx0, cy0, cx1, cy1]
        source = source.crop((cx0, cy0, cx1, cy1))
        blur = [[b[0] - cx0, b[1] - cy0, b[2] - cx0, b[3] - cy0] for b in blur or []]
        blur = [b for b in blur if b[0] < source.width and b[2] > 0 and b[1] < source.height and b[3] > 0]
    for box in blur or []:
        mask = Image.new("L", source.size, 0)
        ImageDraw.Draw(mask).rectangle(tuple(box), fill=255)
        source = Image.composite(source.filter(ImageFilter.GaussianBlur(22)), source,
                                 mask.filter(ImageFilter.GaussianBlur(30)))
    source = ImageEnhance.Color(source).enhance(1.10)
    dx_local = dx + cx0 * scale
    dy_local = dy + cy0 * scale
    base = ImageOps.fit(source, (W, H)).filter(ImageFilter.GaussianBlur(40)).convert("RGBA")
    transformed = source.convert("RGBA").transform((W, H), Image.Transform.AFFINE,
        (1 / scale, 0, -dx_local / scale, 0, 1 / scale, -dy_local / scale), Image.Resampling.BICUBIC)
    # When the frame is shifted down to make room for words, feather its boundary into its own blurred extension.
    alpha = transformed.getchannel("A").filter(ImageFilter.GaussianBlur(28))
    if dy_local > 0:
        fade = Image.new("L", (W, H), 255)
        ImageDraw.Draw(fade).rectangle((0, 0, W, round(dy_local)), fill=0)
        fade.paste(Image.linear_gradient("L").resize((W, 150)), (0, round(dy_local)))
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
    face_floor_px = round(run.rules["face_floor_frac"] * H)
    if family == "dialogue":
        canvas = run.renderer.pastel({"bg": ["#dceeff", "#fce4ef"]}).convert("RGBA")
        draw = ImageDraw.Draw(canvas)
        for top, bottom in ((24, 185), (180, 390)):
            # A dark card supports the renderer's light text and its white outline as well as darker outlined type.
            draw.rounded_rectangle((175, top, 1745, bottom), radius=42, fill=(48, 53, 79, 235))
        centres = (600, 1320) if len(row) == 2 else (320, 960, 1600)
        faces = {name: portrait(canvas, people[name], (x, 760), 620, colours[i],
                                face_floor_px if name == run.host_name else 120)
                 for i, (name, x) in enumerate(zip(row, centres, strict=True))}
        if people[run.host_name].get("clean_used"):
            host["clean_used"] = people[run.host_name]["clean_used"]
    else:
        if host["kind"] != "frame":
            raise ValueError("COLLAB_LAYOUT_REQUIRES_SCREENSHOT")
        words = run.rules["face_top_default_px"]
        # The top two lines use the normal renderer; all faces stay below its measured text zone.
        face_top = max(600, words)
        # Leave room on the right for the second guest; the crop also avoids copying more of a busy stream sidebar.
        visible_w = 1250 if family == "stacked" else W
        canvas, host_face = frame_background(host, 660 if family == "portraits" else 710, face_top, 380, blur,
                                             visible_w=visible_w)
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
            faces[guest["name"]] = portrait(canvas, guest, center, 280 if family == "stacked" else 340, colours[i + 1], 120)
    out = os.path.join(run.out, f"{route}.{family}.png")
    canvas.convert("RGB").save(out)
    return out, faces, {}, row
