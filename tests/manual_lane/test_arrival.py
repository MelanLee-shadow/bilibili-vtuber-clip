"""Step 4's arrival evidence (arrival.py, A12): who speaks in a voice collab from the order the same words reach two
streams.  the channel's request (of the clip published that day)."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

from fakes import FakeRemote, make_ctx

from src.autoslice.manual_lane import arrival, guest_identity
from src.autoslice.manual_lane import subtitle_track as st
from src.autoslice.manual_lane import speaker_v8 as sv
from src.autoslice.manual_lane import stage_impl as si
from src.autoslice.manual_lane.adapters import RemoteResult

TRIO = {"recording_date": "2026-11-23", "speaker_mode": "uniform_host", "enablement_evidence": None,
        "participants": [["李豆沙", "小李"], ["安晚awa"], ["礼墨Sumi"]], "own_uploader_mids": [1]}
# 443680 (published 11-24): v8 heard the host's 「就是死了就不会疼了。」 as a guest, and one line holds two people
RUNS = [{"start_ms": 0, "end_ms": 3000, "cluster": 2},       # the host
        {"start_ms": 3000, "end_ms": 4000, "cluster": 2},    # the host again: v8 says guest here
        {"start_ms": 4000, "end_ms": 5000, "cluster": 2},    # one line, the host then 礼墨Sumi
        {"start_ms": 5000, "end_ms": 8000, "cluster": 0},
        {"start_ms": 8000, "end_ms": 11000, "cluster": 0},   # 礼墨Sumi
        {"start_ms": 11000, "end_ms": 14500, "cluster": 1},  # 安晚awa
        {"start_ms": 14500, "end_ms": 16000, "cluster": 1}]  # the arrival says 安晚awa's path, v8 is sure it is the host
DOC = {"status": "OK", "centres_ms": [-471.1, -348.1, 0.8], "runs": RUNS}


def _script(name):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).resolve().parents[2] / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _v8():
    """Step 4's voiceprint rows of six lines over RUNS (speaker_v8.cue_speakers: host_prob is the host's)."""
    spans = [(0, 3000, 0.95), (3000, 4000, 0.3), (4000, 8000, 0.2), (8000, 11000, 0.05), (11000, 14500, 0.1), (14500, 16000, 0.85)]
    return [{"start_ms": a, "end_ms": b, "speaker": sv.HOST if p >= 0.8 else sv.GUEST_LABEL, "conf": max(p, 1 - p),
             "units": 2, "host_prob": p} for a, b, p in spans]


def _cues():
    return [{"start_ms": r["start_ms"], "end_ms": r["end_ms"]} for r in _v8()]


def test_the_lowest_path_is_the_recording_s_owner_and_a_single_remaining_path_the_other_guest():
    trio = ["安晚awa", "礼墨Sumi"]
    assert arrival.group_persons([False, False, True], "李豆沙", "礼墨Sumi", trio) == (["礼墨Sumi", "安晚awa", "李豆沙"], {})
    assert arrival.group_persons([False, True], "李豆沙", "雾深Girimi", ["雾深Girimi"]) == (["雾深Girimi", "李豆沙"], {})
    # four people: the two between are guests, which of them is not known
    assert arrival.group_persons([False, False, False, True], "主", "甲", ["甲", "乙", "丙"])[0] == ["甲", arrival.UNNAMED, arrival.UNNAMED, "主"]
    # more paths than registered guests (someone unregistered on a device of their own): another guest, unnamed
    assert arrival.group_persons([False, False, True], "李豆沙", "礼墨Sumi", ["礼墨Sumi"]) == (["礼墨Sumi", arrival.UNNAMED, "李豆沙"], {})


def test_a_group_given_to_nobody_has_no_label_and_leaves_the_paths_it_could_be_unnamed():
    trio, shared = ["安晚awa", "礼墨Sumi"], "礼墨Sumi或安晚awa"
    # the group between is the host's second latency or 安晚awa's own path: the lowest can then be either guest
    assert arrival.group_persons([False, None, True], "李豆沙", "礼墨Sumi", trio) == (
        [shared, None, "李豆沙"], {shared: ["礼墨Sumi", "安晚awa"]})
    assert arrival.group_persons([False, None, True], "李豆沙", "礼墨Sumi", ["礼墨Sumi"]) == (["礼墨Sumi", None, "李豆沙"], {})
    # the highest is not placed: the two paths below it are the two guests' as before
    assert arrival.group_persons([False, False, None], "李豆沙", "礼墨Sumi", trio) == (["礼墨Sumi", "安晚awa", None], {})
    # the lowest is not placed: the path above it is not known to be the owner's
    assert arrival.group_persons([None, False, True], "李豆沙", "礼墨Sumi", trio) == (
        [None, shared, "李豆沙"], {shared: ["礼墨Sumi", "安晚awa"]})
    assert arrival.group_persons([None, False, False, True], "李豆沙", "礼墨Sumi", trio) == (
        [None, shared, "安晚awa", "李豆沙"], {shared: ["礼墨Sumi", "安晚awa"]})
    assert arrival.group_persons([None, True], "李豆沙", "礼墨Sumi", trio) == ([None, "李豆沙"], {})


# 11-24 784080: three groups (-2 / +487 / +597 ms) with 李豆沙 and 礼墨Sumi registered.  安晚awa sat with 礼墨Sumi (one sound
# path), and the host's call latency changed once: +597 before, +487 after.
MOVED = [{"start_ms": 0, "end_ms": 4000, "cluster": 0},         # 礼墨Sumi's path
         {"start_ms": 4000, "end_ms": 6000, "cluster": 2},      # the host, at the first latency: v8 mishears this line
         {"start_ms": 6000, "end_ms": 10000, "cluster": 2},
         {"start_ms": 10000, "end_ms": 14000, "cluster": 0},
         {"start_ms": 14000, "end_ms": 16000, "cluster": 2},    # one line of the host's across the change
         {"start_ms": 16000, "end_ms": 18000, "cluster": 1},
         {"start_ms": 18000, "end_ms": 22000, "cluster": 1}]    # the host, at the second latency
MOVED_DOC = {"status": "OK", "centres_ms": [-2.3, 487.3, 597.1], "runs": MOVED}
DUO = dict(TRIO, recording_date="2026-11-24", participants=[["李豆沙", "小李"], ["礼墨Sumi"]])


def _heard(probs=(0.05, 0.3, 0.97, 0.2, 0.9, 0.93)):
    """Step 4's voiceprint rows of six lines over MOVED (speaker_v8.cue_speakers: host_prob is the host's)."""
    spans = [(0, 4000), (4000, 6000), (6000, 10000), (10000, 14000), (14000, 18000), (18000, 22000)]
    return [{"start_ms": a, "end_ms": b, "speaker": sv.HOST if p >= 0.5 else sv.GUEST_LABEL, "conf": max(p, 1 - p),
             "units": 2, "host_prob": p} for (a, b), p in zip(spans, probs, strict=True)]


def test_the_groups_the_host_s_voiceprint_hears_as_her_are_all_the_host():
    voice = arrival.host_time(MOVED, 3, _heard())
    # line 5 is split between two groups (neither holds four fifths of it): it counts for no group
    assert voice == [(0.0, 8000.0), (4000.0, 0.0), (4000.0, 2000.0)]
    assert arrival.host_groups(voice) == [False, True, True]       # the highest stays hers with one line misheard
    # a line in the classifier's undecided band (0.4 .. 0.6) is no conclusion, and a group without one is nobody's
    voice = arrival.host_time(MOVED, 3, _heard((0.05, 0.5, 0.97, 0.5, 0.9, 0.45)))
    assert voice == [(0.0, 4000.0), (0.0, 0.0), (4000.0, 0.0)] and arrival.host_groups(voice) == [False, None, True]
    # the registration has one guest: her path carries her name (the lags cannot see a second person on it)
    assert arrival.group_persons([False, True, True], "李豆沙", "礼墨Sumi", ["礼墨Sumi"]) == (["礼墨Sumi", "李豆沙", "李豆沙"], {})
    # both registered and one path: the path is either of them, the voiceprint is left to say which
    shared = "礼墨Sumi或安晚awa"
    assert arrival.group_persons([False, True, True], "李豆沙", "礼墨Sumi", ["安晚awa", "礼墨Sumi"]) == (
        [shared, "李豆沙", "李豆沙"], {shared: ["礼墨Sumi", "安晚awa"]})
    # two paths for three guests: the lowest can be anyone, a higher one anyone but the recording's owner
    assert arrival.group_persons([False, False, True], "主", "甲", ["甲", "乙", "丙"]) == (
        ["甲或乙或丙", "乙或丙", "主"], {"甲或乙或丙": ["甲", "乙", "丙"], "乙或丙": ["乙", "丙"]})


def test_a_group_is_someone_s_only_on_what_the_voiceprint_heard_in_it():
    # (ms heard as the host, ms heard as someone else) per group, lowest lag first; the values are the measured ones
    groups = arrival.host_groups
    # whole clips: 397290 (the host says little), 784080 (her latency moved), a duo
    assert groups([(1600.0, 29500.0), (0.0, 9500.0), (2300.0, 0.0)]) == [False, False, True]
    assert groups([(6100.0, 79600.0), (8900.0, 0.0), (20100.0, 0.0)]) == [False, True, True]
    assert groups([(5200.0, 87500.0), (85000.0, 0.0)]) == [False, True]
    # one misheard line does not turn the owner's path into the host (443680 at 0-30 s, 1254550 at 70-100 s)
    assert groups([(1000.0, 0.0), (25900.0, 0.0)]) == [None, True]
    assert groups([(2800.0, 2000.0), (12000.0, 0.0)]) == [None, True]
    # a group between the ends without voiceprint lines is not a guest by default (298620 at 10-70 s: her second latency)
    assert groups([(5800.0, 15800.0), (0.0, 0.0), (1600.0, 0.0)]) == [False, None, True]
    assert groups([(1000.0, 20000.0), (2000.0, 0.0), (9000.0, 2000.0)]) == [False, None, True]      # under 3 s
    assert groups([(1000.0, 20000.0), (3000.0, 0.0), (9000.0, 2000.0)]) == [False, True, True]
    assert groups([(1000.0, 20000.0), (0.0, 3000.0), (9000.0, 2000.0)]) == [False, False, True]
    assert groups([(1000.0, 20000.0), (4000.0, 3000.0), (9000.0, 2000.0)]) == [False, None, True]   # neither side has two thirds
    # the highest is not the host's by default: the host silent (1493180 at 250-280 s, 625460 at 40-70 s: 安晚awa)
    assert groups([(0.0, 14400.0), (0.0, 2800.0)]) == [False, None]
    assert groups([(0.0, 14800.0), (0.0, 0.0)]) == [False, None]
    assert groups([(0.0, 20000.0), (2000.0, 12000.0)]) == [False, False]       # concluded at length: two guests
    # ... nor a guest's when she is misheard for half of ten seconds (a changed voice, singing)
    assert groups([(0.0, 30000.0), (0.0, 9000.0), (5400.0, 6600.0)]) == [False, False, None]
    assert groups([(12000.0, 1000.0), (30000.0, 0.0)]) == [True, True]         # nobody but her, her latency moved
    assert groups([(0.0, 0.0), (0.0, 0.0)]) == [None, None]                    # no voiceprint: no answer


def test_a_line_lying_in_a_group_given_to_nobody_is_not_the_arrival_s():
    segments, named, persons = arrival.engine(MOVED_DOC, ["礼墨Sumi", None, "李豆沙"], "李豆沙")
    assert named == {"g0": {"speaker": sv.GUEST_LABEL}, "g2": {"speaker": sv.HOST}} and persons == {"g0": "礼墨Sumi", "g2": "李豆沙"}
    cues = [{"start_ms": r["start_ms"], "end_ms": r["end_ms"], "text": ""} for r in _heard()]
    got = arrival.cue_clusters(cues, segments, named, persons, {})
    # line 5 is half in the unplaced group, line 6 wholly: the label rule alone would call line 5 the host's
    assert [None if c is None else c["speaker"] for c in got] == [sv.GUEST_LABEL, sv.HOST, sv.HOST, sv.GUEST_LABEL, None, None]
    assert sv.cue_clusters(cues, segments, named)[4]["speaker"] == sv.HOST
    track = [{"start_s": c["start_ms"] / 1000, "end_s": c["end_ms"] / 1000} for c in cues]
    assert arrival.line_persons(track, segments, persons, {}) == {1: "礼墨Sumi", 2: "李豆沙", 3: "李豆沙", 4: "礼墨Sumi"}


def test_without_the_words_times_a_line_is_the_person_who_holds_four_fifths_of_it():
    segments, named, persons = arrival.engine(DOC, ["礼墨Sumi", "安晚awa", "李豆沙"], "李豆沙")
    assert named == {"g0": {"speaker": sv.GUEST_LABEL}, "g1": {"speaker": sv.GUEST_LABEL}, "g2": {"speaker": sv.HOST}}
    cues = [{"start_s": c["start_ms"] / 1000, "end_s": c["end_ms"] / 1000, "text": f["text"]} for c, f in zip(_cues(), FINALS)]
    assert arrival.line_persons(cues, segments, persons, {}) == {1: "李豆沙", 2: "李豆沙", 4: "礼墨Sumi", 5: "安晚awa", 6: "安晚awa"}    # line 3 holds two


def _lines():
    return [dict(c, text=f["text"]) for c, f in zip(_cues(), FINALS)]


def test_under_the_arrival_groups_v8_is_a_doubt_only_where_it_says_the_host():
    segments, named, persons = arrival.engine(DOC, ["礼墨Sumi", "安晚awa", "李豆沙"], "李豆沙")
    fused = sv.fuse(_v8(), {"arrival": arrival.cue_clusters(_lines(), segments, named, persons, {})}, primary="arrival")
    assert [f["speaker"] for f in fused] == [sv.HOST, sv.HOST, *[sv.GUEST_LABEL] * 4]
    # the line holding two people, and the line the arrival gives to a guest while the host's voiceprint says the host
    assert [f["speaker_confidence"] for f in fused] == ["高", "高", "低", "高", "高", "低"]
    assert not sv.acoustic_tie(fused[1]) and sv.acoustic_votes(fused[1]) == [sv.HOST]     # v8 saying a guest is no doubt
    assert sv.acoustic_tie(fused[5]) and sv.acoustic_votes(fused[5]) == [sv.GUEST_LABEL, sv.HOST]
    assert "主播声纹（v8）判为李豆沙（概率 0.85）" in fused[5]["speaker_why"]
    # v8's host inside its own mid band is a coin toss, not a doubt
    mid = [dict(r, v8_mid="tie") for r in _v8()]
    toss = sv.fuse(mid, {"arrival": arrival.cue_clusters(_lines(), segments, named, persons, {})}, primary="arrival")[5]
    assert toss["speaker_confidence"] == "高" and sv.acoustic_votes(toss) == [sv.GUEST_LABEL]
    # the same rows under MOSS as the label engine: the disagreement with v8 is a doubt and a tie, as before
    by_moss = sv.fuse(_v8(), {"moss": sv.cue_clusters(_cues(), segments, named)})
    assert by_moss[1]["speaker_confidence"] == "低" and sv.acoustic_tie(by_moss[1])


def _host(found=True, doc=DOC, people=3):
    """A media host that has (or has not) a guest's recording and what the arrival tool finds of the clip in it."""
    def script(name, body, remote):
        if name == "s4-guest-audio":
            files = [{"bvid": "BV1guest", "cid": 1, "path": "/day/guest-audio/BV1guest-1.m4s"}] if found else []
            return RemoteResult(0, "GUEST_AUDIO " + json.dumps({"guests": {"礼墨Sumi": {"files": files}, "安晚awa": {"files": []}}}) + "\n", "")
        if name == "s4-arrival":
            assert "--cut /r/clip/cut/cut.mp4" in body and f"--people {people}" in body and "BV1guest-1.m4s" in body
            remote.files["/r/clip/speaker/arrival-BV1guest.json"] = json.dumps(doc).encode()
            return RemoteResult(0, f"ARRIVAL {doc['status']}\n", "")
        return RemoteResult(0, "", "")
    return FakeRemote(on_script=script)


