"""Step 3, variant C (TASK-5, U119): the old pipeline's goal in the new code, with no extra gates.

1. Detect: BCUT and the jianying ASR disagree on the cue, or the ledger / entity-confusable lexicon hits the cue, or
   CPA's full-text review marks the cue (unreadable, contradicts its context, a name or work mis-heard).  Those cues
   are the doubts.  No other cue is listened to.
2. Listen only the doubt windows, by escalation: 3.8 Low first; if CPA cannot settle the doubt, argon.  Gemini
   3.8 and argon are one family (U122: only a change of vendor is another family).  A doubt stops at the first level that settles it.  At most two levels; at the limit
   the doubt is recorded with its best reading and is NOT blocked (soft budget: accounted, never refused).
3. Accounting goes into PROCESS: doubts, calls per level (new and from cache), soft-budget use, thinking tokens from
   the CPA ledger, and a warning line when a step's output + thinking tokens pass 500 000 (warning only, never a stop).

Reuses stage_impl's helpers and the doubt rows of step3_doubts; step 3 dispatches here when MANUAL_LANE_ADJUDICATE_MODE=C.
"""

from __future__ import annotations

import functools
import datetime as dt
import json
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from itertools import pairwise
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, NotRequired, TypedDict, cast

from . import doubt_policy, evidence_attach, lane_lines, mishearing, name_consistency, name_match, normalize, suspicion
from .lane_rules import rules
from .layout import layout
from .recorder_events import evidence_texts
from .runner import Ctx, StepOutcome
from .srt_text import _context_lines
from .step3_doubts import (
    decided_doubt,
    doubt_entry,
    evidence_extra,
    gated_outcome,
    lone_and_holds,
    state_counts,
    write_subtitles,
)

if TYPE_CHECKING:
    from src.autoslice.cpa_gemini_client import JsonValue
    from .gates import AnchorLabel
    from .speaker_v8 import MixedSpeakers
    from .srt_text import SrtCue
    from .state import ClipInfo, CpaEvent
    from .step3_evidence import ListeningModel, ListeningRecord, ListeningWindow, OutsideWindow, WindowAsrRecord
    from .work_lookup import LookupResult

USAGE_SCRIPT = Path(__file__).resolve().parents[3] / "assets" / "manual_lane" / "remote" / "usage_ledger.py"
TOKEN_WARN_LIMIT = 500_000


class GeminiUsage(TypedDict):
    purpose: str
    window: list[str]
    calls: int
    responded: int
    not_responded: int
    input_tokens: int
    output_tokens: int
    thinking_tokens: int
    total_tokens: int
    output_plus_thinking: int
    per_model: dict[str, dict[str, int]]


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def gemini_usage(ctx: Ctx, since: str) -> tuple[GeminiUsage | None, str]:
    """Listening usage since `since` from the CPA ledger on OCI3 (read-only).  Returns (usage, error text)."""
    from . import stage_impl as si

    rd = ctx.remote_dir
    ctx.adapters.remote.put(USAGE_SCRIPT, f"{rd}/usage_ledger.py")
    body = (f"timeout 120 python3 {rd}/usage_ledger.py manual_lane_subtitle_listen {since} 9999 "
            f"{layout().usage_ledger}\n")
    res = si._run_remote(ctx, "s3-usage", body, timeout=300)
    lines = [ln for ln in res.stdout.splitlines() if ln.startswith("{")]
    if res.rc != 0 or not lines:
        return None, f"rc={res.rc} stdout={res.stdout[-160:]} stderr={res.stderr[-160:]}"
    return cast(GeminiUsage, json.loads(lines[-1])), ""


def usage_lines(usage: GeminiUsage | None, error: str) -> list[str]:
    if usage is None:
        return [f"Gemini 用量统计失败（台账读取不成功，不影响本步）：{error}"]
    total = int(usage["output_plus_thinking"])
    lines = [f"Gemini 用量（本步台账，听写）：调用 {usage['calls']}（成功 {usage['responded']}，未成功 {usage['not_responded']}），"
             f"输入 {usage['input_tokens']}，输出 {usage['output_tokens']}，思考 {usage['thinking_tokens']}，"
             f"输出加思考 {total}"]
    if total > TOKEN_WARN_LIMIT:
        lines.append(f"WARN 用量：本步输出加思考 {total} token，超过 {TOKEN_WARN_LIMIT}（只告警，不停跑）")
    return lines


@dataclass
class _Detection:
    """What step 1 of variant C found: the evidence attached to every cue and the doubts with their reasons."""
    by_id: dict[int, SrtCue]
    full_source_context: str
    asr_lines: dict[str, list[evidence_attach.WitnessLine]]
    danmaku: list[evidence_attach.Danmaku]
    jy_att: evidence_attach.AsrAttachment
    witness_att: Mapping[str, evidence_attach.AsrAttachment]
    jy_by_cue: dict[int, str]
    mixed: dict[int, MixedSpeakers]
    reasons: dict[int, list[str]]
    lines: lane_lines.LinePlan | None = None


