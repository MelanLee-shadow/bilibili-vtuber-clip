"""Step 3 body: ASR, listening witnesses, doubts, CPA adjudication (variant C is variant_c.py).

Split out of stage_impl.py (TASK-13); stage_impl re-exports every name, so callers and tests keep
`stage_impl.<name>`.
"""

from __future__ import annotations

import functools
import json
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING, TypedDict, cast

from . import (
    doubt_policy,
    evidence_attach,
    gates,
    lane_lines,
    name_consistency,
    parallel,
    variant_c,
    work_lookup,
    writing_canon,
)
from .adapters import remote_python
from .layout import layout
from .runner import Ctx, StepOutcome
from .srt_text import SrtCue, _context_lines, _norm_text, parse_srt
from .step3_adjudicate import (
    STRICT_RULES,
    _adjudicate_all,
    _attach_work_lookups,
    _corrected_cues,
    _lookup_process,
    adjudicate_mode,
    resolve_concatenation,
)
from .step3_doubts import (
    decided_doubt,
    doubt_entry,
    evidence_extra,
    gated_outcome,
    lone_and_holds,
    state_counts,
    write_subtitles,
)
from .step3_evidence import (
    ARGON,
    LAST_LISTEN_STATS,
    M38,
    _asr_witnesses,
    _audio_attach_line,
    _audio_lines,
    _danmaku_lines,
    _evidence_for_cue,
    _evidence_process,
    _reviewer_final,
    _listen_pass,
    reviewer_label_rows,
    listen_windows,
    upload_listen_tools,
)
from .step4_speaker import _mixed_by_cue, _speaker_chunks
from .step_import import _step3_import, imported
from .step_io import REMOTE_TOOLS, _ask_cpa, _clip, _fail, _local, _run_remote, linked_session, two_speaker_doubts

if TYPE_CHECKING:
    from .state import ClipInfo, CpaEvent
    from .step3_evidence import CueEvidence, IvanLabel, IvanSection


class SortedCues(TypedDict):
    doubts: list[doubt_policy.FinalDoubt]
    维护者: list[doubt_policy.FinalDoubt]
    no_vote: list[int]
    auto_keep: list[doubt_policy.FinalDoubt]
    read_aloud: list[int]


def step3_subtitles(ctx: Ctx) -> StepOutcome:
    """Step 3 for every adjudication mode, then the words of subs/final.srt written in the table's canonical form
    (writing_canon: an imported push too; a verbatim push takes over its source clip's verdicts)."""
    rec = imported(ctx)
    return writing_canon.settle_step3(ctx, _step3_decided(ctx), verbatim=bool(rec and rec.get("verbatim")))


def _step3_decided(ctx: Ctx) -> StepOutcome:
    """The Gemini usage of the step (CPA ledger, read-only) goes to PROCESS on every exit path; above 500 000
    output+thinking tokens it writes a WARN line and never stops the step.  An imported push (state.imported) is step
    3's decided subtitles."""
    parallel.reset()          # the step's own account of its parallel calls (parallel_line reads it back)
    rec = imported(ctx)
    if rec:
        return _step3_import(ctx, rec)
    adjudicate_mode()  # validate legacy mode even though unanchored single-speaker uses the agent
    if not linked_session(ctx.session) and not reviewer_label_rows(ctx.state.data.get('reviewer_labels')):
        return _step3_body(ctx)   # per-call audio receipts, aggregate ledger read after the batch
    step_start = variant_c.now_iso()
    outcome = _step3_body(ctx)
    outcome.process.extend(variant_c.usage_lines(*variant_c.gemini_usage(ctx, step_start)))
    return outcome


FREE_ASR_TOOL = REMOTE_TOOLS / "free_asr_paced.py"
FREE_ASR_PROVIDERS = ("bcut", "jianying")


