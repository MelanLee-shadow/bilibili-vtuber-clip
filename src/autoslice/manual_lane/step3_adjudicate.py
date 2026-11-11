"""Step 3 adjudication: the adjudication modes and prompt parts, CPA adjudication of the doubts, work
lookups, the corrected and final cues, lone-character drops, holds, and the splice guard.

Split out of stage_impl.py (TASK-13); stage_impl re-exports every name, so callers and tests keep
`stage_impl.<name>`.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from ..topic_entity_graph import load_topic_entity_graph
from . import (
    doubt_policy,
    mishearing,
    normalize,
    parallel,
    work_lookup,
)
from .runner import Ctx, StepOutcome
from .config import participant_description, unknown_participant
from .srt_text import SrtCue, _json_from_text
from .step3_evidence import ADJUDICATE_BATCH
from .step_io import HOST_LABEL, REMOTE_TOOLS, _ask_cpa, _cpa_ok, _fail, _render

if TYPE_CHECKING:
    from .lane_lines import LinePlan
    from .normalize import NormalizeLog
    from .state import CpaEvent
    from .subtitle_track import TrackCue

ADJUDICATE_MODES = ("full", "A", "B", "C")


class CorrectedCue(SrtCue):
    other_speaker_text: str


DEFAULT_ADJUDICATE_MODE = "C"   # U119: listen only the doubts, escalate while unsettled (A and B listen every cue: reference only)
# full: every cue, two passes, the prompt as before (the 2nd-round design).  A: only cues a witness disagrees with, one
# pass.  B: every cue, two passes, and the two constraints below.  Chosen by MANUAL_LANE_ADJUDICATE_MODE.
STRICT_RULES = """严格规则（必须遵守）：
(1) 证人一致的读法默认保留。只有它在上下文里读不通、或者和前后文矛盾时才能改；改的时候必须在 reason 字段写出理由（哪一句、哪里矛盾）。没有理由，就保留证人一致的读法。
(2) 逐字保留听到的内容：不能删语气词（啊、嗯、吧、呢、哦、呃），不能合并、改写或润色句子。清楚处必须用原话。"""


def adjudicate_mode() -> str:
    mode = os.environ.get("MANUAL_LANE_ADJUDICATE_MODE", DEFAULT_ADJUDICATE_MODE)
    if mode not in ADJUDICATE_MODES:
        raise ValueError(f"MANUAL_LANE_ADJUDICATE_MODE must be one of {ADJUDICATE_MODES}, got {mode!r}")
    return mode


MISHEARING_PAIRS = REMOTE_TOOLS.parent / "mishearing_pairs.json"
PROPER_NOUNS = REMOTE_TOOLS.parent / "proper_nouns.json"


def _participant_line(ctx: Ctx) -> str:
    """The names on stage for this session, from the session's own participant groups (first name = canonical)."""
    return participant_description(ctx.session, deduplicate=True) or HOST_LABEL


def render_proper_nouns() -> str:
    """Render the same spelling table used by the single-speaker path."""
    names = json.loads(PROPER_NOUNS.read_text(encoding="utf-8"))
    return "\n".join(
        f"- 规范词形：{n['canonical']}；读法/写法：{'、'.join(n['variants'])}；条件：{n['condition']}"
        + (f"；不要写成：{'、'.join(n['rejected_variants'])}" if n.get('rejected_variants') else '')
        + (f"；说明：{n['note']}" if n.get('note') else '')
        if 'canonical' in n else
        f"- {n['role']}（正式写法）：{n['name']}（{n['note']}）" if n.get("note")
        else f"- 专名（正式写法）：{n['name']}（别称：{'、'.join(n['aliases'])}）"
        for n in names
    )


