"""What a clip refers to outside itself is found once and read by every step (references.py; owner a
capability is made once and called wherever it is needed).  Fakes only: no model, no network."""

from __future__ import annotations

import os
import json

from fakes import MERGE, FakeRemote, FakeSearch, make_ctx

from src.autoslice.manual_lane import circle_context as cc
from src.autoslice.manual_lane import graph_context, references

HEARD, SAYING = "我很可爱请亏我钱", "我很可爱，请给我钱"
URL = "https://wiki.example.org/saying"
ROWS = [{"title": "出处", "url": URL, "snippet": "这句话出自某位主播"}]
DRAFT = [{"id": 1, "start_s": 0.0, "end_s": 2.0, "text": "大家好"}, {"id": 2, "start_s": 2.0, "end_s": 4.0, "text": HEARD},
         {"id": 3, "start_s": 4.0, "end_s": 6.0, "text": "某某联盟又开会了"}]
SPOTTED = [{"term": HEARD, "cues": [2], "kind": "现成说法", "why": "读不通，像一句流行语", "likely": SAYING},
           {"term": "某某联盟", "cues": [3], "kind": "圈内事件", "why": "没交代是什么", "likely": ""}]


class Cpa:
    """`spot` answers the reading of the draft (None: the call fails); the lookup answers found for `known` terms."""

    def __init__(self, spot=SPOTTED, known=None):
        self.spot, self.known, self.calls = spot, known if known is not None else {HEARD: ("一句流行语，出自某位主播。", SAYING)}, []

    def ask(self, *, call_id, prompt_text, model, effort, remote_dir, max_output_tokens=None):
        self.calls.append(call_id)
        if call_id == references.SPOT_CALL:
            text = None if self.spot is None else json.dumps({"references": self.spot} if isinstance(self.spot, list) else self.spot,
                                                             ensure_ascii=False)
        else:
            items = json.loads(prompt_text.split("检索结果）：\n", 1)[1])
            text = json.dumps([{"term": i["term"], "found": i["term"] in self.known,
                                "note": self.known.get(i["term"], ("", ""))[0], "wording": self.known.get(i["term"], ("", ""))[1],
                                "sources": [URL] if i["term"] in self.known else [], "why_not": "" if i["term"] in self.known else "没说到"}
                               for i in items], ensure_ascii=False)
        return {"call_id": call_id, "model": model, "effort": effort, "rc": 1 if text is None else 0, "elapsed_s": 0.1,
                "response_text": text or "", "stderr_tail": "CPA_HTTP_ERROR (fake)" if text is None else ""}


def _ctx(tmp_path, cpa, *, configured=True):
    ctx = make_ctx(tmp_path, remote=FakeRemote(), cpa=cpa)
    ctx.adapters.lookup = FakeSearch(configured=configured, default=ROWS)
    return ctx


def test_the_draft_is_read_once_and_what_is_found_goes_to_the_subtitle_agent_as_a_lead(tmp_path):
    cpa = Cpa()
    ctx = _ctx(tmp_path, cpa)
    got = references.for_subtitles(ctx, DRAFT)
    assert got["leads"] == [{"heard": HEARD, "cues": [2], "is": "一句流行语，出自某位主播。", "wording": SAYING,
                             "sources": ["wiki.example.org"]}]
    assert [c["call_id"] for c in got["cpa"]] == [references.SPOT_CALL, references.LOOKUP_CALL]
    assert SAYING in [q for q, _ in ctx.adapters.lookup.calls]                  # the guessed wording is searched under its own words
    assert "某某联盟" in got["line"] and HEARD in got["line"]
    store = references.load(ctx)
    assert store[HEARD]["status"] == cc.FOUND and store[HEARD]["stage"] == references.SUBTITLES and store["某某联盟"]["status"] == cc.NOT_FOUND
    again = references.for_subtitles(ctx, DRAFT)                                # the agent's other runs: the store answers
    assert again["leads"] == got["leads"] and again["cpa"] == [] and len(cpa.calls) == 2
    changed = [dict(c, text=c["text"] + "啊") for c in DRAFT]                   # another draft is another reading
    references.for_subtitles(ctx, changed)
    assert len(cpa.calls) > 2


