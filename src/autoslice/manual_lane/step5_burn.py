"""Step 5: ASS, burn, intro (片头 fail-closed, 维护者).

Split out of stage_impl.py (TASK-13); stage_impl re-exports every name, so callers and tests keep
`stage_impl.<name>`.
"""

from __future__ import annotations

import json
import shlex

from . import (
    gates,
)
from .adapters import remote_python
from .layout import layout
from .runner import Ctx, StepOutcome
from .step_io import REMOTE_FONTS, REMOTE_TOOLS, _fail, _local, _run_remote



def step5_burn(ctx: Ctx) -> StepOutcome:
    rd = ctx.remote_dir
    clip = ctx.state.data["clip"]
    recording_date = ctx.session["recording_date"]
    if clip.get("recording_date", recording_date) != recording_date:
        return _fail("INTRO_CLIP_DATE_MISMATCH", "clip and session recording dates differ")
    identity_args = f"{shlex.quote(recording_date)} {shlex.quote(clip['cid'])}"
    ctx.adapters.remote.put(ctx.clip_dir / "speaker/speaker.srt", f"{rd}/burn/speaker.srt")
    ctx.adapters.remote.put(REMOTE_TOOLS / "render_ass.py", f"{rd}/tools/render_ass.py")
    ctx.adapters.remote.put(REMOTE_TOOLS / "prepend_intro.py", f"{rd}/tools/prepend_intro.py")
    body = (f"mkdir -p {rd}/burn {rd}/tools\n"
            f"{remote_python(f'{rd}/tools/render_ass.py {rd}/burn/speaker.srt {rd}/burn/final.ass')} || exit 11\n"
            f"ffmpeg -hide_banner -loglevel error -y -i {rd}/cut/cut.mp4 "
            f"-vf \"subtitles='{rd}/burn/final.ass':fontsdir='{REMOTE_FONTS}'\" "
            f"-c:v libx264 -preset veryfast -crf 20 -c:a copy -movflags +faststart {rd}/burn/main_burned.mp4 || exit 12\n"
            f"MANUAL_LANE_INTRO_DIR={layout().intro_dir} "
            f"{remote_python(f'{rd}/tools/prepend_intro.py {rd}/burn/main_burned.mp4 {rd}/burn/final.mp4 ')}"
            f"{rd}/burn/intro_work {rd}/burn/intro_result.json {identity_args} || exit 13\n")
    res = _run_remote(ctx, "s5-burn", body, timeout=3600)
    if res.rc != 0:
        return _fail(f"BURN_FAILED_RC{res.rc}", (res.stdout + res.stderr)[-400:])
    local_result = _local(ctx, "burn/intro_result.json")
    ctx.adapters.remote.get(f"{rd}/burn/intro_result.json", local_result)
    intro = json.loads(local_result.read_text(encoding="utf-8"))
    verdict = gates.intro_gate(intro)
    if not verdict.ok:
        return _fail(verdict.reason_code, verdict.detail)
    return StepOutcome(True, products={"intro_result": str(local_result)},
                       note=f"intro={verdict.evidence.get('intro_id')} offset={verdict.evidence.get('intro_offset_ms')}ms "
                            f"OCI3 {rd}/burn/final.mp4",
                       process=[f"片头前置：{verdict.evidence.get('intro_id')}，偏移 {verdict.evidence.get('intro_offset_ms')} ms"])