def _clip_hint_readings(ctx: Ctx) -> list[str]:
    """Only ordinary transcription inputs, never prompts, decisions, danmaku or the proper-name table itself."""
    from .srt_text import parse_srt

    texts = [str(name) for group in (ctx.session.get("participants") or []) if group and not unknown_participant(str(group[0]))
             for name in group]

    def collect(value: Any) -> None:
        if isinstance(value, str):
            texts.append(value)
        elif isinstance(value, list):
            for item in value:
                collect(item)
        elif isinstance(value, dict):
            # Ignore errors / cache metadata; only recognised or dictated speech counts.
            if value.get("status") not in (None, "OK"):
                return
            if isinstance(value.get("text"), str):
                texts.append(value["text"])
            for key in ("segments", "lines", "records", "results"):
                if key in value:
                    collect(value[key])

    for name in ("asr/bcut.srt", "asr/jianying.srt"):
        path = ctx.clip_dir / name
        if path.exists():
            texts.extend(str(c["text"]) for c in parse_srt(path.read_text(encoding="utf-8")))
    paths = [ctx.clip_dir / "witness/mai.json", ctx.clip_dir / "witness/moss.json",
             ctx.clip_dir / "listen/readings.json"]
    paths.extend(sorted((ctx.clip_dir / "witness").glob("result-windows-*.json")))
    for path in paths:
        if path.exists():
            collect(json.loads(path.read_text(encoding="utf-8")))
    return texts


def _confusion_lines(ctx: Ctx | None = None, readings: Sequence[str] = ()) -> str:
    """Clip-relevant hints plus every identity authority; relevance does not widen scope."""
    pairs = mishearing.load_pairs(MISHEARING_PAIRS)
    if ctx is not None:
        pairs = mishearing.relevant_pairs(pairs, [*_clip_hint_readings(ctx), *readings])
    return "\n".join(filter(None, [mishearing.render_lines(pairs), render_proper_nouns()]))


WINDOW_ASR_DOC = ("带 @win 的（asr:mai@win、asr:moss@win）是同一引擎只识别本句窗口音频的结果，与整片识别的同名引擎算同一家；"
                  "它只听到本句，句首句尾可能截到相邻句的一两个字。")


def _batch_answer_contract(ids: Sequence[str]) -> Callable[[Any], str]:
    """The adjudication's contract on one batch answer (TASK-35): one row per doubt of the batch, keyed by its id.

    A whole answer that does not do that is not a smaller answer.  Cut answers apart, this was how the defect showed
    on ORI3: the doubts left out were judged UNRESOLVED (`decide` on no row) or stopped the clip at CPA_ROW_MISSING.
    It is the same failure as a cut answer and takes the same path — asked again, and a tool failure when the tries
    run out (the failure keeps the name the gate already uses: CPA_ROW_MISSING).  An answer that is not a list gives
    no row to any doubt of the batch, so it is read the same way.  An answer with no JSON value at all (prose, a
    refusal) is not this case: it is the caller's own CPA_RESPONSE_UNPARSEABLE and is not re-sent.
    """
    def accepts(value: Any) -> str:
        rows = value if isinstance(value, list) else []
        got = {str(row.get("id")) for row in rows if isinstance(row, dict) and row.get("id")}
        missing = [d for d in ids if d not in got]
        return f"CPA_ANSWER_MISSING_IDS ({len(missing)}/{len(ids)}: {', '.join(missing[:10])})" if missing else ""

    return accepts


