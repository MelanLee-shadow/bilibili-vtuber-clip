"""The channel's core draws (assets/<profile>/core_draws.json): which draws a candidate's main line is on, and the points
the views of the published clips give them (the channel's request).  Since they order
the candidates past the quality gate and never lift one over it; collab and sajiao are must-cut (the channel's request).  Since the same call also says what kind
of thing happens in a candidate and where the host stands in it: two gates pass or fail, the points still order the seats,
and the collab draw is must-cut, and counts, only where something happens between her and a colleague and she is a party."""
import json

import pytest

from src.autoslice.manual_lane import core_draws as cd, discover_select as ds

from test_discover import DATE, entry


CUES = {"1": (1000, "你不能每天都来"), "2": (4000, "我也要跟礼墨单独播"), "3": (9000, "那你晕吧")}


def row(cid, draws, *, story="self", role="party", pin=2, understood=True, peaks=None, **extra):
    return {"id": cid, "draws": draws, "why": "主线", "story": story, "role": role, "pin": pin, "her_line": "我也要跟礼墨单独播",
            "understood": understood, "missing": "" if understood else "前因在片外",
            "peaks": [{"cue": 2, "quote": "我也要跟礼墨单独播", "who": "李豆沙", "why": "改口"}] if peaks is None else peaks, **extra}


def answer(labels):
    return lambda prompt, accepts=None: json.dumps({"labels": [row(k, v) for k, v in labels.items()]}, ensure_ascii=False)


def candidate(cid, **kw):
    return {"candidate_id": cid, "hook": cid, "text": "……", "cue_ids": ["1", "2", "3"], **kw}


def _drawn(cid, segment, start, end, draws, *, story="self", role="party", understood=True, peaks=True, **kw):
    """A labelled candidate: by default her own matter, she a party to it, followable, with its key moment."""
    clip = entry(cid, segment, start, end, **kw)
    clip["core_draws"] = {"version": cd.load()["version"], "draws": draws, "why": "主线", "story": story, "role": role,
                          "her_line": "", "understood": understood, "missing": "" if understood else "前因在片外", "dropped": []}
    clip["peaks"] = [{"quote": "那你晕吧", "who": "李豆沙", "at_ms": start, "why": "反转"}] if peaks else []
    return clip


def test_the_default_channel_has_its_draws_and_the_views_order_them():
    asset = cd.load()
    points = {d["key"]: d["points"] for d in asset["draws"]}
    assert set(points) == {"collab", "relation", "new_look", "yuri", "mishap", "sajiao"}, "her own CP is relation (no cp)"
    assert {d["key"] for d in asset["draws"] if d.get("must_cut")} == {"collab", "sajiao"}
    assert {d["key"] for d in asset["draws"] if d.get("takes_seat")} == {"sajiao"}     # 「撒娇也算进 5 条」
    assert min(points.values()) >= 0 and asset["audience_points_per_level"] > 0      # a draw is never a penalty


def test_the_quality_gate_only_passes_or_fails_and_the_seat_needs_the_content_floor():
    """the channel's request: an admitted card scoring 70 (under the old 75 floor)
    on relation takes a seat; an 86.25 card with no draw and audience interest 2 (4.6 points) does not."""
    least = cd.floor()
    drawn = _drawn("drawn", "segB", 0, 500, ["relation"], level=4, floor=False)
    plain = _drawn("plain", "segA", 0, 500, [], level=4)
    plain_card = plain["selection_scorecard"]
    plain_card["dimensions"].update(audience_salience=2)
    plain_card["raw_score"] = plain_card["effective_score"] = sum(plain_card["dimensions"][k] * w for k, w in plain_card["weights"].items()) / 4
    assert least == 6 and cd.content(drawn) >= least > cd.content(plain) and ds.quality(plain) > 75 > ds.quality(drawn)
    seating = ds.seat_day(DATE, [plain, drawn], built=[])
    assert [e["cid"] for e in seating.seated] == ["drawn"]
    assert [(r["cid"], r["reason"]) for r in seating.rejected] == [("plain", ds.BELOW_MIN_CONTENT)]
    assert "effective_score 86.25 does not count" in seating.rejected[0]["detail"]


