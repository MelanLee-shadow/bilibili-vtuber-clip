"""`discover`: the recordings of a day become candidate clips the steps can run (`init` of every chosen candidate).

For each day: (a) inventory of the recorder's archive (discover_media.inventory); (b) every recording made ready - audio,
whole-recording BCUT subtitles, danmaku XML (discover_media.prepare); (c) the selection function called on each recording
and the day's candidates seated by the production selection's order, admission and quota (discover_select); (d) a clip
state for every seated candidate, exactly what `init` makes (clip_init), so `run` starts at step 1.

Everything is recorded in the ledger `<task>/discover/ledger.json` (one entry per day) and can be repeated: a recording
or its subtitles whose fingerprint did not change are not prepared or selected again, a candidate already made into a clip
is never made again, and a clip that exists (running, published) is never touched.  A recording that fails (subtitles,
model call) is recorded with its reason and left for the next run; the others go on.  `status` without --cid prints the ledger.

Without --date the days looked at are the newest `auto_recent_dates` date directories of the archive (none earlier than
`auto_not_before`: the sessions before it were made by hand) whose recordings are not the ones a completed record was
made from, where nothing was written for `settle_minutes` and no danmaku file still waits for its recording
(`lone_danmaku_hours`; lane_rules `discover`): a day that may still be recording or uploading is not touched, and a
recording that arrives after the day was finished reopens it.  --date names days explicitly (no such wait).

A channel whose draws asset names stories (core_draws.has_stories) has three more things done for the day once every
recording is selected: a talk candidate labelled not followable is given the lines before it, from the previous
recording too (discover_lead_in); its talk candidates on the same matter are found (discover_topics); and a seated
candidate with a lead-in, or a seated group, is made into one clip, its pieces joined in broadcast order by the
same-session compilation (`_build_joined`).

Session: `recording_date` is the day; `participants` and `speaker_mode` are the channel profile's single-host defaults
(the host, `uniform_host`) unless the day's directory holds `participants.json` (keys of a session config other than
`recording_date`), which then decides.  Whether a session is a collaboration is not judged here: the ledger says so.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
from collections import Counter
from collections.abc import Callable, Iterator, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, cast

from src.autoslice.danmaku_evidence import SessionDensity

from . import compilation, core_draws, discover_lead_in, discover_topics, discover_voices, short_queue
from .adapters import Adapters
from .clip_init import clip_from_entry, create_clip_state, selection_entry
from .config import SessionBlock, SessionConfig, load_session_config
from .discover_media import (
    NO_SPEECH,
    DayInventory,
    DiscoverError,
    Prepared,
    Segment,
    archive_room,
    inventory,
    copy_direct,
    list_dates,
    load_prepared,
    prepare,
    remote_dir_of,
    write_json,
)
from .discover_select import (
    BURST_GATED,
    BURST_IN_CANDIDATE,
    BURST_MADE,
    BURST_PAST_GATE,
    BURST_REASK_FAILED,
    BURST_SEATED,
    BURST_UNANSWERED,
    BURST_NOT_SELECTED,
    BURSTS_UNACCOUNTED,
    OWNER_MARK_NOT_COVERED,
    RecallCallError,
    Selector,
    _cues,
    fits_slug,
    lane_cid,
    load_recall,
    recall_segment,
    burst_funnel,
    burst_swaps,
    seat_day,
    session_danmaku_density,
    session_shadow_density,
    shadow_bursts,
)
from .lane_rules import rules
from .layout import REPO_ROOT, layout
from .multi_run import say
from .runner import Ctx
from .state import SCHEMA as STATE_SCHEMA
from .state import ClipData, ClipState, StateError, utc_now
from .step_io import _ask_cpa, _cpa_ok

LEDGER_SCHEMA = "manual-lane.discover-ledger.v1"
RECALL_PURPOSE = "discover-recall"
TOPICS_CALL_FAILED = "TOPICS_CALL_FAILED"
LEAD_IN_CALL_FAILED = "LEAD_IN_CALL_FAILED"
JOINED_SUFFIX = "_m"                             # the clip id of candidates joined on one matter: the leading one's, plus this
COMPLETE, PARTIAL, NO_CONTENT, NO_ARCHIVE_DIR = "COMPLETE", "PARTIAL", "NO_CONTENT", "NO_ARCHIVE_DIR"
FINISHED = (COMPLETE, NO_CONTENT)                # a day with one of these is done while its recordings stay the same
SINGLE_HOST_NOTE = "本场按单人场处理（是否联动场未判断；会话目录放 participants.json 时以它为准）"
_SESSION_KEYS = {"participants", "speaker_mode", "own_uploader_mids", "enablement_evidence", "released", "speaker_rule"}


def root_of(task: Path) -> Path:
    return task / "discover"


def ledger_path(task: Path) -> Path:
    return root_of(task) / "ledger.json"


def load_ledger(task: Path) -> dict[str, Any]:
    path = ledger_path(task)
    if not path.exists():
        return {"schema": LEDGER_SCHEMA, "days": {}}
    ledger: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    if ledger.get("schema") != LEDGER_SCHEMA:
        raise StateError(f"unexpected schema in {path}: {ledger.get('schema')!r}")
    return ledger


@contextmanager
def _one_discover_at_a_time(task: Path) -> Iterator[None]:
    root_of(task).mkdir(parents=True, exist_ok=True)
    with (root_of(task) / ".lock").open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise StateError(f"another discover is running on {task}") from exc
        yield


def mark_recordings_reclaimed(task: Path, date: str) -> None:
    """Before the lane deletes a finished day's recordings (auto_run.reclaim_recordings): the day stays finished, so
    their going is not taken for a changed day (11-15 and 11-16 were rediscovered as NO_CONTENT)."""
    with _one_discover_at_a_time(task):
        ledger = load_ledger(task)
        day = ledger["days"].get(date, {})
        if day.get("status") not in FINISHED:
            raise StateError(f"{date} is not a finished day")
        if not day.get("recordings_reclaimed"):
            day["recordings_reclaimed"] = utc_now()
            write_json(ledger_path(task), ledger)


def resolve_session(task: Path, date: str) -> tuple[SessionConfig, dict[str, str]]:
    """The day's session config (written as `<day dir>/session.json`) and where its people came from."""
    from src.autoslice.channel_profile import load_channel_profile

    day_dir = root_of(task) / date
    profile = load_channel_profile(REPO_ROOT)
    # the host's group leads with her display name: it is how the lane names her to every judge and how it checks
    # that she is there (HOST_MISSING); her own words for herself follow as aliases
    host = [profile.display_name, *(a for a in profile.self_reference_aliases if a != profile.display_name)]
    doc: dict[str, Any] = {"recording_date": date, "participants": [host],
                           "speaker_mode": "uniform_host", "own_uploader_mids": []}
    given = day_dir / "participants.json"
    source, note = "channel_default", SINGLE_HOST_NOTE
    if given.exists():
        extra = json.loads(given.read_text(encoding="utf-8"))
        unknown = sorted(set(extra) - _SESSION_KEYS)
        if unknown:
            raise DiscoverError("PARTICIPANTS_FILE_INVALID", f"{given}: keys {unknown} are not session fields (recording_date is the day's)")
        doc.update(extra)
        source, note = "participants_file", f"参与者取自 {given.name}"
    write_json(day_dir / "session.json", doc)
    return load_session_config(day_dir / "session.json"), {"file": str(day_dir / "session.json"), "source": source, "note": note}