TRIO_READING = [{"n": 3, "speaker": sv.GUEST_LABEL, "reason": "前半句是她自己，后半句是别人问",
                 "split": {"first": "那我直接死了。", "first_speaker": sv.HOST, "second": "什么？她想拿炸弹炸我们？",
                           "second_speaker": sv.GUEST_LABEL}},
                {"n": 6, "speaker": sv.HOST, "reason": "接的是她自己上一句"}]


class _Cpa:
    """A CPA whose reading of the pending lines is `rows` (none: it changes nothing)."""
    def __init__(self, rows=TRIO_READING):
        self.asked, self.rows = [], rows

    def ask(self, *, call_id, prompt_text, model, effort, remote_dir):
        self.asked.append(prompt_text)
        answer = {"anchors": []} if call_id == "guest-identity" else self.rows    # step 4 names the guests: no citation
        return {"call_id": call_id, "model": model, "effort": effort, "rc": 0, "elapsed_s": 0.1,
                "response_text": json.dumps(answer, ensure_ascii=False), "stderr_tail": ""}


FINALS = [{"text": "果然awawa就是这种幕后黑手的人。"}, {"text": "就是死了就不会疼了。"},
          {"text": "那我直接死了。什么？她想拿炸弹炸我们？"}, {"text": "你死了我们怎么办"},
          {"text": "我只是一个非常勤勤恳恳的矿工"}, {"text": "这不重要。"}]


