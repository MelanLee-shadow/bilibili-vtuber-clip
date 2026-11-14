#!/usr/bin/env python3
"""cover_candidates.py JOB.json

Step 6 cover candidates on OCI3 (70-cover, lidousha-title-cover skill section 6): the same cover lines on 2-4
feasible pictures, each rendered by the skill's render_cover.py and read once by CPA vision; the caller scores them
with the repo's title_cover_candidates rules and keeps the best.

- screenshot: the picked live frame (danmaku blurred), as before.
- emote: an official emote of the host (the repo's keyword pick, chosen by the caller) on a pastel background, the
  guest as a crop of the same frame (skill: stream_frame, cutout none), the audience's sticker when the story is about the audience.
- cutout: the frame with its background removed by CPA images/edits (skill redraw_background "cutout" prompt); the
  two figures found in it (alpha columns) as two layers on a pastel background, the host's head and shoulders large;
  two equal participants of a collaboration (job "peers", U158) stand the same size instead (peer_layers).
- ai_sticker: a chibi sticker of this moment from a crop of the host (skill "sticker" prompt), with the guest's figure
  from the cut-out (found by its alpha columns: a generated image does not keep the frame's positions); or ai_scene (skill "scene" prompt) when the caller's brief says the story needs a scene.

Image generation goes through the repo's CPA image-edit adapter (cover_generation._call_cpa_image_edit: gpt-image-2.5
first, gpt-image-2 only when 2.5 is explicitly unavailable).  A generated image whose request (prompt and reference
bytes) is unchanged is reused, not asked again.  Every paid request is listed in OUT/result.json "calls".
Runs under the root wrapper that loads cpa.env (CPA_BASE_URL / CPA_API_KEY); the key is never printed or written.

Text style: the renderer rotates it from the repo's one style table (job "styles"; skill section 6: colours, size ratio, tilt and stroke are chosen by the content hash, skipping the styles of the last three covers, job "content_sha256" and "recent_style_ids"); nothing here pins a style.  The background colours of the pastel routes come from lane_rules `cover.backgrounds` (cover_background.py).
"""
import base64
import contextlib
import hashlib
import importlib.util
import io
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

from PIL import Image, ImageChops, ImageDraw, ImageFilter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))   # the sibling modules uploaded with this tool
import cover_background as background
import cover_clock as clock
import cover_fast_text as fast_text
import cover_face_crop as crop_mod
import cover_layout as layout
import cover_text_fit as text_fit
from cover_face_crop import frame_face, protect

CALLS = []
GENERATIONS = {}   # candidate kind -> {model, reported_model, reused}: what produced the layer this run used
# Composed covers: both people stand on the bottom edge (the host larger, on the right), the lines go above them
# (layout "top"); the renderer's own checks keep the text off the host's face and the guest's head.
GUEST_H = 560        # a guest who is not an equal: her head and shoulders, standing on the bottom edge on the left
BUST = 0.65          # a bust layer is the top 65 % of the body box (head and shoulders)


def sha(path):
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def keep_canvas(path):
    """Stickers and cut-outs keep their own canvas (and alpha); only the scene goes onto the 1920x1080 cover canvas."""
    with Image.open(path) as im:
        return im.size


@clock.timed("generate")
def generate(job, kind, prompt, reference, out_path, canvas=None):
    """One CPA image edit through the repo adapter, or the earlier image when its request is unchanged."""
    key_path = out_path + ".key.json"
    key = {"prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(), "reference_sha256": sha(reference)}
    if os.path.exists(out_path) and os.path.exists(key_path):
        with open(key_path, encoding="utf-8") as fh:
            if json.load(fh) == key:
                GENERATIONS[kind] = {"model": requested_model(out_path + ".request.json"),
                                     "reported_model": reported_model(out_path + ".response.json"), "reused": True}
                return {"status": "REUSED", "output_path": out_path}
    sys.path.insert(0, job["repo"])
    from pathlib import Path

    from src.autoslice.cover_generation import _call_cpa_image_edit

    base = os.environ.get("CPA_BASE_URL", "").strip().rstrip("/")
    api_key = os.environ.get("CPA_API_KEY", "")
    call = {"kind": "image_edit", "candidate": kind, "rc": 1}
    CALLS.append(call)
    started = time.monotonic()
    result = _call_cpa_image_edit(base_url=base, api_key=api_key, reference_path=Path(reference),
                                  output_path=Path(out_path), prompt=prompt,
                                  request_path=Path(out_path + ".request.json"),
                                  response_path=Path(out_path + ".response.json"),
                                  normalize_canvas=canvas)
    call["elapsed_s"] = round(time.monotonic() - started, 1)
    call["status"] = result.get("status")
    call["selected_model"] = result.get("selected_model")
    call["attempted_models"] = result.get("attempted_models")
    call["reason_code"] = result.get("reason_code")
    call["reported_model"] = reported_model(out_path + ".response.json")
    if result.get("status") == "AI_BACKGROUND_READY":
        call["rc"] = 0
        with open(key_path, "w", encoding="utf-8") as fh:
            json.dump(key, fh)
    if call["rc"] == 0:
        GENERATIONS[kind] = {"model": call["selected_model"], "reported_model": call["reported_model"], "reused": False}
    return result


def requested_model(request_path):
    """The model name of the last request recorded for an image (the repo adapter's request evidence)."""
    try:
        with open(request_path, encoding="utf-8") as fh:
            attempts = json.load(fh).get("attempts") or []
    except (OSError, ValueError):
        return None
    return (attempts[-1] or {}).get("model") if attempts else None


def reported_model(response_path):
    """The model the provider says produced the image (never the requested name); UNKNOWN when it does not say."""
    try:
        with open(response_path, encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, ValueError):
        return "UNKNOWN"
    for attempt in reversed(doc.get("attempts") or doc.get("responses") or []):
        obs = (attempt or {}).get("service_observation") or {}
        model = obs.get("reported_model") or obs.get("response_model")
        if model:
            return model
    return "UNKNOWN"


def b64_jpeg(path, width=1280):
    with Image.open(path) as im:
        im = im.convert("RGB")
        if im.width > width:
            im = im.resize((width, round(im.height * width / im.width)), Image.LANCZOS)
        buf = io.BytesIO()
        im.save(buf, format="JPEG", quality=88)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def vision(job, name, question, images, out_dir):
    """One CPA vision reading (images: [(path, width)]), kept as OUT/<name>.response.json; an unchanged question on
    unchanged images is read from there, not asked again.  Returns (answer text, reused)."""
    raw_path = os.path.join(out_dir, f"{name}.response.json")
    key = {"question": hashlib.sha256(question.encode()).hexdigest(), "images": [sha(p) for p, _ in images],
           "model": job["judge_model"]}
    key_path = raw_path + ".key.json"
    if os.path.exists(raw_path) and os.path.exists(key_path):
        with open(key_path, encoding="utf-8") as fh:
            if json.load(fh) == key:
                with open(raw_path, encoding="utf-8") as fh2:
                    return output_text(json.load(fh2)), True
    base = os.environ.get("CPA_BASE_URL", "").strip().rstrip("/")
    content = [{"type": "input_text", "text": question}] + [
        {"type": "input_image", "image_url": "data:image/jpeg;base64," + b64_jpeg(p, w)} for p, w in images]
    body = {"model": job["judge_model"], "reasoning": {"effort": "low"}, "max_output_tokens": 1600,
            "input": [{"role": "user", "content": content}]}
    req = urllib.request.Request(f"{base}/responses", data=json.dumps(body).encode(), method="POST", headers={
        "Authorization": "Bearer " + os.environ.get("CPA_API_KEY", ""), "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"})
    call = {"kind": "vision", "candidate": name, "model": job["judge_model"], "effort": "low", "rc": 1}
    CALLS.append(call)
    started = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=240) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    finally:
        call["elapsed_s"] = round(time.monotonic() - started, 1)
    call["usage"] = payload.get("usage") or {}
    # TASK-26: only an answer the provider completed is kept and read (cpa_text.completion_of's rule); an incomplete
    # one is a failed call, raised before it is written, so it is never reused as the answer to this question
    statuses = [i.get("status") for i in payload.get("output") or [] if i.get("type") == "message"]
    if (payload.get("status") != "completed" or payload.get("incomplete_details") or payload.get("error")
            or any(s != "completed" for s in statuses) or not output_text(payload)):
        call["incomplete"] = {"status": payload.get("status"), "incomplete_details": payload.get("incomplete_details")}
        raise ValueError(f"CPA vision answer not complete: status={payload.get('status')}")
    call["rc"] = 0
    with open(raw_path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False)
    with open(key_path, "w", encoding="utf-8") as fh:
        json.dump(key, fh)
    return output_text(payload), False


