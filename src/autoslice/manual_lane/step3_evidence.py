"""Step 3 evidence: listening windows and passes, whole-clip and window ASR witnesses, danmaku, the readings
and evidence attached to each cue.

Split out of stage_impl.py (TASK-13); stage_impl re-exports every name, so callers and tests keep
`stage_impl.<name>`.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, NotRequired, TypedDict, cast

from . import (
    evidence_attach,
    normalize,
    overlap_join,
)
from .adapters import remote_python, root_script
from .gates import AnchorLabel
from .lane_rules import rules
from .layout import layout
from .runner import Ctx
from . import session_events
from .srt_text import _norm_text
from .step_io import PROMPTS, REMOTE_TOOLS, _clip, _local, _run_remote

if TYPE_CHECKING:
    from src.autoslice.cpa_gemini_client import JsonValue
    from . import doubt_policy
    from .speaker_v8 import MixedSpeakers
    from .srt_text import SrtCue, TimedText





class ListeningModel(TypedDict):
    name: str
    slug: str


class ListeningLine(TypedDict, total=False):
    start_s: str | float
    start: str | float
    text: str


class ListeningRecord(TypedDict, total=False):
    win: str
    model: str
    slug: str
    status: str
    lines: list[str | ListeningLine]


LISTEN_MODELS: tuple[ListeningModel, ...] = (
    {"name": "Gemini 3.8 Flash (Low)", "slug": "38m"},
    {"name": "gemini-4-argon", "slug": "argon"},
)
M38, ARGON = LISTEN_MODELS
LISTEN_MARGIN_S = 1.5   # at most this much audio before/after a cue; never past the neighbour's gap midpoint (U111)
DANMAKU_XML_ROOT = layout().danmaku_dir   # <recordings root>/<room id> on the recording archive
REVIEWER_SOURCE = "REVIEWER_LISTENED"
ADJUDICATE_BATCH = 10   # smaller batches: a 25-doubt batch with full readings hit upstream 524 (10-31 diagnosis)


class ListeningWindow(evidence_attach.AudioWindow):
    win: str
    core: list[float]
    tempo: float


class OutsideWindow(TypedDict):
    source: str
    win: str
    cue_id: int
    abs_start_s: float
    text: str


class CueEvidence(TypedDict, total=False):
    readings: dict[str, str]
    asr_notes: list[str]
    danmaku: list[str]
    speaker_segments: list[evidence_attach.SourcedSpeakerSegment]
    speaker_note: str
    other_span_s: list[float]
    read_aloud: str
    playbook: dict


class WindowAsrSegment(TypedDict, total=False):
    start_ms: int | float
    end_ms: int | float
    text: str
    speaker: str


class WindowAsrRecord(TypedDict, total=False):
    win: str
    provider: str
    status: str
    segments: list[WindowAsrSegment]
    reused: bool
    excerpt_ms: int
    billed: dict[str, JsonValue]


class WitnessStatus(TypedDict, total=False):
    status: str
    reason_code: str | None
    message: str
    segments: int
    billed: dict[str, JsonValue]


class IvanLabel(AnchorLabel):
    text: NotRequired[str]
    speaker: NotRequired[str]
    choice: NotRequired[str]
    truth_key: NotRequired[str]


class IvanSection(TypedDict, total=False):
    source: str
    export_file: str
    export_sha256: str
    imported_at: str
    labels: list[IvanLabel]


def listen_windows(bcut: Sequence[SrtCue], cut_len: float, *, prefix: str = "",
                   tempo: float = 1.0) -> list[ListeningWindow]:
    """One listening window per BCUT cue (U111): a window carries one sentence and never reaches the start of a
    neighbouring one.  The excerpt runs from the midpoint of the gap to the previous cue (or 1.5 s before the cue)
    to the midpoint of the gap to the next cue (or 1.5 s after it)."""
    windows: list[ListeningWindow] = []
    for index, cue in enumerate(bcut):
        a0, b0 = float(cue["start_s"]), float(cue["end_s"])
        if index > 0:
            prev_end = float(bcut[index - 1]["end_s"])
            lo = max(prev_end + (a0 - prev_end) / 2, a0 - LISTEN_MARGIN_S)
        else:
            lo = a0 - LISTEN_MARGIN_S
        if index + 1 < len(bcut):
            next_start = float(bcut[index + 1]["start_s"])
            hi = min(b0 + (next_start - b0) / 2, b0 + LISTEN_MARGIN_S)
        else:
            hi = b0 + LISTEN_MARGIN_S
        windows.append({"win": f"{prefix}C{index:03d}", "core": [a0, b0],
                        "audio": [max(0.0, lo), min(cut_len, max(hi, b0))],
                        "cue_ids": [cue["id"]], "tempo": tempo})
    return windows


def wide_listen_windows(bcut: Sequence[SrtCue], cue_ids: set[int], cut_len: float,
                        pieces: Sequence[tuple[float, float]]) -> list[ListeningWindow]:
    """Neighbouring sentences and at least three seconds each side, bounded by the clip and each piece.

    Piece coordinates are on the cut clock. Only identical windows share a request; overlapping windows are never
    chained together. Names are based on the actual bounds, and no window crosses a compilation seam.
    """
    intervals: list[tuple[int, float, float, int]] = []
    for i, cue in enumerate(bcut):
        cid = int(cue["id"])
        if cid not in cue_ids:
            continue
        start, end = float(cue["start_s"]), float(cue["end_s"])
        middle = (start + end) / 2
        owner = next((j for j, (lo, hi) in enumerate(pieces) if lo <= middle < hi), None)
        if owner is None:
            raise ValueError(f"cue {cid} has no compilation piece at {middle:.3f}s")
        lower, upper = pieces[owner]
        before = float(bcut[i - 1]["start_s"]) if i else start
        after = float(bcut[i + 1]["end_s"]) if i + 1 < len(bcut) else end
        lo = max(0.0, lower, min(start - 3.0, before))
        hi = min(cut_len, upper, max(end + 3.0, after))
        if hi <= lo:
            raise ValueError(f"cue {cid} has an empty wide listening window")
        intervals.append((owner, lo, hi, cid))
    merged: list[tuple[int, float, float, list[int]]] = []
    for owner, lo, hi, cid in sorted(intervals):
        if merged and (owner, lo, hi) == merged[-1][:3]:
            old_owner, old_lo, old_hi, ids = merged[-1]
            merged[-1] = (old_owner, old_lo, old_hi, [*ids, cid])
        else:
            merged.append((owner, lo, hi, [cid]))
    return [{"win": f"W-{round(lo * 1000):09d}-{round(hi * 1000):09d}",
             "audio": [lo, hi], "core": [lo, hi], "cue_ids": ids, "tempo": 1.0}
            for _, lo, hi, ids in merged]


LAST_LISTEN_STATS: dict[str, int] = {}   # the most recent listening pass: requested, reused, new, and each status count


def upload_listen_tools(ctx: Ctx, prompt: str = "listen_blind.md") -> None:
    """The one copy of listen_windows.py and the prompt on the media host: every pass of a step reads the same files,
    so a step that starts several passes at once uploads them once, before the passes start (TASK-31)."""
    rd = ctx.remote_dir
    for name, src in (("listen_windows.py", REMOTE_TOOLS / "listen_windows.py"),
                      (prompt, PROMPTS / prompt)):
        ctx.adapters.remote.put(src, f"{rd}/listen/{name}")


def _parse_listen_counts(stdout: str) -> dict[str, int]:
    """The remote tool ends with `LISTEN_DONE <jobs> records <n> {json counts}`; no such line means no counts."""
    for line in reversed(stdout.splitlines()):
        if line.startswith("LISTEN_DONE"):
            m = re.search(r"(\{.*\})\s*$", line)
            return {k: int(v) for k, v in json.loads(m.group(1)).items()} if m else {}
    return {}


def _listen_pass(ctx: Ctx, label: str, windows: Sequence[ListeningWindow],
                 models: Sequence[ListeningModel], *, prompt: str = "listen_blind.md",
                 stats: dict[str, int] | None = None, prepared: bool = False,
                 one_attempt: bool = False) -> list[ListeningRecord] | None:
    """One remote listening pass.  Returns the records of THIS pass only: the (window, model) pairs it requested,
    matched on each record's `win` and `model` fields (the model name, not the slug, so a leftover of another thinking
    tier cannot be read as this pass).  The remote all.json holds every record of the clip; callers accumulate across
    passes.  Returns None when the remote tool itself failed.  A model that errors on a window is a record, not a
    failure.

    TASK-31: two passes of one step may run at once (the windows file and the models file carry the pass label, so
    they never collide).  `prepared` says the shared files — the tool and the prompt — were already uploaded by the
    caller once, which is what a parallel caller does before it starts the passes.  `stats` (a dict the caller owns)
    receives this pass's counts; without it they go to LAST_LISTEN_STATS as before."""
    rd = ctx.remote_dir
    media = 'cut.mp3' if (ctx.clip_dir / 'cut/cut.mp3').exists() and not (ctx.clip_dir / 'cut/cut.mp4').exists() else 'cut.mp4'
    cut = f"{rd}/cut/{media}"
    # MANUAL_LANE_REUSE_ONLY=1: use the listening records already on OCI3 and call no model (quota is exhausted)
    reuse_flag = " --reuse-only" if os.environ.get("MANUAL_LANE_REUSE_ONLY") == "1" else ""
    attempt_flag = " --one-attempt" if one_attempt else ""
    local_w = _local(ctx, f"listen/windows-{label}.json")
    local_w.write_text(json.dumps(list(windows), ensure_ascii=False, indent=1), encoding="utf-8")
    local_m = _local(ctx, f"listen/models-{label}.json")
    local_m.write_text(json.dumps(list(models), ensure_ascii=False), encoding="utf-8")
    if not prepared:
        upload_listen_tools(ctx, prompt)
    ctx.adapters.remote.put(local_w, f"{rd}/listen/windows-{label}.json")
    ctx.adapters.remote.put(local_m, f"{rd}/listen/models-{label}.json")
    body = (f"mkdir -p {rd}/listen/out\n"
            + root_script(remote_python(f"{rd}/listen/listen_windows.py {rd}/listen/windows-{label}.json {cut} "
                                        f"{rd}/listen/out {rd}/listen/{prompt} {rd}/listen/models-{label}.json"
                                        f"{reuse_flag}{attempt_flag}"),
                          env=["cpa"], owned=f"{rd}/listen/out", recursive=True))
    res = _run_remote(ctx, f"s3-listen-{label}", body, timeout=5400)
    counts = _parse_listen_counts(res.stdout)
    this_pass = {"requested": len(windows) * len(models), "reused": counts.get("reused", 0),
                 "new": sum(counts.get(k, 0) for k in ("OK", "ERROR", "QUOTA")), **counts}
    (LAST_LISTEN_STATS if stats is None else stats).clear()
    (LAST_LISTEN_STATS if stats is None else stats).update(this_pass)
    if res.rc != 0 or "LISTEN_DONE" not in res.stdout:
        return None
    local_all = _local(ctx, "listen/readings.json")
    ctx.adapters.remote.get(f"{rd}/listen/out/all.json", local_all)
    everything = cast(list[ListeningRecord], json.loads(local_all.read_text(encoding="utf-8")))
    requested = {(w["win"], m["name"]) for w in windows for m in models}
    return [r for r in everything if (r.get("win"), r.get("model")) in requested]


