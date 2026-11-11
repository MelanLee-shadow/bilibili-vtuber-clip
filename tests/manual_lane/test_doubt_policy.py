"""Doubt voting and CPA's final word: the current text votes, readings group by exact original text,
ties are never settled by lexical order, and CPA's choice stands (no after-the-fact override).

U122: every Gemini listening (3.8 Low, argon, and the legacy 3.6 / 3.7 records) is ONE family; a version number is
not another vendor.  ASR engines (jianying, mai, moss) are each their own family."""

from __future__ import annotations

from fakes import rules_with

from src.autoslice.manual_lane import doubt_policy as dp
from src.autoslice.manual_lane import suspicion

CUR = "有0.5那么多吗"
X = "有0.25那么多吗"


def test_gemini_3_8_and_argon_are_one_family_so_they_do_not_maintain_a_reading():
    m = dp.maintenance({"audio:38m@P0": X, "audio:argon@P0": X}, CUR)
    assert m["maintained"] is False and m["families"] == []


def test_legacy_3_6_and_3_7_records_are_the_same_gemini_family_as_3_8():
    assert dp.witness_family("audio:36h@P0") == dp.witness_family("audio:37m@P0") == dp.witness_family("audio:38m@P0")
    assert dp.witness_family("audio:argon@P2") == "gemini"
    m = dp.maintenance({"audio:36h@P0": X, "audio:37m@P0": X, "audio:38m@P0": X, "audio:argon@P0": X}, CUR)
    assert m["maintained"] is False and m["families"] == []


def test_gemini_3_8_agreeing_with_jianying_is_two_families_and_maintains_the_reading():
    m = dp.maintenance({"audio:38m@P0": X, "jianying": X}, CUR)
    assert m["maintained"] is True and m["reading"] == X
    assert m["families"] == ["asr:jianying", "gemini"]


def test_two_independent_families_with_the_same_non_current_reading_maintain_it():
    m = dp.maintenance({"audio:38m@P0": X, "asr:mai": X, "audio:argon@P0": "有一天"}, CUR)
    assert m["maintained"] is True and m["reading"] == X
    assert m["families"] == ["asr:mai", "gemini"]


def test_one_family_alone_does_not_maintain_even_over_many_rounds():
    m = dp.maintenance({"audio:38m@P0": X, "audio:38m@P1": X, "audio:38m@P2": X}, CUR)
    assert m["maintained"] is False and m["families"] == []


def test_an_asr_engine_is_its_own_family():
    m = dp.maintenance({"asr:mai": X, "audio:38m@P0": X}, CUR)
    assert m["maintained"] is True and m["families"] == ["asr:mai", "gemini"]
    j = dp.maintenance({"jianying": X, "asr:moss": X}, CUR)
    assert j["maintained"] is True


def test_bcut_danmaku_and_unknown_keys_never_vote():
    m = dp.maintenance({"bcut": X, "danmaku": X, "mystery": X, "audio:38m@P0": X}, CUR)
    assert m["maintained"] is False


def test_the_current_text_votes_and_can_be_held_by_itself_plus_one_family():
    m = dp.maintenance({"audio:38m@P0": CUR, "audio:argon@P0": X}, CUR)
    assert m["maintained"] is False and m["current_held"] is True


def test_a_tie_that_includes_the_current_text_keeps_the_current_text():
    # current + jianying agree (2 families); gemini + mai agree on another reading (2 families): current wins the tie
    m = dp.maintenance({"jianying": CUR, "asr:mai": X, "audio:38m@P0": X}, CUR)
    assert m["maintained"] is False and m["current_held"] is True and m["tie"] is True


def test_a_tie_without_the_current_text_maintains_nothing_and_is_not_settled_by_order(tmp_path, monkeypatch):
    rules_with(tmp_path, monkeypatch, candidates={"gemini_min_overlap": 0})   # synthetic texts share nothing
    # two readings, each held by two families, neither is the current text: CPA decides (no lexical tie-break)
    readings = {"audio:38m@P0": "甲甲甲", "asr:moss": "甲甲甲", "audio:argon@P0": "乙乙乙", "asr:mai": "乙乙乙"}
    m = dp.maintenance(readings, "丙丙丙")
    assert m["maintained"] is False and m["tie"] is True


def test_punctuation_and_filler_variants_keep_their_own_sources():
    m = dp.maintenance({"audio:38m@P0": "有 0.25，那么多吗", "asr:mai": "嗯有0.25那么多吗。"}, CUR)
    assert m["maintained"] is False
    assert m["candidates"]["有 0.25，那么多吗"]["families"] == ["gemini"]
    assert m["candidates"]["嗯有0.25那么多吗。"]["families"] == ["asr:mai"]


def test_different_numbers_are_never_merged_into_one_reading():
    # 0.5 and 0.25 differ in their digits, so they are two readings even though the letters are close
    m = dp.maintenance({"audio:38m@P0": "有0.25那么多吗", "asr:mai": "有0.5那么多吗"}, CUR)
    assert m["maintained"] is False


def test_candidates_put_the_current_text_first_and_merge_equal_readings():
    cands = dp.candidates({"bcut": CUR, "audio:38m@P0": X, "audio:argon@P0": X, "asr:mai": CUR}, CUR)
    assert cands[0]["text"] == CUR and cands[0]["source"] == "current"
    assert [c["source"] for c in cands][1:] == ["audio:38m@P0"]
    # 3.8 and argon give the same reading: one Gemini family, so supporters is 1 (not 2)
    assert cands[1]["supporters"] == 1 and cands[1]["families"] == ["gemini"]


