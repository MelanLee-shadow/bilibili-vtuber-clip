"""Step 6 cover: every feasible route is made at once (70-cover), not stage by stage until one passes.

The routes are split into independent remote jobs that run at the same time (parallel.run, bound lane_rules
`concurrency.cpa`, shared with every other CPA call of the process; this module adds no bound of its own):

- screenshot, emote, the scene (or the chibi sticker alone) are one job each;
- the cut-out and the chibi sticker are one job when the frame has a guest: the sticker's guest layer is the cut-out's
  (cover_candidates.py reuses it), so that pair stays in one tool run, cut-out first.

Each job has its own work directory on the media host (readings, generated layers); the rendered candidates and their judge
answers are written flat to `cover/cand/`, where the vetoed-candidates fallback also reads them.  A route that cannot be
made is a recorded fact (`infeasible`), not a failure; the step fails only when no candidate was rendered at all."""

from __future__ import annotations

import contextlib
import json
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, cast

from . import parallel
from .adapters import remote_python, root_script
from .step6_cover_types import CoverCandidate, CoverCandidateCall, CoverCandidateResult
from .step_io import _local, _run_remote

if TYPE_CHECKING:
    from .runner import Ctx

TEXT_TOO_LONG_RC = 23      # the renderer's text-does-not-fit exit (cover_candidates.py)


@dataclass
class GroupRun:
    name: str
    routes: list[str]
    rc: int = 0
    note: str = ""
    wall_s: float = 0.0
    result: CoverCandidateResult | None = None
    rows: list[CoverCandidate] = field(default_factory=list)
    skipped: str = ""          # why the job was not run (the route is not feasible for this story)
    stdout_path: str = ""      # the job's whole output, kept in its own file (local path)
    stderr_path: str = ""
    tool_failure: str = ""     # the tool itself failed: nonzero exit, or it left no result file (never "infeasible")


class RouteInfeasible(Exception):
    """Raised by a route's `before` hook: the route cannot be made for this story; the reason is recorded."""


def cover_groups(routes: Sequence[str], *, has_guests: bool) -> list[list[str]]:
    """The routes as independent jobs.  With a guest the cut-out and the sticker share one job (cut-out first)."""
    paired = has_guests and "cutout" in routes and "ai_sticker" in routes
    groups = [[r] for r in routes if not (paired and r in ("cutout", "ai_sticker"))]
    if paired:
        groups.append(["cutout", "ai_sticker"])
    return groups


def group_job(job: dict, routes: Sequence[str], rd: str, tag: str = "") -> tuple[str, dict]:
    """`tag` ("-r2": the second round of a redo) gives the job its own files and work directory, so nothing of the round
    before is reused or overwritten except the candidates' pictures (named by route)."""
    name = "+".join(routes) + tag
    return name, dict(job, routes=list(routes), work=f"{rd}/cover/cand/work-{name}", result=f"result-{name}.json")


def run_group(ctx: Ctx, job: dict, routes: Sequence[str], tag: str = "") -> GroupRun:
    """One media-host run of cover_candidates.py for `routes`; fetches its candidates' pictures and judge answers."""
    rd = ctx.remote_dir
    name, gjob = group_job(job, routes, rd, tag)
    local_job = _local(ctx, f"cover/job-{name}.json")
    local_job.write_text(json.dumps(gjob, ensure_ascii=False, indent=2), encoding="utf-8")
    ctx.adapters.remote.put(local_job, f"{rd}/cover/job-{name}.json")
    body = (f"mkdir -p {gjob['work']}\nrm -f {rd}/cover/cand/{gjob['result']}\n"
            + root_script(remote_python(f"{rd}/cover/cover_candidates.py {rd}/cover/job-{name}.json"),
                          env=["cpa"], owned=f"{rd}/cover", recursive=True))
    started = time.monotonic()
    # one script file per job: concurrent jobs must never share a writable name (the adapter writes <name>.sh)
    res = _run_remote(ctx, f"s6-cover-job-{name.replace('+', '_')}", body, timeout=3600)
    run = GroupRun(name=name, routes=list(routes), rc=res.rc, note=(res.stdout + res.stderr)[-400:])
    for stream, text in (("stdout", res.stdout), ("stderr", res.stderr)):
        path = _local(ctx, f"cover/cand/logs/{name.replace('+', '_')}.{stream}.txt")
        path.write_text(text, encoding="utf-8")
        setattr(run, f"{stream}_path", str(path))
    result_local = _local(ctx, f"cover/cand/{gjob['result']}")
    try:
        ctx.adapters.remote.get(f"{rd}/cover/cand/{gjob['result']}", result_local)
        run.result = cast(CoverCandidateResult, json.loads(result_local.read_text(encoding="utf-8")))
    except (RuntimeError, OSError, ValueError):
        run.result = None
    if run.result is None:
        run.tool_failure = f"TOOL_NO_RESULT_RC{res.rc}"
    elif res.rc not in (0, TEXT_TOO_LONG_RC):
        run.tool_failure = f"TOOL_RC{res.rc}"
    for row in (run.result or {}).get("rows", []):
        run.rows.append(row)
        if row.get("status") == "RENDERED":
            for ext in ("jpg", "png"):
                ctx.adapters.remote.get(f"{rd}/cover/cand/{row['id']}.{ext}", ctx.clip_dir / f"cover/cand/{row['id']}.{ext}")
        if row.get("answer") is not None:
            with contextlib.suppress(RuntimeError):
                ctx.adapters.remote.get(f"{rd}/cover/cand/{row['id']}.judge.response.json",
                                        ctx.clip_dir / f"cover/cand/{row['id']}.judge.response.json")
    run.wall_s = round(time.monotonic() - started, 2)
    return run


