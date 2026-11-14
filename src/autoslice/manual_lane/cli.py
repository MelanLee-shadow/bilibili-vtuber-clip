"""Command-line driver for the manual-lane pipeline (wrapped by scripts/manual_lane.py).

init           create state/<cid>.json from a selection-result entry and a session config
auto           one unattended round: discover, then every unfinished clip of the task is run on (auto_run.py)
discover       recordings of a day -> candidates -> clips: inventory, whole-recording subtitles, selection, `init` of each
               chosen candidate (discover.py); `status` without --cid lists its ledger
compile        same-session theme compilation: CPA picks disjoint intervals of one session, they are cut and joined
               into one virtual source, and `init` is run on it (then `run` takes it like any clip; compilation.py)
run            run the steps, each when the steps it depends on are done (stages.DEPENDS; steps that do not depend on
               each other run at once); a step is skipped when its products exist.  --cid may be repeated (or
               --cids-file, one per line): the clips run side by side, each with its own log file.  --upload needs
               --quote and runs step 10, the upload (step10_run.py).  --trim (off by default): after the speaker step, try the mid-clip cut of
               `trim` and, when it makes a new clip, run the later steps on that one.  A clip whose import brought
               subs/cut.srt (the owner's 删 marks) takes that cut without --trim, and stops if it cannot be made
status         print the step table for one clip
listen-queue   build the static listening page for the UNRESOLVED, meaning-affecting doubts (muted audio)
listen-import  store 维护者's exported labels in each clip's state (validated all-or-nothing)
import-subtitles  a hand push's decided subtitles and speakers become step 3's input (U130); steps 3+ run again
resolve-dissent   replace the default (keep) of one review objection to what the owner edited: keep it by name or take the review's
reselect       --from-counter only: open a candidate from CPA's last cut (U103)
supersede      mark a candidate SUPERSEDED by a named successor (its file is kept)
trim           cut unrelated thanks out of a finished clip: ask CPA twice, build a new clip without them, move the decided
               subtitles onto it, supersede the original (trim_run.py).  Exit 0 trimmed / nothing to cut / skipped, 3 not
               ready (steps 3 and 4 not done), 2 failed
score          score clips/<cid>/subs/final.srt against truth labels (cer_whole first; time-aligned text, speaker)
revise         a published clip is redone in a successor clip <cid>_r<N> of the same manuscript (its package lands in its own
               directory, step 8 builds the same-BV repair authority); the original is superseded (revise_run.py)
record-title-receipt  the title-only edit receipt (title-repair's VERIFIED_TITLE_ONLY) of a published manuscript is replayed in full in the
               root boundary and registered under the manuscript; step 8 of every successor then carries it (title_receipt.py)
import-cover   a push's finished cover (cover/cover.png + cover/cover.json) becomes step 6's cover as it is; --withdraw takes it back (cover_import.py)
redo           --from <step name or number>: forget that step and the steps that depend on it (state backed up under the clip), and
               make a joined source (compilation, trim successor) again from its recorded plan; `run` then makes them (redo.py)
"""

from __future__ import annotations

import argparse
import json
import os
import re
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, cast

from .adapters import Adapters, DanmakuSource, LocalRemote, Remote, SshCpaJudge, SshRemote
from .chat_archive import ArchiveChat
from .clip_init import SelectionDocument, SelectionEntry, clip_from_entry, create_clip_state
from .compilation import RecordingInput, compile_session
from . import dissent_register, persona_observe
from .config import load_session_config
from .layout import layout
from .listen import ListenError, clip_queue, import_labels, write_queue
from .reselect import SUPERSEDED, load_cues, reselect_from_counter, supersede_candidate
from .multi_run import Progress, clip_ids, run_many, say
from .runner import Ctx, RunReport, UploadNotAuthorized, check_upload_request, run_clip
from .score import (
    SCORER_VERSIONS,
    TSV_COLUMNS,
    error_lines,
    format_table,
    load_truth,
    score_clip,
    truth_clip_id,
    tsv_rows,
)
from .stages import DEPENDS, GATES, STEPS
from .state import DEFAULT_BCUT_SOURCE_ZERO_MS, ClipState, StateError, bcut_source_zero_ms, clip_run_lock
from .trim_run import AdaptersFor, owner_marks, record_unfinished, trim_clip
from .work_lookup import WebLookup

if TYPE_CHECKING:
    from .listen import QueuePlan, QueueState


SPEAKER_STEP = next(step.number for step in STEPS if step.key == "speaker")      # `run --trim` cuts after this step


def _selection_entry(selection_path: Path, cid: str) -> SelectionEntry:
    data = cast(SelectionDocument, json.loads(Path(selection_path).read_text(encoding="utf-8")))
    for entry in data.get("input_pool", []):
        if entry.get("cid") == cid:
            return entry
    raise StateError(f"candidate {cid} not found in {selection_path}")


