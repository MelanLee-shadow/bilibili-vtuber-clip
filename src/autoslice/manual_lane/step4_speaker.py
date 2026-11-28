"""Step 4: speaker (uniform host; a linked clip separates the speakers with the frozen v8 classifier and
the whole-clip clusters).

Split out of stage_impl.py (TASK-13); stage_impl re-exports every name, so callers and tests keep
`stage_impl.<name>`.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, TypedDict, cast

from . import channel
from . import (
    arrival,
    channel_facts,
    collab_guests,
    gates,
    normalize,
    session_voices,
    speaker_v8,
    step4_other_voices as ov,
)
from .lane_rules import rules
from .config import participant_description, participant_names
from .layout import layout
from .runner import Ctx, StepOutcome
from .srt_text import SrtCue, _as_int, _json_from_text, parse_srt
from .step3_adjudicate import PROPER_NOUNS, lone_char_line, record_lone
from .step_import import ImportRecord, imported
from .step_io import (
    HOST_LABEL,
    REMOTE_TOOLS,
    _ask_cpa,
    _cpa_ok,
    _fail,
    _local,
    _log,
    _render,
    _run_remote,
    json_asker,
    linked_session,
)

if TYPE_CHECKING:
    from .state import CpaEvent
    from .subtitle_track import TrackCue

# ----- step 4: speaker (uniform host; colour only with a recorded enablement) --------------------


def _label_host_lines(final_srt: Path) -> str:
    """uniform_host: every cue is the host.  The speaker SRT carries the canonical label on the first line of each
    cue, as the pilot clips do (`[host] text`).  The burn renderer drops the label from the picture.  Timing and
    the text are untouched; this works on the text blocks directly so multi-line cues keep their lines."""
    out_blocks = []
    for block in re.split(r"\n[ \t]*\n", final_srt.read_text(encoding="utf-8").strip()):
        lines = block.splitlines()
        timing = next((i for i, ln in enumerate(lines) if "-->" in ln), None)
        if timing is not None and timing + 1 < len(lines):
            lines[timing + 1] = f"[{HOST_LABEL}] {lines[timing + 1]}"
        out_blocks.append("\n".join(lines))
    return "\n\n".join(out_blocks) + "\n"


def _reviewer_speaker_rows(ctx: Ctx) -> list[speaker_v8.IvanSpeakerLabel]:
    """维护者's imported labels for this clip, with their speaker (the export file the labels were imported from)."""
    维护者 = ctx.state.data.get("reviewer_labels") or {}
    export = 维护者.get("export_file")
    if not export or not Path(export).exists():
        return []
    labels = json.loads(Path(export).read_text(encoding="utf-8")).get("labels", {})
    cid = ctx.state.data["clip"]["cid"]
    return [cast(speaker_v8.IvanSpeakerLabel, dict(v))
            for k, v in labels.items() if str(k).startswith(cid + ":") and "speaker" in v]


SPEAKER_V8_ROOT = layout().speaker_v8_root   # per-clip v8 workspaces on the media host (one directory per clip)
# the frozen v8 workspace (code, classifier, CAM++ venv and model, scorer), handed to the shell tools by name; a linked
# clip's voiceprint runs take session_voices.v8_env instead (the frozen v8, or its session's classifier)
SPEAKER_V8_ENV = session_voices.env_line(layout().speaker_v8)
CHUNKS_ENV = "speaker/chunks-env.txt"        # the SPEAKER_V8_* settings the chunk run was made with
SPEAKER_V8_SCRIPT = REMOTE_TOOLS / "speaker_v8_run.sh"


def _label_speakers(final_srt: Path, labels: Sequence[str]) -> str:
    """speaker.srt: each final cue's first text line gets `[名字] `; timing and text are untouched (as _label_host_lines)."""
    blocks = re.split(r"\n[ \t]*\n", final_srt.read_text(encoding="utf-8").strip())
    if len(blocks) != len(labels):
        raise ValueError(f"speaker labels {len(labels)} do not match final cues {len(blocks)}")
    out_blocks = []
    for block, label in zip(blocks, labels, strict=True):
        lines = block.splitlines()
        timing = next((i for i, ln in enumerate(lines) if "-->" in ln), None)
        if timing is not None and timing + 1 < len(lines):
            lines[timing + 1] = f"[{label}] {lines[timing + 1]}"
        out_blocks.append("\n".join(lines))
    return "\n\n".join(out_blocks) + "\n"


def _speaker_chunks(ctx: Ctx, bcut: Sequence[SrtCue]) -> tuple[dict[int, list[speaker_v8.ChunkPrediction]], str]:
    """Before the subtitles are decided (U129): the frozen v8 classifier on ~1.2 s chunks of every BCUT cue of a linked
    clip, on OCI3 in the clip's own chunk workspace (compute only, no paid model).  Returns ({cue id: [{s, e,
    host_prob}]}, error text).  A clip with one participant, or a failed run, returns no chunks: recorded, never a stop.
    The run is reused when the chunk list is unchanged."""
    if not linked_session(ctx.session):
        return {}, "单人场次，不分说话人"
    return voice_chunks(ctx, bcut)


def voice_chunks(ctx: Ctx, bcut: Sequence[SrtCue]) -> tuple[dict[int, list[speaker_v8.ChunkPrediction]], str]:
    """The voiceprint's reading of ~1.2 s chunks of the given cues, whoever the clip belongs to: ({cue id: [{s, e,
    host_prob}]}, error text).  `_speaker_chunks` is this for a linked clip; `voices.others_heard` asks it of every clip."""
    cid = ctx.state.data["clip"]["cid"]
    units = [u for c in bcut for u in speaker_v8.cue_chunks(int(c["id"]), round(c["start_s"] * 1000),
                                                             round(c["end_s"] * 1000), cid)]
    units_doc = {"units": [dict(u, seg=cid) for u in units]}
    try:
        env = session_voices.v8_env(ctx)
    except session_voices.SessionVoicesError as exc:
        return {}, f"本场分类器训练失败（不影响本步）：{exc}"
    units_local, pred_local = _local(ctx, "speaker/chunks-units.json"), _local(ctx, "speaker/chunks-pred.json")
    same = units_local.exists() and json.loads(units_local.read_text(encoding="utf-8")) == units_doc
    if not (same and pred_local.exists() and chunks_env(ctx) == env):
        rd = ctx.remote_dir
        pred_local.unlink(missing_ok=True)      # never paired with other chunks: a failed run below leaves no reading
        units_local.write_text(json.dumps(units_doc, ensure_ascii=False), encoding="utf-8")
        ctx.adapters.remote.put(units_local, f"{rd}/speaker/chunks-units.json")
        ctx.adapters.remote.put(SPEAKER_V8_SCRIPT, f"{rd}/speaker/speaker_v8_run.sh")
        workdir = f"{SPEAKER_V8_ROOT}/{cid}-chunks"
        res = _run_remote(ctx, "s3-speaker-chunks",
                          f"{env}bash {rd}/speaker/speaker_v8_run.sh {cid} {rd}/cut/cut.mp4 "
                          f"{workdir} {rd}/speaker/chunks-units.json\n", timeout=3600)
        if "SPEAKER_V8_DONE" not in res.stdout:
            return {}, f"声纹分段失败（不影响本步）：rc={res.rc} {res.stdout[-120:]} {res.stderr[-120:]}"
        try:
            ctx.adapters.remote.get(f"{workdir}/out/pred.json", pred_local)
        except RuntimeError as exc:
            return {}, f"声纹分段结果取回失败（不影响本步）：{exc}"
        _local(ctx, CHUNKS_ENV).write_text(env, encoding="utf-8")
    pred_rows = cast("list[speaker_v8.PredictionWithId]",
                     json.loads(pred_local.read_text(encoding="utf-8")).get("predictions", []))
    preds = {str(r["id"]): r for r in pred_rows}
    out: dict[int, list[speaker_v8.ChunkPrediction]] = {}
    for u in units:
        p = preds.get(u["id"])
        if p and p.get("ok"):
            out.setdefault(int(u["cue"]), []).append(
                {"s": u["s"], "e": u["e"], "host_prob": float(dict(p.get("probs") or {}).get(speaker_v8.HOST, 0.0))})
    return out, ""


def chunks_env(ctx: Ctx) -> str:
    """The SPEAKER_V8_* settings the clip's chunk run was made with; a run from before they were recorded was the
    frozen v8's (no other classifier existed then)."""
    path = _local(ctx, CHUNKS_ENV)
    return path.read_text(encoding="utf-8") if path.is_file() else SPEAKER_V8_ENV


def _mixed_by_cue(chunks: Mapping[int, Sequence[speaker_v8.ChunkPrediction]]) -> dict[int, speaker_v8.MixedSpeakers]:
    return {cid: m for cid, ch in chunks.items() if (m := speaker_v8.mixed_speakers(ch)) is not None}


def _speaker_v8(ctx: Ctx) -> StepOutcome:
    """A linked clip (two or more participants, U110): host or guest per cue from the frozen v8 classifier, run on OCI3
    in this clip's own workspace.  Uncertain means guest colour (U114); a failed run stops the step (no guessing)."""
    layout().need_speaker_v8()
    got = _v8_predictions(ctx)
    if isinstance(got, StepOutcome):
        return got
    units, preds, unit_kind = got
    return _speaker_from_preds(ctx, units, preds, unit_kind=unit_kind)