class _LatestDecision(TypedDict):
    doubt: doubt_policy.FinalDoubt
    decided: doubt_policy.Decision
    row: doubt_policy.CpaAnswer | None
    level: str


class _PriorDecision(TypedDict):
    cue_id: int
    start_s: float
    end_s: float
    source_text: str
    level: str
    status: Literal["settled", "provisional"]
    state: str | None
    final_text: str | None
    dropped: bool | None
    other_speaker_text: NotRequired[JsonValue]
    drop: NotRequired[JsonValue]


class _ListenRequest(TypedDict):
    cue_id: int
    reason: str


@dataclass
class _Escalation:
    """The state carried from one escalation level to the next."""
    pending: set[int]
    latest: dict[int, _LatestDecision] = field(default_factory=dict)
    level_stats: list[str] = field(default_factory=list)
    windows_listened: dict[int, float] = field(default_factory=dict)
    attempts: dict[int, int] = field(default_factory=dict)
    lines_by_source: dict[str, list[evidence_attach.WitnessLine]] = field(default_factory=dict)
    audio_outside: list[OutsideWindow] = field(default_factory=list)
    lookup_cache: dict[str, LookupResult] = field(default_factory=dict)
    accumulated: list[ListeningRecord] = field(default_factory=list)
    asr_records: list[WindowAsrRecord] = field(default_factory=list)
    asr_billed: dict[str, dict[str, float]] = field(default_factory=dict)   # new window ASR calls per provider
    labelled: set[int] = field(default_factory=set)
    request_log: list[str] = field(default_factory=list)


def run_step3(ctx: Ctx, *, bcut: Sequence[SrtCue], jianying: Sequence[SrtCue],
              clip: ClipInfo, cut_len: float, labels: Mapping[int, AnchorLabel]) -> StepOutcome:
    from . import stage_impl as si

    cpa_calls: list[CpaEvent] = []
    process: list[str] = []
    det = _detect(ctx, si, bcut, jianying, cut_len, labels, cpa_calls, process)
    if isinstance(det, StepOutcome):
        return det
    esc = _Escalation(pending=set(det.reasons), labelled=set(labels))
    failure = _escalate(ctx, si, det, esc, bcut, clip, cut_len, cpa_calls, process)
    if failure is not None:
        return failure
    final_doubts = _assemble(ctx, si, det, esc, labels, process)



    name_record = name_consistency.resolve(ctx, final_doubts, [c["id"] for c in bcut], ask=functools.partial(si._ask_cpa, purpose="name-consistency"))
    name_calls = 1 if name_record.get("cpa_call") else 0
    cpa_calls.extend([cast("CpaEvent", name_record["cpa_call"])] if name_calls else [])
    process.append(name_consistency.summary_line(name_record))
    _account(si, esc, cpa_calls, process, name_calls=name_calls)

    # 4. write the subtitles, doubts and the gate ----------------------------------------------------------
    doubts_path, final_path, norm_log = write_subtitles(ctx, bcut, final_doubts, det.lines)
    counts = state_counts(final_doubts)
    process.append(f"四态计数 {counts}；规范化 {len(norm_log.get('canon_replacements', []))} 处替换，"
                   f"相邻句开头重复 {len(norm_log.get('adjacent_repeats', []))} 处（只记录，不删字）")
    process.append(name_match.unknown_line(
        [str(d.get("final_text") or "") for d in final_doubts], ctx.session.get("participants") or [],
        [text for row in det.danmaku for text in evidence_texts(row)] + [str(clip.get("hook") or "")],
        extra=name_consistency.undecided_rows(name_record)))
    lone_and_holds(ctx, norm_log, final_doubts, process)
    return gated_outcome(ctx, final_doubts, cpa_calls, process,
                         products={"doubts": str(doubts_path), "final_srt": str(final_path)}, extra_products={},
                         note=f"variant C: 疑点 {len(det.reasons)}，四态 {counts}, 未定 {len(esc.pending)}")


def _full_source_context(bcut: Sequence[SrtCue], jianying: Sequence[SrtCue],
                         danmaku: Sequence[evidence_attach.Danmaku]) -> str:
    """The same independently numbered source texts and timed comments for both C consumers."""
    return ("【BCUT 全片字幕：唯一可提名的编号】\n" + _context_lines(bcut)
            + "\n\n【剪映独立 ASR 全片字幕：编号仅供参考，不能提名】\n" + _context_lines(jianying)
            + "\n\n【弹幕（剪辑时间线 t_s / text）】\n"
            + json.dumps(danmaku, ensure_ascii=False, separators=(",", ":")))


