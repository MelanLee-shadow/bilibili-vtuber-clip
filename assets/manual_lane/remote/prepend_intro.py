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
    # A channel whose own intro manifest says `enabled: false` has no intro: the clip goes on without one.  Anything
    # else that leaves no intro (the manifest missing, the emergency switch) stays an error.
    manifest = Path(os.environ[REPO_ENV]) / bi.BRANDING_INTRO_MANIFEST_RELPATH
    try:
        disabled = json.loads(manifest.read_text(encoding="utf-8")).get("enabled") is False
    except (OSError, ValueError):
        disabled = False
    if not disabled or os.environ.get(bi.BRANDING_INTRO_ENV_SWITCH, "").strip():
        raise bi.BrandingIntroError("manual lane requires an enabled branding intro")
    shutil.copyfile(src, out)
    res = {"status": "NO_INTRO", "reason": f"{bi.BRANDING_INTRO_MANIFEST_RELPATH}: enabled is false", "intro_id": None,
           "intro_offset_ms": 0}
    res_path.write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(res, ensure_ascii=False))
    raise SystemExit(0)
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
