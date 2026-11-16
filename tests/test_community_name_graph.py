"""Community names folded into the candidate knowledge graph: deduped against the graph, dated by their earliest video
or a web-stated date (never by the crawler's first sighting), origins kept, written through the news crawler's run.

Owner 「有进行查重吗以及，知道这些梗的来源？…海獭冲击竟然是今天才出来的新梗？…这些在知识图里吗，还是额外的？」
"""
from __future__ import annotations

import datetime as dt
import json

import pytest

from src.autoslice import community_name_crawler
from src.autoslice import community_name_graph as names
from src.autoslice import news_event_graph as store
from src.autoslice.knowledge_graph import Graph, load_graph
from src.autoslice.news_event_judge import Row
from tests.test_news_event_crawler import NOW, OWNER, STAMP, FakeLookup, _answer, _cpa, _node, _run, repo  # noqa: F401

TODAY = NOW.date()


def _rel(surface, canonical, kind="meme_of", mid="1", first="2026-11-09T00:00:00Z"):
    return {"entity_id": f"bilibili:{mid}", "canonical": canonical, "surface": surface, "relation_kind": kind,
            "first_seen_at": first, "last_seen_at": "2026-11-10T00:00:00Z", "accepted_at": first,
            "evidence_summary": "", }


def _snapshot(*rels):
    return {"schema_version": names.SNAPSHOT_SCHEMA, "relations": list(rels)}


def _state(*rows):
    return {"schema_version": names.STATE_SCHEMA, "mappings": [
        {"entity_id": e, "surface": s, "evidence": [{"bvid": f"BV{i}", "published_at": at, "url": f"https://b.test/{i}"}
                                                    for i, at in enumerate(ats)]} for e, s, ats in rows]}


def test_schemas_are_the_community_crawlers():
    assert names.SNAPSHOT_SCHEMA == community_name_crawler.SNAPSHOT_SCHEMA
    assert names.STATE_SCHEMA == community_name_crawler.STATE_SCHEMA


def test_earliest_video_comes_from_the_evidence_not_the_first_sighting():
    rels = names.relations(_snapshot(_rel("海獭冲击", "栞栞Shiori", mid="9")),
                           _state(("bilibili:9", "海獭冲击", ["2026-11-15T10:00:00Z", "2026-03-02T08:00:00Z", None])))
    assert rels[0]["video_at"] == "2026-03-02" and rels[0]["video_url"] == "https://b.test/1"
    assert rels[0]["first_seen_at"].startswith("2026-11-09"), "kept only as when the crawler first saw it"
    (nodes, _), report = names.fold(rels, {}, Graph([], []), at=STAMP)
    assert nodes["term:海獭冲击"].valid == {"from": "2026-03-02", "until": None}
    assert report["in_graph_now"][0]["earliest_source"] == "crawler_evidence"
    no_state = names.relations(_snapshot(_rel("海獭冲击", "栞栞Shiori", mid="9")), None)
    (nodes, _), _ = names.fold(no_state, {}, Graph([], []), at=STAMP)
    assert nodes["term:海獭冲击"].valid is None, "no evidence date: undated, not dated by first_seen"


def test_a_spelling_the_graph_already_has_is_reported_not_added(repo):
    graph = load_graph(repo)
    rels = names.relations(_snapshot(_rel("小李", OWNER, kind="alias_of"), _rel("kmx", OWNER, kind="associated_with"),
                                     _rel("豆豆", OWNER, kind="alias_of")), None)
    (nodes, edges), report = names.fold(rels, {}, graph, at=STAMP)
    assert {k["name"]: k["in_graph_as"] for k in report["already_known"]} == {
        "小李": f"person:{OWNER}", "kmx": "term:kmx"}
    assert set(nodes) == {"person:豆豆"}, "an alias is a node of its person's own kind"
    assert set(edges) == {f"person:豆豆|alias_of|person:{OWNER}"}
    assert report["relations"] == 3


