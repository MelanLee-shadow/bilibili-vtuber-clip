"""`auto`: one unattended round - discover, run every unfinished clip, leave a clip that keeps stopping at one place."""
import json

from fakes import SESSION
from src.autoslice.manual_lane import auto_run as ar
from src.autoslice.manual_lane.state import ClipState


def clip(task, cid, steps=None, status=None):
    info = {"cid": cid, "recording_date": "2026-11-09", "source_name": "s.flv", "start_ms": 0, "end_ms": 1000,
            "start_cue": 1, "end_cue": 2, "hook": "", "mac_bcut_srt": "x.srt", "remote_dir": f"/r/{cid}"}
    if status:
        info["status"] = status
    state = ClipState.create(task / "state" / f"{cid}.json", clip=info, session=SESSION)
    state.data["steps"] = steps or {}
    state.save()
    return state


def stop(task, cid, step, reason, cpa=None):
    state = ClipState.load(task / "state" / f"{cid}.json")
    state.data["steps"][step] = {"status": "STOPPED", "reason": reason, **({"cpa": cpa} if cpa else {})}
    state.save()


def finish(task, cid, last="review"):
    state = ClipState.load(task / "state" / f"{cid}.json")
    state.data["steps"] = {last: {"status": "DONE"}}
    state.save()


def round_(task, on_run, *, upload=False, found=None):
    calls = []

    def run_clips(cids):
        calls.append(list(cids))
        for cid in cids:
            on_run(cid)
        return 0
    result = ar.auto_round(task, discover_days=lambda: found or {"days": []}, run_clips=run_clips, upload=upload)
    return result, calls


def test_a_round_runs_every_unfinished_clip_and_only_those(tmp_path):
    clip(tmp_path, "done", {"review": {"status": "DONE"}})
    clip(tmp_path, "fresh")
    clip(tmp_path, "halfway", {"cut": {"status": "DONE"}})
    clip(tmp_path, "replaced", status="SUPERSEDED")
    result, calls = round_(tmp_path, lambda cid: finish(tmp_path, cid), found={"days": [{"date": "2026-11-09"}]})
    assert calls == [["fresh", "halfway"]] and result["finished"] == ["fresh", "halfway"] and result["stuck"] == []
    assert result["discover"] == {"days": [{"date": "2026-11-09"}]}
    assert round_(tmp_path, lambda cid: None)[1] == []                 # nothing left: the next round runs nothing


def test_with_the_upload_a_reviewed_clip_is_not_finished_until_it_is_online(tmp_path):
    clip(tmp_path, "reviewed", {"review": {"status": "DONE"}})
    assert round_(tmp_path, lambda cid: None)[1] == []
    result, calls = round_(tmp_path, lambda cid: finish(tmp_path, cid, "upload"), upload=True)
    assert calls == [["reviewed"]] and result["finished"] == ["reviewed"]


def test_a_reviewed_clip_someone_kept_back_is_not_run_by_an_uploading_round(tmp_path):
    clip(tmp_path, "kept", {"review": {"status": "DONE"}})
    clip(tmp_path, "ready", {"review": {"status": "DONE"}})
    hold = tmp_path / "clips/kept/upload/HOLD"
    hold.parent.mkdir(parents=True)
    hold.write_text("kept back by the reviewer")
    result, calls = round_(tmp_path, lambda cid: finish(tmp_path, cid, "upload"), upload=True)
    assert calls == [["ready"]] and result["finished"] == ["ready"]
    hold.unlink()
    assert round_(tmp_path, lambda cid: finish(tmp_path, cid, "upload"), upload=True)[1] == [["kept"]]