def _pass_tag(win_name: str) -> str:
    return win_name.split("-", 1)[0] if "-" in win_name else "P0"


def _readings_for_cue(cue: SrtCue, lines_by_source: Mapping[str, Sequence[evidence_attach.WitnessLine]]) -> dict[str, str]:
    """Text of each source for this cue (source keys are pass-qualified).  A line that carries `owners` (set by its
    producer through evidence_attach) belongs to exactly those cues, and when it also carries `parts` each owner reads
    only its own slice of the line (an ASR segment over several sentences, TASK-17).  Otherwise: a line with word-level
    timing contributes only its words inside the cue; a line without it contributes its text when it starts near the
    cue (trimmed later against the neighbours, see _trimmed_readings)."""
    readings: dict[str, str] = {}
    for source, lines in lines_by_source.items():
        hits: list[str] = []
        for ln in lines:
            if "owners" in ln:
                # attached by its producer (evidence_attach): ASR segments by overlap, listening lines by window
                if int(cue["id"]) in ln["owners"]:
                    hits.append(ln["parts"][int(cue["id"])] if "parts" in ln else ln["text"])
            elif ln.get("words"):
                hits.append(normalize.words_in_cue(ln["words"], cue["start_s"], cue["end_s"]))
            elif cue["start_s"] - 0.5 <= ln["abs_start_s"] <= cue["end_s"] + 0.5:
                hits.append(ln["text"])
        text = " ".join(h.strip() for h in hits if h and h.strip()).strip()
        if text:
            readings[source] = text
    return readings


