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


def _evaluate(top, ft, fb, rules, pad):
    """(layer height, face height, limited_by) for a face whose top sits at `top` and which spans the fractions ft..fb of
    the layer: head room above the face is at most `top` (the picture cannot leave through the top), the face and the
    chin margin must stay above the frame's bottom, and the face is never larger than the target."""
    span = fb - ft
    target = rules["face_target_frac"] * FRAME_H
    h, limit = target / span, None
    head = (top - pad - rules["layer_top_min_px"]) / ft if ft > 0 else math.inf
    if head < h:
        h, limit = head, "HEAD_ABOVE_FACE"
    room = (FRAME_H - top) / (1 + rules["chin_margin_frac"]) / span
    if room < h:
        h, limit = room, "FRAME_BOTTOM"
    return h, h * span, limit


def solve(ft, fb, rules, need_top, pad=0):
    """Where the face top goes and how tall the layer is.  `ft`, `fb`: the face's top and bottom as fractions of the
    layer's alpha height; `need_top`: the face top the punchline's floor size needs (None: not reachable, e.g. a line too
    wide for the safe zone).  Every face top is tried and the best by `layout_order` wins (each wish met beats any
    later one), then the larger face, then the higher face (more room for the text)."""
    target = round(rules["face_target_frac"] * FRAME_H)
    floor = round(rules["face_floor_frac"] * FRAME_H)
    want = need_top if need_top is not None else rules["face_top_default_px"]
    best = None
    for top in range(rules["face_top_min_px"], FRAME_H - floor, 2):
        h, fh, limit = _evaluate(top, ft, fb, rules, pad)
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


def place_solo(layer, face, renderer, rules, need_top, host_side, centre_frac=None):
    """One person's picture placed by the measured face.  `layer`: {src, cutout, crop?} (crop: the region of the source
    kept); `face`: the face box in the source picture's pixels.  Returns (sticker dict, face box in cover pixels, plan);
    raises ValueError("HOST_FACE_OUTSIDE_FIGURE") when the face is not inside the person's alpha box (the picture shows
    no face, e.g. only legs).  `centre_frac`: where the face centre goes, when the cover has another main layer."""
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
    bleed = cut and _bleed_fits(ft, fb, rules, need_top)
    if bleed:                                         # no outline: the cut edge is then the canvas edge exactly
        outline = 0
    pad = outline + 4 if outline else 0               # the renderer pads the picture by the outline before it pastes
    plan = solve(ft, fb, rules, need_top, pad)
    if bleed:
        h_target = round(rules["face_target_frac"] * FRAME_H / (fb - ft))
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
    y = round(plan["face_top"] - pad - ft * h)
    sticker = {"src": layer["src"], "cutout": layer["cutout"], "crop": crop, "x": x, "y": max(0, y), "h": h,
               "measure_outline": True}
    if "outline" in layer or bleed:
        sticker["outline"] = 0 if bleed else layer["outline"]
    box = [round(x + pad + fx0), round(max(0, y) + pad + ft * h), round(x + pad + fx1), round(max(0, y) + pad + fb * h)]
    plan.update(width=w, layer_xy=[x, max(0, y)], face=box, face_px=box[3] - box[1])
    if cut:                                           # recorded either way: a flat cut inside the canvas is the source's, not the layout's
        plan["head_cut"] = "AT_CANVAS_TOP" if bleed else "INSIDE_CANVAS"
        plan["cut_edge_y"] = max(0, y) + pad
    return sticker, box, plan


SAFE_X0, SAFE_X1, FACE_GAP = 260, 1660, 30      # the renderer's feed-safe zone and the gap it keeps beside the face


class Column:
    """The cover's words stacked in a column beside the person (owner 2026-11-13: not every cover one line top left and
    one line bottom right).  `job["text_layout"]` left | right asks for it; `job["column_rows"]` are the rows (the same
    words broken at word boundaries, the punchline's rows marked `punch`).  A candidate gets the column only when its
    person, placed on the other side, leaves the punchline's rows room for the floor size; otherwise it keeps the two
    lines on top, and `record` says why.  A cover with another person or a fixed centre is never a column."""

    def __init__(self, job, rules):
        self.side = job.get("text_layout") if job.get("text_layout") in ("left", "right") else None
        self.rows = [dict(r) for r in job.get("column_rows") or []]
        self.rules, self.placed, self.refused = rules, {}, {}

    def place(self, run, route, layer, face, need_top, host_side, centre_frac, guest):
        """place_solo for this candidate: beside the column when one is asked for and fits, else as it always was."""
        if self.side and len(self.rows) >= 2 and not guest and centre_frac is None:
            frac = self.rules["column_face_center_x_frac"]
            sticker, box, plan = place_solo(layer, face, run.renderer, self.rules, None, "right",
                                            frac if self.side == "left" else 1 - frac)
            zone = box[0] - FACE_GAP - SAFE_X0 if self.side == "left" else SAFE_X1 - box[2] - FACE_GAP
            ink = run.text_ink({"lines": self.rows})[1]
            start = self.rules["punch_start_px"]
            size = start - 6 * ((start - self.rules["punch_floor_px"]) // 6)      # the renderer's own size grid
            wide = max(ink(i, size)[0] for i, row in enumerate(self.rows) if row.get("punch"))
            if wide <= zone:
                self.placed[route] = {"zone_px": zone, "punch_row_px": wide, "at_size": size}
                return sticker, box, dict(plan, text_layout=self.side)
            self.refused[route] = {"zone_px": zone, "punch_row_px": wide, "at_size": size}
        return place_solo(layer, face, run.renderer, self.rules, need_top, host_side, centre_frac)

    def text(self, route, lines, layout="top"):
        """The layout and lines of a candidate's spec."""
        return {"layout": self.side, "lines": self.rows} if route in self.placed else {"layout": layout, "lines": lines}

    def record(self):
        return {"asked": self.side, "rows": [r["text"] for r in self.rows], "placed": self.placed, "refused": self.refused}
