"""TASK-28: the e05 coach checklist written into the generator (assets/manual_lane/lane_rules.json).  One test per rule;
each fails on the code before TASK-28 (ed5428bb1).  Synthetic texts only."""

from __future__ import annotations

import json
import re

from fakes import FakeRemote, make_ctx, srt

from src.autoslice.manual_lane import doubt_policy, speaker_v8, suspicion
from src.autoslice.manual_lane import stage_impl as si
from src.autoslice.manual_lane.adapters import RemoteResult
from src.autoslice.manual_lane.normalize import numerals


def rules(section):   # imported per test: the module is new in TASK-28, so each test fails on its own on the old code
    from src.autoslice.manual_lane.lane_rules import rules as read

    return read(section)


def _rules_file():
    from src.autoslice.manual_lane.lane_rules import RULES_FILE

    return RULES_FILE


def test_every_rule_section_names_its_coach_source():
    doc = json.loads(_rules_file().read_text(encoding="utf-8"))
    for name in ("detection", "listening", "candidates", "orphans", "split", "speaker"):
        assert "COACH-ADVICE e05" in doc[name]["source"], name



def test_bcut_and_jianying_differing_only_in_number_spelling_pronoun_or_repeats_are_not_a_doubt():
    assert not suspicion.asr_disagrees("他有五个", "她有5个")
    assert not suspicion.asr_disagrees("我我我觉得可以", "我觉得可以")
    assert not suspicion.asr_disagrees("就是这样了", "就是这样的")          # small words only
    assert suspicion.asr_disagrees("我觉得可以", "我觉得不行")


def test_jianying_empty_where_bcut_has_words_is_a_doubt():
    assert suspicion.one_side_empty("我觉得可以", "")
    assert not suspicion.one_side_empty("我觉得可以", "我觉得可以")



def test_a_near_homophone_of_a_table_name_and_unknown_letters_are_doubt_reasons():
    from src.autoslice.manual_lane import name_match

    names = name_match.entries([["李豆沙"], ["雾深Girimi", "连线"]])
    assert name_match.near_names("我跟乌深说好了", names) == ["雾深Girimi"]
    assert name_match.near_names("我跟雾深说好了", names) == []            # written as the name: no doubt
    reasons = name_match.detection_reasons("这就是PUA吧", names, rules("detection"))
    assert reasons and "PUA" in reasons[0]


def _remote(records_by_label, scripts):
    def script(name, body, remote):
        if name == "s3-asr":
            return RemoteResult(0, "ASR_OK\n", "")
        if name.startswith("s3-listen-"):
            label = name[len("s3-listen-"):]
            scripts.append(label)
            remote.files["/r/clip/listen/out/all.json"] = json.dumps(records_by_label.get(label, []),
                                                                     ensure_ascii=False).encode()
            return RemoteResult(0, 'LISTEN_DONE 1 records 1 {"OK": 1}\n', "")
        return RemoteResult(0, "", "")
    remote = FakeRemote(on_script=script)
    remote.files["/r/clip/asr/bcut.srt"] = remote.files["/r/clip/asr/jianying.srt"] = srt(3, step=2.0).encode()
    return remote


class _Cpa:
    def __init__(self):
        self.calls: list[str] = []

    def ask(self, *, call_id, prompt_text, model, effort, remote_dir):
        self.calls.append(call_id)
        if call_id == "review-c":
            text = json.dumps({"suspicious": [{"cue_id": 1, "reason": "读不通"}]})
        else:
            ids = list(dict.fromkeys(re.findall(r'"id": "([^"]+)"', prompt_text)))
            text = json.dumps([{"id": i, "state": "KEEP", "final_text": "", "confidence": "高",
                                "affects_meaning": False} for i in ids])
        return {"call_id": call_id, "model": model, "effort": effort, "rc": 0, "elapsed_s": 0.1,
                "response_text": text, "stderr_tail": ""}