def test_the_title_step_does_not_search_again_what_the_subtitle_step_looked_up(tmp_path):
    cpa = Cpa()
    ctx = _ctx(tmp_path, cpa)
    references.for_subtitles(ctx, DRAFT)
    searched = len(ctx.adapters.lookup.calls)
    merged = dict(json.loads(MERGE), unexplained=[{"term": "某某联盟", "cues": [3], "why": "笑点靠它"},
                                                 {"term": "新的指称", "cues": [1], "why": "没解释"}])
    cpa.known["新的指称"] = ("一个新企划。", "")
    found = cc.gather(ctx, merged, "1 大家好\n2 " + HEARD + "\n3 某某联盟又开会了", [], graph_context.read(ctx, []))
    asked = [q for q, _ in ctx.adapters.lookup.calls[searched:]]
    assert asked and all(q.startswith("新的指称") for q in asked)                # only the reference nobody had looked up
    by_term = {t["term"]: t for t in found["terms"]}
    assert by_term["某某联盟"]["status"] == cc.NOT_FOUND and by_term["新的指称"]["status"] == cc.FOUND
    assert by_term[HEARD]["status"] == cc.FOUND                                 # what the subtitle step found comes along
    assert f"（规范写法：{SAYING}）" in cc.block(found)
    assert references.load(ctx)["新的指称"]["stage"] == references.TITLE


def test_nothing_here_stops_the_step(tmp_path):
    failed = references.for_subtitles(_ctx(tmp_path / "a", Cpa(spot=None)), DRAFT)
    assert failed["leads"] == [] and "未能读底稿" in failed["line"]
    unconfigured = _ctx(tmp_path / "b", Cpa(), configured=False)
    got = references.for_subtitles(unconfigured, DRAFT)
    assert got["leads"] == [] and references.load(unconfigured)[HEARD]["status"] == cc.UNCONFIGURED
    assert [c["call_id"] for c in got["cpa"]] == [references.SPOT_CALL]         # nothing to explain without results
    nothing = references.for_subtitles(_ctx(tmp_path / "c", Cpa(spot=[])), DRAFT)
    assert nothing["leads"] == [] and "没有需要另查的指称" in nothing["line"]
    bad = references.for_subtitles(_ctx(tmp_path / "d", Cpa(spot={"term": "x"})), DRAFT)
    assert bad["leads"] == []


def test_what_the_clips_looked_up_is_left_for_the_news_crawler_and_becomes_its_seeds(tmp_path, monkeypatch):
    from src.autoslice.knowledge_graph import Graph
    from src.autoslice.news_event_seeds import clip_seed_names, derive_seeds

    seeds = tmp_path / "assets" / "reference_seeds.json"
    monkeypatch.setattr(references, "seeds_path", lambda: seeds)
    ctx = _ctx(tmp_path / "clip", Cpa())
    references.for_subtitles(ctx, DRAFT)
    rows = json.loads(seeds.read_text(encoding="utf-8"))
    assert [(r["name"], r["kind"], r["status"]) for r in rows] == [("某某联盟", "圈内事件", cc.NOT_FOUND)]     # the saying is not the crawler's
    assert clip_seed_names(seeds) == ["某某联盟"] and clip_seed_names(tmp_path / "none.json") == [] and clip_seed_names(None) == []
    # a reference of no clear kind that was not found, and a single character, are not names to hand over
    junk = [cc.TermContext(term=t, cues=[1], why="", queries=[], status=st, note="", sources=[], reason="", kind=k)
            for t, st, k in (("听不清的残句", cc.NOT_FOUND, "其他"), ("细", cc.NOT_FOUND, "人物"), ("某个组合", cc.FOUND, "其他"))]
    references.remember(ctx, junk, references.SUBTITLES)
    assert sorted(r["name"] for r in json.loads(seeds.read_text(encoding="utf-8"))) == ["某个组合", "某某联盟"]
    got = derive_seeds(owner="主播", graph=Graph({}, []), registry=None, dynamics=None, day=0, limit=5,
                       clip_names=clip_seed_names(seeds))
    assert sorted((s.name, s.origin) for s in got) == [("主播", "owner"), ("某个组合", "clips"), ("某某联盟", "clips")]


# ---- a nickname that is not the person's name (owner 香蕉牛奶 = 咲间妮娜, name → xjnn → another word) -----------

NICK, PERSON = "西瓜冰沙", "夏歌白霜"       # invented (xgbs): the repository graph has the real pair now, and its letters would be a lead
PAGE = {"title": f"{PERSON} - 萌娘百科", "url": URL, "snippet": f"别名 霜老师、XGBS、{NICK}"}


class PromptCpa(Cpa):
    def ask(self, *, call_id, prompt_text, **kw):
        self.prompts = [*getattr(self, "prompts", []), prompt_text]
        return super().ask(call_id=call_id, prompt_text=prompt_text, **kw)


