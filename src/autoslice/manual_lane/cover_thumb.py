"""Step 6 cover: the thumbnail check (70-cover; the approved batch was looked at at 180 px wide).

The chosen picture is shrunk to `cover.thumb_width_px` on the media host and one CPA vision reading says whether the host's
face is still recognisable and whether the punchline is still legible.  A tool or answer problem is `UNJUDGED` (recorded,
never blocking); only a reading that says no is `FAIL`."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, TypedDict, cast

from .adapters import CPA_JUDGE_MODEL, remote_python, root_script
from .step_io import REMOTE_TOOLS, _local, _render, _run_remote

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
    """Shrink candidate `cid` on the media host and read it once.  Returns the verdict and the call's cpa record."""
    rd = ctx.remote_dir
    job = {"out": f"{rd}/cover/cand", "id": cid, "width": width, "judge_model": CPA_JUDGE_MODEL,
           "question": _render("cover_thumb.md", lines=" / ".join(lines), identity=identity)}
    local_job = _local(ctx, f"cover/thumb-{cid}.job.json")
    local_job.write_text(json.dumps(job, ensure_ascii=False, indent=2), encoding="utf-8")
    ctx.adapters.remote.put(THUMB_TOOL, f"{rd}/cover/cover_thumb.py")
    ctx.adapters.remote.put(local_job, f"{rd}/cover/thumb-{cid}.job.json")
    res = _run_remote(ctx, f"s6-cover-thumb-{cid}", root_script(
        remote_python(f"{rd}/cover/cover_thumb.py {rd}/cover/thumb-{cid}.job.json"), env=["cpa"],
        owned=f"{rd}/cover", recursive=True), timeout=900)
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
    took = calls[-1].get("elapsed_s") if calls else None
    verdict["elapsed_s"] = cast("float | None", took)
    record = cast("CpaEvent", {"call_id": f"cover-thumb-{cid}", "kind": "vision", "model": CPA_JUDGE_MODEL, "rc": res.rc,
                               "elapsed_s": took, "usage": (calls[-1].get("usage") if calls else None)})
    return verdict, [record]
