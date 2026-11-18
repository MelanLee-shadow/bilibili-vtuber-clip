"""`auto`: one unattended round (what the media host's timer runs).

1. `discover` over the days that are recorded and not yet done.
2. Every clip of the task that is not finished is run on (`run` resumes by products: a finished step is not made again).
   Finished means its last step is DONE - the upload when the round uploads, else the review.
   A reviewed clip someone kept back (`<clip>/upload/HOLD`) is left to them.
3. A clip that stopped at the same step for the same reason `STUCK_AFTER` rounds in a row is left alone and listed: someone
   has to look, and running it again would only spend the same calls - until another version is deployed: it may hold the
   fix, so the clip is run once more (1084420 sat stuck after its step-8 fix went live).  A clip waiting for the platform (the upload still
   going live, the day's quota) did not fail: it is polled every round.  Neither did a clip whose step stopped because
   the model gave no answer (the account's quota or balance, the route, an outage) or something else outside the clip
   was in the way (a redeploy under the round, a recogniser's rate limit; `provider_failed`): the next round
   asks again, however long the provider is away, and the round's report lists it under `waiting_provider`.

Publishing unattended has one switch on the media host, the one the old publication queue obeys (90-publish.md):
`<autoslice base>/PUBLISH_PAUSED`.  While it is there - or when the host cannot be asked - a round that was started with
the upload makes clips up to the sealed package and sends nothing; the round after the marker is removed uploads them.
A person running `run --upload` on a clip they looked at is not this switch's business.

4. At most one song attempt (song_day.py): the day's song is made up to a prepared package for review, never uploaded here.

5. The media of clips that are online is reclaimed (`reclaim`; lane_rules `auto`), and so are a day's recordings once
   its clips are all online or kept back and 115 has the recordings (`reclaim_recordings`).

The ledger `<task>/auto/ledger.json` keeps, per clip, how often it was run and where it last stopped.  Two rounds never run
at once (the second leaves at once; the timer's next tick takes over).
"""

from __future__ import annotations

import fcntl
import json
import shlex
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from .adapters import Remote, root_script
from .discover_media import write_json
from .lane_rules import rules
from .layout import TOPOLOGY_FILE, layout
from .stages import STEPS
from .state import ClipState, StateError, utc_now
from .step10_check import HOLD_FILE
from .step10_run import WAITING_LIVE, WAITING_LOCK, WAITING_QUOTA

LEDGER_SCHEMA = "manual-lane.auto-ledger.v1"
STUCK_AFTER = 3
WAITING = (WAITING_LIVE, WAITING_QUOTA, WAITING_LOCK)
BUSY = "ANOTHER_ROUND_RUNNING"
PAUSE_MARKER = "PUBLISH_PAUSED"


def publication_paused(remote: Remote, base: str) -> bool:
    """Whether the host's emergency switch for unattended publishing is set.  An answer that is not a plain OPEN counts
    as set: an unattended round never publishes on a guess."""
    result = remote.run_script(name="auto-publish-switch", timeout=120,
                               body=f"if [ -e {shlex.quote(base)}/{PAUSE_MARKER} ]; then echo PUBLISH_PAUSED; else echo PUBLISH_OPEN; fi\n")
    return result.rc != 0 or result.stdout.strip() != "PUBLISH_OPEN"


def deployed_version() -> str:
    """The commit the runtime was deployed from (deploy writes `<repo>/DEPLOYED_COMMIT`); "" outside a deployed tree."""
    path = Path(__file__).resolve().parents[3] / "DEPLOYED_COMMIT"
    return path.read_text(encoding="utf-8").strip() if path.is_file() else ""


def ledger_path(task: Path) -> Path:
    return task / "auto" / "ledger.json"


def _load(task: Path) -> dict[str, Any]:
    path = ledger_path(task)
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"schema": LEDGER_SCHEMA, "clips": {}}


