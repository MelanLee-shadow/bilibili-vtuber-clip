"""A circle nickname that is not the person's name is found where it is listed, by the lane and by the crawler.

香蕉牛奶是咲间妮娜的圈内昵称，取自名字拼音首字母 xjnn；这类昵称新闻爬虫应当查得到.  The clip of that day asked "香蕉牛奶 李豆沙" and "香蕉牛奶 虚拟主播",
got pages about a drink and wrote NOT_FOUND; the crawler asked the same and judged events.  Fakes only: no model, no network.
"""

from __future__ import annotations

import datetime as dt
import json

import pytest

from src.autoslice import clip_name_identity as identity
from src.autoslice import name_initials
from src.autoslice import news_event_crawler as crawler
from src.autoslice import news_event_graph as store
from src.autoslice.knowledge_graph import Graph, Node, Source, dump_graph

NAME, PERSON, PAGE = "香蕉牛奶", "咲间妮娜", "https://wiki.example.org/sakuma"
DRINK = {"title": "在家制作真正好喝的香蕉牛奶", "url": "https://food.example.org/1", "snippet": "香蕉牛奶的做法"}
LISTED = {"title": f"{PERSON} - 萌娘百科", "url": PAGE,
          "snippet": f"本名 {PERSON} 别名 妮老师、妮娜、XJNN、{NAME}、臭臭妮 发色 棕发"}
NOW = dt.datetime(2026, 11, 22, 6, 57, tzinfo=dt.timezone.utc)
STAMP = "2026-11-22T06:57:00Z"


def _node(kind, name, aliases=(), status="accepted"):
    return Node(id=f"{kind}:{name}", kind=kind, names={"display": name}, aliases=tuple(aliases), summary="",
                status=status, sources=(Source("ledger", "test"),))


def test_the_letters_are_the_first_letter_of_every_character_of_a_short_chinese_name():
    assert name_initials.letters(NAME) == name_initials.letters(PERSON) == "xjnn"
    assert name_initials.letters("香香烧烤") == "xxsk"
    assert [name_initials.letters(x) for x in ("豆沙", "李豆沙ab", "今天我们一起去吃饭", "")] == ["", "", "", ""]


def test_a_result_shows_the_letters_written_out_or_a_title_name_with_the_same_letters():
    assert name_initials.signs(NAME, LISTED["title"], LISTED["snippet"]) == ["XJNN", PERSON]
    assert name_initials.signs(NAME, DRINK["title"], DRINK["snippet"]) == []
    assert name_initials.signs(NAME, "食谱", f"今天说说{PERSON}") == [], "a name in the running text is not a title"
    assert name_initials.signs(NAME, "xjnnx 测评", "") == [], "the letters inside a longer word are another word"


def test_known_names_with_the_same_letters_are_leads():
    known = [("星汐Seki", ["星汐Seki", "xxsk", "七宝"]), ("南町", ["南町", "Nightin"]), (PERSON, [PERSON])]
    assert name_initials.same_letters("香香烧烤", known) == ["星汐Seki"]        # the letters written out as an alias
    assert name_initials.same_letters(NAME, known) == [PERSON]                  # a name with the same letters
    assert name_initials.same_letters(PERSON, known) == [], "a name is not a lead for itself"


def test_the_search_is_repeated_when_no_result_shows_the_letters_and_the_page_a_sign_points_to_is_opened():
    """What the first real run showed: one engine answered without the page, another gave the page with a snippet that
    stops before the list of names."""
    cut = dict(LISTED, snippet="跳转到正文 页面 讨论 查看源代码 萌娘百科，万物皆可萌的百科全书")
    answers = [[DRINK], [DRINK, cut]]
    asked: list[str] = []
    opened: list[str] = []

    def search(query):
        asked.append(query)
        return answers[len(asked) - 1]

    def page(url, about):
        opened.append(url)
        assert about == f"别名 {NAME}"
        return "导航 " * 200 + f"本名 {PERSON}\n别名 妮老师、妮娜、XJNN、{NAME}、臭臭妮 " + "正文 " * 200

    rows = name_initials.listed(NAME, search, page)
    assert asked == [f"{NAME} 别名 虚拟UP主"] * 2 and opened == [PAGE]
    assert [r["url"] for r in rows] == [PAGE, DRINK["url"]], "the result that shows the letters comes first"
    assert f"XJNN、{NAME}" in rows[0]["snippet"] and len(rows[0]["snippet"]) <= name_initials.PASSAGE_CHARS
    assert name_initials.listed("豆沙", lambda q: [DRINK]) == [DRINK], "a name without letters is asked once"
    broken = name_initials.listed(NAME, lambda q: [cut], lambda url, about: (_ for _ in ()).throw(OSError("down")))
    assert broken == [cut], "a page that cannot be read leaves the result as it was"
    assert name_initials.passage("没有这个名字的一页", NAME) == ""