def cmd_init(args: argparse.Namespace) -> int:
    session_cfg = load_session_config(Path(args.session))
    clip = clip_from_entry(_selection_entry(Path(args.selection), args.cid), cid=args.cid,
                           recording_date=session_cfg.recording_date, bcut_srt=Path(args.mac_bcut_srt),
                           bcut_source_zero_ms=args.bcut_source_zero_ms, remote_root=args.remote_root,
                           source_dir=args.source_dir, selection_file=Path(args.selection))
    state = create_clip_state(Path(args.task_dir), clip, session_cfg)
    print(json.dumps({"state": str(state.path), "clip": clip["cid"], "start_cue": clip["start_cue"],
                      "end_cue": clip["end_cue"], "speaker_mode": session_cfg.speaker_mode}, ensure_ascii=False))
    return 0


def _recording_input(spec: str) -> RecordingInput:
    name, sep, bcut = spec.partition("=")
    if not sep or not name or not bcut:
        raise ValueError(f"--recording wants NAME=ORIGINAL_BCUT_SRT[@ZERO_MS], got {spec!r}")
    path, at, zero = bcut.rpartition("@")
    if at and re.fullmatch(r"-?\d+", zero):
        return RecordingInput(name=name, bcut_srt=Path(path), zero_ms=int(zero))
    return RecordingInput(name=name, bcut_srt=Path(bcut))


def cmd_compile(args: argparse.Namespace) -> int:
    task = Path(args.task_dir)
    session_cfg = load_session_config(Path(args.session))
    session_root = layout().session_root(session_cfg.recording_date)
    remote_root = args.remote_root or session_root
    scripts_dir = f"{remote_root}/{args.cid}/steps"
    adapters = _adapters(scripts_dir, args.on_media_host)
    result = compile_session(task=task, cid=args.cid, theme=args.theme, session=session_cfg,
                             recordings=[_recording_input(spec) for spec in args.recording],
                             source_dir=args.source_dir or f"{session_root}/sources", remote_root=remote_root,
                             adapters=adapters)
    if not result.compilation:
        print(json.dumps({"cid": args.cid, "compilation": False, "reason": result.reason}, ensure_ascii=False))
        return 3          # CPA's answer: this session holds no such compilation (not an error)
    manifest, entry, bcut = result.manifest, result.selection_file, result.bcut_srt
    if manifest is None or entry is None or bcut is None:
        raise StateError("compile_session returned a compilation without its manifest, entry or BCUT")
    if not (task / "state" / f"{args.cid}.json").exists():
        cmd_init(argparse.Namespace(task_dir=args.task_dir, cid=args.cid, selection=str(entry), session=args.session,
                                    mac_bcut_srt=str(bcut), bcut_source_zero_ms=0, remote_root=remote_root,
                                    source_dir=f"{remote_root}/sources"))     # the joined file's directory
    print(json.dumps({"cid": args.cid, "compilation": True, "hook": result.hook, "duration_ms": manifest["duration_ms"],
                      "pieces": manifest["pieces"]}, ensure_ascii=False))
    return 0


def _adapters(scripts_dir: str, on_media_host: bool) -> Adapters:
    """The lane's real adapters.  On the media host the controller's scripts run there and the chat is read from the
    recorder's archive mount there (chat_archive); otherwise both go over ssh."""
    remote: Remote = LocalRemote(scripts_dir=scripts_dir) if on_media_host else SshRemote(scripts_dir=scripts_dir)
    return Adapters(remote=remote, cpa=SshCpaJudge(remote), danmaku=ArchiveChat() if on_media_host else DanmakuSource(),
                    lookup=WebLookup())


def _ctx(task: Path, cid: str, *, on_media_host: bool = False, source_dir: str | None = None) -> Ctx:
    state = ClipState.load(task / "state" / f"{cid}.json")
    scripts_dir = f"{state.data['clip']['remote_dir']}/steps"
    adapters = _adapters(scripts_dir, on_media_host)
    return Ctx(state=state, session=state.data["session"], clip_dir=task / "clips" / cid,
               remote_dir=state.data["clip"]["remote_dir"], adapters=adapters, source_dir=source_dir)


def _finish_run(cid: str, ctx: Ctx, report: RunReport, **extra: object) -> int:
    """Print the run's one JSON line (`extra` keys last) and give its exit code.  The review's objections to the owner's
    edits (kept by default, dissent_register) are first printed, each in full."""
    if dissent_register.items(ctx.state):
        say("\n".join(dissent_register.process_lines(ctx.state)))
    if observed := persona_observe.after_run(ctx):      # the sealed clip is read once for what it shows of the host
        say(observed)
    say(json.dumps({"cid": cid, "passed": report.passed, "reused": report.reused,
                    "stopped_at": report.stopped_at, "reason": report.reason,
                    "halted_before_upload": report.halted_before_upload,
                    "cpa_calls": ctx.state.cpa_call_count(), **extra}, ensure_ascii=False))
    return 1 if report.stopped_at is not None else 0


