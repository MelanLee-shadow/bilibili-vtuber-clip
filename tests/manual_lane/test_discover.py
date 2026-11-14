"""`discover`: the recordings of a day become candidate clips (inventory -> subtitles -> selection -> clip states)."""
import json
import types

import pytest

from fakes import FakeDanmaku, FakeRemote
from src.autoslice.manual_lane import discover as dv, discover_media as dm, discover_select as ds
from src.autoslice.manual_lane.adapters import Adapters, RemoteResult

DATE = "2026-11-09"
ARCHIVE = dm.archive_dir(DATE)
# a real scorecard of the trial (texts shortened); raw = sum(dimension * weight) / 4
CARD = json.loads('{"schema_version": "lidousha-selection-scorecard.v1", "status": "VALID", "tier": 1, "requested_tier": 1, '
                  '"tier_basis": "generic_event", "tier_reason": "窗内完整交代起因、误会和她的辩解。", '
                  '"tier_evidence_cues": [176, 183, 186, 190, 242, 246, 248], "dimensions": {"lidousha_centrality": 3, '
                  '"stance_intensity": 2, "audience_salience": 2, "relationship_interaction": 3, "persona_reversal": 3, '
                  '"comedic_payoff": 3, "self_contained": 4}, "weights": {"lidousha_centrality": 25, "stance_intensity": 20, '
                  '"audience_salience": 15, "relationship_interaction": 15, "persona_reversal": 10, "comedic_payoff": 10, '
                  '"self_contained": 5}, "raw_score": 67.5, "uncertainty_penalty": 0.0, "fatigue_penalty": 0.0, '
                  '"effective_score": 67.5, "reason_codes": [], "candidate_quality": {"schema_version": '
                  '"candidate-selection-quality.v1", "watchability": "STRONG", "differentiation": "DISTINCTIVE", '
                  '"production_disposition": "PRODUCTION", "reason": "面向路人也能理解的故事，主事件成立。", '
                  '"evidence_cues": [176, 179, 181, 183]}, "admission_policy": "selection-scorecard-admission.v4", '
                  '"start_cue": 2, "end_cue": 4}')


def card(level=3):
    """A valid scorecard whose score moves with one dimension (level 0-4; this one keeps the card admitted at every level)."""
    doc = json.loads(json.dumps(CARD))
    doc["dimensions"]["persona_reversal"] = level
    doc["raw_score"] = doc["effective_score"] = sum(doc["dimensions"][k] * w for k, w in doc["weights"].items()) / 4
    return doc


def entry(cid, segment, start, end, *, confidence=0.9, kind="talk", level=3):
    return {"cid": cid, "candidate_id": cid, "kind": kind, "start_ms": start, "end_ms": end, "confidence": confidence,
            "hook": cid, "selection_scorecard": card(level), "source": {"name": segment, "path": f"{ARCHIVE}/{segment}.flv"}}


def test_clip_id_carries_the_recording_start_and_fits_the_package_slug():
    assert ds.lane_cid("semantictalk_544690_710640", "22966160_20261109-21-49-17") == "semantictalk_544690_710640_11092149"
    with pytest.raises(dm.DiscoverError, match="SEGMENT_NAME_UNRECOGNISED"):
        ds.lane_cid("semantictalk_1_2", "recording")
    with pytest.raises(dm.DiscoverError, match="CLIP_ID_UNUSABLE"):
        ds.lane_cid("semantictalk_" + "9" * 30, "22966160_20261109-21-49-17")


def test_seats_go_by_rank_then_the_soft_limit_per_recording_is_refilled():
    pool = [entry(f"a{n}", "segA", n * 1000, n * 1000 + 500, level=4) for n in range(4)]        # four strong ones in one recording
    pool += [entry("b0", "segB", 0, 500, level=2), entry("b1", "segB", 1000, 1500, level=1)]
    seating = ds.seat_day(DATE, pool, built=[])
    assert seating.policy["cap"] == 5 and seating.policy["free_seats"] == 5
    # two of segA first (soft limit), then segB's two, then the slack goes back to segA's third
    assert [e["cid"] for e in seating.seated] == ["a0", "a1", "b0", "b1", "a2"]
    assert [(r["cid"], r["reason"]) for r in seating.rejected] == [("a3", ds.QUOTA_FULL)]