def test_step_4_labels_a_trio_by_the_arrival_and_cuts_a_two_person_line_at_its_switch(tmp_path):
    # the guard on the ordinary path: a three-person session, the guest's recording found on the host
    cpa = _Cpa()
    ctx = make_ctx(tmp_path, remote=_host(), cpa=cpa, session=TRIO)
    ctx.remote_dir = "/r/clip"
    rows, calls, process = si._speaker_clusters(ctx, FINALS, _cues(), _v8())
    assert [r["speaker"] for r in rows] == [sv.HOST, sv.HOST, *[sv.GUEST_LABEL] * 3, sv.HOST]
    assert [r["label_from"] for r in rows] == ["arrival"] * 6
    assert len(cpa.asked) == 1 and "3「那我直接死了。什么？她想拿炸弹炸我们？」" in cpa.asked[0]
    assert "2「就是死了就不会疼了。」" not in cpa.asked[0]         # v8 saying a guest against the arrival is no reason to ask
    assert "到达时间" in cpa.asked[0] and "第 1.0 秒处由李豆沙换成连线" in cpa.asked[0]
    # v8 saying the host against the arrival's guest is: the two routes tie and the reading decides (line 6)
    assert "6「这不重要。」：arrival 判为连线，主播声纹（v8）判为李豆沙（概率 0.85）" in cpa.asked[0]
    assert "声音同时出现时，arrival 会把它算给嘉宾。声音证据打平，可以按语境选一方" in cpa.asked[0]
    assert "已知的例外" in cpa.asked[0] and "比声纹可靠。" not in cpa.asked[0]
    assert rows[5]["speaker_acoustic"] == sv.GUEST_LABEL and rows[5]["speaker_how"] == "打平，按语境"
    assert [(p["start_ms"], p["end_ms"], p["speaker"]) for p in rows[2]["split"]] == [
        (4000, 5000, sv.HOST), (5000, 8000, sv.GUEST_LABEL)]
    assert any("在礼墨Sumi的公开录播（BV1guest）里对上" in p and "礼墨Sumi -471 毫秒" in p for p in process)
    assert any("声纹有结论的句子里听成李豆沙和听成别人的秒数为 -471 毫秒组 0.0:3.0、-348 毫秒组 1.5:3.5、+1 毫秒组 3.0:1.0" in p
               for p in process)
    assert any("说话人按到达时间：6 句由它定" in p and "它判嘉宾而主播声纹判李豆沙的 1 句" in p for p in process)
    saved = arrival.load(ctx.clip_dir)
    assert saved is not None and saved["persons"] == {"g0": "礼墨Sumi", "g1": "安晚awa", "g2": "李豆沙"}


def test_without_a_recording_or_when_the_clip_is_not_in_it_or_one_side_speaks_step_4_is_as_before(tmp_path):
    one_side = {"status": "FEW_GROUPS", "centres_ms": [0.8]}       # the whole clip is one sound path: nothing to tell apart
    unheard = dict(DOC, runs=[dict(r, start_ms=r["start_ms"] + 100000, end_ms=r["end_ms"] + 100000) for r in RUNS])     # no line in any group
    said = []
    for n, remote in enumerate((_host(found=False), _host(doc={"status": "NOT_FOUND"}), _host(doc=one_side), _host(doc=unheard))):
        ctx = make_ctx(tmp_path / str(n), remote=remote, cpa=None, session=TRIO)
        ctx.remote_dir = "/r/clip"
        rows, calls, process = si._speaker_clusters(ctx, FINALS, _cues(), _v8())
        assert [r["speaker"] for r in rows] == [r["speaker"] for r in _v8()] and calls == []
        said += [p for p in process if p.startswith("到达时间证据：") and "照旧按声纹" in p]
        assert arrival.load(ctx.clip_dir) is None
    assert "没找到嘉宾当天的公开录播" in said[0] and "BV1guest 里没找到这一段" in said[1]
    assert "只量到 1 组时间差（全片只有一边在说话时就是一组，要两组才分得出人）" in said[2]
    # groups the voiceprint says nothing of are nobody's: no "the highest is the host" without it
    assert "量到 3 组时间差，但每组的声纹结论都不够定它是谁（听成李豆沙:听成别人的秒数 0.0:0.0、0.0:0.0、0.0:0.0）" in said[3]


MOVED_FINALS = [{"text": "没有不让你来"}, {"text": "我也想去"}, {"text": "我也想三个人一起"}, {"text": "你对着我们撒娇也没有用。"},
                {"text": "不是，我在群里发了一条消息。"}, {"text": "看到了，到底是谁没看消息？"}]


def _moved(tmp_path, session, probs=(0.05, 0.3, 0.97, 0.2, 0.9, 0.93), cpa=None):
    ctx = make_ctx(tmp_path, remote=_host(doc=MOVED_DOC, people=len(session["participants"])), cpa=cpa, session=session)
    ctx.remote_dir = "/r/clip"
    heard = _heard(probs)       # by default the voiceprint hears the host's line 2 as someone else
    cues = [{"start_ms": r["start_ms"], "end_ms": r["end_ms"]} for r in heard]
    return ctx, si._speaker_clusters(ctx, MOVED_FINALS, cues, heard)


def test_more_groups_than_registered_people_still_label_the_clip(tmp_path):
    # 11-24 784080 said 「延迟组 3 个，本场 2 人」 and fell back to the voiceprint
    ctx, (rows, calls, process) = _moved(tmp_path, DUO)
    assert [r["speaker"] for r in rows] == [sv.GUEST_LABEL, sv.HOST, sv.HOST, sv.GUEST_LABEL, sv.HOST, sv.HOST]
    assert [r["label_from"] for r in rows] == ["arrival"] * 6
    assert [r["speaker_confidence"] for r in rows] == ["高"] * 6 and calls == []     # line 5 crosses the change: one person
    saved = arrival.load(ctx.clip_dir)
    assert saved["persons"] == {"g0": "礼墨Sumi", "g1": "李豆沙", "g2": "李豆沙"} and saved["among"] == {}
    assert saved["heard_ms"] == [[0, 8000], [4000, 0], [4000, 2000]]       # line 2 was misheard: the highest group stays hers
    line = next(p for p in process if p.startswith("到达时间证据："))
    assert "礼墨Sumi -2 毫秒、李豆沙 +487 毫秒、李豆沙 +597 毫秒" in line
    assert "-2 毫秒组 0.0:8.0、+487 毫秒组 4.0:0.0、+597 毫秒组 4.0:2.0" in line and "2 组都是李豆沙" in line
    assert "最低一组是录播主人礼墨Sumi那一路" in line


def test_a_group_the_voiceprint_does_not_conclude_on_is_left_to_the_voiceprint_and_the_review(tmp_path):
    # 298620 at 10-70 s: the host's second latency had no voiceprint line and was labelled a guest's path, decisively
    cpa = _Cpa(rows=[])
    ctx, (rows, calls, process) = _moved(tmp_path, TRIO, probs=(0.05, 0.3, 0.97, 0.2, 0.9, 0.5), cpa=cpa)
    saved = arrival.load(ctx.clip_dir)
    assert saved["persons"] == {"g0": "礼墨Sumi或安晚awa", "g2": "李豆沙"} and set(saved["named"]) == {"g0", "g2"}
    assert saved["among"] == {"礼墨Sumi或安晚awa": ["礼墨Sumi", "安晚awa"]}        # not 礼墨Sumi / 安晚awa by position
    assert [r["label_from"] for r in rows] == ["arrival"] * 4 + ["v8", "v8"]
    assert [r["speaker"] for r in rows] == [sv.GUEST_LABEL, sv.HOST, sv.HOST, sv.GUEST_LABEL, sv.HOST, sv.HOST]     # lines 5, 6: v8's
    assert [r["speaker_confidence"] for r in rows] == ["高"] * 4 + ["低", "低"]
    assert len(calls) == 1 and "5「不是，我在群里发了一条消息。」：arrival 簇没有覆盖本句，按 v8" in cpa.asked[0]
    line = next(p for p in process if p.startswith("到达时间证据："))
    assert "礼墨Sumi或安晚awa -2 毫秒、未定 +487 毫秒、李豆沙 +597 毫秒" in line
    assert "1 组声纹结论不够、没分给谁，主要落在这些组里的句子不按到达时间，照声纹和复核" in line
    assert any("说话人按到达时间：4 句由它定" in p and "它没听到或没定下是谁的 2 句按 v8" in p for p in process)


