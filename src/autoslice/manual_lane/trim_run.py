"""The `trim` command: cut unrelated thanks out of the middle of a clip that has finished steps 3 and 4.

Orchestration of the pure parts in trim.py.  For a plain clip X whose subtitles (step 3) and speakers (step 4) are done:

1. CPA is asked twice (prompts/trim.md, call ids trim-1 and trim-2) which runs of final cues are thanks nobody picks up;
   only the cues both answers propose, and only those `trim.guard` lets through, are cut.
2. X is not edited.  A new clip Y (`<X>_trim`) is built from the kept intervals of the same recording with the same
   machinery as a same-session compilation (`compilation.compile_pieces`), runs steps 1, 0 and 2, receives X's decided
   subtitles, speakers and doubts moved onto its clock as a verbatim import (`step_import.record_import`), and runs
   steps 3 and 4 from that import.  Nothing is dictated, listened to or decided again.
3. Only then X is marked SUPERSEDED by Y (`reselect.supersede_candidate`).  Everything X owns on disk besides
   clips/<X>/trim/ stays as it was, so X remains whole and can be returned to.

`trim_clip` returns a dict whose `status` is one of
  TRIMMED    Y exists, X is SUPERSEDED by it                                          (final)
  NONE       nothing to cut: no span both answers proposed that passed the guard      (final)
  FAILED     a call, an answer, the build of Y or one of its steps failed; X is untouched; the next run asks again.  What an
             earlier failed run left of Y (its state file and clips/<Y>/) is moved aside, not deleted, to `….failed-<UTC>`
             and the run goes on from the asking; decision.json says where it went in `retried_from`
  NOT_READY  X's subtitles or speakers are not done
  SKIP_SUPERSEDED / SKIP_COMPILATION / SKIP_IMPORTED   X is not a plain clip with its own decided subtitles
             (an imported X whose push brought subs/cut.srt, the owner's 删 marks, is no SKIP_IMPORTED: see below)
  SKIP_ADVANCED   X has already finished a step after the speaker step (the cut is made before them, not after)
The owner's marks: when X's import brought `cut.srt` (import/cut.srt, the sentences the channel owner marked 删, on X's
clock), those sentences are the cut.  No model is asked; the marks and X's final cues are put in one table by time
(`trim.with_marks`), each run of marks is a span, and everything after that is the same as for a model's spans.  The
guard's NO_THANKS (a model's judgment) does not apply to the owner's decision; EDGE and TOO_SHORT (not possible
mechanically) do: then `status` is FAILED with the spans and the reason, nothing is cut.  decision.json says
`source: owner_cut_marks`.  Y does not take `cut.srt` over (it is already cut).
TRIMMED, NONE and FAILED are also written to clips/<X>/trim/decision.json (a final one is returned as it is on the next
run); the other answers touch nothing.
"""

from __future__ import annotations

import datetime as dt
import json
import shutil
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, NotRequired, TypedDict, cast

from . import compilation, cover_import, gates, publication_id, subtitle_track, trim, writing_canon
from .adapters import Adapters
from .lane_rules import rules
from .recorder_events import Danmaku
from .reselect import SUPERSEDED, supersede_candidate
from .runner import Ctx, run_clip
from .source_read import clip_source_dir
from .srt_text import SrtCue, _json_from_text, parse_srt
from .stages import STEPS
from .state import ClipState, CpaEvent, StateError, bcut_source_zero_ms, sha256_file
from .step_io import _ask_cpa, _cpa_ok, _log

if TYPE_CHECKING:
    from .step_import import ImportedDoubt

PURPOSE = "trim"
SUFFIX = publication_id.TRIM_SUFFIX
OWNER_MARKS = "owner_cut_marks"      # decision["source"] when the cut is the channel owner's 删 marks
MAX_CID = 41                         # the longest candidate id the lane's files accept
FINAL = frozenset({"NONE", "TRIMMED", "SKIP_SUPERSEDED", "SKIP_COMPILATION", "SKIP_IMPORTED"})
QUOTE_CHARS = 200
AdaptersFor = Callable[[str], Adapters]       # a clip's remote directory -> the adapters whose remote runs its scripts


class _Failed(Exception):
    """A stop with its reason.  X has not been changed."""


class RemovedCue(TypedDict):
    n: int
    start_s: float
    end_s: float
    text: str


