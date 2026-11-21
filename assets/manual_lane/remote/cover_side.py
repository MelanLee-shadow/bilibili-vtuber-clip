"""A screenshot with the words in a column beside her, on a colour panel (step 6, 70-cover; cover_candidates.py).

Owner 2026-11-21: the covers of one stream were the same picture, one line top left and one line lower right, in one
colour (「今天这个新切片全都是同一个标题封面……颜色太单一……总是左上一行，然后右下一行」; said before on 10-31, 11-02 and
11-13).  The lane did take turns at layouts and backgrounds, but only for stickers on a pastel ground; the screenshot
route had one composition (the face as large as the head allows, two lines over the hair), so when a stream shows her on
one unchanging scene every free cover of the day was that picture.  The research the skill rests on (section 6, 同行封面)
says what peers do: the person and the words are both large, the words take a third to a half of the picture, and the
ground behind them is simple (a gradient, dots, a blurred frame).

So a solo frame has a second composition: the window is moved until her face stands at the column's place
(`column_face_center_x_frac`, or its mirror), the words stack on the other side, and that side gets a panel in one of
the lane's background colours (the same table and the same turns as the pastel routes), which is what changes the
colour of the cover from one clip to the next.  `plan` is plain geometry and says why when the frame has no such
window; then the route keeps the two lines on top.
"""
import math

from PIL import Image, ImageDraw, ImageFilter

import cover_face_crop as crop_mod
import cover_text_fit as text_fit
from cover_layout import FACE_GAP, SAFE_X0, SAFE_X1

FRAME_W, FRAME_H = 1920, 1080


def plan(face, head_top, keep, rules, side, row_px):
    """({crop, face, face_px, zone_px, target_px, limited_by}, None) for the window [x0, y0, width] of the live frame
    with the words on `side` (left | right) and her on the other side, or (None, why).  `face` is her face in frame
    pixels, `head_top` the top of her head, `keep` the other boxes that must stay in the window, `row_px` the width of
    the widest punchline row at the floor size (it must fit between the safe edge and her face).  The window gives her
    face the target height unless her head and chin need a wider one (never one that cuts them, never one that puts the
    face under the veto floor), and her face centre stands at the column's place as far as the frame's edges allow."""
    if head_top is None:
        return None, "HEAD_TOP_UNMEASURED"
    frac = rules["column_face_center_x_frac"] if side == "left" else 1 - rules["column_face_center_x_frac"]
    cx, fh = (face[0] + face[2]) / 2, face[3] - face[1]
    margin = round(rules["chin_margin_frac"] * fh)
    top = max(0, min([head_top, *[b[1] for b in keep]]))
    bottom = min(FRAME_H, max([face[3] + margin, *[b[3] for b in keep]]))
    target = round(rules["face_target_frac"] * FRAME_H)
    w_tall = math.ceil((bottom - top) * FRAME_W / FRAME_H)              # her head and chin fit in its height
    w_floor = FRAME_W * fh / (rules["face_floor_frac"] * FRAME_H)       # wider than this the face is under the veto floor
    w = min(FRAME_W, max(math.floor(FRAME_W * fh / target), w_tall))
    if w > w_floor:
        return None, "COLUMN_FACE_UNDER_FLOOR"
    h = round(w * FRAME_H / FRAME_W)
    wish = round(cx - frac * w)
    x0 = min(max(wish, 0), FRAME_W - w)
    y0 = min(FRAME_H - h, top)
    if y0 + h < bottom or any(b[0] < x0 or b[2] > x0 + w for b in keep):
        return None, "COLUMN_KEEPS_OUTSIDE"
    out = crop_mod.frame_face(face, [x0, y0, w])
    zone = out[0] - FACE_GAP - SAFE_X0 if side == "left" else SAFE_X1 - out[2] - FACE_GAP
    if row_px > zone:
        return None, "COLUMN_NO_ROOM"
    limited = "FRAME_EDGE" if x0 != wish else None if out[3] - out[1] >= target - 1 else "HEAD_AND_CHIN"
    return {"crop": [x0, y0, w], "face": out, "face_px": out[3] - out[1], "zone_px": zone, "target_px": target,
            "limited_by": limited}, None


