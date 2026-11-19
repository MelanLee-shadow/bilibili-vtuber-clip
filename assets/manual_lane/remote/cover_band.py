"""A screenshot whose punchline has no room beside the faces (step 6, 70-cover: 字放不下先换版式，不直接转 AI).

The whole picture is shrunk, centred and standing on the bottom edge; the band above it and the sides are the same picture
blurred (the source's own tones, no card or skin), the shrunk picture fading into it at its edges (no pasted-on rectangle).  Its faces and kept boxes move with it, so the text keeps clear of them
exactly as before.  Used by cover_candidates.py on the media host; the geometry is pure and tested on its own."""
from PIL import Image, ImageDraw, ImageFilter

FRAME_W, FRAME_H = 1920, 1080
MIN_SCALE = 0.6     # never smaller: below this the faces fall under the face floor (lane_rules cover.face_floor_frac) anyway
FEATHER = 90        # px over which the sharp picture fades into the blur: a hard rectangle edge reads as a pasted layer


def scale_for(face_top, need_top, min_scale=MIN_SCALE):
    """The scale that brings the faces' top down to `need_top`; None when no shrink is needed, the punchline cannot reach
    its floor at any size (need_top None), or more than `min_scale` would be needed."""
    if need_top is None or face_top >= need_top:
        return None
    scale = (FRAME_H - need_top) / (FRAME_H - face_top)
    return round(scale, 4) if min_scale <= scale < 1 else None


def moved(box, scale):
    """A full-canvas box after the shrink: scaled, centred, standing on the bottom edge."""
    ox, oy = FRAME_W * (1 - scale) / 2, FRAME_H * (1 - scale)
    return [round(ox + box[0] * scale), round(oy + box[1] * scale), round(ox + box[2] * scale), round(oy + box[3] * scale)]


def moved_any(boxes, scale):
    """One box, a list of boxes, or None, moved."""
    if not boxes:
        return boxes
    return [moved(b, scale) for b in boxes] if isinstance(boxes[0], list) else moved(boxes, scale)


def feather_mask(size, feather=FEATHER):
    """Opaque inside, fading to clear over `feather` px at the top, left and right edges (the bottom stands on the frame)."""
    w, h = size
    mask = Image.new("L", (w, h), 0)
    ImageDraw.Draw(mask).rectangle((feather // 2, feather // 2, w - 1 - feather // 2, h + feather), fill=255)
    return mask.filter(ImageFilter.GaussianBlur(feather / 4))


def banded_spec(spec, need_top, out_path):
    """The spec of the banded picture (written to `out_path`), or None when the band cannot give the text its room."""
    if spec.get("band") or "frame" not in spec:
        return None
    scale = scale_for(spec["face"][1], need_top)
    if scale is None:
        return None
    x0, y0, w = spec["crop"]
    with Image.open(spec["frame"]) as im:
        picture = im.convert("RGB").crop((x0, y0, x0 + w, y0 + round(w * FRAME_H / FRAME_W))).resize((FRAME_W, FRAME_H), Image.LANCZOS)
    canvas = picture.filter(ImageFilter.GaussianBlur(40))
    small = picture.resize((round(FRAME_W * scale), round(FRAME_H * scale)), Image.LANCZOS)
    canvas.paste(small, ((FRAME_W - small.width) // 2, FRAME_H - small.height), feather_mask(small.size))
    canvas.save(out_path)
    return dict(spec, frame=out_path, crop=[0, 0, FRAME_W], face=moved(spec["face"], scale),
                blur=[moved(b, scale) for b in spec.get("blur") or []], keep=[moved(b, scale) for b in spec.get("keep") or []],
                band={"scale": scale, "face_top_needed": need_top, "face_top_before": spec["face"][1],
                      "crop_before": list(spec["crop"])})