def test_candidates_that_cannot_be_seated_keep_the_reason():
    pool = [entry("song", "segA", 0, 500, kind="song"), entry("unsure", "segA", 1000, 1500, confidence=0.7),
            entry("top", "segA", 2000, 3000, level=4), entry("inside", "segA", 2500, 2800, level=3)]
    held = entry("held", "segB", 0, 500)
    held["selection_scorecard"]["candidate_quality"]["production_disposition"] = "RESERVE"
    seating = ds.seat_day(DATE, [*pool, held], built=[])
    assert [e["cid"] for e in seating.seated] == ["top"]
    assert {r["cid"]: r["reason"] for r in seating.rejected} == {
        "song": ds.NOT_TALK, "unsure": ds.BELOW_MIN_CONFIDENCE, "inside": ds.OVERLAPS_SEATED, "held": ds.ADMISSION_HOLD}


def test_clips_already_built_keep_their_seats_and_are_not_seated_again():
    built = [{"cid": f"old{n}", "segment": "segA", "start_ms": n * 1000, "end_ms": n * 1000 + 500} for n in range(4)]
    pool = [entry("old0", "segA", 0, 500), entry("new1", "segB", 0, 500, level=4), entry("new2", "segB", 1000, 1500),
            entry("clash", "segA", 1200, 1400, level=4)]
    seating = ds.seat_day(DATE, pool, built)
    assert seating.policy["free_seats"] == 1 and [e["cid"] for e in seating.seated] == ["new1"]
    assert {r["cid"]: r["reason"] for r in seating.rejected} == {"clash": ds.OVERLAPS_SEATED, "new2": ds.QUOTA_FULL}


# ----- a whole day through a fake media host ---------------------------------------------------------------------

SRT = "".join(f"{n}\n00:00:{n:02d},000 --> 00:00:{n + 1:02d},000\n第{n}句\n\n" for n in range(1, 7))
SEGMENTS = {"22966160_20261109-21-00-51": 90_000,          # the empty shell of the stream's first moment (under stub_max_bytes)
            "22966160_20261109-21-00-52": 700_000_000,
            "22966160_20261109-21-30-00": 600_000_000,
            "22966160_20261109-23-59-05": 1_282_220}        # a second of silence after the stream: recognised, nothing said


class Host:
    """What the media host answers: the archive listing, the audio, the recognition, the danmaku copy."""

    def __init__(self, now=2_000_000_000.0, written=1_000_000_000.0, arrived=None):
        self.now, self.written, self.asr_calls, self.failing = now, written, [], set()
        self.arrived = set(SEGMENTS) if arrived is None else set(arrived)        # the recordings in the archive; danmaku always is
        self.copy_rc, self.bodies, self.listing = 0, {}, None

    def __call__(self, name, body, remote):
        self.bodies[name] = body
        if name == "disc-direct-copy":                      # the recorder's newest days, copied straight to the host
            return RemoteResult(self.copy_rc, "DIRECT_COPY_OK\n" if self.copy_rc == 0 else "", "ssh: no route" if self.copy_rc else "")
        if name.startswith("disc-dates"):
            if self.listing is not None:                    # a host with no recordings root, or no directory for the room yet
                return RemoteResult(0, f"NOW {self.now}\n{self.listing}\n", "")
            return RemoteResult(0, f"NOW {self.now}\nD\t2026-11-07\nD\t{DATE}\n", "")       # 11-07: made by hand, before auto_not_before
        if name.startswith("disc-inv-"):
            rows = "".join((f"F\t{ARCHIVE}/{seg}.flv\t{size}\t{self.written}\n" if seg in self.arrived else "")
                           + f"F\t{ARCHIVE}/{seg}.xml\t10\t{self.written}\n" for seg, size in SEGMENTS.items())
            return RemoteResult(0, f"NOW {self.now}\nDIR\n{rows}", "")
        segment = name.split("-", 2)[2]
        rd = f"{dm.layout().session_root(DATE)}/discover/{segment}"
        if name.startswith("disc-audio-"):
            return RemoteResult(0, "AUDIO_OK\n", "")
        if name.startswith("disc-asr-"):
            self.asr_calls.append(segment)
            if segment in self.failing:
                return RemoteResult(1, "FREE_ASR bcut FAILED\nASR_MISSING\n", "")
            if segment.endswith("23-59-05"):
                return RemoteResult(0, "FREE_ASR bcut OK\nASR_MISSING\n", "")
            remote.files[f"{rd}/asr/bcut.srt"], remote.files[f"{rd}/asr/bcut.json"] = SRT.encode(), b"{}"
            return RemoteResult(0, "FREE_ASR bcut OK\nASR_OK\n", "")
        if name.startswith("disc-xml-"):
            remote.files[f"{rd}/source.xml"] = b"<i></i>"
            return RemoteResult(0, "", "")
        raise AssertionError(name)


