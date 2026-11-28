"""Face-driven placement of the person on a collage cover (step 6, 70-cover; cover_candidates.py on the media host).

The approved collage was laid out from the face, not from the picture: scale the sticker so its face is the target
height (lane_rules `cover.face_target_frac`), put the face top where the two lines above it have room for the punchline
floor, centre the face a little right of the middle, and let the lower body leave the frame.  `solve` does that on plain
numbers; `place_solo` applies it to one real picture (a drawn sticker, a cut-out, an official emote) whose face was
measured in that picture.  No guessed face fractions: a picture without a measured face is not laid out (the caller
records the route as unmakeable).

The order in which the three wishes give way to each other is lane_rules `cover.layout_order` (with its source): the
face never goes under the veto floor, then the punchline floor, then the face target.
"""
import math

FRAME_W, FRAME_H = 1920, 1080


def _evaluate(top, ft, fb, rules, pad, face_frac=None):
    """(layer height, face height, limited_by) for a face whose top sits at `top` and which spans the fractions ft..fb of
    the layer: head room above the face is at most `top` (the picture cannot leave through the top), the face and the
    chin margin must stay above the frame's bottom, and the face is never larger than the target (`face_frac` of the
    height: the composition template's, else lane_rules cover.face_target_frac)."""
    span = fb - ft
    target = (face_frac or rules["face_target_frac"]) * FRAME_H
    h, limit = target / span, None
    head = (top - pad - rules["layer_top_min_px"]) / ft if ft > 0 else math.inf
    if head < h:
        h, limit = head, "HEAD_ABOVE_FACE"
    room = (FRAME_H - top) / (1 + rules["chin_margin_frac"]) / span
    if room < h:
        h, limit = room, "FRAME_BOTTOM"
    return h, h * span, limit


def solve(ft, fb, rules, need_top, pad=0, face_frac=None, face_top=None):
    """Where the face top goes and how tall the layer is.  `ft`, `fb`: the face's top and bottom as fractions of the
    layer's alpha height; `need_top`: the face top the punchline's floor size needs (None: not reachable, e.g. a line too
    wide for the safe zone).  Every face top is tried and the best by `layout_order` wins (each wish met beats any
    later one), then the larger face, then the higher face (more room for the text).  A composition template may ask
    for its own face height (`face_frac`) and for the face top (`face_top`, px: the highest the face may stand; the
    words go under it): then only the tops from there down are tried."""
    target = round((face_frac or rules["face_target_frac"]) * FRAME_H)
    floor = round(rules["face_floor_frac"] * FRAME_H)
    want = need_top if need_top is not None else rules["face_top_default_px"]
    best = None
    tops = (range(rules["face_top_min_px"], FRAME_H - floor, 2) if face_top is None else
            range(max(2, round(face_top)), max(3, round(face_top) + 1 + round(0.12 * FRAME_H)), 2))
    for top in tops:
        h, fh, limit = _evaluate(top, ft, fb, rules, pad, face_frac)
        if face_top is not None:       # the template's face top: the first top whose face reaches the target, else the largest
            met = {"face_floor": fh >= floor, "punch_floor": True, "face_target": fh >= target - 1}
            key = (met["face_floor"], met["face_target"], -top if met["face_target"] else round(fh))
            if best is None or key > best[0]:
                best = (key, top, h, fh, limit, met)
            continue
        met = {"face_floor": fh >= floor, "punch_floor": top >= want and need_top is not None, "face_target": fh >= target - 1}
        key = (tuple(met[name] for name in rules["layout_order"]), round(min(fh, target)), -top)
        if best is None or key > best[0]:
            best = (key, top, h, fh, limit, met)
    _, top, h, fh, limit, met = best
    return {"face_top": top, "layer_h": max(1, round(h)), "face_px": round(fh), "target_px": target, "floor_px": floor,
            "need_top": need_top, "met": met,
            "limited_by": None if met["face_target"] else (limit or "PUNCH_TEXT_ZONE")}


