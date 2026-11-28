"""Steps 1, 0 and 2: boundary (CPA judges the cut and it is adopted), external duplicate check, cut on OCI3.

Split out of stage_impl.py (TASK-13); stage_impl re-exports every name, so callers and tests keep
`stage_impl.<name>`.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping, MutableMapping, Sequence
from typing import TYPE_CHECKING, Any, TypedDict, cast

from . import (
    compilation,
    gates,
    normalize,
)
from .adapters import PROFILE_ENV, REMOTE_PY, REMOTE_REPO, root_script
from .clip_init import clip_entry
from .discover_select import MUST_CUT, OWNER_MARK
from .runner import Ctx, StepOutcome
from .source_read import (
    SourceMissing,
    clip_source_dir,
    source_audio_probe_script,
    source_cut_script,
)
from .srt_text import SrtCue, _context_lines, _json_from_text, format_srt, window_cues
from .state import RECUT_CLEANUP_FAILED, RECUT_PENDING, StateError, recut_cleanup_needed
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
    from src.autoslice.cpa_gemini_client import JsonValue

    from .state import ClipInfo, CpaEvent


# ----- step 1: boundary (CPA judges the cut and it is adopted) -------------------------------
BOUNDARY_CONTEXT_CUES = 12
BOUNDARY_RETRY_EXTRA_CUES = 12
BOUNDARY_MAX_RETRIES = 1
BOUNDARY_VERDICTS = frozenset({"ACCEPT", "REJECT"})


class _ParsedBoundary(TypedDict):
    raw: dict[str, JsonValue]
    verdict: str
    start_cue: int | None
    end_cue: int | None
    reason: str


class _BoundaryAttempt(TypedDict):
    call_id: str
    verdict: str
    start_cue: int | None
    end_cue: int | None
    reason: str
    accepted: bool
    problem: str


class _BoundaryAccepted(TypedDict):
    start_cue: int
    end_cue: int
    held: list[int]
    judged: dict[str, JsonValue]
    rejected_proposal: tuple[int, int] | None
    attempts: list[_BoundaryAttempt]
    records: list[CpaEvent]
    process: list[str]


class _ExtendSide(TypedDict, total=False):
    start_cue: int | None
    end_cue: int | None
    link: str
    why: str


class _ExtendAnswer(TypedDict, total=False):
    before: _ExtendSide | None
    after: _ExtendSide | None


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


def _peak_cues(cues: Sequence[SrtCue], peaks: Sequence[Mapping[str, Any]]) -> list[tuple[int, str]]:
    """(cue id, quote) of each of the selection's key moments (`peaks`, core_draws: `at_ms` is the start of the moment's
    cue on the recording's clock, the clock the clip's BCUT cues are on).  A peak no cue holds breaks that contract."""
    found = []
    for peak in peaks:
        at = int(peak["at_ms"])
        cue = next((c for c in cues if round(float(c["start_s"]) * 1000) <= at < round(float(c["end_s"]) * 1000)), None)
        if cue is None:
            raise ValueError(f"peak at {at} ms 「{peak['quote']}」 is in no cue of the clip's subtitles")
        found.append((int(cue["id"]), str(peak["quote"])))
    return found


def _cue_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if math.isfinite(value) and value.is_integer() else None
    if isinstance(value, str) and re.fullmatch(r"\s*-?\d+\s*", value):
        try:
            return int(value)
        except ValueError:
            return None
    return None


def _boundary_shown(cues: Sequence[SrtCue], start_cue: int, end_cue: int, *, widened: bool = False) -> list[SrtCue]:
    by_id = {c["id"]: c for c in cues}
    if widened and start_cue in by_id and end_cue in by_id:
        return _extend_shown(cues, start_cue, end_cue)
    lo, hi = start_cue - BOUNDARY_CONTEXT_CUES, end_cue + BOUNDARY_CONTEXT_CUES
    return [c for c in cues if lo <= c["id"] <= hi]


def _retry_shown(
    cues: Sequence[SrtCue],
    first_shown: Sequence[SrtCue],
    p_start: int,
    p_end: int,
) -> list[SrtCue]:
    """Strictly widen every side of `first_shown` that still has cues in `cues`, unioned with `_extend_shown`."""
    if not cues:
        return []
    wanted = {c["id"] for c in first_shown}
    id_to_idx = {c["id"]: i for i, c in enumerate(cues)}
    first_indices = [id_to_idx[c["id"]] for c in first_shown if c["id"] in id_to_idx]
    if first_indices:
        lo_idx = max(0, min(first_indices) - BOUNDARY_RETRY_EXTRA_CUES)
        hi_idx = min(len(cues) - 1, max(first_indices) + BOUNDARY_RETRY_EXTRA_CUES)
        wanted.update(c["id"] for c in cues[lo_idx:hi_idx + 1])
    if p_start in id_to_idx and p_end in id_to_idx:
        wanted.update(c["id"] for c in _extend_shown(cues, p_start, p_end))
    return [c for c in cues if c["id"] in wanted]


def _boundary_prompt(
    clip: ClipInfo,
    cues: Sequence[SrtCue],
    start_cue: int,
    end_cue: int,
    peaks: Sequence[tuple[int, str]],
    *,
    feedback: str = "",
    shown: Sequence[SrtCue] | None = None,
) -> str:
    context_cues = shown if shown is not None else _boundary_shown(cues, start_cue, end_cue)
    return _render(
        "boundary.md",
        start_cue=str(start_cue),
        end_cue=str(end_cue),
        hook=clip["hook"],
        peaks=_peak_lines(peaks),
        feedback=(f"\n{feedback}\n" if feedback else ""),
        context=_context_lines(context_cues),
    )


def _boundary_retry_feedback(
    p_start: int,
    p_end: int,
    prev_reason: str,
    problem: str,
    shown: Sequence[SrtCue],
) -> str:
    lo = shown[0]["id"] if shown else p_start
    hi = shown[-1]["id"] if shown else p_end
    return (
        f"上一轮审校退回说明：\n"
        f"- 原选题起止 {p_start}–{p_end} 已被判 REJECT，绝对不得原样采用。\n"
        f"- 上一轮理由：{prev_reason or '（未写理由）'}。\n"
        f"- 上一轮切点未采纳原因：{problem}。\n"
        f"- 请在下方扩展字幕窗口（cue {lo}–{hi}）内重新给出完整故事边界：\n"
        f"  1) 开头：向前纳入更长的前情铺垫，使开场无需直播上下文也能独立理解；\n"
        f"  2) 结尾：移到完整句子的边界——若当前句在下方允许窗口（cue {lo}–{hi}）内说完，则向后延到该句句尾；"
        f"否则回退到上一个已说完整的句子末尾，不得截在半句话上；\n"
        f"  3) 所给起止须不同于 {p_start}–{p_end}、全部落在下方窗口 cue {lo}–{hi} 内、保留全部关键句且总时长不超过 "
        f"{gates.CLIP_MAX_SECONDS / 60:.0f} 分钟；若窗口内仍找不到合格边界，判 REJECT 并将 start_cue 与 end_cue 置为 null。"
    )


def _peak_lines(peaks: Sequence[tuple[int, str]]) -> str:
    return "\n".join(f"{cue} {quote}" for cue, quote in peaks) or "（选片没有列关键句）"


def _extend_shown(cues: Sequence[SrtCue], start_cue: int, end_cue: int) -> list[SrtCue]:
    """The lines the second question shows around a short story: CLIP_MIN_SECONDS beyond it on each side, at least 12
    cues.  A side can only be widened within them (`_extension`): the model is not asked about lines it did not see."""
    by_id = {c["id"]: c for c in cues}
    t0 = float(by_id[start_cue]["start_s"]) - gates.CLIP_MIN_SECONDS
    t1 = float(by_id[end_cue]["end_s"]) + gates.CLIP_MIN_SECONDS
    return [c for c in cues if start_cue - 12 <= c["id"] <= end_cue + 12 or t0 <= float(c["start_s"]) <= t1]


def _extend_prompt(clip: ClipInfo, cues: Sequence[SrtCue], start_cue: int, end_cue: int, seconds: float,
                   peaks: Sequence[tuple[int, str]]) -> str:
    """The second question, asked only of a story under the talk minimum: is there interaction on the same matter right
    before or after it, within the lines `_extend_shown` gives."""
    return _render("boundary_extend.md", story_start=str(start_cue), story_end=str(end_cue), story_seconds=f"{seconds:.1f}",
                   min_seconds=f"{gates.CLIP_MIN_SECONDS:.0f}", hook=clip["hook"], peaks=_peak_lines(peaks),
                   link_chars=str(LINK_MIN_CHARS), context=_context_lines(_extend_shown(cues, start_cue, end_cue)))


_NOT_TEXT = re.compile(r"[\s，。！？、,.!?~…「」“”\"']")
# a link of fewer distinct characters is laughter or a filler found anywhere (「哈哈哈」「可以可以」): it ties nothing to the story.
# The links of the 11-23 replays that tied a side were 8 and more (「哎呀呀，你怎么作弊啊」).
LINK_MIN_CHARS = 4


def _extension(cues: Sequence[SrtCue], start_cue: int, end_cue: int, answer: _ExtendAnswer) -> tuple[int, int, list[str]]:
    """The story widened by the sides the answer takes in, and why each side was or was not taken.  A side is taken only
    when it moves outward to a line the question showed (`_extend_shown`) and its `link` - at least LINK_MIN_CHARS distinct
    characters - is found in the lines it adds.  This bounds what a side can add (at most the shown lines) and makes the
    model quote it; whether the quote is the same matter is still the model's reading, written down in decision.json."""
    shown = {c["id"] for c in _extend_shown(cues, start_cue, end_cue)}
    text = {c["id"]: _NOT_TEXT.sub("", str(c["text"])) for c in cues}
    new_start, new_end, notes = start_cue, end_cue, []
    for side, key in (("before", "start_cue"), ("after", "end_cue")):
        got = answer.get(side)
        if got is not None and not isinstance(got, Mapping):
            notes.append(f"{side}：格式异常（非对象），不并")
            continue
        row = cast(_ExtendSide, got) if isinstance(got, Mapping) else cast(_ExtendSide, {})
        cue, link = _cue_int(row.get(key)), _NOT_TEXT.sub("", str(row.get("link") or ""))
        if cue is None or (cue >= start_cue if side == "before" else cue <= end_cue):
            notes.append(f"{side}：不并")
            continue
        if cue not in shown:
            notes.append(f"{side}：要并到 cue {cue}，超出了给它看的上下文，不并")
            continue
        added = range(cue, start_cue) if side == "before" else range(end_cue + 1, cue + 1)
        if len(set(link)) < LINK_MIN_CHARS or link not in "".join(text.get(i, "") for i in added):
            notes.append(f"{side}：要并到 cue {cue}，但所摘原话「{row.get('link', '')}」不在新并的句子里"
                         f"或不到 {LINK_MIN_CHARS} 个不同的字，不并")
            continue
        if side == "before":
            new_start = cue
        else:
            new_end = cue
        notes.append(f"{side}：并到 cue {cue}（「{row.get('link')}」：{row.get('why', '')}）")
    return new_start, new_end, notes


