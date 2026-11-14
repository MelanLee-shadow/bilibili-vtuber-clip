"""Step 3 doubt rows, shared by the ordinary path (step3_subtitles) and variant C (variant_c): the doubt built from a
cue's evidence, the row CPA's decision makes of it, the step's products (final.srt, the records) and the gate.

TASK-13: both paths spelled these out inline (variant_c.run_step3 and _step3_body were the lane's two most complex
functions).  Key order is kept exactly as each path wrote it: doubts.json and the prompts built from the doubts are
compared byte for byte (cached CPA answers are keyed by the prompt).

The one place final.srt is written is `write_subtitles`.  A sentence CPA decided not to subtitle
(doubt_policy.is_dropped) is omitted and its words and reason are recorded in subs/dropped_cues.json and PROCESS.
Historical refusal records remain readable.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, TypedDict, cast

from . import doubt_policy, gates, normalize, overlap_join
from .runner import Ctx, StepOutcome
from .srt_text import SrtCue, TimedText, format_srt
from .step3_adjudicate import CorrectedCue, _final_cues, hold_line, lone_char_line, record_lone
from .step3_text_classes import merged_timing
from .step_io import _local, _log, linked_session

if TYPE_CHECKING:
    from .evidence_attach import SourcedSpeakerSegment
    from .lane_lines import LinePlan
    from .state import ClipInfo, CpaEvent
    from .step3_evidence import CueEvidence

EVIDENCE_KEYS = ("asr_notes", "read_aloud", "speaker_note", "other_span_s", "speaker_segments")
DROPPED_CUES = "subs/dropped_cues.json"


class DropCue(TypedDict, total=False):
    id: str | None
    doubt_id: str | None
    cue_id: int | None
    text: str
    said: str
    start_s: float
    end_s: float
    reason: str
    reason_key: str | None
    drop_kind: str
    final_text: str
    written: str


class DecisionBetween(TypedDict, total=False):
    settled_at_level: str


class DecisionAfter(TypedDict, total=False):
    unsettled: bool


class SubtitleLog(normalize.NormalizeLog, total=False):
    drop_cues: list[DropCue]
    refused_cues: list[DropCue]


def evidence_extra(ev: CueEvidence) -> doubt_policy.FinalDoubt:
    """The optional evidence a doubt carries when the cue has it (ASR notes, read-aloud danmaku, two speakers)."""
    view = cast("Mapping[str, str | dict[str, str] | list[str] | list[float] | list[SourcedSpeakerSegment]]", ev)
    return cast(doubt_policy.FinalDoubt, {k: view[k] for k in EVIDENCE_KEYS if view.get(k)})


def doubt_entry(cue: SrtCue, clip: ClipInfo, ev: CueEvidence, *, differing: Sequence[str],
                lead: doubt_policy.FinalDoubt | None = None, trail: doubt_policy.FinalDoubt | None = None
                ) -> doubt_policy.FinalDoubt:
    """One doubt for CPA: the cue, its absolute span in the recording, its readings and what differs, the maintenance
    view (two families with the same non-current reading), the candidates and the nearby danmaku.  `lead` goes right
    after the current text, `trail` at the end."""
    readings = ev["readings"]
    maint = doubt_policy.maintenance(readings, cue["text"])
    offset_s = clip["start_ms"] / 1000
    return cast(doubt_policy.FinalDoubt, {"id": f"d{int(cue['id']):03d}", "cue_id": cue["id"], "current": cue["text"], **(lead or {}),
            "abs_start_ms": round((offset_s + cue["start_s"]) * 1000),
            "abs_end_ms": round((offset_s + cue["end_s"]) * 1000),
            "readings": readings, "differing": list(differing),
            "maintenance": {"maintained": maint["maintained"], "reading": maint["reading"],
                            "families": maint["families"], "candidates": maint["candidates"],
                            "current_held": maint["current_held"]},
            "candidates": doubt_policy.candidates(readings, cue["text"]),
            "danmaku": ev["danmaku"], **(trail or {})})


def decided_doubt(doubt: doubt_policy.FinalDoubt, decided: doubt_policy.Decision, row: doubt_policy.CpaAnswer | None,
                  *, between: DecisionBetween | None = None, after: DecisionAfter | None = None
                  ) -> doubt_policy.FinalDoubt:
    """The doubt with CPA's decision (doubt_policy.decide) and the answer's reasons; `between` goes before the
    other-speaker fields, `after` at the end.  A dropped sentence has state ADOPT or FIT_CONTEXT, no final text,
    `dropped` true, and the cue's own text kept in `current`."""
    row = row or {}
    return cast(doubt_policy.FinalDoubt, dict(doubt, state=decided["state"], final_text=decided["final_text"],
                affects_meaning=decided["affects_meaning"], source=decided["source"],
                maintained=decided["maintained"], reason_key=row.get("reason_key"),
                confidence=decided["confidence"], against_maintained=row.get("against_maintained"),
                reason=row.get("reason"), basis=row.get("basis"), cpa_call=row.get("cpa_call"),
                dropped=bool(decided.get("dropped")), drop_refused_written=bool(decided.get("drop_refused_written")),
                drop_routed=bool(decided.get("drop_routed")), drop_said=str(decided.get("drop_said") or ""),
                **({"runs_same": decided["runs_same"]} if "runs_same" in decided else {}),
                **cast("Mapping[str, str]", between or {}),
                other_speaker_text=str(row.get("other_speaker_text") or "").strip(),
                other_first=bool(row.get("other_first")), **cast("Mapping[str, bool]", after or {})))


