"""Short must-cut clips (step 1 stopped them BOUNDARY_SHORT_QUEUED): grouped by a draw that is a theme (core_draws
`compiles`) once the day is final and its cuts are known, made into one compilation in broadcast order when two or more
add up to more than the minimum, otherwise left queued."""

from __future__ import annotations

import json
from pathlib import Path

from fakes import SESSION

from src.autoslice.manual_lane import auto_run, core_draws, short_queue
from src.autoslice.manual_lane.state import ClipState


def _clip(task: Path, cid: str, *, segment: str, start_ms: int, end_ms: int, draws=("sajiao",), boundary="QUEUED",
          mark=False, label=None):
    """A built clip of the day: its selection entry as discover_select seats it (a mark carries no `must_cut` field), its
    state and, when queued, the cut step 1 kept.  The labels are a party's matter between persons (collab is must-cut)."""
    sel = task / "entries" / f"{cid}.json"
    sel.parent.mkdir(parents=True, exist_ok=True)
    label = label or {"version": core_draws.load()["version"], "draws": list(draws), "why": "", "story": "person",
                      "role": "party"}
    entry = {"cid": cid, "candidate_id": cid, "source": {"name": segment, "path": f"/rec/{segment}.flv", "directory": "/d"},
             "start_ms": start_ms - 5000, "end_ms": end_ms + 5000, "cue_ids": ["1", "9"], "hook": f"{cid} 的钩子",
             "core_draws": label, **({"seat_reason": "OWNER_MARK", "owner_mark": True} if mark
                                     else {"seat_reason": "MUST_CUT", "must_cut": list(draws)})}
    sel.write_text(json.dumps({"input_pool": [entry]}, ensure_ascii=False), encoding="utf-8")
    clip = {"cid": cid, "recording_date": "2026-11-25", "source_name": f"{segment}.flv", "start_ms": start_ms,
            "end_ms": end_ms, "start_cue": 1, "end_cue": 9, "hook": "h", "mac_bcut_srt": "/x", "remote_dir": "/r",
            "selection_file": str(sel)}
    state = ClipState.create(task / "state" / f"{cid}.json", clip=clip, session=SESSION)
    if boundary == "QUEUED":
        state.data["steps"]["boundary"] = {"status": "STOPPED", "reason": "BOUNDARY_SHORT_QUEUED", "cpa": []}
        decision = task / "clips" / cid / "boundary" / "decision.json"
        decision.parent.mkdir(parents=True, exist_ok=True)
        decision.write_text(json.dumps({"adopted": {"start_cue": 3, "end_cue": 7, "start_ms": start_ms, "end_ms": end_ms}}),
                            encoding="utf-8")
    elif boundary == "DONE":
        state.data["steps"]["boundary"] = {"status": "DONE", "reason": "", "cpa": []}
    elif boundary == "NOT_ACCEPTED":
        state.data["steps"]["boundary"] = {
            "status": "STOPPED",
            "reason": "BOUNDARY_NOT_ACCEPTED",
            "cpa": [{"call_id": "boundary", "rc": 0}, {"call_id": "boundary-retry-1", "rc": 0}],
        }
    elif boundary == "HELD":
        (task / "clips" / cid / auto_run.HOLD_FILE).parent.mkdir(parents=True, exist_ok=True)
        (task / "clips" / cid / auto_run.HOLD_FILE).write_text("held by hand", encoding="utf-8")
    state.save()
    return {"cid": cid, "state": str(state.path)}


def _queue(task, day, build, *, final=True):
    return short_queue.day_queue(task, "2026-11-25", day, final=final, build=build, adapters=None)


class Builder:
    def __init__(self):
        self.leads = []

    def __call__(self, task, date, adapters, lead):
        self.leads.append(lead)
        cid = f"{lead['cid']}_m"
        ClipState.create(task / "state" / f"{cid}.json", clip={"cid": cid}, session=SESSION).save()
        return {"cid": cid, "state": str(task / "state" / f"{cid}.json"), "duration_ms": 70_000}


def test_two_short_pieces_of_one_draw_become_one_compilation_in_broadcast_order(tmp_path):
    day = {"built": [_clip(tmp_path, "late", segment="r_22-00", start_ms=100_000, end_ms=140_000),
                     _clip(tmp_path, "early", segment="r_21-30", start_ms=500_000, end_ms=535_000),
                     _clip(tmp_path, "full", segment="r_21-30", start_ms=0, end_ms=90_000, boundary="DONE")]}
    build = Builder()
    record = _queue(tmp_path, day, build)
    assert [m["cid"] for m in build.leads[0]["topic_members"]] == ["early", "late"]
    assert build.leads[0]["topic_members"][0]["cue_ids"] == ["3", "7"]           # the cut step 1 decided, not the proposal
    assert record["status"] == "DONE" and record["made"][0]["members"] == ["early", "late"]
    for cid in ("early", "late"):
        clip = ClipState.load(tmp_path / "state" / f"{cid}.json").data["clip"]
        assert clip["status"] == "SUPERSEDED" and clip["superseded_by"] == "early_m"
    again = _queue(tmp_path, day, build)
    assert len(build.leads) == 1 and again["made"] == record["made"]            # made once


def test_nothing_is_grouped_while_a_clip_of_the_day_has_no_boundary_yet(tmp_path):
    day = {"built": [_clip(tmp_path, "a", segment="r_21", start_ms=0, end_ms=40_000),
                     _clip(tmp_path, "b", segment="r_22", start_ms=0, end_ms=40_000),
                     _clip(tmp_path, "c", segment="r_22", start_ms=90_000, end_ms=200_000, boundary=None)]}
    build = Builder()
    record = _queue(tmp_path, day, build)
    assert record["status"] == "WAITING_FOR_BOUNDARY" and not build.leads