def _adjudicate_all(ctx: Ctx, doubts: Sequence[doubt_policy.FinalDoubt], context: str, cpa_calls: list[CpaEvent],
                    *, tag: str, prior: Mapping[str, doubt_policy.CpaAnswer] | None = None, rules: str = ""
                    ) -> tuple[dict[str, doubt_policy.CpaAnswer], StepOutcome | None]:
    """One adjudication pass over every doubt, in batches.  `prior` (second pass) is shown to CPA as its earlier answer
    to re-check.  Returns (rows by doubt id, None) or ({}, failure outcome).

    TASK-31: the batches are independent of each other and run at once (parallel.run, bound lane_rules
    concurrency.cpa).  Results come back in batch order, so a failure is reported as it was when the batches ran one
    after another: the earliest failed batch (in batch order) stops the pass, and the record list holds the batches
    that answered before it, then the failing one — the same list a serial pass built.

    TASK-35: each batch carries its own id contract into the call (_batch_answer_contract), so a cut or short answer
    is asked again there and is never handed on as a batch with fewer rows.
    """
    adjudicated: dict[str, doubt_policy.CpaAnswer] = {}
    participants = _participant_line(ctx)
    confusions = _confusion_lines(ctx, [str(t) for d in doubts for t in (d.get("readings") or {}).values()])
    batches = []
    for n, offset in enumerate(range(0, len(doubts), ADJUDICATE_BATCH), 1):
        chunk: list[doubt_policy.FinalDoubt] = []
        for d in doubts[offset:offset + ADJUDICATE_BATCH]:
            view = cast(doubt_policy.FinalDoubt, dict(d))
            if prior is not None and d["id"] in prior:
                view["first_pass"] = first_pass_view(prior[d["id"]])
            if doubt_policy.vote_scheme() == "list_only":
                view.pop("maintenance", None)   # no "how many agree" hint under this scheme
            chunk.append(view)
        windowed = any(str(k).endswith("@win") for d in chunk for k in (d.get("readings") or {}))
        prompt = _render("subtitle_adjudicate.md", doubts=json.dumps(chunk, ensure_ascii=False, indent=1),
                         context=context, participants=participants, confusions=confusions, strict_rules=rules,
                         window_asr_doc=WINDOW_ASR_DOC if windowed else "")
        batches.append((n, prompt, _batch_answer_contract([str(d["id"]) for d in chunk])))
    records = parallel.run(
        batches, lambda batch: _ask_cpa(ctx, f"adjudicate-{tag}-{batch[0]}", batch[1], accepts=batch[2],
                                        purpose="adjudicate"),
        provider="cpa")
    for _batch, record in zip(batches, records, strict=True):
        cpa_calls.append(record)
        if not _cpa_ok(record):
            # TASK-35: a whole answer that left doubts out is reported as the row-missing failure it is (the gate that
            # already names this); a cut answer that stayed cut after the tries is a failed call.
            missing = record.get("reason") == "CPA_ANSWER_MISSING_IDS"
            return {}, _fail("CPA_ROW_MISSING" if missing else "CPA_CALL_FAILED",
                             record.get("stderr_tail", ""), cpa=cpa_calls)
        try:
            rows = _json_from_text(record["response_text"])
        except ValueError:   # a whole JSON value is guaranteed by _ask_cpa; this is a wrong shape, not a cut answer
            return {}, _fail("CPA_RESPONSE_UNPARSEABLE", record["file"], cpa=cpa_calls)
        for row in rows:
            if isinstance(row, dict) and row.get("id"):
                adjudicated[str(row["id"])] = cast(doubt_policy.CpaAnswer, dict(row, cpa_call=record["call_id"]))
    return adjudicated, None


def first_pass_view(row: doubt_policy.CpaAnswer) -> doubt_policy.CpaAnswer:
    """CPA's earlier answer as shown to the next pass or level: the text, the confidence and the reasons (TASK-6 E)."""
    return cast(doubt_policy.CpaAnswer,
                {k: row.get(k) for k in ("state", "final_text", "other_speaker_text", "other_first",
                                         "confidence", "reason", "basis", "work_query")
                 if row.get(k) not in (None, "")})


