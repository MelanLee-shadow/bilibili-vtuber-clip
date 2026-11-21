#!/usr/bin/env python3
"""cover_thumb.py JOB.json

Step 6 thumbnail check (70-cover; the approved batch was looked at at 180 px wide): the chosen candidate's final picture is
shrunk to the phone-list width and a CPA vision reading says whether the host's face is still recognisable and whether the
punchline is still legible.  A no sends a free candidate away, so the reading goes through veto.confirm: asked again
after a no, an axis fails only when most readings fail it (one reading must not turn a usable screenshot into a paid
picture).  JOB: out (the candidates directory), id (the candidate),
width, question, judge_model.  Writes OUT/<id>.thumb.png and OUT/<id>.thumb.result.json {answer, verdict, readings, calls};
runs under the root wrapper that loads cpa.env, like cover_candidates.py (its vision client is reused, with its cache keyed by the thumbnail's bytes)."""
import json
import os
import re
import sys

from PIL import Image

import veto
from cover_candidates import CALLS, vision

AXES = ("face_recognizable", "punch_legible")


def parse(answer):
    """The JSON object of one answer, or None."""
    match = re.search(r"\{.*\}", answer, re.DOTALL)
    try:
        verdict = json.loads(match.group(0)) if match else None
    except ValueError:
        return None
    return verdict if isinstance(verdict, dict) else None


def axes_of(verdict):
    """{axis: bool} of one parsed answer, or None when an axis is missing or not a boolean."""
    if not isinstance(verdict, dict) or not all(
            isinstance(verdict.get(a), dict) and isinstance(verdict[a].get("pass"), bool) for a in AXES):
        return None
    return {a: verdict[a]["pass"] for a in AXES}


def main(job_path):
    with open(job_path, encoding="utf-8") as fh:
        job = json.load(fh)
    out, cid, width = job["out"], job["id"], int(job["width"])
    thumb = os.path.join(out, f"{cid}.thumb.png")
    with Image.open(os.path.join(out, f"{cid}.png")) as im:
        im.convert("RGB").resize((width, round(im.height * width / im.width)), Image.LANCZOS).save(thumb)

    def read(n):                             # each asking under its own cache name
        return vision(job, f"{cid}.thumb" + ("" if n == 1 else f".{n}"), job["question"], [(thumb, width)], out)

    taken, settled = veto.confirm(read, lambda got: axes_of(parse(got[0])))
    readings = [{"answer": t["answer"][0] if t["answer"] else None, "verdict": parse(t["answer"][0]) if t["answer"] else None,
                 "cached": bool(t["answer"] and t["answer"][1]), **({"error": t["error"]} if "error" in t else {})}
                for t in taken]
    verdict = readings[0]["verdict"] if len(readings) == 1 else None
    if settled is not None and len(readings) > 1:
        verdict = {a: {"pass": v["pass"], "reason": f"{readings[v['from']]['verdict'][a].get('reason')}"
                                                    f"（{v['judged']} 次判定里 {v['fails']} 次不过）"}
                   for a, v in settled.items()}
    with open(os.path.join(out, f"{cid}.thumb.result.json"), "w", encoding="utf-8") as fh:
        json.dump({"answer": readings[0]["answer"], "verdict": verdict, "cached": readings[0]["cached"],
                   "readings": readings, "calls": CALLS}, fh, ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main(sys.argv[1])