def _parse_boundary_answer(record: CpaEvent) -> tuple[_ParsedBoundary | None, str]:
    try:
        judged = _json_from_text(record["response_text"])
    except ValueError:
        return None, record["file"]
    if not isinstance(judged, Mapping):
        return None, f"{record['file']}: CPA answer is not a JSON object"
    verdict = judged.get("verdict")
    if not isinstance(verdict, str) or verdict.strip() not in BOUNDARY_VERDICTS:
        return None, f"{record['file']}: unexpected verdict {verdict!r}"
    return {
        "raw": dict(judged),
        "verdict": verdict.strip(),
        "start_cue": _cue_int(judged.get("start_cue")),
        "end_cue": _cue_int(judged.get("end_cue")),
        "reason": str(judged.get("reason") or "").strip(),
    }, ""


def _parse_extend_answer(record: CpaEvent) -> tuple[_ExtendAnswer | None, str]:
    try:
        answer = _json_from_text(record["response_text"])
    except ValueError:
        return None, record["file"]
    if not isinstance(answer, Mapping):
        return None, f"{record['file']}: extend answer is not a JSON object"
    return cast(_ExtendAnswer, answer), ""


def _validate_boundary_correction(
    cues: Sequence[SrtCue],
    *,
    proposal: tuple[int, int],
    shown_ids: set[int],
    peaks: Sequence[tuple[int, str]],
    start_cue: int | None,
    end_cue: int | None,
) -> tuple[tuple[int, int, list[int]] | None, str]:
    if start_cue is None or end_cue is None:
        return None, "未给出有效整数起止 cue"
    if (start_cue, end_cue) == proposal:
        return None, f"仍给出原被拒切点 {proposal[0]}–{proposal[1]}"
    by_id = {c["id"]: c for c in cues}
    if start_cue not in by_id or end_cue not in by_id or start_cue > end_cue:
        return None, f"修正切点 {start_cue}–{end_cue} 不在源段内或起止倒置"
    if start_cue not in shown_ids or end_cue not in shown_ids:
        return None, f"修正切点 {start_cue}–{end_cue} 超出了展示的字幕上下文"
    raw_duration = float(by_id[end_cue]["end_s"]) - float(by_id[start_cue]["start_s"])
    if raw_duration <= 0:
        return None, f"修正切点 {start_cue}–{end_cue} 时长非正（{raw_duration:.1f}s）"
    held = [cue for cue, _ in peaks if not start_cue <= cue <= end_cue]
    final_start, final_end = (min(start_cue, *held), max(end_cue, *held)) if held else (start_cue, end_cue)
    if (final_start, final_end) == proposal:
        return None, f"保留关键句后仍退回原被拒切点 {proposal[0]}–{proposal[1]}"
    duration = float(by_id[final_end]["end_s"]) - float(by_id[final_start]["start_s"])
    if duration <= 0:
        return None, f"修正切点 {final_start}–{final_end} 时长非正（{duration:.1f}s）"
    if duration > gates.CLIP_MAX_SECONDS:
        return None, f"修正切点 {final_start}–{final_end} 时长 {duration:.1f}s 超过上限 {gates.CLIP_MAX_SECONDS / 60:.0f} 分钟"
    return (final_start, final_end, held), ""


