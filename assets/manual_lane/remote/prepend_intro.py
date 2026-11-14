#!/usr/bin/env python3
"""Prepend the registered intro using the recording date and candidate identity."""
import json
import os
import shutil
import sys
from pathlib import Path

sys.dont_write_bytecode = True
REPO_ENV = "MANUAL_LANE_REMOTE_REPO"   # the deployed repo, set by the lane (adapters.remote_python)
sys.path.insert(0, os.environ[REPO_ENV])
from src.autoslice import branding_intro as bi

INTRO_DIR = Path(os.environ["MANUAL_LANE_INTRO_DIR"])
src, out, work, res_path = Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3]), Path(sys.argv[4])
ctx = bi.require_branding_intro(
    Path(os.environ[REPO_ENV]), media_dir=INTRO_DIR,
    clip_identity={"recording_date": sys.argv[5], "candidate_id": sys.argv[6]},
)
if ctx is None:
    raise bi.BrandingIntroError("manual lane requires an enabled branding intro")
# A successor of a published manuscript keeps the intro its published version carries, whatever the rotation would pick today
# (time-weighted); a pin that is not a rotation member now is left out and the rotation picks.
pin = os.environ.get("MANUAL_LANE_PIN_INTRO", "")
if pin and pin in [c["intro_id"] for c in ctx["candidates"]]:
    os.environ[bi.BRANDING_INTRO_PICK_ENV] = pin
work.mkdir(parents=True, exist_ok=True)
tmp = work / "main_for_intro.mp4"
shutil.copyfile(src, tmp)
res = bi.prepend_branding_intro(context=ctx, main_path=tmp, work_dir=work / "tmp")
shutil.move(tmp, out)
res = json.loads(json.dumps(res, default=str))
res_path.write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
print(json.dumps({k: res.get(k) for k in ("status", "intro_id", "method", "intro_offset_ms")}, ensure_ascii=False))
