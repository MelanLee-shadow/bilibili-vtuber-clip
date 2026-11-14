#!/usr/bin/env python3
"""cover_thumb.py JOB.json

Step 6 thumbnail check (70-cover; the approved batch was looked at at 180 px wide): the chosen candidate's final picture is
shrunk to the phone-list width and one CPA vision reading says whether the host's face is still recognisable and whether the
punchline is still legible.  JOB: out (the candidates directory), id (the candidate), width, question, judge_model.
Writes OUT/<id>.thumb.png and OUT/<id>.thumb.result.json {answer, verdict, calls}; runs under the root wrapper that loads
cpa.env, like cover_candidates.py (its vision client is reused, with its cache keyed by the thumbnail's bytes)."""
import json
import os
import re
import sys

from PIL import Image

from cover_candidates import CALLS, vision


def main(job_path):
    with open(job_path, encoding="utf-8") as fh:
        job = json.load(fh)
    out, cid, width = job["out"], job["id"], int(job["width"])
    thumb = os.path.join(out, f"{cid}.thumb.png")
    with Image.open(os.path.join(out, f"{cid}.png")) as im:
        im.convert("RGB").resize((width, round(im.height * width / im.width)), Image.LANCZOS).save(thumb)
    answer, cached = vision(job, f"{cid}.thumb", job["question"], [(thumb, width)], out)
    match = re.search(r"\{.*\}", answer, re.DOTALL)
    try:
        verdict = json.loads(match.group(0)) if match else None
    except ValueError:
        verdict = None
    with open(os.path.join(out, f"{cid}.thumb.result.json"), "w", encoding="utf-8") as fh:
        json.dump({"answer": answer, "verdict": verdict, "cached": cached, "calls": CALLS}, fh, ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main(sys.argv[1])
