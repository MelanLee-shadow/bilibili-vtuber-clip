"""Step 6 cover: the rounds of one cover render (the first make, the redo before the fallback), the choice and the record.

`step6_cover._render_cover` sets the first round up; this module runs the rounds (cover_redo says which routes are made
again and what is kept), picks the cover (thumbnail check, then the fallback among the candidates not vetoed on their
material) and writes choose.json, the route record and the PROCESS lines."""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from functools import partial
from typing import TYPE_CHECKING, cast

from . import cover_redo
from .adapters import CPA_JUDGE_MODEL
from .cover_emote_pick import pick_emote
from .cover_pick import background_process_lines, candidate_record, choose_checked, rank
from .cover_redo import RedoLog, RetryBudget
from .cover_run import (
    GroupRun,
    RouteInfeasible,
    cover_groups,
    infeasible_routes,
    run_all,
    timing_line,
    tool_failures,
)
from .cover_thumb import ThumbVerdict
from .lane_rules import rules
from .step6_cover import (
    COVER_AI_ROUTES,
    _scored_rows,
    _thumb,
    cover_calls_line,
    cover_candidate_calls,
    cover_composition_key,
    cover_host_side,
    cover_repeats,
    utc_now_iso,
)
from .step6_cover_types import CoverCandidate, CoverPlan, CoverRenderResult, CoverRoute, FrameCall
from .step_io import _local, _run_remote

if TYPE_CHECKING:
    from src.autoslice.cpa_gemini_client import JsonValue

    from .runner import Ctx
    from .state import CpaEvent
    from .step6_cover_fallback import FallbackResult


def _checked_frames(ctx: Ctx) -> list[FrameCall]:
    try:
        return list(json.loads((ctx.clip_dir / "cover/frames.json").read_text(encoding="utf-8")).get("checked") or [])
    except (OSError, ValueError):
        return []