def test_nothing_is_grouped_before_the_days_selection_is_final(tmp_path):
    # a named day (discover --dates) while the stream is still on: two short pieces are known, a third may come
    day = {"built": [_clip(tmp_path, "a", segment="r_20", start_ms=0, end_ms=40_000),
                     _clip(tmp_path, "b", segment="r_20-30", start_ms=0, end_ms=40_000)]}
    build = Builder()
    record = _queue(tmp_path, day, build, final=False)
    assert record["status"] == "WAITING_FOR_BOUNDARY" and not build.leads
    assert "还没定局" in record["waiting"][0]["why"]


def test_a_clip_held_by_hand_before_step_1_does_not_keep_the_others_waiting(tmp_path):
    day = {"built": [_clip(tmp_path, "a", segment="r_21", start_ms=0, end_ms=40_000),
                     _clip(tmp_path, "b", segment="r_22", start_ms=0, end_ms=40_000),
                     _clip(tmp_path, "h", segment="r_22", start_ms=90_000, end_ms=200_000, boundary="HELD")]}
    build = Builder()
    assert _queue(tmp_path, day, build)["status"] == "DONE" and len(build.leads) == 1


def test_an_owner_marked_clip_is_grouped_by_its_draws_like_a_must_cut_one(tmp_path):
    # the mark is seated OWNER_MARK with no `must_cut` field; its labels say 撒娇: it joins the evening's other 撒娇 piece
    day = {"built": [_clip(tmp_path, "marked", segment="r_20", start_ms=0, end_ms=40_000, mark=True),
                     _clip(tmp_path, "cut", segment="r_21", start_ms=0, end_ms=35_000)]}
    build = Builder()
    record = _queue(tmp_path, day, build)
    assert record["made"][0]["members"] == ["marked", "cut"] and record["made"][0]["theme"] == "sajiao"


def test_a_pair_too_short_collab_pieces_and_a_mark_without_a_draw_stay_queued_with_why(tmp_path):
    # three short collab pieces of one evening are not one subject (a teammate's play, dinner, goodbyes): not compiled
    day = {"built": [_clip(tmp_path, "s1", segment="r_21", start_ms=0, end_ms=25_000),
                     _clip(tmp_path, "s2", segment="r_22", start_ms=0, end_ms=30_000),
                     *(_clip(tmp_path, f"c{i}", segment=f"r_2{i}", start_ms=60_000, end_ms=110_000, draws=("collab",))
                       for i in (1, 2, 3)),
                     _clip(tmp_path, "m1", segment="r_22", start_ms=60_000, end_ms=100_000, draws=(), mark=True)]}
    build = Builder()
    record = _queue(tmp_path, day, build)
    assert not build.leads and record["status"] == "WAITING"
    why = {w["cid"]: w["why"].split("：")[0] for w in record["waiting"]}
    assert why == {"s1": "TOO_SHORT", "s2": "TOO_SHORT", "c1": "NOT_A_THEME", "c2": "NOT_A_THEME", "c3": "NOT_A_THEME",
                   "m1": "NO_THEME"}
    assert any("排队 c1" in line and "联动同场" in line for line in short_queue.lines(day))


def test_a_must_cut_seat_is_grouped_by_the_draws_it_was_seated_for(tmp_path):
    # 11-23: seated MUST_CUT ["collab"] under the 11-17 labels (no story/role); read again under today's asset they are
    # not must-cut at all.  The seat says why it is a must-cut clip: collab, not a theme -> NOT_A_THEME, not NO_THEME
    old = {"version": "2026-11-17", "draws": ["collab"], "why": ""}
    day = {"built": [_clip(tmp_path, f"c{i}", segment=f"r_2{i}", start_ms=0, end_ms=45_000, draws=("collab",), label=old)
                     for i in (1, 2, 3)]}
    record = _queue(tmp_path, day, Builder())
    assert {w["why"].split("：")[0] for w in record["waiting"]} == {"NOT_A_THEME"}
    assert record["pieces"][0]["draws"] == ["collab"]


def test_a_theme_piece_alone_stays_queued(tmp_path):
    day = {"built": [_clip(tmp_path, "s1", segment="r_21", start_ms=0, end_ms=45_000)]}
    record = _queue(tmp_path, day, Builder())
    assert record["waiting"][0]["why"].startswith("ALONE")


def test_a_day_without_short_clips_has_no_queue(tmp_path):
    day = {"built": [_clip(tmp_path, "full", segment="r", start_ms=0, end_ms=90_000, boundary="DONE")]}
    assert _queue(tmp_path, day, Builder()) is None
    assert "short_queue" not in day


def test_a_queued_clip_is_not_run_again_by_the_round(tmp_path):
    _clip(tmp_path, "q", segment="r", start_ms=0, end_ms=40_000)
    run, stuck = auto_run.pending(tmp_path, {"clips": {}}, upload=False, version="v")
    assert run == [] and stuck == []


def test_short_queue_waits_while_a_clip_stopped_with_boundary_not_accepted(tmp_path):
    day = {"built": [_clip(tmp_path, "a", segment="r_21", start_ms=0, end_ms=40_000),
                     _clip(tmp_path, "b", segment="r_22", start_ms=0, end_ms=40_000),
                     _clip(tmp_path, "c", segment="r_22", start_ms=90_000, end_ms=200_000, boundary="NOT_ACCEPTED")]}
    build = Builder()
    record = _queue(tmp_path, day, build)
    assert record["status"] == "WAITING_FOR_BOUNDARY" and not build.leads and not record["made"]
    assert ClipState.load(tmp_path / "state" / "a.json").data["clip"].get("status") != "SUPERSEDED"