class RemovedRun(TypedDict):
    span: list[int]
    cues: list[RemovedCue]


class TrimDecision(TypedDict):
    """The result of `trim_clip` and the content of clips/<X>/trim/decision.json; the keys of a run that stopped early
    are the ones it had reached."""

    cid: str
    source: NotRequired[str]
    hook: NotRequired[str]
    successor: NotRequired[str | None]
    retried_from: NotRequired[list[str]]
    answers: NotRequired[list[str]]
    calls: NotRequired[list[CpaEvent]]
    windows: NotRequired[list[tuple[float, float]]]
    pieces_ms: NotRequired[list[tuple[int, int]]]
    proposed: NotRequired[list[list[trim.Span]]]
    agreed: NotRequired[list[trim.Span]]
    passed: NotRequired[list[trim.Span]]
    vetoed: NotRequired[list[trim.Veto]]
    removed: NotRequired[list[RemovedRun]]
    status: NotRequired[str]
    reason: NotRequired[str]


@dataclass
class _Material:
    final_cues: list[subtitle_track.TrackCue]       # subs/final.srt
    speaker_cues: list[subtitle_track.TrackCue]     # the same cues with the speaker of speaker/speaker.srt
    doubts: list[trim.OriginalDoubt]
    bcut_cues: list[SrtCue]
    length_s: float
    owner_spans: list[trim.Span] = field(default_factory=list)      # the owner's marks among the cues above (empty: no marks)


def _decision_path(task: Path, cid: str) -> Path:
    return task / "clips" / cid / "trim" / "decision.json"


def _write_decision(task: Path, cid: str, result: TrimDecision) -> TrimDecision:
    path = _decision_path(task, cid)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return cast(TrimDecision, json.loads(path.read_text(encoding="utf-8")))      # what a later run reads back is what this one returns


def record_unfinished(task: Path, cid: str, reason: str) -> None:
    """Leave 'the trim did not finish' in clips/<cid>/PROCESS.md, in the section shape runner.write_process writes (that
    function takes a step; this is no step, so the section is appended here)."""
    path = task / "clips" / cid / "PROCESS.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with path.open("a", encoding="utf-8") as fh:
        fh.write(f"\n## 片中剪除 · 未完成 · {stamp}\n\n- 片中剪除未完成：{reason}\n")


def _move_residue(task: Path, successor: str) -> list[str]:
    """What an earlier failed attempt left of the successor, locally: state/<Y>.json and clips/<Y>/ renamed to
    `….failed-<UTC>` (never deleted; the media host's directory and joined file stay where they are, the rebuild writes
    over the file).  Returns the new paths; [] when there is nothing."""
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    moved: list[str] = []
    for old in (task / "state" / f"{successor}.json", task / "clips" / successor):
        if not old.exists():
            continue
        new = old.with_name(f"{old.name}.failed-{stamp}")
        if new.exists():
            raise _Failed(f"{new} already exists; the leftover of {successor} was not moved")
        old.rename(new)
        moved.append(str(new))
    return moved


def _ctx(task: Path, cid: str, adapters_for: AdaptersFor, source_dir: str | None = None) -> Ctx:
    state = ClipState.load(task / "state" / f"{cid}.json")
    remote_dir = state.data["clip"]["remote_dir"]
    return Ctx(state=state, session=state.data["session"], clip_dir=task / "clips" / cid, remote_dir=remote_dir,
               adapters=adapters_for(remote_dir), source_dir=source_dir)


# ----- the materials of X --------------------------------------------------------------------------------
def _not_ready(ctx: Ctx) -> str:
    """Why X's subtitles and speakers are not both done, or ''."""
    for key in ("subtitles", "speaker"):
        if ctx.state.step(key).get("status") != "DONE":
            return f"step {key} of {ctx.state.data['clip']['cid']} is not DONE"
    for rel in ("subs/final.srt", "speaker/speaker.srt", "subs/doubts.json"):
        if not (ctx.clip_dir / rel).is_file():
            return f"{ctx.clip_dir / rel} is missing"
    return ""


