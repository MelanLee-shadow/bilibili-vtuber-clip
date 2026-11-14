"""Steps 1, 0 and 2: boundary (CPA judges the cut and it is adopted), external duplicate check, cut on OCI3.

Split out of stage_impl.py (TASK-13); stage_impl re-exports every name, so callers and tests keep
`stage_impl.<name>`.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import TYPE_CHECKING, TypedDict, cast

from . import (
    compilation,
    gates,
    normalize,
)
from .adapters import REMOTE_PY, REMOTE_REPO
from .runner import Ctx, StepOutcome
from .source_read import (
    SourceMissing,
    clip_source_dir,
    source_audio_probe_script,
    source_cut_script,
)
from .srt_text import SrtCue, _as_int, _context_lines, _json_from_text, format_srt, window_cues
from .step_io import (
    ROOM_ID,
    _ask_cpa,
    _clip,
    _cpa_ok,
    _fail,
    _local,
    _log,
    _mac_bcut_cues,
    _render,
    _run_remote,
)

if TYPE_CHECKING:
    from .state import ClipInfo


# ----- step 1: boundary (CPA judges the cut and it is adopted) -------------------------------
class ProbeStream(TypedDict, total=False):
    codec_type: str
    width: int
    height: int


class ProbeFormat(TypedDict, total=False):
    duration: str | float


class ProbeDocument(TypedDict, total=False):
    streams: list[ProbeStream]
    format: ProbeFormat


class ProbeSummary(gates.CutProbe):
    width: int | None
    height: int | None


def _boundary_prompt(clip: ClipInfo, cues: Sequence[SrtCue], start_cue: int, end_cue: int) -> str:
    lo, hi = start_cue - 12, end_cue + 12
    return _render("boundary.md", start_cue=str(start_cue), end_cue=str(end_cue), hook=clip["hook"],
                   context=_context_lines([c for c in cues if lo <= c["id"] <= hi]))


def _compilation_boundary(ctx: Ctx, manifest: compilation.Manifest) -> StepOutcome:
    """A same-session compilation has no boundary review: its intervals were chosen and cut at cue boundaries by the
    selection step (compilation.json).  The review judges one continuous story's start and end and the 60 s-10 min
    check is that story's size, so on a compilation it would trim the joined clip to another range (or stop it); the
    whole joined file is the candidate."""
    clip = _clip(ctx)
    adopted = {"start_cue": clip["start_cue"], "end_cue": clip["end_cue"], "start_ms": clip["start_ms"],
               "end_ms": clip["end_ms"]}
    if manifest.get("kind") == compilation.KIND_TRIM:
        reason = (f"片中剪除：本片由 {manifest['origin']['cid']} 挖掉与主线无关的致谢后拼成，"
                  "起止沿用原片已定的边界，所以不再跑边界审查")
    else:
        reason = ("同场合集：各段起止已由选段步骤按 cue 边界定好并拼接（compilation.json），整条拼接片就是候选；"
                  "边界审查针对单个连续故事的起止和 60 s–10 min，会把合集裁成别的范围，所以不跑")
    decision = _local(ctx, "boundary/decision.json")
    decision.write_text(json.dumps({"proposal": [clip["start_cue"], clip["end_cue"]], "cpa": None, "skipped": reason,
                                    "adopted": adopted}, ensure_ascii=False, indent=2), encoding="utf-8")
    return StepOutcome(True, products={"boundary": str(decision)}, note="同场合集：跳过边界审查，整条拼接片采用",
                       process=[reason, f"合集 {len(manifest['pieces'])} 段，拼接后 {manifest['duration_ms'] / 1000:.2f}s"])


def step1_boundary(ctx: Ctx) -> StepOutcome:
    manifest = compilation.load_manifest(ctx.clip_dir)
    if manifest is not None:
        return _compilation_boundary(ctx, manifest)
    clip = _clip(ctx)
    cues = _mac_bcut_cues(ctx)
    proposal = clip.get("proposal") or {"start_cue": clip["start_cue"], "end_cue": clip["end_cue"]}
    p_start, p_end = int(proposal["start_cue"]), int(proposal["end_cue"])
    record = _ask_cpa(ctx, "boundary", _boundary_prompt(clip, cues, p_start, p_end), purpose="boundary")
    if not _cpa_ok(record):
        return _fail("CPA_CALL_FAILED", record.get("stderr_tail", ""), cpa=[record])
    try:
        judged = _json_from_text(record["response_text"])
    except ValueError:
        return _fail("CPA_RESPONSE_UNPARSEABLE", record["file"], cpa=[record])
    start_cue, end_cue = _as_int(judged.get("start_cue")), _as_int(judged.get("end_cue"))
    if start_cue is None or end_cue is None:
        return _fail("BOUNDARY_NO_CUT_PAIR", f"CPA gave no start_cue/end_cue: {str(judged)[:200]}", cpa=[record])
    verdict = gates.boundary_sanity(cues, start_cue=start_cue, end_cue=end_cue)
    if not verdict.ok:
        return _fail(verdict.reason_code, verdict.detail, cpa=[record])
    by_id = {c["id"]: c for c in cues}
    start_ms = round(float(by_id[start_cue]["start_s"]) * 1000)
    end_ms = round(float(by_id[end_cue]["end_s"]) * 1000)
    clip["start_cue"] = start_cue
    clip["end_cue"] = end_cue
    clip["start_ms"] = start_ms
    clip["end_ms"] = end_ms
    decision = _local(ctx, "boundary/decision.json")
    decision.write_text(json.dumps({"proposal": [p_start, p_end], "cpa": judged,
                                    "adopted": {"start_cue": start_cue, "end_cue": end_cue,
                                                "start_ms": start_ms, "end_ms": end_ms}},
                                   ensure_ascii=False, indent=2), encoding="utf-8")
    dur = verdict.evidence["duration_s"]
    return StepOutcome(True, products={"boundary": str(decision)}, cpa=[_log(record)],
                       note=f"CPA 判定切点 {start_cue}–{end_cue}，采用",
                       process=[f"提议切点（输入）：{p_start}–{p_end}",
                                f"CPA 判定：{judged.get('verdict')}；理由：{judged.get('reason', '')}",
                                f"采用切点 {start_cue}–{end_cue}：{start_ms / 1000:.2f}s–{end_ms / 1000:.2f}s，"
                                f"时长 {dur:.1f}s（源段内、时长在 30s–10min 内，常识检查通过）"])



def _source_intervals(ctx: Ctx, clip: ClipInfo, start: float, end: float) -> list[dict[str, str | int | float]]:
    """The real recording intervals of the clip: one for an ordinary clip; for a same-session compilation, every
    piece's own recording and interval (not the joined file)."""
    manifest = compilation.load_manifest(ctx.clip_dir)
    if manifest is None:
        return [{"recording": clip["source_name"], "room_id": ROOM_ID, "start_seconds": start, "end_seconds": end}]
    return [{"recording": row["recording"], "room_id": ROOM_ID, "start_seconds": row["source_start_ms"] / 1000,
             "end_seconds": row["source_end_ms"] / 1000}
            for row in compilation.source_intervals(manifest, round(start * 1000), round(end * 1000))]