def test_person_is_found_by_canonical_or_its_cjk_part_else_a_candidate_with_the_space(repo):
    graph = load_graph(repo)
    rels = names.relations(_snapshot(_rel("awawa", "安晚awa", kind="alias_of"),
                                     _rel("獭獭", "栞栞Shiori", kind="alias_of", mid="77"),
                                     _rel("喵喵团", "栞栞Shiori", kind="fan_name_of", mid="77")), None)
    (nodes, edges), _ = names.fold(rels, {}, graph, at=STAMP)
    assert "person:awawa|alias_of|person:安晚" in edges
    shiori = nodes["person:栞栞Shiori"]
    assert shiori.status == "candidate" and shiori.sources[0].ref == "https://space.bilibili.com/77"
    assert "community:喵喵团|fan_name_of|person:栞栞Shiori" in edges and "person:獭獭" in nodes


def _rows(*urls):
    return [Row(title="t", url=u, snippet="s") for u in urls]


def _video(day, bvid):
    return names.Video(title=f"标题{bvid}", url=f"https://www.bilibili.com/video/{bvid}/",
                       published_at=f"{day}T00:00:00+00:00", text="")


@pytest.mark.parametrize("row, why", [
    ({"earliest": "2025-01-02", "date_basis": "", "url": None}, "date text"),
    ({"earliest": "2027-01-02", "date_basis": "2027年", "url": None}, "past"),
    ({"earliest": "2025-01", "date_basis": "2025年1月", "url": None}, "YYYY-MM-DD"),
    ({"earliest": None, "date_basis": "", "url": "https://elsewhere.test"}, "not one of the results"),
])
def test_origin_answers_are_checked_row_by_row(row, why):
    rel, other = names.relations(_snapshot(_rel("海獭冲击", "栞栞Shiori"), _rel("獭獭", "栞栞Shiori")), None)
    items = [(rel, _rows("https://a.test"), []), (other, _rows("https://b.test"), [_video("2025-02-04", "BV1")])]
    good = {"key": names.key(other), "meaning": "粉丝名", "origin": "", "earliest": None, "date_basis": "",
            "url": "https://www.bilibili.com/video/BV1/"}
    raw = json.dumps({"origins": [{"key": names.key(rel), "meaning": "m", "origin": "o"} | row, good]})
    got, problems = names.parse_origins(raw, items, TODAY.isoformat())
    assert list(got) == [names.key(other)], "a bad row is dropped alone; a video url counts as a given url"
    assert len(problems) == 1 and why in problems[0]
    assert got[names.key(other)]["first_video_at"] == "2025-02-04" and got[names.key(other)]["probed"]
    assert names.parse_origins('{"origins": []}', items, TODAY.isoformat())[1][-1].endswith("no answer")
    with pytest.raises(names.OriginError):
        names.parse_origins("not json", items, TODAY.isoformat())


def test_origins_are_looked_up_once_and_a_blank_answer_is_asked_again_later():
    rel = names.relations(_snapshot(_rel("海獭冲击", "栞栞Shiori")), None)[0]
    searched = []

    def search(q):
        searched.append(q)
        return _rows("https://a.test")

    def ask(prompt):
        return json.dumps({"origins": [{"key": names.key(rel), "meaning": "", "origin": "", "earliest": None,
                                        "date_basis": "", "url": None}]})
    origins: dict = {}
    assert names.lookup_origins([rel], origins, search=search, ask_json=ask, today=TODAY)["judged"] == 1
    assert searched == ["海獭冲击 栞栞 梗 由来"]
    names.lookup_origins([rel], origins, search=search, ask_json=ask, today=TODAY + dt.timedelta(days=5))
    assert len(searched) == 1, "a blank answer waits RELOOK_DAYS"
    names.lookup_origins([rel], origins, search=search, ask_json=ask, today=TODAY + dt.timedelta(days=31))
    assert len(searched) == 2