def _ask_in(directory: Path, name: str, remote_dir: str, adapters: Adapters, *, call: str, purpose: str) -> core_draws.Ask:
    """A model call of the selection whose record is kept under `directory`: the lane's CPA call (cached by prompt, effort
    by purpose), under the process's shared `cpa` bound; a failed call raises.  `accepts` is the caller's contract on the
    answer (step_io._ask_cpa): an answer outside it is not cached and is asked again."""
    from .parallel import slot

    state = ClipState(directory / f"{call}.state.json", cast(ClipData, {"schema": STATE_SCHEMA, "clip": {"cid": name}, "steps": {}}))
    ctx = Ctx(state=state, session=cast(SessionBlock, {}), clip_dir=directory, remote_dir=remote_dir, adapters=adapters)

    def ask(prompt: str, accepts: Callable[[Any], str] | None = None) -> str:
        with slot("cpa"):
            record = _ask_cpa(ctx, f"{call}-{hashlib.sha256(prompt.encode('utf-8')).hexdigest()[:16]}", prompt, purpose=purpose,
                              accepts=accepts)
        if not _cpa_ok(record):
            raise RecallCallError(str(record.get("stderr_tail") or record.get("reason") or "CPA call failed"))
        return record["response_text"]

    return ask


def _ask_for(task: Path, segment: Segment, adapters: Adapters) -> core_draws.Ask:
    """The model call of one recording's selection."""
    return _ask_in(root_of(task) / segment.date / segment.name, segment.name, remote_dir_of(segment), adapters,
                   call="recall", purpose=RECALL_PURPOSE)