def _whole_clip_sources(bcut: Sequence[SrtCue], jy: evidence_attach.AsrAttachment,
                        lines_by_source: Mapping[str, Sequence[evidence_attach.WitnessLine]]
                        ) -> dict[str, list[TimedText]]:
    """The whole-clip readings ({source: [{start_s, end_s, text}]}): BCUT, jianying and the whole-clip MAI / MOSS lines
    (asr:<name>; the window ASR asr:<name>@win and the listening lines are not whole-clip).  overlap_join decides a
    repeat at a sentence join from them (U159)."""
    out: dict[str, list[TimedText]] = {
        "bcut": [{"start_s": float(c["start_s"]), "end_s": float(c["end_s"]), "text": str(c["text"])} for c in bcut],
        "jianying": cast("list[TimedText]", list(jy.segments))}
    for source, lines in lines_by_source.items():
        if source.startswith("asr:") and "@" not in source:
            out[source] = [{"start_s": float(ln["abs_start_s"]), "end_s": float(ln.get("end_s", ln["abs_start_s"])),
                            "text": str(ln["text"])} for ln in lines]
    return out


def _trimmed_readings(cue: SrtCue, lines_by_source: Mapping[str, Sequence[evidence_attach.WitnessLine]],
                      prev: SrtCue | None, nxt: SrtCue | None,
                      sources: Mapping[str, Sequence[TimedText]]) -> dict[str, str]:
    """The readings of this cue with the words that belong to the neighbouring sentences (prev / nxt cues, or None)
    removed (U-rule a reading is the cue's own).  Audio and ASR readings are trimmed by
    overlap_join.trim_reading: a repeat of the neighbour at the join goes only when the whole-clip readings (`sources`)
    say it was said once (U159: a speaker who says it again keeps it). BCUT remains the current text; jianying
    uses the same join when it reaches this entry, as it does at the normal attachment entry."""
    readings = _readings_for_cue(cue, lines_by_source)
    out: dict[str, str] = {}
    for source, text in readings.items():
        if source.startswith(("audio:", "asr:")) or source == "jianying":
            trimmed, _ = overlap_join.trim_reading(text, cue, prev, nxt, sources)
            if trimmed:
                out[source] = trimmed
        else:
            out[source] = text
    return out