def test_guests_sharing_a_path_are_told_from_the_host_and_left_to_the_voiceprint(tmp_path):
    ctx, (rows, _calls, process) = _moved(tmp_path, TRIO)
    assert [r["speaker"] for r in rows] == [sv.GUEST_LABEL, sv.HOST, sv.HOST, sv.GUEST_LABEL, sv.HOST, sv.HOST]
    assert [r["label_from"] for r in rows] == ["arrival"] * 6
    saved = arrival.load(ctx.clip_dir)
    assert saved["persons"] == {"g0": "礼墨Sumi或安晚awa", "g1": "李豆沙", "g2": "李豆沙"}
    assert saved["among"] == {"礼墨Sumi或安晚awa": ["礼墨Sumi", "安晚awa"]}
    line = next(p for p in process if p.startswith("到达时间证据："))
    assert "登记的嘉宾 2 位、定下来的嘉宾声音只有 1 路，有人共用一路或没定下来（礼墨Sumi或安晚awa）" in line
    assert "是哪一位按声纹在这几位之间分，分不出的不写名字" in line
    # step 6: the arrival names nobody on that path and says who its lines can be
    track = [{"start_s": r["start_ms"] / 1000, "end_s": r["end_ms"] / 1000, "text": f["text"], "speaker": r["speaker"]}
             for r, f in zip(rows, MOVED_FINALS, strict=True)]
    assert guest_identity.arrival_lines(ctx, track, ["安晚awa", "礼墨Sumi"]) == {}
    assert guest_identity.arrival_among(ctx, track) == {1: ["礼墨Sumi", "安晚awa"], 4: ["礼墨Sumi", "安晚awa"]}


def test_the_owner_s_label_stands_over_the_arrival(tmp_path):
    ctx = make_ctx(tmp_path, remote=_host(), cpa=_Cpa(), session=TRIO)
    ctx.remote_dir = "/r/clip"
    decided = _v8()
    decided[1] = dict(decided[1], source="维护者", speaker=sv.GUEST_LABEL)
    rows, _calls, _process = si._speaker_clusters(ctx, FINALS, _cues(), decided)
    assert rows[1]["speaker"] == sv.GUEST_LABEL and rows[1]["speaker_why"] == "维护者 标注"


def test_step_6_names_the_guests_lines_from_the_arrival(tmp_path):
    ctx = make_ctx(tmp_path, remote=_host(), cpa=None, session=TRIO)
    segments, named, persons = arrival.engine(DOC, ["礼墨Sumi", "安晚awa", "李豆沙"], "李豆沙")
    (ctx.clip_dir / "speaker").mkdir(parents=True, exist_ok=True)
    (ctx.clip_dir / arrival.PATH).write_text(json.dumps({"segments": segments, "named": named, "persons": persons}), encoding="utf-8")
    track = [{"start_s": 0.0, "end_s": 3.0, "text": "a", "speaker": sv.HOST},
             {"start_s": 5.0, "end_s": 8.0, "text": "什么？她想拿炸弹炸我们？", "speaker": sv.GUEST_LABEL},
             {"start_s": 11.0, "end_s": 14.5, "text": "我只是一个非常勤勤恳恳的矿工", "speaker": sv.GUEST_LABEL}]
    assert guest_identity.arrival_lines(ctx, track, ["安晚awa", "礼墨Sumi"]) == {2: "礼墨Sumi", 3: "安晚awa"}


def test_lag_groups_are_histogram_peaks_and_a_wide_stretch_s_groups_are_moved_onto_the_clip():
    tool = _script("arrival_speakers")
    lags = [0.0, 2.0, -3.0, 4.0, 131.0, 128.0, 135.0, 480.0, 476.0, 483.0, 479.0, 60.0]      # one stray between two groups
    assert [round(c) for c in tool.lag_groups(lags)] == [1, 131, 480]
    # the clip holds two of the three people and its zero point is 470 ms off the wide stretch's
    clip = [-470.0, -468.0, -473.0, -471.0, 1.0, 3.0, -2.0, 0.0]
    moved, share = tool.fit_groups([0.0, 130.0, 472.0], clip)
    assert [round(c) for c in moved] == [-470, -341, 0] and share == 1.0      # the middle one follows the two the clip has
    assert tool.fit_groups([0.0, 130.0, 472.0], [5.0, 4.0, 6.0, 5.0, 3.0, 7.0]) is None      # one group fits anywhere
    rows = [[0, 2], [50, 2], [100, -1], [150, 2], [200, 2], [250, 0], [300, 0], [350, 0], [400, 0], [450, 0], [500, 1]]
    assert [(r["start_ms"], r["end_ms"], r["cluster"]) for r in tool.runs(rows, 50, 400)] == [(175, 425, 2), (425, 675, 0)]


def test_a_guest_s_recording_is_one_published_after_the_broadcast_day_began_that_names_her_and_no_other_date():
    tool = _script("guest_recording_audio")
    day = 1795377600.0       # any start
    row = {"title": "【录播】【礼墨】黄金矿工三人组！【2026.11.23-20:00】", "author": "礼墨录播组", "pubdate": day + 20 * 3600,
           "duration": "274:25"}
    names = ("礼墨Sumi", tool.short_name("礼墨Sumi"))
    assert names[1] == "礼墨" and tool.wanted(row, names, day, "20261123")
    assert not tool.wanted(dict(row, title="【录播】【礼墨】双倍杂谈！【2026.11.24-19:01】"), names, day, "20261123")
    assert not tool.wanted(dict(row, duration="12:00"), names, day, "20261123")              # a clip, not a recording
    assert not tool.wanted(dict(row, pubdate=day - 3600), names, day, "20261123")           # before the day began
    assert not tool.wanted(dict(row, title="【录播】【别人】", author="别人录播组"), names, day, "20261123")


# ----- who says a line, by where its words fall (arrival.line_words) -------------------------------------------------
TRIO_PERSONS = ["礼墨Sumi", "安晚awa", "李豆沙"]


def _bcut(utterances):
    """A free_asr_client document: [(start, end, [(letter, start, end), ...])]."""
    return {"utterances": [{"start_time": a, "end_time": b, "transcript": "".join(w[0] for w in words),
                            "words": [{"label": w, "start_time": x, "end_time": y} for w, x, y in words]}
                           for a, b, words in utterances]}


def _engine(runs):
    return arrival.engine({"runs": [{"start_ms": a, "end_ms": b, "cluster": g} for a, b, g in runs]}, TRIO_PERSONS, "李豆沙")


# 443680 (11-23, published 11-24), 86.1-94.9 s: the arrival's stretches (0 礼墨Sumi, 1 安晚awa, 2 李豆沙) and BCUT's letters.
# BCUT timed 「就不会疼了」 after the end it gave the line (90.24 s): the host speaks until 91.0 s, 安晚awa says
# 「不是这个意思」 (no recogniser has it) from 91.3 s.
FROZEN_RUNS = [(84075, 89275, 0), (89325, 90325, 2), (90425, 90825, 2), (91325, 91825, 1), (92325, 94925, 0)]
FROZEN_BCUT = _bcut([
    (86080, 87520, [("就", 86080, 86240), ("先", 86240, 86400), ("冻", 86440, 86640), ("住", 86640, 86800), ("然", 86800, 86840),
                    ("后", 86840, 86960), ("再", 86960, 87120), ("炸", 87120, 87320), ("掉", 87360, 87520)]),
    (87520, 88120, [("然", 87520, 87600), ("后", 87600, 87720), ("就", 87720, 87880), ("炸", 87880, 87960), ("了", 87960, 88120)]),
    (88120, 89440, [("四", 88120, 88240), ("分", 88240, 88440), ("五", 88440, 88560), ("裂", 88560, 88680), ("也", 88680, 88760),
                    ("无", 88760, 88880), ("法", 88880, 89080), ("复", 89080, 89280), ("活", 89280, 89440)]),
    (89440, 91810, [("就", 89440, 89560), ("是", 89560, 89840), ("四", 89840, 90040), ("了", 90040, 90280), ("就", 90280, 90440),
                    ("不", 90440, 90560), ("会", 90560, 90760), ("疼", 90760, 91040), ("了", 91040, 91640)]),
    (91810, 94890, [("好", 91970, 92650), ("好", 92850, 93050), ("可", 93050, 93210), ("怕", 93210, 93370), ("哈", 93370, 93650)])])
FROZEN_LINES = [{"start_ms": 86080, "end_ms": 87520, "text": "就先冻住，然后再炸掉"},
                {"start_ms": 87520, "end_ms": 89440, "text": "然后就炸得四分五裂，无法复活。"},
                {"start_ms": 89440, "end_ms": 90240, "text": "就是死了就不会疼了。"},
                {"start_ms": 90240, "end_ms": 91810, "text": "不是这个意思。"},
                {"start_ms": 91810, "end_ms": 94890, "text": "好好可怕。"}]