def test_a_name_is_also_searched_where_other_names_are_listed_and_its_letters_reach_the_reader(tmp_path):
    """The witness: beside the host and as a streamer the words are a drink; the page that lists the name is found by
    the alias query, and the reader is shown that the page's title name has the name's first letters."""
    draft = [{"id": 1, "start_s": 0.0, "end_s": 2.0, "text": f"她跟{NICK}在一起"}, {"id": 2, "start_s": 2.0, "end_s": 4.0, "text": HEARD}]
    spot = [{"term": NICK, "cues": [1], "kind": "人物", "why": "没交代是谁", "likely": ""},
            {"term": HEARD, "cues": [2], "kind": "现成说法", "why": "像一句流行语", "likely": ""}]
    cpa = PromptCpa(spot=spot, known={NICK: (f"{PERSON}的昵称。", NICK)})
    ctx = make_ctx(tmp_path, remote=FakeRemote(), cpa=cpa)
    ctx.adapters.lookup = FakeSearch(configured=True, rows={f"{NICK} 别名 虚拟UP主": [PAGE]})
    got = references.for_subtitles(ctx, draft)
    queries = [q for q, _ in ctx.adapters.lookup.calls]
    assert queries[:3] == [f"{NICK} 李豆沙", f"{NICK} 虚拟主播", f"{NICK} 别名 虚拟UP主"]
    assert f"{HEARD} 别名 虚拟UP主" not in queries, "a saying is not looked up as somebody's other name"
    items = json.loads(cpa.prompts[-1].split("检索结果）：\n", 1)[1])
    assert items[0]["initials"] == {"letters": "XGBS", "in_results": [{"url": URL, "as": ["XGBS", PERSON]}], "known_names": []}
    assert got["leads"] == [{"heard": NICK, "cues": [1], "is": f"{PERSON}的昵称。", "wording": NICK, "sources": ["wiki.example.org"]}]


def test_a_known_name_with_the_same_letters_is_a_lead_and_is_searched_beside_the_term(tmp_path):
    from src.autoslice.knowledge_graph import Graph, Node, Source

    node = Node(id=f"person:{PERSON}", kind="person", names={"display": PERSON}, aliases=(), summary="", status="accepted",
                sources=(Source("ledger", "test"),))
    graph = graph_context.GraphContext("READ", "", Graph([node], []))
    assert graph.same_initials(NICK) == [PERSON] and graph.same_initials("南町") == [] and graph_context.GraphContext("NONE", "").same_initials(NICK) == []
    search = FakeSearch(configured=True)
    item = cc.TermContext(term=NICK, cues=[1], why="", queries=[], status="", note="", sources=[], reason="", kind="人物",
                          same_initials=[PERSON])
    assert [q["query"] for q in cc._search(search, item)][2:] == [f"{NICK} 别名 虚拟UP主", f"{NICK} {PERSON}"]


def test_what_the_graph_says_of_a_nickname_keeps_the_nickname_as_the_writing():
    from types import SimpleNamespace

    from src.autoslice.knowledge_graph import Graph, Node, Source

    accepted = Node(id=f"person:{PERSON}", kind="person", names={"display": PERSON}, aliases=(NICK,), summary="",
                    status="accepted", sources=(Source("ledger", "test"),))
    graph = SimpleNamespace(graph=Graph([accepted], []), lines=())
    lead = references._graph_lead(graph, {"term": NICK, "cues": [1]}, NICK)
    assert lead["wording"] == NICK and lead["verified"] is True
    assert lead["is"] == f"{NICK}是{PERSON}的另一个叫法"       # she said the nickname: it is not rewritten
    assert references._graph_lead(graph, {"term": "笑间妮娜", "cues": [1]}, PERSON)["wording"] == PERSON

    cand = Node(id=f"person:{PERSON}", kind="person", names={"display": PERSON}, aliases=(NICK,), summary="候选说明",
                status="candidate", sources=(Source("search", URL),))
    cand_graph = SimpleNamespace(graph=Graph([cand], []), lines=())
    clue = references._graph_lead(cand_graph, {"term": NICK, "cues": [1]}, NICK)
    assert clue["verified"] is False and clue["status"] == "candidate" and clue["wording"] == ""
    assert f"{NICK}可能是{PERSON}的另一个叫法（未核实）" in clue["is"]