def danmaku_timeline(ctx: Ctx, cut_len: float) -> tuple[list[evidence_attach.Danmaku], list[session_events.UnknownSegment]]:
    """(source chat on the cut clock, unknown segments).  Chat is looked up on the live session's timeline, not inside
    the one recording file the cut was taken from (session_events): ordinary comments keep their 90 s/30 s window,
    and SC, gifts and guards reach back to the start of the broadcast, because the streamer reads missed ones long
    after they were sent.  Read-only archive input."""
    clip = _clip(ctx)
    directory = f"{DANMAKU_XML_ROOT}/{clip['recording_date']}"
    stem = Path(clip["source_name"]).stem
    local_xml = _local(ctx, "danmaku/recording.xml")
    if not local_xml.exists():
        local_xml.write_bytes(ctx.adapters.danmaku.fetch(f"{directory}/{stem}.xml"))
    session, unknown = session_events.load(ctx.adapters.danmaku, directory, stem, local_xml.read_bytes(),
                                           _local(ctx, "danmaku/session.json"))
    start = clip["start_ms"] / 1000
    rows: list[evidence_attach.Danmaku] = []
    for event in session:
        t = event["t_s"] - start
        if "type" not in event and -evidence_attach.DANMAKU_BEFORE_S <= t <= cut_len + evidence_attach.DANMAKU_AFTER_S:
            rows.append({"t_s": round(t, 2), "text": event["text"]})
    for event in session:
        t = event["t_s"] - start
        if "type" in event and t <= cut_len:
            rows.append({**event, "t_s": round(t, 2)})
    _local(ctx, "danmaku/lines.json").write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
    return rows, unknown


def _danmaku_lines(ctx: Ctx, cut_len: float) -> list[evidence_attach.Danmaku]:
    return danmaku_timeline(ctx, cut_len)[0]


def _audio_lines(records: Sequence[ListeningRecord], windows: Mapping[str, ListeningWindow]
                 ) -> tuple[dict[str, list[evidence_attach.WitnessLine]], list[OutsideWindow]]:
    """Listening lines by pass-qualified source, each owned by the cue of the window it was heard in (evidence_attach).
    Low-tier outputs vary: a bare string, or an object with start_s / start and text; a bare string has no time and
    starts where the cue starts (the window's lead margin).  Returns (lines by source, lines whose time lies outside
    their own window: recorded, never attached to another cue)."""
    lines_by_source: dict[str, list[evidence_attach.WitnessLine]] = {}
    outside: list[OutsideWindow] = []
    for rec in records:
        w = windows.get(cast(str, rec.get("win")))
        if rec.get("status") != "OK" or w is None:
            continue
        start, tempo = float(w["audio"][0]), float(w["tempo"])
        lead = max(0.0, float(w["core"][0]) - start)
        tag = _pass_tag(rec["win"])
        # a per-speaker listening (pass S) is evidence shown to CPA, not a vote: its lines carry 甲：/乙： labels
        key = f"speakers:{rec['slug']}" if tag == "S" else f"audio:{rec['slug']}@{tag}"
        for ln in rec.get("lines", []):
            if isinstance(ln, str):
                rel, text = lead, ln
            else:
                rel = float(ln.get("start_s", ln.get("start", lead)) or 0.0)
                text = str(ln.get("text", ""))
            if not text.strip():
                continue
            abs_start = start + rel * tempo
            owner = evidence_attach.window_line_owner(w, abs_start)
            if owner is None:
                outside.append({"source": key, "win": rec["win"], "cue_id": int(w["cue_ids"][0]),
                                "abs_start_s": round(abs_start, 2), "text": text})
                continue
            lines_by_source.setdefault(key, []).append({"abs_start_s": abs_start, "text": text, "owners": [owner]})
    return lines_by_source, outside


