#!/usr/bin/env python3
"""pick_frame.py CUT.mp4 FRAME_DIR OUT.json T1,T2,... [AVOID.json]

Cover frame choice (skill 7, and the 692 review).  For each candidate time, extract one full-resolution frame (the cut
is 1920x1080, so boxes read from it are in that space) and ask the CPA vision route whether the picture shows an
unrelated page or UI.  The first frame answered CLEAN is chosen.  Then the same frame is read once more for boxes:
every block of viewers' text wherever it sits (to blur), the host's face (the only thing the blur keeps off), the host's body (to keep clear of the blur, and the reference of the cover's
generated layers) and the guest's body (the guest's layer of a composed cover).  The host is identified from the
frame and nameplate, allowing the headwear actually shown instead of assuming her ears are visible.

A frame that looks like the base frame of a cover this stream already has (AVOID.json, step6_cover.base_avoid: the
same recording within SAME_TIME_S seconds, or her face's 16x16 dHash within SAME_FACE_BITS of that cover's) is passed
over for the next one; only when every frame is such a repeat is the first of them taken (`repeat_forced`).  Her face
box in each frame is the face reading's own answer (the layout's face does not stay in one place: a box carried from
another frame finds nothing alike, 2026-11-25 calibration).

Before that, the whole cut is looked at for the host's signature move (owner 2026-11-14: a clip that has a classic
action, such as squatting down holding her head when shy, takes its screenshot there): frames every ACTION_STEP_S
seconds are tiled into numbered sheets and each sheet is asked once; when several sheets have one, one more question
picks among them.  That frame is the first candidate.

OUT.json = {"chosen": seconds or null, "action": {"t", "what"} or {"t": null, "why"}, "checked": [{t, verdict, text}], "calls": [{kind, t, model, rc, elapsed_s, usage}], "boxes": {"danmaku": [[x0,y0,x1,y1]...],
"person": [[...]...], "face": [[...]...], "guest": [[...]...]} or null}.  A box read that fails leaves boxes null; the caller then uses the fixed layout.

Runs on OCI3 under the root wrapper that loads cpa.env (CPA_BASE_URL / CPA_API_KEY); the key is never printed.
"""
import base64
import io
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request

QUOTA_CODES = frozenset({
    "organization_spend_limit_exceeded",
    "insufficient_quota",
    "insufficient_user_quota",
    "credit_balance_exhausted",
    "billing_hard_limit_reached",
    "usage_limit_reached",
})
QUOTA_TYPES = frozenset({"insufficient_quota", "usage_limit_reached"})


def _models():
    """(vision_model, fallback_model, fallback_low_effort, fallback_min_tokens) from lane_rules `cpa`."""
    primary = os.environ.get("MANUAL_LANE_VISION_MODEL") or "gpt-6.2-sol"
    fallback = os.environ.get("MANUAL_LANE_FALLBACK_MODEL")
    effort, min_tokens = "low", 32768
    try:
        sys.path.insert(0, os.environ.get("MANUAL_LANE_REMOTE_REPO") or os.path.abspath(os.path.join(os.path.dirname(__file__), "../../..")))
        from src.autoslice.manual_lane.lane_rules import rules
        sec = rules("cpa")
        if "MANUAL_LANE_VISION_MODEL" not in os.environ:
            primary = str(sec.get("vision_model") or sec.get("model") or primary)
        if fallback is None:
            fallback = str(sec.get("fallback_model") or "")
        efforts = sec.get("fallback_efforts")
        if isinstance(efforts, dict):
            effort = str(efforts.get("low") or "low")
        min_tokens = int(sec.get("fallback_min_output_tokens") or min_tokens)
    except (ImportError, KeyError, ValueError, OSError):
        if fallback is None:
            fallback = "gemini-4-neon"
    return primary, (fallback if fallback != primary else ""), effort, min_tokens


MODEL, FALLBACK_MODEL, FALLBACK_EFFORT, FALLBACK_MIN_TOKENS = _models()