def free_asr_body(rd: str, *, media: str | None = None,
                  providers: Sequence[str] = FREE_ASR_PROVIDERS) -> tuple[str, int]:
    """The whole-clip BCUT / jianying recognition on the media host, paced (U152; free_asr_paced.py, the pacing in
    the profile's manual_lane.json free_asr).  Returns (body, timeout): the timeout covers the rate-limit cooldowns.
    `media` and `providers` default to the clip's cut and both providers; a same-session compilation (compilation.py)
    names its own media file and asks for BCUT only."""
    pace = layout().free_asr
    body = (f"mkdir -p {rd}/asr {rd}/tools\n"
            + remote_python(f"{rd}/tools/free_asr_paced.py {media or f'{rd}/cut/cut.mp4'} {rd}/asr "
                            f"{layout().free_asr_state_dir} "
                            f"{pace.min_interval_s:g} {pace.rate_limit_cooldown_s:g} {pace.attempts} "
                            f"{pace.rate_limit_attempts} {' '.join(providers)}") + "\n"
            + "if " + " && ".join(f"test -s {rd}/asr/{p}.srt" for p in providers)
            + "; then echo ASR_OK; else echo ASR_MISSING; fi\n")
    return body, 3600 + int(pace.rate_limit_attempts * pace.rate_limit_cooldown_s)


def _fetch_free_asr(ctx: Ctx) -> StepOutcome | None:
    """BCUT and jianying for the whole clip, reused when their products are already here (the cut is fixed by then).
    Both are required: a provider that is given up (rate limited) or fails stops the step (ASR_MISSING_SOURCE)."""
    rd = ctx.remote_dir
    if all(_local(ctx, f"asr/{p}.srt").exists() for p in FREE_ASR_PROVIDERS):
        return None
    ctx.adapters.remote.put(FREE_ASR_TOOL, f"{rd}/tools/free_asr_paced.py")
    body, timeout = free_asr_body(rd)
    res = _run_remote(ctx, "s3-asr", body, timeout=timeout)
    if "ASR_OK" not in res.stdout:
        return _fail("ASR_MISSING_SOURCE", f"rc={res.rc} stdout={res.stdout[-200:]} stderr={res.stderr[-300:]}")
    for name in FREE_ASR_PROVIDERS:
        ctx.adapters.remote.get(f"{rd}/asr/{name}.srt", _local(ctx, f"asr/{name}.srt"))
    return None


def _step3_body(ctx: Ctx) -> StepOutcome:
    failure = _fetch_free_asr(ctx)
    if failure is not None:
        return failure
    bcut = parse_srt(_local(ctx, "asr/bcut.srt").read_text(encoding="utf-8"))
    jianying = parse_srt(_local(ctx, "asr/jianying.srt").read_text(encoding="utf-8"))
    clip = _clip(ctx)
    cut_len = (clip["end_ms"] - clip["start_ms"]) / 1000


    reviewer_section = ctx.state.data.get("reviewer_labels") or {}
    anchor = gates.reviewer_label_gate(reviewer_label_rows(reviewer_section), bcut, source_offset_s=clip["start_ms"] / 1000)
    if not anchor.ok:
        return _fail(anchor.reason_code, anchor.detail)
    labels = cast("dict[int, IvanLabel]", dict(anchor.evidence["matched"].items()))
    mode = adjudicate_mode()
    if not linked_session(ctx.session) and not labels:
        from .playbook_step3 import run
        return run(ctx, bcut=bcut, jianying=jianying, clip=clip, cut_len=cut_len)
    if mode == "C":   # multi-speaker attribution retains its existing flow
        return variant_c.run_step3(ctx, bcut=bcut, jianying=jianying, clip=clip, cut_len=cut_len, labels=labels)
    return _full_listening(ctx, bcut, jianying, clip, cut_len, labels, reviewer_section)


def parallel_line() -> str:
    """PROCESS: what the step's parallel calls actually did — each provider's calls, its bound and its peak, and the
    wait (TASK-31: the speed test and any rerun read this line, not a claim)."""
    snap = parallel.snapshot()
    rows = "；".join(f"{name} 调用 {row['calls']}，上限 {row['limit']}，峰值并发 {row['peak']}"
                    for name, row in sorted(snap["providers"].items()))
    return f"并行执行：{rows or '无 provider 调用'}（上限见 lane_rules concurrency；provider 的 429/配额由 adapters 自己的重试处理）"