def _failure(exc: Exception) -> dict[str, str]:
    return {"code": exc.code if isinstance(exc, DiscoverError) else type(exc).__name__, "detail": str(exc)[:400]}


def _prepare_segment(task: Path, adapters: Adapters, segment: Segment) -> tuple[dict[str, Any], Prepared | None]:
    """One recording prepared (subtitles, danmaku).  Returns its ledger record and what was prepared, None when it failed or
    has no speech; a failure is the record's `reason`, never an exception."""
    directory = root_of(task) / segment.date / segment.name
    rec: dict[str, Any] = {"name": segment.name, "path": segment.path, "size": segment.size, "mtime": segment.mtime,
                           "xml_exists": segment.xml_fp is not None, "stub": False, "source_fp": segment.source_fp}
    try:
        prepared = prepare(adapters.remote, segment, directory, load_prepared(directory))
    except Exception as exc:  # noqa: BLE001 - the per-recording boundary: one recording's failure must not stop the others
        if isinstance(exc, DiscoverError) and exc.code == NO_SPEECH:
            rec["prepare"], rec["status"] = {"status": NO_SPEECH}, NO_SPEECH
        else:
            rec["prepare"], rec["status"] = {"status": "FAILED", **_failure(exc)}, "PREPARE_FAILED"
        return rec, None
    rec["prepare"] = {"status": "OK", "srt_sha256": prepared["srt_sha256"], "xml_sha256": prepared["xml"]["sha256"]}
    return rec, prepared


def _work_segment(task: Path, adapters: Adapters, segment: Segment, selector: Selector | None, rec: dict[str, Any],
                  prepared: Prepared, density: SessionDensity | None) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """One prepared recording's candidates recalled, its danmaku bursts against the day's `density`.  Returns its ledger
    record and its candidate entries (with clip ids); a failure is the record's `reason`, never an exception."""
    directory = root_of(task) / segment.date / segment.name
    phase = "select"
    try:
        reused = load_recall(directory, prepared) is not None
        recall = recall_segment(prepared, directory, ask=_ask_for(task, segment, adapters), density=density,
                                selector=selector, hear=discover_voices.hearer(adapters.remote))
        entries = [dict(e, cid=lane_cid(e["candidate_id"], segment.name)) for e in recall["input_pool"]]
        rec["select"] = {"status": "OK", "reused": reused, "candidates": len(entries),
                         "inputs": recall["inputs"], "danmaku": "OK" if prepared["xml"]["exists"] else "MISSING"}
        if tied := recall.get("owner_marks", {}).get("tied"):
            rec["owner_marks"] = tied
        if bursts := recall.get("bursts"):           # every danmaku burst: in a candidate, or why not
            rec["bursts"] = bursts
        rec["status"] = "SELECTED"
        return rec, entries
    except Exception as exc:  # noqa: BLE001 - the per-recording boundary: one recording's failure must not stop the others
        rec[phase] = {"status": "FAILED", **_failure(exc)}
        rec["status"] = "SELECT_FAILED"
        return rec, []


def _built_record(entry: dict[str, Any], state: Path) -> dict[str, Any]:
    return {"cid": entry["cid"], "candidate_id": entry["candidate_id"], "segment": entry["source"]["name"],
            "start_ms": entry["start_ms"], "end_ms": entry["end_ms"], "state": str(state)}


def _build(task: Path, session: SessionConfig, entry: dict[str, Any]) -> dict[str, Any]:
    """The clip of a seated candidate: what `init` makes from its entry and the recording's subtitles."""
    cid, source = entry["cid"], entry["source"]
    selection = root_of(task) / "entries" / f"{cid}.json"
    write_json(selection, {"input_pool": [entry]})
    clip = clip_from_entry(cast(Any, entry), cid=cid, recording_date=session.recording_date,
                           bcut_srt=Path(source["directory"]) / "bcut.srt", bcut_source_zero_ms=0, remote_root=None,
                           source_dir=str(PurePosixPath(source["path"]).parent), selection_file=selection)
    return _built_record(entry, create_clip_state(task, clip, session).path)