def test_past_the_gate_the_content_points_order_the_seats_not_the_effective_score():
    """strong 87.5 with no draw against drawn 85 on relation: the content (draws + audience interest) decides."""
    strong, drawn = _drawn("strong", "segA", 0, 500, [], level=3), _drawn("drawn", "segB", 0, 500, ["relation"], level=2)
    per = cd.load()["audience_points_per_level"]
    assert cd.content(strong) == round(3 * per, 2) and cd.content(drawn) == round(11.6 + 3 * per, 2)
    assert [e["cid"] for e in ds.seat_day(DATE, [strong, drawn], built=[]).seated] == ["drawn", "strong"]


def test_a_candidate_holding_a_danmaku_burst_is_ranked_up_but_not_lifted_over_the_floor(monkeypatch):
    """the channel's request: a candidate holding a burst of its
    broadcast (`bursts`, discover_select._settle_bursts) gains the asset's `burst_points` in the order of the seats -- a
    close one overtakes, a strong draw does not -- and its content points, so the floor, stay what they are.  The size is
    the asset's fit; here it is set to 3 to show the order."""
    real = cd.load()
    assert 0 < real["burst_points"] < min(d["points"] for d in real["draws"] if d["key"] in {"relation", "collab", "mishap"})
    asset = dict(real, burst_points=3.0)
    monkeypatch.setattr(cd, "load", lambda: asset)
    bonus = asset["burst_points"]
    hot, upset = _drawn("hot", "segA", 0, 500, [], level=3), _drawn("upset", "segB", 0, 500, ["yuri"], level=3)
    cp = _drawn("cp", "segC", 0, 500, ["relation"], level=3)
    flat = _drawn("flat", "segD", 0, 500, [], level=3)
    hot["bursts"] = ["B1"]
    low = dict(_drawn("low", "segE", 0, 500, [], level=2), bursts=["B2"])
    low_card = low["selection_scorecard"]
    low_card["dimensions"].update(audience_salience=2)
    low_card["raw_score"] = low_card["effective_score"] = sum(low_card["dimensions"][k] * w for k, w in low_card["weights"].items()) / 4
    assert cd.content(hot) == cd.content(flat) < cd.content(upset) < cd.content(hot) + bonus < cd.content(cp)
    assert cd.burst_points(hot) == bonus and cd.burst_points(flat) == 0 and cd.content(low) + bonus >= cd.floor() > cd.content(low)
    seating = ds.seat_day(DATE, [flat, upset, hot, cp, low], built=[])
    assert [e["cid"] for e in seating.seated] == ["cp", "hot", "upset", "flat"]
    assert {r["cid"]: r["reason"] for r in seating.rejected} == {"low": ds.BELOW_MIN_CONTENT}


def test_must_cut_draws_need_only_the_basic_quality_gate_and_a_collab_takes_no_seat():
    """11-14 replay: 「变矮求抱抱」 (sajiao, effective 65.75) is a clip 维护者 wants made; only the basic gate holds it back.
    the channel's request: with the five seats all built, the collab is still made, the sajiao clip is not."""
    built = [{"cid": f"b{n}", "segment": "segZ", "start_ms": n * 1000, "end_ms": n * 1000 + 500} for n in range(5)]
    cute = _drawn("cute", "segA", 0, 500, ["sajiao"], level=4, floor=False)
    guest = _drawn("guest", "segB", 0, 500, ["collab"], story="person")
    both = _drawn("both", "segF", 0, 500, ["collab", "sajiao"], story="person", level=4, floor=False)   # a collab: no seat
    unsure = _drawn("unsure", "segC", 0, 500, ["sajiao"], confidence=0.1)
    plain_low = _drawn("plain_low", "segE", 0, 500, [], level=4, floor=False)
    other = _drawn("other", "segD", 0, 500, [], level=4)
    seating = ds.seat_day(DATE, [cute, guest, both, unsure, plain_low, other], built=built)
    assert {e["cid"]: e["seat_reason"] for e in seating.seated} == {"guest": ds.MUST_CUT, "both": ds.MUST_CUT}
    assert seating.policy["must_cut"] == 2 and seating.policy["must_cut_seated"] == 0
    assert {r["cid"]: r["reason"] for r in seating.rejected} == {
        "cute": ds.QUOTA_FULL, "unsure": ds.BELOW_MIN_CONFIDENCE, "plain_low": ds.BELOW_MIN_CONTENT, "other": ds.QUOTA_FULL}
    assert cd.takes_seat(cute) and not cd.takes_seat(both) and not cd.takes_seat(guest) and not cd.takes_seat(other)