def _review_prompt(ctx: Ctx, si: Any, full_source_context: str,
                   split_proposals: Sequence[lane_lines.SplitProposal]) -> str:
    instructions = cast(str, si._render("subtitle_review.md", participants=si._participant_line(ctx),
                                       confusions=si._confusion_lines(ctx))).rstrip()
    return (instructions + "\n\n【剪映有效分句提案】\n"
            + json.dumps(split_proposals, ensure_ascii=False, separators=(",", ":"))
            + "\n\n" + full_source_context)


def _detect(ctx: Ctx, si: Any, bcut: Sequence[SrtCue], jianying: Sequence[SrtCue],
            cut_len: float, labels: Mapping[int, AnchorLabel], cpa_calls: list[CpaEvent],
            process: list[str]) -> _Detection | StepOutcome:
    """1. evidence attachment and detection.  Every ASR segment, listening line and danmaku comment is attached by
    evidence_attach (the same rule as A/B).

    TASK-31: the whole-clip witnesses (OCI3) and the recording's danmaku (N100) are two hosts and two files, so the
    danmaku is read while the witnesses are still being recognised."""
    by_id = {int(c["id"]): c for c in bcut}
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="lane-danmaku") as pool:
        danmaku_job = pool.submit(si._danmaku_lines, ctx, cut_len)
        asr_lines, asr_status = si._asr_witnesses(ctx)
        danmaku = danmaku_job.result()
    process.append(si.witness_status_line(asr_status))
    jy_att = evidence_attach.attach_asr(bcut, evidence_attach.segments_from_srt_cues(jianying), source="jianying")
    lines = lane_lines.plan(bcut, jy_att)
    process.extend(lines.process_lines())
    witness_att = evidence_attach.own_witness_lines(bcut, asr_lines)
    jy_by_cue = {cid: jy_att.text(cid) for cid in by_id}
    notes = {cid: jy_att.notes(cid) + [n for att in witness_att.values() for n in att.notes(cid)] for cid in by_id}
    read_aloud = {cid: evidence_attach.read_aloud_hit(c, danmaku) for cid, c in by_id.items()}
    process.extend(si._evidence_process(jy_att, witness_att, danmaku, [cid for cid, h in read_aloud.items() if h]))
    # two people inside one sentence (U111, U129): v8 on ~1.2 s chunks of every cue, before anything is decided
    chunks, chunk_error = si._speaker_chunks(ctx, [*bcut, *lane_lines.chunk_cues(lines)])
    detected = si._mixed_by_cue(chunks)
    mixed = detected if si.two_speaker_doubts() else {}   # measured: off by default (TASK-6 C5/C5b)
    process.append(f"声纹分段（v8，约 1.2 秒一段）：{sum(len(v) for v in chunks.values())} 段，句内像有两人的 {len(detected)} 句"
                   + ("（作为疑点并分人听写）" if mixed else "（只记录，供第 4 步分说话人；两人疑点默认关闭）")
                   + (f"：cue {sorted(detected)}" if detected else "") + (f"；{chunk_error}" if chunk_error else ""))
    pairs = mishearing.load_pairs(si.MISHEARING_PAIRS)
    asr = {cid: suspicion.asr_disagrees(c["text"], jy_by_cue[cid]) for cid, c in by_id.items()}
    lex = {cid: suspicion.lexicon_hits(c["text"], pairs) for cid, c in by_id.items()}
    extra = detection_extra(ctx, by_id, jy_by_cue)
    split_proposals = lane_lines.effective_split_proposals(bcut, lines)
    full_source_context = _full_source_context(bcut, jianying, danmaku)
    review_prompt = _review_prompt(ctx, si, full_source_context, split_proposals)
    rec = si._ask_cpa(ctx, "review-c", review_prompt, purpose="review-c")
    cpa_calls.append(rec)
    if not si._cpa_ok(rec):
        return cast(StepOutcome, si._fail("CPA_CALL_FAILED", rec.get("stderr_tail", ""), cpa=cpa_calls))
    try:
        review = suspicion.parse_review(rec["response_text"])
    except ValueError:
        return cast(StepOutcome, si._fail("CPA_RESPONSE_UNPARSEABLE", rec["file"], cpa=cpa_calls))
    proposed_ids = {proposal["cue_id"] for proposal in split_proposals}
    for cid, reason in review.keep_together.items():
        if cid in proposed_ids:
            del lines.splits[cid]
            process.append(f"CPA 保留整句：cue {cid}，取消剪映分句；理由：{reason}")
    candidates = [cid for cid in by_id if cid not in labels]
    joins = {int(cast("SrtCue", c)["id"]): r for p, c in pairwise([None, *bcut])
             if (r := normalize.join_repeat(p, cast("SrtCue", c)))}
    reasons = suspicion.merge_suspicious(candidates, asr=asr, lexicon=lex,
                                         review={k: v for k, v in review.suspicious.items() if k in candidates},
                                         asr_notes=notes, read_aloud=read_aloud,
                                         speakers={k: v["note"] for k, v in mixed.items()}, joins=joins,
                                         extra=extra)
    process.append(f"疑点检测：BCUT 与剪映不一致 {sum(asr.values())} 句，对照表命中 {sum(1 for v in lex.values() if v)} 句，"
                   f"CPA 整片审读标出 {len(review.suspicious)} 句，ASR 漏句或句内另起一段 {sum(1 for v in notes.values() if v)} 句，"
                   f"与上一句相接处重复 {len(joins)} 句，"
                   f"疑似念弹幕 {sum(1 for v in read_aloud.values() if v)} 句，声纹显示句内两人 {len(mixed)} 句，"
                   f"剪映无字、近音名字或未知字母词 {sum(1 for v in extra.values() if v)} 句；"
                   f"合计疑点 {len(reasons)} 句，其余 "
                   f"{len(by_id) - len(reasons) - len(labels)} 句不听（直接用 BCUT 文字）")
    return _Detection(by_id=by_id, full_source_context=full_source_context, asr_lines=asr_lines,
                      danmaku=danmaku, jy_att=jy_att, witness_att=witness_att,
                      jy_by_cue=jy_by_cue, mixed=mixed, reasons=reasons, lines=lines)