def stopped_at(state: ClipState) -> tuple[str, str] | None:
    """The first step of the clip that stopped, with its reason; None when nothing stopped."""
    for step in STEPS:
        rec = state.step(step.key)
        if rec.get("status") == "STOPPED":
            return step.key, str(rec.get("reason") or "")
    return None


# what a stopped step's note says when the stop is the surroundings' and not the clip's: the code was redeployed under
# the running round (the tools refuse to mix two versions), a free recogniser turned the request away for its rate
OUTSIDE_STOPS = ("RUNTIME_VERSION_CHANGED_WITHIN_TASK", "RATE_LIMITED")


def provider_failed(state: ClipState, step: str) -> bool:
    """Whether the stopped step failed on something outside the clip, which the next round may find gone: a model call
    that came back without an answer after its tries (step_io._ask_cpa; with several runs of a step at once the failed
    call need not be the last one recorded), or one of OUTSIDE_STOPS in the step's note (the first
    unattended round: a redeploy and a recogniser's rate limit each stopped a clip)."""
    record = state.step(step)
    return (any(call.get("rc") not in (0, None) for call in record.get("cpa") or [])
            or any(mark in str(record.get("note") or "") for mark in OUTSIDE_STOPS))


def finished(state: ClipState, *, upload: bool) -> bool:
    return state.step("upload" if upload else "review").get("status") == "DONE"


def pending(task: Path, ledger: dict[str, Any], *, upload: bool,
            version: str | None = None) -> tuple[list[str], list[dict[str, Any]]]:
    """(the clips to run this round, the clips left alone as stuck under this deployed `version`)."""
    version = deployed_version() if version is None else version
    run, stuck = [], []
    for path in sorted((task / "state").glob("*.json")):
        state = ClipState.load(path)
        cid = state.data["clip"]["cid"]
        if state.data["clip"].get("status") == "SUPERSEDED" or finished(state, upload=upload):
            continue
        if (task / "clips" / cid / HOLD_FILE).is_file():
            continue                                  # kept back by someone: not run on (clips held mid-way kept using models)
        seen = ledger["clips"].get(cid, {}).get("stopped")
        if (seen and seen["times"] >= STUCK_AFTER and stopped_at(state) == (seen["step"], seen["reason"])
                and seen.get("version", "") == version):
            stuck.append({"cid": cid, **seen})
        else:
            run.append(cid)
    return run, stuck


def _record(task: Path, ledger: dict[str, Any], cids: Sequence[str]) -> list[str]:
    """Note where each clip of the round stopped; returns the clips that wait for the model provider."""
    waiting = []
    for cid in cids:
        state = ClipState.load(task / "state" / f"{cid}.json")
        entry = ledger["clips"].setdefault(cid, {"runs": 0})
        entry["runs"] += 1
        entry["last_run"] = utc_now()
        stop = stopped_at(state)
        if stop is not None and stop[1] not in WAITING and provider_failed(state, stop[0]):
            waiting.append(cid)
        if stop is None or stop[1] in WAITING or cid in waiting:
            entry.pop("stopped", None)
        else:
            seen = entry.get("stopped") or {}
            same = (seen.get("step"), seen.get("reason")) == stop
            same = same and seen.get("version", "") == deployed_version()
            entry["stopped"] = {"step": stop[0], "reason": stop[1], "times": seen["times"] + 1 if same else 1,
                                "version": deployed_version()}
    write_json(ledger_path(task), ledger)
    return waiting


MEDIA = ("mp4", "mkv", "flv", "wav", "mp3", "m4a")


def _delete_media(directory: Path, least: int) -> int:
    """Delete the video and audio files of `least` bytes or more under a local directory; returns how many."""
    gone = 0
    for path in directory.rglob("*") if directory.is_dir() else ():
        if path.is_file() and not path.is_symlink() and path.suffix.lstrip(".").lower() in MEDIA and path.stat().st_size >= least:
            path.unlink()
            gone += 1
    return gone


def _states(task: Path) -> dict[str, ClipState]:
    return {p.stem: ClipState.load(p) for p in sorted((task / "state").glob("*.json"))} if (task / "state").is_dir() else {}