def test_sajiao_clips_take_seats_first_and_the_best_content_points_win_when_they_outnumber_them():
    """The 11-15 replay had four sajiao clips (effective 58-62.5) besides the day's four: they now share the five seats.
    Sajiao goes ahead of higher-scored ordinary candidates; past five, the content points (draws + audience) pick."""
    cute = [_drawn(f"cute{n}", f"seg{n}", 0, 500, ["sajiao"], level=n % 5, floor=False) for n in range(6)]
    strong = _drawn("strong", "segX", 0, 500, [], level=4)
    seating = ds.seat_day(DATE, [strong, *cute], built=[])
    assert [e["cid"] for e in seating.seated] == ["cute4", "cute3", "cute2", "cute1", "cute0"]
    assert all(e["seat_reason"] == ds.MUST_CUT for e in seating.seated) and seating.policy["must_cut_seated"] == 5
    assert {r["cid"]: r["reason"] for r in seating.rejected} == {"cute5": ds.QUOTA_FULL, "strong": ds.QUOTA_FULL}
    two = ds.seat_day(DATE, [strong, cute[0], cute[1]], built=[])
    assert [e["cid"] for e in two.seated] == ["cute1", "cute0", "strong"]          # they go first, the rest still fill


def test_a_must_cut_candidate_over_a_built_clip_is_not_made_twice():
    built = [{"cid": "b0", "segment": "segA", "start_ms": 0, "end_ms": 500}]
    seating = ds.seat_day(DATE, [_drawn("cute", "segA", 100, 400, ["sajiao"])], built=built)
    assert seating.seated == [] and seating.rejected[0]["reason"] == ds.OVERLAPS_SEATED


def test_stacked_draws_stop_at_the_cap():
    clip = _drawn("stacked", "segA", 0, 500, ["mishap", "new_look", "relation"], level=0, floor=False)
    assert cd.points(clip) == cd.load()["max_points"] == 12
    assert cd.content(clip) == 12 + 2 * cd.load()["audience_points_per_level"]      # the card's audience interest is level 2
    assert [e["cid"] for e in ds.seat_day(DATE, [clip], built=[]).seated] == ["stacked"]     # its effective 60 does not count


def test_the_pool_is_labelled_in_one_call_and_a_kept_label_is_not_asked_again():
    pool = [candidate("a", hook="被问为何总在同事家开播"), candidate("b", hook="猴群")]
    calls = []

    def ask(prompt, accepts=None):
        calls.append(prompt)
        return answer({"a": ["relation"], "b": []})(prompt)

    assert cd.label(pool, ask, CUES) is True and len(calls) == 1 and "只算主线" in calls[0]
    assert "#2 我也要跟礼墨单独播" in calls[0] and "没看这场直播、但认识这些主播和圈内梗的观众" in calls[0]
    assert pool[0]["core_draws"]["draws"] == ["relation"] and pool[1]["core_draws"]["draws"] == []
    assert pool[0]["core_draws"]["story"] == "self" and pool[0]["core_draws"]["role"] == "party"
    # the key moments are kept on the candidate itself, under the key the title step reads, at the cue's time in the recording
    assert pool[0]["peaks"] == [{"quote": "我也要跟礼墨单独播", "who": "李豆沙", "at_ms": 4000, "why": "改口"}]
    assert "peaks" not in pool[0]["core_draws"]
    assert cd.label(pool, ask, CUES) is False and len(calls) == 1
    pool.append(candidate("c", hook="标记的段落"))          # one added later (an owner mark): only it is asked
    assert cd.label(pool, answer({"c": ["sajiao"]}), CUES) is True and pool[2]["core_draws"]["draws"] == ["sajiao"]


@pytest.mark.parametrize("raw", ['{"labels": [{"id": "a", "draws": ["百合"]}]}', '{"labels": []}', "not json",
                                 '{"labels": [{"id": "a", "draws": []}, {"id": "a", "draws": []}]}',
                                 json.dumps({"labels": [row("a", [], story="game")]}),             # not one of the asset's stories
                                 json.dumps({"labels": [row("a", [], role="lead")]}),
                                 json.dumps({"labels": [row("a", [], peaks="none")]}),
                                 json.dumps({"labels": [row("a", ["mishap"])]})])                  # a mishap without whose it is
