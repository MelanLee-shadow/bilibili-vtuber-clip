"""TASK-29: the coordinator's rulings on TASK-28.  Each test fails on ed5428bb1 and, except the ruling-2 test (a TASK-28
rule kept, checked here on the production ladder), on the TASK-28 working tree.  Synthetic texts only."""

from __future__ import annotations

import json
import re

from fakes import SESSION, FakeRemote, IdCpa, make_ctx, srt

from src.autoslice.manual_lane import speaker_v8 as sv
from src.autoslice.manual_lane import stage_impl as si
from src.autoslice.manual_lane import suspicion
from src.autoslice.manual_lane.adapters import RemoteResult
from src.autoslice.manual_lane.step3_evidence import whole_witnesses


# ----- ruling 1: the production defaults stay those of ed5428bb1 (TASK-11) --------------------------------------------
def test_the_default_ladder_and_whole_clip_witnesses_are_those_of_ed5428bb1(monkeypatch):
    from src.autoslice.manual_lane.lane_rules import RULES_FILE

    for name in ("MANUAL_LANE_LADDER", "MANUAL_LANE_WHOLE_WITNESS", "MANUAL_LANE_RULES"):
        monkeypatch.delenv(name, raising=False)
    listening = json.loads(RULES_FILE.read_text(encoding="utf-8"))["listening"]
    assert (listening["ladder"], listening["whole_witness"]) == ("asr_first", "mai,moss")
    assert suspicion.ladder() == suspicion.DEFAULT_LADDER == "asr_first"
    assert suspicion.escalation_levels() == (("L1", "asr", ("mai", "moss")), ("L2", "listen", ("38m",)),
                                             ("L3", "listen", ("argon",)))
    assert whole_witnesses() == ["mai", "moss"]


def test_with_engine_clusters_a_v8_mid_band_cue_is_no_acoustic_tie_by_itself():
    # the production step 4 (MOSS 8c1 clusters, MAI flags): the routes vote as at ed5428bb1; the mid band only makes a
    # tie in the v8-only review (no engine clusters)
    clustered = {"speaker": sv.HOST, "label_from": "moss", "v8_speaker": sv.HOST,
                 "clusters": {"moss": sv.HOST, "mai": sv.GUEST_LABEL}, "v8_mid": "tie", "speaker_confidence": "低"}
    assert not sv.acoustic_tie(clustered)
    alone = {"speaker": sv.HOST, "label_from": "v8", "clusters": {}, "v8_mid": "tie", "speaker_confidence": "低",
             "review_route": sv.MID_REVIEW}
    assert sv.acoustic_tie(alone)


# ----- ruling 2: on the production ladder, a doubt 3.8 heard nothing of goes to argon even when CPA wrote 高 ---------
def test_on_the_production_ladder_a_doubt_without_a_3_8_reading_still_goes_to_argon(tmp_path, monkeypatch):
    from test_task11_witnesses_cover_lone import _ladder_ctx

    high = [{"id": "d001", "state": "KEEP", "final_text": "", "confidence": "高", "affects_meaning": False}]
    ctx, calls, _cpa = _ladder_ctx(tmp_path, monkeypatch, "asr_first", {"adjudicate-c-L2-1": high})
    out = si.step3_subtitles(ctx)
    assert out.ok, (out.reason, out.note)
    assert "s3-asrwin-C-L1" in calls
    assert [c for c in calls if c.startswith("s3-listen-")] == ["s3-listen-C-L2", "s3-listen-C-L3"]


# ----- ruling 3: an alias is written only when the alias itself is said (proper_nouns.json notes) ---------------------
class _Cpa:
    def __init__(self):
        self.prompts: dict[str, str] = {}

    def ask(self, *, call_id, prompt_text, model, effort, remote_dir):
        self.prompts[call_id] = prompt_text
        if call_id == "review-c":
            text = json.dumps({"suspicious": []})
        else:
            ids = list(dict.fromkeys(re.findall(r'"id": "([^"]+)"', prompt_text)))
            text = json.dumps([{"id": i, "state": "KEEP", "final_text": "", "confidence": "高",
                                "affects_meaning": False} for i in ids])
        return {"call_id": call_id, "model": model, "effort": effort, "rc": 0, "elapsed_s": 0.1,
                "response_text": text, "stderr_tail": ""}


def _two_line_srt(texts):
    return "\n\n".join(f"{i}\n{si.fmt_ts(2.0 * (i - 1))} --> {si.fmt_ts(2.0 * i)}\n{t}"
                       for i, t in enumerate(texts, 1)) + "\n"