def test_a_doubt_whose_window_gave_no_3_8_reading_goes_to_argon_even_when_cpa_wrote_high(tmp_path, monkeypatch):
    monkeypatch.setenv("MANUAL_LANE_ADJUDICATE_MODE", "C")
    scripts: list[str] = []
    name = {m["slug"]: m["name"] for m in si.LISTEN_MODELS}["argon"]
    records = {"C-L1": [], "C-L2": [{"win": "C000", "model": name, "slug": "argon", "status": "OK",
                                     "lines": [{"start_s": 0.0, "text": "句子1"}]}]}
    ctx = make_ctx(tmp_path, remote=_remote(records, scripts), cpa=_Cpa(), bcut_text=srt(3, step=2.0))
    out = si.step3_subtitles(ctx)
    assert out.ok, (out.reason, out.note)
    assert scripts == ["C-L1", "C-L2"]       # 3.8 heard nothing: CPA's 高 at L1 did not settle it


# ----- 3. selection: source counts do not infer quality from overlap ------------------------------------------------
def test_a_gemini_reading_counts_its_source_even_when_far_from_asrs():
    cands = doubt_policy.candidates({"jianying": "今天我们吃饭", "audio:38m@P0": "完全别的东西了"}, "今天我们吃饭")
    gem = next(c for c in cands if c["text"] == "完全别的东西了")
    assert gem["supporters"] == 1 and gem["families"] == ["gemini"] and "audio:38m@P0" in gem["sources"]
    near = doubt_policy.candidates({"audio:38m@P0": "今天我们吃面"}, "今天我们吃饭")
    assert next(c for c in near if c["text"] == "今天我们吃面")["supporters"] == 1



def _att(cues, segs):
    from src.autoslice.manual_lane import evidence_attach as ea

    return ea.attach_asr(cues, [{"start_s": a, "end_s": b, "text": t} for a, b, t in segs], source="jianying")


def test_a_short_or_repeated_orphan_is_not_subtitled_and_a_long_one_is_its_own_line():
    from src.autoslice.manual_lane import lane_lines

    cues = [{"id": 1, "start_s": 0.0, "end_s": 2.0, "text": "今天天气真好"},
            {"id": 2, "start_s": 10.0, "end_s": 12.0, "text": "我们出去玩吧"}]
    att = _att(cues, [(0.0, 2.0, "今天天气真好"), (2.5, 3.0, "好的"), (3.0, 3.8, "今天天气真好"),
                      (5.0, 6.5, "我去买个东西"), (10.0, 12.0, "我们出去玩吧")])
    plan = lane_lines.plan(cues, att)
    assert [o["text"] for o in plan.orphan_lines] == ["我去买个东西"]
    assert sorted(o["text"] for o in plan.orphan_dropped) == ["今天天气真好", "好的"]
    assert att.orphans == [] and att.text(1) == "今天天气真好"      # detached: no longer a reading of cue 1
    assert att.stats()["unhandled"] == 0


# ----- 5. repeat counts remain distinct facts for CPA ----------------------------------------------------------------
def test_a_reading_with_a_different_repeat_count_does_not_vote_for_current():
    cands = doubt_policy.candidates({"jianying": "我觉得可以", "audio:38m@P0": "我觉得可以"}, "我我我觉得可以")
    by = {c["text"]: c for c in cands}
    assert by["我我我觉得可以"]["families"] == ["current"]
    assert by["我觉得可以"]["families"] == ["asr:jianying", "gemini"]



def test_unknown_letter_spellings_are_listed_and_the_prompt_carries_the_name_rules():
    from src.autoslice.manual_lane import name_match

    names = name_match.entries([])
    rows = name_match.unknown_spellings(["他说NTR了", "又是NTR", "Girimi来了"], names, ["弹幕：ntr", "标题"])
    assert rows == []                                                # NTR is in the danmaku, Girimi is a name
    rows = name_match.unknown_spellings(["他说SHAW了"], names, ["弹幕"])
    assert rows == [{"spelling": "SHAW", "count": 1}]
    prompt = (_rules_file().parent / "prompts" / "subtitle_adjudicate.md").read_text(encoding="utf-8")
    assert "以原文为书写证据" in prompt and "未定专名" in prompt
    assert "确定后仍按写法表呈现" in prompt



