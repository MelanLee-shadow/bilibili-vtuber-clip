"""Must-cut clips whose whole story is shorter than the talk minimum: queued, grouped, made into one compilation.

The boundary step (step_cut.step1_boundary) first widens a short story by the interaction it belongs to on either side;
a must-cut clip (an owner's mark or a must-cut core draw) that is still under `gates.CLIP_MIN_SECONDS` stops with
`gates.BOUNDARY_SHORT_QUEUED`, its cut kept in `boundary/decision.json`.  Here, after discovery, every day of the ledger
with such clips is looked at (docs/pipeline/30-boundary.md 不足 60 秒的必切片):

- the day is grouped only once its selection is final (`final`: discover's day COMPLETE and settled, not a named day whose
  recording is still growing) and every clip built for it has its boundary decided (none still to run step 1, none
  waiting for the model; a clip a person holds back does not count): a short clip of the same draw may still come;
- one group per must-cut draw of the day that is a theme of its own (core_draws `compiles`: 撒娇 with 撒娇), read from the
  draws it was seated for, or for a clip seated for an owner's mark from its labels (core_draws.must_cut); its pieces in
  broadcast order; a group is
  cut in broadcast order into compilations of at most `gates.CLIP_MAX_SECONDS`, and one is made when it has two pieces or
  more and together more than `gates.CLIP_MIN_SECONDS`.  It is made by the same-session compilation with the pieces
  already decided (`discover._build_joined`; no boundary review on it), and its members are marked SUPERSEDED by it;
- what is not made stays in the queue, with why, in the day's ledger record `short_queue` (`ledger_lines` prints it).
  A clip with no must-cut draw (an owner's mark alone) has no theme to be grouped by (NO_THEME); a must-cut draw that is
  not a theme (联动同场: something between her and a colleague, any matter) does not make pieces one subject (NOT_A_THEME):
  both wait for a person.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from . import core_draws, gates
from .adapters import Adapters
from .auto_run import HOLD_FILE, provider_failed
from .clip_init import clip_entry
from .discover_select import MUST_CUT
from .reselect import SUPERSEDED, supersede_candidate
from .state import ClipState, StateError

QUEUE_KEY = "short_queue"                 # the day's ledger record
ALONE, TOO_SHORT, NO_THEME, NOT_A_THEME = "ALONE", "TOO_SHORT", "NO_THEME", "NOT_A_THEME"
AUTHORITY = "docs/pipeline/30-boundary.md 不足 60 秒的必切片"
QUOTE = "完整故事仍不足 60 秒的必切片进合集队列，当晚同题材凑够两段、超过 60 秒时按直播先后剪成一条合集"   # the rule, 30-boundary.md


def _theme(entry: Mapping[str, Any], asset: Mapping[str, Any] | None) -> tuple[str | None, list[str]]:
    """(the draw the piece is grouped by, its must-cut draws): its first must-cut draw that is a theme (`compiles`), None
    when it has none.  A must-cut seat carries the draws it was seated for (discover_select writes `must_cut`; labels of
    an older asset version would read differently now); a clip seated for an owner's mark has none: read from its labels."""
    draws = list(entry["must_cut"]) if entry["seat_reason"] == MUST_CUT else core_draws.must_cut(entry, asset)
    themes = {d["key"] for d in (asset or {}).get("draws", []) if d.get("compiles")}
    return next((k for k in draws if k in themes), None), draws


def _piece(task: Path, cid: str, state: ClipState, asset: Mapping[str, Any] | None) -> dict[str, Any]:
    """A queued clip as a compilation member: its selection entry with the cut the boundary step decided."""
    entry = clip_entry(state.data["clip"])
    if entry is None:                     # only a clip with an entry is must-cut (step_cut._must_cut)
        raise StateError(f"{cid}: a queued clip has no selection entry")
    adopted = json.loads((task / "clips" / cid / "boundary" / "decision.json").read_text(encoding="utf-8"))["adopted"]
    theme, draws = _theme(entry, asset)
    return dict(entry, cid=cid, cue_ids=[str(adopted["start_cue"]), str(adopted["end_cue"])],
                start_ms=int(adopted["start_ms"]), end_ms=int(adopted["end_ms"]), theme=theme, draws=draws)


def chunks(pieces: Sequence[Mapping[str, Any]]) -> tuple[list[list[Mapping[str, Any]]], list[tuple[Mapping[str, Any], str]]]:
    """(the compilations to make, the pieces left waiting with why) of one theme's pieces in broadcast order: cut in order
    into runs of at most CLIP_MAX_SECONDS; a run is made with two pieces or more and more than CLIP_MIN_SECONDS."""
    runs: list[list[Mapping[str, Any]]] = []
    for piece in pieces:
        span = int(piece["end_ms"]) - int(piece["start_ms"])
        if runs and sum(int(p["end_ms"]) - int(p["start_ms"]) for p in runs[-1]) + span <= gates.CLIP_MAX_SECONDS * 1000:
            runs[-1].append(piece)
        else:
            runs.append([piece])
    made, waiting = [], []
    for run in runs:
        total = sum(int(p["end_ms"]) - int(p["start_ms"]) for p in run) / 1000
        if len(run) >= 2 and total > gates.CLIP_MIN_SECONDS:
            made.append(run)
        else:
            why = ALONE if len(run) == 1 else TOO_SHORT
            waiting += [(p, f"{why}：同题材 {len(run)} 段合计 {total:.1f}s") for p in run]
    return made, waiting


def _order(piece: Mapping[str, Any]) -> tuple[str, int]:
    return str(piece["source"]["name"]), int(piece["start_ms"])      # recording names carry their start time