def test_a_line_whose_words_the_neighbour_s_words_leave_no_time_for_is_who_speaks_in_what_is_left():
    segments, named, persons = _engine(FROZEN_RUNS)
    letters = {"bcut": arrival.recognised_letters(FROZEN_BCUT)}
    # the whole-line rule: 「不是这个意思。」 holds as much of the host as of 安晚awa, the label rule calls it the host's
    assert sv.cue_clusters(FROZEN_LINES[3:4], segments, named)[0]["speaker"] == sv.HOST
    got = arrival.cue_clusters(FROZEN_LINES, segments, named, persons, letters)
    # its own letters are not in the recognition: what the host's 「疼了」 leaves of its time is 安晚awa's, so the label is
    # the guests'; the neighbours' words moved the label but do not settle it (the whole window holds the host): reviewed
    assert [(c["speaker"], c["spans"]) for c in got] == [(sv.GUEST_LABEL, False), (sv.GUEST_LABEL, False), (sv.HOST, False),
                                                         (sv.GUEST_LABEL, True), (sv.GUEST_LABEL, False)]
    assert got[3]["note"].endswith("；前后句对上的字占掉了 90.2–91.3 秒，剩下的 91.3–91.8 秒里：安晚awa 91.3–91.8 秒（0.5 秒）")
    assert got[3]["note"].startswith("李豆沙 90.2–90.3 秒（0.1 秒）、李豆沙 90.4–90.8 秒（0.4 秒）、安晚awa 91.3–91.8 秒")
    fused = sv.fuse([{"start_ms": c["start_ms"], "end_ms": c["end_ms"], "speaker": sv.GUEST_LABEL, "host_prob": 0.5, "units": 1}
                     for c in FROZEN_LINES], {arrival.ENGINE: got}, primary=arrival.ENGINE)
    assert (fused[3]["speaker"], fused[3]["speaker_confidence"]) == (sv.GUEST_LABEL, "低")
    assert sv.adopt_review(fused[3], {"speaker": sv.HOST, "reason": "接着上一句"}, "不是这个意思。")["speaker"] == sv.GUEST_LABEL
    # the host's line ends where she stops, not where the recognition cut it; the next starts there; no text changes
    assert got[2]["times"] == [89440, 90825] and got[3]["times"] == [90825, 91810]
    track = [{"start_s": c["start_ms"] / 1000, "end_s": c["end_ms"] / 1000, "text": c["text"]} for c in FROZEN_LINES]
    assert arrival.line_persons(track, segments, persons, letters) == {1: "礼墨Sumi", 2: "礼墨Sumi", 3: "李豆沙", 5: "礼墨Sumi"}
    # without the words' times the lines go by their whole time, as before
    assert arrival.line_persons(track, segments, persons, {}) == {1: "礼墨Sumi", 2: "礼墨Sumi", 3: "李豆沙", 5: "礼墨Sumi"}
    assert "times" not in arrival.cue_clusters(FROZEN_LINES, segments, named, persons, {})[2]


# 443680, 31.7-39.6 s: 「但是，但但是冰冻是人祸。」 holds 安晚awa (35.0-35.9 s) and 礼墨Sumi (36.2-36.5 s) inside the host's
# line; the whole-line rule gave it to the guests (安晚awa holds the most time) and the review kept the guest.
FROST_RUNS = [(28475, 32325, 0), (32425, 32875, 1), (32975, 33325, 0), (33375, 33625, 1), (33675, 34325, 0), (34325, 34725, 2),
              (35025, 35925, 1), (36225, 36525, 0), (36625, 43725, 2)]
FROST_BCUT = _bcut([
    (31740, 33510, [("石", 31870, 32070), ("头", 32070, 32310), ("真", 32350, 32549), ("的", 32549, 32630), ("是", 32630, 32910),
                    ("天", 32910, 33110), ("灾", 33270, 33470)]),
    (33510, 34650, [("石", 33550, 33790), ("头", 33790, 33950), ("是", 33950, 34190), ("天", 34190, 34350), ("灾", 34390, 34590)]),
    (34650, 37430, [("但", 34710, 34910), ("是", 34910, 35270), ("但", 35310, 35550), ("但", 35950, 36110), ("是", 36110, 36310),
                    ("冰", 36350, 36550), ("冻", 36590, 36790), ("是", 36870, 37070), ("人", 37070, 37270), ("话", 37270, 37430)]),
    (37430, 39610, [("就", 37430, 37590), ("是", 37590, 37790), ("阿", 37910, 38110), ("晚", 38110, 38270), ("真", 38270, 38430),
                    ("的", 38430, 38670), ("可", 38670, 38830), ("高", 38870, 39070), ("兴", 39110, 39310), ("了", 39310, 39510)])])
FROST_LINES = [{"start_ms": 31740, "end_ms": 33510, "text": "哦，应该……石头真的是天灾。"},
               {"start_ms": 33510, "end_ms": 34650, "text": "石头是天灾"},
               {"start_ms": 34650, "end_ms": 37430, "text": "但是，但但是冰冻是人祸。"},
               {"start_ms": 37430, "end_ms": 39610, "text": "就是安晚在那可高兴了。"}]


def test_a_line_s_label_is_who_its_words_fall_on_and_a_line_they_do_not_settle_is_reviewed_with_who_speaks_when():
    segments, named, persons = _engine(FROST_RUNS)
    assert sv.cue_clusters(FROST_LINES[2:3], segments, named)[0]["speaker"] == sv.GUEST_LABEL      # the whole-line rule
    got = arrival.cue_clusters(FROST_LINES, segments, named, persons, {"bcut": arrival.recognised_letters(FROST_BCUT)})
    line = got[2]
    assert line["speaker"] == sv.HOST and line["spans"]         # most of its words are the host's; not four fifths: reviewed
    assert ("李豆沙 34.6–34.7 秒（0.1 秒）、安晚awa 35.0–35.9 秒（0.9 秒）、礼墨Sumi 36.2–36.5 秒（0.3 秒）、李豆沙 36.6–37.4 秒（0.8 秒）"
            in line["note"])
    assert "「冻是人」李豆沙" in line["note"] and "「祸」识别稿里没对上" in line["note"]
    assert got[3]["speaker"] == sv.HOST and not got[3]["spans"]
    v8 = [{"start_ms": c["start_ms"], "end_ms": c["end_ms"], "speaker": sv.GUEST_LABEL, "conf": 0.7, "units": 2, "host_prob": 0.3}
          for c in FROST_LINES]
    fused = sv.fuse(v8, {"arrival": got}, primary="arrival")
    assert fused[2]["speaker_confidence"] == "低" and "arrival 显示本句里不止一个人（李豆沙 34.6–34.7 秒" in fused[2]["speaker_why"]
    # the review reads the host by default: 「嘉宾」 without a content proof changes nothing
    took = sv.adopt_review(fused[2], {"speaker": sv.GUEST_LABEL, "evidence": "无"}, FROST_LINES[2]["text"])
    assert took["speaker"] == sv.HOST


def _letters(*rows):
    return {"bcut": arrival.recognised_letters(_bcut([(row[0][1], row[-1][2], row) for row in rows]))}


def test_a_line_is_its_speaker_s_even_with_others_heard_around_its_words_and_is_cut_only_at_a_break_between_two_turns():
    # most of the words on one person: theirs, though the window holds the host as long as 礼墨Sumi
    segments, named, persons = _engine([(0, 1000, 2), (1000, 3000, 0), (3000, 4000, 2)])
    words = _letters([(ch, 1100 + 250 * k, 1300 + 250 * k) for k, ch in enumerate("我游戏还在动")])
    assert sv.cue_clusters([{"start_ms": 0, "end_ms": 4000}], segments, named)[0]["spans"]
    row = arrival.cue_clusters([{"start_ms": 0, "end_ms": 4000, "text": "我游戏还在动。"}], segments, named, persons, words)[0]
    assert row["speaker"] == sv.GUEST_LABEL and not row["spans"] and "parts" not in row
    # two people one after the other, a break between their words: two lines, at the second one's first word
    segments, named, persons = _engine([(0, 1500, 1), (1600, 3000, 0)])
    words = _letters([("大", 100, 300), ("意", 300, 500), ("了", 500, 700), ("怎", 1700, 1900), ("么", 1900, 2100), ("办", 2100, 2300)])
    line = {"start_ms": 0, "end_ms": 3000, "text": "大意了，怎么办"}
    row = arrival.cue_clusters([line], segments, named, persons, words)[0]
    assert row["parts"] == [{"start_ms": 0, "end_ms": 1700, "text": "大意了，", "speaker": sv.GUEST_LABEL},
                            {"start_ms": 1700, "end_ms": 3000, "text": "怎么办", "speaker": sv.GUEST_LABEL}]
    track = [{"start_s": p["start_ms"] / 1000, "end_s": p["end_ms"] / 1000, "text": p["text"]} for p in row["parts"]]
    assert arrival.line_persons(track, segments, persons, words) == {1: "安晚awa", 2: "礼墨Sumi"}
    # no break between the two people's words (a cut would fall inside a phrase): not cut, and no one's name; two guests
    # leave the label settled, the host and a guest send it to the review
    joined = [dict(line, text="大意了怎么办")]
    row = arrival.cue_clusters(joined, segments, named, persons, words)[0]
    assert "parts" not in row and not row["spans"] and row["speaker"] == sv.GUEST_LABEL
    assert arrival.line_persons([dict(track[0], end_s=3.0, text="大意了怎么办")], segments, persons, words) == {}
    host = _engine([(0, 1500, 2), (1600, 3000, 0)])
    row = arrival.cue_clusters(joined, host[0], host[1], host[2], words)[0]
    assert "parts" not in row and row["spans"] and "「大意了」李豆沙、「怎么办」礼墨Sumi" in row["note"]
    # the stretches change more than once: not cut
    segments, named, persons = _engine([(0, 1500, 1), (1600, 2000, 0), (2000, 2600, 1), (2600, 3000, 0)])
    assert "parts" not in arrival.cue_clusters([line], segments, named, persons, words)[0]