def _online_of(task: Path, states: dict[str, ClipState], cid: str, seen: frozenset[str] = frozenset()) -> Path | None:
    """The clip's online receipt, following a supersession (trim, revise) to the clip that replaced it; None if not online."""
    state = states.get(cid)
    if state is None or cid in seen:
        return None
    if (after := state.data["clip"].get("superseded_by")):
        return _online_of(task, states, str(after), seen | {cid})
    online = task / "clips" / cid / "upload" / "online.json"
    return online if state.step("upload").get("status") == "DONE" and online.is_file() else None


def recordings_released(task: Path, days: dict[str, Any], states: dict[str, ClipState]) -> list[str]:
    """The recorded days the lane is done with: discovery finished the day, and every clip cut from it is online (or
    replaced by one that is) or kept back (`upload/HOLD`; if it is released later it reads the recording from 115)."""
    from .discover import FINISHED

    open_days = {str(state.data["clip"].get("recording_date")) for cid, state in states.items()
                 if state.data["clip"].get("status") != "SUPERSEDED" and _online_of(task, states, cid) is None
                 and not (task / "clips" / cid / HOLD_FILE).is_file()}
    return sorted(d for d, day in days.items() if day.get("status") in FINISHED and d not in open_days)


def reclaim_recordings(task: Path, ledger: dict[str, Any], remote: Remote) -> list[dict[str, Any]]:
    """公开规则：旧录像我说过了，切片发出去后AND上传到网盘后就可以删掉. For each day the lane is done with
 (`recordings_released`), the lane's copy of each video (layout `recordings_direct_root`) is deleted when 115 has a
 file of the same name and size (the archive, layout `archive_room_dir`) and the recorder's spool no longer holds it
 (the spool mover lets go of a segment only after 115 and 123 both reported it finished). Chat files stay (small, and
 a released clip may still read them). A day is noted in the ledger once nothing large of it is left."""
    from .discover import load_ledger as discover_ledger
    from .discover import mark_recordings_reclaimed
    from .discover_media import archive_dir, direct_room

    room = direct_room()
    if room is None:
        return []
    spool = f"{json.loads(TOPOLOGY_FILE.read_text(encoding='utf-8'))['spool_root']}/{layout().room_id}"
    least = int(rules("auto")["media_min_bytes"])
    reclaimed = ledger.setdefault("recordings_reclaimed", {})
    done = []
    for date in recordings_released(task, discover_ledger(task)["days"], _states(task)):
        if date in reclaimed:
            continue
        try:
            mark_recordings_reclaimed(task, date)
        except StateError as exc:                    # a discover is running, or the day reopened: next round
            done.append({"date": date, "what": "recordings", "status": "DEFERRED", "detail": str(exc)})
            continue
        day, arch, held = (shlex.quote(path) for path in (f"{room}/{date}", archive_dir(date), f"{spool}/{date}"))
        command = (f"[ -d {day} ] || {{ echo RECLAIM_DONE; exit 0; }}; left=0; "
                   f"for f in {day}/*.flv; do [ -f \"$f\" ] || continue; n=$(basename \"$f\"); s=$(stat -c %s \"$f\"); "
                   f"a=$(timeout 120 stat -c %s {arch}/\"$n\" 2>/dev/null || echo MISSING); "
                   f"if [ ! -e {held}/\"$n\" ] && [ \"$a\" = \"$s\" ] && ! fuser -s \"$f\" 2>/dev/null; then rm -f \"$f\" && echo DELETED $n $s; "
                   f"else echo KEPT $n $s $a; [ \"$s\" -ge {least} ] && left=1; fi; done; "
                   "[ $left = 0 ] && echo RECLAIM_DONE; echo RECLAIM_OK")
        result = remote.run_script(name=f"auto-reclaim-recordings-{date}", timeout=1800,
                                   body=root_script(command.replace("'", "'\\''"), env=()))
        lines = result.stdout.splitlines()
        if result.rc != 0 or not {"RECLAIM_OK", "RECLAIM_DONE"} & set(lines):
            done.append({"date": date, "what": "recordings", "status": "FAILED", "detail": (result.stdout + result.stderr)[-300:]})
            continue
        entry = {"date": date, "what": "recordings", "deleted": [line.split()[1] for line in lines if line.startswith("DELETED ")],
                 "kept": [line.split(maxsplit=1)[1] for line in lines if line.startswith("KEPT ")]}
        if "RECLAIM_DONE" in lines:
            reclaimed[date] = {"at": utc_now(), "deleted": entry["deleted"]}
        if entry["deleted"] or "RECLAIM_DONE" in lines:
            done.append(entry)
    if done:
        write_json(ledger_path(task), ledger)
    return done