def _build_joined(task: Path, session: SessionConfig, adapters: Adapters, entry: dict[str, Any]) -> dict[str, Any]:
    """The clip of a seated candidate that stands for several on one matter (`topic_members`, in broadcast order): the
    members are cut and joined by the same-session compilation with the pieces already decided, and the result is
    initialised like `compile` does.  The compilation's own rules hold (docs/pipeline/20-selection.md 同场主题合集: no limit
    on the number or the total length of the pieces; step 1 does not run on it).  The built record names every member
    (`covers`) so none of them is seated again."""
    members = entry["topic_members"]
    cid = fits_slug(f"{entry['cid']}{JOINED_SUFFIX}")
    source_dirs = {str(PurePosixPath(m["source"]["path"]).parent) for m in members}
    if len(source_dirs) != 1:
        raise DiscoverError("TOPIC_RECORDINGS_APART", f"{cid}: the members' recordings are in {sorted(source_dirs)}; one directory is joined")
    names = list(dict.fromkeys(str(m["source"]["name"]) for m in members))
    recordings = [compilation.RecordingInput(name=f"{name}.flv", bcut_srt=Path(next(
        m["source"]["directory"] for m in members if m["source"]["name"] == name)) / "bcut.srt") for name in names]
    decided = {"compilation": True, "reason": str(entry.get("topic_why", "")), "hook": str(entry["topic_theme"]),
               "segments": [{"segment": names.index(str(m["source"]["name"])) + 1, "start_cue": int(m["cue_ids"][0]),
                             "end_cue": int(m["cue_ids"][-1]), "why": str(m.get("hook", ""))} for m in members]}
    remote_root = layout().session_root(session.recording_date)
    result = compilation.compile_session(task=task, cid=cid, theme=str(entry["topic_theme"]), session=session,
                                         recordings=recordings, source_dir=source_dirs.pop(), remote_root=remote_root,
                                         adapters=adapters, decided=decided)
    manifest, selection, bcut = result.manifest, result.selection_file, result.bcut_srt
    if manifest is None or selection is None or bcut is None:
        raise DiscoverError("TOPIC_JOIN_INCOMPLETE", f"{cid}: the compilation returned no manifest, entry or BCUT")
    # the joined clip's own entry carries what the members were chosen for: the key moments on the joined clock
    doc = json.loads(selection.read_text(encoding="utf-8"))
    piece_of = {(p["recording"], p["start_cue"]): p for p in manifest["pieces"]}
    peaks = []
    for member in members:
        piece = piece_of[(f"{member['source']['name']}.flv", int(member["cue_ids"][0]))]
        peaks += [dict(peak, at_ms=max(0, piece["compiled_start_ms"] + int(peak["at_ms"]) - piece["source_start_ms"]))
                  for peak in member.get("peaks") or []]
    doc["input_pool"][0].update(peaks=peaks, topic_theme=entry["topic_theme"], joined_from=[m["cid"] for m in members],
                                **({"owner_mark": entry["owner_mark"]} if entry.get("owner_mark") else {}))
    write_json(selection, doc)
    clip = clip_from_entry(selection_entry(selection, cid), cid=cid, recording_date=session.recording_date, bcut_srt=bcut,
                           bcut_source_zero_ms=0, remote_root=remote_root, source_dir=f"{remote_root}/sources",
                           selection_file=selection)
    state = task / "state" / f"{cid}.json"
    if not state.exists():
        state = create_clip_state(task, clip, session).path
    return {**_built_record(entry, state), "cid": cid, "duration_ms": manifest["duration_ms"],
            "covers": [{"cid": m["cid"], "segment": m["source"]["name"], "start_ms": m["start_ms"], "end_ms": m["end_ms"]}
                       for m in members]}


def _topics(date: str, pool: list[dict[str, Any]], day_dir: Path, ask: Callable[[str], str]) -> dict[str, Any]:
    """The day's ledger record of the same-matter grouping (the candidates of `pool` are marked in place).  An answer
    that cannot be read leaves the day without groups and says so; a call the model did not answer is `TOPICS_CALL_FAILED`."""
    try:
        return {"status": "OK", "groups": discover_topics.assign(pool, day_dir, ask)["groups"]}
    except discover_topics.TopicsError as exc:
        say(f"[{date}] 同话题归并的回答不能用，当天不归并：{str(exc)[:120]}")
        return {"status": "UNUSABLE_ANSWER", "detail": str(exc)[:300], "groups": []}
    except RecallCallError as exc:
        return {"status": TOPICS_CALL_FAILED, "detail": str(exc)[:400], "groups": []}


