"""The day's song clip (docs/pipeline/50-song-lane.md; 公开规则：默认是每天5条限额和一条歌切).

Discover keeps a recording's song windows in recall.json (kind "song") but seats only talk: a song has its own producer,
the song lane's private-directory entry, which `scripts/manual_lane_song.py` calls on the media host. Until
nothing called it once the old runner was switched off, and no song was made after .

Each unattended round (auto_run) makes at most one song attempt. A day takes part once discover has settled it; its
song windows are tried in recording order, skipping a title the lane already made for another day (the producer itself
refuses a title already published). The first PRODUCED ends the day (one song a day at most; no song is forced when none
is deliverable). A REJECTED window is not tried again; a WAITING one (a provider or the media away) is asked again next
round, at most WAIT_LIMIT times before the next window is tried. A day whose recording was reclaimed is closed. What
is produced is a prepared package: it is reviewed before anything is uploaded (公开规则：不要放行无人值守自动发布。).
The ledger `<task>/songs/ledger.json` keeps every attempt."""

from __future__ import annotations

import json
import re
import shlex
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from src.autoslice.published_song_history import normalize_song_title

from .adapters import REMOTE_REPO, Remote, remote_python, root_script
from .discover_media import write_json
from .layout import layout
from .state import utc_now

SCHEMA = "manual-lane.songs.v1"
PRODUCED, WAITING, REJECTED, SOURCE_GONE = "PRODUCED", "WAITING", "REJECTED", "SOURCE_GONE"
DAY_DONE, DAY_NONE = "SONG_PRODUCED", "NO_DELIVERABLE_SONG"
WAIT_LIMIT = 6          # the song lane's own infrastructure retry cap (SONG_INFRA_RETRY_CAP)
PRODUCER = Path(__file__).resolve().parents[3] / "scripts" / "manual_lane_song.py"   # absent where the song lane is not shipped
PAUSED = '公开规则：目前暂停歌切。不需要去发歌切了，你把它暂停掉吧。'   # "" resumes the song attempt


def enabled() -> bool:
    return not PAUSED and PRODUCER.is_file()


def ledger_path(task: Path) -> Path:
    return task / "songs" / "ledger.json"


def load(task: Path) -> dict[str, Any]:
    path = ledger_path(task)
    if not path.exists():
        return {"schema": SCHEMA, "days": {}}
    ledger: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    if ledger.get("schema") != SCHEMA:
        raise ValueError(f"unexpected schema in {path}: {ledger.get('schema')!r}")
    return ledger


def _title(hook: object) -> str:
    found = re.findall(r"《([^》]+)》", str(hook or ""))
    return found[0] if found else ""


def candidates(task: Path, date: str) -> list[dict[str, Any]]:
    """The day's song windows, in recording order: cid, the recall file it came from, the start/end and the quoted title."""
    found = []
    for recall in sorted((task / "discover" / date).glob("*/recall.json")):
        for entry in json.loads(recall.read_text(encoding="utf-8")).get("input_pool") or []:
            if entry.get("kind") == "song":
                found.append({"cid": entry["cid"], "recall": str(recall), "segment": recall.parent.name,
                              "start_ms": int(entry["start_ms"]), "end_ms": int(entry["end_ms"]), "title": _title(entry.get("hook"))})
    return sorted(found, key=lambda c: (c["segment"], c["start_ms"]))


def _made_titles(ledger: Mapping[str, Any], except_day: str) -> set[str]:
    return {normalize_song_title(a.get("title") or "") for date, day in ledger["days"].items() if date != except_day
            for a in day.get("attempts") or [] if a["outcome"] == PRODUCED and a.get("title")} - {""}


def next_window(ledger: Mapping[str, Any], date: str, windows: list[dict[str, Any]]) -> dict[str, Any] | None:
    day = ledger["days"].get(date) or {}
    tries: dict[str, list[str]] = {}
    for a in day.get("attempts") or []:
        tries.setdefault(a["cid"], []).append(a["outcome"])
    made = _made_titles(ledger, date)
    for window in windows:
        outcomes = tries.get(window["cid"], [])
        if REJECTED in outcomes or SOURCE_GONE in outcomes or outcomes.count(WAITING) >= WAIT_LIMIT:
            continue
        if window["title"] and normalize_song_title(window["title"]) in made:
            continue
        return window
    return None