def _bleed_fits(ft, fb, rules, need_top):
    """A figure whose head is cut by its source picture can leave the cut on the canvas edge (picture top = canvas top) only
    when, at the target face height, the face top still has the room the punchline needs and the body fits the frame."""
    h = rules["face_target_frac"] * FRAME_H / (fb - ft)
    top = ft * h
    return top >= (need_top if need_top is not None else rules["face_top_default_px"]) and top + (fb - ft) * h * (
        1 + rules["chin_margin_frac"]) <= FRAME_H


def place_solo(layer, face, renderer, rules, need_top, host_side, centre_frac=None, face_frac=None, face_top=None):
    """One person's picture placed by the measured face.  `layer`: {src, cutout, crop?} (crop: the region of the source
    kept); `face`: the face box in the source picture's pixels.  Returns (sticker dict, face box in cover pixels, plan);
    raises ValueError("HOST_FACE_OUTSIDE_FIGURE") when the face is not inside the person's alpha box (the picture shows
    no face, e.g. only legs), and ValueError("FIGURE_CUT_ENDS_ABOVE_FRAME_BOTTOM") when the picture cuts the figure
    flat at its bottom (an emote cut at the waist, a cut-out cut by the frame) and the placement leaves that cut inside
    the canvas (a face placed high and small by a composition template: the template is then passed over).  `centre_frac`: where the face centre goes, when the cover has another main layer or the
    composition template says; `face_frac`, `face_top`: the template's face height and face top (solve)."""
    from PIL import Image

    with Image.open(layer["src"]) as raw:
        img = renderer.cutout(raw, layer["cutout"])
    crop = list(layer.get("crop") or [0, 0, img.width, img.height])
    bbox = img.crop(tuple(crop)).getchannel("A").getbbox() or (0, 0, crop[2] - crop[0], crop[3] - crop[1])
    ox, oy = crop[0] + bbox[0], crop[1] + bbox[1]
    bw, bh = bbox[2] - bbox[0], bbox[3] - bbox[1]
    if not (face[0] >= ox - 2 and face[2] <= ox + bw + 2 and face[1] >= oy - 2 and face[3] <= oy + bh + 2):
        raise ValueError("HOST_FACE_OUTSIDE_FIGURE")
    outline = int(layer.get("outline", 12))
    ft, fb = max(0.0, (face[1] - oy) / bh), min(1.0, (face[3] - oy) / bh)
    cut = crop[1] <= 0 and oy <= 1                    # the figure runs into the source picture's own top edge: a flat cut
    cut_bottom = bbox[3] >= crop[3] - crop[1] - 1     # ... or into the kept region's bottom edge (an emote cut at the waist)
    bleed = cut and face_top is None and _bleed_fits(ft, fb, rules, need_top)
    flush = layer.get("flush")                        # L | R: the source picture cuts the figure there (cover_emote_layer)
    if bleed or flush:                                # no outline: the cut edge is then the canvas edge exactly
        outline = 0
    pad = outline + 4 if outline else 0               # the renderer pads the picture by the outline before it pastes
    plan = solve(ft, fb, rules, need_top, pad, face_frac, face_top)
    if bleed and face_top is None:
        h_target = round((face_frac or rules["face_target_frac"]) * FRAME_H / (fb - ft))
        plan.update(face_top=round(ft * h_target), layer_h=h_target, limited_by=None)
        plan["met"] = dict(plan["met"], punch_floor=True, face_target=True)
    h = plan["layer_h"]
    s = h / bh
    w = round(bw * s)
    centre = (centre_frac if centre_frac is not None else rules["sticker_face_center_x_frac"]) * FRAME_W
    if host_side == "left" and centre_frac is None:
        centre = FRAME_W - centre
    fx0, fx1 = (face[0] - ox) * s, (face[2] - ox) * s
    x = round(centre - (fx0 + fx1) / 2 - pad)
    x = max(0, min(x, round(FRAME_W - pad - fx1)))
    if flush:                                         # that side on the canvas edge, wherever the face then stands
        x = 0 if flush == "L" else FRAME_W - w
    y = round(plan["face_top"] - pad - ft * h)
    sticker = {"src": layer["src"], "cutout": layer["cutout"], "crop": crop, "x": x, "y": max(0, y), "h": h,
               "measure_outline": True}
    if "outline" in layer or bleed or flush:
        sticker["outline"] = 0 if bleed or flush else layer["outline"]
    if cut_bottom and max(0, y) + pad + h < FRAME_H:  # the bottom cut would stand inside the canvas, a straight edge across her
        raise ValueError("FIGURE_CUT_ENDS_ABOVE_FRAME_BOTTOM")
    box = [round(x + pad + fx0), round(max(0, y) + pad + ft * h), round(x + pad + fx1), round(max(0, y) + pad + fb * h)]
    plan.update(width=w, layer_xy=[x, max(0, y)], face=box, face_px=box[3] - box[1])
    if flush:
        plan["side_cut"] = {"side": flush, "at": "CANVAS_EDGE"}
    if cut:                                           # recorded either way: a flat cut inside the canvas is the source's, not the layout's
        plan["head_cut"] = "AT_CANVAS_TOP" if bleed else "INSIDE_CANVAS"
        plan["cut_edge_y"] = max(0, y) + pad
    return sticker, box, plan