def run_all(ctx: Ctx, job: dict, groups: Sequence[Sequence[str]],
            before: dict[str, Callable[[], dict]] | None = None, tag: str = "") -> list[GroupRun]:
    """All groups at once, in input order.  `before[route]` runs inside that route's own job first and returns the job
    fields it settles (the emote's model call, which must not hold the other routes back); it may raise RouteInfeasible."""
    hooks = before or {}

    def one(routes: Sequence[str]) -> GroupRun:
        settled: dict = {}
        try:
            for route in routes:
                if route in hooks:
                    settled.update(hooks[route]())
        except RouteInfeasible as exc:
            return GroupRun(name="+".join(routes) + tag, routes=list(routes), skipped=str(exc))
        return run_group(ctx, dict(job, **settled), routes, tag)
    return parallel.run(list(groups), one, provider="cpa")


def infeasible_routes(runs: Sequence[GroupRun], wanted: Sequence[str]) -> list[dict[str, str]]:
    """Why a wanted route has no rendered candidate although its tool ran: the route's own recorded failure, or the
    reason it was not run at all.  A tool that failed or left no result file is NOT here: see `tool_failures`."""
    out: list[dict[str, str]] = []
    for run in runs:
        rows = {r["id"]: r for r in run.rows}
        for route in run.routes:
            if run.skipped:
                out.append({"route": route, "reason": run.skipped})
                continue
            row = rows.get(route)
            if run.tool_failure and row is None:
                continue
            if row is not None and row.get("status") == "RENDERED":
                continue
            out.append({"route": route, "reason": str(row.get("failure") or row.get("judge_error") or "NOT_RENDERED")
                        if row is not None else "NOT_BUILT"})
    out.extend({"route": route, "reason": "NOT_RUN"} for route in wanted if not any(route in run.routes for run in runs))
    return out


def tool_failures(runs: Sequence[GroupRun]) -> list[dict[str, object]]:
    """The jobs whose tool failed (exit code, or no result file), each with its own whole output in its own files."""
    return [{"job": run.name, "routes": run.routes, "reason": run.tool_failure, "rc": run.rc,
             "stdout": run.stdout_path, "stderr": run.stderr_path, "tail": run.note}
            for run in runs if run.tool_failure]


def phase_seconds(runs: Sequence[GroupRun]) -> dict[str, float]:
    """The remote phases summed over the jobs' recorded calls: image generation, face measurement (the locate readings),
    scoring (the judge readings) and rendering (rows' render_s).  Calls and renders of different jobs overlap in time, so
    these are work seconds; each job's own wall clock is `GroupRun.wall_s`."""
    gen = measure = score = render = 0.0
    for run in runs:
        for call in cast("list[CoverCandidateCall]", (run.result or {}).get("calls") or []):
            took = float(call.get("elapsed_s") or 0)
            if call.get("kind") == "image_edit":
                gen += took
            elif str(call.get("candidate") or "").endswith(".judge"):
                score += took
            else:
                measure += took
        render += sum(float(r.get("render_s") or 0) for r in run.rows)
    return {"generate": round(gen, 1), "measure": round(measure, 1), "score": round(score, 1), "render": round(render, 1)}


PHASE_LABELS = {"locate": "定位", "generate": "生图", "layout": "排版", "measure": "量脸", "render": "渲染", "judge": "判官"}


def route_phases(runs: Sequence[GroupRun]) -> str:
    """Where each route's own seconds went inside the tool (the rows' `phases_s`, cover_clock.py): so a slow route shows
    at a glance whether it waited on a model call, laid out, rendered or was judged."""
    parts = []
    for run in runs:
        for row in run.rows:
            phases = row.get("phases_s") or {}
            if phases:
                parts.append(f"{row['id']} " + "、".join(f"{PHASE_LABELS.get(k, k)} {v}s" for k, v in phases.items()))
    return "；".join(parts)


def timing_line(runs: Sequence[GroupRun], *, frame_s: float | None, thumb_s: float, total_s: float) -> str:
    ph = phase_seconds(runs)
    per = "、".join(f"{r.name} {r.wall_s}s" for r in runs)
    return (f"封面各阶段墙钟：取帧 {'未计' if frame_s is None else f'{frame_s:.1f}s'}；各路线同时生成（每条路线整段墙钟：{per}；"
            f"这一段共 {total_s:.1f}s）；远端累计工作秒：生图 {ph['generate']}s、量脸 {ph['measure']}s、打分 {ph['score']}s、"
            f"渲染 {ph['render']}s；缩略图判定 {thumb_s:.1f}s"
            + (f"；各路线内部：{inner}" if (inner := route_phases(runs)) else ""))