def test_an_unusable_answer_fails_and_labels_nothing(raw):
    pool = [candidate("a")]
    with pytest.raises(cd.DrawsError):
        cd.label(pool, lambda prompt, accepts=None: raw, CUES)
    assert "core_draws" not in pool[0] and "peaks" not in pool[0]


def test_a_channel_with_stories_labels_from_the_recordings_subtitles():
    with pytest.raises(cd.DrawsError, match="no cues given"):
        cd.label([candidate("a")], lambda prompt, accepts=None: pytest.fail("asked"))


def test_a_mishap_that_happened_to_someone_else_is_not_her_mishap():
    """11-23 22:46: the guest's computer froze; the label said 「她电脑无响应」 and the clip ranked second of fourteen."""
    pool = [candidate("guest_pc"), candidate("her_own")]
    rows = [row("guest_pc", ["collab", "mishap"], subjects={"mishap": {"host": False, "who": "礼墨Sumi"}}),
            row("her_own", ["mishap"], subjects={"mishap": {"host": True, "who": "李豆沙"}})]
    cd.label(pool, lambda prompt, accepts=None: json.dumps({"labels": rows}, ensure_ascii=False), CUES)
    assert pool[0]["core_draws"]["draws"] == ["collab"]
    assert pool[0]["core_draws"]["dropped"] == [{"draw": "mishap", "who": "礼墨Sumi"}]
    assert pool[1]["core_draws"]["draws"] == ["mishap"] and pool[1]["core_draws"]["dropped"] == []


def test_she_is_a_party_only_on_a_line_that_pins_the_matter_on_her():
    """No line has a speaker before step 4; in a call of three the model took a guest's line for hers (11-23 21:41) and
    made her a party to it.  A party label names the line that can only mean her, inside the candidate, or it is not one."""
    pool = [candidate("named"), candidate("none"), candidate("outside")]
    rows = [row("named", ["collab"], story="person"), row("none", ["collab"], story="person", pin=None),
            row("outside", ["collab"], story="person", pin=9)]
    cd.label(pool, lambda prompt, accepts=None: json.dumps({"labels": rows}, ensure_ascii=False), CUES)
    assert [(e["core_draws"]["role"], e["core_draws"]["labelled"]) for e in pool] == [
        ("party", None), ("participant", "party"), ("participant", "party")]
    assert [cd.must_cut(e) for e in pool] == [["collab"], [], []]


def test_a_highlight_the_voiceprint_heard_as_hers_makes_her_a_party():
    """11-23 20:11: the model's own first peak was her 「我们是团队」「我们不是敌人啊」, heard as hers, and it still said
    participant.  A highlight that is her own line is the party's (the role definition); without a reading it stays as labelled."""
    rows = [row(c, ["collab"], story="person", role="participant", pin=None) for c in ("hers", "theirs")]
    rows[1]["peaks"] = [{"cue": 3, "quote": "那你晕吧", "who": "", "why": "反转"}]
    pool = [candidate("hers"), candidate("theirs")]
    cd.label(pool, lambda prompt, accepts=None: json.dumps({"labels": rows}, ensure_ascii=False), CUES, {"2": 0.97, "3": 0.05})
    assert [(e["core_draws"]["role"], e["core_draws"]["pin"], e["core_draws"]["labelled"]) for e in pool] == [
        ("party", 2, "participant"), ("participant", None, None)]
    assert [cd.must_cut(e) for e in pool] == [["collab"], []]
    unheard = [candidate("hers")]
    cd.label(unheard, lambda prompt, accepts=None: json.dumps({"labels": rows[:1]}, ensure_ascii=False), CUES)
    assert unheard[0]["core_draws"]["role"] == "participant"


def test_a_highlight_heard_as_hers_is_hers_only_when_the_model_also_reads_her_as_the_speaker():
    """The voiceprint sometimes hears a guest as the host (11-24: of the lag group 礼墨 and 安晚 shared, 28% of the
    chunks were classed as hers).  A mark alone does not make her a party: the model's own reading of who said the
    highlight must name her too."""
    rows = [row(c, ["collab"], story="person", role="participant", pin=None) for c in ("guest", "unsure", "hers")]
    rows[0]["peaks"] = [{"cue": 2, "quote": "我也要跟礼墨单独播", "who": "安晚awa", "why": "笑点"}]
    rows[1]["peaks"] = [{"cue": 2, "quote": "我也要跟礼墨单独播", "who": "", "why": "笑点"}]
    rows[2]["peaks"] = [{"cue": 2, "quote": "我也要跟礼墨单独播", "who": "小李", "why": "笑点"}]
    pool = [candidate(c) for c in ("guest", "unsure", "hers")]
    asset = dict(cd.load(), votes=1)
    labels = cd.parse(json.dumps({"labels": rows}, ensure_ascii=False), ["guest", "unsure", "hers"], asset,
                      entries={e["candidate_id"]: e for e in pool}, cues=CUES, voices={"2": 0.97})
    assert [labels[c]["role"] for c in ("guest", "unsure", "hers")] == ["participant", "participant", "party"]