def step0_dedupe(ctx: Ctx) -> StepOutcome:
    clip = _clip(ctx)
    start, end = clip["start_ms"] / 1000, clip["end_ms"] / 1000
    window = [cast(SrtCue, dict(c, start_s=c["start_s"] - start, end_s=c["end_s"] - start))
              for c in window_cues(_mac_bcut_cues(ctx), start, end)]
    local_srt = _local(ctx, "dedup/window.srt")
    local_srt.write_text(format_srt(window), encoding="utf-8")
    session = ctx.session
    package = {
        "candidate_id": clip["cid"], "recording_date": session["recording_date"],
        "participants": [list(g) for g in session["participants"]],
        "source_intervals": _source_intervals(ctx, clip, start, end),
        "duration_seconds": round(end - start, 3), "srt": "window.srt",
        "own_uploader_mids": list(session["own_uploader_mids"]), "subtitle_stage": "source_preview",
    }
    local_pkg = _local(ctx, "dedup/package.json")
    local_pkg.write_text(json.dumps(package, ensure_ascii=False, indent=2), encoding="utf-8")
    rd = ctx.remote_dir
    ctx.adapters.remote.put(local_pkg, f"{rd}/dedup/package.json")
    ctx.adapters.remote.put(local_srt, f"{rd}/dedup/window.srt")
    body = (f"mkdir -p {rd}/dedup/evidence {rd}/dedup/audio\ncd {REMOTE_REPO}\n"
            f"PYTHONDONTWRITEBYTECODE=1 {REMOTE_PY} -B scripts/check_external_duplicate_clips.py "
            f"--package {rd}/dedup/package.json --output {rd}/dedup/result.json "
            f"--evidence-dir {rd}/dedup/evidence --audio-work-dir {rd}/dedup/audio > {rd}/dedup/run.log 2>&1\n"
            "echo rc=$?\n")
    res = _run_remote(ctx, "s0-dedupe", body)
    # Exit 3 is the checker's own "block" signal; the decision in result.json is what counts.
    if res.rc not in (0, 3):
        return _fail("DEDUPE_TOOL_FAILED", f"{res.stdout[-300:]}{res.stderr[-300:]}")
    local_result = _local(ctx, "dedup/result.json")
    ctx.adapters.remote.get(f"{rd}/dedup/result.json", local_result)
    result = json.loads(local_result.read_text(encoding="utf-8"))
    verdict = gates.dedupe_gate(result)
    products = {"dedup_result": str(local_result)}
    if not verdict.ok:
        return StepOutcome(False, reason=verdict.reason_code, products={}, note=verdict.detail,
                           process=["外片查重：完全重复，按 H62 不传"])
    return StepOutcome(True, products=products, note=f"查重结论 {verdict.evidence.get('decision')}",
                       process=[f"查重结论 {verdict.evidence.get('decision')}（H62：只有完全重复才拦）"])