def test_candidate_graph_entry_still_requests_lookup_never_supplies_wording_and_separates_clues(tmp_path, monkeypatch):
    from src.autoslice.knowledge_graph import Edge, Graph, Node, Source
    from src.autoslice.manual_lane import step3_materials

    src = (Source("crawler", URL, "2026-11-27T00:00:00Z"),)
    person = Node("person:弥月", "person", {"display": "弥月"}, (), "主播", "accepted", (Source("ledger", "repo"),))
    cand = Node("community:小博兔", "community", {"display": "小博兔"}, (), "弥月的粉丝称呼", "candidate", src)
    rel = Edge("community:小博兔|fan_name_of|person:弥月", "fan_name_of", cand.id, person.id, "candidate", src)
    graph = Graph([person, cand], [rel])
    bg = graph_context.background(graph, graph_context.mentions(graph, ["小博兔来了"]))
    monkeypatch.setattr(graph_context, "read", lambda _ctx, _texts: bg)

    draft = [{"id": 1, "start_s": 0.0, "end_s": 2.0, "text": "小博兔来了"}]
    spot = [{"term": "小博兔", "cues": [1], "kind": "人物", "why": "没交代是谁", "likely": ""}]
    cpa = Cpa(spot=spot, known={})
    ctx = make_ctx(tmp_path, remote=FakeRemote(), cpa=cpa)
    ctx.adapters.lookup = FakeSearch(configured=True, default=[])

    got = references.for_subtitles(ctx, draft)
    assert any("小博兔" in q for q, _ in ctx.adapters.lookup.calls)
    assert got["leads"] == []
    assert len(got["clues"]) == 1
    clue = got["clues"][0]
    assert clue["heard"] == "小博兔" and clue["cues"] == [1]
    assert clue["verified"] is False and clue["status"] == "candidate" and clue["wording"] == ""
    assert "弥月" in clue["is"]

    cached = references.for_subtitles(ctx, draft)
    assert cached["leads"] == [] and cached["clues"] == got["clues"] and cached["cpa"] == []

    prompt = step3_materials.base_prompt([], [], {}, 10.0, {}, {"status": "NONE"}, got)
    assert '"references": []' in prompt and '"reference_clues"' in prompt and '"小博兔"' in prompt
    agent_prompt = (graph_context.REPO_ROOT / "assets/manual_lane/prompts/subtitle_agent.md").read_text(encoding="utf-8")
    assert "reference_clues" in agent_prompt
    plain_prompt = step3_materials.base_prompt([], [], {}, 10.0, {}, {"status": "NONE"}, {"leads": [], "clues": []})
    assert '"reference_clues"' not in plain_prompt


def test_real_witness_three_reviewer_cases_keep_candidates_unverified(monkeypatch):
    from pathlib import Path
    from types import SimpleNamespace

    import pytest

    from src.autoslice.combined_knowledge_graph import load_layers
    from src.autoslice.manual_lane import collab_guests, cover_guests

    witness = Path(os.environ.get("KNOWLEDGE_RUNTIME_WITNESS") or "/nonexistent-knowledge-runtime-witness")
    if not (witness / "knowledge/nodes.jsonl").is_file() or not (witness / "knowledge/edges.jsonl").is_file():
        pytest.skip(f"witness files missing: {witness}")
    repo_g = load_layers([graph_context.SHARED_ROOT, graph_context.REPO_ROOT / "assets/lidousha"])
    g = load_layers([graph_context.SHARED_ROOT, graph_context.REPO_ROOT / "assets/lidousha", witness])

    # 1. 鼠姐 candidate alias_of edges must not merge 南町 and 花礼 or produce accepted lines
    c1 = graph_context.background(g, graph_context.mentions(g, ["鼠姐"]))
    assert [x.text for x in c1.lines if x.verified] == []
    assert not c1.covers("鼠姐")
    assert any("鼠姐可能是南町的别名（未核实）" in x.text for x in c1.lines)
    assert any("鼠姐可能是花礼的别名（未核实）" in x.text for x in c1.lines)

    # 2. community:小博兔 (candidate) must still be looked up, never fill wording or verified leads
    c2 = graph_context.background(g, graph_context.mentions(g, ["小博兔"]))
    assert g.nodes["community:小博兔"].status == "candidate"
    assert not c2.covers("小博兔")
    asked: list[str] = []
    monkeypatch.setattr(references, "_read", lambda _ctx: {})
    monkeypatch.setattr(references, "_spot", lambda _ctx, _num, _calls: (
        [{"term": "小博兔", "cues": [1], "kind": "人物", "why": "没交代是谁", "likely": ""}], ""))
    monkeypatch.setattr(graph_context, "read", lambda _ctx, _texts: c2)
    monkeypatch.setattr(cc, "look_up", lambda _ctx, terms, *_a: asked.extend(t["term"] for t in terms))
    monkeypatch.setattr(references, "remember", lambda *_a, **_kw: None)
    got = references.for_subtitles(SimpleNamespace(), [{"id": 1, "text": "小博兔来了"}])
    assert asked == ["小博兔"]
    assert got["leads"] == []
    assert len(got["clues"]) == 1
    assert got["clues"][0]["status"] == "candidate" and got["clues"][0]["verified"] is False
    assert got["clues"][0]["wording"] == ""

    # 3. person:熊猫 (candidate) must not be named as an absent person on the cover or in streamers()
    text = (graph_context.REPO_ROOT / "tests/fixtures/psp_e266_successor/current.srt").read_text(encoding="utf-8").splitlines()[330]
    monkeypatch.setattr(graph_context, "load", lambda _ctx: SimpleNamespace(graph=g))
    assert cover_guests.named_not_present(None, [text], ["李豆沙"]) == []
    comb_streamers = collab_guests.streamers(None)
    monkeypatch.setattr(graph_context, "load", lambda _ctx: SimpleNamespace(graph=repo_g))
    assert comb_streamers == collab_guests.streamers(None)