def _full_listening(ctx: Ctx, bcut: Sequence[SrtCue], jianying: Sequence[SrtCue],
                    clip: ClipInfo, cut_len: float, labels: Mapping[int, IvanLabel],
                    reviewer_section: IvanSection) -> StepOutcome:
    """Modes full, A and B: every cue is listened to, then the doubts are adjudicated."""
    # Listening witnesses, one window per cue: Gemini 3.8 (L2); Argon (L4).  Audio is listened blind (no
    # current text is shown).  The 0.8x pass was dropped (TASK-5, provider throughput): it only added more readings of
    # families that are already counted, so family voting is unchanged.
    base = listen_windows(bcut, cut_len)
    # TASK-31: the listening passes and the whole-clip ASR witnesses wait on different providers, so the witness run
    # starts beside the L2 pass instead of after it.  L2 -> L4 stays ordered: listen_windows.py writes one
    # OUT/readings-all.json per pass into the same directory and the caller reads it right after the call, so two passes
    # of the same step must not overlap (the tool is outside this task's writable files).  Gemini L2 is required; argon
    # keeps L2's records when it fails; the witnesses record their own status.
    upload_listen_tools(ctx)
    listen_stats: dict[str, dict[str, int]] = {}
    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="lane-listen") as pool:
        witness_job = pool.submit(_asr_witnesses, ctx)
        records = _listen_pass(ctx, "L2", base, [M38], stats=listen_stats.setdefault("L2", {}), prepared=True)
        argon_records = None if records is None else _listen_pass(
            ctx, "L4", base, [ARGON], stats=listen_stats.setdefault("L4", {}), prepared=True)
        asr_lines, asr_status = witness_job.result()
    if records is None:
        return _fail("LISTEN_TOOL_FAILED", "level 2 listening pass: remote tool failed")
    LAST_LISTEN_STATS.clear()
    LAST_LISTEN_STATS.update(listen_stats.get("L2", {}))
    if argon_records is not None:          # argon failing keeps the L2 and L3a records
        records = records + argon_records
    argon_ok = sum(1 for r in (records or []) if r.get("slug") == "argon" and r.get("status") == "OK")
    danmaku = _danmaku_lines(ctx, cut_len)

    # Evidence attachment (evidence_attach): one rule for every producer and every mode.
    lines_by_source, audio_outside = _audio_lines(records or [], {w["win"]: w for w in base})
    witness_att = evidence_attach.own_witness_lines(bcut, asr_lines)
    lines_by_source.update(asr_lines)
    jy_att = evidence_attach.attach_asr(bcut, evidence_attach.segments_from_srt_cues(jianying), source="jianying")
    lines = lane_lines.plan(bcut, jy_att)

    chunks, chunk_error = _speaker_chunks(ctx, [*bcut, *lane_lines.chunk_cues(lines)])
    detected = _mixed_by_cue(chunks)
    mixed = detected if two_speaker_doubts() else {}   # measured: off by default (TASK-6 C5/C5b)
    witnesses = {"level2": "38m", "level3": "argon",
                 "argon_ok": argon_ok, "asr": asr_status, "danmaku_lines": len(danmaku)}
    _local(ctx, "subs/witnesses.json").write_text(json.dumps(witnesses, ensure_ascii=False, indent=2),
                                                 encoding="utf-8")
    mode = adjudicate_mode()
    sorted_cues = _sort_cues(bcut, clip, labels, reviewer_section, mode,
                             lambda index, cue: _evidence_for_cue(cue, index, bcut, jy_att, witness_att,
                                                                  lines_by_source, danmaku, mixed))
    doubts = sorted_cues["doubts"]

    cpa_calls: list[CpaEvent] = []
    lookup_cache: dict[str, work_lookup.LookupResult] = {}
    adjudicated, corrected, failure = _adjudicate_doubts(ctx, bcut, doubts, mode, cpa_calls, lookup_cache)
    if failure is not None:
        return failure
    concat_notes = _splice_guard(ctx, doubts, adjudicated, _context_lines(corrected), cpa_calls)
    final_doubts: list[doubt_policy.FinalDoubt] = []
    for d in doubts:
        row = adjudicated.get(d["id"])
        if row is None:
            return _fail("CPA_ROW_MISSING", f"CPA returned no row for doubt {d['id']}", cpa=cpa_calls)
        decided = doubt_policy.decide(row, d["current"], maintained=bool(d["maintenance"]["maintained"]))
        final_doubts.append(decided_doubt(d, decided, row))
    final_doubts.extend(sorted_cues["维护者"])
    final_doubts.extend(sorted_cues["auto_keep"])




    name_record = name_consistency.resolve(ctx, final_doubts, [c["id"] for c in bcut], ask=functools.partial(_ask_cpa, purpose="name-consistency"))
    cpa_calls.extend([cast("CpaEvent", name_record["cpa_call"])] if name_record.get("cpa_call") else [])

    counts = state_counts(final_doubts)
    maintained_n = sum(1 for d in final_doubts if d.get("maintained"))
    doubts_path, final_path, norm_log = write_subtitles(ctx, bcut, final_doubts, lines)
    norm_note = (f"字幕规范化（规范表 {len(norm_log['canon_replacements'])} 处替换，拆条 "
                 f"{norm_log['extra_cues_from_split']} 条，按 FMR 的行数与字数限制；相邻句开头重复（只记录，不删字） "
                 f"{len(norm_log['adjacent_repeats'])} 处：{norm_log['adjacent_repeats']}）")

    process = [f"证人：音频模型 3.8（基础窗口）、Argon（{argon_ok} 窗口成功），"
               f"MOSS/MAI {asr_status}，弹幕 {len(danmaku)} 行（只作线索）",
               f"疑点 {len(final_doubts)} 条：维持（两家相同非现稿读法）{maintained_n} 条，"
               f"CPA 裁决 {len(cpa_calls)} 批，四态计数 {counts}"]
    process += _evidence_process(jy_att, witness_att, danmaku, sorted_cues["read_aloud"])
    process += lines.process_lines()
    process += _lookup_process(lookup_cache)
    process.append(f"声纹分段（v8，约 1.2 秒一段）：句内像有两人的 {len(detected)} 句"
                   + (f"：cue {sorted(detected)}" if detected else "") + (f"；{chunk_error}" if chunk_error else ""))
    process.append(_audio_attach_line(lines_by_source, audio_outside))
    process.append(parallel_line())
    process.append(name_consistency.summary_line(name_record))
    process.append(norm_note)
    no_vote = sorted_cues["no_vote"]
    lone_and_holds(ctx, norm_log, final_doubts, process, between=(
        [f"空文字或纯笑声（无任一来源的话语，U112），未送裁决、不出字幕：cue {no_vote}"] if no_vote else [])
        + concat_notes)
    return gated_outcome(ctx, final_doubts, cpa_calls, process,
                         products={"doubts": str(doubts_path), "final_srt": str(final_path)},
                         extra_products={"witnesses": str(_local(ctx, "subs/witnesses.json"))},
                         note=f"疑点 {len(final_doubts)} 条，四态 {counts}")