def state_counts(doubts: Sequence[doubt_policy.DecisionView]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for d in doubts:
        counts[str(d.get("state"))] = counts.get(str(d.get("state")), 0) + 1
    return counts


def record_dropped(clip_dir: Path, rows: Sequence[DropCue], refused: Sequence[DropCue] = ()) -> None:
    """The sentences CPA decided not to subtitle, plus historical refusals, beside final.srt so a reader sees what
    was left out or written and on whose decision.  This is separate from the lone-character normalization record.

    A dropped row is {start_s, end_s, text, said, doubt_id, cue_id, reason_key, drop_kind, reason}: the cue's own
    words, the doubt that decided it, and why.  A refused row carries the same fields plus {decision:
    "drop_refused_written", final_text, written}: what CPA asked to remove, what is written instead, and whether that
    is CPA's own words or the current text.

    A later writer (a review fix) may call this again on the same clip: the rows of the doubts it writes now replace
    their earlier rows and the other rows are kept, so the record always describes the products beside it."""
    path = clip_dir / DROPPED_CUES
    path.parent.mkdir(parents=True, exist_ok=True)

    def row(r: DropCue, **extra: str) -> DropCue:
        return cast(DropCue, {"start_s": round(float(r["start_s"]), 3), "end_s": round(float(r["end_s"]), 3),
                "text": str(r["text"]), "said": str(r.get("said") or ""), "doubt_id": r.get("id"),
                "cue_id": r.get("cue_id"), "reason_key": r.get("reason_key"), "drop_kind": r.get("drop_kind"),
                "reason": str(r.get("reason") or ""), **extra})

    new = [row(r) for r in rows]
    new += [row(r, decision="drop_refused_written", final_text=str(r.get("final_text") or ""),
                written=str(r.get("written") or "")) for r in refused]
    written_now = {r.get("doubt_id") for r in new}
    old = [r for r in (json.loads(path.read_text(encoding="utf-8")) if path.exists() else [])
           if r.get("doubt_id") not in written_now]
    path.write_text(json.dumps(old + new, ensure_ascii=False, indent=1), encoding="utf-8")


def dropped_line(rows: Sequence[DropCue], decisions: Sequence[doubt_policy.FinalDoubt],
                 refused: Sequence[DropCue] = ()) -> str:
    """Describe omissions and any historical refusal/routing records in PROCESS."""
    def one(r: DropCue) -> str:
        said = f"（实际「{r['said']}」）" if r.get("said") and r["said"] != r["text"] else ""
        return f"{r.get('doubt_id') or r.get('id')}「{r['text']}」{said}{r.get('reason') or ''}"

    parts = [f"CPA 判定不出字幕：{len(rows)} 句"
             + ("" if not rows else "：" + "；".join(one(r) for r in rows))]
    if refused:
        parts.append(f"历史记录：CPA 要求删句、旧规则未执行而照写：{len(refused)} 句："
                     + "；".join(f"{r.get('doubt_id')}「{r['text']}」→「{r['final_text']}」"
                                f"（{'CPA 写下的实际内容' if r.get('written') == 'CPA' else '保留现稿'}）"
                                for r in refused))
    routed = [d for d in decisions if d.get("drop_routed")]
    if routed:
        parts.append(f"历史记录：CPA 要求删句，旧规则将孤立单字转交格式处理：{len(routed)} 句："
                     + "；".join(f"{d.get('id')}「{d.get('current')}」" for d in routed))
    return "；".join(parts)


def trim_neighbour_words(ctx: Ctx, bcut: Sequence[SrtCue],
                         doubts: Sequence[doubt_policy.FinalDoubt]) -> list[doubt_policy.FinalDoubt]:
    """Apply the existing join decision to both decided text lanes after parallel answers have all arrived.

    For an aside use only whole-clip source-local clusters that actually contain its words.  Interleaving another
    voice into the comparison would turn concurrent speech into apparent duplication.  No witness / ties keep both.
    """
    sources: dict[str, list[TimedText]] = {}
    for provider in ("mai", "moss"):
        path = ctx.clip_dir / f"witness/{provider}.json"
        if not path.exists():
            continue
        doc = json.loads(path.read_text(encoding="utf-8"))
        for s in doc.get("segments", []) if doc.get("status") == "OK" else []:
            if s.get("speaker"):
                sources.setdefault(f"{provider}:{s['speaker']}", []).append({
                    "start_s": s["start_ms"] / 1000, "end_s": s["end_ms"] / 1000, "text": s["text"]})
    by_id = {d["cue_id"]: cast(doubt_policy.FinalDoubt, dict(d)) for d in doubts}
    context = [cast(CorrectedCue, dict(c, text=str(by_id.get(c["id"], {}).get("final_text") or c["text"]),
                    other_speaker_text=str(by_id.get(c["id"], {}).get("other_speaker_text") or ""))) for c in bcut]
    for i, cue in enumerate(context):
        d = by_id.get(cue["id"])
        if d is None or not cue["other_speaker_text"]:
            continue
        key = overlap_join.fold(cue["other_speaker_text"])
        relevant = {name: segs for name, segs in sources.items()
                    if key and any(key in overlap_join.fold(s["text"])
                                   and s["start_s"] < cue["end_s"] and s["end_s"] > cue["start_s"] for s in segs)}
        text, log = overlap_join.trim_reading(cue["other_speaker_text"], cue,
                                            context[i - 1] if i else None,
                                            context[i + 1] if i + 1 < len(context) else None, relevant)
        if text != cue["other_speaker_text"]:
            d["other_speaker_text"] = text
            d["neighbour_trim"] = {"before": cue["other_speaker_text"], "after": text, "joins": log}
            cue["other_speaker_text"] = text
    return [by_id[d["cue_id"]] for d in doubts]


def write_subtitles(ctx: Ctx, bcut: Sequence[SrtCue], final_doubts: Sequence[doubt_policy.FinalDoubt],
                    lines: LinePlan | None = None, *, preserve_decisions: bool = False) -> tuple[Path, Path, SubtitleLog]:
    """doubts.json and final.srt (the cues as decided, normalised for release; `lines`: lane_lines.LinePlan).  The
    doubt rows and the cues handed to the writer are the same rows, so a dropped sentence (doubt_policy.is_dropped)
    is carried into the normalisation log (`drop_cues`); historical refusals remain in `refused_cues`.  Both are
    recorded in subs/dropped_cues.json and PROCESS.  Returns (doubts path, final path, log)."""
    trimmed = trim_neighbour_words(ctx, bcut, final_doubts)
    for original, row in zip(final_doubts, trimmed, strict=True):
        if isinstance(original, dict):
            original.update(row)
    final_doubts = trimmed
    doubts_path = _local(ctx, "subs/doubts.json")
    doubts_path.write_text(json.dumps(list(final_doubts), ensure_ascii=False, indent=2), encoding="utf-8")
    dropped = [d for d in final_doubts if doubt_policy.is_dropped(d)]
    kept = [d for d in final_doubts if not doubt_policy.is_dropped(d)]
    cue_time = {c["id"]: c for c in bcut}

    def times(d: doubt_policy.FinalDoubt) -> dict[str, float]:
        cue = cue_time.get(cast(int, d.get("cue_id")), cast(SrtCue, {}))
        return {"start_s": float(cue.get("start_s", 0.0)), "end_s": float(cue.get("end_s", 0.0))}

    normalized: Sequence[TimedText]
    base_log: normalize.NormalizeLog
    if preserve_decisions:
        # The single-speaker playbook gives CPA every cue and all text conventions in one decision.
        # Preserve its words, pronouns and fillers. Only cues exceeding the visual limit are split;
        # the resulting pieces stay inside the BCUT intervals of the sentences the line's words came from.
        by_id = {d['cue_id']: d for d in kept}
        merged = merged_timing(bcut, final_doubts)   # time follows the words CPA merged into a line
        decided = [cast(SrtCue, dict(c, text=by_id[c['id']]['final_text'] or c['text'], **merged.get(c['id'], {})))
                   for c in bcut if c['id'] in by_id]
        shaped = normalize.shape_decided_cues(decided)
        normalized = shaped
        base_log = {'canon_replacements': [], 'extra_cues_from_split': len(shaped) - len(decided), 'adjacent_repeats': [],
                    'lone_chars': [], 'numerals': []}
    else:
        normalized, base_log = _final_cues(bcut, kept, one_speaker=not linked_session(ctx.session), lines=lines)
    norm_log = cast(SubtitleLog, base_log)
    # `text` is the cue as it stood (what the subtitle would have said); `said` is what CPA judged was actually
    # said, when it wrote that down.  Both, so a refused cue can be read back as "what CPA asked and what it says".
    norm_log["drop_cues"] = [cast(DropCue, {"id": d.get("id"), "cue_id": d.get("cue_id"), "text": str(d.get("current") or ""),
                             "said": str(d.get("drop_said") or ""), **times(d),
                             "reason": str(d.get("reason") or ""), "reason_key": d.get("reason_key"),
                             "drop_kind": doubt_policy.drop_kind(str(d.get("drop_said") or d.get("current") or ""))})
                            for d in dropped]
    norm_log["refused_cues"] = [cast(DropCue, {"id": d.get("id"), "cue_id": d.get("cue_id"), "text": str(d.get("current") or ""),
                                "said": str(d.get("drop_said") or ""), **times(d),
                                "reason": str(d.get("reason") or ""), "reason_key": d.get("reason_key"),
                                "drop_kind": doubt_policy.drop_kind(str(d.get("drop_said") or d.get("current") or "")),
                                "final_text": str(d.get("final_text") or ""),
                                "written": "CPA" if d.get("state") in {"ADOPT", "FIT_CONTEXT"} else "CURRENT"})
                               for d in final_doubts if d.get("drop_refused_written")]
    final_path = _local(ctx, "subs/final.srt")
    final_path.write_text(format_srt(normalized), encoding="utf-8")
    return doubts_path, final_path, norm_log


def lone_and_holds(ctx: Ctx, norm_log: SubtitleLog, final_doubts: Sequence[doubt_policy.FinalDoubt],
                   process: list[str], *, between: Sequence[str] = ()) -> None:
    """Record release-shape drops, CPA omissions, historical refusals and holds in PROCESS."""
    lone = norm_log.get("lone_chars", [])
    process.append(lone_char_line(lone))
    record_lone(ctx.clip_dir, lone, fresh=True)
    dropped = norm_log.get("drop_cues") or []
    refused = norm_log.get("refused_cues") or []
    record_dropped(ctx.clip_dir, dropped, refused)
    process.append(dropped_line(dropped, final_doubts, refused))
    process.extend(between)
    process.extend(f"跨句重复（同一 overlap_join，另一人字段）{d['id']}：{d['neighbour_trim']}"
                   for d in final_doubts if d.get("neighbour_trim"))
    process.append(hold_line(final_doubts))


def gated_outcome(ctx: Ctx, final_doubts: Sequence[doubt_policy.FinalDoubt], cpa_calls: Sequence[CpaEvent],
                  process: list[str], *, products: Mapping[str, str], extra_products: Mapping[str, str],
                  note: str) -> StepOutcome:
    """The doubt gate over the decided rows (only a hold the release does not cover stops the step)."""
    verdict = gates.doubt_gate(final_doubts, released_reasons=ctx.state.released_reasons())
    logged = [_log(c) for c in cpa_calls]
    if not verdict.ok:
        return StepOutcome(False, reason=verdict.reason_code, cpa=logged, note=verdict.detail,
                           products=dict(products), process=process)
    return StepOutcome(True, products={**products, **extra_products}, cpa=logged, note=note, process=process)