def test_candidate_clue_branch_reads_candidate_material_only_for_dual_status_alias(tmp_path, monkeypatch):
    from src.autoslice.knowledge_graph import Graph, Node, Source
    from src.autoslice.manual_lane import step3_materials

    cand_src = (Source("search", URL, "2026-11-21T22:46:47Z"),)
    acc_uncovered = Node("person:咲间妮娜", "person", {"display": "咲间妮娜"}, ("香蕉牛奶",), "", "accepted", (Source("ledger", "repo"),))
    cand_char = Node("character:咲间妮娜", "character", {"display": "咲间妮娜"}, ("香蕉牛奶",), "设定为棕发、粉瞳", "candidate", cand_src)

    # Case A: accepted node is NOT covered in prompt -> falls through to clue branch, which must read candidate only
    g_uncovered = Graph([acc_uncovered, cand_char], [])
    bg_uncovered = graph_context.background(g_uncovered, graph_context.mentions(g_uncovered, ["香蕉牛奶"]))
    assert not bg_uncovered.covers("香蕉牛奶")
    monkeypatch.setattr(graph_context, "read", lambda _ctx, _texts: bg_uncovered)
    ctx_a = _ctx(tmp_path / "a", Cpa(spot=[{"term": "香蕉牛奶", "cues": [1], "kind": "人物", "why": "没交代是谁", "likely": ""}], known={}))
    ctx_a.adapters.lookup = FakeSearch(configured=True, default=[])
    got_a = references.for_subtitles(ctx_a, [{"id": 1, "start_s": 0.0, "end_s": 2.0, "text": "香蕉牛奶来了"}])
    assert any("香蕉牛奶" in q for q, _ in ctx_a.adapters.lookup.calls)
    assert got_a["leads"] == [] and len(got_a["clues"]) == 1
    clue = got_a["clues"][0]
    assert clue["verified"] is False and clue["status"] == "candidate" and clue["wording"] == ""
    assert clue["sources"] == ["知识图（未核实候选）"]
    assert "香蕉牛奶可能是咲间妮娜的另一个叫法（未核实）" in clue["is"]
    assert "设定为棕发、粉瞳" in clue["is"] and "香蕉牛奶是咲间妮娜的另一个叫法" not in clue["is"]
    prompt_a = json.loads(step3_materials.base_prompt([], [], {}, 10.0, {}, {"status": "NONE"}, got_a).rsplit("初始转写与片长：", 1)[1])
    assert prompt_a["references"] == [] and prompt_a["reference_clues"] == got_a["clues"]

    # Case B: accepted node IS covered -> goes to references only, nothing accepted enters reference_clues
    acc_covered = Node("person:咲间妮娜", "person", {"display": "咲间妮娜"}, ("香蕉牛奶",), "活跃在 bilibili 的虚拟主播", "accepted", (Source("ledger", "repo"),))
    g_covered = Graph([acc_covered, cand_char], [])
    bg_covered = graph_context.background(g_covered, graph_context.mentions(g_covered, ["香蕉牛奶"]))
    assert bg_covered.covers("香蕉牛奶")
    monkeypatch.setattr(graph_context, "read", lambda _ctx, _texts: bg_covered)
    ctx_b = _ctx(tmp_path / "b", Cpa(spot=[{"term": "香蕉牛奶", "cues": [1], "kind": "人物", "why": "没交代是谁", "likely": ""}], known={}))
    ctx_b.adapters.lookup = FakeSearch(configured=True, default=[])
    got_b = references.for_subtitles(ctx_b, [{"id": 1, "start_s": 0.0, "end_s": 2.0, "text": "香蕉牛奶来了"}])
    assert ctx_b.adapters.lookup.calls == []
    assert len(got_b["leads"]) == 1 and got_b["leads"][0]["status"] == "accepted" and got_b["leads"][0]["wording"] == "香蕉牛奶"
    assert got_b["clues"] == []
    prompt_b = json.loads(step3_materials.base_prompt([], [], {}, 10.0, {}, {"status": "NONE"}, got_b).rsplit("初始转写与片长：", 1)[1])
    assert "reference_clues" not in prompt_b
    direct_clue = references._graph_clue(bg_covered, {"term": "香蕉牛奶", "cues": [1]}, "香蕉牛奶")
    assert direct_clue["verified"] is False and direct_clue["status"] == "candidate" and direct_clue["wording"] == ""