def owner_marks(ctx: Ctx) -> list[subtitle_track.TrackCue]:
    """The sentences the channel owner marked 删 in the push (import/cut.srt, X's clock); [] when the import brought none."""
    from .step_import import IMPORT_DIR, imported

    rec = imported(ctx)
    if rec is None or "cut.srt" not in rec["files"]:
        return []
    return subtitle_track.parse_blocks((ctx.clip_dir / IMPORT_DIR / "cut.srt").read_text(encoding="utf-8"))


def _material(ctx: Ctx, marks: Sequence[subtitle_track.TrackCue] = ()) -> _Material:
    clip = ctx.state.data["clip"]
    final = subtitle_track.parse_blocks((ctx.clip_dir / "subs/final.srt").read_text(encoding="utf-8"))
    speaker = subtitle_track.parse_blocks((ctx.clip_dir / "speaker/speaker.srt").read_text(encoding="utf-8"))
    if len(speaker) != len(final) or any(abs(a["start_s"] - b["start_s"]) > 1e-3 for a, b in zip(final, speaker, strict=True)):
        raise _Failed(f"speaker/speaker.srt ({len(speaker)} cues) does not match subs/final.srt ({len(final)} cues)")
    doubts = json.loads((ctx.clip_dir / "subs/doubts.json").read_text(encoding="utf-8"))
    bcut_path = ctx.clip_dir / "asr/bcut.srt"
    bcut = parse_srt(bcut_path.read_text(encoding="utf-8")) if bcut_path.is_file() else []
    length_s = (clip["end_ms"] - clip["start_ms"]) / 1000
    if not marks:
        return _Material(final, speaker, doubts, bcut, length_s)
    final, spans = trim.with_marks(final, marks)
    speaker, same = trim.with_marks(speaker, marks)
    if same != spans:
        raise _Failed("the owner's cut marks sit differently among subs/final.srt and speaker/speaker.srt")
    return _Material(final, speaker, doubts, bcut, length_s, spans)


def _events(ctx: Ctx, length_s: float) -> list[Danmaku]:
    """The SC / guard / gift records inside the clip on the clip clock, from step 3's own danmaku product
    (danmaku/lines.json, written by step3_evidence.danmaku_timeline).  A clip without that file has none."""
    path = ctx.clip_dir / "danmaku/lines.json"
    if not path.is_file():
        return []
    rows = cast("list[Danmaku]", json.loads(path.read_text(encoding="utf-8")))
    return [r for r in rows if r.get("type") and 0 <= float(r["t_s"]) <= length_s]


# ----- the two asks --------------------------------------------------------------------------------------
def _ask_twice(ctx: Ctx, task: Path, prompts: Sequence[str], n_cues: int, decision: TrimDecision) -> list[list[trim.Span]]:
    """Both asks in order; returns the spans of each answer.  `decision` gets the raw answers (`answers`) and the CPA
    records (`calls`) as they arrive, so a failed run still shows what was asked and said.  The CPA records of the calls (prompt and
    completion) go under <task>/trim-cpa/<X>/cpa/, away from X's own directory; the raw answers go to
    clips/<X>/trim/answer-N.txt as they came.  _Failed on a failed call or an unusable answer; an unusable answer is also
    dropped from the cache, so the next run asks again."""
    cid = ctx.state.data["clip"]["cid"]
    asking = Ctx(state=ctx.state, session=ctx.session, clip_dir=task / "trim-cpa" / cid, remote_dir=ctx.remote_dir,
                 adapters=ctx.adapters)
    out = ctx.clip_dir / "trim"
    out.mkdir(parents=True, exist_ok=True)
    spans: list[list[trim.Span]] = []
    answers = decision.setdefault("answers", [])
    records = decision.setdefault("calls", [])
    for number, prompt in enumerate(prompts, 1):
        record = _ask_cpa(asking, f"trim-{number}", prompt, purpose=PURPOSE)
        records.append(_log(record).copy())
        (out / f"prompt-{number}.txt").write_text(prompt, encoding="utf-8")
        if not _cpa_ok(record):
            raise _Failed(f"CPA_CALL_FAILED: trim-{number}: {record.get('stderr_tail', '')}")
        answers.append(record["response_text"])
        (out / f"answer-{number}.txt").write_text(record["response_text"], encoding="utf-8")
        try:
            spans.append(trim.parse_answer(_json_from_text(record["response_text"]), n_cues))
        except ValueError as exc:
            Path(str(record.get("cache_path", ""))).unlink(missing_ok=True)
            raise _Failed(f"CPA_RESPONSE_UNUSABLE: trim-{number}: {exc}") from exc
    return spans