def test_a_clip_that_stops_at_the_same_place_three_rounds_is_left_alone(tmp_path):
    clip(tmp_path, "bad")
    clip(tmp_path, "good")
    def run(cid):
        if cid == "bad":
            stop(tmp_path, cid, "subtitles", "ASR_MISSING_SOURCE")
        else:
            finish(tmp_path, cid)
    assert [round_(tmp_path, run)[1] for _ in range(3)] == [[["bad", "good"]], [["bad"]], [["bad"]]]
    result, calls = round_(tmp_path, run)
    assert calls == [] and result["stuck"] == [{"cid": "bad", "step": "subtitles", "reason": "ASR_MISSING_SOURCE", "times": 3}]
    ledger = json.loads(ar.ledger_path(tmp_path).read_text())
    assert ledger["clips"]["bad"]["runs"] == 3 and "stopped" not in ledger["clips"]["good"]
    stop(tmp_path, "bad", "subtitles", "ANOTHER_REASON")              # it stops somewhere else now (someone changed something)
    assert round_(tmp_path, lambda cid: None)[1] == [["bad"]]          # so it is run again, and the count starts over
    assert json.loads(ar.ledger_path(tmp_path).read_text())["clips"]["bad"]["stopped"]["times"] == 1


def test_a_clip_waiting_for_the_platform_is_polled_every_round(tmp_path):
    clip(tmp_path, "uploading", {"review": {"status": "DONE"}})
    def waits(cid):
        stop(tmp_path, cid, "upload", ar.WAITING_LIVE)
    assert [round_(tmp_path, waits, upload=True)[1] for _ in range(5)] == [[["uploading"]]] * 5
    assert "stopped" not in json.loads(ar.ledger_path(tmp_path).read_text())["clips"]["uploading"]


def test_a_clip_whose_model_gave_no_answer_is_asked_again_every_round_and_listed(tmp_path):
    clip(tmp_path, "unanswered")
    clip(tmp_path, "answered")
    def run(cid):          # the same step and reason every round; only one of them is the provider's doing
        calls = [{"call_id": "a", "rc": 0 if cid == "answered" else 1}, {"call_id": "b", "rc": 0}]
        stop(tmp_path, cid, "speaker", "SPEAKER_SCREEN_FAILED", cpa=calls)
    rounds = [round_(tmp_path, run) for _ in range(5)]
    assert [calls for _, calls in rounds] == [[["answered", "unanswered"]]] * 3 + [[["unanswered"]]] * 2
    assert all(result["waiting_provider"] == ["unanswered"] for result, _ in rounds)
    assert [s["cid"] for s in rounds[-1][0]["stuck"]] == ["answered"]
    assert "stopped" not in json.loads(ar.ledger_path(tmp_path).read_text())["clips"]["unanswered"]


def test_a_second_round_at_the_same_time_leaves_at_once(tmp_path):
    clip(tmp_path, "slow")
    inner = []
    def run(cid):
        inner.append(ar.auto_round(tmp_path, discover_days=lambda: {}, run_clips=lambda cids: 0, upload=False))
        finish(tmp_path, cid)
    result, _calls = round_(tmp_path, run)
    assert inner == [{"status": ar.BUSY}] and result["status"] == "ROUND_DONE"


def test_unattended_publishing_obeys_the_hosts_switch_and_never_publishes_on_a_guess():
    from fakes import FakeRemote
    from src.autoslice.manual_lane.adapters import RemoteResult

    def host(answer, rc=0):
        return FakeRemote(on_script=lambda name, body, remote: RemoteResult(rc, answer, ""))
    assert ar.publication_paused(host("PUBLISH_PAUSED\n"), "/opt/bilive/autoslice") is True
    assert ar.publication_paused(host("PUBLISH_OPEN\n"), "/opt/bilive/autoslice") is False
    assert ar.publication_paused(host("", rc=255), "/opt/bilive/autoslice") is True          # the host could not be asked
    assert ar.publication_paused(host("something else"), "/opt/bilive/autoslice") is True
    remote = host("PUBLISH_OPEN\n")
    ar.publication_paused(remote, "/opt/bilive/autoslice")
    assert "/opt/bilive/autoslice/PUBLISH_PAUSED" in remote.scripts[0][1]