def song_round(task: Path, *, days: Mapping[str, Mapping[str, Any]],
               attempt: Callable[[str, Mapping[str, Any]], Mapping[str, Any]]) -> list[dict[str, Any]]:
    """At most one song attempt over the settled days; returns what was done (for the round's report)."""
    ledger, done = load(task), []
    for date in sorted(days):
        info = days[date]
        if info.get("status") != "COMPLETE" or not info.get("settled"):
            continue
        day = ledger["days"].setdefault(date, {"status": "OPEN", "attempts": []})
        if day["status"] != "OPEN":
            continue
        window = next_window(ledger, date, candidates(task, date))
        if window is None:
            day["status"] = DAY_NONE
            write_json(ledger_path(task), ledger)
            continue
        doc = attempt(date, window)
        produced = doc.get("result") or {}
        record = {"cid": window["cid"], "segment": window["segment"], "outcome": doc.get("outcome", WAITING),
                  "title": str(produced.get("title") or window["title"] or ""), "reason_codes": doc.get("reason_codes") or [],
                  "at": utc_now()}
        day["attempts"].append(record)
        if record["outcome"] == PRODUCED:
            day["status"] = DAY_DONE
        write_json(ledger_path(task), ledger)
        if record["outcome"] == SOURCE_GONE:          # nothing was produced or asked: the round tries the next day
            day["status"] = SOURCE_GONE
            write_json(ledger_path(task), ledger)
            done.append({"date": date, **record})
            continue
        return [*done, {"date": date, **record}]
    return done


ATTEMPT_TIMEOUT_S = 4 * 3600       # the producer's tight + full-source selector runs (each up to 90 min) and the packaging


def remote_attempt(task: Path, remote: Remote) -> Callable[[str, Mapping[str, Any]], dict[str, Any]]:
    """One song attempt on the media host: the window's recall and BCUT SRT are put beside the tool's result, the tool
    runs as root with the CPA credentials (the producer's own Gemini keys come from its environment), and the result is
    fetched back into <task>/songs/<date>/.  No result is WAITING with the tool's exit code."""
    lay = layout()

    def attempt(date: str, window: Mapping[str, Any]) -> dict[str, Any]:
        rd, cid = f"{lay.work_root}/songs/{date}", str(window["cid"])
        recall = Path(str(window["recall"]))
        remote_recall = f"{rd}/{window['segment']}/recall.json"
        remote.put(recall, remote_recall)
        remote.put(recall.with_name("bcut.srt"), f"{rd}/{window['segment']}/bcut.srt")
        result = f"{rd}/{cid}.result.json"
        tool = remote_python(" ".join(map(shlex.quote, [
            "scripts/manual_lane_song.py", "--date", date, "--recall", remote_recall, "--candidate", cid,
            "--output-root", f"{lay.autoslice_base}/private-runs/lane-songs", "--result", result])))
        body = f"rm -f {shlex.quote(result)}\n" + root_script(
            f"cd {shlex.quote(REMOTE_REPO)} && export AUTOSLICE_BASE={shlex.quote(lay.autoslice_base)} && {tool}".replace("'", "'\\''"),
            env=["cpa"], owned=rd, recursive=True)
        res = remote.run_script(name=f"song-{date}-{cid}", body=body, timeout=ATTEMPT_TIMEOUT_S)
        local = task / "songs" / date / f"{cid}.result.json"
        try:
            remote.get(result, local)
            return dict(json.loads(local.read_text(encoding="utf-8")))
        except (RuntimeError, OSError, ValueError):
            return {"outcome": WAITING, "reason_codes": [f"SONG_TOOL_NO_RESULT_RC{res.rc}"],
                    "tail": (res.stdout + res.stderr)[-400:]}

    return attempt


def ledger_lines(task: Path) -> list[str]:
    ledger = load(task)
    lines = [f"歌切已暂停：{PAUSED}"] if PAUSED else []
    for date in sorted(ledger["days"]):
        day = ledger["days"][date]
        last = (day.get("attempts") or [{}])[-1]
        lines.append(f"歌切 {date} {day['status']}  试过 {len(day.get('attempts') or [])} 次"
                     + (f"，最后 {last.get('cid')}《{last.get('title')}》{last.get('outcome')} {','.join(last.get('reason_codes') or [])[:120]}"
                        if last else ""))
    return lines
