"""Face-driven geometry of the cover (step 6, 70-cover; shared by cover_candidates.py on the media host and the tests).

The frame is cropped around the host's measured face: the crop is as tight as it can be without cutting the head or the
face (or any box the cover must keep), up to the face height the approved covers have (lane_rules `cover.face_target_frac`).
When the target cannot be reached without cutting the head, the largest crop that holds it is used and the face height
that really results is recorded.  Pure functions on plain numbers and lists; no image library.
"""
import json
import math
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))     # the tools sit side by side, on the media host and here
from pick_frame import FACE_HINT, HOST

FRAME_W, FRAME_H = 1920, 1080
CHEST = 0.8

HEAD_LOCATE = ("这张图（{w}x{h} 像素）里有虚拟主播" + HOST + "；以画面实际造型和名牌辨认，" + FACE_HINT
               + "身份提示：{identity}；图里可能还有别人。请给出" + HOST + "本人主脸（眉毛到下巴、"
               "左脸颊到右脸颊）的像素框，以及她头部最高处（头发、耳朵、发饰都算）的 y 像素。只输出一个 JSON 对象："
               '{{"host_face": [x0, y0, x1, y1], "host_head_top": y}}，整数；看不出就写 null。')


def frame_face(face, crop):
    """A face box of the source frame in cover pixels for a full-canvas picture spec (crop = [x0, y0, width])."""
    x0, y0, width = crop
    scale = FRAME_W / width
    return [round((face[0] - x0) * scale), round((face[1] - y0) * scale),
            round((face[2] - x0) * scale), round((face[3] - y0) * scale)]


def protect(faces):
    """The box the cover text must keep clear of: every participant's main face, down to the chest (CHEST)."""
    faces = [f for f in faces if f]
    x0, y0 = min(f[0] for f in faces), min(f[1] for f in faces)
    x1, y1 = max(f[2] for f in faces), max(f[3] for f in faces)
    reach = round(CHEST * max(f[3] - f[1] for f in faces))
    return [max(0, x0), max(0, y0), min(FRAME_W, x1), min(FRAME_H, y1 + reach)]


def read_head(answer, w, h):
    """(face box, head top) from the vision answer of HEAD_LOCATE; either is None when absent or out of the picture."""
    match = re.search(r"\{.*\}", answer, re.DOTALL)
    try:
        doc = json.loads(match.group(0)) if match else {}
    except ValueError:
        return None, None
    face, top = doc.get("host_face"), doc.get("host_head_top")
    ok = (isinstance(face, list) and len(face) == 4 and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in face))
    face = [int(round(v)) for v in face] if ok else None
    if face and not (0 <= face[0] < face[2] <= w and 0 <= face[1] < face[3] <= h):
        face = None
    top = int(round(top)) if isinstance(top, (int, float)) and not isinstance(top, bool) else None
    if face and top is not None and not (0 <= top <= face[1]):
        top = None
    return face, top


def plan_crop(face, head_top, keep, rules, text_top=None):
    """The crop [x0, y0, width] of the live frame (height = width * 9 / 16) that gives the host's face the target height.
    `keep` are the other boxes that must stay inside the crop; `text_top` is the output y the face top should have (room
    for the text above it).  Returns {crop, target_px, face_px, limited_by, face}: limited_by is None when the target
    is reached or the face is already larger, `REGION` when the head (or a kept box) allows only a smaller enlargement, `PUNCH_TEXT_ZONE` when the punchline's
    room above the face (`text_top`) took precedence over the target (lane_rules cover.layout_order; never under the floor),
    `HEAD_TOP_UNMEASURED` when no head top was measured (the head cannot be protected, so the frame stays whole)."""
    fh = face[3] - face[1]
    target = round(rules["face_target_frac"] * FRAME_H)
    whole = {"crop": [0, 0, FRAME_W], "target_px": target, "face_px": fh, "limited_by": None, "face": list(face)}
    if target <= fh:
        return whole
    if head_top is None:
        return dict(whole, limited_by="HEAD_TOP_UNMEASURED")
    margin = round(rules["chin_margin_frac"] * fh)
    region = [min([face[0], *[b[0] for b in keep]]), min([head_top, *[b[1] for b in keep]]),
              max([face[2], *[b[2] for b in keep]]), min(FRAME_H, max([face[3] + margin, *[b[3] for b in keep]]))]
    rw, rh = region[2] - region[0], region[3] - region[1]
    k = min(target / fh, FRAME_W / rw, FRAME_H / rh)
    by_text = None
    if text_top is not None and region[3] > face[1]:      # the chin margin sits on the frame's bottom: the face top is 1080 - D * k (3 px for rounding)
        by_text = max((FRAME_H - text_top - 3) / (region[3] - face[1]), rules["face_floor_frac"] * FRAME_H / fh)  # never under the floor
        k = min(k, by_text)
    if k <= 1:
        return dict(whole, limited_by="REGION")
    w = math.ceil(FRAME_W / k)
    while round(w * FRAME_H / FRAME_W) < rh or w < rw:
        w += 1
    w = min(w, FRAME_W)
    h = round(w * FRAME_H / FRAME_W)
    x_lo, x_hi = max(0, region[2] - w), min(FRAME_W - w, region[0])
    x0 = min(max(round((face[0] + face[2]) / 2 - rules["face_center_x_frac"] * w), x_lo), x_hi)
    want = face[1] - text_top * w / FRAME_W if text_top is not None else region[1]
    y_lo, y_hi = max(0, region[3] - h), min(FRAME_H - h, region[1])
    y0 = min(max(round(want), y_lo), y_hi)
    crop = [x0, y0, w]
    out = frame_face(face, crop)
    return {"crop": crop, "target_px": target, "face_px": out[3] - out[1], "face": out,
            "limited_by": None if out[3] - out[1] >= target - 1 else "PUNCH_TEXT_ZONE" if by_text == k else "REGION"}