def _sort_cues(bcut: Sequence[SrtCue], clip: ClipInfo, labels: Mapping[int, IvanLabel],
               reviewer_section: IvanSection, mode: str,
               evidence: Callable[[int, SrtCue], CueEvidence]) -> SortedCues:
    """Every cue goes one way: 维护者's label (final), no speech, variant A's undisputed keep, or a doubt for CPA."""
    out: SortedCues = {"doubts": [], "维护者": [], "no_vote": [], "auto_keep": [], "read_aloud": []}
    for index, cue in enumerate(bcut):
        ev = evidence(index, cue)
        readings = ev["readings"]
        if ev.get("read_aloud"):
            out["read_aloud"].append(int(cue["id"]))
        differing = sorted(src for src, txt in readings.items()
                           if src != "bcut" and _norm_text(txt) != _norm_text(cue["text"]))
        if cue["id"] in labels:
            out["维护者"].append(_reviewer_final(cue, labels[cue["id"]], sha=str(reviewer_section.get("export_sha256", "")),
                                           readings=readings, differing=differing, danmaku_near=ev["danmaku"]))
        elif not doubt_policy.voiced(cue["text"]) and not any(doubt_policy.voiced(v) for v in readings.values()):
            # Empty and laughter-only material is not speech; spoken answers follow the normal mode path.
            out["no_vote"].append(cue["id"])
        else:
            entry = doubt_entry(cue, clip, ev, differing=differing, trail=evidence_extra(ev))
            if mode == "A" and not differing and not entry.get("asr_notes") and not entry.get("read_aloud") \
                    and not entry.get("speaker_note"):
                # variant A: a cue no witness disagrees with is not sent to CPA; it keeps its text
                out["auto_keep"].append(cast(doubt_policy.FinalDoubt, dict(entry, state="KEEP", final_text=cue["text"], affects_meaning=False,
                                             source="NO_WITNESS_DISAGREEMENT", maintained=False, confidence="高",
                                             reason_key=None, basis="证人无分歧，未送裁决（变体 A）", cpa_call=None)))
            else:
                out["doubts"].append(entry)
    return out