def test_the_news_run_writes_names_into_the_runtime_graph(tmp_path, repo):
    names_path, state_path = tmp_path / "community_names.json", tmp_path / "state.json"
    names_path.write_text(json.dumps(_snapshot(_rel("海獭冲击", "安晚awa"), _rel("小李", OWNER, kind="alias_of")),
                                     ensure_ascii=False), "utf-8")
    state_path.write_text(json.dumps(_state(("bilibili:1", "海獭冲击", ["2026-11-09T01:00:00Z"]))), "utf-8")
    notes: list[str] = []
    community = names.load(names_path, state_path, tmp_path / names.ORIGINS_FILE, notes)
    assert not notes
    origin_url = "https://wiki.test/otter"

    def answer(prompt):
        if "最早什么时候出现" in prompt:
            return json.dumps({"origins": [{"key": "bilibili:1|海獭冲击", "meaning": "安晚扑人的动作", "origin": "某次直播",
                                            "earliest": "2025-08-01", "date_basis": "2025年8月1日", "url": origin_url}]},
                              ensure_ascii=False)
        return _answer()
    lookup = FakeLookup({"海獭冲击": _rows(origin_url)})
    report = _run(tmp_path, repo, lookup, _cpa(answer), community=community, name_videos=lambda name: [])
    assert report.community["origins"] | {} == {"known": 1, "due": 1, "searched": 1, "search_failed": 0, "judged": 1,
                                                "judge_failed": 0, "videos_found": 0, "video_search_failed": 0}, \
        "the already-known 小李 is not looked up"
    combined = store.load_combined(repo, tmp_path / "runtime")
    otter = combined.nodes["term:海獭冲击"]
    assert otter.status == "candidate" and otter.valid == {"from": "2025-08-01", "until": None}
    assert otter.summary == "安晚扑人的动作；来源：某次直播" and otter.sources[0].ref == origin_url
    assert any(e.id == "term:海獭冲击|related_to|person:安晚" and e.note == "梗" for e in combined.edges)
    out = report.to_json()["community"]
    assert out["in_graph_now"][0]["earliest_source"] == "stated" and out["in_graph_now"][0]["earliest"] == "2025-08-01"
    assert out["already_known"] == [{"name": "小李", "of": OWNER, "in_graph_as": f"person:{OWNER}"}]
    assert names.load_origins(tmp_path / names.ORIGINS_FILE)["bilibili:1|海獭冲击"]["url"] == origin_url
    # the next day: its own node from yesterday is not "already known", and the origin is not looked up again
    lookup2 = FakeLookup({"海獭冲击": _rows(origin_url)})
    again = _run(tmp_path, repo, lookup2, _cpa(answer), community=community, now=NOW + dt.timedelta(days=1),
                 name_videos=lambda name: [])
    assert [n["name"] for n in again.community["in_graph_now"]] == ["海獭冲击"]
    assert not any("梗 由来" in q for q in lookup2.queries)


def test_an_unreadable_origins_file_is_kept_and_the_names_still_fold(tmp_path, repo):
    names_path = tmp_path / "community_names.json"
    names_path.write_text(json.dumps(_snapshot(_rel("海獭冲击", "安晚awa")), ensure_ascii=False), "utf-8")
    origins = tmp_path / names.ORIGINS_FILE
    origins.write_text("{broken", "utf-8")
    community = names.load(names_path, None, origins, [])
    report = _run(tmp_path, repo, FakeLookup({"海獭冲击": _rows("https://a.test")}), _cpa(), community=community)
    assert origins.read_text("utf-8") == "{broken"
    assert any("community origins unreadable" in e for e in report.errors)
    assert "term:海獭冲击" in store.load_combined(repo, tmp_path / "runtime").nodes


def test_a_failed_origin_call_is_reported():
    rel = names.relations(_snapshot(_rel("海獭冲击", "栞栞Shiori")), None)[0]
    notes: list[str] = []

    def ask(prompt):
        raise RuntimeError("cpa down")
    stats = names.lookup_origins([rel], {}, search=lambda q: _rows("https://a.test"), ask_json=ask, today=TODAY,
                                 notes=notes)
    assert stats["judge_failed"] == 1 and notes == ["origin batch 0: RuntimeError: cpa down"]


