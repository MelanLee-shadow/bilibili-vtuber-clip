#!/usr/bin/env python3
"""pick_frame.py CUT.mp4 FRAME_DIR OUT.json T1,T2,...

Cover frame choice (skill 7, and the 692 review).  For each candidate time, extract one full-resolution frame (the cut
is 1920x1080, so boxes read from it are in that space) and ask the CPA vision route whether the picture shows an
unrelated page or UI.  The first frame answered CLEAN is chosen.  Then the same frame is read once more for boxes:
the danmaku box and gift bar (to blur), the host's body (to keep clear of the blur, and the reference of the cover's
generated layers) and the guest's body (the guest's layer of a composed cover).  The host is identified from the
frame and nameplate, allowing the headwear actually shown instead of assuming her ears are visible.

Before that, the whole cut is looked at for the host's signature move (owner 2026-11-14: a clip that has a classic
action, such as squatting down holding her head when shy, takes its screenshot there): frames every ACTION_STEP_S
seconds are tiled into numbered sheets and each sheet is asked once; when several sheets have one, one more question
picks among them.  That frame is the first candidate.

OUT.json = {"chosen": seconds or null, "action": {"t", "what"} or {"t": null, "why"}, "checked": [{t, verdict, text}], "calls": [{kind, t, model, rc, elapsed_s, usage}], "boxes": {"danmaku": [[x0,y0,x1,y1]...],
"person": [[...]...], "guest": [[...]...]} or null}.  A box read that fails leaves boxes null; the caller then uses the fixed layout.

Runs on OCI3 under the root wrapper that loads cpa.env (CPA_BASE_URL / CPA_API_KEY); the key is never printed.
"""
import base64
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request

MODEL = "gpt-6.1-sol"


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
BOX_QUESTION = ("这是直播画面（1920x1080 像素）。请给出三类矩形，坐标是整幅画面的像素，格式 [x0,y0,x1,y1]：\n"
                "danmaku：直播间右侧的弹幕框和礼物栏（聊天、礼物、弹幕区域）；\n"
                "person：主播" + HOST + "本人的身体范围" + (BODY_HINT or "，依据名牌和画面辨认") + "；\n"
                "guest：画面里另一位主播（不是" + HOST + "）的身体范围。\n"
                "只输出一个 JSON 对象：{\"danmaku\": [[x0,y0,x1,y1]], \"person\": [[x0,y0,x1,y1]], \"guest\": [[x0,y0,x1,y1]]}；"
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
                 "或者只有角落里很小的头像、图标、表情贴图，都算看不清。看得清只回答 FACE，看不清只回答 NOFACE。只回答这一个词。")
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


# Every box of the cover is read and placed in 1920x1080 (the questions say so, cover_face_crop.FRAME_W/H): a source of
# another size is scaled into it (goldfish 4, a 1280x720 stream: every screenshot crop fell outside the frame)
FRAME_SIZE_FILTER = "scale=1920:1080:force_original_aspect_ratio=decrease,pad=1920:1080:(ow-iw)/2:(oh-ih)/2"


def extract(cut, t, path):
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{t:.3f}", "-i", cut,
                    "-frames:v", "1", "-vf", FRAME_SIZE_FILTER, "-q:v", "3", path], check=True)
    with open(path, "rb") as fh:
        return base64.b64encode(fh.read()).decode("ascii")


CALLS = []   # every paid vision request made, for the step's cost record (OUT.json "calls")


def ask(base, key, image_b64, question, max_tokens=64, kind="clean", t=None):
    body = {"model": MODEL, "reasoning": {"effort": "low"}, "max_output_tokens": max_tokens,
            "input": [{"role": "user", "content": [
                {"type": "input_text", "text": question},
                {"type": "input_image", "image_url": "data:image/jpeg;base64," + image_b64}]}]}
    req = urllib.request.Request(f"{base}/responses", data=json.dumps(body).encode(), method="POST", headers={
        "Authorization": f"Bearer {key}", "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"})
    call = {"kind": kind, "t": t, "model": MODEL, "effort": "low", "rc": 1}
    CALLS.append(call)
    started = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=150) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    finally:
        call["elapsed_s"] = round(time.monotonic() - started, 1)
    call["usage"] = payload.get("usage") or {}
    text = payload.get("output_text") or ""
    if not text:
        for item in payload.get("output") or []:
            if item.get("type") == "message":
                text += "".join(c.get("text", "") for c in item.get("content") or [])
    # TASK-26: a reading counts only when the provider says it completed (same rule as cpa_text.completion_of);
    # anything else is a failed call (the caller records ERROR), never a NOT_CLEAN verdict made of half an answer
    items = [i.get("status") for i in payload.get("output") or [] if i.get("type") == "message"]
    if (payload.get("status") != "completed" or payload.get("incomplete_details") or payload.get("error")
            or any(s != "completed" for s in items) or not text.strip()):
        call["incomplete"] = {"status": payload.get("status"), "incomplete_details": payload.get("incomplete_details")}
        raise ValueError(f"CPA vision answer not complete: status={payload.get('status')}")
    call["rc"] = 0
    return text.strip()