def _lead_ins(date: str, pool: list[dict[str, Any]], day_dir: Path, ask: Callable[[str], str]) -> dict[str, Any]:
    """The day's ledger record of the lead-ins (the candidates of `pool` are given theirs in place).  An answer that cannot
    be read leaves the day without lead-ins and says so; a call the model did not answer is `LEAD_IN_CALL_FAILED`."""
    try:
        return {"status": "OK", "leads": discover_lead_in.assign(pool, day_dir, ask, _cues)["leads"]}
    except discover_lead_in.LeadInError as exc:
        say(f"[{date}] 往前接前因的回答不能用，当天不接：{str(exc)[:120]}")
        return {"status": "UNUSABLE_ANSWER", "detail": str(exc)[:300], "leads": []}
    except RecallCallError as exc:
        return {"status": LEAD_IN_CALL_FAILED, "detail": str(exc)[:400], "leads": []}


def _process_day(task: Path, adapters: Adapters, inv: DayInventory, day: dict[str, Any], selector: Selector | None) -> list[dict[str, str]]:
    """One day's four phases; updates `day` (its ledger entry) and returns the failures."""
    date = inv.date
    session, session_note = resolve_session(task, date)
    day["session"] = session_note
    content = [s for s in inv.segments if not s.stub]
    stubs = [{"name": s.name, "path": s.path, "size": s.size, "mtime": s.mtime, "xml_exists": s.xml_fp is not None,
              "stub": True, "status": "STUB_SKIPPED"} for s in inv.segments if s.stub]
    with ThreadPoolExecutor(max_workers=max(1, len(content)), thread_name_prefix="lane-discover") as pool:
        prepared = list(pool.map(lambda s: _prepare_segment(task, adapters, s), content))
        # the day's own danmaku density, over every recording of it that is prepared (20-selection.md 链路 2)
        directories = [root_of(task) / date / s.name for s, (_, p) in zip(content, prepared) if p]
        density = session_danmaku_density(directories)
        shadow_density = session_shadow_density(directories)
        done = list(pool.map(lambda job: _work_segment(task, adapters, job[0], selector, job[1][0], job[1][1], density)
                             if job[1][1] is not None else (job[1][0], []), zip(content, prepared)))
    failures = [{"date": date, "segment": rec["name"], **rec[phase]} for rec, _ in done
                for phase in ("prepare", "select") if rec.get(phase, {}).get("status") == "FAILED"]
    failures += [{"date": date, "segment": rec["name"], "code": OWNER_MARK_NOT_COVERED,
                  "detail": f"owner mark at {m['at_ms'] / 1000:.0f}s ({m['text']}): no candidate holds it, not even a recall around it"}
                 for rec, _ in done for m in rec.get("owner_marks", []) if m.get("candidate_id") is None]
    # a danmaku burst whose second question the model did not answer: the next round asks it again (the day stays open)
    failures += [{"date": date, "segment": rec["name"], "code": BURST_REASK_FAILED,
                  "detail": "; ".join(f"{b['id']} {b['start_ms'] // 1000}-{b['end_ms'] // 1000}s" for b in failed)}
                 for rec, _ in done if (failed := [b for b in rec.get("bursts", [])
                                                    if b["disposition"] == BURST_UNANSWERED and b["reask"] == "FAILED"])]
    # one neither in a candidate nor explained when asked again (or with no line around it to ask about) is the recording's
    # failure, named here and in the ledger; asking it a third time would not change it, so it does not keep the day open
    # (a PARTIAL day is never done, and its recordings are never reclaimed: auto_run.recordings_released)
    settled = [{"date": date, "segment": rec["name"], "code": BURSTS_UNACCOUNTED,
                "detail": "; ".join(f"{b['id']} {b['start_ms'] // 1000}-{b['end_ms'] // 1000}s ({b['count']})"
                                    + (" no lines around it" if b["reask"] == "NO_LINES" else "") for b in silent)
                + ": neither in a candidate nor explained, also when asked again"}
               for rec, _ in done if (silent := [b for b in rec.get("bursts", [])
                                                  if b["disposition"] == BURST_UNANSWERED and b["reask"] != "FAILED"])]
    for rec, entries in done:
        if rec.get("status") == "SELECTED":       # written down only, to be weighed on later broadcasts
            rec["shadow_bursts"] = shadow_bursts(root_of(task) / date / rec["name"], shadow_density, rec.get("bursts", []), entries)
        bursts = rec.get("bursts", [])
        silent_n = sum(1 for b in bursts if b["disposition"] == BURST_UNANSWERED)
        say(f"[{date} {rec['name']}] {rec['status']}" + (f" 候选 {len(entries)}" if entries else "")
            + (f"；弹幕突发区 {len(bursts)} 个，" + (f"{silent_n} 个没交代，记失败" if silent_n else "都有交代")
               if bursts else ""))
    day["segments"] = sorted([rec for rec, _ in done] + stubs, key=lambda r: r["name"])
    pool_entries = [e for _, entries in done for e in entries]

    built = {b["cid"]: b for b in day.get("built", [])}
    for entry in pool_entries:        # a clip that exists is built, whoever made it: it keeps its seat and is not made again
        state = task / "state" / f"{entry['cid']}.json"
        if state.exists() and entry["cid"] not in built:
            built[entry["cid"]] = _built_record(entry, state)
    if core_draws.has_stories():
        # a candidate that cannot be followed is given the lines before it; the evening's candidates on the same matter
        # take one seat; either becomes one clip.  A call the model did not answer: nothing is seated this round.
        for key, call, purpose, run, failed in (
                ("lead_ins", "lead-ins", discover_lead_in.PURPOSE, _lead_ins, LEAD_IN_CALL_FAILED),
                ("topics", "topics", discover_topics.PURPOSE, _topics, TOPICS_CALL_FAILED)):
            ask = _ask_in(root_of(task) / date, date, f"{layout().session_root(date)}/discover", adapters, call=call, purpose=purpose)
            day[key] = run(date, pool_entries, root_of(task) / date, ask)
            if day[key]["status"] == failed:
                failures.append({"date": date, "segment": "", "code": failed, "detail": day[key]["detail"]})
                day["selection"] = {"policy": {"held": failed}, "pool": len(pool_entries), "seated": [], "rejected": []}
                day["built"] = list(built.values())
                day["status"] = PARTIAL
                return failures + settled
    already = list(built.values())
    seating = seat_day(date, pool_entries, already)
    for entry in seating.seated:
        try:
            if entry.get("topic_members"):
                made = _build_joined(task, session, adapters, entry)
                built[made["cid"]] = made
            else:
                built[entry["cid"]] = _build(task, session, entry)
        except Exception as exc:  # noqa: BLE001 - one clip's failure must not stop the others
            failures.append({"date": date, "segment": entry["source"]["name"], **_failure(exc)})
    made_from = {c["cid"]: b["cid"] for b in built.values() for c in b.get("covers", [])}
    day["selection"] = {"policy": seating.policy, "pool": len(pool_entries),
                        "seated": [made_from.get(e["cid"], e["cid"]) for e in seating.seated
                                   if made_from.get(e["cid"], e["cid"]) in built],
                        "rejected": seating.rejected,
                        # every burst of the day, how far it got, and whom its points seated in whose place (20-selection.md)
                        "bursts": burst_funnel(day["segments"], pool_entries, seating, set(built) | set(made_from)),
                        "burst_swaps": burst_swaps(date, pool_entries, already, seating)}
    day["built"] = list(built.values())
    spoken = [rec for rec, _ in done if rec["status"] != NO_SPEECH]
    day["status"] = NO_CONTENT if not spoken else PARTIAL if failures else COMPLETE
    return failures + settled


