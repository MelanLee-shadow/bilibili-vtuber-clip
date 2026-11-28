"""Same-session voice samples (session_voices; the channel's request):
the other clips of a session give each person's voice — the owner's labels first, the arrival's sure lines elsewhere."""

import json

from src.autoslice.manual_lane import session_voices as sv

HOST, SUMI, AWA = "李豆沙", "礼墨Sumi", "安晚awa"
PARTICIPANTS = [[HOST, "小李"], [SUMI], [AWA]]
SEGMENTS = [{"start_ms": 0, "end_ms": 2000, "speaker": "g0"}, {"start_ms": 2000, "end_ms": 4000, "speaker": "g2"},
            {"start_ms": 4000, "end_ms": 4600, "speaker": "g0"}, {"start_ms": 4600, "end_ms": 6000, "speaker": "g2"},
            {"start_ms": 6000, "end_ms": 8000, "speaker": "g1"}]
PERSONS = {"g0": SUMI, "g1": AWA, "g2": HOST}
LINES = [{"start_ms": 0, "end_ms": 2000, "text": "今天打什么"}, {"start_ms": 2000, "end_ms": 6000, "text": "打这个吧好不好"},
         {"start_ms": 6000, "end_ms": 8000, "text": "可以可以"}]


def _letters(lines):
    out = []
    for ln in lines:
        n = len(ln["text"])
        step = (ln["end_ms"] - ln["start_ms"]) // n
        out += [[ch, ln["start_ms"] + k * step, ln["start_ms"] + (k + 1) * step] for k, ch in enumerate(ln["text"])]
    return {"bcut": out}


EVIDENCE = {"segments": SEGMENTS, "persons": PERSONS, "letters": _letters(LINES)}


def test_a_line_is_a_sure_sample_only_when_one_voice_holds_its_window():
    # line 2's words are the host's, but 礼墨's 0.6 s inside its window is 15 % of the voice heard there
    assert sv.arrival_high(LINES, EVIDENCE) == {0: SUMI, 2: AWA}


def _clip(task, cid, *, day="2026-11-23", participants=PARTICIPANTS, owner=None, status="DONE", arrival=True):
    (task / "state").mkdir(parents=True, exist_ok=True)
    d = task / "clips" / cid
    (d / "subs").mkdir(parents=True, exist_ok=True)
    (d / "speaker").mkdir(parents=True, exist_ok=True)
    srt = "\n".join(f"{i}\n00:00:0{ln['start_ms'] // 1000},000 --> 00:00:0{ln['end_ms'] // 1000},000\n{ln['text']}\n"
                    for i, ln in enumerate(LINES, 1))
    (d / "subs" / "final.srt").write_text(srt, encoding="utf-8")
    if arrival:
        (d / "speaker" / "arrival.json").write_text(json.dumps(EVIDENCE, ensure_ascii=False), encoding="utf-8")
    imported = {}
    if owner is not None:
        p = d / "import" / "speakers.json"
        p.parent.mkdir()
        p.write_text(json.dumps(owner, ensure_ascii=False), encoding="utf-8")
        imported = {"decided_by": "owner", "files": {"speakers.json": str(p)}}
    state = {"schema": "manual-lane.clip-state.v1", "clip": {"cid": cid, "remote_dir": f"/host/{cid}", "status": status},
             "session": {"recording_date": day, "participants": participants}, "imported": imported}
    from src.autoslice.manual_lane.state import SCHEMA
    state["schema"] = SCHEMA
    (task / "state" / f"{cid}.json").write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")


def test_the_session_samples_come_from_the_other_clips_with_the_owner_over_the_arrival(tmp_path):
    _clip(tmp_path, "semantictalk_100_200_11232000")                        # the clip being made
    _clip(tmp_path, "semantictalk_100_200_11232000_r2")                     # its own revision: not a sample
    _clip(tmp_path, "semantictalk_300_400_11232100", owner=[{"start_s": 0.0, "end_s": 2.0, "text": "今天打什么", "speaker": AWA}])
    _clip(tmp_path, "semantictalk_500_600_11232130")
    _clip(tmp_path, "semantictalk_700_800_11242000", day="2026-11-24")     # another session
    _clip(tmp_path, "semantictalk_900_950_11232200", participants=[[HOST], [SUMI]])   # other people
    got = sv.session_samples(tmp_path, "semantictalk_100_200_11232000")
    rows = sorted((s["clip"].split("_")[1], s["start_ms"], s["person"], s["tier"]) for s in got)
    # 300: his label wins where it meets the arrival's line (he says 安晚, the arrival 礼墨); its other sure line stays
    assert rows == [("300", 0, AWA, "owner"), ("300", 6000, AWA, "arrival"),
                    ("500", 0, SUMI, "arrival"), ("500", 6000, AWA, "arrival")]
    assert all(s["remote_dir"].startswith("/host/") for s in got)