def test_the_words_times_are_step_3_s_recognition_of_this_cut(tmp_path):
    doc = _bcut([(100, 900, [("好", 100, 400), ("的", 400, 900)])])
    srt = "1\n00:00:00,100 --> 00:00:00,900\n好的\n"
    remote = FakeRemote()
    remote.files["/r/clip/asr/bcut.json"] = json.dumps(doc).encode()
    ctx = make_ctx(tmp_path, remote=remote, cpa=None, session=TRIO)
    ctx.remote_dir = "/r/clip"
    (ctx.clip_dir / "asr").mkdir(parents=True, exist_ok=True)
    (ctx.clip_dir / "asr/bcut.srt").write_text(srt, encoding="utf-8")
    letters, why = arrival.clip_letters(ctx)
    assert letters == {"bcut": [["好", 100, 400], ["的", 400, 900]]}           # fetched from the media host, kept beside the SRT
    assert (ctx.clip_dir / "asr/bcut.json").exists() and why == ["jianying 没有逐字时间文件"]
    (ctx.clip_dir / "asr/bcut.srt").write_text(srt.replace("00,100", "00,200"), encoding="utf-8")     # another recognition
    assert arrival.clip_letters(ctx) == ({}, ["bcut 的逐字时间文件和本片的识别稿（asr/bcut.srt）不是同一次识别", "jianying 没有逐字时间文件"])


def test_a_revision_s_words_times_are_its_predecessors_recognition(tmp_path):
    # a revision takes step 3 from the clip it revises and only cut/ is copied on the host (revise_run): its own
    # directory there has no asr/; the recognition is fetched from the nearest predecessor that keeps it
    from src.autoslice.manual_lane.state import ClipState

    doc = _bcut([(100, 900, [("好", 100, 400), ("的", 400, 900)])])
    remote = FakeRemote()
    remote.files["/r/orig/asr/bcut.json"] = json.dumps(doc).encode()
    ctx = make_ctx(tmp_path, remote=remote, cpa=None, session=TRIO, remote_dir="/r/clip")
    for cid, rdir, origin in (("orig_r2", "/r/orig_r2", "orig"), ("orig", "/r/orig", None)):
        state = ClipState.create(tmp_path / "state" / f"{cid}.json", clip=dict(ctx.state.data["clip"], cid=cid, remote_dir=rdir),
                                 session=TRIO)
        if origin:
            state.data["revised_from"] = {"cid": origin, "reason": "test", "at": "2026-11-24T00:00:00Z"}
        state.save()
    ctx.state.data["revised_from"] = {"cid": "orig_r2", "reason": "test", "at": "2026-11-24T00:00:00Z"}
    (ctx.clip_dir / "asr").mkdir(parents=True, exist_ok=True)
    (ctx.clip_dir / "asr/bcut.srt").write_text("1\n00:00:00,100 --> 00:00:00,900\n好的\n", encoding="utf-8")
    letters, why = arrival.clip_letters(ctx)
    assert letters == {"bcut": [["好", 100, 400], ["的", 400, 900]]} and why == ["jianying 没有逐字时间文件"]
    assert (ctx.clip_dir / "asr/bcut.json").exists() and not (ctx.clip_dir / "asr/jianying.json").exists()


# A trio clip through step 4 with BCUT's letters: a line of two people cut at their words, a boundary the recognition put
# before the host stopped moved to where she stops, and step 6 naming the parts.
WORDS_RUNS = [{"start_ms": 0, "end_ms": 4000, "cluster": 2}, {"start_ms": 4000, "end_ms": 8000, "cluster": 0},
              {"start_ms": 8000, "end_ms": 12000, "cluster": 1}, {"start_ms": 12000, "end_ms": 13400, "cluster": 1},
              {"start_ms": 13600, "end_ms": 15000, "cluster": 0}, {"start_ms": 15000, "end_ms": 16700, "cluster": 2},
              {"start_ms": 17000, "end_ms": 18500, "cluster": 1}]
WORDS_FINALS = [(0, 4000, "我们先去那边看看吧"), (4000, 8000, "好的我马上就过来了"), (8000, 12000, "等一下我还没准备好"),
                (12000, 15000, "大意了，怎么办"), (15000, 16000, "就是死了"), (16000, 18500, "不是这个意思")]
WORDS_PROBS = (0.95, 0.05, 0.05, 0.3, 0.95, 0.05)


def _spread(a, b, text):
    step = (b - a) // len(text)
    return [(ch, a + step * k, a + step * (k + 1)) for k, ch in enumerate(text)]


def _words_step4(tmp_path, runs=WORDS_RUNS, probs=WORDS_PROBS):
    from fakes import IdCpa

    cpa = IdCpa()
    ctx = make_ctx(tmp_path, remote=_host(doc=dict(DOC, runs=runs)), cpa=cpa, session=TRIO)
    ctx.remote_dir = "/r/clip"
    utterances = [(0, 4000, _spread(100, 3900, "我们先去那边看看吧")), (4000, 8000, _spread(4100, 7900, "好的我马上就过来了")),
                  (8000, 12000, _spread(8100, 11900, "等一下我还没准备好")),
                  (12000, 15000, [*_spread(12100, 12700, "大意了"), *_spread(13700, 14300, "怎么办")]),
                  (15000, 16000, _spread(15050, 16450, "就是死了"))]                       # 「了」 at 16.1 s: past the line's end
    for name in ("asr", "subs", "speaker"):
        (ctx.clip_dir / name).mkdir(parents=True, exist_ok=True)
    (ctx.clip_dir / "asr/bcut.json").write_text(json.dumps(_bcut(utterances)), encoding="utf-8")
    (ctx.clip_dir / "asr/bcut.srt").write_text("".join(f"{n}\n{si.fmt_ts(a / 1000)} --> {si.fmt_ts(b / 1000)}\n{''.join(w[0] for w in ws)}\n\n"
                                                       for n, (a, b, ws) in enumerate(utterances, 1)), encoding="utf-8")
    (ctx.clip_dir / "subs/final.srt").write_text("".join(f"{n}\n{si.fmt_ts(a / 1000)} --> {si.fmt_ts(b / 1000)}\n{t}\n\n"
                                                         for n, (a, b, t) in enumerate(WORDS_FINALS, 1)), encoding="utf-8")
    units = [{"id": f"u{n}", "s": a, "e": b, "lw": b, "cue": n} for n, (a, b, _t) in enumerate(WORDS_FINALS)]
    (ctx.clip_dir / "speaker/chunks-units.json").write_text(json.dumps({"units": units}), encoding="utf-8")
    preds = [{"id": u["id"], "ok": True, "probs": {"李豆沙": p}} for u, p in zip(units, probs, strict=True)]
    (ctx.clip_dir / "speaker/chunks-pred.json").write_text(json.dumps({"predictions": preds}), encoding="utf-8")
    out = si.step4_speaker(ctx)
    assert out.ok, out.note
    return ctx, out, cpa