def test_real_witness_banana_milk_and_seki_match_repo_references_and_prompt_contract(monkeypatch):
    from pathlib import Path
    from types import SimpleNamespace

    import pytest

    from src.autoslice.combined_knowledge_graph import load_layers
    from src.autoslice.manual_lane import step3_materials

    witness = Path(os.environ.get("KNOWLEDGE_RUNTIME_WITNESS") or "/nonexistent-knowledge-runtime-witness")
    if not (witness / "knowledge/nodes.jsonl").is_file() or not (witness / "knowledge/edges.jsonl").is_file():
        pytest.skip(f"witness files missing: {witness}")
    repo = load_layers([graph_context.SHARED_ROOT, graph_context.REPO_ROOT / "assets/lidousha"])
    combined = load_layers([graph_context.SHARED_ROOT, graph_context.REPO_ROOT / "assets/lidousha", witness])
    search = FakeSearch(configured=True, default=[])

    def _run(g, text, term):
        ctx = SimpleNamespace(state=SimpleNamespace(data={"clip": {"recording_date": "2026-11-23"}}),
                              adapters=SimpleNamespace(lookup=search))
        spot = [{"term": term, "cues": [1], "kind": "人物", "why": "没交代是谁", "likely": ""}]
        monkeypatch.setattr(graph_context, "load", lambda _ctx: graph_context.GraphContext(graph_context.COMBINED, "", g))
        monkeypatch.setattr(references, "_read", lambda _ctx: {})
        monkeypatch.setattr(references, "_spot", lambda _ctx, _num, _calls: (spot, ""))
        monkeypatch.setattr(references, "remember", lambda *_a, **_kw: None)
        refs = references.for_subtitles(ctx, [{"id": 1, "text": text}])
        prompt = step3_materials.base_prompt([], [], {}, 10.0, {}, {"status": "NONE"}, refs)
        return json.loads(prompt.rsplit("初始转写与片长：", 1)[1])

    seki_text = (graph_context.REPO_ROOT / "tests/fixtures/subtitle_audio_anchor_ambiguity/01-kiss.srt").read_text(encoding="utf-8").splitlines()[6]
    seki_repo = _run(repo, seki_text, "星汐")
    seki_comb = _run(combined, seki_text, "星汐")
    assert seki_comb["references"] == seki_repo["references"]
    assert len(seki_comb["references"]) == 1 and "星汐隶属于PSPLive" in seki_comb["references"][0]["is"]
    assert search.calls == []

    banana_repo = _run(repo, "香蕉牛奶、小千村柚柚、小李、尾幼、星汐", "香蕉牛奶")
    banana_comb = _run(combined, "香蕉牛奶、小千村柚柚、小李、尾幼、星汐", "香蕉牛奶")
    assert banana_comb["references"] == banana_repo["references"]
    assert len(banana_comb["references"]) == 1 and banana_comb["references"][0]["status"] == "accepted"
    assert banana_comb.get("reference_clues") is None
    assert search.calls == []