def test_a_cue_jianying_wrote_as_two_segments_is_cut_when_each_piece_keeps_two_content_characters():
    from src.autoslice.manual_lane import lane_lines

    cues = [{"id": 1, "start_s": 0.0, "end_s": 4.0, "text": "今天天气真好我们出去玩吧"}]
    att = _att(cues, [(0.0, 1.9, "今天天气真好"), (2.1, 4.0, "我们出去玩吧")])
    plan = lane_lines.plan(cues, att)
    assert list(plan.splits) == [1]
    doubts = [{"cue_id": 1, "state": "KEEP", "final_text": "今天天气真好我们出去玩吧"}]
    out, _ = si._final_cues(cues, doubts, lines=plan)
    assert [c["text"] for c in out] == ["今天天气真好", "我们出去玩吧"]
    assert out[0]["end_s"] == out[1]["start_s"] == 2.0
    short = lane_lines.apply_split(cues[0], "今天天气真好我们出去玩吧", [dict(plan.splits[1][0], text="今天天气真好我们出去玩"),
                                                              dict(plan.splits[1][1], text="吧")])
    assert short is None                                            # a one-character piece: the cue stays whole



def _pred(p):
    return {"u1": {"ok": True, "probs": {speaker_v8.HOST: p, "other": 1 - p}}}


def test_the_host_threshold_depends_on_how_many_people_speak_and_the_mid_band_goes_to_review():
    cfg = rules("speaker")
    cue, unit = [{"start_ms": 0, "end_ms": 1000}], [{"id": "u1", "start_ms": 0, "end_ms": 1000}]
    two = speaker_v8.host_threshold(2, cfg)
    many = speaker_v8.host_threshold(3, cfg)
    assert (two, many) == (0.5, 0.9)
    row = speaker_v8.cue_speakers(cue, unit, _pred(0.55), host_min=two)[0]
    assert row["speaker"] == speaker_v8.HOST                         # 0.55 was the guest under v8's own 0.6
    assert speaker_v8.mid_band(row, 2, cfg) == "tie"
    assert speaker_v8.acoustic_tie(dict(row, v8_mid="tie", review_route=speaker_v8.MID_REVIEW, speaker_confidence="低"))
    row3 = speaker_v8.cue_speakers(cue, unit, _pred(0.8), host_min=many)[0]
    assert row3["speaker"] == speaker_v8.GUEST_LABEL and speaker_v8.mid_band(row3, 3, cfg) == "guest_unless_content"
    assert speaker_v8.people({"participants": [["a"], ["b"]], "speaker_count": 3}) == 3


def test_without_engine_clusters_the_mid_band_cues_are_read_by_cpa():
    from src.autoslice.manual_lane.step4_speaker import _v8_mid_rows

    rows = _v8_mid_rows([{"speaker": speaker_v8.HOST, "host_prob": 0.55, "v8_mid": "tie", "source": "v8"},
                         {"speaker": speaker_v8.HOST, "host_prob": 0.95, "v8_mid": "", "source": "v8"}])
    assert [r["speaker_confidence"] for r in rows] == ["低", "高"]


# ----- numerals (U136): ordinals, determiner + 一 and approximations stay in characters ------------------------------
def test_ordinals_sequence_words_and_approximate_numbers_stay_in_characters():
    for text in ("第二天再说", "下一秒就哭了", "每一秒都", "第三个月", "三四天", "两三年", "十五六天", "八九十岁", "七八百块钱",
                 "同一年", "不一定"):
        assert numerals(text)[0] == text, text
    assert numerals("等了五分钟")[0] == "等了5分钟"
    assert numerals("一秒钟")[0] == "1秒钟"
    assert numerals("二零一九年")[0] == "2019年" and numerals("二十三天")[0] == "23天"
    assert numerals("前两天")[0] == "前2天"                          # a count of two days: a quantity