def _attach_work_lookups(ctx: Ctx, doubts: Sequence[doubt_policy.FinalDoubt],
                         adjudicated: Mapping[str, doubt_policy.CpaAnswer],
                         cache: dict[str, work_lookup.LookupResult] | None = None) -> list[str]:
    """The work or proper name CPA named for a doubt (`work_query`) is looked up once per distinct query through the
    layers of work_lookup (topic graph, Bangumi, zh Wikipedia, Baidu Baike, Search Router when configured) and the
    result is attached to the doubt, so the next pass and the record both see it.  Every mode calls this after each
    adjudication pass.  Returns the ids of the doubts that received a lookup they did not have before.

    TASK-31: the distinct queries of one pass are independent and are looked up at once (parallel.run, bound
    lane_rules concurrency.lookup); the layers inside one query stay serial because baike's formal-name retry and the
    web fallback read the earlier layers' results.  The cache is filled after all lookups return, in query order."""
    graph, _sha = load_topic_entity_graph(work_lookup.GRAPH_PATH)
    cache = {} if cache is None else cache
    wanted: list[str] = []
    for d in doubts:
        query = str((adjudicated.get(d["id"]) or {}).get("work_query") or "").strip()
        if query and query not in cache and query not in wanted:
            wanted.append(query)
    if wanted:
        for query, result in zip(wanted, parallel.run(
                wanted, lambda q: work_lookup.lookup(q, cast(work_lookup.TopicGraph, graph), web=ctx.adapters.lookup),
                provider="lookup"),
                strict=True):
            cache[query] = result
    fresh: list[str] = []
    for d in doubts:
        query = str((adjudicated.get(d["id"]) or {}).get("work_query") or "").strip()
        if not query:
            continue
        if (d.get("work_lookup") or cast(work_lookup.LookupResult, {})).get("query") != query:
            fresh.append(d["id"])
        d["work_lookup"] = cache[query]
    return fresh


def _lookup_process(cache: Mapping[str, work_lookup.LookupResult]) -> list[str]:
    out = []
    for query, res in cache.items():
        layers = "；".join(f"{x['layer']} {x.get('n', 0)} 条" + (f"（失败：{x['error'][:60]}）" if x.get("error") else "")
                          + (f"（{x['skipped']}）" if x.get("skipped") else "") for x in res.get("layers", []))
        out.append(f"作品查证「{query}」：{layers}；采用来源 {res.get('source')}")
    return out


def _corrected_cues(bcut: Sequence[SrtCue], doubts: Sequence[doubt_policy.FinalDoubt],
                    adjudicated: Mapping[str, doubt_policy.CpaAnswer]) -> list[CorrectedCue]:
    """BCUT cues with the first pass's decisions applied: the context the second pass reads (U-rule: fixed text)."""
    text_by_cue = {c["id"]: c["text"] for c in bcut}
    for d in doubts:
        row = adjudicated.get(d["id"])
        if row is None:
            continue
        decided = doubt_policy.decide(row, d["current"], maintained=bool(d["maintenance"]["maintained"]))
        if decided["state"] in {"KEEP", "ADOPT", "FIT_CONTEXT"} and decided["final_text"]:
            text_by_cue[d["cue_id"]] = decided["final_text"]
    other_by_cue = {d["cue_id"]: str((adjudicated.get(d["id"]) or {}).get("other_speaker_text") or "")
                    for d in doubts}
    return [cast(CorrectedCue, dict(c, text=text_by_cue[c["id"]],
                                    other_speaker_text=other_by_cue.get(c["id"], ""))) for c in bcut]