@clock.timed("judge")
def judge(job, cid, question, image, frame, out_dir):
    """The reading of one rendered candidate (full 1920 width, the first image) with the source frame for identity."""
    return vision(job, f"{cid}.judge", question, [(image, 1920), (frame, 1280)], out_dir)


LOCATE = ("这张图（{w}x{h} 像素）里有虚拟主播" + crop_mod.HOST + "（Q 版、表情包、抠图也算她）；以画面实际造型和名牌辨认，"
          + crop_mod.FACE_HINT + "身份提示：{identity}；"
          "图里可能还有别人。请给出" + crop_mod.HOST + "本人主脸（眉毛到下巴、左脸颊到右脸颊）在这张图里的像素框。只输出一个 JSON 对象："
          '{{"host_face": [x0, y0, x1, y1]}}，整数；看不出就写 null。')


FACE_IN = ("这张图（{w}x{h} 像素）是从一张画面里裁出的一位虚拟主播（边上可能带着别人的一角）。请给出图中这位主要人物的主脸"
           "（眉毛到下巴、左脸颊到右脸颊）的像素框。只输出一个 JSON 对象："
           '{{"face": [x0, y0, x1, y1]}}，整数；看不出就写 null。')


def _located(job, src, out_dir, name, question, key):
    """One CPA vision reading of one picture for one face box (reused while the picture's bytes are unchanged)."""
    with Image.open(src) as im:
        w, h = im.size
    try:
        answer, _ = vision(job, name, question.format(w=w, h=h), [(src, w)], out_dir)
    except (urllib.error.URLError, OSError, ValueError):
        return None
    match = re.search(r"\{.*\}", answer, re.DOTALL)
    try:
        box = json.loads(match.group(0)).get(key) if match else None
    except ValueError:
        box = None
    if isinstance(box, list) and len(box) == 4 and all(isinstance(v, (int, float)) for v in box):
        box = [int(round(v)) for v in box]
        if 0 <= box[0] < box[2] <= w and 0 <= box[1] < box[3] <= h:
            return box
    return None


@clock.timed("locate")
def host_face(job, src, out_dir, name="locate-cutout"):
    """Where the host's face is in one source picture (the live frame, a generated cut-out or sticker, an emote): one
    CPA vision reading of that picture, reused while its bytes are unchanged, so the same picture always gives the same
    box.  Returns [x0, y0, x1, y1] in the picture's pixels, or None."""
    return _located(job, src, out_dir, name, LOCATE.replace("{identity}", job.get("identity", "以图中实际造型为准")), "host_face")


@clock.timed("locate")
def guest_face(job, src, out_dir, name, region):
    """The other participant's face, read inside her own figure (region: the guest box of the frame reading, or her
    figure in the cut-out, both already told apart from the host's): the crop holds one person, so the reading does not
    depend on telling the two apart by name (asked by name on the whole picture, CPA vision returned the host's face as
    the guest's, TASK-20 run).  The crop's bytes key the reading.  Returns the box in the picture's pixels, or None."""
    with Image.open(src) as im:
        x0, y0 = max(0, int(region[0])), max(0, int(region[1]))
        x1, y1 = min(im.width, int(region[2])), min(im.height, int(region[3]))
        if x1 - x0 < 8 or y1 - y0 < 8:
            return None
        crop = os.path.join(out_dir, f"{name}.crop.png")
        im.crop((x0, y0, x1, y1)).save(crop)
    box = _located(job, crop, out_dir, name, FACE_IN, "face")
    return [box[0] + x0, box[1] + y0, box[2] + x0, box[3] + y0] if box else None


@clock.timed("measure")
def measured_face(job, route, spec, host_src, out_dir, renderer):
    """The host's main face on the cover, measured from the picture it comes from (TASK-11: the 25 % floor used to read
    the face box off the rendered candidate, whose bytes change with every blur box or text position, so one frame
    passed in one run and failed in the next).  The face is located once in the host's own source picture (reading
    reused by its bytes) and carried through the spec's own geometry, which is deterministic.  Returns (box, note)."""
    if host_src is None:
        return None, "无主播图层"
    kind, src, crop = host_src["kind"], host_src["src"], host_src.get("crop")
    face = host_src.get("face") or host_face(job, src, out_dir, name=f"locate-{route}")
    if face is None:
        return None, f"看图没有在源图里找到{crop_mod.HOST}的脸"
    if kind == "picture":                      # screenshot / scene: the picture fills the canvas
        return frame_face(face, spec["crop"]), "源图定位 × 整幅换算"
    layer = next(s for s in reversed(spec["stickers"]) if s["src"] == src and s.get("crop") == crop)
    with Image.open(src) as im:
        full = [0, 0, im.width, im.height]
    return place_face(layer, crop or full, face, renderer), "源图定位 × 图层换算"


CHIN_MARGINS = (0.9, 0.7, 0.5, 0.35, 0.15)   # how much below the chin a peer's layer keeps (in face heights), widest first


def output_text(payload):
    text = payload.get("output_text") or ""
    if not text:
        for item in payload.get("output") or []:
            if item.get("type") == "message":
                text += "".join(c.get("text", "") for c in item.get("content") or [])
    return text.strip()


def parse_verdict(text):
    """The JSON object of the answer; box numbers are written as integers (a representation, not a judgement)."""
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        return None
    try:
        value = json.loads(match.group(0))
    except ValueError:
        return None
    if not isinstance(value, dict):
        return None

    def ints(box):
        return [int(round(v)) for v in box] if isinstance(box, list) and all(
            isinstance(v, (int, float)) and not isinstance(v, bool) for v in box) else box
    if "face" in value:
        value["face"] = ints(value["face"])
    if isinstance(value.get("keep"), list):
        value["keep"] = [ints(b) for b in value["keep"]]
    return value


def half(box):
    """The upper part of a body box (BUST): head and shoulders, so the face stays large on the cover."""
    x0, y0, x1, y1 = box
    return [x0, y0, x1, y0 + max(1, round((y1 - y0) * BUST))]