def detection_extra(ctx: Ctx, by_id: Mapping[int, SrtCue], jy_by_cue: Mapping[int, str]
                    ) -> dict[int, list[str]]:
    """TASK-28 detection reasons (lane_rules detection; COACH e05 §1.1 第3/5/6条): jianying has no words where BCUT
    has (one_side_empty), a near-homophone of a name of the table, Latin letters that are no name of the table."""
    cfg = rules("detection")
    names = name_match.entries(ctx.session.get("participants") or [])
    out: dict[int, list[str]] = {}
    for cid, cue in by_id.items():
        why = ["剪映此处没有字（BCUT 有）"] if suspicion.one_side_empty(cue["text"], jy_by_cue.get(cid)) else []
        why += name_match.detection_reasons(str(cue["text"]), names, cfg)
        if why:
            out[cid] = why
    return out


def _plan_line(si: Any, levels: Sequence[tuple[str, str, Sequence[str]]]) -> str:
    return ("升级计划（" + suspicion.ladder() + "）：" + "；".join(
        f"{name} " + ("不加新证人，CPA 先按文字证据定" if kind == "none"
                      else "窗口重识别 " + "/".join(w.upper() for w in wit) if kind == "asr"
                      else "/".join(m["name"] for m in si.LISTEN_MODELS if m["slug"] in wit))
        + ("" if i == 0 else f"（仅 {levels[i - 1][0]} 未定的疑点）") for i, (name, kind, wit) in enumerate(levels))
        + "；仍定不下的由 CPA 选最可能的写法，标低把握，不拦截")


def _escalate(ctx: Ctx, si: Any, det: _Detection, esc: _Escalation, bcut: Sequence[SrtCue],
              clip: ClipInfo, cut_len: float, cpa_calls: list[CpaEvent],
              process: list[str]) -> StepOutcome | None:
    """2. escalation: listen only the doubt windows, stop at the level that settles each doubt."""
    base: list[ListeningWindow] = si.listen_windows(bcut, cut_len)
    win_by_cue = {int(w["cue_ids"][0]): w for w in base}
    two_base = {int(w["cue_ids"][0]): w for w in si.listen_windows(bcut, cut_len, prefix="S-")}   # per-speaker pass
    windows_by_name = {w["win"]: w for w in base} | {w["win"]: w for w in two_base.values()}
    levels = suspicion.escalation_levels()
    first_listen = next((name for name, kind, _ in levels if kind == "listen"), None)
    process.append(_plan_line(si, levels))
    next_level = {cid: 0 for cid in esc.pending}
    batches: dict[int, int] = {}
    while any(n < len(levels) for n in next_level.values()):
        n = min(n for n in next_level.values() if n < len(levels))
        lvl, kind, slugs = levels[n]
        batch = {cid for cid, level in next_level.items() if level == n}
        batches[n] = batches.get(n, 0) + 1
        suffix = "" if batches[n] == 1 else f"-b{batches[n]}"
        windows = [win_by_cue[cid] for cid in sorted(batch) if cid in win_by_cue]
        got = _level_records(ctx, si, det, esc, lvl, kind, slugs, windows, two_base, first_listen, cpa_calls,
                             suffix=suffix)
        if isinstance(got, StepOutcome):
            if esc.request_log:
                got.process.extend([*(f"听写 {line}" for line in esc.level_stats), *esc.request_log])
            return got
        records, stats_text = got
        for w in windows if kind != "none" else []:
            cid = int(w["cue_ids"][0])
            esc.windows_listened[cid] = float(w["audio"][1]) - float(w["audio"][0])
            esc.attempts[cid] = esc.attempts.get(cid, 0) + 1
        # the readings of level n come from levels 1..n, each only for the witnesses actually asked on that window
        esc.accumulated = esc.accumulated + records
        esc.lines_by_source, esc.audio_outside = si._audio_lines(esc.accumulated, windows_by_name)
        esc.lines_by_source.update(det.asr_lines)
        esc.lines_by_source.update(si._asr_window_lines(esc.asr_records, windows_by_name, bcut))
        heard = tuple(slugs) if kind == "listen" and n + 1 < len(levels) else ()
        listen_ahead = any(k == "listen" for _, k, _ in levels[n + 1:])
        failure = _settle_level(ctx, si, det, esc, lvl, stats_text, bcut, clip, cpa_calls, heard=heard,
                                listen_ahead=listen_ahead, batch=batch, suffix=suffix)
        if failure is not None:
            if esc.request_log:
                failure.process.extend([*(f"听写 {line}" for line in esc.level_stats), *esc.request_log])
            return failure
        for cid in batch:
            if cid in esc.pending:
                next_level[cid] = n + 1
            else:
                del next_level[cid]
        for cid in esc.pending - next_level.keys():
            next_level[cid] = 0
    return None