def _v8_predictions(ctx: Ctx) -> tuple[Sequence[speaker_v8.SpeakerChunk], Mapping[str, speaker_v8.RawPrediction], str] | StepOutcome:
    """The frozen v8 classifier's predictions for this clip: (units, predictions by unit id, what a unit is), or the
    failed outcome when the OCI3 run did not finish.  Step 3's ~1.2 s chunk run is used when it exists."""
    rd = ctx.remote_dir
    cid = ctx.state.data["clip"]["cid"]
    bcut = parse_srt(_local(ctx, "asr/bcut.srt").read_text(encoding="utf-8"))
    try:
        env = session_voices.v8_env(ctx)     # the frozen v8, or the session's classifier (switch rule)
    except session_voices.SessionVoicesError as exc:
        return _fail("SESSION_VOICES_FAILED", str(exc))
    chunk_units, chunk_preds = _local(ctx, "speaker/chunks-units.json"), _local(ctx, "speaker/chunks-pred.json")
    if chunk_units.exists() and chunk_preds.exists() and chunks_env(ctx) != env:
        _chunks, err = voice_chunks(ctx, bcut)    # made with another classifier: again with this one
        if err:
            return _fail("SPEAKER_V8_FAILED", err)
    if chunk_units.exists() and chunk_preds.exists():
        # step 3 already scored ~1.2 s chunks of every cue (U129): a line takes the chunks it overlaps, so an
        # interjection line split off a sentence gets its own speaker (training: 107 -> 110 of 125, cue units vs chunks)
        units: list[speaker_v8.SpeakerChunk] = cast("list[speaker_v8.SpeakerChunk]",
                                                    json.loads(chunk_units.read_text(encoding="utf-8"))["units"])
        pred_rows = cast("list[speaker_v8.PredictionWithId]",
                         json.loads(chunk_preds.read_text(encoding="utf-8"))["predictions"])
        return units, {str(r["id"]): r for r in pred_rows}, "约 1.2 秒分段（第 3 步已算）"
    units = [
        {"id": f"{cid}-{round(c['start_s'] * 1000)}", "seg": cid, "s": round(c["start_s"] * 1000),
         "e": round(c["end_s"] * 1000), "lw": round(c["end_s"] * 1000)} for c in bcut]
    units_local = _local(ctx, "speaker/units.json")
    units_local.parent.mkdir(parents=True, exist_ok=True)
    units_local.write_text(json.dumps({"units": units}, ensure_ascii=False), encoding="utf-8")
    ctx.adapters.remote.put(units_local, f"{rd}/speaker/units.json")
    ctx.adapters.remote.put(SPEAKER_V8_SCRIPT, f"{rd}/speaker/speaker_v8_run.sh")
    workdir = f"{SPEAKER_V8_ROOT}/{cid}"
    body = (f"{env}bash {rd}/speaker/speaker_v8_run.sh {cid} {rd}/cut/cut.mp4 {workdir} "
            f"{rd}/speaker/units.json\n")
    res = _run_remote(ctx, "s4-speaker-v8", body, timeout=3600)
    if "SPEAKER_V8_DONE" not in res.stdout:
        return _fail("SPEAKER_V8_FAILED", f"rc={res.rc} stdout={res.stdout[-200:]} stderr={res.stderr[-300:]}")
    pred_path = _local(ctx, "speaker/pred.json")
    ctx.adapters.remote.get(f"{workdir}/out/pred.json", pred_path)
    pred_rows = cast("list[speaker_v8.PredictionWithId]", json.loads(pred_path.read_text(encoding="utf-8"))["predictions"])
    return units, {str(r["id"]): r for r in pred_rows}, "每句一个单元"


def _speaker_from_preds(ctx: Ctx, units: Sequence[speaker_v8.SpeakerChunk],
                        preds: Mapping[str, speaker_v8.RawPrediction], *,
                        unit_kind: str, video: ov.VideoEvidence | None = None,
                        heard: Mapping[str, object] | None = None) -> StepOutcome:
    """The labels of a clip from its v8 predictions.  `video` is the picture evidence of a single-participant clip with
    other voices (step4_other_voices): the host and everyone else are two people, and the review also reads the screen
    subtitles; without it this is the linked-clip path, unchanged."""
    final_path = ctx.clip_dir / "subs/final.srt"
    final_cues = parse_srt(final_path.read_text(encoding="utf-8"))
    cues: list[speaker_v8.MillisecondSpan] = [
        {"start_ms": round(c["start_s"] * 1000), "end_ms": round(c["end_s"] * 1000)} for c in final_cues]
    spans: list[speaker_v8.SpeakerUnit] = [{"id": u["id"], "start_ms": u["s"], "end_ms": u["e"]} for u in units]
    cfg = rules("speaker")
    n_people = speaker_v8.people(ctx.session) if video is None and heard is None else OTHER_VOICES_PEOPLE
    classes = {c for r in preds.values() for c in (r.get("probs") or {})}
    known = video is None and heard is None and speaker_v8.known_participants(ctx.session, classes)
    host_min = speaker_v8.host_threshold(n_people, cfg, known=known)
    decided = [cast(speaker_v8.SpeakerDecision,
                    dict(d, v8_mid=speaker_v8.mid_band(d, n_people, cfg, host_min=host_min)))
               for d in speaker_v8.cue_speakers(cues, spans, preds, host_min=host_min)]
    clip_start = int(ctx.state.data["clip"]["start_ms"])
    decided = speaker_v8.apply_reviewer_labels(
        decided, [(clip_start + c["start_ms"], clip_start + c["end_ms"]) for c in cues], _reviewer_speaker_rows(ctx))
    if video is None:
        fused, cluster_cpa, cluster_process = _speaker_clusters(ctx, final_cues, cues, decided)
    else:
        fused, cluster_cpa, cluster_process = _speaker_video_review(ctx, final_cues, decided, video)
    if fused is None:   # CPA did not answer the review of the undecided cues: a tool failure stops the step
        return _fail("CPA_CALL_FAILED", "speaker-review: " + str(cluster_cpa[-1].get("stderr_tail", ""))[:200],
                     cpa=[_log(c) for c in cluster_cpa], process=cluster_process)
    decided = fused
    labels = [d["speaker"] for d in decided]
    speaker = _local(ctx, "speaker/speaker.srt")
    speaker.write_text(_label_speakers(final_path, labels), encoding="utf-8")
    # step 3 could not join a lone character in a linked clip (no speakers yet); now that every line has its speaker,
    # the same release shape runs on the one track (normalize.release_shape / join_lone_chars) and both files follow
    from . import subtitle_track as st

    track = st.read_track(ctx.clip_dir)
    cuts = {i: d["split"] for i, d in enumerate(decided) if d.get("split")}
    # a line the arrival met with its neighbour where its speaker stops (arrival._switch)
    moved = {i for i, (d, c) in enumerate(zip(decided, cues, strict=True)) if (d["start_ms"], d["end_ms"]) != (c["start_ms"], c["end_ms"])}
    # what someone said into another person's sentence that the review let go unsubtitled (_adopt_interjections)
    drops = {i: [r for r in d.get("interjections") or [] if r["action"] == "dropped"] for i, d in enumerate(decided)}
    drops = {i: rows for i, rows in drops.items() if rows}
    if drops and len(track) != len(decided):
        raise ValueError(f"speaker rows {len(decided)} do not match the subtitle track {len(track)}")
    left_out: list[dict[str, object]] = []
    if (cuts or moved or drops) and len(track) == len(decided):   # a cue split between two speakers: by CPA at the engine's switch (U150), by the arrival at their words
        cut_track: list[TrackCue] = []
        for i, cue in enumerate(track):
            if i in drops:
                kept = _without(plain_text_of(final_cues[i]["text"]), drops[i])
                left_out += [{"start_s": cues[i]["start_ms"] / 1000, "end_s": cues[i]["end_ms"] / 1000, "text": str(r["phrase"]),
                              "reason": f"{r['by'] or '另一人'}插在{r['main']}的话里的{r['kind']}"} for r in drops[i] if r["whole"]]
                if kept:
                    line: TrackCue = {**cue, "text": kept}
                    if i in moved:
                        line["start_s"], line["end_s"] = decided[i]["start_ms"] / 1000, decided[i]["end_ms"] / 1000
                    cut_track.append(line)
            elif i in cuts:
                cut_track.extend({"start_s": p["start_ms"] / 1000, "end_s": p["end_ms"] / 1000, "text": p["text"],
                                  "speaker": p["speaker"]} for p in cuts[i])
            elif i in moved:
                cut_track.append({**cue, "start_s": decided[i]["start_ms"] / 1000, "end_s": decided[i]["end_ms"] / 1000})
            else:
                cut_track.append(cue)
        track = normalize.shape_cues(cut_track)
    joined, lone = normalize.join_lone_chars(normalize._sequence(track))
    if lone or cuts or moved or drops:
        st.write_track(ctx.clip_dir, joined, speakers=True)
    record_lone(ctx.clip_dir, lone, fresh=False)
    _record_interjections(ctx, decided, cues, left_out)
    host_n = sum(1 for d in decided if d["speaker"] == speaker_v8.HOST)
    no_unit = sum(1 for d in decided if d["units"] == 0)
    decision = _local(ctx, "speaker/decision.json")
    # mode: MOSS/MAI clusters gave the labels (8c1), or v8 alone with its mid band read by CPA, or v8 alone
    mid_review = any(d.get("review_route") == speaker_v8.MID_REVIEW for d in decided)
    mode = ("linked_v8_mid_review" if mid_review else
            "linked_v8_clusters" if any(d.get("clusters") for d in decided) else "linked_v8")
    if video is not None:
        mode = OTHER_VOICES_MODE
    elif heard is not None:
        mode = GUESTS_HEARD_MODE
    decision.write_text(json.dumps({"mode": mode, **({"heard": dict(heard)} if heard is not None else {}),
                                    "rule": OTHER_VOICES_RULE if video is not None else f"v8: host iff top={channel.HOST} and p>={host_min} ({n_people} people, lane_rules speaker); uncertain or no unit = guest; mid band -> CPA review; "
                                            "8c1: MOSS clusters named by the host voiceprint give the label (v8 without them); MAI only flags; flagged cues read by CPA, which changes a label only on an acoustic tie or with explicit content evidence (speaker_v8.adopt_review); a cut only at the label engine switch, parts labelled by it",
                                    "cues": decided, "lone_chars": lone,
                                    "video_subtitled": any(d.get("video_subtitled") for d in decided)},
                                   ensure_ascii=False, indent=2), encoding="utf-8")
    kind = "other_voices" if video is not None else GUESTS_HEARD_MODE if heard is not None else "linked_v8"
    lead = ("有他人声音（看视频）v8 二分" if video is not None else
            f"场次没登记嘉宾，但声纹听到别人（{(heard or {}).get('basis')}）：按主播与其他人 v8 二分" if heard is not None else
            "联动 v8 二分")
    return StepOutcome(True, products={"speaker_srt": str(speaker), "decision": str(decision)},
                       note=f"{kind} host={host_n} guest={len(decided) - host_n} no_unit={no_unit}", cpa=cluster_cpa,
                       process=[f"{lead}：{channel.HOST} {host_n} 句，嘉宾 {len(decided) - host_n} 句（其中无可用证人 {no_unit} 句，"
                                f"按嘉宾色）；{session_voices.note(ctx)}，{channel.HOST}阈值 {host_min}（{n_people} 人，lane_rules speaker，e05 §10）；"
                                f"声纹单元：{unit_kind}",
                                *cluster_process, lone_char_line(lone)])