def test_a_reversed_name_said_in_chinese_is_written_in_chinese_and_the_prompt_carries_its_note(tmp_path, monkeypatch):
    monkeypatch.setenv("MANUAL_LANE_ADJUDICATE_MODE", "C")
    bcut = _two_line_srt(["今天开始了", "我们沙豆李队赢了", "下一把再来"])
    jianying = _two_line_srt(["今天开始了", "我们下斗里队赢了", "下一把再来"])

    def script(name, body, remote):
        if name == "s3-asr":
            return RemoteResult(0, "ASR_OK\n", "")
        if name.startswith("s3-listen-"):
            remote.files["/r/clip/listen/out/all.json"] = b"[]"
            return RemoteResult(0, 'LISTEN_DONE 0 records 0 {}\n', "")
        return RemoteResult(0, "", "")
    remote = FakeRemote(on_script=script)
    remote.files["/r/clip/asr/bcut.srt"], remote.files["/r/clip/asr/jianying.srt"] = bcut.encode(), jianying.encode()
    cpa = _Cpa()
    ctx = make_ctx(tmp_path, remote=remote, cpa=cpa, bcut_text=bcut)
    out = si.step3_subtitles(ctx)
    assert out.ok, (out.reason, out.note)
    prompt = next(p for k, p in cpa.prompts.items() if k != "review-c" and "沙豆李" in p and '"id"' in p)
    note = next(r["note"] for r in json.loads(si.PROPER_NOUNS.read_text(encoding="utf-8")) if r["name"] == "沙豆李")
    assert note in prompt                                            # the entry's note reaches CPA
    for rule in ("同指不同昵称各自保留", "指称未定时不套表", "参与者、误听对照和弹幕只提供候选和证据"):
        assert rule in prompt, rule
    assert "说的是哪个叫法（正式名或别名）就写哪个叫法" not in prompt     # TASK-28's wording invited Shadow Lee
    final = (ctx.clip_dir / "subs" / "final.srt").read_text(encoding="utf-8")
    assert "沙豆李" in final and "Shadow" not in final


# ----- rulings 5 and 7: the v8-only mid-band review shows no label for a tie; its own mode ---------------------------
def _pred(uid, host):
    return {"id": uid, "ok": True, "probs": {"李豆沙": host, "礼墨Sumi": 0.0, "安晚awa": round(1 - host, 2)}}


def _mid_review(tmp_path):
    """A two-person clip without MOSS/MAI clusters: v8 alone, cue 2 at host probability 0.55 (the mid band)."""
    workdir = f"{si.SPEAKER_V8_ROOT}/clip"

    def script(name, body, remote):
        if name == "s4-speaker-v8":
            return RemoteResult(0, "SPEAKER_V8_DONE\n", "")
        return RemoteResult(0, "", "")
    remote = FakeRemote(on_script=script)
    remote.files[f"{workdir}/out/pred.json"] = json.dumps(
        {"predictions": [_pred("clip-0", 0.95), _pred("clip-2000", 0.55), _pred("clip-4000", 0.05)]}).encode()
    session = dict(SESSION, participants=[["李豆沙", "小李"], ["雾深Girimi", "雾深"]])
    cpa = IdCpa([])
    ctx = make_ctx(tmp_path, remote=remote, cpa=cpa, session=session, bcut_text=srt(3, step=2.0))
    for sub, name in (("asr", "bcut.srt"), ("subs", "final.srt")):
        (ctx.clip_dir / sub).mkdir(parents=True, exist_ok=True)
        (ctx.clip_dir / sub / name).write_text(srt(3, step=2.0), encoding="utf-8")
    out = si.step4_speaker(ctx)
    assert out.ok, out.note
    return ctx, cpa


def test_the_v8_mid_band_review_shows_a_tie_without_the_v8_label(tmp_path):
    _ctx, cpa = _mid_review(tmp_path)
    review = next(p for c, p in zip(cpa.calls, cpa.prompts, strict=True) if c == "speaker-review")
    assert re.search(r"^2 \[\?\] 句子2$", review, re.M)                # the 0.55 cue: no v8 label to anchor on
    assert re.search(rf"^1 \[{sv.HOST}\] 句子1$", review, re.M) and re.search(rf"^3 \[{sv.GUEST_LABEL}\] 句子3$", review, re.M)


def test_a_v8_mid_band_review_without_clusters_has_its_own_mode_name(tmp_path):
    ctx, _cpa = _mid_review(tmp_path)
    decision = json.loads((ctx.clip_dir / "speaker" / "decision.json").read_text(encoding="utf-8"))
    assert decision["mode"] == "linked_v8_mid_review"


# ----- ruling 6: two letters or more, and no common English short word ------------------------------------------------
def test_short_or_common_english_letters_are_no_unknown_name():
    from src.autoslice.manual_lane import name_match
    from src.autoslice.manual_lane.lane_rules import rules

    names = name_match.entries([])
    cfg = rules("detection")
    assert cfg["latin_min_letters"] == 2 and {"ok", "no", "get", "my", "oh"} <= set(cfg["latin_common_words"])
    assert "TASK-29" in cfg["latin_source"]
    assert name_match.unknown_latin("ok OK no get S my oh B站", names) == []
    assert name_match.unknown_latin("这就是PUA吧，OK", names) == ["PUA"]
    assert name_match.detection_reasons("好的 ok 我 get 了", names, cfg) == []