def _accept_initial_boundary(
    call_id: str,
    parsed: _ParsedBoundary,
    peaks: Sequence[tuple[int, str]],
    records: list[CpaEvent],
    attempts: list[_BoundaryAttempt],
    process: list[str],
) -> _BoundaryAccepted | StepOutcome:
    start_cue, end_cue, reason = parsed["start_cue"], parsed["end_cue"], parsed["reason"]
    if start_cue is None or end_cue is None:
        return _fail("BOUNDARY_NO_CUT_PAIR", f"CPA gave no start_cue/end_cue: {str(parsed['raw'])[:200]}",
                     cpa=records, process=process)
    held = [cue for cue, _ in peaks if not start_cue <= cue <= end_cue] if start_cue <= end_cue else []
    if held:
        start_cue, end_cue = min(start_cue, *held), max(end_cue, *held)
    attempts.append({"call_id": call_id, "verdict": parsed["verdict"], "start_cue": start_cue, "end_cue": end_cue,
                     "reason": reason, "accepted": True, "problem": ""})
    return {"start_cue": start_cue, "end_cue": end_cue, "held": held, "judged": parsed["raw"],
            "rejected_proposal": None, "attempts": attempts, "records": records, "process": process}


def _judge_boundary(
    ctx: Ctx,
    clip: ClipInfo,
    cues: Sequence[SrtCue],
    *,
    p_start: int,
    p_end: int,
    peaks: Sequence[tuple[int, str]],
) -> _BoundaryAccepted | StepOutcome:
    records: list[CpaEvent] = []
    attempts: list[_BoundaryAttempt] = []
    process = [f"提议切点（输入）：{p_start}–{p_end}"]
    first_shown = _boundary_shown(cues, p_start, p_end)
    shown = first_shown
    rejected_proposal: tuple[int, int] | None = None
    feedback, last_problem, last_reason = "", "", ""
    for round_no in range(BOUNDARY_MAX_RETRIES + 1):
        call_id = "boundary" if round_no == 0 else f"boundary-retry-{round_no}"
        prompt = _boundary_prompt(clip, cues, p_start, p_end, peaks, feedback=feedback, shown=shown)
        record = _ask_cpa(ctx, call_id, prompt, purpose="boundary")
        records.append(record)
        if not _cpa_ok(record):
            return _fail("CPA_CALL_FAILED", record.get("stderr_tail", ""), cpa=records, process=process)
        parsed, parse_err = _parse_boundary_answer(record)
        if parsed is None:
            return _fail("CPA_RESPONSE_UNPARSEABLE", parse_err, cpa=records, process=process)
        verdict, start_cue, end_cue, reason = (
            parsed["verdict"], parsed["start_cue"], parsed["end_cue"], parsed["reason"],
        )
        prefix = "CPA 判定" if round_no == 0 else f"CPA 重审（{call_id}）"
        process.append(f"{prefix}：{verdict}；理由：{reason}")
        if round_no == 0 and verdict == "ACCEPT":
            return _accept_initial_boundary(call_id, parsed, peaks, records, attempts, process)
        if verdict == "REJECT":
            rejected_proposal = (p_start, p_end)
        checked, problem = _validate_boundary_correction(
            cues, proposal=(p_start, p_end), shown_ids={c["id"] for c in shown},
            peaks=peaks, start_cue=start_cue, end_cue=end_cue,
        )
        attempts.append({"call_id": call_id, "verdict": verdict, "start_cue": start_cue, "end_cue": end_cue,
                         "reason": reason, "accepted": checked is not None, "problem": problem})
        if checked is not None:
            final_start, final_end, held = checked
            return {"start_cue": final_start, "end_cue": final_end, "held": held, "judged": parsed["raw"],
                    "rejected_proposal": rejected_proposal, "attempts": attempts, "records": records, "process": process}
        last_problem, last_reason = problem, reason
        process.append(f"{call_id} 未采用（{problem}）")
        if round_no < BOUNDARY_MAX_RETRIES:
            next_shown = _retry_shown(cues, first_shown, p_start, p_end)
            if {c["id"] for c in next_shown} == {c["id"] for c in first_shown}:
                process.append(f"{call_id} 两侧均无更多可用字幕，跳过扩窗重试")
                break
            shown = next_shown
            feedback = _boundary_retry_feedback(p_start, p_end, reason, problem, next_shown)
    return _fail(
        gates.BOUNDARY_NOT_ACCEPTED,
        f"边界审查否决原提议 {p_start}–{p_end}，重试后仍未得到有效切点：{last_problem}（最后理由：{last_reason}）",
        cpa=records,
        process=process,
    )