def _definite_quota_error(raw):
    """Return a short quota reason when `raw` (JSON text/bytes or error dict) is a definite quota refusal, else None."""
    if isinstance(raw, (bytes, str)):
        text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw
        try:
            parsed = json.loads(text)
        except ValueError:
            return None
        err = parsed.get("error", parsed) if isinstance(parsed, dict) else None
    elif isinstance(raw, dict):
        err = raw.get("error", raw) if "error" in raw and isinstance(raw.get("error"), dict) else raw
    else:
        return None
    if not isinstance(err, dict):
        return None
    if err.get("code") in QUOTA_CODES:
        return str(err["code"])
    if err.get("type") in QUOTA_TYPES:
        return str(err["type"])
    return None


def _channel():
    """The host's name and how to tell her in a picture, from the channel profile of the deployed repo (the lane names
    the repo, adapters.remote_python; read in place, the tool's own repo); a nameless host when there is no repo."""
    try:
        sys.path.insert(0, os.environ.get("MANUAL_LANE_REMOTE_REPO") or os.path.abspath(os.path.join(os.path.dirname(__file__), "../../..")))
        from src.autoslice.manual_lane import channel
        doc = channel.text()
        return channel.HOST, str(doc.get("picture_body_hint") or ""), str(doc.get("picture_face_hint") or "")
    except (ImportError, ValueError, OSError):
        return "主播", "", ""


HOST, BODY_HINT, FACE_HINT = _channel()
QUESTION = ("这是直播切片的一帧（1920x1080）。画面里是否出现与本段直播内容无关的页面、测试页或界面（例如和直播无关的网页、"
            "测试用的页面标题、系统界面）？如果有，只回答 DIRTY；如果画面干净，只回答 CLEAN。只回答这一个词。")
BOX_QUESTION = ("这是直播画面（1920x1080 像素）。请给出四类矩形，坐标是整幅画面的像素，格式 [x0,y0,x1,y1]：\n"
                "danmaku：画面上每一块能读出观众文字的区域（弹幕框、评论列表、礼物栏、SC、点歌单等，在哪就框哪，"
                "压在人物身上的也框；一块一个框，框住整列文字，宁大勿小）；\n"
                "person：主播" + HOST + "本人的身体范围" + (BODY_HINT or "，依据名牌和画面辨认") + "；\n"
                "face：主播" + HOST + "的脸，只框眉毛到下巴、两颊之间（不含头发、耳朵、发饰）" + ("，" + FACE_HINT.strip("，；。") if FACE_HINT else "") + "；\n"
                "guest：画面里除主播" + HOST + "以外的每一位其他人物/主播（无论前景或背景、是否遮挡别人，须包含头饰、角、帽子、发丝和全部可见身体），"
                "每人一个框，宁大勿小；有几位就给几个框。\n"
                "画面里同时出现多位人物（哪怕同为白发或戴帽子）时，先按当场外貌特征与站位认出主播" + HOST
                + "本人放进 person，其余所有人物全部放进 guest，不要漏人，也不要把别的人物放进 person。\n"
                "只输出一个 JSON 对象：{\"danmaku\": [[x0,y0,x1,y1]], \"person\": [[x0,y0,x1,y1]], \"face\": [[x0,y0,x1,y1]], "
                "\"guest\": [[x0,y0,x1,y1]]}；"
                "找不到的类别写空数组，不要编造。")


ACTION_STEP_S = 2.0
ACTION_CELLS = (5, 4)              # columns, rows of one sheet
ACTION_CELL_PX = (384, 216)
ACTION_QUESTION = ("这是一段直播切片按时间顺序抽出的若干帧，拼成一张图，每格左上角的黄色数字是这一格的编号。看主播" + HOST
                   + (f"（{BODY_HINT.strip('，')}）" if BODY_HINT else "") + "：有没有哪一格里她做出了一眼就能认出、带着情绪的标志性动作——"
                   "例如蹲下抱头、双手捂脸、弯腰捂脸、跳起来、趴下、比心？站着说话、摆手、普通手势都不算。"
                   "只输出一个 JSON 对象：{\"cell\": 编号, \"what\": \"动作的简短描述\"}；没有这样的格就输出 {\"cell\": null}。")
