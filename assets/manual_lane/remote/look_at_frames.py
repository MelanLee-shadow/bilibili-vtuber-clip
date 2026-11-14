#!/usr/bin/env python3
"""look_at_frames.py CUT.mp4 FRAME_DIR FRAMES.json OUT.json

Step 3, the subtitle agent's `frame` tool (src/autoslice/manual_lane/agent_frame.py): look at the picture at a few
moments of the cut and answer the agent's one question about it (what she wears or holds, what a screen or a sign
says, what is being watched).  FRAMES.json = {"question", "frames": [{"id", "t"}]}, t = seconds in the cut.  The
vision client is the cover step's (pick_frame.extract / pick_frame.ask: same route, same model, same completion rule);
this file only wraps the question and reads the answer.  It lives beside pick_frame.py on the media host.

OUT.json = {"question", "model", "frames": [{id, t, ok, seen, answer, error}], "calls": [...]}.  `seen` false means
the picture does not show what was asked: the answer is then no evidence either way.

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

WORKERS = 3
WRAP = ("这是直播切片的一帧（1920x1080）。请只根据这张画面回答下面的问题。看得见什么就如实写什么，"
        "画面上的文字逐字照抄；画面里看不出、看不清的就说看不出，不要按常理或问题的暗示去猜。\n"
        "问题：{question}\n"
        "只输出一个 JSON 对象：{{\"seen\": true或false（画面上能否看出问题问的东西）, \"answer\": \"一两句话的回答\"}}。")


def read_answer(text):
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        return None
    try:
        raw = json.loads(match.group(0))
    except ValueError:
        return None
    if not isinstance(raw, dict) or not isinstance(raw.get("seen"), bool):
        return None
    return {"seen": raw["seen"], "answer": str(raw.get("answer") or "").strip()}


def look(base, key, cut, frame_dir, question, item):
    row = {"id": item["id"], "t": item["t"], "ok": False}
    path = os.path.join(frame_dir, f"{item['id']}.jpg")
    try:
        image = pick_frame.extract(cut, float(item["t"]), path)
        text = pick_frame.ask(base, key, image, WRAP.format(question=question), max_tokens=400, kind="look", t=item["t"])
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
        doc = json.load(fh)
    os.makedirs(frame_dir, exist_ok=True)
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        rows = list(pool.map(lambda item: look(base, key, cut, frame_dir, doc["question"], item), doc["frames"]))
    with open(out_json, "w", encoding="utf-8") as fh:
        json.dump({"question": doc["question"], "model": pick_frame.MODEL, "frames": rows, "calls": pick_frame.CALLS},
                  fh, ensure_ascii=False, indent=1)
    print(f"{out_json}: frames={len(rows)} ok={sum(1 for r in rows if r['ok'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