def _speaker_before(until: int | None) -> bool:
    """True when `until` stops the run before the speaker step (the point after which --trim cuts)."""
    order = [step.number for step in STEPS]
    return until is not None and until in order and order.index(until) < order.index(SPEAKER_STEP)


def _steps(ctx: Ctx, progress: Progress | None, *, until: int | None = None, upload: bool = False,
           quote: str | None = None) -> RunReport:
    """run_clip over the lane's steps with their dependency table and concurrency gates."""
    return run_clip(ctx, STEPS, until=until, upload=upload, quote=quote, deps=DEPENDS, gates=GATES, progress=progress)


def _run_with_trim(args: argparse.Namespace, task: Path, ctx: Ctx, progress: Progress | None = None) -> int:
    """`run --trim`: to the speaker step on the clip; then the mid-clip cut.  Trimmed: the later steps run on the new clip.
    Anything else goes on with the clip; a cut that failed or was not ready is said so (the cut is optional, it never
    stops the clip) - except the owner's marks (import/cut.srt): his decision is cut or the run stops, never the clip
    with the marked sentences still in the video."""
    if _speaker_before(args.until):
        return _finish_run(args.cid, ctx, _steps(ctx, progress, until=args.until, upload=args.upload, quote=args.quote))
    report = _steps(ctx, progress, until=SPEAKER_STEP)
    if report.stopped_at is not None:
        return _finish_run(args.cid, ctx, report)
    result = trim_clip(task=task, cid=args.cid, adapters_for=_adapters_for(args.on_media_host), source_dir=args.source_dir)
    status = result["status"]
    if status != "TRIMMED" and owner_marks(ctx):
        reason = f"{status}: {result.get('reason', '')}"
        say(f"频道主标记的剪除未完成：{reason}")
        record_unfinished(task, args.cid, reason)
        return _finish_run(args.cid, ctx, RunReport(stopped_at=SPEAKER_STEP, reason=f"owner cut marks not applied: {reason}"),
                           trim={"status": status, "reason": result.get("reason", "")})
    if status == "TRIMMED":
        successor, removed = cast(str, result["successor"]), sum(b - a + 1 for a, b in result.get("passed") or [])
        say(f"片中剪除：原片 {args.cid} 已被 {successor} 取代，剪去 {removed} 句")
        with clip_run_lock(task / "state" / f"{successor}.json"):
            ctx = _ctx(task, successor, on_media_host=args.on_media_host)
            report = _steps(ctx, progress, until=args.until, upload=args.upload, quote=args.quote)
        return _finish_run(successor, ctx, report, trim={"status": status, "replaces": args.cid, "removed_cues": removed})
    if status in ("FAILED", "NOT_READY"):
        reason = result.get("reason", "")
        say(f"片中剪除未完成：{reason}")
        record_unfinished(task, args.cid, f"{status}: {reason}")
    report = _steps(ctx, progress, until=args.until, upload=args.upload, quote=args.quote)
    return _finish_run(args.cid, ctx, report, trim={"status": status, "reason": result.get("reason", "")})


def _run_clip_cid(args: argparse.Namespace, task: Path, cid: str, progress: Progress | None) -> int:
    """`run` for one clip, holding the clip's run lock: a second process running it stops with an error."""
    one = argparse.Namespace(**{**vars(args), "cid": cid})
    with clip_run_lock(task / "state" / f"{cid}.json"):
        head = ClipState.load(task / "state" / f"{cid}.json").data["clip"]
        if head.get("status") == "SUPERSEDED":
            raise StateError(f"{cid} is SUPERSEDED by {head.get('superseded_by')}; run the replacement instead")
        ctx = _ctx(task, cid, on_media_host=args.on_media_host, source_dir=args.source_dir)
        if args.trim or owner_marks(ctx):   # the owner's 删 marks (import/cut.srt) are cut at the same place and the same way
            return _run_with_trim(one, task, ctx, progress)
        return _finish_run(cid, ctx, _steps(ctx, progress, until=args.until, upload=args.upload, quote=args.quote))


def cmd_run(args: argparse.Namespace) -> int:
    check_upload_request(args.upload, args.quote)
    task = Path(args.task_dir)
    cids = clip_ids(args.cid, args.cids_file)
    if len(cids) == 1:            # one clip: the same output as before (no log file, no progress lines)
        return _run_clip_cid(args, task, cids[0], None)
    return run_many(cids, lambda cid, progress: _run_clip_cid(args, task, cid, progress),
                    log_dir=task / "run-logs", errors=RUN_ERRORS)