ACTION_PICK = ("这几格是同一段直播切片里不同时刻、主播" + HOST + "做出标志性动作的画面，格子左上角是编号。哪一格的动作最完整、"
               "最能一眼认出、最适合当视频封面？只输出一个 JSON 对象：{\"cell\": 编号, \"what\": \"动作的简短描述\"}。")
FACE_QUESTION = ("这是直播切片的一帧（1920x1080）。画面里能不能看清主播" + HOST + "本人的脸"
                 + (f"（{FACE_HINT.strip('，')}）" if FACE_HINT else "") + "？镜头只拍到腿、脚、身体别的部分，脸在画面外，"
                 "或者只有角落里很小的头像、图标、表情贴图，都算看不清。看得清就给出她的脸的像素框 [x0,y0,x1,y1]"
                 "（只框眉毛到下巴、两颊之间，不含头发、耳朵、发饰），只输出一个 JSON 对象 {\"face\": [x0,y0,x1,y1]}；"
                 "看不清只输出 {\"face\": null}。")
# 2026-11-25 calibration (84 same-stream pairs of the 11-23..11-25 cover frames, labelled by eye: same expression and
# pose or not): the 16x16 dHash of each frame's own face box, in 64ths, at most 13 marked 13 of 18 alike pairs and 4 of
# 21 unlike ones on 11-24/25, and 1 of 12 alike, 0 of 33 unlike on 11-23 (her face small in a corner); the person box's
# 8x8 dHash at 16 (the plan's starting point) marked 11 of 30 alike and 17 of 54 unlike.  Weak: it only orders frames.
SAME_FACE_BITS = 13
SAME_TIME_S = 15      # the same recording within 15 s: the same moment (the plan's starting point; 2 of 3 such pairs alike)
READING_KEY = (BOX_QUESTION + "\n" + ACTION_QUESTION + "\n" + ACTION_PICK + "\n" + FACE_QUESTION)   # a reading made
#                                                                     with other questions is not reused


def duration(cut):
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", cut],
                         check=True, capture_output=True, text=True).stdout
    return float(out.strip())