def test_the_oldest_bilibili_video_dates_a_name_the_crawler_saw_only_lately():
    """海獭冲击: the crawler's own evidence starts ; searching the name itself finds it a year earlier."""
    rel = names.relations(_snapshot(_rel("海獭冲击", "栞栞Shiori", mid="9")),
                          _state(("bilibili:9", "海獭冲击", ["2026-11-15T10:00:00Z"])))[0]
    old = _video("2025-09-01", "BVold")
    asked = []

    def ask(prompt):
        asked.append(prompt)
        return json.dumps({"origins": [{"key": names.key(rel), "meaning": "她扑向观众的动作梗", "origin": "",
                                        "earliest": None, "date_basis": "", "url": old["url"]}]}, ensure_ascii=False)
    origins: dict = {"bilibili:9|海獭冲击": {"meaning": "", "origin": "", "earliest": None, "date_basis": "",
                                           "url": None, "looked_at": TODAY.isoformat(), "first_video_at": None,
                                           "first_video_url": None, "probed": False}}
    stats = names.lookup_origins([rel], origins, search=lambda q: [], ask_json=ask, today=TODAY,
                                 videos=lambda name: [old, _video("2026-11-15", "BVnew")])
    assert stats["videos_found"] == 1 and "标题BVold" in asked[0], "looked up again: it had no Bilibili search yet"
    (nodes, _), report = names.fold([rel], origins, Graph([], []), at=STAMP)
    assert nodes["term:海獭冲击"].valid == {"from": "2025-09-01", "until": None}
    entry = report["in_graph_now"][0]
    assert entry["earliest_source"] == "bilibili_search" and entry["earliest_ref"] == old["url"]


def test_names_of_the_owner_are_looked_up_first(tmp_path, repo):
    names_path = tmp_path / "community_names.json"
    names_path.write_text(json.dumps(_snapshot(*[_rel(f"别人{i}", "甲", mid="5") for i in range(3)],
                                               _rel("豆豆", OWNER, kind="alias_of")), ensure_ascii=False), "utf-8")
    community = names.load(names_path, None, tmp_path / names.ORIGINS_FILE, [])
    searched = []

    def videos(name):
        searched.append(name)
        return []
    names.run(community, load_graph(repo), search=lambda q: [], ask_json=lambda p: '{"origins": []}', today=TODAY,
              at=STAMP, notes=[], owner=OWNER, videos=videos)
    assert searched[0] == "豆豆"


def test_probe_videos_keeps_only_videos_that_carry_the_name_oldest_first():
    class Response:
        stale = False

        def __init__(self, body):
            self.body = body

    class Client:
        def fetch(self, url, headers=None):
            assert "order=totalrank" in url
            rows = [{"bvid": "BV1066531596", "pubdate": 1767225600, "title": "<em>海獭</em>冲击合集", "tag": ""},
                    {"bvid": "BV1037391611", "pubdate": 1735689600, "title": "海獭冲击！", "description": ""},
                    {"bvid": "BV1068764364", "pubdate": 1704067200, "title": "海獭日常", "tag": "无关"},
                    {"bvid": "BV1066549112", "pubdate": 4102444800, "title": "海獭冲击 未来"}]
            return Response(json.dumps({"code": 0, "data": {"result": rows}}))

    got = community_name_crawler.probe_videos(Client(), endpoint="https://api.bilibili.com/x/s", max_results=50,
                                              query="海獭冲击", mixin_key="k" * 32, now=NOW)
    assert [v["url"][-13:-1] for v in got] == ["BV1037391611", "BV1066531596"]
    assert got[0]["published_at"].startswith("2025-01-01") and got[1]["title"] == "海獭冲击合集"