def _report(frozen, session, *, clips=("a", "b"), counts=None):
    keys = ("n", "person_err", "hg_err", "hi_n", "hi_err")
    return {"clips": list(clips), "counts": counts or {HOST: {"lines": 23, "seconds": 45.1}, SUMI: {"lines": 38, "seconds": 71.7}},
            "self_check": {"frozen": dict(zip(keys, frozen, strict=True)), "session": dict(zip(keys, session, strict=True))}}


def test_the_session_classifier_takes_over_only_where_its_self_check_on_his_lines_beats_the_frozen_one():
    # his labelled lines of the left-out clips: fewer errors everywhere -> switch
    ok, why = sv.switch(_report((62, 12, 9, 40, 2), (62, 8, 6, 45, 1)))
    assert ok and why == []
    # host/guest errors equal, sure-line errors 4/177 against 2/145 -> keep the frozen v8
    ok, why = sv.switch(_report((252, 28, 17, 145, 2), (252, 20, 17, 177, 4)))
    assert not ok and len(why) == 2 and why[0].startswith("host/guest errors 17")
    # one sample clip cannot be left out; a class with too little voice cannot be learnt; too few of his lines measured
    ok, why = sv.switch(_report((20, 6, 5, 10, 1), (20, 2, 1, 12, 0), clips=("a",),
                                counts={HOST: {"lines": 6, "seconds": 30.0}, SUMI: {"lines": 38, "seconds": 71.7}}))
    assert not ok and why[0].startswith("1 sample clips") and why[1].startswith(f"{HOST}: 6 lines")
    assert why[2].startswith("20 owner lines measured")
    # no fit (a class without any voice to train on): no self-check, no switch
    ok, why = sv.switch({"clips": ["a", "b"], "counts": {HOST: {"lines": 9, "seconds": 20.0}}, "self_check": None})
    assert not ok and why == ["no self-check"]


def _sample(clip, person, tier, start=0, end=3000, **kw):
    return dict(clip=clip, remote_dir=f"/host/{clip}", start_ms=start, end_ms=end, text="", person=person, tier=tier, **kw)


def test_every_participant_is_a_class_so_one_without_samples_is_seen_short():
    people = sorted([HOST, SUMI, AWA])
    # 安晚 speaks nowhere in the samples: still a class, and the precheck says it has no voice to learn
    got = [_sample("a", HOST, "owner"), _sample("a", SUMI, "owner"), _sample("b", SUMI, "arrival")]
    assert sv.classes_of(people, got) == [HOST, AWA, SUMI]
    doc = {"classes": sv.classes_of(people, got), "samples": [dict(x, cls=x["person"]) for x in got]}
    assert any(w.startswith(f"{AWA}: 0 lines") for w in sv.precheck(doc))
    # two guests on one shared path (11-24): the host and that path
    shared = [_sample("a", f"{SUMI}或{AWA}", "arrival", among=[SUMI, AWA]), _sample("a", AWA, "owner")]
    classes = sv.classes_of(people, shared)
    assert classes == [HOST, f"{SUMI}或{AWA}"]
    assert sv.class_of(AWA, classes, shared) == f"{SUMI}或{AWA}" and sv.class_of("其他", classes, shared) is None


def test_a_session_with_only_arrival_lines_keeps_the_frozen_v8_without_a_fit(tmp_path):
    # 11-24: every sample is an arrival-high line, which step 4 labels by the arrival anyway: nothing to measure on
    _clip(tmp_path, "semantictalk_100_200_11232000")
    _clip(tmp_path, "semantictalk_500_600_11232130")
    _clip(tmp_path, "semantictalk_700_800_11232200")
    doc = sv.samples_doc(tmp_path, "semantictalk_100_200_11232000")
    assert {s["tier"] for s in doc["samples"]} == {"arrival"}
    assert any("0 owner-labelled lines" in w for w in sv.precheck(doc))