def test_stale_unversioned_references_cache_is_recomputed_from_scratch(tmp_path, monkeypatch):
    import hashlib

    from src.autoslice.knowledge_graph import Edge, Graph, Node, Source

    src = (Source("crawler", URL, "2026-11-27T00:00:00Z"),)
    person = Node("person:弥月", "person", {"display": "弥月"}, (), "主播", "accepted", (Source("ledger", "repo"),))
    cand = Node("community:小博兔", "community", {"display": "小博兔"}, (), "弥月的粉丝称呼", "candidate", src)
    rel = Edge("community:小博兔|fan_name_of|person:弥月", "fan_name_of", cand.id, person.id, "candidate", src)
    graph = Graph([person, cand], [rel])
    bg = graph_context.background(graph, graph_context.mentions(graph, ["小博兔来了", HEARD]))
    monkeypatch.setattr(graph_context, "read", lambda _ctx, _texts: bg)

    draft = [{"id": 1, "start_s": 0.0, "end_s": 2.0, "text": "小博兔来了"},
             {"id": 2, "start_s": 2.0, "end_s": 4.0, "text": HEARD}]
    sha = hashlib.sha256(f"1 小博兔来了\n2 {HEARD}".encode()).hexdigest()
    spot = [{"term": "小博兔", "cues": [1], "kind": "人物", "why": "没交代是谁", "likely": ""},
            {"term": HEARD, "cues": [2], "kind": "现成说法", "why": "像流行语", "likely": SAYING}]

    ctx = _ctx(tmp_path, Cpa(spot=spot, known={HEARD: ("新查得的流行语解释。", SAYING)}))
    ctx.adapters.lookup = FakeSearch(configured=True, rows={SAYING: ROWS})
    old_cache = {
        "draft_sha256": sha,
        "graph_leads": [{"heard": "小博兔", "cues": [1], "is": "小博兔（社群）：弥月的粉丝称呼", "wording": "小博兔", "sources": ["知识图"]}],
        "references": [{"term": HEARD, "cues": [2], "why": "像流行语", "queries": [], "status": cc.FOUND,
                        "note": "旧缓存里的旧解释。", "wording": "旧写法", "sources": [URL], "reason": "",
                        "stage": references.SUBTITLES}],
        "line": "old line",
    }
    store_path = ctx.clip_dir / references.STORE
    store_path.parent.mkdir(parents=True, exist_ok=True)
    store_path.write_text(json.dumps(old_cache, ensure_ascii=False, indent=2), encoding="utf-8")

    got = references.for_subtitles(ctx, draft)
    assert all(lead["heard"] != "小博兔" for lead in got["leads"])
    assert got["leads"] == [{"heard": HEARD, "cues": [2], "is": "新查得的流行语解释。",
                             "wording": SAYING, "sources": ["wiki.example.org"]}]
    assert len(got["clues"]) == 1
    assert got["clues"][0]["heard"] == "小博兔" and got["clues"][0]["status"] == "candidate"
    assert got["clues"][0]["verified"] is False and got["clues"][0]["wording"] == ""
    searched = [q for q, _ in ctx.adapters.lookup.calls]
    assert any("小博兔" in q for q in searched) and any(SAYING in q for q in searched)

    saved = json.loads(store_path.read_text(encoding="utf-8"))
    assert saved["schema_version"] == references.SCHEMA_VERSION
    assert saved["subtitles"] == {
        "schema_version": references.SCHEMA_VERSION,
        "draft_sha256": sha,
        "leads": got["leads"],
        "clues": got["clues"],
        "line": got["line"],
    }

    cached = references.for_subtitles(ctx, draft)
    assert cached["leads"] == got["leads"] and cached["clues"] == got["clues"] and cached["cpa"] == []