# ----- the new clip --------------------------------------------------------------------------------------
def _init_successor(task: Path, ctx: Ctx, successor: str, result: compilation.CompileResult, remote_root: str) -> ClipState:
    """The ordinary init of a compiled clip (cli.cmd_init) from the compilation's entry, with X's session."""
    x = ctx.state.data["clip"]
    entry_file = result.selection_file
    if entry_file is None or result.bcut_srt is None:
        raise StateError("compile_pieces returned no entry or BCUT")
    entry = json.loads(entry_file.read_text(encoding="utf-8"))["input_pool"][0]
    card = entry["selection_scorecard"]
    clip = {
        "cid": successor, "recording_date": x["recording_date"], "source_name": Path(entry["segment_path"]).stem + ".flv",
        "start_ms": int(entry["start_ms"]), "end_ms": int(entry["end_ms"]),
        "start_cue": int(card["start_cue"]), "end_cue": int(card["end_cue"]),
        "proposal": {"start_cue": int(card["start_cue"]), "end_cue": int(card["end_cue"])},
        "hook": str(entry.get("hook", "")), "mac_bcut_srt": str(result.bcut_srt), "bcut_source_zero_ms": 0,
        "remote_dir": f"{remote_root}/{successor}", "source_dir": f"{remote_root}/sources",     # the joined file's directory
        "selection_file": str(entry_file),
        "selection_sha256": sha256_file(entry_file),
    }
    state = ClipState.create(task / "state" / f"{successor}.json", clip=clip, session=ctx.state.data["session"])  # type: ignore[arg-type]
    state.data["released"] = [dict(item) for item in ctx.state.data.get("released", [])]
    state.save()
    return state


def _run_to(ctx: Ctx, until: int) -> None:
    report = run_clip(ctx, STEPS, until=until)
    if report.stopped_at is not None:
        raise _Failed(f"{ctx.state.data['clip']['cid']} stopped at step {report.stopped_at}: {report.reason}")


def _write_push(push: Path, mat: _Material, final: list[trim.NumberedCue], speaker: list[trim.NumberedCue],
                doubts: list[ImportedDoubt]) -> None:
    shutil.rmtree(push, ignore_errors=True)
    (push / "subs").mkdir(parents=True)
    (push / "subs/final.srt").write_text(subtitle_track.format_blocks(final, labels=False), encoding="utf-8")
    if any(c.get("speaker") for c in mat.speaker_cues):      # a uniform-host clip has no labels: step 4 labels it as before
        (push / "speaker").mkdir()
        (push / "speaker/speaker.srt").write_text(subtitle_track.format_blocks(speaker, labels=True), encoding="utf-8")
    (push / "subs/doubts.json").write_text(json.dumps(doubts, ensure_ascii=False, indent=2), encoding="utf-8")