SAFE_X0, SAFE_X1, FACE_GAP = 260, 1660, 30      # the renderer's feed-safe zone and the gap it keeps beside the face


class Composition:
    """The composition template a candidate is being made in (the channel's cover_layout_templates.json through the
    shared selector cover_layout_rotation; owner 2026-10-31, 2026-11-13, 2026-11-25: the covers all looked the same).
    `template` is set by the run for each try (cover_candidates.Run.compose); a template says where the words go
    (`text`), whether they are the two cover lines or the column's rows (`rows`, the brief's `job["column_rows"]`), and
    how the person is framed (`framing`: face height, face centre x, face top).  Whether the template fits is the
    renderer's: a layout it refuses, or a punchline under the floor, sends the run on to the next template."""

    def __init__(self, job, rules):
        self.rows = [dict(r) for r in job.get("column_rows") or []]
        self.rules, self.template, self.taken = rules, None, {}

    def free(self):
        """The template places the person (she stands alone); otherwise only the words change."""
        t = self.template
        return t is not None and "solo" in t["applies"]["people"] and (
            t["text"] != "top" or t["framing"].get("face_x_frac") is not None or t["framing"].get("face_top_frac") is not None)

    def place(self, run, route, layer, face, need_top, host_side, centre_frac, guest):
        """place_solo in this template: the template's face height, centre and top for a person alone; as it always was
        for a person beside a guest or a fixed second sticker (the words still change with the template)."""
        fr = (self.template or {}).get("framing") or {}
        if guest or not self.free():
            return place_solo(layer, face, run.renderer, self.rules, need_top, host_side, centre_frac, fr.get("face_frac"))
        top = None if fr.get("face_top_frac") is None else fr["face_top_frac"] * FRAME_H
        sticker, box, plan = place_solo(layer, face, run.renderer, self.rules, None if top is not None else
                                        (need_top if self.template["text"] == "top" else None), "right",
                                        fr.get("face_x_frac"), fr.get("face_frac"), top)
        return sticker, box, dict(plan, template_id=self.template["template_id"])

    def text(self, route, lines, layout="top"):
        """The layout and lines of a candidate's spec in this template (`layout`: a caller's own default without one)."""
        t = self.template
        if t is None:
            return {"layout": layout, "lines": lines}
        out = {"layout": t["text"], "lines": [dict(r) for r in self.rows] if t["rows"] == "column" else lines}
        if t["text"] == "bottom":
            out["banner"] = True
        return out

    def record(self):
        return {"rows": [r["text"] for r in self.rows], "taken": self.taken}