def cmd_discover(args: argparse.Namespace) -> int:
    from .discover import discover

    report = discover(task=Path(args.task_dir), adapters=_adapters_for(args.on_media_host)(f"{layout().work_root}/discover"),
                      dates=args.date)
    print(json.dumps({"days": report.days, "deferred": report.deferred, "failures": report.failures}, ensure_ascii=False))
    return report.exit_code


def cmd_auto(args: argparse.Namespace) -> int:
    """One unattended round (auto_run.py): discover, then every unfinished clip of the task is run on."""
    from .auto_run import auto_round, publication_paused
    from .discover import discover
    from .step10_rules import load as load_upload_rules

    task = Path(args.task_dir)
    publication = "NOT_REQUESTED"
    if args.upload:           # unattended publishing obeys the host's emergency switch (auto_run.publication_paused)
        paused = publication_paused(_adapters_for(args.on_media_host)(f"{layout().work_root}/discover").remote, layout().autoslice_base)
        publication = "PAUSED_BY_HOST_SWITCH" if paused else "OPEN"
    upload = publication == "OPEN"
    quote = load_upload_rules().first_publication.quote if upload else None      # the owner's standing rule, not typed per round

    def discover_days() -> dict[str, object]:
        report = discover(task=task, adapters=_adapters_for(args.on_media_host)(f"{layout().work_root}/discover"))
        return {"days": report.days, "deferred": report.deferred, "failures": report.failures}

    def run_clips(cids: list[str]) -> int:
        one = argparse.Namespace(task_dir=str(task), on_media_host=args.on_media_host, upload=upload, quote=quote,
                                 until=None, trim=False, source_dir=None)
        return run_many(cids, lambda cid, progress: _run_clip_cid(one, task, cid, progress),
                        log_dir=task / "run-logs", errors=RUN_ERRORS)

    result = auto_round(task, discover_days=discover_days, run_clips=run_clips, upload=upload,
                        remote=_adapters_for(args.on_media_host)(f"{layout().work_root}/discover").remote)
    print(json.dumps({**result, "publication": publication}, ensure_ascii=False))
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    if args.cid is None:         # no clip named: the discovery ledger
        from .discover import ledger_lines

        print("\n".join(ledger_lines(Path(args.task_dir))))
        return 0
    state = ClipState.load(Path(args.task_dir) / "state" / f"{args.cid}.json")
    for step in STEPS:
        rec = state.step(step.key)
        print(f"{step.number:>2} {step.key:<12} {rec.get('status', 'PENDING'):<8} {rec.get('reason', '') or ''} "
              f"cpa={len(rec.get('cpa') or [])} {str(rec.get('note', ''))[:90]}")
    print(f"cpa_calls_total={state.cpa_call_count()}")
    return 0


def cmd_listen_queue(args: argparse.Namespace) -> int:
    task = Path(args.task_dir)
    out = Path(args.out) if args.out else task / "listen-queue"
    plans: list[QueuePlan] = []
    wanted = set(args.cid or [])
    named: dict[str, set[str]] = {}
    for spec in args.doubt or []:
        cid, sep, doubt_id = spec.partition(":")
        if not sep or not cid or not doubt_id:
            raise ListenError(f"--doubt wants CID:DOUBT_ID, got {spec!r}")
        named.setdefault(cid, set()).add(doubt_id)
    if wanted:
        wanted |= set(named)
    queued: set[str] = set()
    for path in sorted((task / "state").glob("*.json")):
        state = ClipState.load(path)
        cid = state.data["clip"]["cid"]
        if wanted and cid not in wanted:
            continue
        if state.data["clip"].get("status") == SUPERSEDED:
            continue      # a replaced candidate is not listened to (its successor is)
        plan = clip_queue(cid=cid, state_data=cast("QueueState", state.data), clip_dir=task / "clips" / cid,
                          include=named.get(cid, ()), dev_low_confidence=args.dev_low_confidence)
        if plan and plan["items"]:
            plans.append(plan)
            queued.add(cid)
    for cid in sorted(set(named) - queued):
        raise ListenError(f"{cid}: a named doubt cannot be queued (stage 3 or cut product missing)")
    summary = write_queue(out, plans, remote_for=lambda p: SshRemote(scripts_dir=f"{p['remote_dir']}/steps"))
    print(json.dumps(summary, ensure_ascii=False))
    return 0


def cmd_listen_import(args: argparse.Namespace) -> int:
    summary = import_labels(Path(args.task_dir), Path(args.export))
    print(json.dumps(summary, ensure_ascii=False))
    return 0