def _audio_attach_line(lines_by_source: Mapping[str, Sequence[evidence_attach.WitnessLine]],
                       outside: Sequence[OutsideWindow]) -> str:
    attached = sum(len(v) for k, v in lines_by_source.items() if k.startswith("audio:"))
    sample = "；".join(f"{o['win']} {o['source']} {o['abs_start_s']}s「{o['text'][:12]}」" for o in outside[:5])
    return (f"听写行挂接：挂到本窗口的句子 {attached} 行，时间越出本窗口 {len(outside)} 行（只记录，不挂到别的句）"
            + (f"：{sample}" if sample else ""))


def _evidence_for_cue(cue: SrtCue, index: int, bcut: Sequence[SrtCue],
                      jy: evidence_attach.AsrAttachment, witness_att: Mapping[str, evidence_attach.AsrAttachment],
                      lines_by_source: Mapping[str, Sequence[evidence_attach.WitnessLine]],
                      danmaku: Sequence[evidence_attach.Danmaku],
                      mixed: Mapping[int, MixedSpeakers] | None = None
                      ) -> CueEvidence:
    """Everything attached to one cue, the same for every adjudication mode: readings (bcut, jianying, MAI/MOSS,
    listening), the ASR notes (orphan and extra segments), the read-aloud comment, and the ranked danmaku."""
    prev = bcut[index - 1] if index > 0 else None
    nxt = bcut[index + 1] if index + 1 < len(bcut) else None
    readings = {"bcut": cue["text"]}
    jy_text = jy.text(int(cue["id"]))
    sources = _whole_clip_sources(bcut, jy, lines_by_source)

    def trim(text: str) -> str:
        return overlap_join.trim_reading(text, cue, prev, nxt, sources)[0]

    if jy_text:
        readings["jianying"] = trim(jy_text)
    readings.update(_trimmed_readings(cue, lines_by_source, prev, nxt, sources))
    notes = jy.notes(int(cue["id"]), trim=trim)
    for att in witness_att.values():
        notes += att.notes(int(cue["id"]), trim=trim)
    ranked = evidence_attach.danmaku_for_cue(cue, danmaku, list(readings.values()))
    out: CueEvidence = {"readings": readings, "asr_notes": notes, "danmaku": evidence_attach.danmaku_view(ranked)}
    from . import playbook_evidence as pe
    words = [{'start_s': w['abs_start_s'], 'end_s': w['abs_end_s'], 'text': w['text']}
             for line in lines_by_source.get('jianying', []) for w in line.get('words', [])]
    out['playbook'] = {'slots': pe.slots(cue['text'], readings.get('jianying', '')),
                       'readout_matches': pe.readout_matches(cue, list(readings.values()), list(danmaku)),
                       'repeated_danmaku': pe.spam_matches(cue, list(readings.values()), list(danmaku)),
                       'word_time': pe.cue_words(cue, words)}
    speaker_segments = [cast(evidence_attach.SourcedSpeakerSegment,
                             dict(source=source, **{k: s[k] for k in ("speaker", "start_s", "end_s", "text")}))
                        for source, att in witness_att.items() for s in att.by_cue.get(int(cue["id"]), [])
                        if s.get("speaker")]
    speaker_segments += [cast(evidence_attach.SourcedSpeakerSegment,
                              dict(source=source, **cast("Mapping[str, str | float]", s)))
                         for source, rows in lines_by_source.items()
                         for row in rows if int(cue["id"]) in row.get("owners", [])
                         for s in row.get("speaker_segments", [])]
    speaker_segments = [cast(evidence_attach.SourcedSpeakerSegment, dict(s, text=text))
                        for s in speaker_segments if (text := trim(str(s["text"])))]
    if speaker_segments:
        out["speaker_segments"] = speaker_segments
    two = (mixed or {}).get(int(cue["id"]))
    if two:
        out["speaker_note"], out["other_span_s"] = two["note"], two["other_span_s"]
    hit = evidence_attach.read_aloud_hit(cue, danmaku)
    if hit is not None:
        out["read_aloud"] = (f"疑似在念弹幕：原文「{hit['text']}」（句前 {max(0.0, -hit['dt_s']):.0f} 秒发送，"
                             f"与现稿的相似度 {hit['score']}）")
    return out