def _maybe_extend_short_story(
    ctx: Ctx,
    clip: ClipInfo,
    cues: Sequence[SrtCue],
    *,
    start_cue: int,
    end_cue: int,
    peaks: Sequence[tuple[int, str]],
    verdict: gates.Verdict,
    rejected_proposal: tuple[int, int] | None,
    records: list[CpaEvent],
    process: list[str],
) -> tuple[int, int, gates.Verdict, dict[str, Any] | None] | StepOutcome:
    if (verdict.reason_code != "BOUNDARY_LENGTH_UNREASONABLE"
            or not 0 < verdict.evidence["duration_s"] < gates.CLIP_MIN_SECONDS):
        return start_cue, end_cue, verdict, None
    # a story under the minimum: the interaction it belongs to on either side, asked on its own (30-boundary.md
    # 不足 60 秒的必切片) so that the first judgement of every clip is not pulled toward the minimum
    story_s = verdict.evidence["duration_s"]
    second = _ask_cpa(ctx, "boundary-extend", _extend_prompt(clip, cues, start_cue, end_cue, story_s, peaks),
                      purpose="boundary")
    records.append(second)
    if not _cpa_ok(second):
        return _fail("CPA_CALL_FAILED", second.get("stderr_tail", ""), cpa=records, process=process)
    answer, ext_err = _parse_extend_answer(second)
    if answer is None:
        return _fail("CPA_RESPONSE_UNPARSEABLE", ext_err, cpa=records, process=process)
    ext_start, ext_end, notes = _extension(cues, start_cue, end_cue, answer)
    if rejected_proposal is not None and (ext_start, ext_end) == rejected_proposal:
        notes.append(f"扩展后 {ext_start}–{ext_end} 退回被审查否决的原提议 {rejected_proposal[0]}–{rejected_proposal[1]}，不应用本次扩展")
        process.append(f"故事 {start_cue}–{end_cue} 只有 {story_s:.1f}s，问两边同一件事的互动：{'；'.join(notes)}")
        return start_cue, end_cue, verdict, {"story_s": story_s, "answer": answer, "notes": notes}
    process.append(f"故事 {start_cue}–{end_cue} 只有 {story_s:.1f}s，问两边同一件事的互动：{'；'.join(notes)}")
    return ext_start, ext_end, gates.boundary_sanity(cues, start_cue=ext_start, end_cue=ext_end), {
        "story_s": story_s, "answer": answer, "notes": notes,
    }