def _level_records(ctx: Ctx, si: Any, det: _Detection, esc: _Escalation, lvl: str, kind: str, slugs: Sequence[str],
                   windows: Sequence[ListeningWindow], two_base: Mapping[int, ListeningWindow],
                   first_listen: str | None, cpa_calls: list[CpaEvent], *, suffix: str = ""
                   ) -> tuple[list[ListeningRecord], str] | StepOutcome:
    """The new witnesses of one level: none (CPA decides on the text evidence), window ASR, or listening."""
    if kind == "none":
        return [], f"{lvl}{suffix}（不加新证人：CPA 先按已有的文字证据定，BCUT、剪映、整片 ASR、弹幕）：疑点 {len(windows)}"
    if kind == "asr":
        recs: list[WindowAsrRecord] | None = si._asr_window_pass(ctx, f"C-{lvl}{suffix}", windows, slugs)
        if recs is None:
            return cast(StepOutcome, si._fail("ASR_WINDOW_TOOL_FAILED", f"{lvl} window ASR pass: remote tool failed", cpa=cpa_calls))
        astats = dict(si.LAST_ASR_WINDOW_STATS)
        esc.asr_records = esc.asr_records + recs
        for prov, row in si.asr_window_billed(recs).items():
            acc = esc.asr_billed.setdefault(prov, {"calls": 0, "excerpt_s": 0.0, "reported_s": 0.0})
            for key, value in row.items():
                acc[key] = round(acc[key] + value, 2)
        new = sum(astats.get(k, 0) for k in ("OK", "NO_SPEECH", "ERROR"))
        return [], (f"{lvl}{suffix}（窗口重识别 {'/'.join(w.upper() for w in slugs)}）：窗口 {len(windows)}，请求 "
                    f"{astats.get('requested', len(windows) * len(slugs))}，缓存复用 {astats.get('reused', 0)}，"
                    f"新调用 {new}（成功 {astats.get('OK', 0)}，无话 {astats.get('NO_SPEECH', 0)}，错误 "
                    f"{astats.get('ERROR', 0)}，新送音频 {astats.get('new_audio_ms', 0) / 1000:.1f} s）")
    models = [m for m in si.LISTEN_MODELS if m["slug"] in slugs]
    records: list[ListeningRecord] | None = si._listen_pass(ctx, f"C-{lvl}{suffix}", windows, models)
    if records is None:
        return cast(StepOutcome, si._fail("LISTEN_TOOL_FAILED", f"{lvl} listening pass: remote tool failed", cpa=cpa_calls))
    stats = dict(si.LAST_LISTEN_STATS)
    if lvl == first_listen:
        records = records + _two_speaker_pass(ctx, si, det, esc, two_base, models,
                                              {int(w["cue_ids"][0]) for w in windows}, suffix=suffix)
    return records, (f"{lvl}{suffix}（{'/'.join(m['name'] for m in models)}）：窗口 {len(windows)}，请求 "
                     f"{stats.get('requested', len(windows) * len(models))}，缓存复用 {stats.get('reused', 0)}，"
                     f"新调用 {stats.get('new', 0)}（成功 {stats.get('OK', 0)}，429/额度 {stats.get('QUOTA', 0)}，"
                     f"错误 {stats.get('ERROR', 0)}）")