def selector(cues, *, llm_call, max_candidates, danmaku_hints, danmaku_xml):
    """Two talk candidates per recording (cues 2-4 and 5-6), the first the better one."""
    def candidate(cid, first, last):
        mine = [types.SimpleNamespace(cue_id=c.cue_id, text=c.text) for c in cues if first <= int(c.cue_id) <= last]
        return types.SimpleNamespace(anchor=types.SimpleNamespace(candidate_id=cid), content_type_hint="talk", cues=mine,
                                     boundary=types.SimpleNamespace(resolved_start_ms=first * 1000, resolved_end_ms=(last + 1) * 1000,
                                                                    start_boundary_score=0.9))
    found = [candidate("semantictalk_2000_5000", 2, 4), candidate("semantictalk_5000_7000", 5, 6)]
    cards = {"semantictalk_2000_5000": dict(card(4), start_cue=2, end_cue=4), "semantictalk_5000_7000": dict(card(2), start_cue=5, end_cue=6)}
    return found, {"hooks": {c.anchor.candidate_id: "钩子" for c in found}, "scorecards": cards}


def run(tmp_path, host, **kwargs):
    adapters = Adapters(remote=FakeRemote(on_script=host), cpa=None, danmaku=FakeDanmaku())
    return dv.discover(task=tmp_path / "task", adapters=adapters, selector=selector, **kwargs)


def test_a_day_of_recordings_becomes_clip_states_and_a_ledger(tmp_path):
    host = Host()
    report = run(tmp_path, host, dates=[DATE])
    assert report.exit_code == 0 and not report.failures
    day = dv.load_ledger(tmp_path / "task")["days"][DATE]
    assert {s["name"][-8:]: s["status"] for s in day["segments"]} == {
        "21-00-51": "STUB_SKIPPED", "21-00-52": "SELECTED", "21-30-00": "SELECTED", "23-59-05": "NO_SPEECH"}
    assert day["status"] == dv.COMPLETE                     # a recording nobody speaks in is finished, not a failure
    assert sorted(day["selection"]["seated"]) == [
        "semantictalk_2000_5000_11092100", "semantictalk_2000_5000_11092130",
        "semantictalk_5000_7000_11092100", "semantictalk_5000_7000_11092130"]
    state = json.loads((tmp_path / "task/state/semantictalk_2000_5000_11092130.json").read_text())
    clip = state["clip"]
    assert (clip["source_name"], clip["start_ms"], clip["end_ms"], clip["start_cue"], clip["end_cue"]) == (
        "22966160_20261109-21-30-00.flv", 2000, 5000, 2, 4)
    assert clip["recording_date"] == DATE and clip["source_dir"] == ARCHIVE and clip["hook"] == "钩子"
    assert state["session"]["speaker_mode"] == "uniform_host" and not state["steps"]
    assert "已选片 2" in dv.ledger_lines(tmp_path / "task")[0] and "无人声 1" in dv.ledger_lines(tmp_path / "task")[0]


def test_running_again_recognises_nothing_twice_and_builds_nothing_twice(tmp_path):
    host = Host()
    run(tmp_path, host, dates=[DATE])
    states = sorted((tmp_path / "task/state").iterdir())
    stamps = [p.stat().st_mtime_ns for p in states]
    spoken = [s for s in host.asr_calls if not s.endswith("23-59-05")]
    report = run(tmp_path, host, dates=[DATE])
    assert [s for s in host.asr_calls if not s.endswith("23-59-05")] == spoken          # kept subtitles are not made again
    assert sorted((tmp_path / "task/state").iterdir()) == states and [p.stat().st_mtime_ns for p in states] == stamps
    assert report.days[0]["built"] == 4 and report.days[0]["seated"] == 0


def test_a_failed_recording_leaves_the_day_partial_and_the_others_go_on(tmp_path):
    host = Host()
    host.failing.add("22966160_20261109-21-30-00")
    report = run(tmp_path, host, dates=[DATE])
    assert report.exit_code == 0 and [f["code"] for f in report.failures] == ["ASR_MISSING_SOURCE"]
    day = dv.load_ledger(tmp_path / "task")["days"][DATE]
    assert day["status"] == dv.PARTIAL and len(day["built"]) == 2
    host.failing.clear()
    assert run(tmp_path, host, dates=[DATE]).days[0]["status"] == dv.COMPLETE and len(list((tmp_path / "task/state").iterdir())) == 4


