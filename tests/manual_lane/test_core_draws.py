"""The channel's core draws (assets/<profile>/core_draws.json): which draws a candidate's main line is on, and the points
the views of the published clips give them (公开规则：按照播放量来解释各个要素). Since they order
the candidates past the quality gate and never lift one over it; collab and sajiao are must-cut (公开规则
「质量问题和这个内容问题要分开来…联动肯定是必切…撒娇…一定要放」)."""
import json

import pytest

from src.autoslice.manual_lane import core_draws as cd, discover_select as ds

from test_discover import DATE, entry


def answer(labels):
    return lambda prompt: json.dumps({"labels": [{"id": k, "draws": v, "why": "主线"} for k, v in labels.items()]}, ensure_ascii=False)


def _drawn(cid, segment, start, end, draws, **kw):
    clip = entry(cid, segment, start, end, **kw)
    clip["core_draws"] = {"version": cd.load()["version"], "draws": draws, "why": "主线"}
    return clip


def test_the_default_channel_has_its_draws_and_the_views_order_them():
    asset = cd.load()
    points = {d["key"]: d["points"] for d in asset["draws"]}
    assert set(points) == {"collab", "relation", "new_look", "yuri", "mishap", "sajiao"}, "her own CP is relation (no cp)"
    assert {d["key"] for d in asset["draws"] if d.get("must_cut")} == {"collab", "sajiao"}
    assert min(points.values()) >= 0 and asset["audience_points_per_level"] > 0      # a draw is never a penalty


def test_the_draws_never_lift_a_candidate_over_the_quality_gate():
    """Before a 70 card on relation sat (70 + 11.4 >= 75); quality comes first now."""
    plain, colleague = entry("plain", "segA", 0, 500, level=4, floor=False), _drawn("colleague", "segB", 0, 500, ["relation"],
                                                                                 level=4, floor=False)
    seating = ds.seat_day(DATE, [plain, colleague], built=[])
    assert seating.seated == [] and {r["reason"] for r in seating.rejected} == {ds.BELOW_MIN_SCORE}


def test_past_the_gate_the_content_points_order_the_seats_not_the_effective_score():
    """strong 87.5 with no draw against drawn 85 on relation: the content (draws + audience interest) decides."""
    strong, drawn = entry("strong", "segA", 0, 500, level=3), _drawn("drawn", "segB", 0, 500, ["relation"], level=2)
    per = cd.load()["audience_points_per_level"]
    assert cd.content(strong) == round(3 * per, 2) and cd.content(drawn) == round(11.6 + 3 * per, 2)
    assert [e["cid"] for e in ds.seat_day(DATE, [strong, drawn], built=[]).seated] == ["drawn", "strong"]


def test_must_cut_draws_take_no_seat_and_need_only_the_basic_quality_gate():
    """11-14 replay: 「变矮求抱抱」 (sajiao, effective 65.75) is a clip 维护者 wants made; only the basic gate holds it back."""
    built = [{"cid": f"b{n}", "segment": "segZ", "start_ms": n * 1000, "end_ms": n * 1000 + 500} for n in range(5)]
    cute, guest = _drawn("cute", "segA", 0, 500, ["sajiao"], level=4, floor=False), _drawn("guest", "segB", 0, 500, ["collab"])
    unsure = _drawn("unsure", "segC", 0, 500, ["sajiao"], confidence=0.1)
    plain_low = entry("plain_low", "segE", 0, 500, level=4, floor=False)
    other = entry("other", "segD", 0, 500, level=4)
    seating = ds.seat_day(DATE, [cute, guest, unsure, plain_low, other], built=built)   # the day's five seats are all built
    assert {e["cid"]: e["seat_reason"] for e in seating.seated} == {"guest": ds.MUST_CUT, "cute": ds.MUST_CUT}
    assert seating.policy["must_cut"] == 2
    assert {r["cid"]: r["reason"] for r in seating.rejected} == {
        "unsure": ds.BELOW_MIN_CONFIDENCE, "plain_low": ds.BELOW_MIN_SCORE, "other": ds.QUOTA_FULL}


def test_a_must_cut_candidate_over_a_built_clip_is_not_made_twice():
    built = [{"cid": "b0", "segment": "segA", "start_ms": 0, "end_ms": 500}]
    seating = ds.seat_day(DATE, [_drawn("cute", "segA", 100, 400, ["sajiao"])], built=built)
    assert seating.seated == [] and seating.rejected[0]["reason"] == ds.OVERLAPS_SEATED


def test_stacked_draws_stop_at_the_cap():
    clip = _drawn("stacked", "segA", 0, 500, ["mishap", "new_look", "relation"], level=0, floor=False)
    assert cd.points(clip) == cd.load()["max_points"] == 12
    assert [r["reason"] for r in ds.seat_day(DATE, [clip], built=[]).rejected] == [ds.BELOW_MIN_SCORE]


def test_the_pool_is_labelled_in_one_call_and_a_kept_label_is_not_asked_again():
    pool = [{"candidate_id": "a", "hook": "被问为何总在同事家开播", "text": "……"}, {"candidate_id": "b", "hook": "猴群", "text": "……"}]
    calls = []

    def ask(prompt):
        calls.append(prompt)
        return answer({"a": ["relation"], "b": []})(prompt)

    assert cd.label(pool, ask) is True and len(calls) == 1 and "只算主线" in calls[0]
    assert pool[0]["core_draws"]["draws"] == ["relation"] and pool[1]["core_draws"]["draws"] == []
    assert cd.label(pool, ask) is False and len(calls) == 1
    pool.append({"candidate_id": "c", "hook": "标记的段落", "text": "……"})          # one added later (an owner mark): only it is asked
    assert cd.label(pool, answer({"c": ["sajiao"]})) is True and pool[2]["core_draws"]["draws"] == ["sajiao"]


@pytest.mark.parametrize("raw", ['{"labels": [{"id": "a", "draws": ["百合"]}]}', '{"labels": []}', "not json",
                                 '{"labels": [{"id": "a", "draws": []}, {"id": "a", "draws": []}]}'])
def test_an_unusable_answer_fails_and_labels_nothing(raw):
    pool = [{"candidate_id": "a", "hook": "", "text": ""}]
    with pytest.raises(cd.DrawsError):
        cd.label(pool, lambda prompt: raw)
    assert "core_draws" not in pool[0]


def test_a_label_of_an_older_fit_is_asked_again(monkeypatch):
    pool = [{"candidate_id": "a", "hook": "", "text": "", "core_draws": {"version": "2000-01-01", "draws": ["cp"], "why": ""}}]
    assert cd.must_cut(pool[0]) == []                  # a label of a retired fit names a draw the asset no longer has
    assert cd.label(pool, answer({"a": ["yuri"]})) is True and pool[0]["core_draws"]["version"] == cd.load()["version"]


def test_a_channel_without_draws_asks_nothing_and_adds_nothing(monkeypatch):
    monkeypatch.setattr(cd, "load", lambda: None)
    pool = [entry("x", "segA", 0, 500, level=4)]
    pool[0]["core_draws"] = {"version": "v", "draws": ["relation"], "why": ""}
    assert cd.label(pool, lambda prompt: pytest.fail("asked")) is False
    assert cd.content(pool[0]) == 0.0 and cd.must_cut(pool[0]) == [] and ds.quality(pool[0]) == 90.0