def _two_speaker_pass(ctx: Ctx, si: Any, det: _Detection, esc: _Escalation, two_base: Mapping[int, ListeningWindow],
                      models: Sequence[ListeningModel], batch: set[int], *, suffix: str = "") -> list[ListeningRecord]:
    """The sentences where the voiceprint shows two people are also heard once with the per-speaker prompt."""
    two_windows = [two_base[cid] for cid in sorted(batch) if cid in det.mixed and cid in two_base]
    if not two_windows:
        return []
    two_records: list[ListeningRecord] | None = si._listen_pass(ctx, f"C-S{suffix}", two_windows, models,
                                                               prompt="listen_two_speakers.md")
    two_stats = dict(si.LAST_LISTEN_STATS)
    esc.level_stats.append(
        f"分人听写（{'/'.join(m['name'] for m in models)}，声纹显示两人的句子）：窗口 {len(two_windows)}，"
        f"缓存复用 {two_stats.get('reused', 0)}，新调用 {two_stats.get('new', 0)}（成功 {two_stats.get('OK', 0)}，"
        f"429/额度 {two_stats.get('QUOTA', 0)}，错误 {two_stats.get('ERROR', 0)}）"
        + ("" if two_records is not None else "；工具失败，只记录"))
    return two_records or []


def _prior_decision_context(source_context: str, bcut: Sequence[SrtCue], esc: _Escalation) -> str:
    """Append decisions known at level entry, without treating them as new witness readings."""
    rows: list[_PriorDecision] = []
    for cue in bcut:
        cid = int(cue["id"])
        if cid not in esc.latest:
            continue
        latest = esc.latest[cid]
        decided, answer = latest["decided"], latest["row"]
        row: _PriorDecision = {"cue_id": cid, "start_s": cue["start_s"], "end_s": cue["end_s"],
                               "source_text": cue["text"], "level": latest["level"],
                               "status": "provisional" if cid in esc.pending else "settled",
                               "state": decided["state"], "final_text": decided["final_text"],
                               "dropped": decided["dropped"]}
        if answer is not None:
            if "other_speaker_text" in answer:
                row["other_speaker_text"] = answer["other_speaker_text"]
            if "drop" in answer:
                row["drop"] = answer["drop"]
        rows.append(row)
    if not rows:
        return source_context
    return source_context + "\n\n【先前裁决工作稿】\n" + json.dumps(rows, ensure_ascii=False, separators=(",", ":"))


def _primary_witness_context(whole: Mapping[str, Sequence[evidence_attach.WitnessLine]],
                             acquired: Mapping[str, Sequence[evidence_attach.WitnessLine]]) -> str:
    """Snapshot only acquired observations, retaining separate groups even for equal source keys."""
    def select(lines: Sequence[evidence_attach.WitnessLine]) -> list[evidence_attach.WitnessLine]:
        selected: list[evidence_attach.WitnessLine] = []
        for line in lines:
            view: evidence_attach.WitnessLine = {"abs_start_s": line["abs_start_s"], "text": line["text"]}
            if "end_s" in line:
                view["end_s"] = line["end_s"]
            if "speaker" in line:
                view["speaker"] = line["speaker"]
            if "owners" in line:
                view["owners"] = line["owners"]
            if "parts" in line:
                view["parts"] = line["parts"]
            if "words" in line:
                view["words"] = line["words"]
            if "speaker_segments" in line:
                view["speaker_segments"] = line["speaker_segments"]
            selected.append(view)
        return selected

    whole_view = {source: select(lines) for source, lines in whole.items()}
    # _escalate normally places the same whole-clip lines in acquired too; do not display them twice.
    # If a same-named source has different observations, retain both groups instead of overwriting either.
    acquired_view = {source: select(lines) for source, lines in acquired.items()
                     if source not in whole or lines != whole[source]}
    return ("\n\n【已取得的全片主证据：仅供理解语境，不新增投票；每句仍按本批时间及来源归属裁决】\n"
            "整片 ASR：" + json.dumps(whole_view, ensure_ascii=False, separators=(",", ":"))
            + "\n窗口识别与听写：" + json.dumps(acquired_view, ensure_ascii=False, separators=(",", ":"))
            + "\n缺失或空来源表示没有结果，不代表成功听到。")


def _parse_listen_requests(value: JsonValue) -> list[_ListenRequest]:
    if not isinstance(value, list):
        raise ValueError("listen_requests must be an array")
    requests: list[_ListenRequest] = []
    for item in value:
        if not isinstance(item, dict) or set(item) != {"cue_id", "reason"}:
            raise ValueError("each listen request must contain only cue_id and reason")
        cid, reason = item["cue_id"], item["reason"]
        if not isinstance(cid, int) or isinstance(cid, bool) or not isinstance(reason, str) or not reason.strip():
            raise ValueError("listen request needs an integer cue_id and nonempty reason")
        requests.append({"cue_id": cid, "reason": reason})
    return requests