def apply_crop(spec, crop):
    """Set the crop and carry the spec's blur and keep boxes into cover pixels (clamped; boxes left outside are dropped)."""
    for field in ("blur", "keep"):
        moved = []
        for box in spec.get(field, []):
            m = frame_face(box, crop)
            m = [max(0, m[0]), max(0, m[1]), min(FRAME_W, m[2]), min(FRAME_H, m[3])]
            if m[0] < m[2] and m[1] < m[3]:
                moved.append(m)
        spec[field] = moved
    spec["crop"] = list(crop)
    return spec


def _checked_boxes(boxes, src_w, src_h):
    """Return clipped `[x0, y0, x1, y1]` boxes, or None if `boxes` is missing/invalid/empty-after-clipping.

    Unlike a permissive filter, an unparseable or out-of-frame box never gets dropped silently into `[]`.
    """
    if not isinstance(boxes, list):
        return None
    out = []
    for box in boxes:
        if not (isinstance(box, (list, tuple)) and len(box) == 4
                and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in box)):
            return None
        bx0, by0, bx1, by1 = (round(v) for v in box)
        if bx0 >= bx1 or by0 >= by1:
            return None
        x0 = max(0, min(src_w, bx0))
        y0 = max(0, min(src_h, by0))
        x1 = max(0, min(src_w, bx1))
        y1 = max(0, min(src_h, by1))
        if x0 >= x1 or y0 >= y1:
            return None
        out.append([x0, y0, x1, y1])
    return out


def clean_rects(face, exclude_boxes, src_w=FRAME_W, src_h=FRAME_H):
    """Maximal axis-aligned rectangles inside [0, 0, src_w, src_h] holding `face` and disjoint from `exclude_boxes`.

    Returns rectangles sorted largest-area first (then wider first), or [] when any exclusion box overlaps `face`.
    """
    fx0, fy0, fx1, fy1 = (int(round(v)) for v in face)
    if fx0 >= fx1 or fy0 >= fy1 or fx0 < 0 or fy0 < 0 or fx1 > src_w or fy1 > src_h:
        return []
    if not exclude_boxes:
        return [[0, 0, src_w, src_h]]
    excludes = _checked_boxes(list(exclude_boxes), src_w, src_h)
    if excludes is None:
        return []
    for bx0, by0, bx1, by1 in excludes:
        if bx0 < fx1 and bx1 > fx0 and by0 < fy1 and by1 > fy0:
            return []
    rects = [[0, 0, src_w, src_h]]
    for bx0, by0, bx1, by1 in excludes:
        next_rects = []
        for rx0, ry0, rx1, ry1 in rects:
            if not (bx0 < rx1 and bx1 > rx0 and by0 < ry1 and by1 > ry0):
                next_rects.append([rx0, ry0, rx1, ry1])
                continue
            if bx1 <= fx0:
                next_rects.append([max(rx0, bx1), ry0, rx1, ry1])
            if bx0 >= fx1:
                next_rects.append([rx0, ry0, min(rx1, bx0), ry1])
            if by1 <= fy0:
                next_rects.append([rx0, max(ry0, by1), rx1, ry1])
            if by0 >= fy1:
                next_rects.append([rx0, ry0, rx1, min(ry1, by0)])
        unique = []
        for cand in next_rects:
            if cand[0] < cand[2] and cand[1] < cand[3] and cand not in unique:
                unique.append(cand)
        rects = [cand for cand in unique if not any(
            other != cand and other[0] <= cand[0] and other[1] <= cand[1]
            and other[2] >= cand[2] and other[3] >= cand[3] for other in unique)]
    rects.sort(key=lambda r: (-(r[2] - r[0]) * (r[3] - r[1]), -(r[2] - r[0]), r))
    return rects


def source_guest_preflight(job):
    """Return `PACK_FRAME_GUESTS_UNCHECKED` if `job` lacks checked person/guest fields or is marked unknown, else None."""
    if not isinstance(job.get("source_guest_boxes_unknown"), bool) or job["source_guest_boxes_unknown"]:
        return "PACK_FRAME_GUESTS_UNCHECKED"
    if not isinstance(job.get("source_person_boxes"), list) or not isinstance(job.get("source_guest_boxes"), list):
        return "PACK_FRAME_GUESTS_UNCHECKED"
    return None


def other_person_boxes(job, host_face, src_w=FRAME_W, src_h=FRAME_H):
    """(boxes of other people in the source frame, error_code) for a frame checked against `host_face`.

    Refuses jobs lacking checked fields or with unknown/out-of-frame boxes (`PACK_FRAME_GUESTS_UNCHECKED`),
    and reclassifies any `source_person_boxes` entry not containing `host_face`'s centre as another person.
    """
    if err := source_guest_preflight(job):
        return None, err
    others = _checked_boxes(job["source_guest_boxes"], src_w, src_h)
    persons = _checked_boxes(job["source_person_boxes"], src_w, src_h)
    if others is None or persons is None:
        return None, "PACK_FRAME_GUESTS_UNCHECKED"
    fcx = (host_face[0] + host_face[2]) / 2
    fcy = (host_face[1] + host_face[3]) / 2
    for box in persons:
        if not (box[0] <= fcx <= box[2] and box[1] <= fcy <= box[3]) and box not in others:
            others.append(box)
    return others, None