def sheet(cut, times, path):
    """One numbered sheet of the frames at `times` (cell k = times[k-1]), as base64 jpeg."""
    from PIL import Image, ImageDraw, ImageFont

    try:
        font = ImageFont.truetype("DejaVuSans-Bold.ttf", 24)
    except OSError:
        font = ImageFont.load_default()

    cols = min(ACTION_CELLS[0], len(times))
    rows = (len(times) + cols - 1) // cols
    w, h = ACTION_CELL_PX
    canvas = Image.new("RGB", (cols * w, rows * h))
    draw = ImageDraw.Draw(canvas)
    for k, t in enumerate(times):
        one = path + f".{k}.jpg"
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{t:.3f}", "-i", cut,
                        "-frames:v", "1", "-vf", f"scale={w}:{h}", one], check=True)
        with Image.open(one) as im:
            x, y = (k % cols) * w, (k // cols) * h
            canvas.paste(im.convert("RGB"), (x, y))
        os.remove(one)
        draw.rectangle([x, y, x + 44, y + 30], fill="black")
        draw.text((x + 6, y + 2), str(k + 1), fill="yellow", font=font)
    canvas.save(path, quality=85)
    with open(path, "rb") as fh:
        return base64.b64encode(fh.read()).decode("ascii")


def read_cell(text, count):
    match = re.search(r"\{.*\}", text, re.DOTALL)
    try:
        raw = json.loads(match.group(0)) if match else {}
    except ValueError:
        raw = {}
    cell = raw.get("cell")
    if isinstance(cell, int) and not isinstance(cell, bool) and 1 <= cell <= count:
        return cell, str(raw.get("what") or "").strip()
    return None, ""


def find_action(cut, frame_dir, base, key):
    """{"t": seconds, "what": description} of the host's signature move in the cut, or {"t": None, "why": ...}."""
    length = duration(cut)
    grid = [round(ACTION_STEP_S / 2 + i * ACTION_STEP_S, 3) for i in range(int(length // ACTION_STEP_S))]
    per = ACTION_CELLS[0] * ACTION_CELLS[1]
    found = []
    for n in range(0, len(grid), per):
        times = grid[n:n + per]
        image = sheet(cut, times, os.path.join(frame_dir, f"action-{n // per}.jpg"))
        cell, what = read_cell(ask(base, key, image, ACTION_QUESTION, max_tokens=120, kind="action", t=times[0]), len(times))
        if cell:
            found.append((times[cell - 1], what))
    if not found:
        return {"t": None, "why": f"{len(grid)} 帧里没有标志性动作"}
    t, what = found[0]
    if len(found) > 1:
        image = sheet(cut, [f[0] for f in found], os.path.join(frame_dir, "action-pick.jpg"))
        cell, _ = read_cell(ask(base, key, image, ACTION_PICK, max_tokens=120, kind="action-pick"), len(found))
        t, what = found[(cell or 1) - 1]
    # the frame of the sheet the move was seen in is kept as it is: asked again about frames half a second apart, the
    # model took her standing up from the squat as "the most complete" (1305990, 2026-11-14)
    return {"t": t, "what": what, "seen": [{"t": f[0], "what": f[1]} for f in found]}


# Every box of the cover is read in a 1920x1080 view (`fit_1920x1080` / `FRAME_SIZE_FILTER`) and converted back into the
# source frame's own pixel coordinates before anything uses or stores it (goldfish 4 / 1280x720 streams).
BOX_VIEW_W, BOX_VIEW_H = 1920, 1080
FRAME_SIZE_FILTER = "scale=1920:1080:force_original_aspect_ratio=decrease,pad=1920:1080:(ow-iw)/2:(oh-ih)/2"


def video_size(cut):
    """(width, height) of the first video stream of `cut` via ffprobe."""
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height", "-of", "csv=s=x:p=0", cut],
        check=True, capture_output=True, text=True,
    ).stdout.strip().splitlines()[0]
    w, h = (int(v) for v in out.split("x", 1))
    if w <= 0 or h <= 0:
        raise ValueError(f"invalid video dimensions: {out}")
    return w, h


def _letterbox_params(src_w, src_h):
    """(scale, ox, oy) mapping source `(src_w, src_h)` into `(BOX_VIEW_W, BOX_VIEW_H)` with aspect-ratio padding."""
    scale = min(BOX_VIEW_W / src_w, BOX_VIEW_H / src_h)
    iw, ih = round(src_w * scale), round(src_h * scale)
    return scale, (BOX_VIEW_W - iw) / 2, (BOX_VIEW_H - ih) / 2


def fit_1920x1080(im):
    """RGB 1920x1080 image matching `FRAME_SIZE_FILTER` (aspect-ratio decrease + centered black padding)."""
    from PIL import Image

    rgb = im.convert("RGB")
    src_w, src_h = rgb.size
    if (src_w, src_h) == (BOX_VIEW_W, BOX_VIEW_H):
        return rgb
    scale, ox, oy = _letterbox_params(src_w, src_h)
    iw, ih = max(1, round(src_w * scale)), max(1, round(src_h * scale))
    canvas = Image.new("RGB", (BOX_VIEW_W, BOX_VIEW_H), (0, 0, 0))
    canvas.paste(rgb.resize((iw, ih), Image.LANCZOS), (round(ox), round(oy)))
    return canvas


def to_source_box(box, src_w=BOX_VIEW_W, src_h=BOX_VIEW_H):
    """Convert one [x0, y0, x1, y1] box from the 1920x1080 view into source pixels, clipped; None if invalid/empty."""
    if not (isinstance(box, (list, tuple)) and len(box) == 4
            and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in box)):
        return None
    vx0, vy0, vx1, vy1 = (float(v) for v in box)
    if vx0 >= vx1 or vy0 >= vy1:
        return None
    if (src_w, src_h) == (BOX_VIEW_W, BOX_VIEW_H):
        sx0, sy0, sx1, sy1 = vx0, vy0, vx1, vy1
    else:
        scale, ox, oy = _letterbox_params(src_w, src_h)
        sx0, sy0 = (vx0 - ox) / scale, (vy0 - oy) / scale
        sx1, sy1 = (vx1 - ox) / scale, (vy1 - oy) / scale
    x0 = max(0, min(src_w, round(sx0)))
    y0 = max(0, min(src_h, round(sy0)))
    x1 = max(0, min(src_w, round(sx1)))
    y1 = max(0, min(src_h, round(sy1)))
    return [x0, y0, x1, y1] if x0 < x1 and y0 < y1 else None


def extract(cut, t, path):
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{t:.3f}", "-i", cut,
                    "-frames:v", "1", "-vf", FRAME_SIZE_FILTER, "-q:v", "3", path], check=True)
    with open(path, "rb") as fh:
        return base64.b64encode(fh.read()).decode("ascii")


