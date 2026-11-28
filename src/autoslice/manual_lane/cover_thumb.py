"""Step 6 cover: the thumbnail check (70-cover; the approved batch was looked at at 180 px wide).

The chosen picture is shrunk to `cover.thumb_width_px` on the media host and a CPA vision reading says whether the host's
face is still recognisable and whether the punchline is still legible.  A reading that says no is asked twice more there
and an axis fails only when most readings fail it (the tool's `settle`): one reading of a borderline picture must not
send a usable free candidate away.  A tool or answer problem is `UNJUDGED` (recorded, never blocking)."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, TypedDict, cast

from .adapters import remote_python, root_script
from .step_io import REMOTE_TOOLS, _local, _render, _run_remote, raise_if_vision_unavailable, vision_models

if TYPE_CHECKING:
    from .runner import Ctx
    from .state import CpaEvent

THUMB_TOOL = REMOTE_TOOLS / "cover_thumb.py"


class ThumbVerdict(TypedDict, total=False):
    status: str                    # PASS | FAIL | UNJUDGED
    face_recognizable: bool | None
    punch_legible: bool | None
    reason: str
    width_px: int
    elapsed_s: float | None
    readings: int


def read_thumb(result: dict[str, object] | None, rc: int, width: int) -> ThumbVerdict:
    """The verdict of one thumb.result.json: both readings must be present and boolean to be judged."""
    verdict = (result or {}).get("verdict")
    out: ThumbVerdict = {"status": "UNJUDGED", "face_recognizable": None, "punch_legible": None, "width_px": width}
    if rc != 0 or not isinstance(verdict, dict):
        out["reason"] = f"缩略图判定没有得到可读的答案（rc={rc}）"
        return out
    axes = {k: verdict.get(k) for k in ("face_recognizable", "punch_legible")}
    if not all(isinstance(a, dict) and isinstance(a.get("pass"), bool) for a in axes.values()):
        out["reason"] = "缩略图判定的答案缺项或不是布尔值"
        return out
    face, punch = (cast("dict[str, object]", axes[k]) for k in ("face_recognizable", "punch_legible"))
    out.update(face_recognizable=cast(bool, face["pass"]), punch_legible=cast(bool, punch["pass"]),
               status="PASS" if face["pass"] and punch["pass"] else "FAIL",
               reason=f"脸：{face.get('reason')}；笑点字：{punch.get('reason')}")
    return out


def check_thumb(ctx: Ctx, cid: str, *, width: int, lines: list[str], identity: str) -> tuple[ThumbVerdict, list[CpaEvent]]:
    """Shrink candidate `cid` on the media host and read it.  Returns the verdict and one cpa record per reading."""
    rd = ctx.remote_dir
    vision_model, fb_model, fb_effort = vision_models()
    job = {"out": f"{rd}/cover/cand", "id": cid, "width": width, "judge_model": vision_model,
           "fallback_model": fb_model, "fallback_effort": fb_effort,
           "question": _render("cover_thumb.md", lines=" / ".join(lines), identity=identity)}
    local_job = _local(ctx, f"cover/thumb-{cid}.job.json")
    local_job.write_text(json.dumps(job, ensure_ascii=False, indent=2), encoding="utf-8")
    ctx.adapters.remote.put(THUMB_TOOL, f"{rd}/cover/cover_thumb.py")
    ctx.adapters.remote.put(local_job, f"{rd}/cover/thumb-{cid}.job.json")
    res = _run_remote(ctx, f"s6-cover-thumb-{cid}", root_script(
        remote_python(f"{rd}/cover/cover_thumb.py {rd}/cover/thumb-{cid}.job.json"), env=["cpa"],
        owned=f"{rd}/cover", recursive=True), timeout=900)
    raise_if_vision_unavailable(res.stdout + "\n" + res.stderr)
    result: dict[str, object] | None = None
    try:
        path = _local(ctx, f"cover/cand/{cid}.thumb.result.json")
        ctx.adapters.remote.get(f"{rd}/cover/cand/{cid}.thumb.result.json", path)
        result = json.loads(path.read_text(encoding="utf-8"))
        ctx.adapters.remote.get(f"{rd}/cover/cand/{cid}.thumb.png", _local(ctx, f"cover/cand/{cid}.thumb.png"))
    except (RuntimeError, OSError, ValueError):
        pass
    verdict = read_thumb(result, res.rc, width)
    calls = cast("list[dict[str, object]]", (result or {}).get("calls") or [])
    if fb_notes := list(dict.fromkeys(str(c["fallback_note"]) for c in calls if c.get("fallback_note"))):
        verdict["reason"] = f"{verdict.get('reason', '')}（{'；'.join(fb_notes)}）".lstrip("；")
    spent = [cast(float, c["elapsed_s"]) for c in calls if isinstance(c.get("elapsed_s"), (int, float))]
    verdict["elapsed_s"] = round(sum(spent), 1) if spent else None
    verdict["readings"] = len(cast("list[object]", (result or {}).get("readings") or [])) or len(calls)
    records = [cast("CpaEvent", {"call_id": f"cover-thumb-{cid}" + (f"-{n}" if n > 1 else ""), "kind": "vision",
                                 "model": c.get("model") or vision_model, "rc": c.get("rc", res.rc),
                                 "elapsed_s": c.get("elapsed_s"), "usage": c.get("usage"),
                                 **({k: c[k] for k in ("fallback_from", "fallback_reason", "fallback_note") if k in c})})
               for n, c in enumerate(calls, 1)]
    return verdict, records or [cast("CpaEvent", {"call_id": f"cover-thumb-{cid}", "kind": "vision", "model": vision_model,
                                                  "rc": res.rc, "elapsed_s": None, "usage": None})]