def _final_cues(bcut: Sequence[SrtCue], doubts: Sequence[doubt_policy.FinalDoubt], *, one_speaker: bool = False,
                lines: LinePlan | None = None) -> tuple[list[TrackCue], NormalizeLog]:
    """final.srt cues: BCUT cues with every ADOPT / FIT_CONTEXT text applied, （笑） removed, empty cues dropped (U112),
    then the shape and canon rules (normalize.normalize_cues).  `lines` (lane_lines.LinePlan, TASK-28): a planned cue is
    cut at jianying's boundaries (unless it carries the other person's words) and jianying's orphan lines are added."""
    from . import lane_lines

    final_map = {c["id"]: cast(SrtCue, dict(c)) for c in bcut}
    for d in doubts:
        if d.get("state") in {"ADOPT", "FIT_CONTEXT"} and d.get("final_text") and d.get("cue_id") in final_map:
            final_map[d["cue_id"]]["text"] = cast(str, d["final_text"])
    # A cue with no doubt entry had no voting text at all (filler or laughter only, no reading): it is not subtitled.
    adjudicated_ids = {d.get("cue_id") for d in doubts}
    # the other person's words inside a sentence (U111, U129): an extra line overlapping the sentence in time; the
    # sentence keeps its BCUT time.  The extra line spans the voiceprint's other-speaker chunks, else the cue's 2nd half.
    other = {d["cue_id"]: d for d in doubts if str(d.get("other_speaker_text") or "").strip()
             and d.get("state") in {"KEEP", "ADOPT", "FIT_CONTEXT"} and d.get("cue_id") in final_map}
    cues: list[SrtCue] = []
    for c in bcut:
        if c["id"] not in adjudicated_ids:
            continue
        text = normalize.strip_laughter(final_map[c["id"]]["text"])
        other_doubt = other.get(c["id"])
        extra = (normalize.strip_laughter(str(other_doubt["other_speaker_text"]))
                 if other_doubt else "")
        line = None
        if extra:
            # the other person's line overlaps the sentence; it sits where the voiceprint heard that person, and when
            # CPA says that person spoke first it starts with the sentence and is listed before it (time order)
            a, b = float(c["start_s"]), float(c["end_s"])
            current_doubt = cast(doubt_policy.FinalDoubt, other_doubt)
            first = bool(current_doubt.get("other_first"))
            span = current_doubt.get("other_span_s") or ([a, (a + b) / 2] if first else [(a + b) / 2, b])
            s0, s1 = max(a, float(span[0])), min(b, float(span[1]))
            if first:
                s0 = a
            if s1 - s0 < 0.3:
                s0, s1 = (a, (a + b) / 2) if first else ((a + b) / 2, b)
            line = cast(SrtCue, {"id": c["id"], "start_s": s0, "end_s": s1, "text": extra, "overlap_of": c["id"]})
        if line is not None and line["start_s"] <= float(c["start_s"]):
            cues.append(line)
            line = None
        spec = (lines.splits.get(c["id"]) if lines is not None and other_doubt is None else None)
        pieces = lane_lines.apply_split(final_map[c["id"]], text, spec) if text and spec else None
        if pieces:
            cues.extend(pieces)
        elif text:
            cues.append(cast(SrtCue, dict(final_map[c["id"]], text=text)))
        if line is not None:
            cues.append(line)
    if lines is not None and lines.orphan_lines:
        cues.extend({"id": o["id"], "start_s": o["start_s"], "end_s": o["end_s"],
                     "text": normalize.strip_laughter(o["text"])}
                    for o in lines.orphan_lines)
        cues = [c for c in cues if str(c["text"]).strip()]
        cues.sort(key=lambda c: float(c["start_s"]))   # stable: a line before its sentence stays before it
    return normalize.normalize_cues(cues, one_speaker=one_speaker)



LONE_DROPPED = "subs/lone_dropped.json"


def record_lone(clip_dir: Path, rows: Sequence[normalize.LoneChar], *, fresh: bool) -> None:
    """The isolated characters a writer left out (U138: not subtitled), kept beside final.srt so the scorer counts
    them on neither side (score v4).  Step 3 starts the record afresh; later writers (step 4, a review fix) add."""
    path = clip_dir / LONE_DROPPED
    old = [] if fresh or not path.exists() else json.loads(path.read_text(encoding="utf-8"))
    new = [{"start_s": r["start_s"], "end_s": r.get("end_s", r["start_s"]), "text": r["text"]}
           for r in rows if r.get("action") == "dropped"]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(old + [r for r in new if r not in old], ensure_ascii=False, indent=1), encoding="utf-8")


def lone_char_line(rows: Sequence[normalize.LoneChar]) -> str:
    """The PROCESS line for normalize.join_lone_chars: what was joined, what is not subtitled (U138), and what waits
    for step 4's speakers."""
    joined = [r for r in rows if str(r.get("action", "")).startswith("joined")]
    dropped = [r for r in rows if r.get("action") == "dropped"]
    deferred = [r for r in rows if r.get("action") == "deferred"]
    line = f"孤立单字（发布校验 SRT_SINGLE_CJK_CHARACTER 的同一判定）：并入同一说话人紧挨着的句子 {len(joined)} 处"
    if joined:
        line += "（" + "；".join(f"{r['start_s']:.2f}s「{r['text']}」→{r['why'].split('：', 1)[-1]}" for r in joined) + "）"
    if dropped:
        line += (f"；并不进去、不出字幕 {len(dropped)} 处（U138，打分两边都不计）：" +
                 "；".join(f"{r['start_s']:.2f}s「{r['text']}」" for r in dropped))
    if deferred:
        line += f"；待第 4 步标出说话人 {len(deferred)} 处：" + "；".join(f"{r['start_s']:.2f}s「{r['text']}」" for r in deferred)
    return line