def reclaim(task: Path, ledger: dict[str, Any], remote: Remote, *, now: float) -> list[dict[str, Any]]:
    """The media of clips that are online (lane_rules `auto`): the intermediates at once, the package's video after
    `final_keep_days`.  A clip superseded (trim, revise) by one that is online counts as online with it: nothing will
    read its media again (originals were never reclaimed).  Text, subtitles, covers and records stay.  What
    was done is noted in the ledger, once."""
    cfg = rules("auto")
    least, keep_s = int(cfg["media_min_bytes"]), float(cfg["final_keep_days"]) * 86400
    states = _states(task)

    def online_of(cid: str) -> Path | None:
        return _online_of(task, states, cid)

    done = []
    for state in states.values():
        cid = state.data["clip"]["cid"]
        if (online := online_of(cid)) is None:
            continue
        entry = ledger["clips"].setdefault(cid, {"runs": 0})
        if "intermediates_deleted" not in entry:
            work = shlex.quote(str(state.data["clip"]["remote_dir"]))
            names = " -o ".join(f"-iname '*.{ext}'" for ext in MEDIA)
            result = remote.run_script(name=f"auto-reclaim-{cid}", timeout=600,
                                       body=f"[ -d {work} ] && find {work} -type f \\( {names} \\) -size +{least - 1}c -delete; echo RECLAIMED\n")
            if "RECLAIMED" in result.stdout:
                local = _delete_media(task / "clips" / cid / "fmr_source", least)
                entry["intermediates_deleted"] = utc_now()
                done.append({"cid": cid, "what": "intermediates", "local_files": local})
        if "final_deleted" not in entry and now - online.stat().st_mtime >= keep_s:
            files = sum(_delete_media(task / "clips" / cid / sub, least) for sub in ("pkg", "fmr"))
            entry["final_deleted"] = utc_now()
            done.append({"cid": cid, "what": "final", "local_files": files})
    if done:
        write_json(ledger_path(task), ledger)
    return done


def auto_round(task: Path, *, discover_days: Callable[[], dict[str, Any]], run_clips: Callable[[list[str]], int],
               upload: bool, remote: Remote | None = None, now: float | None = None,
               run_songs: Callable[[], list[dict[str, Any]]] | None = None) -> dict[str, Any]:
    """One round; returns what it did ({discover, ran, stuck, finished, songs} or {status: BUSY})."""
    (task / "auto").mkdir(parents=True, exist_ok=True)
    with (task / "auto" / ".lock").open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"status": BUSY}
        found = discover_days()
        ledger = _load(task)
        run, stuck = pending(task, ledger, upload=upload) if (task / "state").is_dir() else ([], [])
        code = run_clips(run) if run else 0
        waiting = _record(task, ledger, run)
        done = [cid for cid in run if finished(ClipState.load(task / "state" / f"{cid}.json"), upload=upload)]
        songs = run_songs() if run_songs is not None else []
        reclaimed = reclaim(task, ledger, remote, now=time.time() if now is None else now) if remote is not None else []
        reclaimed += reclaim_recordings(task, ledger, remote) if remote is not None else []
        return {"status": "ROUND_DONE", "discover": found, "ran": run, "finished": done, "stuck": stuck, "run_exit_code": code,
                "reclaimed": reclaimed, "waiting_provider": waiting, "songs": songs}