def test_the_media_of_a_clip_that_is_online_is_reclaimed_and_nothing_else_is(tmp_path):
    import os
    import subprocess

    from fakes import FakeRemote
    from src.autoslice.manual_lane.adapters import RemoteResult

    work = tmp_path / "host/online"
    for cid, steps in (("online", {"upload": {"status": "DONE"}}), ("waiting", {"review": {"status": "DONE"}})):
        state = clip(tmp_path, cid, steps)
        state.data["clip"]["remote_dir"] = str(tmp_path / "host" / cid)
        state.save()
        for rel, size in (("fmr_source/source.mp4", 2_000_000), ("pkg/final.mp4", 2_000_000), ("pkg/final.srt", 50),
                          ("fmr/out/x/final.mp4", 2_000_000), ("pkg/cover.png", 2_000_000)):
            path = tmp_path / "clips" / cid / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"0" * size)
        for rel, size in (("cut/cut.mp4", 2_000_000), ("burn/a.wav", 2_000_000), ("subs/final.srt", 50), ("cut/small.mp4", 10)):
            path = tmp_path / "host" / cid / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"0" * size)
    online = tmp_path / "clips/online/upload/online.json"
    online.parent.mkdir(parents=True)
    online.write_text("{}")

    def host(name, body, remote):          # the media host runs the script for real, in this test's directory
        done = subprocess.run(["bash", "-c", body], capture_output=True, text=True)
        return RemoteResult(done.returncode, done.stdout, done.stderr)
    remote = FakeRemote(on_script=host)
    now = online.stat().st_mtime
    first = ar.auto_round(tmp_path, discover_days=lambda: {}, run_clips=lambda cids: 0, upload=False, remote=remote, now=now + 3600)
    assert first["reclaimed"] == [{"cid": "online", "what": "intermediates", "local_files": 1}]
    left = sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*") if p.is_file() and "online" in p.parts and p.suffix in (".mp4", ".wav"))
    assert left == ["clips/online/fmr/out/x/final.mp4", "clips/online/pkg/final.mp4", "host/online/cut/small.mp4"]    # the package's video stays for now
    assert (work / "subs/final.srt").exists() and (tmp_path / "clips/online/pkg/cover.png").exists()
    assert len(list((tmp_path / "host/waiting").rglob("*.mp4"))) == 2 and (tmp_path / "clips/waiting/fmr_source/source.mp4").exists()
    again = ar.auto_round(tmp_path, discover_days=lambda: {}, run_clips=lambda cids: 0, upload=False, remote=remote, now=now + 7200)
    assert again["reclaimed"] == [] and len(remote.scripts) == 1                      # noted in the ledger: asked once
    week = ar.auto_round(tmp_path, discover_days=lambda: {}, run_clips=lambda cids: 0, upload=False, remote=remote, now=now + 8 * 86400)
    assert week["reclaimed"] == [{"cid": "online", "what": "final", "local_files": 2}]
    assert not (tmp_path / "clips/online/pkg/final.mp4").exists() and (tmp_path / "clips/online/pkg/final.srt").exists()
    assert os.path.exists(tmp_path / "clips/waiting/pkg/final.mp4")


def test_a_stop_that_is_the_surroundings_doing_is_waited_out_too(tmp_path):
    # the first unattended round : a redeploy under the round, and a free recogniser's rate limit
    notes = {"redeployed": "RuntimeError: RUNTIME_VERSION_CHANGED_WITHIN_TASK\n",
             "limited": "FREE_ASR bcut OK\nFREE_ASR jianying RATE_LIMITED\nASR_MISSING\n", "broken": "ffmpeg: no such file"}
    for cid in notes:
        clip(tmp_path, cid)
    def run(cid):
        state = ClipState.load(tmp_path / "state" / f"{cid}.json")
        state.data["steps"]["burn"] = {"status": "STOPPED", "reason": "BURN_FAILED_RC13", "note": notes[cid]}
        state.save()
    rounds = [round_(tmp_path, run) for _ in range(5)]
    assert all(result["waiting_provider"] == ["limited", "redeployed"] for result, _ in rounds)
    assert [s["cid"] for s in rounds[-1][0]["stuck"]] == ["broken"]
    assert rounds[-1][1] == [["limited", "redeployed"]]