def hold_line(doubts: Sequence[doubt_policy.DecisionView]) -> str:
    """The PROCESS line for doubt_policy.HOLD_RULE: the clip goes on to the package; these keep it from being sealed."""
    held = doubt_policy.holds(doubts)
    if not held:
        return "定不下且影响意思：无"
    return (f"定不下且影响意思 {len(held)} 句（继续做到成片包，不封存、不上传，等 维护者 审校或听辨改进）："
            + "，".join(f"{d.get('id')}「{d.get('final_text')}」" for d in held))


# ----- splice guard: a chosen reading must be one candidate or one coherent fit, never two candidates joined -----
def resolve_concatenation(ctx: Ctx, doubt: doubt_policy.FinalDoubt, row: doubt_policy.CpaAnswer, *, call_prefix: str,
                          context: str) -> tuple[doubt_policy.CpaAnswer, list[CpaEvent], str]:
    """If CPA's chosen text repeats a run of 3+ characters that no single candidate carries, ask once more with that
    as feedback.  Still spliced: take the single candidate with the most independent sources and record why.
    Returns (row, CPA records, note); the note is empty when nothing had to change."""
    final = str(row.get("final_text") or "")
    cands = list(doubt.get("candidates") or [])
    texts = [str(c["text"]) for c in cands]
    if not cands or not doubt_policy.is_concatenation(final, texts):
        return cast(doubt_policy.CpaAnswer, dict(row)), [], ""
    repeat = "、".join(doubt_policy.repeated_runs(final))
    calls: list[CpaEvent] = []
    view = dict(doubt, feedback=f"不要拼接：上一次的答案「{final}」把两个候选拼接后重复了「{repeat}」。"
                                 "只选一个候选，或给出单一的连贯写法。")
    record = _ask_cpa(ctx, call_prefix, _render("subtitle_adjudicate.md",
                                                doubts=json.dumps([view], ensure_ascii=False, indent=1),
                                                context=context, participants=_participant_line(ctx),
                                                confusions=_confusion_lines(ctx, list((doubt.get("readings") or {}).values()))),
                      purpose="concat-check")   # a confirmation that the text is one candidate, not a joint (U168)
    calls.append(record)
    if _cpa_ok(record):
        try:
            rows = _json_from_text(record["response_text"])
            again = next(r for r in rows if isinstance(r, dict) and str(r.get("id")) == doubt["id"])
        except (ValueError, StopIteration):
            again = None
        if again is not None and not doubt_policy.is_concatenation(str(again.get("final_text") or ""), texts):
            return cast(doubt_policy.CpaAnswer, dict(again, cpa_call=record["call_id"])), calls, \
                f"拼接检出（重复「{repeat}」），重问一次后改为单一写法：{again.get('final_text')}"
    best = max(cands, key=lambda c: int(c.get("supporters") or 0))   # ties keep the earlier one (current text first)
    state = "KEEP" if best.get("source") == "current" else "ADOPT"
    chosen: doubt_policy.CpaAnswer = {"id": doubt["id"], "state": state, "final_text": str(best["text"]),
              "affects_meaning": bool(row.get("affects_meaning")), "reason_key": row.get("reason_key"),
              "against_maintained": "",
              "basis": f"拼接检出（重复「{repeat}」），重问后仍拼接，取独立来源最多的单一候选（{best.get('supporters', 0)} 家）",
              "cpa_call": record["call_id"]}
    return chosen, calls, f"拼接未解决，采用独立来源最多的单一候选（{best.get('supporters', 0)} 家）：{best['text']}"