def _collect_listen_requests(si: Any, det: _Detection, esc: _Escalation, cpa_calls: list[CpaEvent],
                             start: int) -> StepOutcome | None:
    """Read every response row, including rows superseded by a lookup or a later duplicate id."""
    requests: list[tuple[str, _ListenRequest]] = []
    for record in cpa_calls[start:]:
        try:
            rows = si._json_from_text(record["response_text"])
            for row in rows:
                if isinstance(row, dict) and "listen_requests" in row:
                    requests.extend((record["call_id"], request)
                                    for request in _parse_listen_requests(row["listen_requests"]))
        except ValueError as exc:
            # The existing parse-failure path invalidates its final record's cached answer.
            ordered = [r for r in cpa_calls if r is not record] + [record]
            return cast(StepOutcome, si._fail("CPA_RESPONSE_UNPARSEABLE", f"{record['file']}: {exc}",
                                              cpa=ordered))
    for source, request in requests:
        cid, reason = request["cue_id"], request["reason"]
        if cid not in det.by_id:
            disposition = "忽略：不是本片 BCUT 编号"
        elif cid in esc.labelled:
            disposition = "忽略：人工标注保护"
        elif cid in det.reasons:
            disposition = "忽略：已进入疑点流程"
        else:
            disposition = "采用：加入疑点流程首级"
            det.reasons[cid] = [f"CPA 裁决中补充疑点：{reason}"]
            esc.pending.add(cid)
        esc.request_log.append(f"CPA 补听请求 {source}：cue {cid}，理由 {json.dumps(reason, ensure_ascii=False)}；{disposition}")
    return None


def _settle_level(ctx: Ctx, si: Any, det: _Detection, esc: _Escalation, lvl: str, stats_text: str,
                  bcut: Sequence[SrtCue], clip: ClipInfo,
                  cpa_calls: list[CpaEvent], heard: Sequence[str] = (),
                  listen_ahead: bool = False, *, batch: set[int] | None = None, suffix: str = "") -> StepOutcome | None:
    """CPA decides the pending doubts with what the levels so far found; a settled doubt leaves the pending set.
    `heard`: the listening slugs of this level when a next level exists.  With lane_rules
    listening.escalate_without_reading (TASK-28, COACH e05 §1.2) a doubt whose window gave no reading from them stays
    open for the next level even when CPA wrote 高 (CPA decided without hearing it).
    `listen_ahead`: a later level listens.  With lane_rules listening.change_needs_listening (TASK-30, COACH e05 §2
    and §4) a doubt CPA decided to change in content while no Gemini listening reading of it exists yet stays open
    for that level: a change resting only on recognition text (window ASR, whole-clip ASR, jianying) is listened to
    before it stands.  CPA decides again with the listening in front of it; its answer stands (U107) and the vote
    families are unchanged (U59)."""
    context = _prior_decision_context(det.full_source_context + _primary_witness_context(
        det.asr_lines, esc.lines_by_source), bcut, esc)
    doubts: list[doubt_policy.FinalDoubt] = []
    for cid in sorted(esc.pending if batch is None else batch):
        cue = det.by_id[cid]
        pos = next(i for i, c in enumerate(bcut) if int(c["id"]) == cid)
        ev = si._evidence_for_cue(cue, pos, bcut, det.jy_att, det.witness_att, esc.lines_by_source, det.danmaku,
                                  det.mixed)
        doubts.append(doubt_entry(cue, clip, ev, differing=[s for s in ev["readings"] if s != "bcut"],
                                  lead=evidence_extra(ev), trail={"reasons": det.reasons.get(cid, [])}))
    # a doubt carries forward what the earlier level found: its lookup, and CPA's answer and reasons (first_pass)
    prior: dict[str, doubt_policy.CpaAnswer] = {}
    for d in doubts:
        before = esc.latest.get(d["cue_id"])
        if before is not None:
            if before["doubt"].get("work_lookup"):
                d["work_lookup"] = before["doubt"]["work_lookup"]
            if before["row"]:
                prior[d["id"]] = before["row"]
    start = len(cpa_calls)
    late_rules = si._render("subtitle_late_listen.md")
    rows, failure = si._adjudicate_all(ctx, doubts, context, cpa_calls, tag=f"c-{lvl}{suffix}",
                                       prior=prior or None, rules=late_rules)
    if failure is not None:
        return cast(StepOutcome, failure)
    failure = _collect_listen_requests(si, det, esc, cpa_calls, start)
    if failure is not None:
        return failure
    # every level looks up the works CPA named; a doubt that receives a new lookup is asked again with it
    fresh = si._attach_work_lookups(ctx, doubts, rows, esc.lookup_cache)
    again = [d for d in doubts if d["id"] in fresh]
    if again:
        start = len(cpa_calls)
        more, failure = si._adjudicate_all(ctx, again, context, cpa_calls, tag=f"c-{lvl}{suffix}-lookup",
                                           prior=rows, rules=late_rules)
        if failure is not None:
            return cast(StepOutcome, failure)
        failure = _collect_listen_requests(si, det, esc, cpa_calls, start)
        if failure is not None:
            return failure
        rows.update(more)
    settled_now = 0
    unheard = bool(heard) and bool(rules("listening").get("escalate_without_reading"))
    change_unheard = listen_ahead and bool(rules("listening").get("change_needs_listening"))
    for d in doubts:
        decided = doubt_policy.decide(rows.get(d["id"]), d["current"], maintained=bool(d["maintenance"]["maintained"]))
        esc.latest[d["cue_id"]] = {"doubt": d, "decided": decided, "row": rows.get(d["id"]), "level": lvl}
        if unheard and not suspicion.heard_by(d["readings"], heard):
            continue
        if change_unheard and suspicion.unheard_change(decided, d["current"], d["readings"]):
            continue
        if suspicion.is_settled(decided):
            esc.pending.discard(d["cue_id"])
            settled_now += 1
    esc.level_stats.append(f"{stats_text}；本级定下 {settled_now}，仍未定 {len(esc.pending)}")
    return None