Build = Callable[[Path, str, Adapters, dict[str, Any]], dict[str, Any]]


def day_queue(task: Path, date: str, day: dict[str, Any], *, final: bool, build: Build,
              adapters: Adapters) -> dict[str, Any] | None:
    """The day's queue record (also kept as `day[QUEUE_KEY]`), after making what can be made; None when the day has no
    queued clip.  `final`: the day's selection will not seat more clips (discover: COMPLETE and settled).
    `build(task, date, adapters, lead)` makes one compilation from a lead piece carrying `topic_members` (discover passes
    `_build_joined` with the day's session) and returns its built record."""
    states = {str(b["cid"]): ClipState.load(Path(b["state"])) for b in day.get("built", []) if Path(b["state"]).exists()}
    record = dict(day.get(QUEUE_KEY) or {"made": []})
    queued = {cid: st for cid, st in states.items() if st.data["clip"].get("status") != SUPERSEDED
              and st.step("boundary").get("status") == "STOPPED" and st.step("boundary").get("reason") == gates.BOUNDARY_SHORT_QUEUED}
    if not queued and not record["made"]:
        return None
    # a clip held back by a person (upload/HOLD, auto_run.pending) is not run on: it does not keep the others waiting
    unsettled = sorted(cid for cid, st in states.items() if st.data["clip"].get("status") != SUPERSEDED
                       and not (task / "clips" / cid / HOLD_FILE).is_file() and (
                           st.step("boundary").get("status") not in ("DONE", "STOPPED")
                           or st.step("boundary").get("reason") == gates.BOUNDARY_NOT_ACCEPTED
                           or provider_failed(st, "boundary")))
    asset = core_draws.load()
    pieces = sorted((_piece(task, cid, st, asset) for cid, st in queued.items()), key=_order)
    record.update(pieces=[{"cid": p["cid"], "theme": p["theme"], "draws": p["draws"], "segment": p["source"]["name"],
                           "start_ms": p["start_ms"], "end_ms": p["end_ms"], "hook": str(p.get("hook", ""))[:80]}
                          for p in pieces],
                  failures=[], waiting=[])
    if not final or unsettled:   # a short clip of the same draw may still come: nothing is grouped before the day is known
        record["status"] = "WAITING_FOR_BOUNDARY"
        why = ("当天选片还没定局（录像可能还在录）" if not final else f"当天还有 {len(unsettled)} 条片没定边界")
        record["waiting"] = [{"cid": p["cid"], "why": why} for p in pieces]
        day[QUEUE_KEY] = record
        return record
    names = {d["key"]: d["name"] for d in (asset or {}).get("draws", [])}
    for theme in dict.fromkeys(p["theme"] for p in pieces):
        mine = [p for p in pieces if p["theme"] == theme]
        if theme is None:
            record["waiting"] += [
                {"cid": p["cid"], "why": f"{NOT_A_THEME}：必切看点「{'、'.join(names.get(k, k) for k in p['draws'])}」不是题材，"
                                         f"同晚几段未必是同一件事，不自动成组" if p["draws"]
                 else f"{NO_THEME}：没有必切看点（只有频道主标记），不自动成组"} for p in mine]
            continue
        made, waiting = chunks(mine)
        record["waiting"] += [{"cid": p["cid"], "why": why} for p, why in waiting]
        for run in made:
            members = [dict(p) for p in run]
            total = sum(int(p["end_ms"]) - int(p["start_ms"]) for p in run) / 1000
            lead = dict(members[0], topic_members=members, topic_theme=names.get(theme, theme),
                        topic_why=f"当晚 {len(run)} 段必切的「{names.get(theme, theme)}」各自不足 {gates.CLIP_MIN_SECONDS:.0f} 秒"
                                  f"（合计 {total:.1f}s），按直播先后剪成一条合集")
            try:
                built = build(task, date, adapters, lead)
            except Exception as exc:  # noqa: BLE001 - one compilation's failure must not stop the others; the next round retries
                record["failures"].append({"members": [p["cid"] for p in run], "code": type(exc).__name__, "detail": str(exc)[:300]})
                continue
            for p in run:
                supersede_candidate(task_dir=task, cid=p["cid"], by=built["cid"], authority=AUTHORITY, quote=QUOTE,
                                    reason=f"不足 {gates.CLIP_MIN_SECONDS:.0f} 秒的必切片，并进合集 {built['cid']}")
            record["made"].append({"cid": built["cid"], "theme": theme, "members": [p["cid"] for p in run],
                                   "duration_ms": built.get("duration_ms"), "state": built["state"]})
    record["status"] = "WAITING" if record["waiting"] or record["failures"] else "DONE"
    day[QUEUE_KEY] = record
    return record


def lines(day: Mapping[str, Any]) -> list[str]:
    """The day's queue in a few lines (discover.ledger_lines)."""
    record = day.get(QUEUE_KEY)
    if not record:
        return []
    out = [f"    不足 {gates.CLIP_MIN_SECONDS:.0f} 秒的必切片：{record['status']}"]
    out += [f"      合集 {m['cid']}（{m['theme']}，{len(m['members'])} 段"
            + (f"，{m['duration_ms'] / 1000:.1f}s" if m.get("duration_ms") else "") + f"）：{'、'.join(m['members'])}"
            for m in record["made"]]
    out += [f"      排队 {w['cid']}：{w['why']}" for w in record["waiting"]]
    out += [f"      成片失败 {'、'.join(f['members'])}：{f['code']} {f['detail'][:80]}" for f in record["failures"]]
    return out