@dataclass
class Cover:
    """What one cover render shares across its rounds: the first round's job and spec, every round's rows (cover_redo.RedoLog),
    the receipts and the PROCESS lines, the retry budget."""

    ctx: Ctx
    job: dict[str, JsonValue]
    spec: dict[str, JsonValue]
    plan: CoverPlan
    routes: list[str]
    times: list[float]
    budget: RetryBudget
    frame_remote: str
    calls: list[CpaEvent] = field(default_factory=list)
    process: list[str] = field(default_factory=list)
    runs: list[GroupRun] = field(default_factory=list)
    failures: list[dict[str, object]] = field(default_factory=list)
    emote_pick: dict[str, JsonValue] = field(default_factory=dict)
    log: RedoLog = field(default_factory=RedoLog)

    def emote_job(self, cover_lines: Sequence[str]) -> dict:
        emote, pick, made = pick_emote(self.ctx, self.plan, cover_lines)
        self.calls.extend(made)
        self.emote_pick.update(cast("dict[str, JsonValue]", pick))
        if emote is None:
            raise RouteInfeasible("没有主表情包：" + str(pick.get("reason") or "说明里没有一张对得上封面字"))
        return {"emote": emote}

    def make(self, job: dict, routes: Sequence[str], hooks: dict[str, Callable[[], dict]], tag: str = ""
             ) -> tuple[list[GroupRun], list[CoverCandidate], list[dict[str, str]]]:
        """Make `routes` at once (one job per group, cover_run.run_all); returns the runs, the tool's rows and the routes
        that could not be made.  The receipts and PROCESS lines are kept here."""
        runs = run_all(self.ctx, job, cover_groups(routes, has_guests=bool(job["guest_boxes"])), hooks, tag)
        self.runs.extend(runs)
        self.process += [f"封面路线 {run.name}（{run.wall_s}s）：" + (cover_calls_line(run.result) if run.result else run.skipped or f"没有结果（rc={run.rc}）")
                         for run in runs]
        for run in runs:
            self.calls.extend(cover_candidate_calls(run.result))
        infeasible = infeasible_routes(runs, routes)
        if infeasible:
            self.process.append("不可行或没做出的路线（记录，不算失败）：" + "；".join(f"{i['route']}（{i['reason']}）" for i in infeasible))
        failures = tool_failures(runs)
        self.failures += failures
        if failures:
            self.process.append("路线工具失败（完整输出在各自文件里，见 choose.json 的 tool_failures）：" + "；".join(
                f"{f['job']}（{f['reason']}，stdout {f['stdout']}，stderr {f['stderr']}）" for f in failures))
        return runs, [row for run in runs for row in run.rows], infeasible

    def score(self, raw: Sequence[CoverCandidate]) -> list[CoverCandidate]:
        rows, _ = _scored_rows(raw, self.plan)
        for row in rows:
            row["repeat"] = row["status"] == "RENDERED" and cover_repeats(row, self.plan)
        if repeated := [r["id"] for r in rows if r.get("repeat")]:
            self.process.append(f"{'、'.join(repeated)} 与本场此前封面同路线、同构图（70-cover 同场创新）：排在不重复的候选之后")
        return rows

    def redo(self) -> None:
        """While nothing is eligible and a route failed on its material, make those routes again (cover_redo), each round
        spending one of the budget shared with the cover text's shortening."""
        log, ctx = self.log, self.ctx
        while self.budget.left() > 0 and (todo := cover_redo.redo_routes(log.current(), log.rows, self.routes)):
            number = len(log.rounds) + 1
            self.budget.spend()
            routes = list(todo)
            texts = {route: cover_redo.rejection_text(row) for route, row in todo.items()}
            frame_t, moved = self.new_frame(number, todo)
            for new in log.supersede(ctx.clip_dir / "cover/cand", routes):
                for ext in ("png", "jpg"):      # the pictures of the round replaced stay choosable (the fallback reads them there)
                    if (kept := ctx.clip_dir / f"cover/cand/{new}.{ext}").exists():
                        ctx.adapters.remote.put(kept, f"{ctx.remote_dir}/cover/cand/{new}.{ext}")
            brief = cast("dict[str, object]", self.job["brief"])
            words = {route: cover_redo.rejection_text(row, codes=False) for route, row in todo.items()}
            hooks = {route: partial(_retry_brief, brief, words[route]) for route in routes
                     if route in cover_redo.DRAWN and words[route]}
            job = dict(self.job, **moved)
            self.process.append(
                f"封面重做 第 {number - 1}/{self.budget.limit} 次（没有合格候选，{'、'.join(routes)} 被素材类否决）："
                + "；".join(f"{route} {texts[route]}" for route in routes)
                + (f"；换帧 {frame_t}s" if moved else "；没有换帧")
                + "；生成类路线用新的工作目录重新生成，贴纸与场景有判官原话的，原话带进提示")
            _runs, raw, infeasible = self.make(job, routes, hooks, tag=f"-r{number}")
            log.add_round(number, frame_t or log.frames[-1], routes, self.score(raw), raw,
                          infeasible=infeasible, frame_changed=bool(moved),
                          redone={route: {"vetoes": cover_redo.material_vetoes(row), "rejection": texts[route],
                                          "regenerated": route in COVER_AI_ROUTES, "reasons_in_prompt": route in hooks}
                                  for route, row in todo.items()})

    def new_frame(self, number: int, todo: Mapping[str, CoverCandidate]) -> tuple[float | None, dict[str, JsonValue]]:
        """(time, job fields) of another frame for this redo (a route that found no face is made again on another of the
        frames the frame step looked at), or (None, {}) when no route lacked a face or no other frame is left or the frame
        could not be cut."""
        ctx, rd = self.ctx, self.ctx.remote_dir
        t = (cover_redo.alternate_frame(self.times, self.log.frames, _checked_frames(ctx))
             if any(cover_redo.faceless(row) for row in todo.values()) else None)
        if t is None:
            return None, {}
        frame = f"{rd}/cover/frame-r{number}.png"
        made = _run_remote(ctx, f"s6-cover-redo-frame-{number}",
                           f"ffmpeg -hide_banner -loglevel error -y -ss {t:.3f} -i {rd}/cut/cut.mp4 -frames:v 1 {frame} || exit 22\n",
                           timeout=600)
        if made.rc != 0:
            self.process.append(f"封面重做换帧 {t}s 没有取出画面（rc={made.rc}），沿用原帧")
            return None, {}
        local_spec = _local(ctx, f"cover/spec-r{number}.json")
        local_spec.write_text(json.dumps(dict(self.spec, frame=frame), ensure_ascii=False, indent=2), encoding="utf-8")
        ctx.adapters.remote.put(local_spec, f"{rd}/cover/spec-r{number}.json")
        self.frame_remote = frame
        return t, {"frame": frame, "screenshot_spec": f"{rd}/cover/spec-r{number}.json"}