def test_decide_adopts_cpa_choice_whether_or_not_maintained():
    row = {"state": "ADOPT", "final_text": X, "affects_meaning": True, "confidence": "高"}
    out = dp.decide(row, CUR, maintained=True)
    assert out["state"] == "ADOPT" and out["final_text"] == X and out["maintained"] is True


def test_decide_keep_stands_even_against_a_maintained_reading():
    # TASK-5 item 3 (d051): CPA's KEEP is not overwritten by the two-family reading
    out = dp.decide({"state": "KEEP", "final_text": "随便", "affects_meaning": False}, CUR, maintained=True)
    assert out["state"] == "KEEP" and out["final_text"] == CUR


def test_decide_fit_context_keeps_cpa_text():
    out = dp.decide({"state": "FIT_CONTEXT", "final_text": "有0.5那么多吗", "affects_meaning": True,
                     "confidence": "高"}, CUR, maintained=False)
    assert out["state"] == "FIT_CONTEXT" and out["final_text"] == "有0.5那么多吗" and out["confidence"] == "高"


def test_adopt_or_fit_without_a_text_cannot_be_applied_so_it_is_unresolved():
    assert dp.decide({"state": "ADOPT", "final_text": "", "affects_meaning": True}, CUR,
                     maintained=True)["state"] == "UNRESOLVED"
    assert dp.decide({"state": "FIT_CONTEXT", "final_text": " ", "affects_meaning": True}, CUR,
                     maintained=False)["state"] == "UNRESOLVED"


def test_unresolved_with_a_best_guess_becomes_a_low_confidence_fit_never_a_silent_keep():
    out = dp.decide({"state": "UNRESOLVED", "final_text": X, "confidence": "高"}, CUR, maintained=False)
    assert out["state"] == "FIT_CONTEXT" and out["final_text"] == X and out["confidence"] == "低"


def test_missing_or_unknown_cpa_answer_is_unresolved_never_a_silent_keep():
    assert dp.decide(None, CUR, maintained=False)["state"] == "UNRESOLVED"
    assert dp.decide({"state": "guess", "final_text": "x"}, CUR, maintained=True)["state"] == "UNRESOLVED"


def test_candidates_name_the_independent_sources_that_support_each_reading():
    readings = {"bcut": "现稿", "jianying": "圣母在上", "asr:mai": "圣母在上", "audio:38m@P0": "受在上"}
    cands = dp.candidates(readings, "现稿")
    by_text = {c["text"]: c for c in cands}
    assert by_text["圣母在上"]["supporters"] == 2 and by_text["圣母在上"]["families"] == ["asr:jianying", "asr:mai"]
    assert by_text["受在上"]["supporters"] == 1
    assert cands[0]["text"] == "现稿" and cands[0]["source"] == "current"


def test_escalation_plan_is_3_8_then_argon_only(monkeypatch):
    # U122: no 3.6 and no 3.7 level; a doubt still unsettled after argon goes to CPA's choice, marked low.  TASK-11: the
    # production ladder asks the window ASR (MAI, MOSS) first, Gemini only for what it leaves unsettled
    monkeypatch.delenv("MANUAL_LANE_LADDER")
    assert suspicion.ladder() == suspicion.DEFAULT_LADDER == "asr_first"
    assert suspicion.escalation_levels() == (("L1", "asr", ("mai", "moss")), ("L2", "listen", ("38m",)),
                                             ("L3", "listen", ("argon",)))
    assert suspicion.LADDERS["gemini"] == (("L1", "listen", ("38m",)), ("L2", "listen", ("argon",)))
    assert all(slug in ("38m", "argon") for _, kind, slugs in suspicion.escalation_levels() if kind == "listen"
               for slug in slugs)


def test_vote_schemes_change_only_who_is_counted_never_which_candidates_are_shown(monkeypatch):
    from src.autoslice.manual_lane import doubt_policy as dp

    readings = {"bcut": "现稿", "jianying": "读法甲", "asr:mai": "读法甲", "audio:38m@P0": "读法乙"}
    monkeypatch.setenv("MANUAL_LANE_VOTE_SCHEME", "u59")
    by = {c["text"]: c for c in dp.candidates(readings, "现稿")}
    assert by["读法甲"]["supporters"] == 2 and by["现稿"]["supporters"] == 1
    monkeypatch.setenv("MANUAL_LANE_VOTE_SCHEME", "no_asr")
    by = {c["text"]: c for c in dp.candidates(readings, "现稿")}
    assert by["读法甲"]["supporters"] == 0 and by["读法甲"]["sources"] == ["asr:mai", "jianying"]
    assert dp.maintenance(readings, "现稿")["maintained"] is False
    monkeypatch.setenv("MANUAL_LANE_VOTE_SCHEME", "no_current")
    by = {c["text"]: c for c in dp.candidates(readings, "现稿")}
    assert by["现稿"]["supporters"] == 0 and by["读法甲"]["supporters"] == 2
    monkeypatch.setenv("MANUAL_LANE_VOTE_SCHEME", "list_only")
    cands = dp.candidates(readings, "现稿")
    assert {c["text"] for c in cands} == {"现稿", "读法甲", "读法乙"} and all("supporters" not in c for c in cands)
    assert "不按家数计票" in dp.vote_prompt_fields()["vote_rule"]