def _build(task: Path, ctx: Ctx, mat: _Material, passed: list[trim.Span], adapters_for: AdaptersFor,
           decision: TrimDecision, source_dir: str | None) -> None:
    """Y, from kept intervals to step 4, then X superseded.  Fields of the new clip are added to `decision`."""
    cid, clip = ctx.state.data["clip"]["cid"], ctx.state.data["clip"]
    successor = f"{cid}{SUFFIX}"
    decision["successor"] = successor
    if len(successor) > MAX_CID:
        raise _Failed(f"the successor id {successor} is longer than {MAX_CID} characters")
    if (task / "state" / f"{successor}.json").exists():
        raise _Failed(f"{successor} already exists")
    windows = trim.keep_windows(mat.final_cues, passed, mat.length_s)
    pieces_ms = trim.source_pieces(windows, clip["start_ms"])
    decision["windows"] = windows
    decision["pieces_ms"] = pieces_ms
    remote_root = ctx.remote_dir.rsplit("/", 1)[0]
    # X's recording is read from where X's state says it is (the pieces' manifest keeps that directory for Y)
    recording_dir = clip_source_dir(ctx.adapters.remote, ctx.state, requested=source_dir, default_dir=f"{remote_root}/sources")
    adapters = adapters_for(f"{remote_root}/{successor}")
    recording = compilation.RecordingInput(clip["source_name"], Path(clip["mac_bcut_srt"]), bcut_source_zero_ms(clip))
    result = compilation.compile_pieces(
        task=task, cid=successor, recording=recording, recording_date=clip["recording_date"], pieces_ms=pieces_ms,
        hook=clip["hook"], origin={"cid": cid, "removed": [list(s) for s in passed]},
        bcut_cues=trim.moved_bcut(mat.bcut_cues, windows),
        source_dir=recording_dir, remote_root=remote_root, adapters=adapters)
    state_y = _init_successor(task, ctx, successor, result, remote_root)
    ctx_y = Ctx(state=state_y, session=state_y.data["session"], clip_dir=task / "clips" / successor,
                remote_dir=state_y.data["clip"]["remote_dir"], adapters=adapters)
    _run_to(ctx_y, 2)

    # X's decided subtitles on Y's clock: the joined clip's measured pieces, then the cut step 2 took from it
    manifest = compilation.load_manifest(ctx_y.clip_dir)
    if manifest is None:
        raise _Failed(f"{successor} has no compilation.json")
    measured = [(low, round(low + (p["compiled_end_ms"] - p["compiled_start_ms"]) / 1000, 3))
                for (low, _), p in zip(windows, manifest["pieces"], strict=True)]
    ycl = state_y.data["clip"]
    offset, length = ycl["start_ms"] / 1000, (ycl["end_ms"] - ycl["start_ms"]) / 1000
    final = trim.shifted(trim.moved(mat.final_cues, passed, measured), offset, length)
    speaker = trim.shifted(trim.moved(mat.speaker_cues, passed, measured), offset, length)
    doubts = trim.carried_doubts(mat.doubts, final_cues=mat.final_cues, bcut_cues=mat.bcut_cues,
                                 clip_start_ms=clip["start_ms"], spans=passed, windows=measured, new_cues=final,
                                 shift_s=offset)
    push = ctx.clip_dir / "trim" / "push"
    _write_push(push, mat, final, speaker, doubts)
    if (canon := ctx.clip_dir / writing_canon.FILE).is_file():    # X's verdicts on its words: Y takes them over
        shutil.copyfile(canon, push / writing_canon.FILE)
    from .step_import import owner_decided, record_import
    record_import(state_y, ctx_y.clip_dir, push, source="trim", authority=f"片中剪除：{cid}", verbatim=True,
                  decided_by="owner" if owner_decided(ctx) else None)
    cover_import.carry(ctx.state, state_y, ctx.clip_dir, ctx_y.clip_dir)      # the owner's finished cover follows the manuscript
    _run_to(ctx_y, 4)

    cut_text = " ".join(" ".join(str(mat.final_cues[n - 1]["text"]).split()) for a, b in passed for n in range(a, b + 1))
    spans_text = "、".join(f"{a}" if a == b else f"{a}–{b}" for a, b in passed)
    why = ("是频道主标为删的句子，视频随字幕一起剪掉" if decision.get("source") == OWNER_MARKS
           else "是与主线无关、没人接的致谢，两遍问答一致且过保险")
    done = supersede_candidate(
        task_dir=task, cid=cid, by=successor, authority=f"片中剪除（trim 命令）：{cid}", quote=cut_text[:QUOTE_CHARS],
        reason=f"片中剪除：第 {spans_text} 句{why}；剪掉后另建 {successor}，原片保留可回退")
    if not done.ok:
        raise _Failed(f"supersede failed: {done.reason} {done.detail}")