def _retry_brief(brief: Mapping[str, object], rejected: str) -> dict:
    return {"brief": cover_redo.retry_brief(brief, rejected)}


@dataclass
class Picked:
    chosen: str
    why: str
    thumbs: dict[str, ThumbVerdict]
    fallback: FallbackResult | None
    flag: dict[str, object] | None


def pick_cover(cv: Cover, cover_lines: Sequence[str], title: str, identity: str) -> Picked | CoverRenderResult:
    """The eligible candidate whose thumbnail passes, else the fallback: CPA's look among the candidates not vetoed on their
    material, else (all of them are) the one with the fewest vetoes, flagged."""
    ctx, rows, calls, process = cv.ctx, cv.log.rows, cv.calls, cv.process
    width = rules("cover")["thumb_width_px"]
    ranked = rank(rows, repeats=lambda r: bool(r.get("repeat")), target_px=round(rules("cover")["face_target_frac"] * 1080))
    if ranked:
        chosen, why, thumbs = choose_checked(ranked, lambda cid: _thumb(ctx, cid, width, cover_lines, identity, calls))
        return Picked(chosen, why, thumbs, None, None)
    from .step6_cover_fallback import choose_vetoed
    clean, rendered = cover_redo.fallback_pool(rows)
    if not rendered:
        return CoverRenderResult(False, reason="COVER_NO_ELIGIBLE_CANDIDATE", note="no rendered image", cpa=calls, process=process)
    vetoed = "全部候选被代码否决：" + "；".join(f"{r['id']}（{'、'.join(r['score'].get('vetoes') or [])}）" for r in rows)
    if not clean:
        best = cover_redo.fewest_vetoes(rendered)
        flag = cover_redo.material_flag(best)
        process.extend([vetoed, cover_redo.flag_line(flag)])
        return Picked(best["id"], f"所有候选都被素材类否决，取否决项最少的 {best['id']}（{len(cover_redo.vetoes_of(best))} 项）",
                       {best["id"]: _thumb(ctx, best["id"], width, cover_lines, identity, calls)}, None, flag)
    try:
        fallback = choose_vetoed(ctx, clean, title=title, lines=list(cover_lines), model=CPA_JUDGE_MODEL, frame=cv.frame_remote)
    except (RuntimeError, OSError, ValueError, KeyError) as exc:
        return CoverRenderResult(False, reason="COVER_VISION_CHOICE_FAILED", note=str(exc), cpa=calls, process=process)
    if fallback is None:
        return CoverRenderResult(False, reason="COVER_NO_ELIGIBLE_CANDIDATE", note="no rendered image", cpa=calls, process=process)
    chosen = fallback["choice"]["chosen"]
    calls.extend(cover_candidate_calls({"calls": fallback["calls"]}))
    process.append(vetoed + f"；CPA 看图选择 {chosen}：{fallback['choice']['reason']}")
    return Picked(chosen, f"全部候选被代码否决，CPA 看图选择 {fallback['choice']['reason']}",
                   {chosen: _thumb(ctx, chosen, width, cover_lines, identity, calls)}, fallback, None)