def solo(run, spec, face, head_top, clock):
    """Crop a solo frame for the screenshot route and record the plan: beside the column when it is this cover's turn and
    the frame has such a window (then the spec carries the column's rows and asks for the panel), else as tight as the
    head allows with the two lines on top."""
    col = run.column
    if col.side and len(col.rows) >= 2 and "screenshot" not in col.refused:
        wide, size = col.punch_row(run)
        made, why = plan(face, head_top, spec.get("keep") or [], run.rules, col.side, wide)
        if made:
            col.placed["screenshot"] = {"zone_px": made["zone_px"], "punch_row_px": wide, "at_size": size}
            run.plans["screenshot"] = dict(made, head_top=head_top, text_layout=col.side)
            crop_mod.apply_crop(spec, made["crop"])
            spec.update(col.text("screenshot", spec["lines"]))
            # the panel is the column's first look in a stream; a second column on the same side stands on the picture
            if ["screenshot", col.side, "picture_panel"] not in (run.job.get("session_compositions") or []):
                spec["panel"] = {"side": col.side, "edge": panel_edge(col.side, made["face"], run.rules)}
            return
        col.refused["screenshot"] = {"reason": why, "punch_row_px": wide, "at_size": size}
    ink = run.text_ink(spec)[1]
    need = text_fit.need_face_top(ink, len(spec["lines"]), run.rules, run.rules["punch_floor_px"])
    with clock.phase("layout"):
        run.plans["screenshot"] = dict(crop_mod.plan_crop(face, head_top, spec.get("keep") or [], run.rules, need),
                                       head_top=head_top, text_top_wanted=need)
    crop_mod.apply_crop(spec, run.plans["screenshot"]["crop"])


def note(spec):
    """What the judge is told this candidate is."""
    if spec.get("panel"):
        return "直播截图把她放在一侧，另一侧是一块纯色渐变底板，封面字叠成竖列放在底板上（底板和字是排版，不是画面内容）"
    return "直播截图整张当底，弹幕框模糊"


def panel_edge(side, face, rules):
    """The x the panel's slanted edge passes at mid height: as near her face as leaves the gap beside the face box at
    every row of the face (the edge leans towards her at the top, so the face's top row decides)."""
    lean = rules["panel_slant_px"] * max(0.0, 1 - 2 * face[1] / FRAME_H)
    return round(face[0] - FACE_GAP - lean) if side == "left" else round(face[2] + FACE_GAP + lean)


def panel_shape(side, edge, rules):
    """The panel's corners on the cover: from the frame's edge on `side` to a slanted line through `edge` at mid
    height, leaning `panel_slant_px` each way (towards her at the top)."""
    slant = rules["panel_slant_px"]
    if side == "left":
        return [(0, 0), (edge + slant, 0), (edge - slant, FRAME_H), (0, FRAME_H)]
    return [(FRAME_W, 0), (edge - slant, 0), (edge + slant, FRAME_H), (FRAME_W, FRAME_H)]


def paint(frame, spec, colors, rules, out):
    """Write the frame with the panel painted on it to `out` and point the spec at it.  The panel is painted in the
    frame's own pixels (the cover's shape carried back through the crop), so the crop stays what it was and the face is
    measured through it as before.  The panel is the background's gradient (top colour to bottom colour, the renderer's
    own order, over the cover's height) with a dot grid and a white rule along its slanted edge; a blur box under the
    panel is dropped (nothing of the stream shows there)."""
    x0, y0, w = spec["crop"]
    scale = w / FRAME_W
    h = round(w * FRAME_H / FRAME_W)
    side, edge = spec["panel"]["side"], spec["panel"]["edge"]
    picture = Image.open(frame).convert("RGB")
    c0, c1 = ([int(c[i:i + 2], 16) for i in (1, 3, 5)] for c in colors)
    column = Image.new("RGB", (1, h))
    column.putdata([tuple(round(c0[i] + (c1[i] - c0[i]) * y / max(1, h - 1)) for i in range(3)) for y in range(h)])
    ground = column.resize((w, h))
    step, dot = rules["panel_dot_step_px"] * scale, rules["panel_dot_radius_px"] * scale
    dots = ImageDraw.Draw(ground)
    for row in range(int(h / step) + 1):
        y = step / 2 + row * step
        if y >= h:
            break
        lift = tuple(min(255, v + rules["panel_dot_lift"]) for v in ground.getpixel((0, int(y))))
        for col in range(int(w / step) + 1):
            x = step / 2 + (row % 2) * step / 2 + col * step
            dots.ellipse((x - dot, y - dot, x + dot, y + dot), fill=lift)
    shape = panel_shape(side, edge, rules)
    local = [(px * scale, py * scale) for px, py in shape]
    mask = Image.new("L", (w, h), 0)
    ImageDraw.Draw(mask).polygon(local, fill=255)
    window = picture.crop((x0, y0, x0 + w, y0 + h))
    window.paste(ground, (0, 0), mask.filter(ImageFilter.GaussianBlur(max(0.5, 1.5 * scale))))
    ImageDraw.Draw(window).line([local[1], local[2]], fill=(255, 255, 255), width=max(1, round(rules["panel_rule_px"] * scale)))
    picture.paste(window, (x0, y0))
    picture.save(out)
    covered = (lambda b: (b[0] + b[2]) / 2 < edge) if side == "left" else (lambda b: (b[0] + b[2]) / 2 > edge)
    spec.update(frame=out, composition="picture_panel", blur=[b for b in spec.get("blur") or [] if not covered(b)])
    return shape