def test_step_4_cuts_a_line_at_its_two_speakers_words_and_moves_a_boundary_to_where_the_host_stops(tmp_path):
    ctx, out, _cpa = _words_step4(tmp_path)
    track = st.read_track(ctx.clip_dir)
    assert [(c["start_s"], c["end_s"], c["text"], c["speaker"]) for c in track] == [
        (0.0, 4.0, "我们先去那边看看吧", sv.HOST), (4.0, 8.0, "好的我马上就过来了", sv.GUEST_LABEL),
        (8.0, 12.0, "等一下我还没准备好", sv.GUEST_LABEL), (12.0, 13.7, "大意了，", sv.GUEST_LABEL), (13.7, 15.0, "怎么办", sv.GUEST_LABEL),
        (15.0, 16.7, "就是死了", sv.HOST), (16.7, 18.5, "不是这个意思", sv.GUEST_LABEL)]
    assert any("每句按它的字落在谁说话的时间里定（字的时间：必剪逐字；jianying 没有逐字时间文件）" in p for p in out.process)
    assert any("字分成先后两人的按字拆成两句 1 句" in p for p in out.process)
    assert guest_identity.arrival_lines(ctx, track, ["安晚awa", "礼墨Sumi"]) == {2: "礼墨Sumi", 3: "安晚awa", 4: "安晚awa", 5: "礼墨Sumi", 7: "安晚awa"}


def test_a_line_cut_at_its_two_speakers_words_is_not_doubted_by_the_whole_line_s_voiceprint(tmp_path):
    # 「大意了，」 安晚awa then 「怎么办」 the host, v8 hearing the host on the whole line: its one probability is of both
    # parts, so it raises no doubt; the review sees the line as its two parts, each with its own label
    runs = [*WORDS_RUNS[:4], {"start_ms": 13600, "end_ms": 16700, "cluster": 2}, WORDS_RUNS[6]]
    ctx, _out, cpa = _words_step4(tmp_path, runs=runs, probs=(0.95, 0.05, 0.05, 0.95, 0.95, 0.05))
    track = st.read_track(ctx.clip_dir)
    assert [(c["text"], c["speaker"]) for c in track[3:5]] == [("大意了，", sv.GUEST_LABEL), ("怎么办", sv.HOST)]
    rows = json.loads((ctx.clip_dir / "speaker/decision.json").read_text(encoding="utf-8"))["cues"]
    assert rows[3]["speaker_confidence"] == "高" and [p["speaker"] for p in rows[3]["split"]] == [sv.GUEST_LABEL, sv.HOST]
    review = [p for p in cpa.prompts if "4 [" in p]
    assert review and "\n4 [连线] 大意了， [李豆沙] 怎么办\n" in review[0]


# ----- what someone says into another person's sentence (owner) -----------------------------------------
# 一人说主句时，另一人的附和、感叹或说半句就停下的插话可以不出字幕  The host's sentence around 礼墨Sumi's 「好的好的」
# (heard 1.4-2.0 s while the recognitions have no time for it: the host's 「天」 and 「我」 0.1 s apart), the host's 「对对」 after 礼墨Sumi stops and before she goes on (1064220 「是吗？」: one after the other,
# no interjection), and 礼墨Sumi's question at the end of a host's line that holds four fifths of its words, after the
# host stops (a turn of its own: a line to cut off, never to leave out).
INTO_RUNS = [{"start_ms": 0, "end_ms": 1400, "cluster": 2}, {"start_ms": 1400, "end_ms": 2000, "cluster": 0},
             {"start_ms": 2000, "end_ms": 4000, "cluster": 2},
             {"start_ms": 4000, "end_ms": 8000, "cluster": 0}, {"start_ms": 8000, "end_ms": 8600, "cluster": 2},
             {"start_ms": 8600, "end_ms": 12000, "cluster": 0}, {"start_ms": 12000, "end_ms": 14000, "cluster": 2},
             {"start_ms": 14000, "end_ms": 15000, "cluster": 0}, {"start_ms": 15000, "end_ms": 16000, "cluster": 2}]
INTO_FINALS = [(0, 4000, "要不我们今天，好的好的， 我们今天先玩到这。"), (4000, 8000, "好的我马上就过来了"), (8000, 8600, "对对"),
               (8600, 12000, "等一下我还没准备好"), (12000, 15000, "我们先去那边看看好不好吧。绿在哪？"), (15000, 16000, "红黄蓝")]
INTO_UTTERANCES = [(0, 4000, [*_spread(100, 1700, "要不我们今天"), *_spread(1800, 3900, "我们今天先玩到这")]),
                   (4000, 8000, _spread(4100, 7900, "好的我马上就过来了")), (8000, 8600, _spread(8100, 8500, "对对")),
                   (8600, 12000, _spread(8700, 11900, "等一下我还没准备好")),
                   (12000, 15000, [*_spread(12100, 13900, "我们先去那边看看好不好吧"), *_spread(14100, 14700, "绿在哪")]),
                   (15000, 16000, _spread(15100, 15900, "红黄蓝"))]


def _into_lines():
    return [{"start_ms": a, "end_ms": b, "text": t} for a, b, t in INTO_FINALS]


def test_a_phrase_said_into_another_person_s_sentence_is_a_candidate_for_the_review():
    segments, _named, persons = arrival.engine({"runs": INTO_RUNS}, TRIO_PERSONS, "李豆沙")
    letters = {"bcut": arrival.recognised_letters(_bcut(INTO_UTTERANCES))}
    got = arrival.interjections(_into_lines(), segments, persons, letters)
    assert [(c["line"], c["main"], c["by"], c["text"], c["whole"], c["cut"], c["together"]) for c in got] == [
        (0, "李豆沙", "礼墨Sumi", "好的好的", False, None, True),  # 礼墨Sumi heard 1.4-2.0 s, its letters untimed, no room for them
        (4, "李豆沙", "礼墨Sumi", "绿在哪", False, 14100, False)]  # at the line's end, after the host stops: only a cut
    assert INTO_FINALS[0][2][slice(*got[0]["span"])] == "好的好的， "
    # 「对对」 between 礼墨Sumi's own lines is said after she stops and before she goes on: a reply, no candidate
    assert 2 not in [c["line"] for c in got]
    # time for it between the host's recognised words, and her voice not heard through it: said in her pause, no candidate
    paused = [(0, 4000, [*_spread(100, 1300, "要不我们今天"), *_spread(2000, 3900, "我们今天先玩到这")]), *INTO_UTTERANCES[1:]]
    pause_letters = {"bcut": arrival.recognised_letters(_bcut(paused))}
    assert [c["line"] for c in arrival.interjections(_into_lines(), segments, persons, pause_letters)] == [4]
    # the host's words go on around letters the recognitions do not time, with no gap in her voice: no candidate
    steady = [{"start_ms": 0, "end_ms": 4000, "cluster": 2}, *INTO_RUNS[3:]]
    segments, _named, persons = arrival.engine({"runs": steady}, TRIO_PERSONS, "李豆沙")
    assert [c["line"] for c in arrival.interjections(_into_lines(), segments, persons, letters)] == [4]
    # a line of two people's words one after the other is not one person's sentence: cut by the words as before
    two = [*INTO_RUNS[:6], {"start_ms": 12000, "end_ms": 13000, "cluster": 2}, {"start_ms": 13000, "end_ms": 15000, "cluster": 0}, INTO_RUNS[8]]
    segments, _named, persons = arrival.engine({"runs": two}, TRIO_PERSONS, "李豆沙")
    assert 4 not in [c["line"] for c in arrival.interjections(_into_lines(), segments, persons, letters)]


def test_a_stretch_where_nobody_is_heard_is_no_interjection():
    # 897000 「挖矿挖的」 (the owner's note of 安晚awa's main sentence, the host's words behind it need no subtitle):
    # two voices at once match neither path and the recognitions have no time for the words; nothing says whose
    # sentence it is, so it is no candidate and its words stay
    gap = [r for r in INTO_RUNS if r["start_ms"] != 1400]
    segments, _named, persons = arrival.engine({"runs": gap}, TRIO_PERSONS, "李豆沙")
    letters = {"bcut": arrival.recognised_letters(_bcut(INTO_UTTERANCES))}
    lines = [dict(r, text=r["text"].replace("好的好的", "挖矿挖的")) for r in _into_lines()]
    assert [c["line"] for c in arrival.interjections(lines, segments, persons, letters)] == [4]


def test_a_pause_in_the_main_speaker_s_talk_is_no_one_else_where_no_word_is_timed():
    # 397290 「没事」 in 礼墨Sumi's sentence, the clip without recognised letter times: only her path is heard around a
    # 0.4 s pause, so nothing says another voice is there -- with no letter timed, "its letters on no stretch" holds of
    # any line, her own included.  Another person heard in the pause still makes it a candidate, said while she talks.
    lines = [{"start_ms": 0, "end_ms": 2000, "text": "我们先去那边"}, {"start_ms": 2000, "end_ms": 3000, "text": "没事"},
             {"start_ms": 3000, "end_ms": 5000, "text": "那只能这样了呀"}]
    paused = [{"start_ms": 0, "end_ms": 2300, "cluster": 2}, {"start_ms": 2700, "end_ms": 5000, "cluster": 2}]
    segments, _named, persons = arrival.engine({"runs": paused}, TRIO_PERSONS, "李豆沙")
    assert arrival.interjections(lines, segments, persons, {}) == []
    heard = [paused[0], {"start_ms": 2300, "end_ms": 2700, "cluster": 0}, paused[1]]
    segments, _named, persons = arrival.engine({"runs": heard}, TRIO_PERSONS, "李豆沙")
    got = arrival.interjections(lines, segments, persons, {})
    assert [(c["line"], c["main"], c["by"], c["whole"], c["together"]) for c in got] == [(1, "李豆沙", "礼墨Sumi", True, True)]