def record_cover(cv: Cover, picked: Picked, cover_lines: Sequence[str], boxes: Mapping[str, list[list[int]]] | None,
                  *, frame_s: float | None, thumb_s: float, started: float) -> CoverRenderResult:
    """choose.json (every round's candidates and why the next round was made), the contact sheet, cover.png, the route record
    and the PROCESS lines of the chosen cover."""
    from src.autoslice.title_cover_scoring import contact_sheet

    ctx, plan, rows, calls, process = cv.ctx, cv.plan, cv.log.rows, cv.calls, cv.process
    chosen, why, thumbs, fallback = picked.chosen, picked.why, picked.thumbs, picked.fallback
    for row in rows:
        row["thumb"] = thumbs.get(row["id"])
    for record in cv.log.rounds:       # the thumbnail verdicts exist only now
        for candidate in cast("list[dict[str, object]]", record["candidates"]):
            candidate["thumb"] = thumbs.get(str(candidate["id"]))
    cand_dir = ctx.clip_dir / "cover/cand"
    winner = next(r for r in rows if r["id"] == chosen)
    cover_path = _local(ctx, "cover/cover.png")
    cover_path.write_bytes((cand_dir / f"{chosen}.png").read_bytes())
    timing = timing_line(cv.runs, frame_s=frame_s, thumb_s=thumb_s, total_s=time.monotonic() - started)
    choose_path = _local(ctx, "cover/cand/choose.json")
    first = cv.log.rounds[0]
    choose_path.write_text(json.dumps({
        "rows": [candidate_record(r) for r in rows], "chosen": chosen,
        "selected": {"id": chosen, "reason": why, "thumb": (thumbs.get(chosen) or {}).get("status"),
                     "thumb_all_failed": bool(thumbs) and all(t["status"] == "FAIL" for t in thumbs.values())},
        "infeasible_routes": first["infeasible"], "tool_failures": cv.failures,
        "vetoed": [{"id": r["id"], "vetoes": r["score"].get("vetoes") or []} for r in rows if r["score"]["status"] != "ELIGIBLE"],
        "recent": plan["recent"], "participants": plan["participants"],
        "cover_lines": list(cover_lines), "stages": [cv.routes], "emote_pick": cv.emote_pick, "timing": timing,
        "rounds": cv.log.rounds, "redo": {"limit": cv.budget.limit, "used": cv.budget.used},
        "material_flag": picked.flag,
        "model_calls": [{"call": c.get("call_id"), "kind": c.get("kind"), "model": c.get("model"), "elapsed_s": c.get("elapsed_s")}
                        for c in calls], "fallback_choice": fallback and fallback["choice"]
        or picked.flag and {"chosen": chosen, "reason": why, "method": "fewest_vetoes"}}, ensure_ascii=False, indent=2),
        encoding="utf-8")
    try:
        sheet = str(contact_sheet(cand_dir, cast("list[dict[str, JsonValue]]", rows), chosen))
    except (OSError, ValueError):
        sheet = ""
    src_row = next((r for r in cv.log.raw if r["id"] == chosen), cast(CoverCandidate, {}))
    own = (src_row.get("generation") or {}).get(winner["route"]) or {}
    generated = winner["route"] in COVER_AI_ROUTES
    route: CoverRoute = {"method": "skill.candidates", "route_schema": "lidousha-title-cover-route.v1", "at": utc_now_iso(),
             "recording_date": ctx.session.get("recording_date"),
             "chosen": {"id": chosen, "route": winner["route"], "main_emote": winner.get("main_emote"),
                        "score": winner["score"], "style_id": (winner.get("render") or {}).get("style_id"),
                        "layout": (winner.get("render") or {}).get("layout"),
                        "host_side": (winner.get("render") or {}).get("host_side"),
                        "colors": (winner.get("render") or {}).get("colors"),
                        "background_id": (winner.get("background") or {}).get("id"),
                        "punch_font_size": winner.get("punch_font_size"),
                        "composition_key": cover_composition_key(winner["route"], winner.get("render"),
                                                                 winner.get("main_emote"), plan.get("brief"))},
             "peers": plan.get("peers"), "host_side_reason": cover_host_side(plan, boxes)[1],
             "image_generation_used": generated,
             "image_gen_model": (own.get("model") or "UNKNOWN") if generated else "none",
             "reported_model": (own.get("reported_model") or "UNKNOWN") if generated else None,
             "generation": src_row.get("generation") or {},
             "candidates": [{"id": r["id"], "route": r["route"], "status": r["status"], "score": r["score"],
                             "note": r.get("note")} for r in rows],
             "choose": str(choose_path), "sheet": sheet}
    if picked.flag:
        route["material_flag"] = picked.flag
    made_t = cv.log.made[chosen][1]       # the frame the chosen cover was made on (a redo can have changed it)
    if made_t != first["frame_t"]:
        route["frame_s"] = cast(float, made_t)
    process.extend(_cover_summary_lines(cv, rows, winner, route, why, generated, timing))
    return CoverRenderResult(True, products={"cover": str(cover_path), "choose": str(choose_path), "route": route},
                       note=f"cover t={made_t:.2f}s lines={list(cover_lines)} chosen={chosen}", cpa=calls, process=process)


