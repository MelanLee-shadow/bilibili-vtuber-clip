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

from . import cover_guests, cover_redo
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
    base_frame,
    _scored_rows,
    _thumb,
    cover_calls_line,
    cover_candidate_calls,
    cover_composition_key,
    cover_host_side,
    cover_looks_repeated,
    cover_repeats,
    on_action,
    utc_now_iso,
)
from .step6_cover_types import CoverCandidate, CoverPlan, CoverRenderResult, CoverRoute, FrameCall
from .step_io import _local, _run_remote, vision_models

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
    stages: list[list[str]] = field(default_factory=list)          # the stages actually made (cover_stages), in order
    thumbs: dict[str, ThumbVerdict] = field(default_factory=dict)   # thumbnail verdicts by candidate id (each read once)

    def __post_init__(self) -> None:
        if not self.stages:
            self.stages.append(list(self.routes))

    def thumb(self, cid: str, cover_lines: Sequence[str], identity: str) -> ThumbVerdict:
        if cid not in self.thumbs:
            self.thumbs[cid] = _thumb(self.ctx, cid, rules("cover")["thumb_width_px"], cover_lines, identity, self.calls)
        return self.thumbs[cid]

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
        for lost in {(d["name"], d["why"]) for run in runs for row in run.rows for d in cover_guests.faceless(row)}:
            if (line := f"缺{lost[0]}的素材：{lost[1]}；这位不上封面，不拿别的图顶替") not in self.process:
                self.process.append(line)
        infeasible = infeasible_routes(runs, routes)
        if infeasible:
            self.process.append("不可行或没做出的路线（记录，不算失败）：" + "；".join(f"{i['route']}（{i['reason']}）" for i in infeasible))
        failures = tool_failures(runs)
        self.failures += failures
        if failures:
            self.process.append("路线工具失败（完整输出在各自文件里，见 choose.json 的 tool_failures）：" + "；".join(
                f"{f['job']}（{f['reason']}，stdout {f['stdout']}，stderr {f['stderr']}）" for f in failures))
        return runs, [row for run in runs for row in run.rows], infeasible

    def score(self, raw: Sequence[CoverCandidate], frame_t: float | None) -> list[CoverCandidate]:
        rows, _ = _scored_rows(raw, self.plan)
        for row in rows:
            row["on_action"] = on_action(row, self.plan, frame_t)
            fresh = row["status"] != "RENDERED" or row["on_action"]
            row["repeat"] = None if fresh else cover_repeats(row, self.plan)
            row["look_repeat"] = not fresh and cover_looks_repeated(row, self.plan)
        if acted := [r["id"] for r in rows if r.get("on_action") and r["score"]["status"] == "ELIGIBLE"]:
            action = self.plan.get("action_frame") or {}
            self.process.append(f"{'、'.join(acted)} 截在她的标志性动作上（{action.get('what')}）：合格就排在所有候选前面")
        if repeated := [f"{r['id']}（{r['repeat']}）" for r in rows if r.get("repeat")]:
            self.process.append(f"{'、'.join(repeated)} 与最近的封面重复：排在不重复的候选之后（免费的仍可选，不为换样子出 AI 图）")
        if looked := [r["id"] for r in rows if r.get("look_repeat")]:
            self.process.append(f"{'、'.join(looked)} 与本场此前封面同路线、同构图（70-cover 同场创新）：排在不重复的候选之后")
        for row in rows:
            if row.get("cover_layout_note"):
                self.process.append(f"{row['id']} {row['cover_layout_note']}")
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
                f"封面重做 第 {1 + sum(1 for r in log.rounds if r.get('redone'))}/{self.budget.limit} 次（没有合格候选，{'、'.join(routes)} 被素材类否决）："
                + "；".join(f"{route} {texts[route]}" for route in routes)
                + (f"；换帧 {frame_t}s" if moved else "；没有换帧")
                + "；生成类路线用新的工作目录重新生成，贴纸与场景有判官原话的，原话带进提示")
            _runs, raw, infeasible = self.make(job, routes, hooks, tag=f"-r{number}")
            log.add_round(number, frame_t or log.frames[-1], routes, self.score(raw, frame_t or log.frames[-1]), raw,
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
        return t, {"frame": frame, "screenshot_spec": f"{rd}/cover/spec-r{number}.json",
                   "source_guest_boxes_unknown": bool(self.job.get("guest_assets"))}


def _retry_brief(brief: Mapping[str, object], rejected: str) -> dict:
    return {"brief": cover_redo.retry_brief(brief, rejected)}


@dataclass
class Picked:
    chosen: str
    why: str
    thumbs: dict[str, ThumbVerdict]
    fallback: FallbackResult | None
    flag: dict[str, object] | None


def pick_free(cv: Cover, cover_lines: Sequence[str], identity: str) -> Picked | None:
    """The free stage's choice: the first eligible free candidate whose thumbnail passes, or None (then one picture is
    generated, cover_stages; U138: 免费合格即选，都不合格才生 1 张).  A candidate that repeats the covers before it
    (step6_cover.cover_repeats: a composition template of the last covers, the route the last covers all used; 70-cover
    构图样板) goes behind one that does not, and so does one that looks like a cover this
    stream already has (同一场的封面全是同一个样子、同一种颜色); among those a picture from the stream goes
    before a library sticker (the channel's request).  A repeat still passes when nothing else does: it is no
    reason to generate a picture.  An eligible candidate whose thumbnail fails does not end the free stage."""
    ranked = rank(cv.log.rows, repeats=lambda r: bool(r.get("repeat") or r.get("look_repeat")),
                  target_px=round(rules("cover")["face_target_frac"] * 1080))
    ranked.sort(key=lambda r: (bool(r.get("repeat")), bool(r.get("look_repeat")), r.get("route") == "emote"))   # stable
    for position, row in enumerate(ranked):
        if cv.thumb(row["id"], cover_lines, identity)["status"] != "FAIL":
            lead = f"免费候选排第 {position + 1} 的 {row['id']}" if position else f"免费候选中排最前的 {row['id']}"
            if row.get("repeat"):
                lead += f"（{row['repeat']}；没有不重复的免费候选合格，不为换样子出 AI 图）"
            skipped = f"；前 {position} 名缩略图未过" if position else ""
            return Picked(row["id"], f"{lead}（{row['score']['total']} 分）缩略图{cv.thumbs[row['id']]['status']}{skipped}",
                          {r["id"]: cv.thumbs[r["id"]] for r in ranked[:position + 1]}, None, None)
    return None


def pick_cover(cv: Cover, cover_lines: Sequence[str], title: str, identity: str) -> Picked | CoverRenderResult:
    """The eligible candidate whose thumbnail passes, else the fallback: CPA's look among the candidates not vetoed on their
    material and not leaving out a guest (cover_redo.fallback_pool), else (all of them are) the one with the fewest
    vetoes, flagged (step 10 sends nothing on it)."""
    ctx, rows, calls, process = cv.ctx, cv.log.rows, cv.calls, cv.process
    ranked = rank(rows, repeats=lambda r: bool(r.get("repeat") or r.get("look_repeat")),
                  target_px=round(rules("cover")["face_target_frac"] * 1080))
    if ranked:
        chosen, why, thumbs = choose_checked(ranked, lambda cid: cv.thumb(cid, cover_lines, identity))
        return Picked(chosen, why, {**cv.thumbs, **thumbs}, None, None)
    from .step6_cover_fallback import choose_vetoed
    clean, rendered = cover_redo.fallback_pool(rows)
    if not rendered:
        return CoverRenderResult(False, reason="COVER_NO_ELIGIBLE_CANDIDATE", note="no rendered image", cpa=calls, process=process)
    vetoed = "全部候选被代码否决：" + "；".join(f"{r['id']}（{'、'.join(r['score'].get('vetoes') or [])}）" for r in rows)
    if not clean:
        best = cover_redo.fewest_vetoes(rendered)
        flag = cover_redo.material_flag(best)
        process.extend([vetoed, cover_redo.flag_line(flag)])
        return Picked(best["id"], f"所有候选都被素材类否决或漏了该上封面的嘉宾，取否决项最少的 {best['id']}（{len(cover_redo.vetoes_of(best))} 项；上线前停下）",
                       {**cv.thumbs, best["id"]: cv.thumb(best["id"], cover_lines, identity)}, None, flag)
    try:
        fallback = choose_vetoed(ctx, clean, title=title, lines=list(cover_lines), model=vision_models()[0], frame=cv.frame_remote)
    except (RuntimeError, OSError, ValueError, KeyError) as exc:
        reason = "CPA_CALL_FAILED" if ("CPA_FALLBACK_FAILED" in str(exc) or "CPA_QUOTA_REFUSED" in str(exc)) else "COVER_VISION_CHOICE_FAILED"
        return CoverRenderResult(False, reason=reason, note=str(exc), cpa=calls, process=process)
    if fallback is None:
        return CoverRenderResult(False, reason="COVER_NO_ELIGIBLE_CANDIDATE", note="no rendered image", cpa=calls, process=process)
    chosen = fallback["choice"]["chosen"]
    calls.extend(cover_candidate_calls({"calls": fallback["calls"]}))
    process.append(vetoed + f"；CPA 看图选择 {chosen}：{fallback['choice']['reason']}")
    return Picked(chosen, f"全部候选被代码否决，CPA 看图选择 {fallback['choice']['reason']}",
                   {**cv.thumbs, chosen: cv.thumb(chosen, cover_lines, identity)}, fallback, None)


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
    guests = cover_record_guests(ctx, plan, winner, process)
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
        "recent": plan["recent"], "participants": plan["participants"], "roster": plan.get("roster"),
        "cover_lines": list(cover_lines), "stages": cv.stages, "emote_pick": cv.emote_pick, "timing": timing,
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
                                                                 winner.get("main_emote"), plan.get("brief")),
                        "template_id": (winner.get("render") or {}).get("template_id"),
                        "cover_layout": winner.get("cover_layout")},
             "peers": plan.get("peers"), "host_side_reason": cover_host_side(plan, boxes)[1],
             "guests": guests,
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
    route["base_frame"] = base_frame(ctx, float(made_t))
    process.extend(_cover_summary_lines(cv, rows, winner, route, why, generated, timing))
    return CoverRenderResult(True, products={"cover": str(cover_path), "choose": str(choose_path), "route": route},
                       note=f"cover t={made_t:.2f}s lines={list(cover_lines)} chosen={chosen}", cpa=calls, process=process)


