#!/usr/bin/env python3
"""read_banner.py CUT.mp4 OUT_DIR T_SECONDS

Reads the host's own fixed text bar ("正在：某某") from one frame of the cut.  The frame at T_SECONDS is extracted
(width scaled to 1280), saved as OUT_DIR/banner.jpg and put to the CPA vision route once with the question in
screen_banner.md (the file beside this tool).  OUT_DIR/banner.json = {"t", "answer", "model", "usage", "elapsed_s",
"rc"}; "answer" is the model's raw text.  Any failure ends non-zero with the reason on stderr; nothing is retried.

Runs on OCI3 beside pick_frame.py (its model, request and completion check are used as they are) under the root wrapper
that loads cpa.env (CPA_BASE_URL / CPA_API_KEY); the key is never printed.
"""
import base64
import json
import os
import subprocess
import sys

import pick_frame

QUESTION_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "screen_banner.md")
FRAME_WIDTH = 1280
MAX_TOKENS = 200


def frame(cut, t, path):
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{t:.3f}", "-i", cut,
                    "-frames:v", "1", "-vf", f"scale={FRAME_WIDTH}:-2", "-q:v", "3", path], check=True)
    with open(path, "rb") as fh:
        return base64.b64encode(fh.read()).decode("ascii")


def main(argv):
    cut, out_dir, t = argv[1], argv[2], float(argv[3])
    base = os.environ.get("CPA_BASE_URL", "").rstrip("/")
    key = os.environ.get("CPA_API_KEY", "")
    if not base or not key:
        raise SystemExit("CPA_BASE_URL / CPA_API_KEY not set (load cpa.env first)")
    with open(QUESTION_FILE, encoding="utf-8") as fh:
        question = fh.read().strip()
    os.makedirs(out_dir, exist_ok=True)
    try:
        image = frame(cut, t, os.path.join(out_dir, "banner.jpg"))
        answer = pick_frame.ask(base, key, image, question, max_tokens=MAX_TOKENS, kind="banner", t=t)
    except Exception as exc:  # noqa: BLE001 - any failure is the tool's failure: reason on stderr, non-zero exit
        raise SystemExit(f"read_banner failed: {type(exc).__name__}: {str(exc)[:300]}") from exc
    call = pick_frame.CALLS[-1]
    with open(os.path.join(out_dir, "banner.json"), "w", encoding="utf-8") as fh:
        json.dump({"t": t, "answer": answer, "model": call["model"], "usage": call.get("usage"),
                   "elapsed_s": call.get("elapsed_s"), "rc": call["rc"]}, fh, ensure_ascii=False, indent=1)
    print(f"{out_dir}/banner.json: {len(answer)} chars")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