@dataclass
class Report:
    days: list[dict[str, Any]] = field(default_factory=list)
    deferred: list[dict[str, Any]] = field(default_factory=list)
    failures: list[dict[str, str]] = field(default_factory=list)
    attempted: int = 0                           # recordings looked at
    succeeded: int = 0

    @property
    def exit_code(self) -> int:
        """Non-zero when something was attempted and nothing came of it (a day with no archive directory counts)."""
        return 1 if self.failures and self.succeeded == 0 else 0


def discover(*, task: Path, adapters: Adapters, dates: Sequence[str] | None = None, selector: Selector | None = None) -> Report:
    """Run discovery over `dates`, or over the days that are recorded and not yet done (see the module docstring)."""
    report = Report()
    remote = adapters.remote
    with _one_discover_at_a_time(task):
        ledger = load_ledger(task)
        cfg = rules("discover")
        completed = [d for d, day in ledger["days"].items() if day.get("status") == COMPLETE]
        if (copied := copy_direct(remote, completed)) is not None and copied["status"] != "OK":
            report.failures.append({"date": "", "segment": "", "code": "DIRECT_COPY_FAILED", "detail": copied["detail"]})
        known, _now, recorded = list_dates(remote)
        if dates:
            wanted = list(dict.fromkeys(dates))
        else:
            if not recorded:
                report.deferred.append({"date": "", "reason": "NO_RECORDINGS_YET", "detail": f"{archive_room()} does not exist yet: nothing has been recorded for this room"})
            recent = [d for d in known if d >= str(cfg["auto_not_before"])][-int(cfg["auto_recent_dates"]):]
            wanted = recent
        for date in wanted:
            inv = inventory(remote, date)
            if not inv.exists:
                report.failures.append({"date": date, "segment": "", "code": NO_ARCHIVE_DIR, "detail": "no recording of this broadcast day and no such date directory in the archive"})
                continue
            before = ledger["days"].get(date, {})
            if not dates and before.get("status") in FINISHED and (before.get("recordings_reclaimed")
                                                                   or inv.same_recordings(before.get("segments", []))):
                continue                             # finished with exactly these recordings (a recording that came later
                                                     # reopens it), or finished and its recordings deleted (mark_recordings_reclaimed)
            settled = inv.settled(float(cfg["settle_minutes"]), float(cfg["lone_danmaku_hours"]))
            if not dates and not settled:
                report.deferred.append({"date": date, "reason": "RECORDING_MAY_BE_IN_PROGRESS" if inv.segments else "NO_RECORDINGS_YET"})
                continue
            if not settled:                          # a named day: its finished recordings now, the growing one later
                inv, held = inv.finished(float(cfg["settle_minutes"]))
                report.deferred += [{"date": date, "segment": s.name, "reason": "RECORDING_MAY_BE_IN_PROGRESS"} for s in held]
                if not inv.segments:
                    continue
            day = ledger["days"].setdefault(date, {"date": date})
            day.update(explicit=bool(dates), settled=settled)
            try:
                failures = _process_day(task, adapters, inv, day, selector)
            except DiscoverError as exc:           # the day's own configuration (its participants file): the other days go on
                report.failures.append({"date": date, "segment": "", **_failure(exc)})
                continue
            day["updated_at"] = utc_now()
            write_json(ledger_path(task), ledger)
            report.days.append(_summary(day))
            report.failures += failures
            report.attempted += sum(1 for s in day["segments"] if not s["stub"])
            report.succeeded += sum(1 for s in day["segments"] if s["status"] == "SELECTED")
        # must-cut clips the boundary step left under the minimum, of any day: grouped into compilations once the day's cuts
        # are known (short_queue; a finished day is not processed again above, so its queue is looked at here)
        for date, day in ledger["days"].items():
            before = json.dumps(day.get(short_queue.QUEUE_KEY), sort_keys=True)
            record = short_queue.day_queue(task, date, day, final=day.get("status") == COMPLETE and day.get("settled") is True,
                                           build=_build_short, adapters=adapters)
            if record is not None and json.dumps(record, sort_keys=True) != before:
                report.failures += [{"date": date, "segment": "", "code": f["code"], "detail": f["detail"]} for f in record["failures"]]
                write_json(ledger_path(task), ledger)
    return report