def cover_record_guests(ctx: Ctx, plan: CoverPlan, winner: CoverCandidate, process: list[str]) -> dict[str, JsonValue]:
    """The chosen cover's guests (cover_guests: the four sets, the last read off the chosen picture by the judge), and,
    when the cover goes without a guest for want of a picture, the day's record of pictures to fetch and a PROCESS line."""
    roster = plan.get("roster")
    if not roster:
        return {}
    missing = [*roster["missing"], *({"name": d["name"], "why": d["why"]} for d in cover_guests.faceless(winner))]
    seen = cover_guests.visible(winner, plan["participants"][0] if plan["participants"] else "")
    lost = {m["name"] for m in missing}
    unseen = [name for name in roster["required"] if name not in seen and name not in lost]   # the flag stops step 10 on them
    out: dict[str, JsonValue] = {
        "source": roster["source"], "present": list(roster["present"]), "mentioned": list(roster["mentioned"]),
        "needed_on_cover": list(roster["needed_on_cover"]), "actually_visible": cast("JsonValue", seen),
        "missing": cast("JsonValue", missing), "not_visible": cast("JsonValue", unseen),
        "on_cover": [{k: g[k] for k in ("name", "asset_id", "sha256", "source_url")} for g in winner.get("guests_on_cover") or []]}
    if roster["needed_on_cover"]:
        process.append(f"封面上的嘉宾：该交代 {'、'.join(roster['needed_on_cover'])}；判官在成图上看得见 {'、'.join(seen) or '无'}"
                       + (f"；缺图 {'、'.join(m['name'] for m in missing)}" if missing else "")
                       + (f"；有图却在成图上看不见 {'、'.join(unseen)}（上线前停下）" if unseen else ""))
    if missing:
        path = cover_guests.note_wanted(ctx, missing)
        process.append(f"缺图的嘉宾记进当天待补：{path}（{'、'.join(m['name'] + '：' + m['why'] for m in missing)}）")
    return out


def _cover_summary_lines(cv: Cover, rows: Sequence[CoverCandidate], winner: CoverCandidate, route: CoverRoute, why: str,
                         generated: bool, timing: str) -> list[str]:
    chosen = winner["id"]
    out = ["封面候选得分（人物×3、字号×2、贴合、真实、新鲜；CPA 看图逐项给理由）：" + "；".join(
        f"{r['id']} " + (f"{r['score']['total']} 分（P{r['score']['P']} T{r['score']['T']} H{r['score']['H']} "
                         f"C{r['score']['C']} V{r['score']['V']}；脸 {r.get('face_px')}px/{r.get('face_status')}）"
                         if r["score"]["status"] == "ELIGIBLE" else "淘汰 " + "、".join(r["score"].get("vetoes") or []))
        for r in rows)]
    if redos := sum(1 for rec in cv.log.rounds if rec.get("redone")):
        out.append(f"封面重做共 {redos} 次（上限 {cv.budget.limit}，与封面字缩短共用）；各轮：" + "；".join(
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
    out.append(f"封面采用的{winner.get('cover_layout_note') or '构图样板：没有记录（这条路线没有走样板选择）'}")
    out.append(timing)
    return out