CALLS = []   # every paid vision request made, for the step's cost record (OUT.json "calls")


def answered_model():
    return CALLS[-1]["model"] if CALLS else MODEL


def _send_vision(base, key, model, effort, image_b64, question, max_tokens):
    body = {"model": model, "reasoning": {"effort": effort}, "max_output_tokens": max_tokens,
            "input": [{"role": "user", "content": [
                {"type": "input_text", "text": question},
                {"type": "input_image", "image_url": "data:image/jpeg;base64," + image_b64}]}]}
    req = urllib.request.Request(f"{base}/responses", data=json.dumps(body).encode(), method="POST", headers={
        "Authorization": f"Bearer {key}", "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"})
    with urllib.request.urlopen(req, timeout=240) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _extract_complete_text(payload, call):
    call["usage"] = payload.get("usage") or {}
    text = payload.get("output_text") or ""
    if not text:
        for item in payload.get("output") or []:
            if item.get("type") == "message":
                text += "".join(c.get("text", "") for c in item.get("content") or [])
    items = [i.get("status") for i in payload.get("output") or [] if i.get("type") == "message"]
    if (payload.get("status") != "completed" or payload.get("incomplete_details") or payload.get("error")
            or any(s != "completed" for s in items) or not text.strip()):
        call["incomplete"] = {"status": payload.get("status"), "incomplete_details": payload.get("incomplete_details")}
        raise ValueError(f"CPA vision answer not complete: status={payload.get('status')}")
    return text.strip()


