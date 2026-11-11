#!/usr/bin/env python3
"""pick_frame.py CUT.mp4 FRAME_DIR OUT.json T1,T2,...

Cover frame choice (skill 7, and the 692 review).  For each candidate time, extract one full-resolution frame (the cut
is 1920x1080, so boxes read from it are in that space) and ask the CPA vision route whether the picture shows an
unrelated page or UI.  The first frame answered CLEAN is chosen.  Then the same frame is read once more for boxes:
the danmaku box and gift bar (to blur), the host's body (to keep clear of the blur, and the reference of the cover's
generated layers) and the guest's body (the guest's layer of a composed cover).  The host is identified from the
frame and nameplate, allowing the headwear actually shown instead of assuming her ears are visible.

OUT.json = {"chosen": seconds or null, "checked": [{t, verdict, text}], "calls": [{kind, t, model, rc, elapsed_s, usage}], "boxes": {"danmaku": [[x0,y0,x1,y1]...],
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
QUESTION = ("这是直播切片的一帧（1920x1080）。画面里是否出现与本段直播内容无关的页面、测试页或界面（例如和直播无关的网页、"
            "测试用的页面标题、系统界面）？如果有，只回答 DIRTY；如果画面干净，只回答 CLEAN。只回答这一个词。")
BOX_QUESTION = ("这是直播画面（1920x1080 像素）。请给出三类矩形，坐标是整幅画面的像素，格式 [x0,y0,x1,y1]：\n"
                "danmaku：直播间右侧的弹幕框和礼物栏（聊天、礼物、弹幕区域）；\n"
                "person：主播李豆沙本人的身体范围（白发，造型以当场画面为准），依据名牌和画面辨认；她可能戴帽子或眼罩，不能以熊猫耳不可见就认作别人；\n"
                "guest：画面里另一位主播（不是李豆沙）的身体范围。\n"
                "只输出一个 JSON 对象：{\"danmaku\": [[x0,y0,x1,y1]], \"person\": [[x0,y0,x1,y1]], \"guest\": [[x0,y0,x1,y1]]}；"
                "找不到的类别写空数组，不要编造。")


def extract(cut, t, path):
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{t:.3f}", "-i", cut,
                    "-frames:v", "1", "-q:v", "3", path], check=True)
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
    base = os.environ.get("CPA_BASE_URL", "").rstrip("/")
    key = os.environ.get("CPA_API_KEY", "")
    if not base or not key:
        raise SystemExit("CPA_BASE_URL / CPA_API_KEY not set (load cpa.env first)")
    os.makedirs(frame_dir, exist_ok=True)
    checked, chosen, boxes = [], None, None
    for index, t in enumerate(times):
        path = os.path.join(frame_dir, f"frame-{index}.jpg")
        try:
            image = extract(cut, t, path)
            text = ask(base, key, image, QUESTION, kind="clean", t=t)
        except (subprocess.CalledProcessError, urllib.error.URLError, OSError, ValueError) as exc:
            checked.append({"t": t, "verdict": "ERROR", "text": type(exc).__name__})
            continue
        verdict = "CLEAN" if "CLEAN" in text.upper() and "DIRTY" not in text.upper() else "NOT_CLEAN"
        checked.append({"t": t, "verdict": verdict, "text": text[:40]})
        if verdict == "CLEAN":
            chosen = t
            try:
                boxes = read_boxes(ask(base, key, image, BOX_QUESTION, max_tokens=300, kind="boxes", t=t))
            except (urllib.error.URLError, OSError, ValueError):
                boxes = None
            break
    with open(out_json, "w", encoding="utf-8") as fh:
        json.dump({"chosen": chosen, "checked": checked, "boxes": boxes, "calls": CALLS, "times": times, "question": BOX_QUESTION},
                  fh, ensure_ascii=False, indent=1)
    print(f"{out_json}: chosen={chosen} checked={len(checked)} boxes={'yes' if boxes else 'no'}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