def figures(src, renderer, *, raw=False):
    """The people in a cut-out image, left to right: [x0, y0, x1, y1] of each figure found by the alpha columns (a
    generated image does not keep the frame's positions, so the frame's boxes cannot be used on it)."""
    alpha = renderer.cutout(Image.open(src), "auto").getchannel("A")
    w, h = alpha.size
    px = alpha.load()
    cover = [sum(1 for y in range(0, h, 4) if px[x, y] > 128) for x in range(w)]
    filled = [c > max(2, (h // 4) * 0.02) for c in cover]
    runs, start = [], None
    for x, on in enumerate(filled + [False]):
        if on and start is None:
            start = x
        elif not on and start is not None:
            runs.append([start, x])
            start = None
    merged = []
    for run in runs:                                    # legacy path joins gaps narrower than 2 % of the width
        if not raw and merged and run[0] - merged[-1][1] < w * 0.02:
            merged[-1][1] = run[1]
        else:
            merged.append(run)
    merged = [r for r in merged if r[1] - r[0] > w * 0.08]
    if not raw and len(merged) == 1:                     # legacy single-person path
        x0, x1 = merged[0]
        lo, hi = x0 + (x1 - x0) * 3 // 10, x0 + (x1 - x0) * 7 // 10
        valley = min(range(lo, hi), key=lambda x: cover[x]) if hi > lo else None
        if valley is not None and cover[valley] < 0.25 * max(cover[x0:x1]):
            merged = [[x0, valley], [valley, x1]]
    while not raw and len(merged) > 2:                  # legacy single-person path
        gaps = [merged[i + 1][0] - merged[i][1] for i in range(len(merged) - 1)]
        i = gaps.index(min(gaps))
        merged[i:i + 2] = [[merged[i][0], merged[i + 1][1]]]
    out = []
    for x0, x1 in merged:
        bbox = alpha.crop((x0, 0, x1, h)).getbbox()
        if bbox:
            out.append([x0 + bbox[0], bbox[1], x0 + bbox[2], bbox[3]])
    return out


def layer_size(src, cutout_mode, crop, h, renderer):
    """Width of a sticker layer as render_cover will paste it (cut-out, crop, alpha box, scaled to height h)."""
    img = renderer.cutout(Image.open(src), cutout_mode)
    if crop:
        img = img.crop(tuple(crop))
    bbox = img.getchannel("A").getbbox()
    if bbox:
        img = img.crop(bbox)
    return max(1, round(img.width * h / img.height))


def guest_left(guest, renderer):
    """A guest who is not an equal: her head and shoulders on the left, standing on the bottom edge.  Returns (sticker,
    keep box of her head)."""
    gw = layer_size(guest["src"], guest["cutout"], guest.get("crop"), guest["h"], renderer)
    top = 1080 - guest["h"]
    return dict(guest, x=40, y=top), [40, top, min(1920, 40 + gw), top + round(guest["h"] * 0.65)]


def place_face(layer, crop, face, renderer):
    """A face box given in the source image, in cover pixels for a layer pasted from that source (crop, alpha box,
    scaled to the layer's height)."""
    img = renderer.cutout(Image.open(layer["src"]), layer["cutout"]).crop(tuple(crop))
    bbox = img.getchannel("A").getbbox() or (0, 0, img.width, img.height)
    scale = layer["h"] / (bbox[3] - bbox[1])
    ox, oy = crop[0] + bbox[0], crop[1] + bbox[1]
    outline = int(layer.get("outline", 12))
    pad = outline + 4 if outline and layer.get("measure_outline") else 0
    return [layer["x"] + pad + round((face[0] - ox) * scale), layer["y"] + pad + round((face[1] - oy) * scale),
            layer["x"] + pad + round((face[2] - ox) * scale), layer["y"] + pad + round((face[3] - oy) * scale)]


PEER_FACE_TOP_MIN = 320   # peers stand with their faces level and below this line: the band above holds the two lines


def face_safe_order(layers, faces, order, renderer):
    """Order the actual alpha layers so no later figure or its outline covers another participant's face."""
    masks = {}
    for role, layer in layers.items():
        image = renderer.cutout(Image.open(layer["src"]), layer["cutout"]).crop(tuple(layer["crop"]))
        bbox = image.getchannel("A").getbbox()
        if not bbox:
            raise ValueError("PEER_LAYER_EMPTY")
        image = image.crop(bbox)
        width = max(1, round(image.width * layer["h"] / image.height))
        alpha = image.getchannel("A").resize((width, layer["h"]), Image.LANCZOS)
        outline = int(layer.get("outline", 12))
        if outline:
            pad = outline + 4
            mask = Image.new("L", (width + 2 * pad, layer["h"] + 2 * pad), 0)
            mask.paste(alpha, (pad, pad))
            alpha = mask.filter(ImageFilter.MaxFilter(2 * (outline // 2) + 1))
        masks[role] = alpha
    dependencies = {role: set() for role in order}
    for role in order:
        face = faces[role]
        for other in order:
            if other == role:
                continue
            layer, mask = layers[other], masks[other]
            left, top = max(0, face[0] - layer["x"]), max(0, face[1] - layer["y"])
            right = min(mask.width, face[2] - layer["x"])
            bottom = min(mask.height, face[3] - layer["y"])
            if left < right and top < bottom and mask.crop((left, top, right, bottom)).getextrema()[1] > 128:
                dependencies[role].add(other)  # the face's own layer must be pasted later
    preferred = sorted(order, key=lambda role: -abs(order.index(role) - (len(order) - 1) / 2))
    result = []
    while len(result) < len(order):
        ready = [role for role in preferred if role not in result and dependencies[role].issubset(result)]
        if not ready:
            raise ValueError("PEER_FACES_OCCLUDED")
        result.append(ready[0])
    return result


@clock.timed("layout")
def peer_layers(host, guest, renderer, host_side, face_px, floor_px, top_min=PEER_FACE_TOP_MIN):
    """`top_min`: the face top the punchline's floor size needs (never above the old PEER_FACE_TOP_MIN)."""
    top_min = max(PEER_FACE_TOP_MIN, top_min or 0)
    try:
        return _peer_layers(host, guest, renderer, host_side, (face_px, floor_px + 10), top_min)
    except ValueError as exc:
        if not isinstance(guest, list) or len(guest) < 2 or str(exc) not in (
                "PEER_LAYERS_DO_NOT_FIT", "PEER_FACES_OVERLAP", "PEER_FACES_OCCLUDED"):
            raise
        return _peer_layers(host, guest, renderer, host_side, (floor_px,), top_min)


def _margin_layers(people, face_px, margin, top_min, many, renderer):
    """Every person cropped `margin` face heights below the chin and scaled to a face of `face_px`: (layers, their widths,
    whether each fits the frame height with its face top at or below `top_min`)."""
    layers, fits = {}, True
    for role, p in people.items():
        fig, face = p["fig"], p["face"]
        fh = max(1, face[3] - face[1])
        bottom = min(fig[3], face[3] + round(fh * margin))
        crop = [fig[0], fig[1], fig[2], bottom]
        h = round((bottom - fig[1]) * face_px / fh)
        face_top = 1080 - h + round((face[1] - fig[1]) * face_px / fh)
        fits = fits and h <= 1080 and face_top >= top_min
        layers[role] = {"src": p["src"], "cutout": p["cutout"], "crop": crop, "h": h}
        if many:
            layers[role]["measure_outline"] = True
    widths = {r: layer_size(lay["src"], lay["cutout"], lay["crop"], lay["h"], renderer) for r, lay in layers.items()}
    if many:
        widths = {role: width + 32 for role, width in widths.items()}  # renderer's 12px outline plus padding
    return layers, widths, fits


def _peer_layers(host, guest, renderer, host_side, face_sizes, top_min=PEER_FACE_TOP_MIN):
    """Equal participants (U158), cropped below the chin and scaled to equal faces at or above the 25 % floor;
    faces level, both standing on the bottom edge; the host on host_side, in front where they meet.  host /
    guest: {src, cutout, fig: alpha box of the person in src, face: main face in src}.  Returns (stickers, host face,
    guest face, protected box) in cover pixels."""
    many = isinstance(guest, list)
    guests = guest if many else [guest]
    guest_keys = ["guest" if i == 0 else f"guest{i}" for i in range(len(guests))]
    people = {"host": host, **dict(zip(guest_keys, guests))}
    for face_px in face_sizes:
        for margin in CHIN_MARGINS:            # widest margin below the chin first
            layers, widths, fits = _margin_layers(people, face_px, margin, top_min, many, renderer)
            if fits and sum(widths.values()) <= 1920:
                break
        if fits and sum(widths.values()) <= 1920:
            break
    order = [*guest_keys, "host"] if host_side == "right" else ["host", *guest_keys]   # left to right
    total = sum(widths.values())
    gap = max(0, (1920 - total) // (len(order) + 1))
    xs = {}
    position = gap
    for role in order:
        xs[role] = position
        position += widths[role] + gap
    xs[order[-1]] = 1920 - gap - widths[order[-1]] if total <= 1920 else 1920 - widths[order[-1]]
    if total > 1920:
        if len(order) > 2:
            if not fits or max(widths.values()) > 1920:
                raise ValueError("PEER_LAYERS_DO_NOT_FIT")
            # Shoulders may overlap, but every complete layer stays on canvas and face centres are spread evenly.
            for index, role in enumerate(order):
                local = dict(layers[role], x=0, y=1080 - layers[role]["h"])
                box = place_face(local, local["crop"], people[role]["face"], renderer)
                centre = (index + 0.5) * 1920 / len(order)
                xs[role] = max(0, min(1920 - widths[role], round(centre - (box[0] + box[2]) / 2)))
        else:
            xs[order[0]] = 0
    for role, lay in layers.items():
        lay.update(x=xs[role], y=1080 - lay["h"] - (32 if many else 0))
    faces = {r: place_face(layers[r], layers[r]["crop"], people[r]["face"], renderer) for r in layers}
    if len(order) > 2 and any(faces[a][2] > faces[b][0] for a, b in zip(order, order[1:])):
        raise ValueError("PEER_FACES_OVERLAP")
    if many:
        stickers = [layers[role] for role in face_safe_order(layers, faces, order, renderer)]
    else:
        stickers = [layers["guest"], layers["host"]]                   # preserve the two-person composition
    guest_faces = [faces[key] for key in guest_keys]
    return stickers, faces["host"], guest_faces if many else guest_faces[0], protect(list(faces.values()))


def cutout_ready(job, out, redraw, frame):
    """The cut-out made earlier in this clip's step 6 (same prompt, same frame), or None: never generated just to
    serve another candidate."""
    cut_png = os.path.join(out, "gen-cutout.png")
    key_path = cut_png + ".key.json"
    if not (os.path.exists(cut_png) and os.path.exists(key_path)):
        return None
    key = {"prompt_sha256": hashlib.sha256(redraw.build_prompt(job["brief"], "cutout").encode()).hexdigest(),
           "reference_sha256": sha(frame)}
    with open(key_path, encoding="utf-8") as fh:
        if json.load(fh) != key:
            return None
    GENERATIONS["cutout"] = {"model": requested_model(cut_png + ".request.json"),
                             "reported_model": reported_model(cut_png + ".response.json"), "reused": True}
    return cut_png


def alpha_box(src, cutout_mode, renderer):
    """The person's box in a picture that is one person (an emote, a generated sticker): its alpha bounding box."""
    with Image.open(src) as im:
        bbox = renderer.cutout(im, cutout_mode).getchannel("A").getbbox()
        return list(bbox) if bbox else [0, 0, im.width, im.height]


def inside(face, fig):
    """A located face that sits on this figure (its centre inside the figure's box)."""
    return bool(face) and fig[0] <= (face[0] + face[2]) / 2 <= fig[2] and fig[1] <= (face[1] + face[3]) / 2 <= fig[3]


def contains_face(face, fig):
    return bool(face) and fig[0] <= face[0] < face[2] <= fig[2] and fig[1] <= face[1] < face[3] <= fig[3]


PEOPLE_LOCATE = ("这是一张已去背景的人物图（{w}x{h} 像素），请分别找出画面中每位主要人物，忽略头饰上的小玩偶。"
                 "不要按名字辨认。每人给出身体范围 body、本人主脸 face（眉毛到下巴）的像素框 [x0,y0,x1,y1]，"
                 "以及该人可见轮廓 outline 的有序像素点 [[x,y],...]；人物相接或遮挡时沿真实边界描绘，"
                 "不要把邻人的头发或身体包括进去。只输出 JSON："
                 '{{"people":[{{"body":[x0,y0,x1,y1],"face":[x0,y0,x1,y1],"outline":[[x,y],...]}}]}}。')


def located_people(job, src, out_dir, expected):
    """Read all figures once when alpha columns disagree with the frame; vision() caches by source bytes and prompt."""
    with Image.open(src) as im:
        w, h = im.size
    try:
        answer, _ = vision(job, "locate-people-cutout", PEOPLE_LOCATE.format(w=w, h=h), [(src, w)], out_dir)
        match = re.search(r"\{.*\}", answer, re.DOTALL)
        # Some responses omit the outer opening bracket for an outline, but retain its closing bracket.
        raw = re.sub(r'("outline"\s*:\s*)(\[\s*-?\d+(?:\.\d+)?\s*,\s*-?\d+(?:\.\d+)?\s*\]\s*,)',
                     r'\1[\2', match.group(0)) if match else ""
        people = json.loads(raw)["people"] if match else None
    except (urllib.error.URLError, OSError, ValueError, KeyError, TypeError):
        return None
    if not isinstance(people, list) or len(people) != expected:
        return None

    def box(value):
        if not (isinstance(value, list) and len(value) == 4 and
                all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in value)):
            return None
        b = [round(x) for x in value]
        return b if 0 <= b[0] < b[2] <= w and 0 <= b[1] < b[3] <= h else None

    result = []
    for person in people:
        if not isinstance(person, dict):
            return None
        body, face, outline = box(person.get("body")), box(person.get("face")), person.get("outline")
        if not body or not face or not contains_face(face, body) or not isinstance(outline, list) or len(outline) < 3:
            return None
        points = []
        for point in outline:
            if not (isinstance(point, list) and len(point) == 2 and
                    all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in point)):
                return None
            x, y = map(round, point)
            if not (0 <= x <= w and 0 <= y <= h):
                return None
            points.append((x, y))
        result.append({"fig": body, "face": face, "outline": points})
    return result


def separate_people(src, people, renderer):
    """Keep whole bodies outside overlaps; inside overlaps retain only pixels assigned to exactly one silhouette."""
    with Image.open(src) as im:
        source = renderer.cutout(im, "auto")
    masks, outlines = [], []
    for person in people:
        mask = Image.new("L", source.size, 0)
        ImageDraw.Draw(mask).polygon(person["outline"], fill=255)
        bounds = Image.new("L", source.size, 0)
        ImageDraw.Draw(bounds).rectangle(person["fig"], fill=255)
        masks.append(bounds)
        outlines.append(ImageChops.multiply(mask, bounds))
    for i in range(len(people)):
        for j in range(i + 1, len(people)):
            overlap = ImageChops.multiply(masks[i], masks[j])
            if not overlap.getbbox():
                continue
            mi, mj = ImageChops.multiply(overlap, outlines[i]), ImageChops.multiply(overlap, outlines[j])
            only_i, only_j = ImageChops.subtract(mi, mj), ImageChops.subtract(mj, mi)
            masks[i] = ImageChops.subtract(masks[i], ImageChops.subtract(overlap, only_i))
            masks[j] = ImageChops.subtract(masks[j], ImageChops.subtract(overlap, only_j))
    for index, person in enumerate(people):
        layer = source.copy()
        layer.putalpha(ImageChops.multiply(source.getchannel("A"), masks[index]))
        face = person["face"]
        centre = ((face[0] + face[2]) // 2, (face[1] + face[3]) // 2)
        if not layer.getchannel("A").getpixel(centre):
            raise ValueError("PEOPLE_FACE_OUTSIDE_MASK")
        path = f"{src}.person-{index}.png"
        layer.save(path)
        person.update(src=path, cutout="alpha")
    return people


class Run:
    """One cover run: the job, the loaded renderer and redraw, and what the routes leave for each other (the guest's
    face in the live frame, read once; the cut-out's guest layer, reused by the sticker).  TASK-13: main() did all of
    this inline (complexity 30); each route is now its own method, called in the same order."""

    def __init__(self, job):
        self.job = job
        self.images = job["out"]                      # the rendered candidates and their judge answers (the caller reads these)
        self.out = job.get("work") or self.images     # everything else (readings, generated layers): one dir per concurrent job
        os.makedirs(self.images, exist_ok=True)
        os.makedirs(self.out, exist_ok=True)
        self.rules = job["rules"]                     # lane_rules `cover`, read by the caller (numbers and their sources there)
        self.face_px = round(self.rules["face_target_frac"] * 1080)
        self.floor_px = round(self.rules["face_floor_frac"] * 1080)     # the one 25 % floor (lane_rules cover.face_floor_frac)
        self.plans, self.column = {}, layout.Column(job, self.rules)   # route -> its crop and text plans; the text column
        self.renderer = fast_text.fast_renderer(load(job["renderer"], "render_cover"))
        self.redraw = load(job["redraw"], "redraw_background")
        self.renderer.install_table(self.renderer.load_styles(job["styles"]))
        self.frame = job["frame"]                     # extracted by the caller's script at the picked moment
        self.lines = [{"text": t} for t in job["lines"]]
        self.host_box, self.guest_box = job["host_box"], job.get("guest_box")
        self.guest_boxes = job.get("guest_boxes", [self.guest_box] if self.guest_box else [])
        self.expected_people = 1 + len(self.guest_boxes)
        self.host_right = not self.guest_box or (self.host_box[0] + self.host_box[2]) >= (self.guest_box[0] + self.guest_box[2])
        # equal participants (the caller's job "peers"): both stand the same size, the host on the side the caller chose
        self.peers = bool(job.get("peers")) and self.guest_box is not None
        self.host_side = job.get("host_side") or ("right" if self.host_right else "left")
        self.brief = job["brief"]
        # the candidates of this run: the routes the caller asked for (it scores and compares all of them together)
        self.routes = job.get("routes") or ["screenshot", "emote", "cutout",
                                            "ai_scene" if self.brief.get("scene_needed") else "ai_sticker"]
        self.specs, self.failed = [], []
        self.frame_guest = []                 # the guest's face in the live frame, read once when a candidate needs it
        self.guest_layer = None
        self.guest_layers = []
        self.group_failed = False
        self.group_failure = None
        self.cut_png = os.path.join(self.out, "gen-cutout.png")

    def guest_in_frame(self):
        if not self.frame_guest and self.guest_box:
            located = guest_face(self.job, self.frame, self.out, "locate-guest-frame", self.guest_box)
            self.frame_guest.append(located if inside(located, self.guest_box) else None)
        return self.frame_guest[0] if self.frame_guest else None

    def head(self):
        """The host's face and head top in the live frame, one reading (reused while the frame's bytes are unchanged)."""
        with Image.open(self.frame) as im:
            w, h = im.size
        question = crop_mod.HEAD_LOCATE.format(w=w, h=h, identity=self.job.get("identity", "以图中实际造型为准"))
        try:
            answer, _ = vision(self.job, "locate-screenshot-head", question, [(self.frame, w)], self.out)
        except (urllib.error.URLError, OSError, ValueError):
            return None, None
        return crop_mod.read_head(answer, w, h)

    def text_ink(self, spec):
        """(style the rotation tries first, ink(i, size)) for this spec's lines: what the renderer will paint."""
        sha_ = self.job.get("content_sha256") or self.renderer.default_content_sha256(spec)
        style = text_fit.probe_style(self.renderer, sha_, self.job.get("recent_style_ids") or ())
        return style, text_fit.renderer_ink(self.renderer, self.job["font"], spec["lines"], style)

    def need_top(self):
        """The face top the punchline's floor size needs (None: not reachable), computed once from the style the rotation
        will try first."""
        if not hasattr(self, "_need_top"):
            ink = self.text_ink({"lines": self.lines})[1]
            self._need_top = text_fit.need_face_top(ink, len(self.lines), self.rules, self.rules["punch_floor_px"])
        return self._need_top

    def fail(self, route, code):
        self.failed.append({"id": route, "route": route, "failure": code})

    @clock.timed("layout")
    def solo_host(self, route, layer, face, guest=None, centre_frac=None):
        """The host placed by her measured face (cover_layout.place_solo), with the guest of the frame on the left when
        there is one who is not an equal.  Returns (stickers, face box, keep boxes, the host's sticker); a face that is
        not in the picture raises ValueError (the route is not made: no guessed face)."""
        sticker, box, plan = self.column.place(self, route, layer, face, self.need_top(),
                                               "right" if guest else self.host_side, centre_frac, guest)
        self.plans[route] = plan
        stickers, keep = [sticker], []
        if guest:
            left, head = guest_left(guest, self.renderer)
            stickers.insert(0, left)
            keep.append(head)
        return stickers, box, keep, sticker

    def screenshot(self):
        clock.route("screenshot")
        job, frame = self.job, self.frame
        with open(job["screenshot_spec"], encoding="utf-8") as fh:
            spec = json.load(fh)
        # the text keeps clear of the faces actually in this frame (the caller's face box is only a default)
        hf, head_top = self.head()
        if hf is None:                          # a cover of her without her face is not made
            return self.fail("screenshot", "HOST_FACE_NOT_FOUND")
        gf = self.guest_in_frame()
        if not self.guest_boxes:            # a solo frame is cropped around the measured face, as tight as the head allows
            ink = self.text_ink(spec)[1]
            need = text_fit.need_face_top(ink, len(spec["lines"]), self.rules, self.rules["punch_floor_px"])
            with clock.phase("layout"):
                self.plans["screenshot"] = dict(crop_mod.plan_crop(hf, head_top, spec.get("keep") or [], self.rules, need),
                                                head_top=head_top, text_top_wanted=need)
            crop_mod.apply_crop(spec, self.plans["screenshot"]["crop"])
        guest_measured = frame_face(gf, spec["crop"]) if gf else None
        spec["face"] = protect([frame_face(hf, spec["crop"]), guest_measured])
        spec["keep"] = list(spec.get("keep") or []) + ([guest_measured] if guest_measured else [])
        self.specs.append(("screenshot", spec, "直播截图整张当底，弹幕框模糊", None,
                           {"kind": "picture", "src": frame, "face": hf}, guest_measured))

    def emote(self, emote):
        clock.route("emote")
        job, out, frame, guest_box, renderer = self.job, self.out, self.frame, self.guest_box, self.renderer
        cutout = "mask:" + emote["mask"]
        guest = ({"src": frame, "cutout": "none", "crop": half(guest_box), "h": GUEST_H} if guest_box else None)
        hf = host_face(job, emote["hd"], out, name="locate-emote")      # the face is measured in the emote itself
        if hf is None:
            return self.fail("emote", "HOST_FACE_NOT_FOUND")
        gf = self.guest_in_frame() if self.peers else None
        guest_measured = None
        if gf:
            stickers, _, guest_measured, face = peer_layers(
                {"src": emote["hd"], "cutout": cutout, "fig": alpha_box(emote["hd"], cutout, renderer), "face": hf},
                {"src": frame, "cutout": "none", "fig": half(guest_box), "face": gf}, renderer, self.host_side,
                self.face_px, self.floor_px, self.need_top())
            host_ref = {"kind": "layer", "src": emote["hd"], "crop": stickers[1]["crop"], "face": hf}
            keep = [guest_measured]
        else:
            try:
                stickers, face, keep, host = self.solo_host("emote", {"src": emote["hd"], "cutout": cutout}, hf, guest,
                                                            self.rules["fans_main_center_x_frac"] if job.get("fans_emote") else None)
            except ValueError as exc:
                return self.fail("emote", str(exc))
            host_ref = {"kind": "layer", "src": emote["hd"], "crop": host["crop"], "face": hf}
        if job.get("fans_emote"):        # the fans' sticker stands on the other side of the main emote (approved collage)
            fans = {"src": job["fans_emote"]["hd"], "cutout": "mask:" + job["fans_emote"]["mask"], "x": self.rules["fans_x_px"],
                    "h": self.rules["fans_h_px"], "y": 1080 - self.rules["fans_h_px"] + self.rules["fans_bottom_bleed_px"]}
            stickers.append(fans)
            keep = [*keep, [fans["x"], fans["y"], min(1920, fans["x"] + layer_size(fans["src"], fans["cutout"], None, fans["h"], renderer)),
                            fans["y"] + round(fans["h"] * 0.65)]]
        self.specs.append(("emote", {"id": "emote", "stickers": stickers,
                                     **self.column.text("emote", self.lines), "face": face, "keep": keep},
                           f"官方表情包 {emote['id']}（{emote['label']}）当{crop_mod.HOST}"
                           + ("，嘉宾为同一帧的截图层（未抠图）" if guest else "") + ("，两人同样大小" if guest_measured else "")
                           + (f"，粉丝贴纸用 {job['fans_emote']['id']} 号" if job.get("fans_emote") else ""),
                           emote["id"], host_ref, guest_measured))

    def cutout(self):
        """The cut-out of the live frame: generated by the cutout route; a later stage (the sticker) reuses its guest
        layer, never generates it; the free stage does not need it at all."""
        clock.route("cutout")
        if "cutout" in self.routes:
            res = generate(self.job, "cutout", self.redraw.build_prompt(self.brief, "cutout"), self.frame, self.cut_png,
                           keep_canvas)
            ready = self.cut_png if res.get("status") in ("REUSED", "AI_BACKGROUND_READY") else None
            if ready is None:
                self.failed.append({"id": "cutout", "route": "cutout", "failure": f"IMAGE_EDIT_{res.get('reason_code')}"})
        else:
            ready = cutout_ready(self.job, self.out, self.redraw, self.frame) if "ai_sticker" in self.routes else None
        if not ready:
            return
        people = figures(self.cut_png, self.renderer, raw=self.expected_people > 1)
        legacy_two = self.expected_people == 2 and len(people) == 2
        if legacy_two:
            people = figures(self.cut_png, self.renderer)
        layers = self.cutout_layers(people, preserve_legacy=legacy_two)
        if layers is None:
            self.group_failed = True
            if "cutout" in self.routes:
                failure = "PEOPLE_LAYERS_UNAVAILABLE" + (f":{self.group_failure}" if self.group_failure else "")
                self.failed.append({"id": "cutout", "route": "cutout", "failure": failure})
            return
        stickers, face, keep, note, host_ref, guest_measured = layers
        if "cutout" in self.routes:
            self.specs.append(("cutout", {"id": "cutout", "stickers": stickers,
                                          **self.column.text("cutout", self.lines, "top" if self.guest_boxes else "auto"),
                                          "face": face, "keep": keep}, note, None,
                               host_ref, guest_measured))

    def cutout_layers(self, people, *, preserve_legacy=False):
        """The cut-out's layers: two people as equals (peers, both faces located), else the host's upper body on
        the right and the guest on the left; one figure (or a one-person clip): the whole cut-out on the bottom edge.
        Sets the guest layer the sticker reuses.  Returns (stickers, face, keep, note, host ref, guest measured)."""
        job, out, cut_png, renderer = self.job, self.out, self.cut_png, self.renderer
        if not preserve_legacy and (self.expected_people > 2 or
                                    self.peers and len(people) != self.expected_people):
            return self.group_cutout_layers(people)
        located = host_face(job, cut_png, out)
        if located is None:                       # no face in the picture (e.g. only legs): nothing to lay out
            self.group_failure = "HOST_FACE_NOT_FOUND"
            return None
        two = bool(self.guest_box and len(people) == 2)
        host_fig, guest_fig = ((people[1], people[0]) if self.host_right else (people[0], people[1])) if two else (None, None)
        if two:
            self.guest_layer = {"src": cut_png, "cutout": "auto", "crop": guest_fig, "h": GUEST_H}
            located_guest = guest_face(job, cut_png, out, "locate-guest-cutout", guest_fig) if self.peers else None
            if self.peers and inside(located, host_fig) and inside(located_guest, guest_fig):
                self.guest_layer = {"src": cut_png, "cutout": "auto", "fig": guest_fig, "face": located_guest}
                stickers, _, guest_measured, face = peer_layers(
                    {"src": cut_png, "cutout": "auto", "fig": host_fig, "face": located}, self.guest_layer, renderer,
                    self.host_side, self.face_px, self.floor_px, self.need_top())
                return (stickers, face, [guest_measured],
                        "CPA 生图只去掉截图背景（人物不改），两人同样大小（地位同等），贴在浅色底上",
                        {"kind": "layer", "src": cut_png, "crop": stickers[1]["crop"], "face": located}, guest_measured)
            if not inside(located, host_fig):
                self.group_failure = "HOST_FACE_NOT_ON_HER_FIGURE"
                return None
        try:
            stickers, face, keep, host = self.solo_host("cutout", {"src": cut_png, "cutout": "auto", **({"crop": host_fig} if two else {})},
                                                        located, self.guest_layer if two else None)
        except ValueError as exc:
            self.group_failure = str(exc)
            return None
        return (stickers, protect([face]), keep,
                f"CPA 生图只去掉截图背景（人物不改），{crop_mod.HOST}按量出的脸定大小" + ("，嘉宾在左" if two else "，整张贴在浅色底上"),
                {"kind": "layer", "src": cut_png, "crop": host["crop"], "face": located}, None)

    def group_cutout_layers(self, figures_found):
        """Locate the host by her independently read face and reuse the other clean layers for the sticker."""
        host = host_face(self.job, self.cut_png, self.out)
        if host is None:
            return None
        if len(figures_found) != self.expected_people:
            people = located_people(self.job, self.cut_png, self.out, self.expected_people)
            if people is None:
                return None
            try:
                people = separate_people(self.cut_png, people, self.renderer)
            except ValueError as exc:
                self.group_failure = str(exc)
                return None
        else:
            people = [{"src": self.cut_png, "cutout": "auto", "fig": fig} for fig in figures_found]
        host_matches = [i for i, p in enumerate(people) if inside(host, p.get("face") or p["fig"])]
        if len(host_matches) != 1:
            return None
        host_index = host_matches[0]
        if not contains_face(host, people[host_index]["fig"]):
            return None
        people[host_index]["face"] = host
        for i, person in enumerate(people):
            if i == host_index or person.get("face"):
                continue
            face = guest_face(self.job, self.cut_png, self.out, f"locate-guest-cutout-{i}", person["fig"])
            if not contains_face(face, person["fig"]):
                return None
            person["face"] = face
        self.guest_layers = [p for i, p in enumerate(people) if i != host_index]
        try:
            stickers, _, guest_faces, face = peer_layers(people[host_index], self.guest_layers, self.renderer, self.host_side,
                                                           self.face_px, self.floor_px)
        except ValueError as exc:
            self.group_failure = str(exc)
            self.guest_layers = []
            return None
        host_layer = next(layer for layer in stickers
                          if layer["src"] == people[host_index]["src"] and inside(host, layer["crop"]))
        measured = guest_faces if len(guest_faces) > 1 else guest_faces[0]
        return (stickers, face, guest_faces, "CPA 生图去背景，全部同框人物按同等大小分层",
                {"kind": "layer", "src": host_layer["src"], "crop": host_layer["crop"], "face": host}, measured)

    def ai_scene(self):
        clock.route("ai_scene")
        scene_png = os.path.join(self.out, "gen-scene.png")
        res = generate(self.job, "ai_scene", self.redraw.build_prompt(self.brief, "scene"), self.frame, scene_png, None)
        if res.get("status") in ("REUSED", "AI_BACKGROUND_READY"):
            located = host_face(self.job, scene_png, self.out, name="locate-ai_scene")   # the text keeps off her real face
            self.specs.append(("ai_scene", {"id": "ai_scene", "frame": scene_png, "crop": [0, 0, 1920],
                                            "layout": "auto", "lines": self.lines, "face": located or [1100, 120, 1680, 700],
                                            "keep": []},
                               f"整幅 AI 场景（{self.brief.get('scene_reason', '')}）", None,
                               {"kind": "picture", "src": scene_png}, None))
        else:
            self.failed.append({"id": "ai_scene", "route": "ai_scene",
                                "failure": f"IMAGE_EDIT_{res.get('reason_code')}"})

    def ai_sticker(self):
        clock.route("ai_sticker")
        if self.group_failed or (self.expected_people > 2 and not self.guest_layers):
            self.failed.append({"id": "ai_sticker", "route": "ai_sticker", "failure": "PEOPLE_LAYERS_UNAVAILABLE"})
            return
        ref = os.path.join(self.out, "ref-host.png")
        face = host_face(self.job, self.frame, self.out, name="locate-screenshot")
        if face is None:                  # the reference must show her face: a body box alone can be only legs
            return self.fail("ai_sticker", "HOST_FACE_NOT_IN_FRAME")
        region = self.host_box
        if not contains_face(face, region):
            fw, fh = face[2] - face[0], face[3] - face[1]
            region = [max(0, face[0] - fw), max(0, face[1] - 2 * fh),
                      min(1920, face[2] + fw), min(1080, face[3] + 2 * fh)]
        with Image.open(self.frame) as im:
            im.crop(tuple(region)).save(ref)
        sticker_png = os.path.join(self.out, "gen-sticker.png")
        res = generate(self.job, "ai_sticker", self.redraw.build_prompt(self.brief, "sticker"), ref, sticker_png,
                       keep_canvas)
        if res.get("status") not in ("REUSED", "AI_BACKGROUND_READY"):
            self.failed.append({"id": "ai_sticker", "route": "ai_sticker",
                                "failure": f"IMAGE_EDIT_{res.get('reason_code')}"})
            return
        try:
            stickers, face, keep, host_ref, guest_measured = self.sticker_layers(sticker_png)
        except ValueError as exc:
            self.failed.append({"id": "ai_sticker", "route": "ai_sticker", "failure": str(exc)})
            return
        guest_layer = self.guest_layer
        self.specs.append(("ai_sticker", {"id": "ai_sticker", "stickers": stickers,
                                          **self.column.text("ai_sticker", self.lines), "face": face, "keep": keep},
                           f"CPA 生图画的这一刻 Q 版贴纸当{crop_mod.HOST}" + ("，嘉宾为抠图层" if guest_layer and guest_layer["src"] == self.cut_png
                                                      else "，嘉宾为同一帧的截图层（未抠图）" if guest_layer else "")
                           + ("，两人同样大小" if guest_measured else ""), None, host_ref, guest_measured))

    def sticker_layers(self, sticker_png):
        """The generated sticker as the host, beside the cut-out's equal guest or the guest from the same frame (as
        equals when both faces are located, else the old guest layer).  Returns (stickers, face, keep, host ref,
        guest measured)."""
        renderer = self.renderer
        if self.guest_layers:
            hf = host_face(self.job, sticker_png, self.out, name="locate-ai_sticker")
            if not hf:
                raise ValueError("STICKER_HOST_FACE_UNAVAILABLE")
            stickers, _, guests, face = peer_layers(
                {"src": sticker_png, "cutout": "auto", "fig": alpha_box(sticker_png, "auto", renderer), "face": hf},
                self.guest_layers, renderer, self.host_side, self.face_px, self.floor_px, self.need_top())
            measured = guests if len(guests) > 1 else guests[0]
            host_layer = next(layer for layer in stickers if layer["src"] == sticker_png)
            return stickers, face, guests, {"kind": "layer", "src": sticker_png,
                                           "crop": host_layer["crop"], "face": hf}, measured
        peer_guest = self.guest_layer if self.guest_layer and "face" in self.guest_layer else None
        if self.peers and peer_guest is None:          # no equal cut-out guest: the guest from the same frame
            gf = self.guest_in_frame()
            peer_guest = {"src": self.frame, "cutout": "none", "fig": half(self.guest_box), "face": gf} if gf else None
        hf = host_face(self.job, sticker_png, self.out, name="locate-ai_sticker") if self.peers and peer_guest else None
        if hf:
            stickers, _, guest_measured, face = peer_layers(
                {"src": sticker_png, "cutout": "auto", "fig": alpha_box(sticker_png, "auto", renderer), "face": hf},
                peer_guest, renderer, self.host_side, self.face_px, self.floor_px, self.need_top())
            self.guest_layer = peer_guest
            return (stickers, face, [guest_measured],
                    {"kind": "layer", "src": sticker_png, "crop": stickers[1]["crop"], "face": hf}, guest_measured)
        if self.guest_layer and "fig" in self.guest_layer:    # the equal composition was not possible: the old layer
            self.guest_layer = {"src": self.guest_layer["src"], "cutout": self.guest_layer["cutout"],
                                "crop": self.guest_layer["fig"], "h": GUEST_H}
        if self.guest_layer is None and self.guest_box:   # no cut-out of this clip: the guest from the same frame (as emote)
            self.guest_layer = {"src": self.frame, "cutout": "none", "crop": half(self.guest_box), "h": GUEST_H}
        hf = hf or host_face(self.job, sticker_png, self.out, name="locate-ai_sticker")
        if not hf:                         # the drawn sticker shows no face to lay out by
            raise ValueError("STICKER_HOST_FACE_UNAVAILABLE")
        stickers, face, keep, host = self.solo_host("ai_sticker", {"src": sticker_png, "cutout": "auto"}, hf, self.guest_layer)
        return stickers, face, keep, {"kind": "layer", "src": sticker_png, "crop": host["crop"], "face": hf}, None

    def render_row(self, route, spec, note, main_emote, host_ref, guest_measured):
        """Render one candidate, measure its face and have it judged.  Returns its row."""
        job, out = self.job, self.out
        if job.get("style_id"):                # an explicit pin only (skill: spec style_id); the lane sends none
            spec["style_id"] = job["style_id"]
        uses = {"cutout": ["cutout"], "ai_sticker": ["ai_sticker", "cutout"], "ai_scene": ["ai_scene"]}.get(route, [])
        row = {"id": route, "route": route, "note": note, "main_emote": main_emote, "spec": spec,
               "generation": {k: GENERATIONS[k] for k in uses
                              if k in GENERATIONS and (k == route or f"gen-{k}.png" in json.dumps(spec))}}
        if route == "ai_sticker" and self.guest_layers:
            row["note"] = f"CPA 生图画的 Q 版贴纸当{crop_mod.HOST}，嘉宾为抠图层，{self.expected_people} 人同样大小"
        if self.expected_people > 2:
            row["expected_guest_count"] = self.expected_people - 1
        started = time.monotonic()
        clock.route(route)
        with clock.phase("layout"):
            style, ink = self.text_ink(spec)         # size line 1 before rendering, from where the face really sits
            row["background"] = background.choose(self.renderer, spec, style, route, job)   # pastel routes only (None: a frame)
            plan = text_fit.plan_text(ink, len(spec["lines"]), spec["face"][1], style, self.rules)
            text_fit.apply_plan(spec, plan)
        err = io.StringIO()
        try:
            with contextlib.redirect_stderr(err), clock.phase("render"):
                img, rec, selection = self.renderer.render(spec, job["font"],
                                                           recent_style_ids=job.get("recent_style_ids") or (),
                                                           content_sha256=job.get("content_sha256"))
        except SystemExit as exc:
            row.update(status="RENDER_FAILED", failure=f"RENDER_REFUSED: {exc}")
            return row
        path = os.path.join(self.images, f"{route}.png")
        img.save(path)
        img.save(os.path.join(self.images, f"{route}.jpg"), quality=90)
        punch = rec[-1]
        row.update(status="RENDERED", image=path, image_sha256=sha(path), punch_font_size=int(punch["size"]),
                   render=self.render_record(spec, rec, selection, guest_measured), render_s=round(time.monotonic() - started, 2),
                   punch_ink_h=int(punch["h"]), punch_floor_px=self.rules["punch_floor_px"], text_plan=plan,
                   punch_below_floor=int(punch["size"]) < self.rules["punch_floor_px"], crop_plan=self.plans.get(route))
        face_box, face_note = measured_face(job, route, spec, host_ref, out, self.renderer)
        group_faces = (guest_measured if guest_measured and isinstance(guest_measured[0], list)
                       else [guest_measured] if guest_measured else [])
        row.update(face_measured=face_box, face_method=face_note,
                   guest_face_measured=group_faces[0] if group_faces else None)
        if len(group_faces) > 1:
            row["guest_faces_measured"] = group_faces
        question = job["judge_question"].replace("{route}", route).replace("{route_note}", note)
        try:
            answer, cached = judge(job, route, question, path, self.frame, self.images)
        except (urllib.error.URLError, OSError, ValueError) as exc:
            row.update(judge_error=type(exc).__name__)
            return row
        verdict = parse_verdict(answer)
        if isinstance(verdict, dict) and face_box is not None:
            # the face box the score uses is the measured one (the same picture always gives the same box); the
            # judge's own reading of the rendered candidate is kept beside it
            verdict["face_judge"] = verdict.get("face")
            verdict["face"] = [max(0, min(1920, face_box[0])), max(0, min(1080, face_box[1])),
                               max(0, min(1920, face_box[2])), max(0, min(1080, face_box[3]))]
        row.update(answer=answer, verdict=verdict, judge_cached=cached)
        return row

    def render_record(self, spec, rec, selection, guest_measured):
        style = next((s for s in self.renderer.STYLES if s["style_id"] == selection.get("style_id")), {})
        composed = bool(spec.get("stickers"))
        return {"measured_lines": [{k: int(ln[k]) for k in ("x", "y", "w", "h")} for ln in rec],
                "layout": rec[-1].get("layout"), "style_id": selection.get("style_id"),
                "colors": [ln.get("color") for ln in rec], "sizes": [int(ln["size"]) for ln in rec],
                "size_ratio": style.get("size_ratio"), "tilt": rec[-1].get("tilt"),
                "stroke": rec[-1].get("stroke"), "style_selection": selection,
                "host_side": self.host_side if composed and guest_measured else ("right" if self.host_right else "left"),
                "composition": ("peers_equal" if composed and guest_measured else
                                "layers" if composed else "picture")}


def main(argv):
    with open(argv[1], encoding="utf-8") as fh:
        job = json.load(fh)
    run = Run(job)
    if "screenshot" in run.routes:
        run.screenshot()
    if job.get("emote") and "emote" in run.routes:
        run.emote(job["emote"])
    run.cutout()
    if "ai_scene" in run.routes:
        run.ai_scene()
    if "ai_sticker" in run.routes:
        run.ai_sticker()
    rows = list(run.failed) + [run.render_row(*spec) for spec in run.specs]
    for row in rows:
        row["phases_s"] = clock.seconds(row["route"])      # where this route's seconds went (cover_clock.PHASES)
    with open(os.path.join(run.images, job.get("result", "result.json")), "w", encoding="utf-8") as fh:
        json.dump({"rows": rows, "calls": CALLS, "frame_sha256": sha(run.frame), "routes": run.routes,
                   "column": run.column.record()}, fh, ensure_ascii=False, indent=1)
    rendered = sum(1 for r in rows if r.get("status") == "RENDERED")
    print(f"COVER_CANDIDATES_DONE rendered={rendered} calls={len(CALLS)}")
    refused = [r["failure"] for r in rows if r.get("status") == "RENDER_FAILED"]
    if not rendered and refused:             # the text fits no picture: the caller shortens it (renderer's words)
        print("\n".join(refused))
        return 23
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
