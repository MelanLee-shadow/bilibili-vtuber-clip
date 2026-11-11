"""TASK-11 item 6 (U144, U150): step 4 names the whole-clip MOSS / MAI anonymous speaker clusters by the v8 host
voiceprint (enrol first, then name the clusters: the commercial practice), takes the majority of the three witnesses,
and asks CPA only about the cues where they disagree or an engine hears two people; a cue is cut between two speakers
only at an engine's switch and only when CPA's two parts rebuild its text.  Also: an answer a step cannot read is not
kept in the CPA cache."""

from __future__ import annotations

import json

from fakes import FakeRemote, make_ctx

from src.autoslice.manual_lane import speaker_v8 as sv
from src.autoslice.manual_lane import stage_impl as si


def _v8_rows():
    return [{"start_ms": 0, "end_ms": 3000, "speaker": sv.HOST, "conf": 0.9, "units": 2},
            {"start_ms": 3000, "end_ms": 4000, "speaker": sv.HOST, "conf": 0.7, "units": 1},   # v8 wrong here
            {"start_ms": 4000, "end_ms": 8000, "speaker": sv.GUEST_LABEL, "conf": 0.8, "units": 3}]


def _segs():
    return [{"start_ms": 0, "end_ms": 3000, "speaker": "S1"}, {"start_ms": 3000, "end_ms": 4000, "speaker": "S2"},
            {"start_ms": 4000, "end_ms": 6500, "speaker": "S2"}, {"start_ms": 6500, "end_ms": 8000, "speaker": "S1"}]


def _cues():
    return [{"start_ms": r["start_ms"], "end_ms": r["end_ms"]} for r in _v8_rows()]


def test_the_voiceprint_named_moss_cluster_labels_the_cue_and_disagreement_is_flagged():
    decided, segs = _v8_rows(), _segs()
    names = sv.name_clusters(segs, decided)
    assert names["S1"]["speaker"] == sv.HOST and names["S2"]["speaker"] == sv.GUEST_LABEL
    eng = sv.cue_clusters(_cues(), segs, names)
    fused = sv.fuse(decided, {"moss": eng, "mai": eng})
    assert [f["speaker"] for f in fused] == [sv.HOST, sv.GUEST_LABEL, sv.GUEST_LABEL]   # 8c1: MOSS's label
    assert fused[1]["v8_speaker"] == sv.HOST                                 # v8 kept beside it
    assert sv.name_by_voiceprint({"S1": {"cos": 0.31}, "S2": {"cos": 0.72}, "S3": {"cos": 0.40}}) == {
        "S1": {"speaker": sv.GUEST_LABEL, "cos": 0.31}, "S2": {"speaker": sv.HOST, "cos": 0.72},
        "S3": {"speaker": sv.GUEST_LABEL, "cos": 0.4}}                      # only the closest cluster is the host
    assert sv.name_by_voiceprint({"S1": {"cos": 0.9}}) is None               # one cluster: no choice, v8 stays
    assert [f["speaker_confidence"] for f in fused] == ["高", "低", "低"]      # cue 3 holds two speakers
    assert sv.switch_times(_cues()[2], segs, names) == [6500]


def test_a_cue_is_cut_only_at_an_engine_switch_and_only_when_the_parts_rebuild_it():
    cue = {"start_ms": 4000, "end_ms": 7000}
    segs = [{"start_ms": 4000, "end_ms": 5000, "speaker": "S2"}, {"start_ms": 5000, "end_ms": 7000, "speaker": "S1"}]
    names = {"S1": {"speaker": sv.HOST}, "S2": {"speaker": sv.GUEST_LABEL}}
    split = {"first": "是啊，", "first_speaker": sv.GUEST_LABEL, "second": "真的吗", "second_speaker": sv.HOST}
    pieces = sv.split_at_switch(cue, "是啊，真的吗", split, [5000, 6500], segs, names)
    assert [(p["start_ms"], p["end_ms"], p["speaker"]) for p in pieces] == [(4000, 5000, sv.GUEST_LABEL),
                                                                           (5000, 7000, sv.HOST)]
    assert sv.split_at_switch(cue, "是啊，真的吗", split, [], segs, names) is None            # no switch: no cut
    assert sv.split_at_switch(cue, "是啊，真的吗", dict(split, second="真的"), [5000], segs, names) is None   # not rebuilt
    assert sv.split_at_switch(cue, "是啊，真的吗", dict(split, second_speaker=sv.GUEST_LABEL), [5000], segs,
                              names) is None


