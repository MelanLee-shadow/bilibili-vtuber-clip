#!/usr/bin/env python3
"""read_screen_subtitles.py CUT.mp4 FRAME_DIR FRAMES.json OUT.json

Step 4, a clip with other voices (the streamer watching a video or an animation): read the picture.  For each frame of
FRAMES.json ({"frames": [{"id", "t"}]}, t = seconds in the cut) extract one full-resolution frame and ask the CPA vision
route whether a video someone else made is playing in it, and what its own burned-in subtitle says.  The vision client
is the cover step's (pick_frame.extract / pick_frame.ask: same route, same model, same completion rule); this file only
adds the question and the reading of the answer.  It lives beside pick_frame.py on the media host.

OUT.json = {"question", "model", "frames": [{id, t, ok, video_playing, video_subtitle, subtitle_language, other_text,
error}], "calls": [{kind, t, model, effort, rc, elapsed_s, usage}]}.  A frame that could not be read is ok=false with the
reason; the caller decides what a failed reading means.  Nothing here judges who is speaking.

Runs on OCI3 under the root wrapper that loads cpa.env (CPA_BASE_URL / CPA_API_KEY); the key is never printed.
"""
import json
import os
import re
import subprocess
import sys
import urllib.error
from concurrent.futures import ThreadPoolExecutor

import pick_frame

WORKERS = 4   # same bound as witness_windows.py; the provider's own 429 handling decides the real pace
QUESTION = ("这是直播切片的一帧（1920x1080）。主播有时在直播间里播放别人做的视频或动画（被看视频），被看视频的画面里可能带有它自己的字幕。"
            "请只读画面，回答三件事：\n"
            "1. video_playing：画面里是否正在播放别人做的视频、动画或影片（不是主播自己的形象、摄像头、游戏界面或直播间界面）；\n"
            "2. video_subtitle：被看视频画面里自带的字幕文字，按画面原文逐字抄写，日文写日文、中文写中文，多行用一个空格连接；"
            "没有字幕写空字符串。直播间叠加的弹幕、礼物栏、名牌、主播自己加的字幕、界面按钮上的字都不是被看视频的字幕，不要抄；\n"
            "3. subtitle_language：字幕的语言，写「中文」「日文」「其他」或「无」；\n"
            "另外 other_text 用一句话写出画面上还有哪些别的文字层（例如弹幕、主播字幕），没有写空字符串。\n"
            "只输出一个 JSON 对象：{\"video_playing\": true或false, \"video_subtitle\": \"\", \"subtitle_language\": \"\", "
            "\"other_text\": \"\"}。看不清就如实写空字符串，不要编造。")


def read_answer(text):
    """The fields of the model's JSON, or None when it is not the object asked for."""
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        return None
    try:
        raw = json.loads(match.group(0))
    except ValueError:
        return None
    if not isinstance(raw, dict) or not isinstance(raw.get("video_playing"), bool):
        return None
    return {"video_playing": raw["video_playing"], "video_subtitle": str(raw.get("video_subtitle") or "").strip(),
            "subtitle_language": str(raw.get("subtitle_language") or "").strip(),
            "other_text": str(raw.get("other_text") or "").strip()}


def read_frame(base, key, cut, frame_dir, item):
    row = {"id": item["id"], "t": item["t"], "ok": False}
    path = os.path.join(frame_dir, f"{item['id']}.jpg")
    try:
        image = pick_frame.extract(cut, float(item["t"]), path)
        text = pick_frame.ask(base, key, image, QUESTION, max_tokens=600, kind="subtitle", t=item["t"])
    except (subprocess.CalledProcessError, urllib.error.URLError, OSError, ValueError) as exc:
        row["error"] = type(exc).__name__
        return row
    answer = read_answer(text)
    if answer is None:
        row["error"] = "unparseable"
        return row
    row.update(answer, ok=True)
    return row


def main(argv):
    cut, frame_dir, frames_json, out_json = argv[1:5]
    base = os.environ.get("CPA_BASE_URL", "").rstrip("/")
    key = os.environ.get("CPA_API_KEY", "")
    if not base or not key:
        raise SystemExit("CPA_BASE_URL / CPA_API_KEY not set (load cpa.env first)")
    with open(frames_json, encoding="utf-8") as fh:
        items = json.load(fh)["frames"]
    os.makedirs(frame_dir, exist_ok=True)
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        rows = list(pool.map(lambda item: read_frame(base, key, cut, frame_dir, item), items))   # input order kept
    with open(out_json, "w", encoding="utf-8") as fh:
        json.dump({"question": QUESTION, "model": pick_frame.MODEL, "frames": rows, "calls": pick_frame.CALLS},
                  fh, ensure_ascii=False, indent=1)
    print(f"{out_json}: frames={len(rows)} ok={sum(1 for r in rows if r['ok'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