def test_his_labels_in_the_truth_store_are_samples_where_the_clip_has_none(tmp_path, monkeypatch):
    from src.autoslice.manual_lane import channel

    store = tmp_path / "speaker_truth"
    store.mkdir()
    row = {"clip": "semantictalk_300_400_11232100", "start_ms": 6000, "end_ms": 8000, "text": "可以可以", "speaker": SUMI}
    old = {"set": "2026-11-23.owner-a", "tier": "owner", "supersedes": None, "rows": [{**row, "speaker": AWA}]}
    new = {"set": "2026-11-23.owner-b", "tier": "owner", "supersedes": "2026-11-23.owner-a",
           "rows": [row, {**row, "clip": "semantictalk_500_600_11232130", "start_ms": 2000, "end_ms": 6000, "speaker": HOST}]}
    weak = {"set": "2026-11-23.arrival-high", "tier": "arrival", "supersedes": None, "rows": [{**row, "speaker": HOST}]}
    for d in (old, new, weak):
        (store / f"{d['set']}.json").write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(channel, "asset", lambda name: tmp_path / name)
    assert sorted(sv.truth_owner_rows("2026-11-23")) == ["semantictalk_300_400_11232100", "semantictalk_500_600_11232130"]
    task = tmp_path / "task"
    _clip(task, "semantictalk_100_200_11232000")
    _clip(task, "semantictalk_300_400_11232100_r2", owner=[{"start_s": 0.0, "end_s": 2.0, "text": "今天打什么", "speaker": AWA}])
    _clip(task, "semantictalk_300_400_11232100", status="SUPERSEDED")
    _clip(task, "semantictalk_500_600_11232130_r3", arrival=False)
    _clip(task, "semantictalk_500_600_11232130_r10", arrival=False)    # the live revision is the highest number
    got = sv.session_samples(task, "semantictalk_100_200_11232000")
    rows = sorted((s["clip"].rsplit("_", 1)[1], s["start_ms"], s["person"], s["tier"]) for s in got)
    # 300: the import's label, then the store's newest set over the line the import left (it beats the arrival there)
    assert rows == [("r10", 2000, HOST, "owner"), ("r2", 0, AWA, "owner"), ("r2", 6000, SUMI, "owner")]


def _linked_ctx(tmp_path, monkeypatch, on_script):
    """The clip being made in a task whose other clips of the session are 300 (his labels) and 500 (arrival only)."""
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).parent))
    from fakes import FakeRemote

    from src.autoslice.manual_lane.adapters import Adapters
    from src.autoslice.manual_lane.runner import Ctx
    from src.autoslice.manual_lane.state import ClipState

    cid = "semantictalk_100_200_11232000"
    _clip(tmp_path, cid)
    _clip(tmp_path, "semantictalk_300_400_11232100", owner=[{"start_s": 0.0, "end_s": 2.0, "text": "今天打什么", "speaker": AWA},
                                                           {"start_s": 2.0, "end_s": 6.0, "text": "打这个吧好不好", "speaker": HOST}])
    _clip(tmp_path, "semantictalk_500_600_11232130")
    monkeypatch.setattr(sv, "rules", lambda: {"arrival_high_share": 0.9, "min_clips": 2, "per_class_lines": 1,
                                              "per_class_seconds": 1, "owner_lines_min": 2})
    state = ClipState.load(tmp_path / "state" / f"{cid}.json")
    remote = FakeRemote(on_script=on_script)
    return Ctx(state=state, session=state.data["session"], clip_dir=tmp_path / "clips" / cid, remote_dir="/r/clip",
               adapters=Adapters(remote=remote)), remote


def test_step_4_runs_the_session_classifier_when_its_self_check_passes_and_reuses_the_choice(tmp_path, monkeypatch):
    from src.autoslice.manual_lane.adapters import RemoteResult
    from src.autoslice.manual_lane.layout import layout

    keys = ("n", "person_err", "hg_err", "hi_n", "hi_err")

    def fit(name, body, remote):
        work = body.split("--out ")[1].split()[0]
        report = {"clips": ["semantictalk_300_400_11232100", "semantictalk_500_600_11232130"],
                  "counts": {c: {"lines": 2, "seconds": 4.0} for c in (HOST, AWA, SUMI)},
                  "self_check": {"frozen": dict(zip(keys, (2, 1, 1, 1, 0), strict=True)),
                                 "session": dict(zip(keys, (2, 0, 0, 2, 0), strict=True))},
                  "version": f"{work}/classifier"}
        remote.files[f"{work}/session.json"] = json.dumps(report).encode()
        return RemoteResult(0, "SESSION_VOICES_DONE\n", "")

    ctx, remote = _linked_ctx(tmp_path, monkeypatch, fit)
    env = sv.v8_env(ctx)
    (name, body), = remote.scripts
    assert name == "s4-session-voices" and "--host-min-known 0.8" in body and "/r/clip/speaker/speaker_truth.py session" in body
    sent = json.loads(remote.files["/r/clip/speaker/session-samples.json"])
    assert sent["classes"] == [HOST, AWA, SUMI] and {s["tier"] for s in sent["samples"]} == {"owner", "arrival"}
    work = body.split("--out ")[1].split()[0]
    assert f"SPEAKER_V8_CODE_DIR={work}/classifier " in env and f"SPEAKER_V8_SCORE={work}/classifier/score.py " in env
    assert f"SPEAKER_V8_PYTHON={layout().speaker_v8['python']} " in env
    assert "本场分类器" in sv.note(ctx)
    # the same samples: the recorded choice, no second fit
    assert sv.v8_env(ctx) == env and len(remote.scripts) == 1
    # he labels one more line of 500: the samples changed, decided again
    _clip(tmp_path, "semantictalk_500_600_11232130", owner=[{"start_s": 6.0, "end_s": 8.0, "text": "可以可以", "speaker": SUMI}])
    sv.v8_env(ctx)
    assert len(remote.scripts) == 2


