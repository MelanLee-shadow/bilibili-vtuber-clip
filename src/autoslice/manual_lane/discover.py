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

Session: `recording_date` is the day; `participants` and `speaker_mode` are the channel profile's single-host defaults
(the host, `uniform_host`) unless the day's directory holds `participants.json` (keys of a session config other than
`recording_date`), which then decides.  Whether a session is a collaboration is not judged here: the ledger says so.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
from collections.abc import Callable, Iterator, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, cast

from .adapters import Adapters
from .clip_init import clip_from_entry, create_clip_state
from .config import SessionBlock, SessionConfig, load_session_config
from .discover_media import (
    NO_SPEECH,
    DayInventory,
    DiscoverError,
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
from .discover_select import RecallCallError, Selector, lane_cid, load_recall, recall_segment, seat_day
from .lane_rules import rules
from .layout import REPO_ROOT
from .multi_run import say
from .runner import Ctx
from .state import SCHEMA as STATE_SCHEMA
from .state import ClipData, ClipState, StateError, utc_now
from .step_io import _ask_cpa, _cpa_ok

LEDGER_SCHEMA = "manual-lane.discover-ledger.v1"
RECALL_PURPOSE = "discover-recall"
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


def resolve_session(task: Path, date: str) -> tuple[SessionConfig, dict[str, str]]:
    """The day's session config (written as `<day dir>/session.json`) and where its people came from."""
    from src.autoslice.channel_profile import load_channel_profile

    day_dir = root_of(task) / date
    doc: dict[str, Any] = {"recording_date": date, "participants": [list(load_channel_profile(REPO_ROOT).self_reference_aliases)],
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


def _ask_for(task: Path, segment: Segment, adapters: Adapters) -> Callable[[str], str]:
    """The model call of one recording's selection: the lane's CPA call (cached by prompt, effort by purpose), under the
    process's shared `cpa` bound; a failed call raises."""
    from .parallel import slot

    directory = root_of(task) / segment.date / segment.name
    state = ClipState(directory / "recall.state.json", cast(ClipData, {"schema": STATE_SCHEMA, "clip": {"cid": segment.name}, "steps": {}}))
    ctx = Ctx(state=state, session=cast(SessionBlock, {}), clip_dir=directory, remote_dir=remote_dir_of(segment), adapters=adapters)

    def ask(prompt: str) -> str:
        with slot("cpa"):
            record = _ask_cpa(ctx, f"recall-{hashlib.sha256(prompt.encode('utf-8')).hexdigest()[:16]}", prompt, purpose=RECALL_PURPOSE)
        if not _cpa_ok(record):
            raise RecallCallError(str(record.get("stderr_tail") or record.get("reason") or "CPA call failed"))
        return record["response_text"]

    return ask


def _failure(exc: Exception) -> dict[str, str]:
    return {"code": exc.code if isinstance(exc, DiscoverError) else type(exc).__name__, "detail": str(exc)[:400]}


def _work_segment(task: Path, adapters: Adapters, segment: Segment, selector: Selector | None
                  ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """One recording prepared and its candidates recalled.  Returns its ledger record and its candidate entries (with clip
    ids); a failure is the record's `reason`, never an exception."""
    directory = root_of(task) / segment.date / segment.name
    rec: dict[str, Any] = {"name": segment.name, "path": segment.path, "size": segment.size, "mtime": segment.mtime,
                           "xml_exists": segment.xml_fp is not None, "stub": False, "source_fp": segment.source_fp}
    phase = "prepare"
    try:
        prior = load_prepared(directory)
        prepared = prepare(adapters.remote, segment, directory, prior)
        rec["prepare"] = {"status": "OK", "srt_sha256": prepared["srt_sha256"], "xml_sha256": prepared["xml"]["sha256"]}
        phase = "select"
        reused = load_recall(directory, prepared) is not None
        recall = recall_segment(prepared, directory, ask=_ask_for(task, segment, adapters), selector=selector)
        entries = [dict(e, cid=lane_cid(e["candidate_id"], segment.name)) for e in recall["input_pool"]]
        rec["select"] = {"status": "OK", "reused": reused, "candidates": len(entries),
                         "inputs": recall["inputs"], "danmaku": "OK" if prepared["xml"]["exists"] else "MISSING"}
        rec["status"] = "SELECTED"
        return rec, entries
    except Exception as exc:  # noqa: BLE001 - the per-recording boundary: one recording's failure must not stop the others
        if phase == "prepare" and isinstance(exc, DiscoverError) and exc.code == NO_SPEECH:
            rec["prepare"], rec["status"] = {"status": NO_SPEECH}, NO_SPEECH
            return rec, []
        rec[phase] = {"status": "FAILED", **_failure(exc)}
        rec["status"] = "PREPARE_FAILED" if phase == "prepare" else "SELECT_FAILED"
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


def _process_day(task: Path, adapters: Adapters, inv: DayInventory, day: dict[str, Any], selector: Selector | None) -> list[dict[str, str]]:
    """One day's four phases; updates `day` (its ledger entry) and returns the failures."""
    date = inv.date
    session, session_note = resolve_session(task, date)
    day["session"] = session_note
    content = [s for s in inv.segments if not s.stub]
    stubs = [{"name": s.name, "path": s.path, "size": s.size, "mtime": s.mtime, "xml_exists": s.xml_fp is not None,
              "stub": True, "status": "STUB_SKIPPED"} for s in inv.segments if s.stub]
    with ThreadPoolExecutor(max_workers=max(1, len(content)), thread_name_prefix="lane-discover") as pool:
        done = list(pool.map(lambda s: _work_segment(task, adapters, s, selector), content))
    failures = [{"date": date, "segment": rec["name"], **rec[phase]} for rec, _ in done
                for phase in ("prepare", "select") if rec.get(phase, {}).get("status") == "FAILED"]
    for rec, entries in done:
        say(f"[{date} {rec['name']}] {rec['status']}" + (f" 候选 {len(entries)}" if entries else ""))
    day["segments"] = sorted([rec for rec, _ in done] + stubs, key=lambda r: r["name"])
    pool_entries = [e for _, entries in done for e in entries]

    built = {b["cid"]: b for b in day.get("built", [])}
    for entry in pool_entries:        # a clip that exists is built, whoever made it: it keeps its seat and is not made again
        state = task / "state" / f"{entry['cid']}.json"
        if state.exists() and entry["cid"] not in built:
            built[entry["cid"]] = _built_record(entry, state)
    seating = seat_day(date, pool_entries, list(built.values()))
    for entry in seating.seated:
        try:
            built[entry["cid"]] = _build(task, session, entry)
        except Exception as exc:  # noqa: BLE001 - one clip's failure must not stop the others
            failures.append({"date": date, "segment": entry["source"]["name"], **_failure(exc)})
    day["selection"] = {"policy": seating.policy, "pool": len(pool_entries),
                        "seated": [e["cid"] for e in seating.seated if e["cid"] in built], "rejected": seating.rejected}
    day["built"] = list(built.values())
    spoken = [rec for rec, _ in done if rec["status"] != NO_SPEECH]
    day["status"] = NO_CONTENT if not spoken else PARTIAL if failures else COMPLETE
    return failures


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
        if dates:
            wanted = list(dict.fromkeys(dates))
        else:
            known, _now, recorded = list_dates(remote)
            if not recorded:
                report.deferred.append({"date": "", "reason": "NO_RECORDINGS_YET", "detail": f"{archive_room()} does not exist yet: nothing has been recorded for this room"})
            recent = [d for d in known if d >= str(cfg["auto_not_before"])][-int(cfg["auto_recent_dates"]):]
            wanted = recent
        for date in wanted:
            inv = inventory(remote, date)
            if not inv.exists:
                report.failures.append({"date": date, "segment": "", "code": NO_ARCHIVE_DIR, "detail": "no such date directory in the archive"})
                continue
            before = ledger["days"].get(date, {})
            if not dates and before.get("status") in FINISHED and inv.same_recordings(before.get("segments", [])):
                continue                             # finished with exactly these recordings; a recording that came later reopens it
            settled = inv.settled(float(cfg["settle_minutes"]), float(cfg["lone_danmaku_hours"]))
            if not dates and not settled:
                report.deferred.append({"date": date, "reason": "RECORDING_MAY_BE_IN_PROGRESS" if inv.segments else "NO_RECORDINGS_YET"})
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
    return report


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
    return lines