def _evidence_process(jy: evidence_attach.AsrAttachment, witness_att: Mapping[str, evidence_attach.AsrAttachment],
                      danmaku: Sequence[evidence_attach.Danmaku], read_aloud_cues: Sequence[int]) -> list[str]:
    lines = [jy.process_line()] + [att.process_line() for att in witness_att.values()]
    lines.append(f"弹幕挂接：可归属本片的弹幕 {len(danmaku)} 条（片前 {evidence_attach.DANMAKU_BEFORE_S:.0f} 秒至片后 "
                 f"{evidence_attach.DANMAKU_AFTER_S:.0f} 秒），每句取句前 {evidence_attach.DANMAKU_BEFORE_S:.0f} 秒至句后 "
                 f"{evidence_attach.DANMAKU_AFTER_S:.0f} 秒，按相关度与文本质量排序取前 {evidence_attach.DANMAKU_PER_CUE} 条；"
                 f"疑似念弹幕 {len(read_aloud_cues)} 句{('：cue ' + str(list(read_aloud_cues))) if read_aloud_cues else ''}")
    return lines


def reviewer_label_rows(section: IvanSection | None) -> list[IvanLabel]:
    """Labels imported by `listen-import` (state.reviewer_labels.labels); empty when 维护者 has not listened."""
    return [cast(IvanLabel, dict(row)) for row in (section or {}).get("labels", [])]


def _reviewer_final(cue: SrtCue, label: IvanLabel, *, sha: str, readings: Mapping[str, str],
                differing: Sequence[str], danmaku_near: Sequence[str]) -> doubt_policy.FinalDoubt:
    """维护者 listened to this cue: his reading is final (level 1 bypass), no CPA adjudication."""
    text = str(label["text"])
    state = "KEEP" if _norm_text(text) == _norm_text(cue["text"]) else "ADOPT"
    return {"id": f"d{cue['id']:03d}" if differing else f"维护者{cue['id']:03d}", "cue_id": cue["id"],
            "source": REVIEWER_SOURCE, "current": cue["text"], "readings": dict(readings),
            "differing": list(differing), "danmaku": list(danmaku_near[:12]), "state": state,
            "final_text": text, "affects_meaning": False, "reason_key": REVIEWER_SOURCE,
            "basis": f"维护者 亲听标注（{label.get('choice')}；导出文件 sha256 {sha[:12]}）", "cpa_call": None,
            "maintained": True, "maintenance": {"maintained": True, "reading": text, "families": ["维护者"]}}


# whole-clip ASR witnesses: lane_rules listening.whole_witness (mai,moss, TASK-11; TASK-29 ruling 1);
# MANUAL_LANE_WHOLE_WITNESS (a comma list, or "" for none) overrides it
WHOLE_WITNESS_DEFAULT = str(rules("listening").get("whole_witness") or "")
WITNESS_ENV = ("moss", "mai")   # the witness providers' credential files, each sourced only when present


def whole_witnesses() -> list[str]:
    raw = os.environ.get("MANUAL_LANE_WHOLE_WITNESS", str(rules("listening").get("whole_witness") or ""))
    names = [p.strip() for p in raw.split(",") if p.strip()]
    bad = [n for n in names if n not in ("mai", "moss")]
    if bad:
        raise ValueError(f"MANUAL_LANE_WHOLE_WITNESS: unknown provider(s) {bad}")
    return names