def test_step_4_keeps_the_frozen_v8_when_the_self_check_does_not_pass(tmp_path, monkeypatch):
    from src.autoslice.manual_lane.adapters import RemoteResult
    from src.autoslice.manual_lane.step4_speaker import SPEAKER_V8_ENV

    keys = ("n", "person_err", "hg_err", "hi_n", "hi_err")

    def fit(name, body, remote):
        work = body.split("--out ")[1].split()[0]
        report = {"clips": ["a", "b"], "counts": {c: {"lines": 2, "seconds": 4.0} for c in (HOST, AWA, SUMI)},
                  "self_check": {"frozen": dict(zip(keys, (2, 1, 1, 1, 0), strict=True)),
                                 "session": dict(zip(keys, (2, 1, 1, 1, 0), strict=True))}, "version": f"{work}/classifier"}
        remote.files[f"{work}/session.json"] = json.dumps(report).encode()
        return RemoteResult(0, "SESSION_VOICES_DONE\n", "")

    ctx, _remote = _linked_ctx(tmp_path, monkeypatch, fit)
    assert sv.v8_env(ctx) == SPEAKER_V8_ENV
    rec = json.loads((ctx.clip_dir / sv.RECORD).read_text(encoding="utf-8"))
    assert rec["decision"] == "KEEP_FROZEN" and rec["why"][0].startswith("host/guest errors 1 not below")
    assert sv.note(ctx).startswith("分类器 v8 冻结（") and "host/guest errors" in sv.note(ctx)


def test_a_failed_fit_stops_the_choice_instead_of_falling_back(tmp_path, monkeypatch):
    import pytest

    from src.autoslice.manual_lane.adapters import RemoteResult

    ctx, _remote = _linked_ctx(tmp_path, monkeypatch, lambda name, body, remote: RemoteResult(1, "", "boom"))
    with pytest.raises(sv.SessionVoicesError):
        sv.v8_env(ctx)
    assert not (ctx.clip_dir / sv.RECORD).exists()


def test_the_chunk_run_is_made_again_when_the_classifier_changes(tmp_path, monkeypatch):
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).parent))
    from fakes import FakeRemote, make_ctx

    from src.autoslice.manual_lane import step4_speaker
    from src.autoslice.manual_lane.adapters import RemoteResult
    from src.autoslice.manual_lane.srt_text import parse_srt

    def run(name, body, remote):
        workdir = body.split()[-2]
        remote.files[f"{workdir}/out/pred.json"] = json.dumps({"predictions": []}).encode()
        return RemoteResult(0, "SPEAKER_V8_DONE\n", "")

    remote = FakeRemote(on_script=run)
    ctx = make_ctx(tmp_path, remote=remote, cpa=None)
    bcut = parse_srt("1\n00:00:01,000 --> 00:00:03,000\n一句\n")
    envs = iter(["SPEAKER_V8_CODE_DIR=/frozen ", "SPEAKER_V8_CODE_DIR=/frozen ", "SPEAKER_V8_CODE_DIR=/session "])
    monkeypatch.setattr(step4_speaker.session_voices, "v8_env", lambda _ctx: next(envs))
    step4_speaker.voice_chunks(ctx, bcut)
    step4_speaker.voice_chunks(ctx, bcut)          # same units, same classifier: reused
    step4_speaker.voice_chunks(ctx, bcut)          # same units, another classifier: made again with it
    assert [b.split()[0] for _n, b in remote.scripts] == ["SPEAKER_V8_CODE_DIR=/frozen", "SPEAKER_V8_CODE_DIR=/session"]