def _cover_summary_lines(cv: Cover, rows: Sequence[CoverCandidate], winner: CoverCandidate, route: CoverRoute, why: str,
                         generated: bool, timing: str) -> list[str]:
    chosen = winner["id"]
    out = ["封面候选得分（人物×3、字号×2、贴合、真实、新鲜；CPA 看图逐项给理由）：" + "；".join(
        f"{r['id']} " + (f"{r['score']['total']} 分（P{r['score']['P']} T{r['score']['T']} H{r['score']['H']} "
                         f"C{r['score']['C']} V{r['score']['V']}；脸 {r.get('face_px')}px/{r.get('face_status')}）"
                         if r["score"]["status"] == "ELIGIBLE" else "淘汰 " + "、".join(r["score"].get("vetoes") or []))
        for r in rows)]
    if len(cv.log.rounds) > 1:
        out.append(f"封面重做共 {len(cv.log.rounds) - 1} 次（上限 {cv.budget.limit}，与封面字缩短共用）；各轮：" + "；".join(
            f"第 {rec['round']} 轮（帧 {rec['frame_t']}s）" + "、".join(
                f"{c['id']} " + ("合格" if cast("dict[str, object]", c["score"])["status"] == "ELIGIBLE" else "淘汰")
                for c in cast("list[dict[str, object]]", rec["candidates"])) for rec in cv.log.rounds))
    winner_score = f"{winner['score']['total']} 分" if "total" in winner["score"] else "未评分"
    out.append(f"封面采用：{chosen}（{winner['route']}，{winner_score}）：{why}；"
               + (f"生图 {route['image_gen_model']}（上游报告 {route['reported_model'] or 'UNKNOWN'}）" if generated
                  else "这张没有用生图（image_generation_used=false）"))
    if flagged := [f"{r['id']} 笑点 {r.get('punch_font_size')}px 低于 {r.get('punch_floor_px')}px" for r in rows
                   if r.get("punch_below_floor")]:
        out.append("笑点字没有达到下限（记录并标注，不阻塞）：" + "；".join(flagged))
    out.extend(background_process_lines(rows))
    picked = route["chosen"]
    out.append(f"封面字的风格（仓库风格表轮换）：{picked['style_id']}，字色 {'/'.join(picked['colors'] or [])}，"
               f"梗字 {picked['punch_font_size']}px，版式 {picked['layout']}；主播在{picked['host_side']}（{route['host_side_reason']}）")
    out.append(timing)
    return out