def _asr_witnesses(ctx: Ctx) -> tuple[dict[str, list[evidence_attach.WitnessLine]], dict[str, WitnessStatus]]:
    """MOSS and MAI on the whole clip through the repo's own modules on OCI3 (witness_asr.py: one call per provider
    and cut, reused afterwards).  Credentials are sourced by the root wrapper (adapters.root_script) from the host's moss/mai env files;
    this code never reads or prints them.  A provider that is not available is recorded (status, reason) and the ladder
    continues; a provider not configured for whole-clip use is recorded as such.

    TASK-31: the two providers stay in one call, in the tool's own order.  They are not independent at the file level:
    witness_asr.py extracts one `OUTDIR/audio.mp3` and writes both results into one OUTDIR, so two concurrent calls
    would write the same audio file while the other reads it.  The tool is outside this task's writable files, and the
    call is cached by the audio's sha256 (a rerun of the step is free), so the serial order stays."""
    rd = ctx.remote_dir
    clip = _clip(ctx)
    duration_ms = clip["end_ms"] - clip["start_ms"]
    names = whole_witnesses()
    lines: dict[str, list[evidence_attach.WitnessLine]] = {}
    status: dict[str, WitnessStatus] = {n: {"status": "NOT_USED", "reason_code": "WHOLE_CLIP_WITNESS_OFF"}
                              for n in ("moss", "mai") if n not in names}
    if not names:
        return lines, status
    ctx.adapters.remote.put(REMOTE_TOOLS / "witness_asr.py", f"{rd}/witness/witness_asr.py")
    body = (f"mkdir -p {rd}/witness/out\n"
            + root_script(remote_python(f"{rd}/witness/witness_asr.py {rd}/cut/cut.mp4 {rd}/witness/out "
                                        f"{duration_ms} {','.join(names)}"),
                          env=WITNESS_ENV, optional=True, owned=f"{rd}/witness/out", recursive=True))
    res = _run_remote(ctx, "s3-witness", body, timeout=3600)
    for name in names:
        try:
            local = _local(ctx, f"witness/{name}.json")
            ctx.adapters.remote.get(f"{rd}/witness/out/{name}.json", local)
            doc = json.loads(local.read_text(encoding="utf-8"))
        except (RuntimeError, OSError, ValueError):
            doc = {"status": "UNAVAILABLE", "reason_code": "WITNESS_NOT_PRODUCED",
                   "message": (res.stdout + res.stderr)[-200:]}
        if doc.get("status") == "OK":
            lines[f"asr:{name}"] = [cast(evidence_attach.WitnessLine, {"abs_start_s": seg["start_ms"] / 1000,
                                     "end_s": seg.get("end_ms", seg["start_ms"]) / 1000, "text": seg["text"],
                                     **({"speaker": str(seg["speaker"])} if seg.get("speaker") else {})})
                                    for seg in doc.get("segments", [])]
            status[name] = {"status": "OK", "segments": doc.get("segment_count", len(doc.get("segments", []))),
                            "billed": doc.get("billed") or {}}
        else:
            status[name] = {"status": "UNAVAILABLE", "reason_code": doc.get("reason_code"),
                            "message": str(doc.get("message", ""))[:160]}
    return lines, status


def witness_status_line(status: Mapping[str, WitnessStatus]) -> str:
    """PROCESS: what each whole-clip ASR witness gave (a provider that failed is named with its reason, never silent)."""
    parts = []
    for name in ("mai", "moss"):
        row = status.get(name) or {}
        if row.get("status") == "OK":
            parts.append(f"{name.upper()} 成功 {row.get('segments')} 段（返回用量 {row.get('billed') or '无'}）")
        elif row.get("status") == "NOT_USED":
            parts.append(f"{name.upper()} 不跑整片")
        else:
            parts.append(f"{name.upper()} 不可用（{row.get('reason_code')}：{row.get('message', '')}）")
    return "整片 ASR 证人：" + "；".join(parts)


LAST_ASR_WINDOW_STATS: dict[str, int] = {}   # the most recent window ASR pass: requested, reused, new, statuses


def _asr_window_pass(ctx: Ctx, label: str, windows: Sequence[ListeningWindow], providers: Sequence[str],
                     *, stats_out: dict[str, int] | None = None
                     ) -> list[WindowAsrRecord] | None:
    """One window-level ASR pass (witness_windows.py): each doubt window re-recognised by MAI / MOSS, the same window
    the Gemini listening uses.  Cached on OCI3 by (provider, excerpt bytes); MANUAL_LANE_REUSE_ONLY=1 reads the cache
    only.  Returns this pass's records, or None when the remote tool itself failed.  A provider failing on a window is
    a record, not a failure.

    TASK-31: one call for all providers, as before.  The pass is one tool call over (window, provider) jobs, which the
    tool runs 4 at a time in its own pool (witness_windows.py WORKERS); its excerpt cache lives under one OUTDIR, so
    splitting by provider would only add shell calls, not concurrency.  `stats_out` (a dict the caller owns) receives
    this pass's counts, for a caller that runs several passes at once; without it they go to LAST_ASR_WINDOW_STATS."""
    rd = ctx.remote_dir
    reuse_flag = " --reuse-only" if os.environ.get("MANUAL_LANE_REUSE_ONLY") == "1" else ""
    local_w = _local(ctx, f"witness/windows-{label}.json")
    local_w.write_text(json.dumps(list(windows), ensure_ascii=False, indent=1), encoding="utf-8")
    ctx.adapters.remote.put(REMOTE_TOOLS / "witness_windows.py", f"{rd}/witness/witness_windows.py")
    ctx.adapters.remote.put(local_w, f"{rd}/witness/win/windows-{label}.json")
    body = (f"mkdir -p {rd}/witness/win\n"
            + root_script(remote_python(f"{rd}/witness/witness_windows.py {rd}/witness/win/windows-{label}.json "
                                        f"{rd}/cut/cut.mp4 {rd}/witness/win {','.join(providers)}{reuse_flag}"),
                          env=WITNESS_ENV, optional=True, owned=f"{rd}/witness/win", recursive=True))
    res = _run_remote(ctx, f"s3-asrwin-{label}", body, timeout=5400)
    done = next((ln for ln in reversed(res.stdout.splitlines()) if ln.startswith("WITNESS_WINDOWS_DONE")), "")
    m = re.search(r"(\{.*\})\s*$", done)
    stats = {"requested": len(windows) * len(providers), **(json.loads(m.group(1)) if m else {})}
    (LAST_ASR_WINDOW_STATS if stats_out is None else stats_out).clear()
    (LAST_ASR_WINDOW_STATS if stats_out is None else stats_out).update(stats)
    if res.rc != 0 or not done:
        return None
    local = _local(ctx, f"witness/result-windows-{label}.json")
    ctx.adapters.remote.get(f"{rd}/witness/win/result-windows-{label}.json", local)
    return cast(list[WindowAsrRecord], json.loads(local.read_text(encoding="utf-8")))