def test_step_4_asks_cpa_only_about_the_undecided_cues_and_unsure_is_the_guest(tmp_path):
    asked = []

    class Cpa:
        def ask(self, *, call_id, prompt_text, model, effort, remote_dir):
            asked.append(prompt_text)
            rows = [{"n": 2, "speaker": "不确定", "reason": "看不出"},
                    {"n": 3, "speaker": sv.HOST, "reason": "自称",
                     "split": {"first": "是啊，", "first_speaker": sv.GUEST_LABEL, "second": "真的吗",
                               "second_speaker": sv.HOST}}]
            return {"call_id": call_id, "model": model, "effort": effort, "rc": 0, "elapsed_s": 0.1,
                    "response_text": json.dumps(rows, ensure_ascii=False), "stderr_tail": ""}

    from src.autoslice.manual_lane.adapters import RemoteResult

    def script(name, body, remote):
        if name == "s4-moss-voiceprint":
            remote.files[f"{si.SPEAKER_V8_ROOT}/clip-moss/out/centroids.json"] = json.dumps(
                {"S1": {"n": 2, "dur_ms": 4500, "cos": 0.71}, "S2": {"n": 2, "dur_ms": 3500, "cos": 0.22}}).encode()
            return RemoteResult(0, "CLUSTER_COS_DONE\n", "")
        return RemoteResult(0, "", "")

    ctx = make_ctx(tmp_path, remote=FakeRemote(on_script=script), cpa=Cpa())
    (ctx.clip_dir / "witness").mkdir(parents=True)
    for name in ("moss", "mai"):
        (ctx.clip_dir / f"witness/{name}.json").write_text(json.dumps(
            {"status": "OK", "segments": [dict(s, text="x") for s in _segs()]}), encoding="utf-8")
    finals = [{"text": "我是李豆沙"}, {"text": "你好呀"}, {"text": "是啊，真的吗"}]
    rows, calls, process = si._speaker_clusters(ctx, finals, _cues(), _v8_rows())
    assert len(asked) == 1 and "2「你好呀」" in asked[0] and "1「我是李豆沙」" not in asked[0]
    assert "雾深Girimi（联动嘉宾" in asked[0] and "念题" not in asked[0]
    assert rows[0]["speaker"] == sv.HOST and rows[0]["speaker_confidence"] == "高"   # all agree: untouched
    assert rows[1]["speaker"] == sv.GUEST_LABEL                         # 不确定 on a decided cue: its acoustic label
    assert "现在的说话人来自声音证据" in asked[0] and "声音证据是默认值" in asked[0]
    assert any("与主播登记声纹余弦 0.71" in p for p in process)
    assert [p["speaker"] for p in rows[2]["split"]] == [sv.GUEST_LABEL, sv.HOST]
    assert any("两段说话人按声音证据 1 句" in p for p in process)


def test_without_clusters_v8_stands_and_no_cpa_is_asked(tmp_path):
    ctx = make_ctx(tmp_path, remote=FakeRemote(), cpa=None)
    rows, calls, process = si._speaker_clusters(ctx, [{"text": "a"}] * 3, _cues(), _v8_rows())
    assert [r["speaker"] for r in rows] == [r["speaker"] for r in _v8_rows()] and calls == [] and process == []


def test_an_answer_the_step_cannot_read_is_not_kept_in_the_cache(tmp_path):
    cache = tmp_path / "c.json"
    cache.write_text("{}", encoding="utf-8")
    out = si._fail("CPA_RESPONSE_UNPARSEABLE", "x", cpa=[{"call_id": "a", "cache_path": str(cache)}])
    assert not out.ok and not cache.exists()
    keep = tmp_path / "k.json"
    keep.write_text("{}", encoding="utf-8")
    si._fail("CPA_CALL_FAILED", "x", cpa=[{"call_id": "a", "cache_path": str(keep)}])
    assert keep.exists()


def test_the_session_keyword_reaches_the_adjudicator_with_its_context_note_and_is_not_a_person(tmp_path):
    lines = si._confusion_lines()
    assert "本场梗词（正式写法）：契约do爱（是契约恋爱的谐音改口" in lines and "不能把所有『独爱』都换成它" in lines
    ctx = make_ctx(tmp_path, remote=FakeRemote(), cpa=None)
    names, _ = si.speaker_context_lines(ctx)
    assert "契约do爱" not in names and "雾深Girimi" in names


def test_without_a_voiceprint_naming_of_moss_v8_labels_and_mai_only_flags(tmp_path):
    ctx = make_ctx(tmp_path, remote=FakeRemote(), cpa=None)       # the remote embedding never says CLUSTER_COS_DONE
    (ctx.clip_dir / "witness").mkdir(parents=True)
    for name in ("moss", "mai"):
        (ctx.clip_dir / f"witness/{name}.json").write_text(json.dumps(
            {"status": "OK", "segments": [dict(s, text="x") for s in _segs()]}), encoding="utf-8")

    class Cpa:
        def ask(self, *, call_id, prompt_text, model, effort, remote_dir):
            return {"call_id": call_id, "model": model, "effort": effort, "rc": 0, "elapsed_s": 0.1,
                    "response_text": "[]", "stderr_tail": ""}

    ctx.adapters.cpa = Cpa()
    rows, _calls, process = si._speaker_clusters(ctx, [{"text": "a"}, {"text": "b"}, {"text": "c"}], _cues(), _v8_rows())
    assert rows[0]["speaker"] == sv.HOST and rows[0]["speaker_confidence"] == "高"   # MOSS absent is no reason
    assert "moss" not in rows[0]["clusters"] and any("MOSS 无可用簇，标签用 v8" in p for p in process)