def _assemble(ctx: Ctx, si: Any, det: _Detection, esc: _Escalation, labels: Mapping[int, AnchorLabel],
              process: list[str]) -> list[doubt_policy.FinalDoubt]:
    """3. the doubt rows for every cue: decided doubts, the cues not listened to, 维护者's labels."""
    final_doubts = [decided_doubt(item["doubt"], item["decided"], item["row"],
                                  between={"settled_at_level": item["level"]}, after={"unsettled": cid in esc.pending})
                    for cid, item in esc.latest.items()]
    for cid in sorted(esc.pending):
        process.append(f"未定（已到上限，只记录，不拦截）：cue {cid}，当前写法 {esc.latest[cid]['decided']['final_text']!r}，"
                       f"置信 {esc.latest[cid]['decided']['confidence']}")
    for cid, cue in det.by_id.items():
        if cid in esc.latest or cid in labels:
            continue
        if not doubt_policy.voiced(cue["text"]) and not doubt_policy.voiced(det.jy_by_cue[cid]):
            continue   # empty or laughter only: not subtitled (U112); a filler answer stays as its current text
        final_doubts.append({"id": f"k{cid:03d}", "cue_id": cid, "current": cue["text"], "state": "KEEP",
                             "final_text": cue["text"], "affects_meaning": False, "source": "NOT_SUSPICIOUS",
                             "maintained": False, "confidence": "高", "reason_key": None, "basis": "未标疑点，未听",
                             "readings": {"bcut": cue["text"]}, "differing": []})
    sha = str((ctx.state.data.get("reviewer_labels") or {}).get("export_sha256", ""))
    for cid, label in labels.items():
        if cid in det.by_id:
            final_doubts.append(si._reviewer_final(det.by_id[cid], label, sha=sha, readings={"bcut": det.by_id[cid]["text"]},
                                               differing=[], danmaku_near=[]))
    return final_doubts


def _account(si: Any, esc: _Escalation, cpa_calls: Sequence[CpaEvent], process: list[str],
             *, name_calls: int = 0) -> None:
    """Soft budget accounting (never enforced), listening and window-ASR use, evidence and lookups.  `name_calls`: the
    one whole-clip call of the name pass (TASK-36), which is neither the review nor an adjudication batch."""
    seconds = sum(esc.windows_listened.values())
    worst = max(esc.attempts.values()) if esc.attempts else 0
    process.append(f"软预算（只记账）：窗口 {len(esc.windows_listened)}（参考 ≤{suspicion.SOFT_BUDGET['windows']}），"
                   f"累计秒数 {seconds:.1f}（参考 ≤{suspicion.SOFT_BUDGET['seconds']:.0f}），同一窗口最多 {worst} 次"
                   f"（参考 ≤{suspicion.SOFT_BUDGET['per_window_calls']}）；超出的只记录")
    process.extend(f"听写 {line}" for line in esc.level_stats)
    process.extend(esc.request_log)
    if esc.asr_billed:
        process.append("窗口重识别用量（本步新调用；MAI 按 Azure 零售价 $0.54/音频小时，MOSS 返回 input_audio_seconds）："
                       + "；".join(f"{k.upper()} {v['calls']:.0f} 次，送 {v['excerpt_s']:.1f} s，返回计量 {v['reported_s']:.1f} s"
                                  for k, v in sorted(esc.asr_billed.items())))
    process.append(si._audio_attach_line(esc.lines_by_source, esc.audio_outside))
    process.extend(si._lookup_process(esc.lookup_cache))
    process.append(si.parallel_line())
    process.append(f"CPA 调用：整片审读 1，定字 {len(cpa_calls) - 1 - name_calls}（含补证后的定字），"
                   f"同名统一写法 {name_calls}（TASK-36：全片一次）")