def test_a_seat_that_turns_on_the_story_is_labelled_three_times_and_takes_what_two_say(monkeypatch):
    """11-23 20:11 (公开规则chose its title): in one replay of fifteen the label said 「其他完整的事」, the collab lost its
 points and the clip was not made. A recording with a candidate whose collab counts only on some stories is asked
 `votes` times; the story and the role are the middle of the answers (with three: what two say)."""
    stories = iter([("person", "party"), ("other", "party"), ("person", "participant")])

    def ask(prompt, accepts=None):
        asked.append(prompt)
        story, role = next(stories)
        return json.dumps({"labels": [row("a", ["collab"], story=story, role=role), row("b", ["relation"])]}, ensure_ascii=False)

    asked: list[str] = []
    pool = [candidate("a"), candidate("b")]
    assert cd.label(pool, ask, CUES) is True and len(asked) == 3 and len(set(asked)) == 3   # three askings, none read from the cache
    found = pool[0]["core_draws"]
    assert (found["story"], found["role"]) == ("person", "party") and cd.must_cut(pool[0]) == ["collab"] and cd.points(pool[0]) == 9.4
    assert [(v["story"], v["role"]) for v in found["votes"]] == [("person", "party"), ("other", "party"), ("person", "participant")]
    assert pool[0]["peaks"][0]["quote"] == "我也要跟礼墨单独播" and "votes" in pool[1]["core_draws"]
    # three different stories: the middle one in the asset's order; a draw only one answer gives is not kept
    three = iter([("person", ["collab", "mishap"]), ("gag", ["collab"]), ("other", ["collab"])])

    def split(prompt, accepts=None):
        story, draws = next(three)
        return json.dumps({"labels": [row("a", draws, story=story, subjects={"mishap": {"host": True, "who": "李豆沙"}})]},
                          ensure_ascii=False)

    alone = [candidate("a")]
    cd.label(alone, split, CUES)
    assert alone[0]["core_draws"]["story"] == "gag"
    assert alone[0]["core_draws"]["draws"] == ["collab"]
    # no candidate whose seat turns on its story (no collab): asked once
    once: list[str] = []
    plain = [candidate("c")]
    cd.label(plain, lambda prompt, accepts=None: once.append(prompt) or answer({"c": ["relation"]})(prompt), CUES)
    assert len(once) == 1 and "votes" not in plain[0]["core_draws"]


def test_a_peak_outside_the_candidate_is_left_out_and_at_most_three_are_kept():
    peaks = [{"cue": 99, "quote": "不在这段里", "who": "", "why": ""}, {"cue": "#1", "quote": "你不能每天都来", "who": "", "why": "禁令"},
             {"cue": 2, "quote": "", "who": "", "why": "没有原话"}, {"cue": 2, "quote": "我也要跟礼墨单独播", "why": "改口"},
             {"cue": 3, "quote": "那你晕吧"}, {"cue": 3, "quote": "第四处"}]
    pool = [candidate("a")]
    cd.label(pool, lambda prompt, accepts=None: json.dumps({"labels": [row("a", [], peaks=peaks)]}, ensure_ascii=False), CUES)
    assert [(p["quote"], p["at_ms"]) for p in pool[0]["peaks"]] == [("你不能每天都来", 1000), ("我也要跟礼墨单独播", 4000), ("那你晕吧", 9000)]


def test_a_label_of_an_older_fit_is_asked_again(monkeypatch):
    pool = [candidate("a", core_draws={"version": "2000-01-01", "draws": ["cp"], "why": ""})]
    assert cd.must_cut(pool[0]) == []                  # a label of a retired fit names a draw the asset no longer has
    assert cd.label(pool, answer({"a": ["yuri"]}), CUES) is True and pool[0]["core_draws"]["version"] == cd.load()["version"]


