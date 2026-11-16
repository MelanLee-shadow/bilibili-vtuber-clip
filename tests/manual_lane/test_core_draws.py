"""The channel's core draws (assets/<profile>/core_draws.json): which draws a candidate's main line is on, and the points
the views of the published clips give them, added to the seating score (公开规则：按照播放量来解释各个要素…
核心看点播放量自然就高)."""
import json

import pytest

from src.autoslice.manual_lane import core_draws as cd, discover_select as ds

from test_discover import DATE, entry


def answer(labels):
    return lambda prompt: json.dumps({"labels": [{"id": k, "draws": v, "why": "主线"} for k, v in labels.items()]}, ensure_ascii=False)


def test_the_default_channel_has_its_draws_and_the_views_order_them():
    asset = cd.load()
    points = {d["key"]: d["points"] for d in asset["draws"]}
    assert set(points) == {"mishap", "relation", "new_look", "sajiao", "yuri", "cp"}
    assert points["relation"] >= max(points.values()) and min(points.values()) >= 0     # a draw is never a penalty


def test_a_candidate_on_a_core_draw_clears_the_floor_a_plain_one_with_the_same_card_does_not():
    plain, colleague = entry("plain", "segA", 0, 500, level=4, floor=False), entry("colleague", "segB", 0, 500, level=4, floor=False)
    colleague["core_draws"] = {"version": cd.load()["version"], "draws": ["relation"], "why": "和礼墨的事"}
    seating = ds.seat_day(DATE, [plain, colleague], built=[])
    assert [e["cid"] for e in seating.seated] == ["colleague"]
    assert ds.score(colleague) == 81.4 and seating.rejected[0]["cid"] == "plain"


def test_within_a_tier_the_draws_reorder_the_seats():
    """Same Tier: a card 2.5 points lower whose main line is a colleague goes first (85 + 11.4 against 87.5)."""
    strong, drawn = entry("strong", "segA", 0, 500, level=3), entry("drawn", "segB", 0, 500, level=2)
    drawn["core_draws"] = {"version": cd.load()["version"], "draws": ["relation"], "why": "和南町的事"}
    assert (ds.score(strong), ds.score(drawn)) == (87.5, 96.4)
    assert [e["cid"] for e in ds.seat_day(DATE, [strong, drawn], built=[]).seated] == ["drawn", "strong"]


def test_stacked_draws_stop_at_the_cap():
    """11-14 replay: a 59.5 card on mishap + new_look got 16 points and cleared 75; views do not keep adding up."""
    clip = entry("stacked", "segA", 0, 500, level=0, floor=False)
    clip["core_draws"] = {"version": cd.load()["version"], "draws": ["mishap", "new_look", "relation"], "why": ""}
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
    assert cd.label(pool, answer({"a": ["yuri"]})) is True and pool[0]["core_draws"]["version"] == cd.load()["version"]


def test_a_channel_without_draws_asks_nothing_and_adds_nothing(monkeypatch):
    monkeypatch.setattr(cd, "load", lambda: None)
    pool = [entry("x", "segA", 0, 500, level=4)]
    pool[0]["core_draws"] = {"version": "v", "draws": ["relation"], "why": ""}
    assert cd.label(pool, lambda prompt: pytest.fail("asked")) is False and ds.score(pool[0]) == 90.0