def _build_short(task: Path, date: str, adapters: Adapters, lead: dict[str, Any]) -> dict[str, Any]:
    return _build_joined(task, resolve_session(task, date)[0], adapters, lead)


def _summary(day: dict[str, Any]) -> dict[str, Any]:
    segments = day["segments"]
    selection = day["selection"]
    return {"date": day["date"], "status": day["status"], "recordings": len(segments),
            "stubs": sum(1 for s in segments if s["stub"]), "silent": sum(1 for s in segments if s["status"] == NO_SPEECH),
            "selected": sum(1 for s in segments if s["status"] == "SELECTED"),
            "failed": sum(1 for s in segments if s["status"].endswith("_FAILED")), "candidates": selection["pool"],
            "seated": len(selection["seated"]), "rejected": len(selection["rejected"]), "built": len(day["built"])}


def ledger_lines(task: Path) -> list[str]:
    """The ledger in a few lines per day (what `status` without --cid prints)."""
    days = load_ledger(task)["days"]
    if not days:
        return ["发现账本：还没有记录"]
    lines = []
    for date in sorted(days):
        s = _summary(days[date])
        lines.append(f"{date} {s['status']} 录像 {s['recordings']}（空壳 {s['stubs']}、无人声 {s['silent']}、已选片 {s['selected']}、失败 {s['failed']}） "
                     f"候选 {s['candidates']} 入选 {s['seated']} 落选 {s['rejected']} 已建片 {s['built']}  {days[date]['session']['note']}")
        lines += [f"    {r['name']} {r['status']} {r['prepare' if r['status'].startswith('PREPARE') else 'select']['code']}"
                  for r in days[date]["segments"] if r["status"].endswith("_FAILED")]
        lines += [f"    {r['name']} 弹幕突发区没交代 " + "、".join(b["id"] for b in silent) for r in days[date]["segments"]
                  if (silent := [b for b in r.get("bursts", []) if b["disposition"] == BURST_UNANSWERED])]
        lines += [f"    落选 {r.get('cid')} {r.get('reason')}" + (f"：{str(r['detail'])[:80]}" if r.get("detail") else "")
                  for r in days[date]["selection"]["rejected"]]         # why, without opening the ledger (goldfish 4)
        lines += short_queue.lines(days[date])
        if (policy := days[date]["selection"].get("policy") or {}).get("must_cut") is not None:
            # a must-cut draw that takes no seat (the channel's collab) has no daily cap: its number is said (T5,)
            lines.append(f"    必切：不占席 {policy['must_cut']} 条，占席 {policy.get('must_cut_seated', 0)} 条；主人标记 {policy.get('owner_marked', 0)} 条")
        lines += _burst_lines(days[date])
    return lines