# ---- the crawler: whose name is it --------------------------------------------------------------------------------

def _answer(**fields):
    row = {"name": NAME, "found": True, "of": PERSON, "kind": "person", "url": PAGE,
           "basis": f"别名 妮老师、妮娜、XJNN、{NAME}、臭臭妮", "aliases": ["妮老师", "XJNN", "没写在结果里"],
           "summary": "虚拟主播，别名妮老师。"} | fields
    return json.dumps({"items": [row]}, ensure_ascii=False)


def test_the_name_is_searched_where_names_are_listed_and_the_person_becomes_a_candidate_with_it():
    asked: list[str] = []

    def search(query):
        asked.append(query)
        return [DRINK, LISTED]

    prompts: list[str] = []
    made, report = identity.run([NAME], Graph([_node("person", "南町")], []), owner="李豆沙", search=search,
                                ask_json=lambda p: prompts.append(p) or _answer(), at=STAMP)
    assert asked == [f"{NAME} 别名 虚拟UP主"], "a result shows the letters: not asked again"
    assert '"initials": "XJNN"' in prompts[0] and f'"same_initials": ["XJNN", "{PERSON}"]' in prompts[0]
    node = made[0][f"person:{PERSON}"]
    assert node.status == "candidate" and node.aliases == (NAME,) and node.sources == (Source("search", PAGE, STAMP),)
    assert made[1] == {} and report["placed"] == [{"name": NAME, "of": PERSON, "node": f"person:{PERSON}", "url": PAGE,
                                                    "also_listed": ["妮老师", "XJNN"]}], "the page's other names are reported, not added"
    assert Graph(made[0].values(), []).find(NAME), "the next clip finds the person under the nickname"


def test_a_person_the_graph_has_gets_the_name_as_its_own_node_with_an_alias_edge():
    known = Graph([_node("person", PERSON)], [])
    made, _ = identity.run([NAME], known, owner="李豆沙", search=lambda q: [LISTED], ask_json=lambda p: _answer(), at=STAMP)
    assert set(made[0]) == {f"person:{NAME}"} and PERSON in made[0][f"person:{NAME}"].summary
    assert [(e.type, e.from_id, e.to_id) for e in made[1].values()] == [("alias_of", f"person:{NAME}", f"person:{PERSON}")]


@pytest.mark.parametrize(("fields", "why"), [
    ({"url": "https://elsewhere.example/9"}, "url is not one of the name's results"),
    ({"url": DRINK["url"]}, "the result does not hold the person's name"),
    ({"of": NAME}, "no person named"),
    ({"kind": "term"}, "kind is not a person, character or group"),
    ({"basis": "别名 妮老师、妮娜、XJNN"}, "the quoted passage is not in the result or does not hold the name"),
    ({"basis": f"{NAME}是她的昵称"}, "the quoted passage is not in the result or does not hold the name"),
])
def test_an_answer_that_does_not_stand_on_its_own_result_is_dropped(fields, why):
    kept, dropped = identity.parse(_answer(**fields), [(NAME, [DRINK, LISTED])])
    assert kept == [] and dropped == {why: 1}


def test_the_same_letters_alone_place_nobody():
    """The page lists the letters but not the name: a lead in the prompt, not an answer in the graph."""
    letters_only = dict(LISTED, snippet=f"本名 {PERSON} 别名 妮老师、XJNN")
    kept, dropped = identity.parse(_answer(basis="别名 妮老师、XJNN"), [(NAME, [letters_only])])
    assert kept == [] and sum(dropped.values()) == 1


def test_a_name_the_graph_spells_or_one_character_is_not_asked_and_failures_do_not_stop_the_rest():
    known = Graph([_node("person", "南町", aliases=["大N老师"])], [])
    assert identity.pending(["大N老师", "南", NAME, NAME, "190粉毛"], known) == [NAME, "190粉毛"]
    assert identity.pending([f"名{i}号" for i in range(30)], known, limit=3) == ["名0号", "名1号", "名2号"]

    def search(query):
        if "190粉毛" in query:
            raise RuntimeError("search router down")
        return [LISTED]

    made, report = identity.run([NAME, "190粉毛"], known, owner="李豆沙", search=search, ask_json=lambda p: _answer(), at=STAMP)
    assert f"person:{PERSON}" in made[0] and report["counts"] == {"search_failed": 1}
    _, report = identity.run([NAME], known, owner="李豆沙", search=lambda q: [LISTED], ask_json=lambda p: "not json", at=STAMP)
    assert report["counts"] == {"judge_failed": 1} and report["placed"] == []