def read_boxes(text):
    """{'danmaku': [...], 'person': [...]} from the model's JSON, keeping only four-number boxes in the frame."""
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        return None
    try:
        raw = json.loads(match.group(0))
    except ValueError:
        return None
    out = {}
    for key in ("danmaku", "person", "guest"):
        boxes = []
        for box in raw.get(key) or []:
            if isinstance(box, list) and len(box) == 4 and all(isinstance(v, (int, float)) for v in box):
                x0, y0, x1, y1 = (int(v) for v in box)
                if 0 <= x0 < x1 <= 1920 and 0 <= y0 < y1 <= 1080:
                    boxes.append([x0, y0, x1, y1])
        out[key] = boxes
    return out


def main(argv):
    cut, frame_dir, out_json, times = argv[1], argv[2], argv[3], [float(x) for x in argv[4].split(",") if x]
    asked = list(times)          # the caller's frames: the key a later run reuses this reading by
    base = os.environ.get("CPA_BASE_URL", "").rstrip("/")
    key = os.environ.get("CPA_API_KEY", "")
    if not base or not key:
        raise SystemExit("CPA_BASE_URL / CPA_API_KEY not set (load cpa.env first)")
    os.makedirs(frame_dir, exist_ok=True)
    try:
        action = find_action(cut, frame_dir, base, key)
    except (subprocess.CalledProcessError, urllib.error.URLError, OSError, ValueError) as exc:
        action = {"t": None, "why": f"找动作失败：{type(exc).__name__}"}
    if action.get("t") is not None:     # the move's own frame first, then the frames around the cover line
        times = [action["t"], *[t for t in times if abs(t - action["t"]) >= 0.01]]
    checked, chosen, boxes = [], None, None
    first_clean = None           # (t, image) of the first clean frame: taken when no clean frame shows her face
    for index, t in enumerate(times):
        path = os.path.join(frame_dir, f"frame-{index}.jpg")
        try:
            image = extract(cut, t, path)
            text = ask(base, key, image, QUESTION, kind="clean", t=t)
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
            chosen = t           # the signature move may hide her face (owner 2026-11-14): its frame is not asked
            break
        try:                     # 1645880 (2026-11-15): the cover line fell where the camera showed only her shoes
            face = ask(base, key, image, FACE_QUESTION, kind="face", t=t).upper()
        except (urllib.error.URLError, OSError, ValueError) as exc:
            row["face"] = f"ERROR {type(exc).__name__}"
            continue
        row["face"] = "FACE" if "FACE" in face and "NOFACE" not in face else "NOFACE"
        if row["face"] == "FACE":
            chosen = t
            break
    if chosen is None and first_clean is not None:
        chosen = first_clean[0]  # no clean frame shows her face: the first clean one, as before (recorded below)
    if chosen is not None:
        image = first_clean[1] if first_clean and first_clean[0] == chosen else image
        try:
            boxes = read_boxes(ask(base, key, image, BOX_QUESTION, max_tokens=300, kind="boxes", t=chosen))
        except (urllib.error.URLError, OSError, ValueError):
            boxes = None
    with open(out_json, "w", encoding="utf-8") as fh:
        json.dump({"chosen": chosen, "checked": checked, "boxes": boxes, "calls": CALLS, "times": asked, "question": READING_KEY,
                   "action": action, "looked_at": times},
                  fh, ensure_ascii=False, indent=1)
    print(f"{out_json}: chosen={chosen} action={action.get('t')} checked={len(checked)} boxes={'yes' if boxes else 'no'}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