_STAGES = {BURST_NOT_SELECTED: "模型不选", BURST_UNANSWERED: "没交代", BURST_IN_CANDIDATE: "提名了但候选不在池里",
           BURST_GATED: "没过门", BURST_PAST_GATE: "过门没入座", BURST_SEATED: "入座", BURST_MADE: "成片"}


def _burst_lines(day: dict[str, Any]) -> list[str]:
    """The day's bursts as a funnel (found → nominated → past the gates → seated → made), one line each, what the burst
    points changed, and the shadow counts' bursts the main count does not have."""
    rows = day["selection"].get("bursts")
    if rows is None:
        return []
    reached = Counter(r["stage"] for r in rows)
    past = sum(reached[s] for s in (BURST_PAST_GATE, BURST_SEATED, BURST_MADE))
    lines = [f"    弹幕突发区 {len(rows)}：模型不选 {reached[BURST_NOT_SELECTED]}、没交代 {reached[BURST_UNANSWERED]}；"
             f"提名 {len(rows) - reached[BURST_NOT_SELECTED] - reached[BURST_UNANSWERED]} → 过门 {past} → "
             f"入座 {reached[BURST_SEATED] + reached[BURST_MADE]} → 成片 {reached[BURST_MADE]}"]
    lines += [f"      {r['segment']} {r['id']} {r['start_ms'] // 60000:02d}:{r['start_ms'] // 1000 % 60:02d} {r['count']} 条 "
              f"{_STAGES[r['stage']]}" + (f" {r['cid']}" if r["cid"] else "") + (f"：{str(r['why'])[:60]}" if r["why"] else "")
              for r in rows]
    swaps = day["selection"].get("burst_swaps") or {}
    lines.append("    突发区加分换座：" + ("、".join(swaps["in"]) + " 换下 " + "、".join(swaps["out"]) if swaps.get("in") or swaps.get("out")
                                     else "没有（不加也是这几席）"))
    extra = Counter(kind for r in day.get("segments", []) for kind, found in (r.get("shadow_bursts") or {}).items()
                    for b in found if not b["main"])
    if any(r.get("shadow_bursts") for r in day.get("segments", [])):
        lines.append(f"    影子（只记下）：数人多出 {extra['users']} 个，数笑多出 {extra['laughs']} 个")
    return lines