def cmd_import_subtitles(args: argparse.Namespace) -> int:
    from .stage_impl import record_import
    from .step_import import import_redo

    task = Path(args.task_dir)
    state = ClipState.load(task / "state" / f"{args.cid}.json")
    clip_dir, src = task / "clips" / args.cid, Path(args.source_dir)
    redo = import_redo(state, clip_dir, src, decided_by=args.decided_by)
    rec = record_import(state, clip_dir, src, source=args.source, authority=args.authority, decided_by=args.decided_by)
    print(json.dumps({"cid": args.cid, "imported": rec, "redo": redo}, ensure_ascii=False))
    return 0


def cmd_resolve_dissent(args: argparse.Namespace) -> int:
    from .dissent_resolve import resolve_dissent

    task = Path(args.task_dir)
    with clip_run_lock(task / "state" / f"{args.cid}.json"):      # a running process holds the clip's state: not written under it
        print(json.dumps(resolve_dissent(task, args.cid, item=args.item, how="apply" if args.apply else "keep",
                                         by=args.by, reason=args.reason), ensure_ascii=False))
    return 0


def cmd_reselect(args: argparse.Namespace) -> int:
    task = Path(args.task_dir)
    orig = ClipState.load(task / "state" / f"{args.cid}.json")
    clip = orig.data["clip"]
    cues = load_cues(Path(clip["mac_bcut_srt"]), source_zero_ms=bcut_source_zero_ms(clip))
    result = reselect_from_counter(task_dir=task, orig=orig, cues=cues, authority=args.authority, quote=args.quote)
    print(json.dumps({"ok": result.ok, "reason": result.reason, "new_cid": result.new_cid,
                      "detail": result.detail, "chosen": result.chosen}, ensure_ascii=False))
    return 0 if result.ok else 1


def cmd_supersede(args: argparse.Namespace) -> int:
    result = supersede_candidate(task_dir=Path(args.task_dir), cid=args.cid, by=args.by, authority=args.authority,
                                 quote=args.quote, reason=args.reason)
    print(json.dumps({"ok": result.ok, "reason": result.reason, "by": result.new_cid, "detail": result.detail},
                     ensure_ascii=False))
    return 0 if result.ok else 1


def _adapters_for(on_media_host: bool) -> AdaptersFor:
    """The adapters of a clip by its remote directory (what trim_clip asks for), local or over ssh."""
    def adapters_for(remote_dir: str) -> Adapters:
        return _adapters(f"{remote_dir}/steps", on_media_host)

    return adapters_for


def cmd_trim(args: argparse.Namespace) -> int:
    task = Path(args.task_dir)
    result = trim_clip(task=task, cid=args.cid, adapters_for=_adapters_for(args.on_media_host), source_dir=args.source_dir)
    print(json.dumps({"cid": args.cid, "status": result["status"], "successor": result.get("successor"),
                      "removed_cues": sum(b - a + 1 for a, b in result.get("passed") or []),
                      "reason": result.get("reason", "")}, ensure_ascii=False))
    return {"FAILED": 2, "NOT_READY": 3}.get(result["status"], 0)


def cmd_score(args: argparse.Namespace) -> int:
    task = Path(args.task_dir)
    if args.scorer:
        os.environ["MANUAL_LANE_SCORER"] = args.scorer
    truth = load_truth([Path(p) for p in args.truth])
    cids = args.cid or sorted({truth_clip_id(v) for v in truth.values()})
    clips_root = Path(args.clips_dir) if args.clips_dir else task / "clips"   # a snapshot root scores a frozen round
    # a snapshot keeps only the subtitles: the clip's whole-clip readings (truth joins, U159) come from the task's clips
    results = [score_clip(cid, clips_root, truth, Path(args.speaker_from) if args.speaker_from else None,
                          evidence_dirs=[task / "clips"]) for cid in cids]
    print(format_table(results))
    for line in error_lines(results):
        print(line)
    if args.out:
        Path(args.out).write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
    if args.tsv:   # append to a scores table (TASK-12 scores.tsv); the header is written once
        tsv = Path(args.tsv)
        head = "" if tsv.exists() and tsv.stat().st_size else "\t".join(TSV_COLUMNS) + "\n"
        with tsv.open("a", encoding="utf-8") as fh:
            fh.write(head + "\n".join(tsv_rows(results, args.label or "")) + "\n")
    return 0


def cmd_revise(args: argparse.Namespace) -> int:
    from .revise_run import revise_clip

    task = Path(args.task_dir)
    with clip_run_lock(task / "state" / f"{args.cid}.json"):      # the original is superseded: a running process must not hold it
        print(revise_clip(task=task, cid=args.cid, reason=args.reason, adapters_for=_adapters_for(args.on_media_host),
                          fresh_speakers=args.fresh_speakers))
    return 0


