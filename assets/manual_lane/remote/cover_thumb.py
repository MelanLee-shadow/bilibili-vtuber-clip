#!/usr/bin/env python3
"""cover_thumb.py JOB.json

Step 6 thumbnail check (70-cover; the approved batch was looked at at 180 px wide): the chosen candidate's final picture is
shrunk to the phone-list width and a CPA vision reading says whether the host's face is still recognisable and whether the
punchline is still legible.  One reading of a borderline picture can say no where the next says yes, so a reading that
says no is asked twice more and an axis fails only when most of the readings fail it (free candidates first: one
reading must not turn a usable screenshot into a paid picture).  JOB: out (the candidates directory), id (the candidate),
width, question, judge_model.  Writes OUT/<id>.thumb.png and OUT/<id>.thumb.result.json {answer, verdict, readings, calls};
runs under the root wrapper that loads cpa.env, like cover_candidates.py (its vision client is reused, with its cache keyed by the thumbnail's bytes)."""
import json
import os
import re
import sys
import urllib.error

from PIL import Image

from cover_candidates import CALLS, vision

AXES = ("face_recognizable", "punch_legible")
READINGS = 3   # asked in all once the first reading says no; an axis fails when at least two of them fail it


def parse(answer):
    """The JSON object of one answer, or None."""
    match = re.search(r"\{.*\}", answer, re.DOTALL)
    try:
        verdict = json.loads(match.group(0)) if match else None
    except ValueError:
        return None
    return verdict if isinstance(verdict, dict) else None


def judged(verdict):
    return isinstance(verdict, dict) and all(
        isinstance(verdict.get(a), dict) and isinstance(verdict[a].get("pass"), bool) for a in AXES)


def says_no(verdict):
    return judged(verdict) and not all(verdict[a]["pass"] for a in AXES)


def settle(verdicts):
    """One verdict from the readings taken.  A single reading stands as it is; after a first no, each axis goes with
    most of the judged readings, and fewer than two judged readings is no verdict (never a no on one reading)."""
    if len(verdicts) == 1:
        return verdicts[0]
    seen = [v for v in verdicts if judged(v)]
    if len(seen) < 2:
        return None
    merged = {}
    for axis in AXES:
        fails = sum(1 for v in seen if not v[axis]["pass"])
        ok = fails * 2 <= len(seen)
        why = next(v[axis].get("reason") for v in seen if v[axis]["pass"] is ok)
        merged[axis] = {"pass": ok, "reason": f"{why}（{len(seen)} 次判定里 {fails} 次不过）"}
    return merged


def main(job_path):
    with open(job_path, encoding="utf-8") as fh:
        job = json.load(fh)
    out, cid, width = job["out"], job["id"], int(job["width"])
    thumb = os.path.join(out, f"{cid}.thumb.png")
    with Image.open(os.path.join(out, f"{cid}.png")) as im:
        im.convert("RGB").resize((width, round(im.height * width / im.width)), Image.LANCZOS).save(thumb)
    answer, cached = vision(job, f"{cid}.thumb", job["question"], [(thumb, width)], out)
    readings = [{"answer": answer, "verdict": parse(answer), "cached": cached}]
    if says_no(readings[0]["verdict"]):
        for n in range(2, READINGS + 1):     # its own cache name: the same question on the same picture, asked again
            try:
                again, reused = vision(job, f"{cid}.thumb.{n}", job["question"], [(thumb, width)], out)
            except (ValueError, OSError, urllib.error.URLError) as exc:
                readings.append({"answer": None, "verdict": None, "cached": False, "error": f"{type(exc).__name__}: {exc}"})
                continue
            readings.append({"answer": again, "verdict": parse(again), "cached": reused})
    with open(os.path.join(out, f"{cid}.thumb.result.json"), "w", encoding="utf-8") as fh:
        json.dump({"answer": answer, "verdict": settle([r["verdict"] for r in readings]), "cached": cached,
                   "readings": readings, "calls": CALLS}, fh, ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main(sys.argv[1])