def test_a_room_never_recorded_is_said_and_a_wrong_recordings_root_stops(tmp_path):
    never = Host()
    never.listing = "NO_ROOM_DIR"                           # the root is there, the room has no directory yet
    report = run(tmp_path, never)
    assert [d["reason"] for d in report.deferred] == ["NO_RECORDINGS_YET"] and "does not exist yet" in report.deferred[0]["detail"]
    assert not report.days and not report.failures
    wrong = Host()
    wrong.listing = "NO_ARCHIVE_ROOT"                       # a mistyped archive_root must not look like a quiet room
    with pytest.raises(dv.DiscoverError, match="ARCHIVE_ROOT_MISSING"):
        run(tmp_path, wrong)


def test_without_a_date_a_day_that_may_still_be_recording_is_left_alone(tmp_path):
    recording = Host(now=1_000_000_600.0, written=1_000_000_000.0)       # last written ten minutes ago
    report = run(tmp_path, recording)
    assert report.deferred == [{"date": DATE, "reason": "RECORDING_MAY_BE_IN_PROGRESS"}] and not report.days
    report = run(tmp_path, Host())                                       # long finished: taken up without being named
    assert [d["date"] for d in report.days] == [DATE] and report.days[0]["status"] == dv.COMPLETE      # not the earlier day
    assert not run(tmp_path, Host()).days                                # and a finished day is not looked at again


def test_a_day_whose_danmaku_came_before_its_recordings_waits_for_them_and_a_late_recording_reopens_it(tmp_path):
    shell = [next(iter(SEGMENTS))]                                       # only the empty shell has arrived; the danmaku of all four has
    hours = 3600.0
    report = run(tmp_path, Host(now=1_000_000_000.0 + 5 * hours, arrived=shell))
    assert report.deferred == [{"date": DATE, "reason": "RECORDING_MAY_BE_IN_PROGRESS"}] and not report.days
    report = run(tmp_path, Host(now=1_000_000_000.0 + 30 * hours, arrived=shell))      # a day later they are not coming
    assert [d["status"] for d in report.days] == [dv.NO_CONTENT]
    assert not run(tmp_path, Host(now=1_000_000_000.0 + 31 * hours, arrived=shell)).days
    report = run(tmp_path, Host(now=1_000_000_000.0 + 32 * hours))                     # they came after all
    assert [d["status"] for d in report.days] == [dv.COMPLETE] and len(list((tmp_path / "task/state").iterdir())) == 4



def test_the_recorders_own_copy_is_fetched_first_and_read_before_the_archive(tmp_path):
    host = Host()
    report = run(tmp_path, host)
    assert [d["status"] for d in report.days] == [dv.COMPLETE] and not report.failures
    direct = dm.direct_room()
    copy, listing, days = host.bodies["disc-direct-copy"], host.bodies[f"disc-inv-{DATE}"], host.bodies["disc-dates"]
    assert "rsync -a --partial" in copy and f"{dm.layout().recordings_direct_host}:{dm.layout().room_id}/" in copy
    assert copy.index('!= host ]; then continue') < copy.index("rsync -a --partial") < copy.index('echo host > "$mark"')   # one source a day
    assert listing.index(f"{direct}/{DATE}") < listing.index(f"{ARCHIVE}") and "elif [ -d" in listing      # the copy, else the archive
    assert direct in days and dm.archive_room() in days                                             # days of either place count
    assert DATE not in copy.split("for skip in", 1)[1].split(";", 1)[0]       # the first round: the day is not completed yet
    again = Host()
    assert not run(tmp_path, again).days                                       # completed: not looked at again ...
    assert DATE in again.bodies["disc-direct-copy"].split("for skip in", 1)[1].split(";", 1)[0]     # ... and not copied again
    assert dm.rules("discover")["auto_not_before"] in again.bodies["disc-direct-copy"]            # nor a day made by hand


def test_a_day_is_the_same_wherever_its_recordings_are_read_from():
    seg = dm.Segment(date=DATE, path="/direct/room/2026-11-09/a_20261109-21-00-52.flv", size=700, mtime="5.0",
                     xml_size=None, xml_mtime=None, stub=False)
    inv = dm.DayInventory(date=DATE, now=10.0, exists=True, segments=(seg,))
    assert inv.same_recordings([{"path": "/archive/room/2026-11-09/a_20261109-21-00-52.flv", "size": 700, "mtime": "9.0"}])
    assert not inv.same_recordings([{"path": "/archive/room/2026-11-09/a_20261109-21-00-52.flv", "size": 701, "mtime": "5.0"}])
    assert not inv.same_recordings([])


def test_a_failed_copy_is_said_and_the_archive_is_still_read(tmp_path):
    host = Host()
    host.copy_rc = 22
    report = run(tmp_path, host)
    assert [d["status"] for d in report.days] == [dv.COMPLETE]
    assert [f["code"] for f in report.failures] == ["DIRECT_COPY_FAILED"] and report.exit_code == 0