def _into_step4(tmp_path, reading, owner_lines=(), finals=INTO_FINALS):
    cpa = _Cpa(reading)
    ctx = make_ctx(tmp_path, remote=_host(doc=dict(DOC, runs=INTO_RUNS)), cpa=cpa, session=TRIO)
    ctx.remote_dir = "/r/clip"
    if owner_lines:        # the owner's own labels of these lines (listen-import), the clip's export file
        cid, at = ctx.state.data["clip"]["cid"], int(ctx.state.data["clip"]["start_ms"])
        export = tmp_path / "reviewer_labels.json"
        export.write_text(json.dumps({"labels": {f"{cid}:{n + 1}": {"speaker": sv.HOST if n == 2 else "礼墨Sumi",
                                                                   "abs_start_ms": at + finals[n][0], "abs_end_ms": at + finals[n][1]}
                                                 for n in owner_lines}}), encoding="utf-8")
        ctx.state.data["reviewer_labels"] = {"export_file": str(export)}
    for name in ("asr", "subs", "speaker"):
        (ctx.clip_dir / name).mkdir(parents=True, exist_ok=True)
    (ctx.clip_dir / "asr/bcut.json").write_text(json.dumps(_bcut(INTO_UTTERANCES)), encoding="utf-8")
    (ctx.clip_dir / "asr/bcut.srt").write_text("".join(f"{n}\n{si.fmt_ts(a / 1000)} --> {si.fmt_ts(b / 1000)}\n{''.join(w[0] for w in ws)}\n\n"
                                                       for n, (a, b, ws) in enumerate(INTO_UTTERANCES, 1)), encoding="utf-8")
    (ctx.clip_dir / "subs/final.srt").write_text("".join(f"{n}\n{si.fmt_ts(a / 1000)} --> {si.fmt_ts(b / 1000)}\n{t}\n\n"
                                                         for n, (a, b, t) in enumerate(finals, 1)), encoding="utf-8")
    units = [{"id": f"u{n}", "s": a, "e": b, "lw": b, "cue": n} for n, (a, b, _t) in enumerate(INTO_FINALS)]
    (ctx.clip_dir / "speaker/chunks-units.json").write_text(json.dumps({"units": units}), encoding="utf-8")
    preds = [{"id": u["id"], "ok": True, "probs": {"李豆沙": p}} for u, p in zip(units, (0.95, 0.05, 0.95, 0.05, 0.95, 0.95), strict=True)]
    (ctx.clip_dir / "speaker/chunks-pred.json").write_text(json.dumps({"predictions": preds}), encoding="utf-8")
    out = si.step4_speaker(ctx)
    assert out.ok, out.note
    return ctx, out, cpa


def test_step_4_leaves_out_an_interjection_the_review_reads_as_one_and_keeps_every_other_line(tmp_path):
    reading = [{"插": "插1", "kind": "附和", "reason": "礼墨在应声"}, {"插": 2, "kind": "另一人的话", "reason": "礼墨在问绿色在哪"}]
    ctx, out, cpa = _into_step4(tmp_path, reading)
    review = [p for p in cpa.asked if p.startswith("# 说话人复核")]
    assert len(review) == 1 and "插1：第 1 句" in review[0] and "李豆沙说这几个字的时候也在说话（同时说）" in review[0]
    assert "插2：第 5 句" in review[0] and "不是同时说（一个说完另一个接）：不会去掉" in review[0] and "插3" not in review[0]
    track = st.read_track(ctx.clip_dir)
    assert [(c["text"], c["speaker"]) for c in track] == [
        ("要不我们今天，我们今天先玩到这。", sv.HOST), ("好的我马上就过来了", sv.GUEST_LABEL), ("对对", sv.HOST),
        ("等一下我还没准备好", sv.GUEST_LABEL), ("我们先去那边看看好不好吧。", sv.HOST), ("绿在哪？", sv.GUEST_LABEL), ("红黄蓝", sv.HOST)]
    assert (track[5]["start_s"], track[5]["end_s"]) == (14.1, 15.0)
    rows = json.loads((ctx.clip_dir / "speaker/interjections.json").read_text(encoding="utf-8"))
    assert [(r["line"], r["phrase"], r["main"], r["by"], r["kind"], r["action"]) for r in rows] == [
        (1, "好的好的", "李豆沙", "礼墨Sumi或安晚awa", "附和", "dropped"), (5, "绿在哪", "李豆沙", "礼墨Sumi或安晚awa", "另一人的话", "split")]
    assert any("不出字幕 1 处" in p and "拆开 1 处" in p for p in out.process)
    # a reply after the other one stops is never left out, whatever the reading says of it; no reading keeps every word
    ctx, out, _cpa = _into_step4(tmp_path / "kept", [{"插": 2, "kind": "附和"}])
    assert [c["text"] for c in st.read_track(ctx.clip_dir)] == [t for _a, _b, t in INTO_FINALS]
    assert {r["action"] for r in json.loads((ctx.clip_dir / "speaker/interjections.json").read_text(encoding="utf-8"))} == {"kept"}


def test_a_clip_the_owner_labelled_is_not_touched_by_the_interjection_rule(tmp_path):
    # the owner's labels are confirmed : one line he labelled locks the whole
    # clip -- no candidate is read, nothing is left out or cut by it, the lines he did not label included
    reading = [{"插": 1, "kind": "附和"}, {"插": 2, "kind": "另一人的话"}]
    ctx, out, cpa = _into_step4(tmp_path, reading, owner_lines=(2,))
    assert not any("插1" in p for p in cpa.asked)
    assert [c["text"] for c in st.read_track(ctx.clip_dir)] == [t for _a, _b, t in INTO_FINALS]
    assert any("这条片有频道主的标注" in p and "2 处候选" in p for p in out.process)
    assert not (ctx.clip_dir / "speaker/interjections.json").exists() or \
        json.loads((ctx.clip_dir / "speaker/interjections.json").read_text(encoding="utf-8")) == []


def test_a_clip_he_labelled_by_name_is_locked_as_his_session_samples_read_it(tmp_path, monkeypatch):
    # his labels by name in the speaker truth store only (no listen-import, no import): the same reading as the guest
    # names and the session samples (guest_identity.state_owner_rows) locks the clip
    from src.autoslice.manual_lane import channel

    reading = [{"插": 1, "kind": "附和"}, {"插": 2, "kind": "另一人的话"}]
    store = tmp_path / "speaker_truth"
    store.mkdir()
    day = TRIO["recording_date"]
    (store / f"{day}.owner-x.json").write_text(json.dumps({"set": f"{day}.owner-x", "tier": "owner", "supersedes": None,
        "rows": [{"clip": "clip_r2", "start_ms": INTO_FINALS[1][0], "end_ms": INTO_FINALS[1][1], "speaker": "礼墨Sumi",
                  "text": INTO_FINALS[1][2]}]}, ensure_ascii=False), encoding="utf-8")   # labelled on a revision: one family
    monkeypatch.setattr(channel, "asset", lambda name: tmp_path / name)
    ctx, out, cpa = _into_step4(tmp_path / "clip", reading)
    assert not any("插1" in p for p in cpa.asked)
    assert [c["text"] for c in st.read_track(ctx.clip_dir)] == [t for _a, _b, t in INTO_FINALS]
    assert any("这条片有频道主的标注" in p for p in out.process)


def test_a_question_said_into_another_person_s_sentence_is_never_left_out(tmp_path):
    # 1064220 「是吗？」, answered by the next words 「哦哦，还真是」: a question taken up has a meaning of its own
    finals = [(0, 4000, "要不我们今天，是吗？ 我们今天先玩到这。"), *INTO_FINALS[1:]]
    ctx, _out, _cpa = _into_step4(tmp_path, [{"插": 1, "kind": "附和"}], finals=finals)
    assert st.read_track(ctx.clip_dir)[0]["text"] == finals[0][2]
    rows = json.loads((ctx.clip_dir / "speaker/interjections.json").read_text(encoding="utf-8"))
    assert [(r["phrase"], r["kind"], r["action"]) for r in rows][0] == ("是吗", "附和", "kept")