def test_changed_draft_re_looks_up_same_term_instead_of_reusing_prior_draft_result(tmp_path, monkeypatch):
    from src.autoslice.finished_media_review.common import srt_cues
    from src.autoslice.knowledge_graph import Graph
    from src.autoslice.manual_lane import step3_materials

    bg = graph_context.GraphContext(graph_context.COMBINED, "", Graph([], []))
    monkeypatch.setattr(graph_context, "read", lambda _ctx, _texts: bg)

    events: list[tuple[str, str]] = []

    def _spot(_ctx, numbered, _calls):
        text = next(iter(numbered.values()))
        likely = "PlayStation Portable" if "游戏机" in text else "PSP LIVE"
        cue = next(iter(numbered))
        return ([{"term": "PSP", "cues": [cue], "kind": "团体", "why": "指称待查", "likely": likely}], "")

    def _lookup(_ctx, terms, *_args):
        for item in terms:
            events.append((item["term"], item.get("likely", "")))
            game = item.get("likely") == "PlayStation Portable"
            cc._mark(item, cc.FOUND, note="掌上游戏机" if game else "虚拟主播团体",
                     sources=["https://wiki.example.org/game" if game else "https://wiki.example.org/group"])
            item["wording"] = "PlayStation Portable" if game else "PSP LIVE"
        return ""

    monkeypatch.setattr(references, "_spot", _spot)
    monkeypatch.setattr(cc, "look_up", _lookup)
    monkeypatch.setattr(references, "_offer", lambda *_a, **_kw: None)

    cue26 = next(c for c in srt_cues((graph_context.REPO_ROOT / "tests/fixtures/psp_e266_successor/current.srt").read_text(encoding="utf-8"))
                 if c["index"] == 26)
    old_draft = [{"id": 26, "start_s": 0.0, "end_s": 2.0, "text": "小时候玩我的 PSP 游戏机"}]
    new_draft = [{"id": 26, "start_s": 0.0, "end_s": 2.0, "text": cue26["text"]}]
    ctx = _ctx(tmp_path, Cpa())

    first = references.for_subtitles(ctx, old_draft)
    assert first["leads"][0]["wording"] == "PlayStation Portable"
    events.clear()

    after = references.for_subtitles(ctx, new_draft)
    assert events == [("PSP", "PSP LIVE")]
    assert after["leads"][0]["wording"] == "PSP LIVE" and "虚拟主播团体" in after["leads"][0]["is"]
    prompt = json.loads(step3_materials.base_prompt(new_draft, [], {}, 10.0, {}, {"status": "NONE"}, after).rsplit("初始转写与片长：", 1)[1])
    assert prompt["references"][0]["wording"] == "PSP LIVE"

    events.clear()
    cached = references.for_subtitles(ctx, new_draft)
    assert events == [] and cached["cpa"] == [] and cached["leads"] == after["leads"]


def test_removed_term_from_earlier_draft_does_not_return_on_cache_hit(tmp_path, monkeypatch):
    import hashlib

    from src.autoslice.finished_media_review.common import srt_cues
    from src.autoslice.knowledge_graph import Graph
    from src.autoslice.manual_lane import step3_materials

    bg = graph_context.GraphContext(graph_context.COMBINED, "", Graph([], []))
    monkeypatch.setattr(graph_context, "read", lambda _ctx, _texts: bg)
    monkeypatch.setattr(references, "_offer", lambda *_a, **_kw: None)

    q_cue = next(c for c in srt_cues((graph_context.REPO_ROOT / "tests/fixtures/psp_e266_successor/current.srt").read_text(encoding="utf-8"))
                 if c["index"] == 46)
    p_cue = next(c for c in srt_cues((graph_context.REPO_ROOT / "tests/fixtures/psp_e266_successor/parent.srt").read_text(encoding="utf-8"))
                 if c["index"] == 46)
    assert "抢了家族" in p_cue["text"] and "抢了家族" not in q_cue["text"]

    current = [{"id": 46, "start_s": 0.0, "end_s": 2.0, "text": q_cue["text"]}]
    ctx = _ctx(tmp_path, Cpa(spot=[{"term": "channel家族", "cues": [46], "kind": "团体", "why": "指称待查", "likely": ""}], known={}))
    ctx.adapters.lookup = FakeSearch(configured=True, default=[])

    store_path = ctx.clip_dir / references.STORE
    store_path.parent.mkdir(parents=True, exist_ok=True)
    old_doc = {
        "draft_sha256": hashlib.sha256(f"46 {p_cue['text']}".encode()).hexdigest(),
        "references": [{"term": "抢了家族", "cues": [46], "why": "指称待查", "queries": [], "status": cc.FOUND,
                        "note": "旧稿家族（模拟）", "wording": "抢了家族", "sources": [URL], "reason": "",
                        "stage": references.SUBTITLES}],
        "line": "old line",
    }
    store_path.write_text(json.dumps(old_doc, ensure_ascii=False, indent=2), encoding="utf-8")

    first = references.for_subtitles(ctx, current)
    cached = references.for_subtitles(ctx, current)
    assert first["leads"] == []
    assert cached["leads"] == [] and cached["cpa"] == []
    prompt = json.loads(step3_materials.base_prompt(current, [], {}, 10.0, {}, {"status": "NONE"}, cached).rsplit("初始转写与片长：", 1)[1])
    assert prompt["references"] == []
    assert "抢了家族" in references.load(ctx)
    assert json.loads(store_path.read_text(encoding="utf-8"))["subtitles"]["leads"] == []