def test_a_collab_is_must_cut_only_where_something_happens_between_her_and_a_colleague():
    """the channel's request; 频道的要求（of three collab game clips）.  The game the three of them play is labelled collab too: it queues, and takes a seat."""
    built = [{"cid": f"b{n}", "segment": "segZ", "start_ms": n * 1000, "end_ms": n * 1000 + 500} for n in range(5)]
    banned = _drawn("banned", "segA", 0, 500, ["collab"], story="person", role="party")
    game = _drawn("game", "segB", 0, 500, ["collab"], story="other", role="participant")
    beside = _drawn("beside", "segC", 0, 500, ["collab"], story="person", role="participant")   # between two others, she joins in
    assert cd.must_cut(banned) == ["collab"] and cd.must_cut(game) == [] and cd.must_cut(beside) == []
    assert cd.points(banned) == 9.4 and cd.points(game) == cd.points(beside) == 0      # nor do the others get its points
    seating = ds.seat_day(DATE, [game, banned, beside], built=built)
    assert {e["cid"]: e["seat_reason"] for e in seating.seated} == {"banned": ds.MUST_CUT}
    assert {r["cid"]: r["reason"] for r in seating.rejected} == {"game": ds.QUOTA_FULL, "beside": ds.QUOTA_FULL}
    free = ds.seat_day(DATE, [game, banned, beside], built=[])
    assert [e["cid"] for e in free.seated] == ["banned", "beside", "game"]       # the others by their points, here equal


def test_two_gates_pass_or_fail_and_a_story_that_needs_its_key_moment_has_one():
    watching = _drawn("watching", "segA", 0, 500, ["collab"], story="person", role="bystander")
    lost = _drawn("lost", "segB", 0, 500, ["relation"], understood=False)
    flat = _drawn("flat", "segC", 0, 500, ["collab"], story="other", role="participant", peaks=False)
    fine = _drawn("fine", "segD", 0, 500, ["relation"], story="self", peaks=False)     # only `other` needs the moment
    seating = ds.seat_day(DATE, [watching, lost, flat, fine], built=[])
    assert [e["cid"] for e in seating.seated] == ["fine"]
    assert {r["cid"]: r["reason"] for r in seating.rejected} == {
        "watching": ds.NOT_ABOUT_HOST, "lost": ds.NOT_UNDERSTOOD, "flat": ds.NO_PEAK}
    assert "前因在片外" in next(r["detail"] for r in seating.rejected if r["cid"] == "lost")


def test_the_points_order_the_seats_whatever_the_story_and_a_good_game_still_passes_the_floor():
    """The story does not order the seats: any one strong draw is enough (the channel's request).
    A game the colleagues play together gets no collab points: on audience interest 2 it stays under the floor, on 3 it
    passes like any game would.  Only the content points order them here."""
    flat = _drawn("flat", "segA", 0, 500, ["collab"], story="other", role="participant")
    flat_card = flat["selection_scorecard"]
    flat_card["dimensions"].update(audience_salience=2)
    flat_card["raw_score"] = flat_card["effective_score"] = sum(flat_card["dimensions"][k] * w for k, w in flat_card["weights"].items()) / 4
    game = _drawn("game", "segB", 0, 500, ["collab"], story="other", role="participant")
    upset = _drawn("upset", "segC", 0, 500, ["yuri"], story="self")
    cp = _drawn("cp", "segD", 0, 500, ["relation"], story="self")
    gag = _drawn("gag", "segE", 0, 500, ["mishap"], story="gag")
    seating = ds.seat_day(DATE, [flat, game, upset, cp, gag], built=[])
    assert cd.content(cp) > cd.content(gag) > cd.content(upset) > cd.content(game) >= cd.floor() > cd.content(flat)
    assert [e["cid"] for e in seating.seated] == ["cp", "gag", "upset", "game"]
    assert {r["cid"]: r["reason"] for r in seating.rejected} == {"flat": ds.BELOW_MIN_CONTENT}