def _must_cut(clip: ClipInfo) -> bool:
    """Whether the clip was seated as a must-cut (an owner's mark or a must-cut core draw, discover_select): such a clip
    is queued when its story is short, not dropped.  A clip made without a selection entry (by hand) is not."""
    entry = clip_entry(clip)
    return entry is not None and entry.get("seat_reason") in (MUST_CUT, OWNER_MARK)


def _compilation_boundary(ctx: Ctx, manifest: compilation.Manifest) -> StepOutcome:
    """A same-session compilation has no boundary review: its intervals were chosen and cut at cue boundaries by the
    selection step (compilation.json).  The review judges one continuous story's start and end and the 60 s-10 min
    check is that story's size, so on a compilation it would trim the joined clip to another range (or stop it); the
    whole joined file is the candidate."""
    clip = _clip(ctx)
    adopted = {"start_cue": clip["start_cue"], "end_cue": clip["end_cue"], "start_ms": clip["start_ms"],
               "end_ms": clip["end_ms"]}
    if manifest.get("kind") == compilation.KIND_TRIM:
        reason = (f"片中剪除：本片由 {manifest['origin']['cid']} 挖掉与主线无关的话题段或致谢后拼成，"
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
    peaks = _peak_cues(cues, clip.get("peaks") or [])
    judged_res = _judge_boundary(ctx, clip, cues, p_start=p_start, p_end=p_end, peaks=peaks)
    if isinstance(judged_res, StepOutcome):
        return judged_res
    start_cue, end_cue, held = judged_res["start_cue"], judged_res["end_cue"], judged_res["held"]
    judged, rejected_proposal, attempts, records, process = (
        judged_res["judged"], judged_res["rejected_proposal"], judged_res["attempts"],
        judged_res["records"], judged_res["process"],
    )
    if held:
        process.append(f"CPA 的切点把选片关键句 cue {held} 切到了片外，按四命题 1 放宽到 {start_cue}–{end_cue}")
    verdict = gates.boundary_sanity(cues, start_cue=start_cue, end_cue=end_cue)
    story = {"start_cue": start_cue, "end_cue": end_cue}
    extended = _maybe_extend_short_story(
        ctx, clip, cues, start_cue=start_cue, end_cue=end_cue, peaks=peaks,
        verdict=verdict, rejected_proposal=rejected_proposal, records=records, process=process,
    )
    if isinstance(extended, StepOutcome):
        return extended
    start_cue, end_cue, verdict, extend = extended
    short = (verdict.reason_code == "BOUNDARY_LENGTH_UNREASONABLE"
             and 0 < verdict.evidence["duration_s"] < gates.CLIP_MIN_SECONDS and _must_cut(clip))
    if not verdict.ok and not short:
        return _fail(verdict.reason_code, verdict.detail, cpa=records, process=process)
    if rejected_proposal is not None and (start_cue, end_cue) == rejected_proposal:
        return _fail(
            gates.BOUNDARY_NOT_ACCEPTED,
            f"拒绝原样采用被审查否决的提议切点 {rejected_proposal[0]}–{rejected_proposal[1]}",
            cpa=records,
            process=process,
        )
    by_id = {c["id"]: c for c in cues}
    start_ms = round(float(by_id[start_cue]["start_s"]) * 1000)
    end_ms = round(float(by_id[end_cue]["end_s"]) * 1000)
    adopted = {"start_cue": start_cue, "end_cue": end_cue, "start_ms": start_ms, "end_ms": end_ms}
    dur = verdict.evidence["duration_s"]
    decision = _local(ctx, "boundary/decision.json")
    decision.write_text(json.dumps({"proposal": [p_start, p_end], "cpa": judged, "adopted": adopted,
                                    **({"attempts": attempts} if len(attempts) > 1 else {}),
                                    **({"peaks_kept": held} if held else {}),
                                    **({"story": story, "extend": extend} if extend is not None else {}),
                                    **({"short": {"duration_s": dur, "queue": gates.BOUNDARY_SHORT_QUEUED}} if short else {})},
                                   ensure_ascii=False, indent=2), encoding="utf-8")
    logged = [_log(r) for r in records]
    if short:
        # not adopted as a clip: the cut is kept in decision.json for the compilation queue (short_queue.py)
        return StepOutcome(False, reason=gates.BOUNDARY_SHORT_QUEUED, cpa=logged,
                           note=f"必切片 {start_cue}–{end_cue} 只有 {dur:.1f}s，两边没有更多同一件事的内容：进当晚同题材合集队列（不是题材的看点等人处理）",
                           process=[*process, f"{start_ms / 1000:.2f}s–{end_ms / 1000:.2f}s，{dur:.1f}s，"
                                              f"不到 {gates.CLIP_MIN_SECONDS:.0f}s；必切，不丢，进合集队列"])
    clip["start_cue"], clip["end_cue"], clip["start_ms"], clip["end_ms"] = start_cue, end_cue, start_ms, end_ms
    note_verb = "判定" if judged.get("verdict") == "ACCEPT" and len(attempts) == 1 else "修正"
    return StepOutcome(True, products={"boundary": str(decision)}, cpa=logged,
                       note=f"CPA {note_verb}切点 {start_cue}–{end_cue}，采用",
                       process=[*process, f"采用切点 {start_cue}–{end_cue}：{start_ms / 1000:.2f}s–{end_ms / 1000:.2f}s，"
                                          f"时长 {dur:.1f}s（源段内、时长在 {gates.CLIP_MIN_SECONDS:.0f}s–"
                                          f"{gates.CLIP_MAX_SECONDS / 60:.0f}min 内，常识检查通过）"])



def _source_intervals(ctx: Ctx, clip: ClipInfo, start: float, end: float) -> list[dict[str, str | int | float]]:
    """The real recording intervals of the clip: one for an ordinary clip; for a same-session compilation, every
    piece's own recording and interval (not the joined file)."""
    manifest = compilation.load_manifest(ctx.clip_dir)
    if manifest is None:
        return [{"recording": clip["source_name"], "room_id": ROOM_ID, "start_seconds": start, "end_seconds": end}]
    return [{"recording": row["recording"], "room_id": ROOM_ID, "start_seconds": row["source_start_ms"] / 1000,
             "end_seconds": row["source_end_ms"] / 1000}
            for row in compilation.source_intervals(manifest, round(start * 1000), round(end * 1000))]


def _dedupe_log_tail(ctx: Ctx) -> str:
    """The end of the checker's own log (dedup/run.log on the media host), '' when it cannot be read."""
    local = _local(ctx, "dedup/run.log")
    try:
        ctx.adapters.remote.get(f"{ctx.remote_dir}/dedup/run.log", local)
        return local.read_text(encoding="utf-8", errors="replace").strip()[-400:]
    except (OSError, RuntimeError):
        return ""


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
    # As root, like step 10's recheck: the checker reads this host's upload login to know our own uploads, and that
    # file is root's (11-20: run as the lane's user it could not open it and every clip of the night stopped here).
    cmd = (f"cd {REMOTE_REPO} && env {PROFILE_ENV} {REMOTE_PY} -B scripts/check_external_duplicate_clips.py "
           f"--package {rd}/dedup/package.json --output {rd}/dedup/result.json "
           f"--evidence-dir {rd}/dedup/evidence --audio-work-dir {rd}/dedup/audio --upload-account if-configured "
           f"> {rd}/dedup/run.log 2>&1")
    body = (f"mkdir -p {rd}/dedup/evidence {rd}/dedup/audio\n"
            + root_script(cmd.replace("'", "'\\''"), env=(), owned=f"{rd}/dedup", recursive=True))
    res = _run_remote(ctx, "s0-dedupe", body)
    # Exit 3 is the checker's own "block" signal; the decision in result.json is what counts.  Any other exit is the
    # checker failing: its own log says why.
    if res.rc not in (0, 3):
        return _fail("DEDUPE_TOOL_FAILED", _dedupe_log_tail(ctx) or f"{res.stdout[-300:]}{res.stderr[-300:]}")
    local_result = _local(ctx, "dedup/result.json")
    ctx.adapters.remote.get(f"{rd}/dedup/result.json", local_result)
    result = json.loads(local_result.read_text(encoding="utf-8"))
    verdict = gates.dedupe_gate(result)
    products = {"dedup_result": str(local_result)}
    if not verdict.ok:
        return StepOutcome(False, reason=verdict.reason_code, products={}, note=verdict.detail,
                           process=["外片查重：完全重复，按 H62 不传"])
    covered = gates.coverage_gate(result)
    found = [f"{v.get('bvid')}「{v.get('title')}」" for v in result.get("videos", []) if not v.get("excluded")]
    seen = f"同场他人视频 {len(found)} 条" + (f"：{'；'.join(found[:8])}" if found else "") + f"；{gates.overlap_line(result)}"
    if not covered.ok:
        return StepOutcome(False, reason=covered.reason_code, products=products, note=covered.detail,
                           process=[seen, f"已被覆盖、没有新意（20-selection §2b）：{covered.detail}"])
    return StepOutcome(True, products=products, note=f"查重结论 {verdict.evidence.get('decision')}",
                       process=[seen, f"查重结论 {verdict.evidence.get('decision')}（H62：只有完全重复才拦；没有被覆盖到没新意）"])


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
    if recut_cleanup_needed(ctx.state.data):
        from .redo import invalidate_for_recut

        try:
            invalidate_for_recut(ctx)
        except compilation.CompilationError as exc:
            return _fail(exc.code, str(exc))
        except SourceMissing as exc:
            return _fail("SOURCE_RECORDING_MISSING", str(exc))
        except StateError as exc:
            return _fail(RECUT_CLEANUP_FAILED, str(exc))
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
    if ctx.state.data.get(RECUT_PENDING):
        with ctx.state.lock:
            cast("MutableMapping[str, Any]", ctx.state.data).pop(RECUT_PENDING, None)
            ctx.state.save()
    return StepOutcome(True, products={"cut_probe": str(local_probe)},
                       note=f"cut {summary['duration']:.3f}s {summary['width']}x{summary['height']} on OCI3 {cut}",
                       process=snap_process + [f"剪出 {summary['duration']:.3f}s，{summary['width']}x{summary['height']}"
                                               f"（OCI3 {cut}）"])