def cmd_redo(args: argparse.Namespace) -> int:
    from .redo import redo_clip

    task = Path(args.task_dir)
    with clip_run_lock(task / "state" / f"{args.cid}.json"):      # the same lock as `run`: a running process holds the clip's state
        print(json.dumps(redo_clip(task=task, cid=args.cid, start=args.start, adapters_for=_adapters_for(args.on_media_host)),
                         ensure_ascii=False))
    return 0


def cmd_record_title_receipt(args: argparse.Namespace) -> int:
    from .title_receipt import register

    doc = register(task=Path(args.task_dir), cid=args.cid, receipt=Path(args.receipt), adapters_for=_adapters_for(args.on_media_host))
    print(json.dumps(doc, ensure_ascii=False))
    return 0


def cmd_import_cover(args: argparse.Namespace) -> int:
    from .cover_import import import_cover, withdraw

    task = Path(args.task_dir)
    if args.withdraw:
        if args.source_dir or args.authority or not args.decided_by or not args.reason:
            raise StateError("import-cover --withdraw takes --decided-by (who withdraws) and --reason, and no --source-dir / --authority")
    elif not (args.source_dir and args.decided_by and args.authority) or args.reason:
        raise StateError("import-cover needs --source-dir, --decided-by and --authority (and no --reason: that is for --withdraw)")
    with clip_run_lock(task / "state" / f"{args.cid}.json"):      # a running process holds the clip's state: not written under it
        state = ClipState.load(task / "state" / f"{args.cid}.json")
        if state.data["clip"].get("status") == SUPERSEDED:
            raise StateError(f"{args.cid} is SUPERSEDED by {state.data['clip'].get('superseded_by')}; import the cover into the replacement")
        if args.withdraw:
            done = withdraw(state, decided_by=args.decided_by, reason=args.reason)
            print(json.dumps({"cid": args.cid, "withdrawn_cover": done,
                              "note": "step 6 and the steps after it are forgotten; the next `run` draws the cover itself"}, ensure_ascii=False))
            return 0
        rec = import_cover(state, task / "clips" / args.cid, Path(args.source_dir), decided_by=args.decided_by, authority=args.authority)
    print(json.dumps({"cid": args.cid, "imported_cover": rec}, ensure_ascii=False))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="manual_lane")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init")
    p.add_argument("--task-dir", required=True)
    p.add_argument("--cid", required=True)
    p.add_argument("--selection", required=True)
    p.add_argument("--session", required=True)
    p.add_argument("--bcut-srt", "--mac-bcut-srt", dest="mac_bcut_srt", required=True,
                   help="the whole recording's original BCUT subtitle file (--mac-bcut-srt is the old name)")
    p.add_argument("--bcut-source-zero-ms", type=int, default=DEFAULT_BCUT_SOURCE_ZERO_MS,
                   help="source-recording time corresponding to BCUT file zero (default: 0)")
    p.add_argument("--remote-root", default=None,
                   help="the session's work directory on the media host (default: <work_root>/<recording date>)")
    p.add_argument("--source-dir", default=None,
                   help="where the clip's recording is on the media host; recorded in the clip (default: found at step 2)")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("discover", help="recordings of a day -> candidates -> clips: inventory, whole-recording subtitles, "
                       "selection, then `init` of every chosen candidate (repeatable; ledger in <task>/discover/ledger.json)")
    p.add_argument("--task-dir", required=True)
    p.add_argument("--date", action="append", default=None, metavar="YYYY-MM-DD",
                   help="a recording date to process (repeatable).  Without it: the newest dates of the archive that are "
                        "recorded (newest recording last written longer ago than lane_rules discover.settle_minutes) and "
                        "have no completed record")
    p.add_argument("--on-media-host", action="store_true", help="run controller and media work on the media host")
    p.set_defaults(func=cmd_discover)

    p = sub.add_parser("auto", help="one unattended round: discover the new recording days, then run every unfinished clip of "
                       "the task on; a clip that stopped three rounds in a row at the same place is left for someone to look at")
    p.add_argument("--task-dir", required=True)
    p.add_argument("--on-media-host", action="store_true", help="run controller and media work on the media host")
    p.add_argument("--upload", action="store_true", help="also run step 10 under the owner's standing rule (lane_rules upload.authorization) "
                   "- unless the host's PUBLISH_PAUSED marker is set: then the round makes sealed packages and sends nothing")
    p.set_defaults(func=cmd_auto)

    p = sub.add_parser("compile", help="same-session theme compilation: select, cut, join, then init")
    p.add_argument("--task-dir", required=True)
    p.add_argument("--cid", required=True)
    p.add_argument("--session", required=True)
    p.add_argument("--theme", required=True, help="the compilation's theme, given to CPA as is")
    p.add_argument("--recording", action="append", required=True, metavar="NAME=BCUT_SRT[@ZERO_MS]",
                   help="one recording of the session and its original BCUT file, in recording order (repeatable); "
                        "ZERO_MS is the recording time at the BCUT file's zero (as init's --bcut-source-zero-ms; "
                        "default 0)")
    p.add_argument("--source-dir", default=None,
                   help="where the recordings are on the media host (default: <session work directory>/sources)")
    p.add_argument("--remote-root", default=None,
                   help="the work directory on the media host; the joined file goes to its sources/ "
                        "(default: <work_root>/<recording date>)")
    p.add_argument("--on-media-host", action="store_true", help="run controller and media work on the media host")
    p.set_defaults(func=cmd_compile)

    p = sub.add_parser("run")
    p.add_argument("--task-dir", required=True)
    p.add_argument("--cid", action="append", default=None,
                   help="the clip to run; repeat it to run several clips side by side (each with its own log file)")
    p.add_argument("--cids-file", default=None, help="more clips to run, one per line")
    p.add_argument("--until", type=int, default=None)
    p.add_argument("--on-media-host", action="store_true", help="run controller and media work on the media host")
    p.add_argument("--upload", action="store_true", help="also run step 10; requires --quote (维护者's words)")
    p.add_argument("--quote", default=None)
    p.add_argument("--trim", action="store_true",
                   help="after the speaker step, cut unrelated thanks out of the clip (as `trim`); when that makes a new clip "
                        "the later steps run on it.  Off by default")
    p.add_argument("--source-dir", default=None,
                   help="where the clip's recording is on the media host when its state records none (default: <its work "
                        "directory>/sources); the directory it is found in is recorded in the clip")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("status", help="the step table of one clip; without --cid, the discovery ledger of the task")
    p.add_argument("--task-dir", required=True)
    p.add_argument("--cid", default=None)
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("listen-queue")
    p.add_argument("--task-dir", required=True)
    p.add_argument("--dev-low-confidence", action="store_true",
                   help="development only: also queue the doubts CPA marked 低把握 (the final version does not)")
    p.add_argument("--out", default=None)
    p.add_argument("--cid", action="append", default=None, help="limit the page to these candidates (repeatable)")
    p.add_argument("--doubt", action="append", default=None, metavar="CID:DOUBT_ID",
                   help="also queue this doubt even if its state is not UNRESOLVED (repeatable)")
    p.set_defaults(func=cmd_listen_queue)

    p = sub.add_parser("listen-import")
    p.add_argument("--task-dir", required=True)
    p.add_argument("--export", required=True)
    p.set_defaults(func=cmd_listen_import)

    p = sub.add_parser("import-subtitles", help="a hand push's decided subtitles and speakers become step 3's input")
    p.add_argument("--task-dir", required=True)
    p.add_argument("--cid", required=True)
    p.add_argument("--source-dir", required=True, help="the push for this clip: subs/final.srt, speaker/speaker.srt, "
                   "subs/review.tsv, subs/cut.srt (the owner's 删 marks), boundary/decision.json")
    p.add_argument("--source", required=True, help="the push's name, e.g. manual-push-r2")
    p.add_argument("--authority", required=True, help="the authority for using it (e.g. U130)")
    p.add_argument("--decided-by", choices=["owner"], default=None,
                   help="owner: the channel owner decided these subtitles himself; the review only records objections")
    p.set_defaults(func=cmd_import_subtitles)

    p = sub.add_parser("resolve-dissent", help="dispose of one review objection to what the owner edited (step 9 / REVIEW.md)")
    p.add_argument("--task-dir", required=True)
    p.add_argument("--cid", required=True)
    p.add_argument("--item", type=int, required=True, help="the objection's number in REVIEW.md / state dissent")
    how = p.add_mutually_exclusive_group(required=True)
    how.add_argument("--keep", action="store_true", help="the owner's writing stands")
    how.add_argument("--apply", action="store_true", help="take the review's proposal (text or speaker): an owner-authorised "
                     "edit of the import; steps 3+ run again")
    p.add_argument("--by", required=True, help="who decided")
    p.add_argument("--reason", required=True, help="on what grounds")
    p.set_defaults(func=cmd_resolve_dissent)

    p = sub.add_parser("reselect")
    p.add_argument("--task-dir", required=True)
    p.add_argument("--cid", required=True, help="the original candidate")
    p.add_argument("--from-counter", action="store_true", required=True,
                   help="the only mode: window = CPA's last cut recorded in the original's boundary step")
    p.add_argument("--authority", required=True)
    p.add_argument("--quote", required=True)
    p.set_defaults(func=cmd_reselect)

    p = sub.add_parser("supersede")
    p.add_argument("--task-dir", required=True)
    p.add_argument("--cid", required=True)
    p.add_argument("--by", required=True)
    p.add_argument("--authority", required=True)
    p.add_argument("--quote", required=True)
    p.add_argument("--reason", required=True)
    p.set_defaults(func=cmd_supersede)

    p = sub.add_parser("trim", help="cut unrelated thanks out of a finished clip into a new clip (original superseded)")
    p.add_argument("--task-dir", required=True)
    p.add_argument("--cid", required=True, help="a plain clip whose subtitles (step 3) and speakers (step 4) are done")
    p.add_argument("--on-media-host", action="store_true", help="run controller and media work on the media host")
    p.add_argument("--source-dir", help="where the clip's recording is on the media host when its state records none "
                   "(default: <its work directory>/sources); the directory it is found in is recorded in the clip")
    p.set_defaults(func=cmd_trim)

    p = sub.add_parser("score")
    p.add_argument("--task-dir", required=True)
    p.add_argument("--truth", action="append", required=True, help="truth labels JSON (repeatable)")
    p.add_argument("--cid", action="append", default=None, help="clips to score (default: every clip in truth)")
    p.add_argument("--speaker-from", default=None,
                   help="JSON {truth_id: speaker} overriding the pipeline speaker (e.g. a prefill baseline)")
    p.add_argument("--out", default=None, help="write per-cue rows and summaries as JSON")
    p.add_argument("--clips-dir", default=None, help="score a frozen snapshot root <root>/<cid>/ instead of task clips")
    p.add_argument("--tsv", default=None, help="append one row per clip and a TOTAL row (cer_whole first) to this TSV")
    p.add_argument("--label", default=None, help="the label column of the TSV rows (e.g. epoch and set)")
    p.add_argument("--scorer", choices=SCORER_VERSIONS, default=None,
                   help="scoring definition (v4: v3 with the lone-character rule on both sides; v3: v2 without fillers; v1 = frozen TASK-5)")
    p.set_defaults(func=cmd_score)

    p = sub.add_parser("revise", help="redo a published clip in a successor clip of the same manuscript (original superseded)")
    p.add_argument("--task-dir", required=True)
    p.add_argument("--cid", required=True, help="a clip the registry holds as published (not superseded)")
    p.add_argument("--reason", required=True, help="why it is revised (kept in the original's supersede record)")
    p.add_argument("--fresh-speakers", action="store_true", help="do not take over the speakers: step 4 decides them again")
    p.add_argument("--on-media-host", action="store_true", help="run controller and media work on the media host")
    p.set_defaults(func=cmd_revise)

    p = sub.add_parser("redo", help="forget a step and the steps that depend on it so `run` makes them again; a joined source is rebuilt")
    p.add_argument("--task-dir", required=True)
    p.add_argument("--cid", required=True)
    p.add_argument("--from", dest="start", required=True, help="the step to start from: its name (title_cover) or number (6)")
    p.add_argument("--on-media-host", action="store_true", help="run controller and media work on the media host")
    p.set_defaults(func=cmd_redo)

    p = sub.add_parser("record-title-receipt", help="register the title-only edit receipt of a published manuscript for its successors")
    p.add_argument("--task-dir", required=True)
    p.add_argument("--cid", required=True, help="the manuscript's clip or any successor of it (_trim, _r<N>); its latest successor's "
                   "title/title.txt must be the title the receipt gave")
    p.add_argument("--receipt", required=True, help="the title-repair receipt (status VERIFIED_TITLE_ONLY), an absolute path")
    p.add_argument("--on-media-host", action="store_true", help="run controller and media work on the media host")
    p.set_defaults(func=cmd_record_title_receipt)

    p = sub.add_parser("import-cover", help="a push's finished cover becomes step 6's cover as it is; step 6 and later run again")
    p.add_argument("--task-dir", required=True)
    p.add_argument("--cid", required=True)
    p.add_argument("--source-dir", help="the push: cover/cover.png (1920x1080 PNG) and cover/cover.json "
                   '({"lines": [...], "decided_by": "owner"|"push", "authority": "...", "note": ""})')
    p.add_argument("--decided-by", choices=["owner", "push"], help="who decided this cover (with --withdraw: who withdraws it)")
    p.add_argument("--authority", help="the basis for using it")
    p.add_argument("--withdraw", action="store_true", help="take the clip's decided cover back (needs --decided-by and --reason): "
                   "step 6 and later are forgotten and the next run draws the cover itself")
    p.add_argument("--reason", help="why the decided cover is withdrawn (with --withdraw)")
    p.set_defaults(func=cmd_import_cover)
    return parser


RUN_ERRORS = (StateError, UploadNotAuthorized, ListenError, ValueError, OSError)


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return cast(Callable[[argparse.Namespace], int], args.func)(args)
    except RUN_ERRORS as exc:
        print(f"error: {exc}")
        return 2