def ask(base, key, image_b64, question, max_tokens=64, kind="clean", t=None):
    call = {"kind": kind, "t": t, "model": MODEL, "effort": "low", "rc": 1}
    CALLS.append(call)
    started = time.monotonic()
    quota_reason = None
    try:
        try:
            payload = _send_vision(base, key, MODEL, "low", image_b64, question, max_tokens)
            quota = _definite_quota_error(payload.get("error")) if isinstance(payload, dict) else None
            if quota:
                quota_reason = f"200 {quota}"
            else:
                text = _extract_complete_text(payload, call)
                call["rc"] = 0
                return text
        except urllib.error.HTTPError as exc:
            err_bytes = exc.read()
            quota = _definite_quota_error(err_bytes)
            if not quota:
                raise urllib.error.HTTPError(exc.url, exc.code, exc.msg, exc.hdrs, io.BytesIO(err_bytes)) from None
            quota_reason = f"HTTP {exc.code} {quota}"
        if not FALLBACK_MODEL or FALLBACK_MODEL == MODEL:
            raise RuntimeError(f"CPA_QUOTA_REFUSED: {quota_reason}")
        orig_model = MODEL
        call["model"] = FALLBACK_MODEL
        call["effort"] = FALLBACK_EFFORT
        call["fallback_from"] = orig_model
        call["fallback_reason"] = f"CPA_QUOTA_REFUSED: {quota_reason}"
        try:
            fb_payload = _send_vision(base, key, FALLBACK_MODEL, FALLBACK_EFFORT, image_b64, question,
                                      max(max_tokens, FALLBACK_MIN_TOKENS))
            text = _extract_complete_text(fb_payload, call)
        except urllib.error.HTTPError as fb_exc:
            fb_body = fb_exc.read()
            fb_code = _definite_quota_error(fb_body) or f"HTTP {fb_exc.code}"
            call["fallback_note"] = f"原模型 {orig_model} 额度不足（{quota_reason}），改用 {FALLBACK_MODEL} 仍失败（{fb_code}）"
            raise RuntimeError(
                f"CPA_FALLBACK_FAILED: 原模型 {orig_model} 额度不足（{quota_reason}）；回退模型 {FALLBACK_MODEL} 也不可用（{fb_code}）"
            ) from fb_exc
        except Exception as fb_exc:
            fb_why = f"{type(fb_exc).__name__}: {str(fb_exc)[:120]}"
            call["fallback_note"] = f"原模型 {orig_model} 额度不足（{quota_reason}），改用 {FALLBACK_MODEL} 仍失败（{fb_why}）"
            raise RuntimeError(
                f"CPA_FALLBACK_FAILED: 原模型 {orig_model} 额度不足（{quota_reason}）；回退模型 {FALLBACK_MODEL} 也不可用（{fb_why}）"
            ) from fb_exc
        call["fallback_note"] = f"原模型 {orig_model} 额度不足（{quota_reason}），改用 {FALLBACK_MODEL}"
        call["rc"] = 0
        return text
    finally:
        call["elapsed_s"] = round(time.monotonic() - started, 1)


def face_hash(path, box):
    """Her face's 16x16 dHash (256 bits, hex) in the frame at `path`."""
    from PIL import Image

    with Image.open(path) as im:
        g = im.convert("L").crop(tuple(box)).resize((17, 16), Image.LANCZOS)
    px, bits = g.tobytes(), 0
    for r in range(16):
        for c in range(16):
            bits = (bits << 1) | (px[r * 17 + c] > px[r * 17 + c + 1])
    return f"{bits:064x}"


def face_bits(a, b):
    """The distance of two face hashes in 64ths (a 16x16 hash has 256 bits)."""
    return bin(int(a, 16) ^ int(b, 16)).count("1") / 4


def repeat_of(t, dhash, avoid):
    """Why the frame at `t` (seconds into the cut) repeats a cover of this stream, or None.  `avoid`: {"source", "offset_s"
    (the cut's start on that recording's clock; null for a cut pieced together), "covers": [{cid, source, t_source, dhash}]}."""
    for cover in avoid.get("covers") or []:
        if (cover.get("t_source") is not None and avoid.get("offset_s") is not None and cover.get("source") == avoid.get("source")
                and abs(avoid["offset_s"] + t - cover["t_source"]) < SAME_TIME_S):
            return f"{cover['cid']} 的封面底帧同一时刻（相差 {abs(avoid['offset_s'] + t - cover['t_source']):.1f}s）"
        if dhash and cover.get("dhash") and face_bits(dhash, cover["dhash"]) <= SAME_FACE_BITS:
            return f"{cover['cid']} 的封面底帧脸一样（dHash 距离 {face_bits(dhash, cover['dhash'])}）"
    return None


def read_face(text):
    match = re.search(r"\{.*\}", text, re.DOTALL)
    try:
        box = json.loads(match.group(0)).get("face") if match else None
    except (ValueError, AttributeError):
        return None
    if isinstance(box, list) and len(box) == 4 and all(isinstance(v, (int, float)) for v in box):
        x0, y0, x1, y1 = (int(v) for v in box)
        if 0 <= x0 < x1 <= 1920 and 0 <= y0 < y1 <= 1080:
            return [x0, y0, x1, y1]
    return None