def _asr_window_lines(records: Sequence[WindowAsrRecord], windows: Mapping[str, ListeningWindow],
                      cues: Sequence[SrtCue]) -> dict[str, list[evidence_attach.WitnessLine]]:
    """Window ASR lines by source (asr:<provider>@win), each owned by its window's cue.  A window carries one sentence,
    but its audio runs into the gaps around it: each segment goes through the same rule as every other ASR segment
    (evidence_attach.segment_slices over the clip's cues) and only the window cue's slice is kept.  Orphans keep
    the same nearest-cue owner, and an interjection stays whole at the same maximum-overlap owner as a whole-clip
    segment.  The reading is then trimmed against the neighbours like every ASR reading."""
    out: dict[str, list[evidence_attach.WitnessLine]] = {}
    for rec in records:
        w = windows.get(str(rec.get("win")))
        segs = [s for s in rec.get("segments") or [] if str(s.get("text") or "").strip()]
        if rec.get("status") != "OK" or w is None or not segs:
            continue
        own, a0 = int(w["cue_ids"][0]), float(w["audio"][0])
        pieces = []
        speaker_segments: list[evidence_attach.SpeakerSegment] = []
        timed = [cast(evidence_attach.AsrSegment, dict(s, start_s=a0 + int(s["start_ms"]) / 1000,
                      end_s=a0 + int(s.get("end_ms", s["start_ms"])) / 1000)) for s in segs]
        for s in timed:
            s0, s1 = s["start_s"], s["end_s"]
            slices, _orphan, _gap = evidence_attach.segment_slices(
                cues, s0, s1, str(s["text"]), whole_utterance=evidence_attach.is_interjection(cues, s, timed))
            piece = slices.get(own, "").strip()
            if piece:
                pieces.append((s0, piece))
                if s.get("speaker"):
                    speaker_segments.append({"speaker": str(s["speaker"]), "start_s": s0,
                                             "end_s": s1, "text": piece})
        if pieces:
            row: evidence_attach.WitnessLine = {"abs_start_s": pieces[0][0], "text": " ".join(t for _, t in pieces),
                                                "owners": [own]}
            if speaker_segments:
                row["speaker_segments"] = speaker_segments
            out.setdefault(f"asr:{rec['provider']}@win", []).append(row)
    return out


def asr_window_billed(records: Sequence[WindowAsrRecord]) -> dict[str, dict[str, float]]:
    """New window calls per provider: calls, excerpt seconds, and the seconds the provider itself reported."""
    out: dict[str, dict[str, float]] = {}
    for rec in records:
        if rec.get("reused") or rec.get("status") == "MISSING":
            continue
        row = out.setdefault(str(rec.get("provider")), {"calls": 0, "excerpt_s": 0.0, "reported_s": 0.0})
        row["calls"] += 1
        row["excerpt_s"] += int(rec.get("excerpt_ms") or 0) / 1000
        billed = rec.get("billed") or {}
        ms = billed.get("duration_ms")
        row["reported_s"] += float(cast("str | int | float",
                                        ms / 1000 if isinstance(ms, (int, float)) else billed.get("duration") or 0))
    return {k: {kk: round(vv, 2) for kk, vv in v.items()} for k, v in out.items()}
