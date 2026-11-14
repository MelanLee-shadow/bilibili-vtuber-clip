"""Ask the existing vision reader to choose when all rendered covers have code vetoes."""
from __future__ import annotations

import json
from collections.abc import Sequence
from typing import TYPE_CHECKING, TypedDict, cast

from .adapters import remote_python, root_script
from .step_io import REMOTE_TOOLS, _local, _run_remote

if TYPE_CHECKING:
    from .runner import Ctx
    from .step6_cover import CoverCandidate, CoverCandidateCall


class FallbackChoice(TypedDict):
    chosen: str
    reason: str


class FallbackResult(TypedDict):
    choice: FallbackChoice
    calls: list[CoverCandidateCall]


def choose_vetoed(ctx: Ctx, rows: Sequence[CoverCandidate], *, title: str, lines: list[str],
                   model: str, frame: str | None = None) -> FallbackResult | None:
    rendered = [r for r in rows if r["status"] == "RENDERED"]
    if not rendered:
        return None
    rd = ctx.remote_dir
    remote_cover = f"{rd}/cover"
    job = {"out": f"{remote_cover}/cand", "frame": frame or f"{remote_cover}/frame.png", "title": title,
           "lines": lines, "judge_model": model,
           "candidates": [{"id": r["id"], "vetoes": r["score"]["vetoes"]} for r in rendered]}
    local_job = _local(ctx, "cover/fallback.job.json")
    local_job.write_text(json.dumps(job, ensure_ascii=False, indent=2), encoding="utf-8")
    ctx.adapters.remote.put(REMOTE_TOOLS / "cover_fallback.py", f"{remote_cover}/cover_fallback.py")
    ctx.adapters.remote.put(local_job, f"{remote_cover}/fallback.job.json")
    result = _run_remote(ctx, "s6-cover-fallback", root_script(
        remote_python(f"{remote_cover}/cover_fallback.py {remote_cover}/fallback.job.json"),
        env=["cpa"], owned=remote_cover, recursive=True), timeout=1800)
    if result.rc != 0:
        raise RuntimeError(f"CPA_COVER_CHOICE_FAILED rc={result.rc}: {(result.stdout + result.stderr)[-400:]}")
    path = _local(ctx, "cover/cand/fallback.result.json")
    ctx.adapters.remote.get(f"{remote_cover}/cand/fallback.result.json", path)
    ctx.adapters.remote.get(f"{remote_cover}/cand/fallback-choose.response.json",
                            _local(ctx, "cover/cand/fallback-choose.response.json"))
    data = cast(FallbackResult, json.loads(path.read_text(encoding="utf-8")))
    if data["choice"]["chosen"] not in {r["id"] for r in rendered}:
        raise ValueError("CPA_COVER_CHOICE_INVALID")
    return data