def read_boxes(text, frame_size=(BOX_VIEW_W, BOX_VIEW_H)):
    """{'danmaku': [...], 'person': [...] | None, 'face': [...], 'guest': [...] | None} in `frame_size` pixels.

    Strict keys (`person`, `guest`): missing key, null, non-list, or any invalid/out-of-frame box yields `None`
    so callers keep unknown distinct from an explicit empty `[]`.
    """
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        return None
    try:
        raw = json.loads(match.group(0))
    except ValueError:
        return None
    if not isinstance(raw, dict):
        return None
    src_w, src_h = frame_size
    out = {}
    for key in ("person", "guest"):
        val = raw.get(key)
        if not isinstance(val, list):
            out[key] = None
            continue
        boxes = []
        valid = True
        for box in val:
            converted = to_source_box(box, src_w, src_h)
            if converted is None:
                valid = False
                break
            boxes.append(converted)
        out[key] = boxes if valid else None
    for key in ("danmaku", "face"):
        val = raw.get(key)
        out[key] = ([c for b in val if (c := to_source_box(b, src_w, src_h)) is not None]
                    if isinstance(val, list) else [])
    return out


def look_at_face(row, path, image, t, base_url, key, avoid):
    """(her face box, its hash) when the frame at `t` shows her face, else None; the row records the answer and whether
    the frame repeats a cover of this stream."""
    try:                     # 1645880 (2026-11-15): the cover line fell where the camera showed only her shoes
        face = read_face(ask(base_url, key, image, FACE_QUESTION, max_tokens=80, kind="face", t=t))
    except (urllib.error.URLError, OSError, ValueError) as exc:
        row["face"] = f"ERROR {type(exc).__name__}"
        return None
    row["face"] = "FACE" if face else "NOFACE"
    if not face:
        return None
    try:
        dhash = face_hash(path, face)
    except OSError as exc:       # ranking only: the frame is still judged on its moment, the failure recorded
        dhash, row["dhash_error"] = None, type(exc).__name__
    row.update(face_box=face, repeat=repeat_of(t, dhash, avoid))
    return face, dhash


def fallback(first_repeat, first_clean, image):
    """(chosen, base, its image) when no frame was taken on the way: every frame showing her face repeats a cover, the
    first of them (`repeat_forced`); no clean frame shows her face, the first clean one, as before; else nothing."""
    if first_repeat is not None:
        t, image, face, dhash = first_repeat
        return t, {"t": t, "face": face, "dhash": dhash, "repeat_forced": True}, image
    if first_clean is not None:
        return first_clean[0], {}, first_clean[1]
    return None, {}, image