def _without(text: str, rows: Sequence[Mapping[str, object]]) -> str:
    """The line's text without the phrases the review let go (their `span`s, text positions of the plain text)."""
    out = text
    for r in sorted(rows, key=lambda r: -cast("list[int]", r["span"])[0]):
        s0, s1 = cast("list[int]", r["span"])
        out = out[:s0] + out[s1:]
    return out.strip(" ，,、")


INTERJECTIONS = "speaker/interjections.json"


def _record_interjections(ctx: Ctx, decided: Sequence[speaker_v8.SpeakerDecision], cues: Sequence[speaker_v8.MillisecondSpan],
                          left_out: list[dict[str, object]]) -> None:
    """speaker/interjections.json: every candidate the review read, with the line, the phrase, whose sentence it was said
    into and by whom, the review's kind and reason and what was done (dropped / split / kept); a whole line not
    subtitled is recorded as a decided gap for step 8's check of speech without subtitles (package_guards)."""
    from . import package_guards

    rows = [{"line": i + 1, "start_s": cues[i]["start_ms"] / 1000, "end_s": cues[i]["end_ms"] / 1000, **r}
            for i, d in enumerate(decided) for r in d.get("interjections") or []]
    path = _local(ctx, INTERJECTIONS)
    if rows:
        path.write_text(json.dumps(rows, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    else:
        path.unlink(missing_ok=True)
    if left_out:
        package_guards.record_left_out(ctx.clip_dir, left_out, '第 4 步：主要说话人以外的插话不出字幕（公开规则）')


PERSONA = channel.asset("persona.md")


def plain_text_of(text: str) -> str:
    from .subtitle_track import plain_text

    return plain_text(text)


def speaker_context_lines(ctx: Ctx) -> tuple[str, str]:
    """(names, fans) for the speaker review: the on-stage people with their aliases from 维护者's proper-noun table
    (U117), and the fan-culture line of the persona asset.  Nothing here comes from a clip or its truth."""
    on_stage = set(participant_names(ctx.session))
    nouns = json.loads(PROPER_NOUNS.read_text(encoding="utf-8"))
    names = "、".join(f"{n['name']}（{n.get('role', '')}；也叫 {'、'.join(n.get('aliases') or [])}）"
                      for n in nouns if n["name"] in on_stage) or "、".join(sorted(on_stage))
    fans = channel_facts.fans(PERSONA.read_text(encoding="utf-8"))
    if len(participant_names(ctx.session, visual=True)) > len(participant_names(ctx.session)):
        names += "；" + participant_description(ctx.session)
    return names, fans


SPEAKER_CLUSTER_SCRIPT = REMOTE_TOOLS / "speaker_cluster_cos.sh"


def _moss_voiceprint_names(ctx: Ctx, segs: Sequence[speaker_v8.EngineSegment]) -> dict[str, speaker_v8.ClusterName] | None:
    """MOSS clusters named by the host's enrolled voiceprint on OCI3 (speaker_cluster_cos.sh, v8's CAM++ copy):
    speaker_v8.name_by_voiceprint.  Reused while the segments are unchanged.  None when it cannot name them (one cluster,
    or the embedding failed: recorded by the caller as v8 labels), and when the session has more than two people.  8c1
    was validated on two-person collabs only; with three people a cluster can hold two of them and naming the whole
    cluster by its closest centroid labels a guest as the host (11-23, a three-person voice collab: MOSS found two
    clusters in 583860, 897000 and 625460, the host's cluster also held 礼墨Sumi, and every published line of that
    cluster was burned as 李豆沙 although v8 heard a guest; the channel's request).  v8 there is the
    host voiceprint judged cue by cue, the speaker prefill 维护者 corrected on 10-31 (101/115 right; the channel's request)."""
    rd, cid = ctx.remote_dir, ctx.state.data["clip"]["cid"]
    units = [{"id": f"m{i}", "seg": cid, "s": int(sg["start_ms"]), "e": int(sg["end_ms"]), "lw": int(sg["end_ms"])}
             for i, sg in enumerate(segs)]
    index = [{"id": f"m{i}", "cluster": str(sg["speaker"])} for i, sg in enumerate(segs)]
    if len({r["cluster"] for r in index}) < 2 or speaker_v8.people(ctx.session) > 2:
        return None     # one cluster, or more than two people: v8 labels (moss_short_of_people)
    units_local, index_local = _local(ctx, "speaker/moss-units.json"), _local(ctx, "speaker/moss-index.json")
    cent_local = _local(ctx, "speaker/moss-centroids.json")
    doc = {"units": units}
    if not (cent_local.exists() and units_local.exists()
            and json.loads(units_local.read_text(encoding="utf-8")) == doc):
        units_local.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
        index_local.write_text(json.dumps(index, ensure_ascii=False), encoding="utf-8")
        for local, name in ((units_local, "moss-units.json"), (index_local, "moss-index.json"),
                            (SPEAKER_CLUSTER_SCRIPT, "speaker_cluster_cos.sh")):
            ctx.adapters.remote.put(local, f"{rd}/speaker/{name}")
        workdir = f"{SPEAKER_V8_ROOT}/{cid}-moss"
        res = _run_remote(ctx, "s4-moss-voiceprint",
                          f"{SPEAKER_V8_ENV}bash {rd}/speaker/speaker_cluster_cos.sh {cid} {rd}/cut/cut.mp4 "
                          f"{workdir} {rd}/speaker/moss-units.json {rd}/speaker/moss-index.json\n", timeout=3600)
        if "CLUSTER_COS_DONE" not in res.stdout:
            return None
        ctx.adapters.remote.get(f"{workdir}/out/centroids.json", cent_local)
    return speaker_v8.name_by_voiceprint(cast("dict[str, speaker_v8.Centroid]",
                                               json.loads(cent_local.read_text(encoding="utf-8"))))


@dataclass
class _Clusters:
    """The witnesses' whole-clip clusters: per engine, the cluster of each cue, the cluster names and the segments."""
    engines: dict[str, list[speaker_v8.ClusterCue | None]] = field(default_factory=dict)
    named: dict[str, dict[str, speaker_v8.ClusterName]] = field(default_factory=dict)
    segments: dict[str, list[speaker_v8.EngineSegment]] = field(default_factory=dict)
    short_of_people: tuple[int, int] | None = None     # (MOSS clusters, people of the session) when more than two people
    many_people: int = 0                                # the session's people when more than two: no engine clusters
    mai_one_cluster: bool = False                      # MAI heard one cluster: it cannot tell anyone apart
    arrival_line: str = ""                             # what the arrival evidence found (arrival.for_clip), for PROCESS
    interjections: list[arrival.Interjection] = field(default_factory=list)    # arrival.interjections, for the review
    labels: dict[str, str] = field(default_factory=dict)                        # {an arrival person: host or guest label}


class WitnessDocument(TypedDict, total=False):
    status: str
    segments: list[speaker_v8.EngineSegment]


def _witness_clusters(ctx: Ctx, final_cues: Sequence[SrtCue], cues: Sequence[speaker_v8.MillisecondSpan],
                      decided: Sequence[speaker_v8.SpeakerDecision]) -> _Clusters:
    """MOSS clusters named by the host's voiceprint (v8 overlap when it cannot), MAI's named by v8 overlap; MOSS
    without a voiceprint naming is dropped (v8 labels, MAI flags).

    A cluster votes only where it can be one person.  With more than two people neither engine's clusters take part
    (8c1 and the MAI naming were validated on two-person collabs; three people in two or three clusters mix): the
    review counts the routes, and with v8 and MAI alone one MAI vote against v8 is a tie that an unsure reading turns
    into the guest.  MAI names a cluster by the v8 label of most of its time, and the host is the minority of every
    mixed cluster, so every MAI cluster was the guest in all three of 维护者's 11-23 trio clips and in 850930: replayed
    on his 173 labelled lines (53 the host's), v8 alone is wrong on 18, v8 with MAI's vote and an unsure review on 52
    -- 1493180, 1627860, 397290 and 850930 came out with no line of the host's at all .  The same holds
    for MAI hearing a single cluster on any line v8 labels (no MOSS, or a line MOSS does not cover): one name for
    everyone can only tie the other person's lines.  Beside a MOSS label MAI stays the third route as before (three
    routes never tie).  "More than two" is speaker_v8.people, the count the thresholds and the review use: an unnamed
    guest (连线) or a recorded speaker_count is a person too."""
    cl = _Clusters()
    evidence, cl.arrival_line = arrival.for_clip(ctx, decided)
    if evidence is not None:    # the arrival groups given to the host or to a guests' sound path: the label engine, whatever the people count
        cl.named[arrival.ENGINE] = evidence["named"]
        cl.segments[arrival.ENGINE] = evidence["segments"]
        lines: list[arrival.TextSpan] = [{**c, "text": plain_text_of(f["text"])} for c, f in zip(cues, final_cues, strict=True)]
        cl.engines[arrival.ENGINE] = arrival.cue_clusters(lines, evidence["segments"], evidence["named"], evidence["persons"],
                                                          evidence.get("letters") or {})
        cl.interjections = arrival.interjections(lines, evidence["segments"], evidence["persons"], evidence.get("letters") or {})
        cl.labels = {p: evidence["named"][g]["speaker"] for g, p in evidence["persons"].items()}
    if speaker_v8.people(ctx.session) > 2:     # the count the thresholds use: an unnamed guest (连线) is a person too
        cl.many_people = speaker_v8.people(ctx.session)
    for name in ("moss", "mai"):
        path = _local(ctx, f"witness/{name}.json")
        try:
            doc = cast(WitnessDocument, json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue
        segs = [s for s in doc.get("segments") or [] if s.get("speaker")] if doc.get("status") == "OK" else []
        if not segs:
            continue
        cl.named[name] = (_moss_voiceprint_names(ctx, segs) if name == "moss" else None) or \
            speaker_v8.name_clusters(segs, decided)
        cl.engines[name] = speaker_v8.cue_clusters(cues, segs, cl.named[name])
        cl.segments[name] = segs
    if "moss" in cl.named and not any("cos" in v for v in cl.named["moss"].values()):
        cl.engines.pop("moss")   # MOSS without a voiceprint naming (one cluster, embedding failed): v8 labels, MAI flags
        found, people = len({str(sg["speaker"]) for sg in cl.segments["moss"]}), speaker_v8.people(ctx.session)
        if found >= 2 and people > 2:
            cl.short_of_people = (found, people)
    one = "mai" in cl.engines and len({str(sg["speaker"]) for sg in cl.segments["mai"]}) < 2
    if "mai" in cl.engines and (cl.many_people or (one and "moss" not in cl.engines)):
        cl.engines.pop("mai")
        cl.mai_one_cluster = not cl.many_people
    elif one:   # beside MOSS: the single cluster stays the third route only on the lines MOSS labels
        cl.engines["mai"] = [m if lead is not None else None for m, lead in zip(cl.engines["mai"], cl.engines["moss"], strict=True)]
        cl.mai_one_cluster = True
    return cl


def _no_cluster_line(cl: _Clusters) -> str:
    """Why no engine's clusters take part (_witness_clusters), for PROCESS; empty when nothing was set aside."""
    if cl.many_people:
        moss = f"MOSS 分出 {cl.short_of_people[0]} 组；" if cl.short_of_people else ""
        return (f"本场 {cl.many_people} 人（{moss}按组定名只在两人联动上验证过，三人以上一组可能混两个人），"
                "标签用 v8（主播声纹逐句判）；MOSS、MAI 的分簇都不参与：MAI 按 v8 重叠给每组定名，"
                f"{channel.HOST}在混着人的组里都占少数，每组都成了嘉宾，v8 判为{channel.HOST}的句会被它打成平局")
    if cl.mai_one_cluster:
        return "MAI 只分出一组（分不出人），标签按 v8 的句它不参与"
    return ""


def _cluster_line(cl: _Clusters, fused: Sequence[speaker_v8.SpeakerDecision], low: Sequence[int]) -> str:
    return ("说话人分簇：" + "；".join(
        f"{e.upper()} " + "，".join(f"{c}→{v['speaker']}" + (f"（与主播登记声纹余弦 {v['cos']}）" if "cos" in v else "")
                                    for c, v in cl.named[e].items()) for e in cl.engines)
            + (f"；标签用 MOSS 簇（8c1：余弦最高的一簇是{channel.HOST}，其余是嘉宾）" if "moss" in cl.engines else
               "；MOSS 无可用簇，标签用 v8")
            + ("" if "mai" not in cl.engines else
               "，MAI 只分出一组，只在 MOSS 给标签的句上作辅助信号" if cl.mai_one_cluster else
               "，MAI 簇（按 v8 重叠定名）只作辅助信号")
            + f"；与 v8、MAI 都一致 {len(fused) - len(low)} 句，不一致或句内两人、待 CPA 复核 {len(low)} 句")


def _by_words(fused: list[speaker_v8.SpeakerDecision], rows: Sequence[speaker_v8.ClusterCue | None],
              decided: Sequence[speaker_v8.SpeakerDecision]) -> None:
    """What the arrival reads from the words (arrival.cue_clusters) beyond a label: a line whose words are two people's,
    one after the other, is cut at their words (`split`, each part labelled by its own person); two lines of two people
    whose boundary the recognition put before the first one stopped meet where they stop (start_ms / end_ms).  Not on a
    line the owner labelled, nor a boundary beside one."""
    维护者 = [d.get("source") == "维护者" for d in decided]
    for i, row in enumerate(rows):
        if row is None or 维护者[i]:
            continue
        if row.get("parts"):
            fused[i]["split"] = row["parts"]
        times = row.get("times")
        if times and times[1] != fused[i]["end_ms"] and i + 1 < len(fused) and not 维护者[i + 1]:
            fused[i]["end_ms"] = fused[i + 1]["start_ms"] = times[1]


def _arrival_line(fused: Sequence[speaker_v8.SpeakerDecision], low: Sequence[int]) -> str:
    by = [r for r in fused if r.get("label_from") == arrival.ENGINE]
    against = sum(1 for i in low if fused[i].get("label_from") == arrival.ENGINE and len(speaker_v8.acoustic_votes(fused[i])) > 1)
    cut = sum(1 for r in by if r.get("split"))
    return (f"说话人按到达时间：{len(by)} 句由它定（{channel.HOST} {sum(1 for r in by if r['speaker'] == speaker_v8.HOST)} 句；"
            f"字分成先后两人的按字拆成两句 {cut} 句），它没听到或没定下是谁的 {len(fused) - len(by)} 句按 v8；"
            f"待 CPA 复核 {len(low)} 句：字和时间都定不下的（写明这句里先后是谁、各几秒，字落在谁那里）、它没定的，和它判嘉宾"
            f"而主播声纹判{channel.HOST}的 {against} 句（{channel.HOST}的短句会被嘉宾那边同时的声音盖过，这种句两路打平按语境）；"
            "其余声纹和分簇不与它争")


ENGINE_NAMES = {arrival.ENGINE: "到达时间"}


def _switch_note(row: speaker_v8.SpeakerDecision, cue: speaker_v8.MillisecondSpan, cl: _Clusters) -> str:
    """The label engine's change points inside the cue: where a cut may fall and who is on each side."""
    name, notes = row.get("switch_engine"), []
    c0, c1 = int(cue["start_ms"]), int(cue["end_ms"])
    for t in row.get("switches_ms") or []:
        sides = speaker_v8.switch_sides(cue, t, cl.segments[cast(str, name)], cl.named[cast(str, name)])
        if sides and sides[0] != sides[1]:
            notes.append(f"第 {(t - c0) / 1000:.1f} 秒处由{sides[0]}换成{sides[1]}")
    return (f"。给出标签的 {ENGINE_NAMES.get(str(name), str(name).upper())} 在本句（共 {(c1 - c0) / 1000:.1f} 秒）里换人：" + "、".join(notes)
            + "；语境也表明前后是两个人时可以在这里切分") if notes else ""


def _review_prompt(ctx: Ctx, fused: list[speaker_v8.SpeakerDecision], low: Sequence[int], final_cues: Sequence[SrtCue],
                   cues: Sequence[speaker_v8.MillisecondSpan], cl: _Clusters,
                   asked: Sequence[arrival.Interjection] = ()) -> str:
    """The CPA reading of the pending cues with the whole clip's lines.  Marks on each pending row where the label
    engine hears the speaker change inside the cue (the only places a cue may be cut; MAI only flags, so its switches
    do not cut, TASK-18)."""
    for i in low:
        name = fused[i]["label_from"]
        if name in cl.segments:
            switches = speaker_v8.switch_times(cues[i], cl.segments[name], cl.named[name])
            if switches:
                fused[i]["switches_ms"], fused[i]["switch_engine"] = switches, name
    # a mid-band tie shows no label: v8's coin toss would anchor the reading (TASK-29 ruling 5); a line the arrival cut
    # by its words is shown as its parts, each with its own label (_by_words)
    lines = "\n".join(f"{i + 1} " + (" ".join(f"[{p['speaker']}] {p['text']}" for p in r["split"]) if r.get("split") else
                                     f"[{'?' if speaker_v8.mid_tie(r) else r['speaker']}] {final_cues[i]['text']}")
                      for i, r in enumerate(fused))
    pending = "\n".join(f"{i + 1}「{final_cues[i]['text']}」：{fused[i]['speaker_why']}。" + (
        "声音证据打平，可以按语境选一方" if speaker_v8.acoustic_tie(fused[i])
        else f"声音证据倾向{fused[i]['speaker']}，只有明确的内容证据才能改整句的说话人")
        + _switch_note(fused[i], cues[i], cl) for i in low)
    names, fans = speaker_context_lines(ctx)
    who, evidence = _review_setting(ctx, fused)
    return _render("speaker_review.md", host=speaker_v8.HOST, guest=speaker_v8.GUEST_LABEL, lines=lines,
                   pending=pending or "（没有）", names=names, fans=fans, who=who, evidence=evidence,
                   interjections="\n".join(_interjection_row(k, c, final_cues) for k, c in enumerate(asked, 1)) or "（没有）")


# ----- what someone says into another person's sentence (owner) ------------------------------------------
INTERJECTION_DROP = ("附和", "感叹", "无实义", "半句")       # the kinds the owner lets go unsubtitled
INTERJECTION_OWN_LINE = "另一人的话"                        # the other person's own line: cut off when it is at an edge
INTERJECTION_KEEP = ("主说话人的话", "异口同声", "拿不准")


def _interjection_row(k: int, c: arrival.Interjection, final_cues: Sequence[SrtCue]) -> str:
    """One candidate as the review reads it: where it is, who says the sentence around it, who is heard in it, and
    whether it was said while the main speaker talks (only then may it go unsubtitled)."""
    i = c["line"]
    by = f"同时听到{c['by']}"
    where = "整句" if c["whole"] else f"第 {i + 1} 句「{plain_text_of(final_cues[i]['text'])}」里的「{c['text']}」"
    when = (f"{c['main']}说这几个字的时候也在说话（同时说）" if c["together"] else
            f"这几个字是{c['main']}停下以后说的，不是同时说（一个说完另一个接）：不会去掉，只看它是不是另一个人自己的一句话")
    return (f"插{k}：{where}（第 {i + 1} 句，{c['at'][0] / 1000:.1f}–{c['at'][1] / 1000:.1f} 秒）。前后是{c['main']}的话，"
            f"这几个字不落在{c['main']}说话的时间里，{by}；{when}。这段时间里：{c['heard']}"
            + ("。如果是另一个人自己的一句话，会在这里拆成两句" if c["cut"] is not None else ""))


def _question(c: arrival.Interjection, final_cues: Sequence[SrtCue]) -> bool:
    """The candidate is a question (its words end on a question mark in its line): kept, never left out -- the main
    speaker goes on right after it, and a question the next words take up has a meaning of its own (1064220 「是吗？」,
    answered 「哦哦，还真是」)."""
    taken = plain_text_of(final_cues[c["line"]]["text"])[slice(*c["span"])]
    return any(m in taken for m in "？?")


def _interjection_answers(record: CpaEvent) -> dict[int, Mapping[str, object]]:
    """The review's rows on the candidates, by candidate number (「插」: 2, or 「插2」 as the prompt labels it — every one
    of the three replayed answers on 897000 「挖矿挖的」 wrote "插1"); an unreadable answer is no answer: kept."""
    out: dict[int, Mapping[str, object]] = {}
    try:
        for row in _json_from_text(record["response_text"]):
            k = _as_int(str(row.get("插")).strip().removeprefix("插")) if isinstance(row, dict) else None
            if k is not None:
                out[k - 1] = row
    except ValueError:
        return {}
    return out


def _adopt_interjections(fused: list[speaker_v8.SpeakerDecision], asked: Sequence[arrival.Interjection],
                         answers: Mapping[int, Mapping[str, object]], final_cues: Sequence[SrtCue], cl: _Clusters) -> str:
    """The review's reading of each candidate (owner): 附和、感叹、无实义 or 半句 said into another person's
    sentence while that person talks (`together`; one said after the other stops is a turn of its own) is not subtitled (`interjection` on the line's row, applied to the track by _speaker_from_preds);
    another person's own line at an edge of the line is cut off (`split`, as the arrival's cut by words); anything
    else, or no answer, keeps the words.  Not dropped from inside a line whose label is no longer the main speaker's
    (the review of the pending rows moved it), nor from a line cut meanwhile.  Returns the PROCESS line."""
    done: dict[str, int] = {}
    for k, c in enumerate(asked):
        i, answer = c["line"], answers.get(k) or {}
        kind, reason = str(answer.get("kind") or "").strip(), str(answer.get("reason") or "")[:120]
        main = cl.labels.get(c["main"])
        row = {"phrase": c["text"], "span": list(c["span"]), "whole": c["whole"], "main": c["main"], "by": c["by"],
               "at_ms": list(c["at"]), "kind": kind or "没答", "reason": reason}
        if kind in INTERJECTION_DROP and c["together"] and not _question(c, final_cues) \
                and (c["whole"] or fused[i]["speaker"] == main) and not fused[i].get("split"):
            row["action"] = action = "dropped"
        elif kind == INTERJECTION_OWN_LINE and c["cut"] is not None and main and not fused[i].get("split") \
                and fused[i]["speaker"] == main and c["by"] in cl.labels:
            text, (s0, s1) = plain_text_of(final_cues[i]["text"]), c["span"]
            first, second = (text[:s0].strip(), text[s0:].strip()) if s0 else (text[:s1].strip(), text[s1:].strip())
            labels = (main, cl.labels[c["by"]]) if s0 else (cl.labels[c["by"]], main)
            fused[i]["split"] = [{"start_ms": fused[i]["start_ms"], "end_ms": c["cut"], "text": first, "speaker": labels[0]},
                                 {"start_ms": c["cut"], "end_ms": fused[i]["end_ms"], "text": second, "speaker": labels[1]}]
            row["action"] = action = "split"
            row["persons"] = [c["main"], c["by"]] if s0 else [c["by"], c["main"]]
        else:
            row["action"] = action = "kept"
        done[action] = done.get(action, 0) + 1
        fused[i].setdefault("interjections", []).append(row)
    together = sum(1 for c in asked if c["together"])
    return (f"插话（公开规则：主要说话人说话期间别人插的附和、感叹、无实义的话和说了半句的话可以不出字幕）：候选 {len(asked)} 处"
 f"（和主要说话人同时说的 {together} 处，其余是一个说完另一个接、只可能拆句），"
 f"CPA 判为附和、感叹、无实义或半句而不出字幕 {done.get('dropped', 0)} 处，"
 f"判为另一个人自己的话、在句首或句尾拆开 {done.get('split', 0)} 处，保留 {done.get('kept', 0)} 处（有实义、问句、异口同声、拿不准或没答都保留）")


def _review_setting(ctx: Ctx, fused: Sequence[speaker_v8.SpeakerDecision]) -> tuple[str, str]:
    """How many people speak and where the current labels come from, as the review prompt states them.  The prompt
    used to say 「说话的有两位」 and describe MOSS naming for every collab; in the 11-23 three-person collab the
    labels were v8's and three people spoke, so the reader was told the wrong setting."""
    host, guest = speaker_v8.HOST, speaker_v8.GUEST_LABEL
    n = speaker_v8.people(ctx.session)
    who = (f"说话的有 {n} 位：主播「{host}」和 {n - 1} 位连线嘉宾；这一步的标签只分两种，主播标「{host}」，"
           f"其他每一位都标「{guest}」（是哪一位嘉宾另有一步定）。" if n >= 3 else f"说话的有两位：主播「{host}」和连线嘉宾（标签写「{guest}」）。")
    if any(r.get("label_from") == arrival.ENGINE for r in fused):
        evidence = (f"每一句按到达时间定：同一句话在说话人自己的直播里先出现，对照嘉宾的公开录播，时间差分成几组，"
                    f"每组是一路声音，主播声纹认出的组是{host}，认出是别人的组是{guest}。一句话归它的字落在谁说话的时间里的那个人"
                    f"（字的时间来自识别稿，句子起止常偏早偏晚，字比句子的起止准）；字分成先后两段、各属一人的句子已经在字的分界处"
                    f"拆成两句（上面全片的句子里显示成两段，各带自己的标签）；它没听到、或落在声纹认不出的组里的句子按 v8（按主播声纹逐段判断）。待定的句子写了这一句的时间里先后"
                    f"是谁、各几秒，以及哪几个字落在谁说话的时间里（“识别稿里没对上”是识别稿里找不到这几个字的时间，"
                    f"“没听到人声”是那一刻到达时间没听出是谁；写着“前后句对上的字占掉了…”的，是这句自己的字没对上时间，"
                    f"去掉前后句的字占掉的那一头后按剩下的时间定的标签，整段时间里还有别人，所以待定）。"
                    f"到达时间是物理证据，一般比声纹可靠；已知的例外：{host}的短句和嘉宾那边的声音同时出现时，它会把这句算给{guest}，这种句主播声纹判为{host}的列在待定里，两路打平。")
    elif all(r.get("label_from", "v8") == "v8" for r in fused):
        evidence = f"每一句按主播声纹（v8，按主播声纹逐段判断）定是不是{host}，把握不够的算{guest}。"
    else:
        evidence = (f"MOSS 把整片分成几个说话人，与主播登记声纹最像的那一个算{host}，其余算{guest}；"
                    "MOSS 没覆盖的句子按 v8（按主播声纹逐段判断）。")
    return who, evidence


def _review_answers(record: CpaEvent) -> dict[int, speaker_v8.SpeakerReview]:
    """CPA's rows by cue index (an unreadable answer is no answer: the acoustic labels stand)."""
    answers: dict[int, speaker_v8.SpeakerReview] = {}
    try:
        for row in _json_from_text(record["response_text"]):
            if isinstance(row, dict) and _as_int(row.get("n")) is not None:
                answers[int(row["n"]) - 1] = cast(speaker_v8.SpeakerReview, row)
    except ValueError:
        answers = {}
    return answers


def _adopt_answers(fused: list[speaker_v8.SpeakerDecision], low: Sequence[int],
                   answers: Mapping[int, speaker_v8.SpeakerReview], final_cues: Sequence[SrtCue],
                   cues: Sequence[speaker_v8.MillisecondSpan], cl: _Clusters) -> str:
    """Apply the reading to the pending rows (the acoustic label is the default: speaker_v8.adopt_review decides what
    the reading may change; a cut only at the label engine's switch).  Returns the PROCESS line."""
    changed = unsure = refused = 0
    for i in low:
        answer = answers.get(i) or cast(speaker_v8.SpeakerReview, {})
        text = plain_text_of(final_cues[i]["text"])
        said = str(answer.get("speaker") or "").strip()
        took = speaker_v8.adopt_review(fused[i], answer, text)
        unsure += int(took["how"] == "打平，拿不准按嘉宾色")
        changed += int(took["speaker"] != fused[i]["speaker"])
        refused += int("refused" in took)
        fused[i]["speaker_acoustic"] = fused[i]["speaker"]
        fused[i]["speaker"] = took["speaker"]
        fused[i]["speaker_how"] = took["how"]
        fused[i]["speaker_cpa"] = said or None
        fused[i]["speaker_cpa_evidence"] = str(answer.get("evidence") or "")[:20]
        fused[i]["speaker_cpa_reason"] = str(answer.get("reason") or "")[:120]
        if "refused" in took:
            fused[i]["speaker_cpa_refused"] = took["refused"]
        name = fused[i].get("switch_engine")
        pieces = speaker_v8.split_at_switch(cues[i], text, answer.get("split"), fused[i].get("switches_ms") or [],
                                            cl.segments.get(name, []), cl.named.get(name)) if name else None
        if pieces:
            fused[i]["split"] = pieces
        elif answer.get("split"):
            fused[i]["split_refused"] = ("两段拼不回原文，或标签规则听到的两段说话人与语境不一致" if name
                                         else "标签引擎在本句里没有换人时刻")
    cut = sum(1 for i in low if fused[i].get("split"))
    asked = sum(1 for i in low if (answers.get(i) or {}).get("split"))
    return (f"CPA 复核待定 {len(low)} 句：改动 {changed} 句（声音证据打平时按语境，否则只认明确的内容证据；"
            f"没有内容证据、未采纳 {refused} 句），打平且拿不准按嘉宾色 {unsure} 句；CPA 给出切分 {asked} 句，"
            f"在标签引擎换人时刻切开、两段说话人按声音证据 {cut} 句（speaker_v8.split_at_switch）")


def _speaker_clusters(ctx: Ctx, final_cues: Sequence[SrtCue], cues: Sequence[speaker_v8.MillisecondSpan],
                      decided: Sequence[speaker_v8.SpeakerDecision]
                      ) -> tuple[list[speaker_v8.SpeakerDecision] | None, list[CpaEvent], list[str]]:
    """TASK-11 item 6 (U144, coordinator 8c1 rule): the whole-clip MOSS clusters named by the host's enrolled voiceprint
    (8c1) give the label, v8 where MOSS gives none; MAI's clusters (named by v8 overlap) only flag.  The cues where the
    label, v8 or MAI disagree, or an engine hears two people, are read once by CPA with the whole clip's lines.
    The acoustic label is the default (TASK-18): the reading decides only where the acoustic routes tie (uncertain
    = guest, U114), elsewhere only explicit content evidence changes it (speaker_v8.adopt_review); a cut is made
    only at the label engine's switch and each part takes that engine's label.  A cue 维护者 labelled keeps his label.
    Without clusters (a witness missing or without speakers) v8 stands.  Returns (rows, CPA records, PROCESS lines);
    rows is None when the CPA call failed (the step stops: a tool failure, never a silent default)."""
    cl = _witness_clusters(ctx, final_cues, cues, decided)
    if not cl.engines:
        fused = _v8_mid_rows(decided)
        low = [i for i, r in enumerate(fused) if r["speaker_confidence"] == "低"]
        aside = [line for line in (cl.arrival_line, _no_cluster_line(cl)) if line]
        if not low:
            return [cast(speaker_v8.SpeakerDecision, dict(d)) for d in decided], [], aside
        process = [*aside, f"v8 中间带（e05 §10 第3款，lane_rules speaker）：{len(low)} 句交 CPA 复核"
                           f"（两人场按语境与轮次，拿不准按嘉宾色；三人以上只认明确的内容证据）"]
    else:
        by_arrival = arrival.ENGINE in cl.engines
        fused = speaker_v8.fuse(decided, cl.engines, primary=arrival.ENGINE if by_arrival else "moss")
        for row, before in zip(fused, decided, strict=True):
            if before.get("source") == "维护者":
                row["speaker"] = before["speaker"]
                row["speaker_confidence"] = "高"
                row["speaker_why"] = "维护者 标注"
        if by_arrival:
            _by_words(fused, cl.engines[arrival.ENGINE], decided)
        low = [i for i, r in enumerate(fused) if r["speaker_confidence"] == "低"]
        process = ([cl.arrival_line, _arrival_line(fused, low)] if by_arrival else
                   [line for line in (cl.arrival_line, _cluster_line(cl, fused, low)) if line])
    # not in a clip the owner labelled (his labels lock the whole clip: listen-import's, or by name as the guest names
    # and the session samples read them, guest_identity.state_owner_rows), nor on a line the arrival cut between two
    # people by their words
    from . import guest_identity

    owner = (any(d.get("source") == "维护者" for d in decided) or bool(_reviewer_speaker_rows(ctx))
             or bool(guest_identity.state_owner_rows(ctx.state)))
    asked = [] if owner else [c for c in cl.interjections if not fused[c["line"]].get("split")]
    if owner and cl.interjections:
        process.append(f"插话：这条片有频道主的标注，整条片不按插话规则删字、拆句（到达时间找到的 {len(cl.interjections)} 处候选不交复核）")
    calls: list[CpaEvent] = []
    if not low and not asked:
        return fused, calls, process
    record = _ask_cpa(ctx, "speaker-review", _review_prompt(ctx, fused, low, final_cues, cues, cl, asked),
                  purpose="speaker-review")
    calls.append(record)
    if not _cpa_ok(record):
        return None, calls, process
    if low:
        process.append(_adopt_answers(fused, low, _review_answers(record), final_cues, cues, cl))
    if asked:
        process.append(_adopt_interjections(fused, asked, _interjection_answers(record), final_cues, cl))
    return fused, calls, process


def _v8_mid_rows(decided: Sequence[speaker_v8.SpeakerDecision]) -> list[speaker_v8.SpeakerDecision]:
    """v8 alone (no engine clusters): the cues in the v8 mid band (speaker_v8.mid_band) are "低" for the CPA review; a
    cue 维护者 labelled keeps his label."""
    out: list[speaker_v8.SpeakerDecision] = []
    for d in decided:
        mid = "" if d.get("source") == "维护者" else str(d.get("v8_mid") or "")
        why = (f"v8 {channel.HOST}概率 {d.get('host_prob')}，在两人场中间带" if mid == "tie"
               else f"v8 {channel.HOST}概率 {d.get('host_prob')}，三人以上场未到{channel.HOST}阈值" if mid else "")
        out.append(cast(speaker_v8.SpeakerDecision,
                        dict(d, v8_speaker=d["speaker"], clusters={}, label_from="v8", review_route=speaker_v8.MID_REVIEW,
                             speaker_confidence="低" if mid else "高", speaker_why=why)))
    return out


# ----- a single-participant clip with other voices (the streamer watching a video or an animation) ----------------
OTHER_VOICES_PEOPLE = 2          # the host and "everyone else": the two-person thresholds and mid band of lane_rules speaker
OTHER_VOICES_MODE = "other_voices_video"
OTHER_VOICES_ROUTE = "other_voices"
OTHER_VOICES_RULE = (f"v8: host iff top={channel.HOST} and p>=two_person_host_min (host and everyone else = two people, lane_rules speaker); "
                     "picture: frames inside each cue are read by CPA vision for the watched video's own subtitle, the system only "
                     "computes how much of the cue's text it contains; one CPA review (effort.purposes.speaker-video) decides every "
                     "cue from the voiceprint and the picture; it may overturn v8 only on a voiceprint tie, with a screen subtitle "
                     "it quotes from this cue's frames (guest only), or with explicit content evidence (speaker_v8.content_evidence); "
                     "unsure = guest colour; no engine switch exists, so no cue is cut (speaker_v8.split_at_switch); when the "
                     "watched video carries its own subtitles, a cue whose words are on the screen is the video's line and "
                     "the video's lines are not burned (_already_on_screen); words such a video does not show are read as a "
                     "voiceprint tie: the review decides them from what is said (_unshown)")
SCREEN_EVIDENCE = "画面字幕"      # the evidence kind a reading of the watched video's own subtitle gives (guest only)


def _flat(text: str) -> str:
    return " ".join(plain_text_of(text).split())


def _video_rows(decided: Sequence[speaker_v8.SpeakerDecision]) -> list[speaker_v8.SpeakerDecision]:
    """The rows the review reads: every cue the voiceprint labelled, each with v8's label as the default; a cue 维护者
    labelled keeps his label and is not asked."""
    out: list[speaker_v8.SpeakerDecision] = []
    for d in decided:
        维护者 = d.get("source") == "维护者"
        why = "维护者 标注" if 维护者 else f"v8 {channel.HOST}概率 {d.get('host_prob')}"
        out.append(cast(speaker_v8.SpeakerDecision,
                        dict(d, v8_speaker=d["speaker"], clusters={}, label_from="v8", review_route=OTHER_VOICES_ROUTE,
                             speaker_confidence="高" if 维护者 else "低", speaker_why=why)))
    return out


def _voice_tie(row: speaker_v8.SpeakerDecision) -> bool:
    """The voiceprint gives no direction: v8's host probability is in the two-person mid band, or no unit could be
    embedded for the cue (then its host/guest default is only 'uncertain means guest')."""
    return row.get("v8_mid") == "tie" or row.get("units") == 0 or bool(row.get("unshown"))


def _unshown(fused: list[speaker_v8.SpeakerDecision], final_cues: Sequence[SrtCue], frames: Sequence[ov.FrameReading]) -> None:
    """A watched video that carries its own subtitles shows what its people say, so words that are not on the screen
    are the host talking over it or a sound the video left unsubtitled (owner a line of hers said over the
    video was given to the video).  Her voice and the video's are then one mixture and the voiceprint has no direction
    (851360: the video's own lines scored 0.05 to 0.998 for her): such a cue is a tie, its `unshown` words are shown to
    the review, and the review decides from what is said.  Only cues the voiceprint did not give to the host."""
    if not ov.own_subtitles(frames):
        return
    cfg = rules("other_voices")
    for row, cue in zip(fused, final_cues, strict=True):
        if row.get("source") == "维护者" or row["speaker"] == speaker_v8.HOST:
            continue
        start, end = float(cue["start_s"]), float(cue["end_s"])
        text = " ".join(plain_text_of(cue["text"]).split())
        rest = ov.without_shown(text, start, end, frames)
        if (ov.shown_on_screen(text, start, end, frames) < float(cfg["unshown_max_shown"])
                and sum(ch.isalnum() for ch in rest) >= int(cfg["unshown_min_letters"])):
            row["unshown"] = rest


def _video_prompt(ctx: Ctx, fused: Sequence[speaker_v8.SpeakerDecision], asked: Sequence[int],
                  final_cues: Sequence[SrtCue], frames: Mapping[int, Sequence[ov.FrameReading]]) -> str:
    # a voiceprint tie shows no label: v8's coin toss would anchor the reading (as the linked review does)
    lines = "\n".join(f"{i + 1} [{'?' if _voice_tie(r) else r['speaker']}] {_flat(final_cues[i]['text'])}"
                      for i, r in enumerate(fused))
    pending = "\n".join(ov.evidence_line(i, _flat(final_cues[i]["text"]), frames.get(i, []), fused[i].get("host_prob"),
                                         "打平" if _voice_tie(fused[i]) else fused[i]["speaker"],
                                         unshown=str(fused[i].get("unshown") or "")) for i in asked)
    names, fans = speaker_context_lines(ctx)
    return _render("speaker_video.md", host=speaker_v8.HOST, guest=speaker_v8.GUEST_LABEL, lines=lines,
                   pending=pending, names=names, fans=fans)


def _adopt_video(row: speaker_v8.SpeakerDecision, answer: speaker_v8.SpeakerReview | None, text: str,
                 frames: Sequence[ov.FrameReading]) -> speaker_v8.AdoptedSpeaker:
    """The label the review leaves on one cue: v8's label stands unless the review is unsure (guest colour), the
    voiceprint ties (then the review decides), or the review overturns it with evidence the code can check: a screen
    subtitle quoted from this cue's frames (for the guest only), or explicit content evidence."""
    current = str(row["speaker"])
    if answer is None:
        return {"speaker": current, "how": "CPA 未答，按声音证据"}
    said = str(answer.get("speaker") or "").strip()
    if said == "不确定":
        return {"speaker": speaker_v8.GUEST_LABEL, "how": "拿不准，按嘉宾色"}
    if said not in (speaker_v8.HOST, speaker_v8.GUEST_LABEL):
        return {"speaker": current, "how": "CPA 答案无效，按声音证据"}
    if _voice_tie(row):   # v8's label here is a coin toss, so the reading decides, whatever it equals
        return {"speaker": said, "how": "画面字幕里没有，按内容" if row.get("unshown") else "声纹打平，按画面字幕与语境"}
    if said == current:
        return {"speaker": current, "how": "与声音证据一致"}
    if str(answer.get("evidence") or "").strip() == SCREEN_EVIDENCE:
        if said != speaker_v8.GUEST_LABEL:
            why = "画面字幕只能证明这句来自被看视频，不能证明是主播"
        elif not ov.quote_in_readings(str(answer.get("quote") or ""), frames):
            why = "摘抄的画面字幕不在本句取到的帧里"
        else:
            return {"speaker": said, "how": "画面字幕"}
    else:
        why = speaker_v8.content_evidence(answer, text)
        if not why:
            return {"speaker": said, "how": "内容证据"}
    return {"speaker": current, "how": "按声音证据", "refused": why}


def _speaker_video_review(ctx: Ctx, final_cues: Sequence[SrtCue], decided: Sequence[speaker_v8.SpeakerDecision],
                          video: ov.VideoEvidence
                          ) -> tuple[list[speaker_v8.SpeakerDecision] | None, list[CpaEvent], list[str]]:
    """One CPA review of the whole clip (a clip with other voices): every cue not labelled by 维护者 is read with the
    voiceprint and the watched video's screen subtitle at that moment.  Rows are None when the CPA call failed (the
    step stops: a tool failure, never a silent default).  Returns (rows, cpa records incl. the vision calls, PROCESS)."""
    fused = _video_rows(decided)
    frames = ov.by_cue(video.cue.frames)
    asked = [i for i, r in enumerate(fused) if r.get("source") != "维护者"]
    calls: list[CpaEvent] = [*video.screen.calls, *video.cue.calls]
    process = [video.screen.line, video.cue.line]
    for i, row in enumerate(fused):
        subs = [str(f.get("video_subtitle") or "") for f in frames.get(i, []) if f["ok"] and f.get("video_playing")]
        row["screen_subtitles"] = [x for x in subs if x]
        row["screen_match"] = round(ov.best_match(_flat(final_cues[i]["text"]), frames.get(i, []))[0], 2)
    if not asked:
        return fused, calls, process
    _unshown(fused, final_cues, video.cue.frames)
    record = _ask_cpa(ctx, "speaker-video", _video_prompt(ctx, fused, asked, final_cues, frames),
                      purpose="speaker-video")
    calls.append(record)
    if not _cpa_ok(record):
        return None, calls, process
    answers = _review_answers(record)
    changed = unsure = refused = by_screen = 0
    for i in asked:
        answer = answers.get(i)
        took = _adopt_video(fused[i], answer, plain_text_of(final_cues[i]["text"]), frames.get(i, []))
        said = str((answer or {}).get("speaker") or "").strip()
        changed += int(took["speaker"] != fused[i]["speaker"])
        unsure += int(took["how"] == "拿不准，按嘉宾色")
        refused += int("refused" in took)
        by_screen += int(took["how"] == "画面字幕")
        fused[i]["speaker_acoustic"] = fused[i]["speaker"]
        fused[i]["speaker"] = took["speaker"]
        fused[i]["speaker_how"] = took["how"]
        fused[i]["speaker_cpa"] = said or None
        fused[i]["speaker_cpa_evidence"] = str((answer or {}).get("evidence") or "")[:20]
        fused[i]["speaker_cpa_reason"] = str((answer or {}).get("reason") or "")[:120]
        if "refused" in took:
            fused[i]["speaker_cpa_refused"] = took["refused"]
        if fused[i].get("unshown") and took["speaker"] == speaker_v8.HOST:      # which of the unshown words are hers
            fused[i]["unshown"] = ov.host_part(str(fused[i]["unshown"]), (answer or {}).get("host_part"))
        if (answer or {}).get("split"):   # no engine hears a change inside a cue here: the existing rule cuts nowhere
            fused[i]["split_refused"] = "标签引擎在本句里没有换人时刻"
    process.append(f"CPA 逐句复核 {len(asked)} 句（声纹 + 被看视频画面字幕）：改动 {changed} 句，其中凭画面字幕 {by_screen} 句；"
                   f"证据不成立、未采纳 {refused} 句；拿不准按嘉宾色 {unsure} 句；"
                   f"画面字幕吻合度 ≥ 50% 的句子 {sum(1 for r in fused if r.get('screen_match', 0) >= 0.5)} 句")
    process.extend(_already_on_screen(fused, final_cues, video.cue.frames))
    return fused, calls, process


def _already_on_screen(fused: list[speaker_v8.SpeakerDecision], final_cues: Sequence[SrtCue],
                       frames: Sequence[ov.FrameReading]) -> list[str]:
    """A watched video that carries its own subtitles (owner what the video already shows as a subtitle is
    not written again; only the host's own words get subtitles).  A sentence whose words are on the screen is the
    video's line whoever voices it (the host reading along is the same words twice), and no sentence of the video is
    burned (step 5 reads `video_subtitled`).  Sentences the owner labelled keep his label."""
    if not ov.own_subtitles(frames):
        return []
    least = float(rules("other_voices")["on_screen_min"])
    moved = back = 0
    for i, row in enumerate(fused):
        row["video_subtitled"] = True
        row["on_screen"] = round(ov.shown_on_screen(plain_text_of(final_cues[i]["text"]), float(final_cues[i]["start_s"]),
                                                    float(final_cues[i]["end_s"]), frames), 2)
        if row.get("source") == "维护者":
            continue
        text = " ".join(plain_text_of(final_cues[i]["text"]).split())
        rest = ov.without_shown(text, float(final_cues[i]["start_s"]), float(final_cues[i]["end_s"]), frames)
        if row["speaker"] == speaker_v8.GUEST_LABEL:
            continue
        if row.get("unshown"):                     # words the video does not show, given to the host by the review
            back += 1
            if row["unshown"] != text:
                row.update(burned_from=text, burned_text=row["unshown"])
        elif row["on_screen"] >= least:
            row.update(speaker=speaker_v8.GUEST_LABEL, speaker_how="画面上已有这句的字幕")
            moved += 1
        elif row["on_screen"] > 0 and rest != text:      # part of the sentence is on the screen: only the rest is burned
            row.update(burned_from=text, burned_text=rest)
    shown = sum(1 for r in fused if r["speaker"] != speaker_v8.GUEST_LABEL)
    return [f"被看视频自带字幕：画面上已有字幕的句子不再出字幕（{moved} 句由此改判为视频台词，{back} 句画面上没有的话按内容判给主播）；"
            f"成片只烧主播自己的 {shown} 句，视频台词 {len(fused) - shown} 句不烧"]


def _speaker_other_voices(ctx: Ctx, screen: ov.Readings) -> StepOutcome:
    """The route of a single-participant clip whose screen shows a video playing: v8 on every cue (OCI3, compute only),
    the screen subtitle at each cue, one CPA review.  Any failed tool stops the step with its reason."""
    got = _v8_predictions(ctx)
    if isinstance(got, StepOutcome):
        return got
    units, preds, unit_kind = got
    final_cues = parse_srt((ctx.clip_dir / "subs/final.srt").read_text(encoding="utf-8"))
    cue = ov.read_frames(ctx, "cue", ov.cue_requests(final_cues))
    if cue.error:
        return _fail("SPEAKER_SCREEN_FAILED", cue.error, cpa=[*screen.calls, *cue.calls], process=[screen.line, cue.line])
    return _speaker_from_preds(ctx, units, preds, unit_kind=unit_kind, video=ov.VideoEvidence(screen, cue))


def _speaker_import(ctx: Ctx, rec: ImportRecord) -> StepOutcome | None:
    """The imported push's speakers, one per final cue (written by step 3's import), or None when the push has none."""
    sidecar = ctx.clip_dir / "subs/import_speakers.json"
    if not sidecar.exists():
        return None
    labels = json.loads(sidecar.read_text(encoding="utf-8"))
    final_path = ctx.clip_dir / "subs/final.srt"
    if not all(labels):
        return _fail("IMPORT_SPEAKER_MISSING", "the imported push leaves some cues without a speaker")
    speaker = _local(ctx, "speaker/speaker.srt")
    try:
        speaker.write_text(_label_speakers(final_path, labels), encoding="utf-8")
    except ValueError as exc:
        return _fail("IMPORT_SPEAKER_MISMATCH", str(exc))
    decision = _local(ctx, "speaker/decision.json")
    decision.write_text(json.dumps({"mode": "import", "source": rec.get("source"), "labels": labels},
                                   ensure_ascii=False, indent=2), encoding="utf-8")
    host_n = sum(1 for x in labels if x == speaker_v8.HOST)
    return StepOutcome(True, products={"speaker_srt": str(speaker), "decision": str(decision)},
                       note=f"import {rec.get('source')} host={host_n} guest={len(labels) - host_n}",
                       process=[f"说话人导入自 {rec.get('source')}：{channel.HOST} {host_n} 句，{speaker_v8.GUEST_LABEL} "
                                f"{len(labels) - host_n} 句（未跑 v8）"])


GUESTS_HEARD_MODE = "guests_heard_v8"


def guests_heard(ctx: Ctx) -> dict[str, object] | None:
    """Step 3's voiceprint answer (subs/voices.json) when it says, past lane_rules other_voices guests_min_share or
    guests_min_run, that people other than the host talk in a clip of a session that names no guest; else None.
    U453 : such a clip was labelled all host, guests' lines and all."""
    path = ctx.clip_dir / "subs/voices.json"
    try:
        heard = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    cfg = rules("other_voices")
    share, run = heard.get("share"), heard.get("longest_run")
    if not heard.get("others") or share is None or run is None:
        return None
    return heard if float(share) >= float(cfg["guests_min_share"]) or int(run) >= int(cfg["guests_min_run"]) else None


def _name_guests(ctx: Ctx) -> tuple[list[str], list[CpaEvent]] | StepOutcome:
    """collab_guests for a clip of a session that names no guest and in which others are heard: (the day's guests now
    in the session, the judge's call when this clip was the day's first to ask), or the stopped outcome when the judge
    did not answer (the next round asks again, rather than labelling a collab as the host and "the others")."""
    calls: list[CpaEvent] = []
    try:
        return collab_guests.name_guests(ctx, json_asker(ctx, calls), heard=True), calls
    except collab_guests.GuestsUnknown as exc:
        return _fail("CPA_CALL_FAILED", str(exc), cpa=calls)


PERSONS = "speaker/persons.json"     # who says each line, by name: the burn's colours and the title's names read it


def _name_persons(ctx: Ctx) -> tuple[list[CpaEvent], str] | StepOutcome:
    """speaker/persons.json, [{start_s, end_s, text, speaker, person}] per line of the labelled track: the host's lines
    are the host's; a guest line is the guest guest_identity names (two or more named guests: the owner's labels, the
    arrival, the voiceprint, the review of the recognisers' groups; step 6 reads the same record), or the one named
    guest when the session has one and nobody unnamed; null otherwise.  Written again by every run of this step (a
    stale file is removed); a failed naming call stops the step, so the burn and the title never name from two
    different answers.  Returns (CPA records, PROCESS line)."""
    from . import guest_identity
    from . import subtitle_track as st

    path = _local(ctx, PERSONS)
    path.unlink(missing_ok=True)
    track = st.read_track(ctx.clip_dir)
    names = participant_names(ctx.session)
    guests = [n for n in names if n != speaker_v8.HOST]
    calls: list[CpaEvent] = []
    if guest_identity.needed(ctx, track):
        named = guest_identity.identify(ctx, track, json_asker(ctx, calls))
        record = json.loads((ctx.clip_dir / guest_identity.PATH).read_text(encoding="utf-8"))
        if record.get("failed"):
            return _fail("CPA_CALL_FAILED", "guest-identity: 嘉宾是谁这一问没有答复", cpa=calls)
        how = "嘉宾各句是谁按 guest_identity（频道主标注、到达时间、声纹、识别器分组与对话证据）"
    elif len(guests) == 1 and speaker_v8.people(ctx.session) == len(names) and any(
            c.get("speaker") == speaker_v8.GUEST_LABEL for c in track):
        named = {i: guests[0] for i, c in enumerate(track, 1) if c.get("speaker") == speaker_v8.GUEST_LABEL}
        how = f"本场只登记了一位嘉宾、没有未具名的人：嘉宾句都是{guests[0]}"
    else:
        return calls, ""
    rows = [{"start_s": c["start_s"], "end_s": c["end_s"], "text": c["text"], "speaker": c.get("speaker"),
             "person": speaker_v8.HOST if c.get("speaker") == speaker_v8.HOST else named.get(i)}
            for i, c in enumerate(track, 1)]
    path.write_text(json.dumps(rows, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    guest_rows = [r for r in rows if r["speaker"] == speaker_v8.GUEST_LABEL]
    counts = "、".join(f"{p} {sum(1 for r in guest_rows if r['person'] == p)} 句" for p in guests
                      if any(r["person"] == p for r in guest_rows))
    return calls, (f"谁说的（{PERSONS}，烧录颜色与标题封面同用）：{how}；嘉宾 {len(guest_rows)} 句里定到人的 "
                   f"{sum(1 for r in guest_rows if r['person'])} 句（{counts or '无'}），其余按通用嘉宾样式")


def step4_speaker(ctx: Ctx) -> StepOutcome:
    out = _step4_labels(ctx)
    if not out.ok:
        _local(ctx, PERSONS).unlink(missing_ok=True)
        return out
    named = _name_persons(ctx)
    if isinstance(named, StepOutcome):
        return StepOutcome(False, reason=named.reason, note=named.note, cpa=[*out.cpa, *named.cpa], process=out.process)
    calls, line = named
    return StepOutcome(True, products=out.products, note=out.note, cpa=[*out.cpa, *calls], rewind=out.rewind,
                       process=[*out.process, *([line] if line else [])])


def _step4_labels(ctx: Ctx) -> StepOutcome:
    rec = imported(ctx)
    if rec:
        outcome = _speaker_import(ctx, rec)
        if outcome is not None:
            return outcome
    if linked_session(ctx.session):   # U110: a linked clip always separates the speakers
        return _speaker_v8(ctx)
    verdict = gates.speaker_mode_gate(cast(gates.SpeakerSession, ctx.session))
    decision = _local(ctx, "speaker/decision.json")
    decision.write_text(json.dumps({"speaker_mode": ctx.session.get("speaker_mode"),
                                    "enablement_evidence": ctx.session.get("enablement_evidence"),
                                    "rule": ctx.session.get("speaker_rule")}, ensure_ascii=False, indent=2),
                        encoding="utf-8")
    if not verdict.ok:
        return _fail(verdict.reason_code, verdict.detail)
    # a single participant is not always a single voice: the streamer may be watching a video or an animation.  The
    # picture says so (a few frames, CPA vision); a clip that shows none keeps uniform host below, byte for byte.
    screen_frames = ov.screen_requests(parse_srt((ctx.clip_dir / "subs/final.srt").read_text(encoding="utf-8")))
    screen = ov.read_frames(ctx, "screen", screen_frames) if screen_frames else ov.Readings()
    if screen.error:
        return _fail("SPEAKER_SCREEN_FAILED", screen.error, cpa=screen.calls, process=[screen.line])
    if ov.video_playing(screen.frames):
        return _speaker_other_voices(ctx, screen)
    heard = guests_heard(ctx)
    if heard is not None:   # nobody named a guest, but the voiceprint hears others talk
        named = _name_guests(ctx)        # who the day's record says they are, before the speakers are told apart
        if isinstance(named, StepOutcome):
            return named
        guests, guest_calls = named
        if guests:                       # a linked clip from here on: the people count, the thresholds and the arrival evidence are its own
            out = _speaker_v8(ctx)
            return StepOutcome(out.ok, reason=out.reason, products=out.products, note=out.note,
                               cpa=[*screen.calls, *guest_calls, *out.cpa],
                               process=[*([screen.line] if screen.frames else []), collab_guests.process_line(ctx, guests), *out.process])
        # host and the others, two people
        got = _v8_predictions(ctx)
        if isinstance(got, StepOutcome):
            return got
        units, preds, unit_kind = got
        out = _speaker_from_preds(ctx, units, preds, unit_kind=unit_kind, heard=heard)
        return StepOutcome(out.ok, reason=out.reason, products=out.products, note=out.note,
                           cpa=[*screen.calls, *guest_calls, *out.cpa], process=[*([screen.line] if screen.frames else []), *out.process])
    speaker = _local(ctx, "speaker/speaker.srt")
    speaker.write_text(_label_host_lines(ctx.clip_dir / "subs/final.srt"), encoding="utf-8")
    return StepOutcome(True, products={"speaker_srt": str(speaker), "decision": str(decision)},
                       note=f"speaker_mode={verdict.evidence['speaker_mode']}", cpa=screen.calls,
                       process=["uniform host：每句都是主播，不加说话人标签（无分色开启记录）",
                                *([screen.line, "画面上没有在播放的被看视频，声纹也没听到够多的别人声音，保持 uniform host"] if screen.frames else [])])