def test_the_evenings_candidates_on_one_matter_take_one_seat_together():
    """维护者, of two clips on who is whose pet made twenty minutes apart: 同话题的两条应合并成一条"""
    first = _drawn("pet_2241", "seg2230", 676950, 759940, ["relation"], story="gag", level=4)
    again = _drawn("pet_2303", "seg2300", 192270, 422230, ["sajiao"], story="gag", level=3)
    other = _drawn("other", "seg2300", 900000, 990000, ["relation"], level=2)
    for clip in (first, again):
        clip.update(topic="pet_2241", topic_theme="谁是谁的宠物", topic_why="后一条接着前一条的结论")
    seating = ds.seat_day(DATE, [again, other, first], built=[])
    lead = next(e for e in seating.seated if e.get("topic_members"))
    # the sajiao one goes first (must-cut, takes a seat) and carries the other, in broadcast order
    assert lead["cid"] == "pet_2303" and [m["cid"] for m in lead["topic_members"]] == ["pet_2241", "pet_2303"]
    assert [e["cid"] for e in seating.seated] == ["pet_2303", "other"]
    assert [(r["cid"], r["reason"]) for r in seating.rejected] == [("pet_2241", ds.SAME_TOPIC)]
    # once the joined clip is built it holds both: neither is seated again, nothing over either stretch is
    built = [{"cid": "pet_2303_m", "segment": "seg2300", "start_ms": 192270, "end_ms": 422230, "covers": [
        {"cid": "pet_2241", "segment": "seg2230", "start_ms": 676950, "end_ms": 759940},
        {"cid": "pet_2303", "segment": "seg2300", "start_ms": 192270, "end_ms": 422230}]}]
    late = _drawn("late", "seg2230", 700000, 720000, ["relation"])
    later = ds.seat_day(DATE, [again, other, first, late], built=built)
    assert [e["cid"] for e in later.seated] == ["other"] and later.policy["free_seats"] == 4
    assert {r["cid"]: r["reason"] for r in later.rejected} == {"late": ds.OVERLAPS_SEATED}
    # a clip is at most ten minutes: a member that no longer fits is not joined, and not made on its own either
    long = _drawn("pet_2330", "seg2330", 0, 400_000, ["relation"], story="gag", level=2)
    long.update(topic="pet_2241", topic_theme="谁是谁的宠物", topic_why="")
    capped = ds.seat_day(DATE, [again, first, long], built=[])
    assert [[m["cid"] for m in e["topic_members"]] for e in capped.seated] == [["pet_2241", "pet_2303"]]
    assert "not made" in next(r["detail"] for r in capped.rejected if r["cid"] == "pet_2330")


def test_a_lead_that_is_not_seated_leaves_its_topic_to_the_next_member():
    """A topic's members give up their seats to its lead only when the lead is seated: a lead that overlaps a built clip
    must not take a must-cut member down with it, recorded as 「made as one clip」.  A must-cut that takes no seat leads
    before one that does, so the joined clip of a collab and a sajiao clip takes no seat."""
    built = [{"cid": "b0", "segment": "segA", "start_ms": 0, "end_ms": 400}]
    first = _drawn("first", "segA", 0, 500, ["collab"], story="person", role="party", level=4)
    second = _drawn("second", "segB", 0, 500, ["collab"], story="person", role="party", level=3)
    for clip in (first, second):
        clip.update(topic="first", topic_theme="同一件事", topic_why="")
    seating = ds.seat_day(DATE, [first, second], built=built)
    assert [(e["cid"], e["seat_reason"]) for e in seating.seated] == [("second", ds.MUST_CUT)]
    assert {r["cid"]: r["reason"] for r in seating.rejected} == {"first": ds.OVERLAPS_SEATED}
    cute = _drawn("cute", "segC", 0, 500, ["sajiao"], story="self", level=4)
    banned = _drawn("banned", "segD", 0, 500, ["collab"], story="person", role="party", level=2)
    for clip in (cute, banned):
        clip.update(topic="cute", topic_theme="同一件事", topic_why="")
    full_day = [{"cid": f"b{n}", "segment": "segZ", "start_ms": n, "end_ms": n + 1} for n in range(5)]
    joined = ds.seat_day(DATE, [cute, banned], built=full_day)
    assert [(e["cid"], [m["cid"] for m in e["topic_members"]]) for e in joined.seated] == [("banned", ["cute", "banned"])]
    # an ordinary lead that finds the seats full: its member is not 「made as one clip」 with it either
    plain = [_drawn("p1", "segA", 0, 500, ["relation"], level=4), _drawn("p2", "segB", 0, 500, ["relation"], level=3)]
    for clip in plain:
        clip.update(topic="p1", topic_theme="同一件事", topic_why="")
    full = ds.seat_day(DATE, plain, built=full_day)
    assert full.seated == [] and {r["cid"]: r["reason"] for r in full.rejected} == {"p1": ds.QUOTA_FULL, "p2": ds.QUOTA_FULL}