class Lookup:
    def __init__(self):
        self.queries: list[str] = []

    def web(self, query, limit=5):
        self.queries.append(query)
        return [DRINK, LISTED] if "别名" in query else [DRINK]


def test_the_daily_run_places_a_name_the_clips_left_and_the_lane_reads_it(tmp_path):
    """The witness, through the crawler's own run: the seed the clip left ends as a candidate the combined graph spells."""
    repo = tmp_path / "repo"
    dump_graph(Graph([_node("person", "李豆沙"), _node("person", "南町")], []), repo)
    lookup = Lookup()

    def llm(prompt):
        if '{"mentions"' in prompt:
            return json.dumps({"mentions": []})
        return _answer() if "别名或昵称" in prompt else json.dumps({"items": []})

    report = crawler.crawl(owner="李豆沙", repo_root=repo, runtime_root=tmp_path / "runtime", registry=None, dynamics=None,
                           search=crawler.RouterSearch(lookup, deadline=10**9, sleep=lambda s: None), llm_call=llm,
                           now=NOW, max_seeds=10, clip_names=[NAME])
    assert f"{NAME} 别名 虚拟UP主" in lookup.queries
    assert report.clip_names["placed"] == [{"name": NAME, "of": PERSON, "node": f"person:{PERSON}", "url": PAGE,
                                            "also_listed": ["妮老师", "XJNN"]}]
    assert f"person:{PERSON}" in report.new_nodes and report.to_json()["clip_names"]["asked"] == [NAME]
    combined = store.load_combined(repo, tmp_path / "runtime")
    assert [n.id for n in combined.find(NAME)] == [f"person:{PERSON}"]
    again = crawler.crawl(owner="李豆沙", repo_root=repo, runtime_root=tmp_path / "runtime", registry=None, dynamics=None,
                          search=crawler.RouterSearch(Lookup(), deadline=10**9, sleep=lambda s: None), llm_call=llm,
                          now=NOW + dt.timedelta(days=1), max_seeds=10, clip_names=[NAME])
    assert again.clip_names["asked"] == [], "a placed name is not asked again"


def test_a_name_written_in_the_other_script_or_as_a_homophone_is_the_same_name():
    """The first member not used to design this: the clip writes 谢宝, the page of 謝兒 lists 謝寶."""
    page_row = {"title": "謝兒 - 萌娘百科", "url": PAGE, "snippet": "本名 謝兒（Ciel） 別名 A917、天使大人、謝寶 瞳色 黃瞳"}
    assert name_initials.written(page_row["snippet"], "谢宝") == "謝寶" and name_initials.written("别名 妮老师", "谢宝") == ""
    assert name_initials.written(f"别名 {NAME}", NAME) == NAME and name_initials.written("abc", "") == ""
    assert "謝寶" in name_initials.passage("导航 " * 300 + page_row["snippet"] + " 正文" * 300, "谢宝")
    answer = json.dumps({"items": [{"name": "谢宝", "found": True, "of": "謝兒", "kind": "person", "url": PAGE,
                                    "basis": "別名 A917、天使大人、謝寶", "aliases": ["天使大人"], "summary": "虚拟主播。"}]}, ensure_ascii=False)
    kept, dropped = identity.parse(answer, [("谢宝", [page_row])])
    assert [(f.name, f.of, f.aliases) for f in kept] == [("谢宝", "謝兒", ("天使大人",))] and not dropped
    assert '"written_as": "謝寶"' in identity.prompt("李豆沙", [("谢宝", [page_row])])


def test_a_page_is_read_whichever_way_the_engine_answers():
    """The second real run: one engine answered web_fetch with `excerpts` and no `content`, and the page read as empty."""
    from src.autoslice.manual_lane import work_lookup

    def body(document):
        return json.dumps({"jsonrpc": "2.0", "id": 2, "result": {"structuredContent": {"documents": [document]}}}, ensure_ascii=False)

    assert work_lookup._mcp_page(body({"content": "整页文字"})) == "整页文字"
    assert work_lookup._mcp_page(body({"excerpts": ["# 咲间妮娜", f"| 别名 | XJNN、{NAME} |"]})) == f"# 咲间妮娜\n| 别名 | XJNN、{NAME} |"
    assert work_lookup._mcp_page("data: " + body({"content": "流式"})) == "流式"
    assert work_lookup._mcp_page(json.dumps({"result": {"structuredContent": {"documents": []}}})) == ""
    asked = []
    lookup = work_lookup.WebLookup(search_mcp_url="http://router.test/mcp")
    lookup._tool = lambda name, arguments: asked.append((name, arguments)) or body({"content": "x" * 10})
    assert lookup.page(PAGE, f"别名 {NAME}") == "x" * 10
    assert asked == [("web_fetch", {"urls": [PAGE], "objective": f"别名 {NAME}", "full_content": True})]
