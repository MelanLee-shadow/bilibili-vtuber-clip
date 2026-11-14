"""`auto`: one unattended round (what the media host's timer runs).

1. `discover` over the days that are recorded and not yet done.
2. Every clip of the task that is not finished is run on (`run` resumes by products: a finished step is not made again).
   Finished means its last step is DONE - the upload when the round uploads, else the review.
   A reviewed clip someone kept back (`<clip>/upload/HOLD`) is left to them.
3. A clip that stopped at the same step for the same reason `STUCK_AFTER` rounds in a row is left alone and listed: someone
   has to look, and running it again would only spend the same calls.  A clip waiting for the platform (the upload still
   going live, the day's quota) did not fail: it is polled every round.  Neither did a clip whose step stopped because
   the model gave no answer (the account's quota or balance, the route, an outage) or something else outside the clip
   was in the way (a redeploy under the round, a recogniser's rate limit; `provider_failed`): the next round
   asks again, however long the provider is away, and the round's report lists it under `waiting_provider`.

Publishing unattended has one switch on the media host, the one the old publication queue obeys (90-publish.md):
`<autoslice base>/PUBLISH_PAUSED`.  While it is there - or when the host cannot be asked - a round that was started with
the upload makes clips up to the sealed package and sends nothing; the round after the marker is removed uploads them.
A person running `run --upload` on a clip they looked at is not this switch's business.

4. The media of clips that are online is reclaimed (`reclaim`; lane_rules `auto`).

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

from .adapters import Remote
from .discover_media import write_json
from .lane_rules import rules
from .stages import STEPS
from .state import ClipState, utc_now
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


def pending(task: Path, ledger: dict[str, Any], *, upload: bool) -> tuple[list[str], list[dict[str, Any]]]:
    """(the clips to run this round, the clips left alone as stuck)."""
    run, stuck = [], []
    for path in sorted((task / "state").glob("*.json")):
        state = ClipState.load(path)
        cid = state.data["clip"]["cid"]
        if state.data["clip"].get("status") == "SUPERSEDED" or finished(state, upload=upload):
            continue
        if upload and state.step("review").get("status") == "DONE" and (task / "clips" / cid / HOLD_FILE).is_file():
            continue                                  # looked at and kept back by someone: only the upload is left, and it is theirs
        seen = ledger["clips"].get(cid, {}).get("stopped")
        if seen and seen["times"] >= STUCK_AFTER and stopped_at(state) == (seen["step"], seen["reason"]):
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
            entry["stopped"] = {"step": stop[0], "reason": stop[1], "times": seen["times"] + 1 if same else 1}
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


def reclaim(task: Path, ledger: dict[str, Any], remote: Remote, *, now: float) -> list[dict[str, Any]]:
    """The media of clips that are online (lane_rules `auto`): the intermediates at once, the package's video after
    `final_keep_days`.  Text, subtitles, covers and records stay.  What was done is noted in the ledger, once."""
    cfg = rules("auto")
    least, keep_s = int(cfg["media_min_bytes"]), float(cfg["final_keep_days"]) * 86400
    done = []
    for path in sorted((task / "state").glob("*.json")) if (task / "state").is_dir() else ():
        state = ClipState.load(path)
        cid = state.data["clip"]["cid"]
        online = task / "clips" / cid / "upload" / "online.json"
        if state.step("upload").get("status") != "DONE" or not online.is_file():
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
               upload: bool, remote: Remote | None = None, now: float | None = None) -> dict[str, Any]:
    """One round; returns what it did ({discover, ran, stuck, finished} or {status: BUSY})."""
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
        reclaimed = reclaim(task, ledger, remote, now=time.time() if now is None else now) if remote is not None else []
        return {"status": "ROUND_DONE", "discover": found, "ran": run, "finished": done, "stuck": stuck, "run_exit_code": code,
                "reclaimed": reclaimed, "waiting_provider": waiting}