def test_a_channel_without_draws_asks_nothing_and_adds_nothing(monkeypatch):
    monkeypatch.setattr(cd, "load", lambda: None)
    pool = [entry("x", "segA", 0, 500, level=4)]
    pool[0]["core_draws"] = {"version": "v", "draws": ["relation"], "why": ""}
    assert cd.label(pool, lambda prompt, accepts=None: pytest.fail("asked")) is False
    assert cd.content(pool[0]) == 0.0 and cd.must_cut(pool[0]) == [] and ds.quality(pool[0]) == 90.0
    assert cd.gate(pool[0]) is None and not cd.has_stories()      # no stories: no gates


class _Host:
    """The lane's CPA as the media host serves it: a request id it has finished is answered again with that same answer
    (cpa_text.py REUSED terminal answer); a new id gets the next scripted answer."""

    def __init__(self, answers):
        self.answers, self.given, self.calls = list(answers), {}, []

    def ask(self, *, call_id, prompt_text, model, effort, remote_dir, **_):
        self.calls.append(call_id)
        if call_id not in self.given:
            self.given[call_id] = self.answers.pop(0)
        return {"call_id": call_id, "model": model, "effort": effort, "rc": 0, "elapsed_s": 0.1,
                "response_text": self.given[call_id], "stderr_tail": ""}


def _asker(tmp_path, host):
    from src.autoslice.manual_lane import discover
    from src.autoslice.manual_lane.adapters import Adapters

    # a recording's directory under the day's: the answer cache is the task's, two levels up (step_io._cpa_cache_path)
    return discover._ask_in(tmp_path / "day" / "seg", "seg", "/host/seg", Adapters(cpa=host), call="recall",
                            purpose=discover.RECALL_PURPOSE)


GAG = json.dumps({"labels": [row("a", ["relation", "gag"], story="gag")]}, ensure_ascii=False)   # 11-25 replays: 7 of 97
GOOD = json.dumps({"labels": [row("a", ["relation"], story="gag")]}, ensure_ascii=False)


def test_an_answer_with_a_story_key_among_the_draws_is_asked_again_not_kept(tmp_path):
    # the story key gag written among the draws is outside the contract (core_draws.parse); it is not cached, the same
    # question is asked again, and the answer that meets the contract is the one kept
    host = _Host([GAG, GOOD])
    pool = [candidate("a")]
    assert cd.label(pool, _asker(tmp_path, host), CUES) is True
    assert pool[0]["core_draws"]["draws"] == ["relation"] and pool[0]["core_draws"]["story"] == "gag"
    assert len(host.calls) == 2 and host.calls[1].endswith("--reply-2")
    assert list((tmp_path / "day" / "seg" / "cpa").glob("*.rejected-1.completion.txt"))   # the turned-down answer is on record
    again = [candidate("a")]
    assert cd.label(again, _asker(tmp_path, host), CUES) is True and len(host.calls) == 2   # the good answer is the cached one


def test_a_recording_whose_labelling_answers_were_all_turned_down_is_asked_afresh_on_the_next_run(tmp_path):
    from src.autoslice.manual_lane.adapters import CPA_TEXT_TRIES
    from src.autoslice.manual_lane.discover_select import RecallCallError

    host = _Host([GAG] * CPA_TEXT_TRIES + [GOOD])
    with pytest.raises(RecallCallError, match="CORE_DRAWS_UNUSABLE"):     # the run's tries are spent: the call failed
        cd.label([candidate("a")], _asker(tmp_path, host), CUES)
    assert len(host.calls) == CPA_TEXT_TRIES and not list((tmp_path / "cache").rglob("*.json"))   # none of them cached
    # the next run does not ask the host for the ids it turned down (it would give the same answers again): it goes on
    pool = [candidate("a")]
    assert cd.label(pool, _asker(tmp_path, host), CUES) is True and pool[0]["core_draws"]["draws"] == ["relation"]
    assert host.calls[CPA_TEXT_TRIES:] == [f"{host.calls[0]}--reply-{CPA_TEXT_TRIES + 1}"]
