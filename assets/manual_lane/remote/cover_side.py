"""A screenshot framed by the cover's composition template: her beside the words (a column or a close-up, on a colour
panel), or framed for the diagonal or bottom words (step 6, 70-cover; cover_candidates.py).

Owner 2026-11-21: the covers of one stream were the same picture, one line top left and one line lower right, in one
colour (同一场的封面全是同一个样子、同一种颜色; said before on 10-31, 11-02 and
11-13).  The lane did take turns at layouts and backgrounds, but only for stickers on a pastel ground; the screenshot
route had one composition (the face as large as the head allows, two lines over the hair), so when a stream shows her on
one unchanging scene every free cover of the day was that picture.  The research the skill rests on (section 6, 同行封面)
says what peers do: the person and the words are both large, the words take a third to a half of the picture, and the
ground behind them is simple (a gradient, dots, a blurred frame).

So a solo frame is framed by the cover's composition template (cover_layout_templates.json): the window is moved and
sized until her face has the template's height and stands at its place, the words go where the template says, and a
column's side gets a panel in one of the lane's background colours (the same table and the same turns as the pastel
routes).  `plan` is plain geometry (cover_layout_rotation.window, the skill renderer's own) and says why when the frame
has no such window; then the template is refused and the run tries the next one.
"""
from PIL import Image, ImageDraw, ImageFilter

import cover_face_crop as crop_mod
from cover_compose import layouts
import cover_text_fit as text_fit
from cover_layout import FACE_GAP

FRAME_W, FRAME_H = 1920, 1080


def plan(face, head_top, keep, rules, template, src_w=FRAME_W, src_h=FRAME_H):
    """({crop, face, face_px, target_px, limited_by, pad}, None) for the window [x0, y0, width] of the live frame that frames
    her as `template` says (cover_layout_rotation.window: the skill renderer crops the same way), or (None, why).  A
    template with a panel (the words on a plain ground beside her) may reach past the frame's edge on the words' side
    (`pad`: the frame is extended there before the panel is painted).  Whether the words then fit is the renderer's."""
    side = template["text"] if template["framing"].get("panel") and template["text"] in ("left", "right") else None
    return layouts.window(face, head_top, keep, template["framing"], src_w=src_w, src_h=src_h,
                          chin_margin_frac=rules["chin_margin_frac"], floor_frac=rules["face_floor_frac"], pad_side=side)


def solo(run, spec, face, head_top, clock):
    """Crop a solo frame for the screenshot route in the run's composition template and record the plan: the template's
    window (and its panel for the words, cover_background.panel) when it frames her itself, else (the top template) as
    tight as the head allows with the two lines on top.  A frame without the template's window refuses the template."""
    comp = run.column
    template = comp.template
    if comp.free():
        with Image.open(spec["frame"]) as im:
            src_w, src_h = im.size
        made, why = plan(face, head_top, spec.get("keep") or [], run.rules, template, src_w, src_h)
        if made is None:
            raise layouts.TemplateRefused(why)
        run.plans["screenshot"] = dict(made, head_top=head_top, template_id=template["template_id"])
        if made["pad"]:                 # the panel is painted on the extended frame (cover_background.panel, paint)
            spec["pad"] = made["pad"]
            shift = made["pad"]["px"] if made["pad"]["side"] == "left" else 0
            for field in ("blur", "keep"):
                spec[field] = [[b[0] + shift, b[1], b[2] + shift, b[3]] for b in spec.get(field) or []]
        crop_mod.apply_crop(spec, made["crop"])
        spec.update(comp.text("screenshot", spec["lines"]))
        if made["pad"] or (template["framing"].get("panel") and ["screenshot", template["text"], "picture_panel"]
                           not in (run.job.get("session_compositions") or [])):
            spec["panel"] = {"side": template["text"], "edge": panel_edge(template["text"], made["face"], run.rules)}
        return
    ink = run.text_ink(spec)[1]
    need = text_fit.need_face_top(ink, len(spec["lines"]), run.rules, run.rules["punch_floor_px"])
    with clock.phase("layout"):
        run.plans["screenshot"] = dict(crop_mod.plan_crop(face, head_top, spec.get("keep") or [], run.rules, need),
                                       head_top=head_top, text_top_wanted=need)
    crop_mod.apply_crop(spec, run.plans["screenshot"]["crop"])
    spec.update(comp.text("screenshot", spec["lines"]))


def pad_shift(spec):
    """How far right the frame's own pixels sit in the extended frame (a pad on the left), else 0."""
    pad = spec.get("pad") or {}
    return int(pad["px"]) if pad.get("side") == "left" else 0


def in_frame(spec, frame):
    """The part of the cover's window that is the frame's own pixels, as a box in the frame (cover_dead_panel), or the
    window itself when nothing is padded."""
    if not spec.get("pad") or not spec.get("crop"):
        return spec.get("crop")
    x, y, w = (int(v) for v in spec["crop"][:3])
    with Image.open(frame) as im:
        src_w = im.width
    x -= pad_shift(spec)
    return [max(0, x), y, min(src_w, x + w), y + round(w * FRAME_H / FRAME_W)]


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
    if spec.get("pad"):         # the window reaches past the frame on the words' side: the frame is extended there first
        px, pad_side = int(spec["pad"]["px"]), spec["pad"]["side"]
        wider = Image.new("RGB", (picture.width + px, picture.height), tuple(int(colors[0][i:i + 2], 16) for i in (1, 3, 5)))
        wider.paste(picture, (px if pad_side == "left" else 0, 0))
        picture = wider
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
    spec.pop("pad", None)       # the picture written is the extended one
    return shape