def main(argv):
    if len(argv) >= 4 and argv[1] == "--boxes-only":
        from PIL import Image

        frame_path, out_json = argv[2], argv[3]
        base_url = os.environ.get("CPA_BASE_URL", "").rstrip("/")
        key = os.environ.get("CPA_API_KEY", "")
        if not base_url or not key:
            raise SystemExit("CPA_BASE_URL / CPA_API_KEY not set (load cpa.env first)")
        with Image.open(frame_path) as im:
            frame_size = im.size
            rgb = fit_1920x1080(im)
            buf = io.BytesIO()
            rgb.save(buf, format="JPEG", quality=88)
        image_b64 = base64.b64encode(buf.getvalue()).decode("ascii")
        try:
            boxes = read_boxes(ask(base_url, key, image_b64, BOX_QUESTION, max_tokens=400, kind="boxes"),
                               frame_size=frame_size)
        except (urllib.error.URLError, OSError, ValueError):
            boxes = None
        with open(out_json, "w", encoding="utf-8") as fh:
            json.dump({"boxes": boxes, "frame_size": list(frame_size), "calls": CALLS,
                       "question": BOX_QUESTION, "model": answered_model()},
                      fh, ensure_ascii=False, indent=1)
        return 0
    cut, frame_dir, out_json, times = argv[1], argv[2], argv[3], [float(x) for x in argv[4].split(",") if x]
    try:
        frame_size = video_size(cut)
    except (subprocess.CalledProcessError, OSError, ValueError):
        frame_size = (BOX_VIEW_W, BOX_VIEW_H)
    avoid = {}
    if len(argv) > 5:
        with open(argv[5], encoding="utf-8") as fh:
            avoid = json.load(fh)
    asked = list(times)          # the caller's frames: the key a later run reuses this reading by
    base_url = os.environ.get("CPA_BASE_URL", "").rstrip("/")
    key = os.environ.get("CPA_API_KEY", "")
    if not base_url or not key:
        raise SystemExit("CPA_BASE_URL / CPA_API_KEY not set (load cpa.env first)")
    os.makedirs(frame_dir, exist_ok=True)
    try:
        action = find_action(cut, frame_dir, base_url, key)
    except (subprocess.CalledProcessError, urllib.error.URLError, OSError, ValueError) as exc:
        action = {"t": None, "why": f"找动作失败：{type(exc).__name__}"}
    if action.get("t") is not None:     # the move's own frame first, then the frames around the cover line
        times = [action["t"], *[t for t in times if abs(t - action["t"]) >= 0.01]]
    checked, chosen, boxes = [], None, None
    first_clean = None           # (t, image) of the first clean frame: taken when no clean frame shows her face
    first_repeat = None          # (t, image, face, dhash) of the first frame showing her face that repeats a cover
    base = {}                    # the chosen frame's face and its hash (the next covers of the stream avoid it)
    for index, t in enumerate(times):
        path = os.path.join(frame_dir, f"frame-{index}.jpg")
        try:
            image = extract(cut, t, path)
            text = ask(base_url, key, image, QUESTION, kind="clean", t=t)
        except (subprocess.CalledProcessError, urllib.error.URLError, OSError, ValueError) as exc:
            checked.append({"t": t, "verdict": "ERROR", "text": type(exc).__name__})
            continue
        verdict = "CLEAN" if "CLEAN" in text.upper() and "DIRTY" not in text.upper() else "NOT_CLEAN"
        row = {"t": t, "verdict": verdict, "text": text[:40]}
        checked.append(row)
        if verdict != "CLEAN":
            continue
        first_clean = first_clean or (t, image)
        if action.get("t") is not None and abs(t - action["t"]) < 0.01:
            row["repeat"] = repeat_of(t, None, avoid)     # her face is not asked here: only the same moment repeats
            if not row["repeat"]:
                chosen, base = t, {"t": t}   # the signature move may hide her face (owner 2026-11-14): not asked
                break
            continue
        seen = look_at_face(row, path, image, t, base_url, key, avoid)
        if seen and not row["repeat"]:
            chosen, base = t, {"t": t, "face": seen[0], "dhash": seen[1]}
            break
        if seen:
            first_repeat = first_repeat or (t, image, *seen)
    if chosen is None:
        chosen, base, image = fallback(first_repeat, first_clean, image)
    if chosen is not None:
        try:
            boxes = read_boxes(ask(base_url, key, image, BOX_QUESTION, max_tokens=400, kind="boxes", t=chosen),
                               frame_size=frame_size)
        except (urllib.error.URLError, OSError, ValueError):
            boxes = None
    with open(out_json, "w", encoding="utf-8") as fh:
        json.dump({"chosen": chosen, "checked": checked, "boxes": boxes, "frame_size": list(frame_size),
                   "calls": CALLS, "times": asked, "question": READING_KEY,
                   "model": CALLS[-1]["model"] if CALLS else MODEL,
                   "action": action, "looked_at": times, "avoid": avoid, "base": base},
                  fh, ensure_ascii=False, indent=1)
    print(f"{out_json}: chosen={chosen} action={action.get('t')} checked={len(checked)} boxes={'yes' if boxes else 'no'}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