def trim_clip(*, task: Path, cid: str, adapters_for: AdaptersFor, source_dir: str | None = None) -> TrimDecision:
    """Run the command for clip `cid` of `task`; see the module docstring.  `source_dir`: where X's recording is on the
    media host when X's state records no directory and it is not <X's work directory>/sources.  `adapters_for(remote_dir)`
    makes the adapters for a clip (the CLI builds the ssh ones; a test hands in fakes)."""
    task = Path(task)
    previous = _decision_path(task, cid)
    if previous.is_file():
        recorded = cast(TrimDecision, json.loads(previous.read_text(encoding="utf-8")))
        if recorded.get("status") in FINAL:
            return recorded                      # decided: neither asked nor built again
    ctx = _ctx(task, cid, adapters_for, source_dir)
    clip = ctx.state.data["clip"]
    head: TrimDecision = {"cid": cid}
    if clip.get("status") == SUPERSEDED:
        return {**head, "status": "SKIP_SUPERSEDED", "reason": f"{cid} is superseded by {clip.get('superseded_by')}"}
    if (ctx.clip_dir / compilation.MANIFEST_FILE).exists():
        return {**head, "status": "SKIP_COMPILATION", "reason": f"{cid} is itself a compilation"}
    marks = owner_marks(ctx)
    if ctx.state.data.get("imported") and not marks:
        return {**head, "status": "SKIP_IMPORTED", "reason": f"the subtitles of {cid} were imported by hand"}
    keys = [step.key for step in STEPS]
    later = [key for key in keys[keys.index("speaker") + 1:] if ctx.state.step(key).get("status") == "DONE"]
    if later:      # burned, packaged or uploaded already: the cut belongs before those steps, never after an upload
        return {**head, "status": "SKIP_ADVANCED", "reason": f"{cid} has already finished later steps: {', '.join(later)}"}
    problem = _not_ready(ctx)
    if problem:
        return {**head, "status": "NOT_READY", "reason": problem}

    decision: TrimDecision = {**head, "hook": clip["hook"], "successor": None}
    try:
        # X is not superseded (returned above) and its last answer was not final: a successor state file is what a build
        # that did not finish left
        if (task / "state" / f"{cid}{SUFFIX}.json").exists():
            decision["retried_from"] = _move_residue(task, f"{cid}{SUFFIX}")
        mat = _material(ctx, marks)
        numbered: list[trim.NumberedCue] = [{"id": n, **c} for n, c in enumerate(mat.final_cues, 1)]
        if marks:       # the channel owner's decision: no model is asked; what cannot be done mechanically stops the run
            decision["source"] = OWNER_MARKS
            spans, agreed = [mat.owner_spans], mat.owner_spans
            passed, vetoed = trim.guard(agreed, numbered, thanks_words=None, clip_len_s=mat.length_s,
                                        min_keep_s=gates.CLIP_MIN_SECONDS)
            if vetoed:
                decision["proposed"], decision["agreed"], decision["passed"], decision["vetoed"] = spans, agreed, passed, vetoed
                raise _Failed("OWNER_CUT_NOT_POSSIBLE: " + "; ".join(
                    f"cues {v['span'][0]}..{v['span'][1]} ({numbered[v['span'][0] - 1]['start_s']}-"
                    f"{numbered[v['span'][1] - 1]['end_s']} s) {v['veto']}: {v['detail']}" for v in vetoed))
        else:
            events = trim.events_text(_events(ctx, mat.length_s))
            prompts = [trim.prompt(clip["hook"], events, numbered, run=run) for run in (1, 2)]
            spans = _ask_twice(ctx, task, prompts, len(numbered), decision)
            agreed = trim.agreed(*spans)
            passed, vetoed = trim.guard(agreed, numbered, thanks_words=rules("trim_thanks_words")["words"],
                                        clip_len_s=mat.length_s, min_keep_s=gates.CLIP_MIN_SECONDS)
        decision["proposed"], decision["agreed"], decision["passed"], decision["vetoed"] = spans, agreed, passed, vetoed
        decision["removed"] = [{"span": [a, b], "cues": [{"n": n, "start_s": numbered[n - 1]["start_s"],
                                                          "end_s": numbered[n - 1]["end_s"], "text": numbered[n - 1]["text"]}
                                                         for n in range(a, b + 1)]} for a, b in passed]
        if not passed:
            return _write_decision(task, cid, {**decision, "status": "NONE", "reason": (
                "no span both answers proposed passed the guard" if agreed else "the two answers share no span to cut")})
        _build(task, ctx, mat, passed, adapters_for, decision, source_dir)
    except _Failed as exc:
        return _write_decision(task, cid, {**decision, "status": "FAILED", "reason": str(exc)})
    except (compilation.CompilationError, StateError, ValueError, RuntimeError, OSError) as exc:
        return _write_decision(task, cid, {**decision, "status": "FAILED", "reason": f"{type(exc).__name__}: {exc}"})
    return _write_decision(task, cid, {**decision, "status": "TRIMMED", "reason": (
        f"cut {sum(b - a + 1 for a, b in passed)} cues{' (the owner marked)' if marks else ''}; "
        f"{decision['successor']} replaces {cid}")})