def _adjudicate_doubts(ctx: Ctx, bcut: Sequence[SrtCue], doubts: Sequence[doubt_policy.FinalDoubt], mode: str,
                       cpa_calls: list[CpaEvent], lookup_cache: dict[str, work_lookup.LookupResult]
                       ) -> tuple[dict[str, doubt_policy.CpaAnswer], Sequence[SrtCue], StepOutcome | None]:
    """CPA adjudication of every doubt, two passes (TASK-5 item 2): pass 1 reads BCUT's context; pass 2 reads the
    context as pass 1 corrected it, with pass 1's answer shown as a prior to re-check.  CPA's answer stands (item 3).
    Variant A: one pass; a doubt that received a work lookup is asked again with it (TASK-6 C).  Returns
    (adjudicated, the corrected cues, failure)."""
    rules = STRICT_RULES if mode == "B" else ""
    adjudicated, failure = _adjudicate_all(ctx, doubts, _context_lines(bcut), cpa_calls, tag="p1", rules=rules)
    if failure is not None:
        return adjudicated, bcut, failure
    fresh = _attach_work_lookups(ctx, doubts, adjudicated, lookup_cache)
    if mode == "A":
        again = [d for d in doubts if d["id"] in fresh]
        if again:
            rows, failure = _adjudicate_all(ctx, again, _context_lines(bcut), cpa_calls, tag="lookup", prior=adjudicated)
            if failure is not None:
                return adjudicated, bcut, failure
            adjudicated.update(rows)
        return adjudicated, bcut, None
    corrected = _corrected_cues(bcut, doubts, adjudicated)
    adjudicated, failure = _adjudicate_all(ctx, doubts, _context_lines(corrected), cpa_calls, tag="p2",
                                           prior=adjudicated, rules=rules)
    return adjudicated, corrected, failure


def _splice_guard(ctx: Ctx, doubts: Sequence[doubt_policy.FinalDoubt],
                  adjudicated: dict[str, doubt_policy.CpaAnswer], context: str,
                  cpa_calls: list[CpaEvent]) -> list[str]:
    """resolve_concatenation on every adjudicated doubt (in place); returns its PROCESS notes.

    TASK-31: the doubts are independent (each re-asks CPA about its own sentence, nothing reads another's answer), so
    they run at once (parallel.run, bound lane_rules concurrency.cpa).  Answers and notes are applied in doubt order."""
    notes: list[str] = []
    todo = [d for d in doubts if d["id"] in adjudicated]
    if not todo:
        return notes
    results = parallel.run(
        todo, lambda d: resolve_concatenation(ctx, d, adjudicated[d["id"]], call_prefix=f"concat-{d['id']}",
                                             context=context), provider="cpa")
    for d, (new_row, extra, note) in zip(todo, results, strict=True):
        cpa_calls.extend(extra)
        if note:
            notes.append(f"{d['id']}：{note}")
        adjudicated[d["id"]] = new_row
    return notes