# ----- step 2: cut (ffmpeg on OCI3; tool-output sanity only) --------------------------------
def _probe_summary(probe: ProbeDocument) -> ProbeSummary:
    streams = probe.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), cast(ProbeStream, {}))
    return {"duration": float(probe.get("format", {}).get("duration", -1)),
            "width": video.get("width"), "height": video.get("height")}


def _snap_cut_points(ctx: Ctx, src: str) -> dict[str, str]:
    """Move the clip's start and end to a nearby silence in the source (A7 wants both in silence).  The clip's
    milliseconds are updated in place when a silence is found; otherwise the point stays and the reason is returned."""
    clip = _clip(ctx)
    rd = ctx.remote_dir
    s0, e0 = clip["start_ms"] / 1000, clip["end_ms"] / 1000
    windows = {"start": (max(0.0, s0 - normalize.SNAP_PROBE_S), s0 + normalize.SNAP_PROBE_S),
               "end": (max(0.0, e0 - normalize.SNAP_PROBE_S), e0 + normalize.SNAP_PROBE_S)}
    probe = source_audio_probe_script(
        src, windows, log_dir=f"{rd}/cut",
        audio_filter=f"silencedetect=noise={normalize.SILENCE_NOISE_DB}dB:d={normalize.SILENCE_MIN_S}")
    _run_remote(ctx, "s2-snap", f"mkdir -p {rd}/cut\n{probe}", timeout=900)   # a failed probe is recorded per point below
    decisions: dict[str, str] = {}
    new = {"start": s0, "end": e0}
    for role, (ws, _we) in windows.items():
        point = s0 if role == "start" else e0
        try:
            local = _local(ctx, f"cut/snap-{role}.log")
            ctx.adapters.remote.get(f"{rd}/cut/snap-{role}.log", local)
            found = normalize.snap_point(point, normalize.silence_intervals(
                local.read_text(encoding="utf-8", errors="replace"), ws), role=role)
        except (RuntimeError, OSError) as exc:
            decisions[role] = f"静音探测不可用（{str(exc)[:80]}），保持 {point:.3f}s"
            continue
        if found is None:
            decisions[role] = f"{point:.3f}s 附近 ±{normalize.SNAP_RADIUS_S}s 内无静音，保持原样"
            continue
        new[role] = found
        decisions[role] = f"{point:.3f}s → {found:.3f}s（静音）"
    clip["start_ms"] = round(new["start"] * 1000)
    clip["end_ms"] = round(new["end"] * 1000)
    ctx.state.save()
    return decisions


def step2_cut(ctx: Ctx) -> StepOutcome:
    clip = _clip(ctx)
    rd = ctx.remote_dir
    try:
        source_dir = clip_source_dir(ctx.adapters.remote, ctx.state, requested=ctx.source_dir,
                                     default_dir=f"{rd.rsplit('/', 1)[0]}/sources")
    except SourceMissing as exc:
        return _fail("SOURCE_RECORDING_MISSING", str(exc))
    src = f"{source_dir}/{clip['source_name']}"
    snap = _snap_cut_points(ctx, src)
    start, dur = clip["start_ms"] / 1000, (clip["end_ms"] - clip["start_ms"]) / 1000
    cut = f"{rd}/cut/cut.mp4"
    # The cut is the lane's one definition (source_read.source_cut_script): as root, seek ahead coarsely then exactly,
    # bounded constant-rate output, audio timestamp gaps filled without shifting speech.
    body = (f"mkdir -p {rd}/cut\n(\n{source_cut_script(src, start_s=start, duration_s=dur, output=cut)})\n"
            "rc=$?\n"
            f"ffprobe -v error -of json -show_format -show_streams {cut} > {rd}/cut/probe.json\n"
            "exit $rc\n")
    snap_process = [f"切点吸附：起点 {snap['start']}；终点 {snap['end']}"]
    res = _run_remote(ctx, "s2-cut", body, timeout=3600)
    if res.rc != 0:
        return _fail("CUT_FFMPEG_FAILED", f"rc={res.rc} stderr={res.stderr[-400:]}", process=snap_process)
    local_probe = _local(ctx, "cut/probe.json")
    ctx.adapters.remote.get(f"{rd}/cut/probe.json", local_probe)
    summary = _probe_summary(cast(ProbeDocument, json.loads(local_probe.read_text(encoding="utf-8"))))
    verdict = gates.cut_gate(probe=summary, expected_seconds=dur)
    if not verdict.ok:
        return _fail(verdict.reason_code, verdict.detail, process=snap_process)
    return StepOutcome(True, products={"cut_probe": str(local_probe)},
                       note=f"cut {summary['duration']:.3f}s {summary['width']}x{summary['height']} on OCI3 {cut}",
                       process=snap_process + [f"剪出 {summary['duration']:.3f}s，{summary['width']}x{summary['height']}"
                                               f"（OCI3 {cut}）"])
