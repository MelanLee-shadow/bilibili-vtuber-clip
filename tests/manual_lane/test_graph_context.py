"""Step 6 reads the knowledge graph before it goes to the web (graph_context, circle_context.gather).  The graphs here are
small made-up ones written to a temporary directory (invented names, no real clip's subtitles); no model, no network."""

from __future__ import annotations

import os
import json
import random
from pathlib import Path
from types import SimpleNamespace

import pytest
from fakes import MERGE, FakeCpa, FakeRemote, FakeSearch, make_ctx

from src.autoslice.combined_knowledge_graph import load_layers
from src.autoslice.knowledge_graph import Edge, Graph, GraphError, Node, Source, dump_graph
from src.autoslice.manual_lane import circle_context as cc
from src.autoslice.manual_lane import graph_context as gc
from src.autoslice.manual_lane import stage_impl as si
from src.autoslice.manual_lane.layout import layout
from src.autoslice.manual_lane.runner import StepOutcome

LEDGER = (Source("ledger", "assets/x.json", None),)
CRAWL = "https://news.example.net/a/1"
CRAWL_AT = "2026-10-05T01:02:03+00:00"
SEARCH = (Source("search", CRAWL, CRAWL_AT),)
RUNTIME = layout().news_graph_root


def node(name, kind="person", *, summary="", status="accepted", aliases=(), official=None, valid=None, sources=None,
         subtitle=None):
    names = {"display": name, **({"official": official} if official else {})}
    return Node(f"{kind}:{name}", kind, names, tuple(aliases), summary, status,
                sources or (LEDGER if status == "accepted" else SEARCH), valid, subtitle)


def edge(a, kind, b, *, status="accepted", valid=None):
    return Edge(f"{a.id}|{kind}|{b.id}", kind, a.id, b.id, status, LEDGER if status == "accepted" else SEARCH,
                valid=valid)


GROUP = node("甲团", "group", summary="甲团是虚构的团体", aliases=("甲组",), official="KoTeam")
MEMBER = node("乙某", summary="乙某是甲团成员", aliases=("乙乙",))
OTHER = node("戊某", summary="戊某与本片无关")
PROJECT = node("丙企划", "project", summary="丙企划是虚构的企划")
EVENT = node("丁大会", "event", summary="丁大会是爬虫读到的活动", status="candidate", valid={"from": "2026-11-01", "until": None})
REPO = Graph([GROUP, MEMBER, OTHER, PROJECT], [edge(MEMBER, "member_of", GROUP), edge(OTHER, "member_of", GROUP),
                                               edge(MEMBER, "participates_in", PROJECT)])
CANDIDATES = Graph([EVENT], [edge(MEMBER, "participates_in", EVENT, status="candidate")])


def _write(root, graph):
    dump_graph(graph, root)
    return root


@pytest.fixture
def repo(tmp_path, monkeypatch):
    root = _write(tmp_path / "repo", REPO)
    monkeypatch.setattr(gc, "load_channel_profile", lambda _root: SimpleNamespace(asset_root=root))
    return root


def _ctx(tmp_path, *, runtime=CANDIDATES, cpa=None):
    remote = FakeRemote()
    if runtime is not None:
        stage = _write(tmp_path / "runtime", runtime)
        for rel in ("knowledge/nodes.jsonl", "knowledge/edges.jsonl"):
            remote.files[f"{RUNTIME}/{rel}"] = (stage / rel).read_bytes()
    return make_ctx(tmp_path / "ctx", remote=remote, cpa=cpa or FakeCpa([]))


def _ids(nodes):
    return [n.id for n in nodes]


# ----- naming: which entities a clip names -----------------------------------------------------------------------
def test_an_alias_an_official_name_and_another_case_each_name_the_entity(repo):
    graph = Graph([GROUP, MEMBER, OTHER], [])
    assert _ids(gc.mentions(graph, ["今天聊到乙乙的事"])) == [MEMBER.id]
    assert _ids(gc.mentions(graph, ["the kOTEAM thing"])) == [GROUP.id]
    assert _ids(gc.mentions(graph, ["甲组和乙某"])) == [GROUP.id, MEMBER.id]
    assert gc.mentions(graph, ["xkoteamx 没有写成整词"]) == []


def test_short_common_and_table_excluded_spellings_do_not_name_an_entity():
    one_char = node("某", summary="单字")
    short_latin = node("Q", "group", summary="一个字母")
    common = node("what", "term", summary="常见英文词")
    off = node("甲乙丙", "term", summary="专名表标了不检测", subtitle={"detection_name": False})
    graph = Graph([one_char, short_latin, common, off], [])
    assert gc.mentions(graph, ["某 Q what 甲乙丙"]) == []


# ----- background: accepted and candidate apart ------------------------------------------------------------------
def test_a_member_brings_its_group_and_the_group_summary(repo, tmp_path):
    context = gc.read(_ctx(tmp_path, runtime=None), ["乙乙来了"])
    block = context.block()
    assert "乙某隶属于甲团" in block and "甲团是虚构的团体" in block and "丙企划是虚构的企划" in block
    assert "戊某" not in block                         # an entity the clip does not name stays out
    assert gc.CLUES_HEAD not in block and context.status == gc.REPO_ONLY


def test_candidates_are_unverified_leads_with_their_source_and_never_assertion_grounds(repo, tmp_path):
    context = gc.read(_ctx(tmp_path), ["乙乙来了"])
    accepted, clues = context.block().split("\n\n")
    assert accepted.startswith(gc.VERIFIED_HEAD) and clues.startswith(gc.CLUES_HEAD)
    assert "不得作为标题断言的依据" in clues
    assert "丁大会" in clues and "丁大会" not in accepted
    assert "（出处：news.example.net，2026-10-05）" in clues and "时间：2026-11-01~" in clues
    assert "丙企划" in accepted and context.status == gc.COMBINED and context.reason == ""


def test_an_entity_that_is_only_a_candidate_is_a_lead_too(repo, tmp_path):
    only = Graph([EVENT], [])
    context = gc.background(only, gc.mentions(only, ["丁大会开始了"]))
    assert context.block().startswith(gc.CLUES_HEAD) and "丁大会" in context.block()


# ----- caps and order --------------------------------------------------------------------------------------------
def _fan(n_hubs, n_each):
    hubs = [node(f"枢{i}号") for i in range(n_hubs)]
    projects = [[node(f"企{i}-{j}", "project", summary="企划", valid={"from": f"2026-0{1 + j % 9}-01", "until": None})
                 for j in range(n_each)] for i in range(n_hubs)]
    nodes = [*hubs, *(p for row in projects for p in row)]
    edges = [edge(h, "participates_in", p) for h, row in zip(hubs, projects, strict=True) for p in row]
    return nodes, edges, hubs


def test_one_entity_keeps_at_most_six_neighbours_the_newest_first():
    nodes, edges, hubs = _fan(1, 9)
    graph = Graph(nodes, edges)
    context = gc.background(graph, [hubs[0]])
    kept = [line for line in context.lines if not line.own]
    assert len(kept) == 6
    assert min(line.newest for line in kept) >= "2026-04-01"       # the three oldest windows were cut


def test_the_whole_clip_is_capped_and_a_shared_neighbour_ranks_first():
    nodes, edges, hubs = _fan(5, 6)
    shared = node("共企", "project", summary="两个实体共有")
    nodes.append(shared)
    edges += [edge(hubs[0], "participates_in", shared), edge(hubs[1], "participates_in", shared)]
    graph = Graph(nodes, edges)
    context = gc.background(graph, hubs)
    assert len(context.lines) == 20
    assert shared.id in context.lines[0].ids and context.lines[0].conn == 2


def test_the_order_does_not_depend_on_the_order_of_the_input():
    nodes, edges, hubs = _fan(4, 8)
    first = gc.background(Graph(nodes, edges), hubs).block()
    random.Random(7).shuffle(nodes)
    random.Random(8).shuffle(edges)
    assert gc.background(Graph(nodes, edges), list(reversed(hubs))).block() == first


# ----- degradation -----------------------------------------------------------------------------------------------
def test_no_runtime_directory_leaves_the_repository_graph(repo, tmp_path):
    loaded = gc.load(_ctx(tmp_path, runtime=None))
    assert loaded.status == gc.REPO_ONLY and "运行时候选图读不到" in loaded.reason and loaded.graph is not None
    assert loaded.graph is not None and EVENT.id not in loaded.graph.nodes and MEMBER.id in loaded.graph.nodes


@pytest.mark.parametrize("damage", ["not json at all\n", None], ids=["garbage", "edge-to-nowhere"])
def test_damaged_candidate_files_leave_the_repository_graph_and_say_why(repo, tmp_path, damage):
    ctx = _ctx(tmp_path)
    if damage is None:
        lost = Graph([], [edge(MEMBER, "participates_in", EVENT, status="candidate")])
        stage = _write(tmp_path / "lost", lost)
        ctx.adapters.remote.files[f"{RUNTIME}/knowledge/nodes.jsonl"] = (stage / "knowledge/nodes.jsonl").read_bytes()
        ctx.adapters.remote.files[f"{RUNTIME}/knowledge/edges.jsonl"] = (stage / "knowledge/edges.jsonl").read_bytes()
    else:
        ctx.adapters.remote.files[f"{RUNTIME}/knowledge/nodes.jsonl"] = damage.encode()
    loaded = gc.load(ctx)
    assert loaded.status == gc.REPO_ONLY and "运行时候选图损坏" in loaded.reason
    assert loaded.graph is not None and EVENT.id not in loaded.graph.nodes


def test_an_unreadable_repository_graph_means_no_graph(tmp_path, monkeypatch):
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setattr(gc, "load_channel_profile", lambda _root: SimpleNamespace(asset_root=empty))
    context = gc.read(_ctx(tmp_path), ["乙乙来了"])
    assert context.status == gc.NONE and "仓库图读不到" in context.reason and context.block() == ""
    assert not context.covers("乙乙") and context.record()["mentioned"] == []


# ----- graph first, then the web ---------------------------------------------------------------------------------
def _gather(ctx, context, *terms, search=None):
    ctx.adapters.lookup = search or FakeSearch(configured=True)
    merged = dict(json.loads(MERGE), unexplained=[{"term": t, "cues": [1], "why": "x"} for t in terms])
    return cc.gather(ctx, merged, "1 句子1\n", [], context)


def test_a_reference_the_graph_knows_is_not_searched_and_an_unknown_one_still_is(repo, tmp_path):
    ctx = _ctx(tmp_path)
    context = gc.read(ctx, ["乙乙来了，还提到了丁大会"])
    search = FakeSearch(configured=True)
    found = _gather(ctx, context, "乙乙", "丁大会", "没人知道的词", search=search)
    assert [t["term"] for t in found["terms"]] == ["丁大会", "没人知道的词"]
    assert search.queries == [
        "丁大会 李豆沙", "丁大会 虚拟主播", *["丁大会 别名 虚拟UP主"] * 2,
        "没人知道的词 李豆沙", "没人知道的词 虚拟主播", *["没人知道的词 别名 虚拟UP主"] * 2,
    ]
    assert found["graph"]["skipped_search"] == ["乙乙"]       # accepted node skips search; candidate node is still searched


def test_a_reference_in_the_graph_but_without_background_in_the_prompt_is_still_searched(repo, tmp_path):
    ctx = _ctx(tmp_path)
    context = gc.read(ctx, ["今天没有提到任何实体"])
    search = FakeSearch(configured=True)
    found = _gather(ctx, context, "乙乙", search=search)
    assert [t["term"] for t in found["terms"]] == ["乙乙"] and len(search.calls) == 3
    assert found["graph"]["skipped_search"] == []


# ----- the record ------------------------------------------------------------------------------------------------
def test_the_record_says_what_was_read_shown_and_saved(repo, tmp_path):
    ctx = _ctx(tmp_path)
    context = gc.read(ctx, ["乙乙来了"])
    found = _gather(ctx, context, "乙乙")
    on_disk = json.loads((ctx.clip_dir / "title/context.json").read_text(encoding="utf-8"))
    assert on_disk["graph"] == found["graph"]
    record = on_disk["graph"]
    assert (record["status"], record["reason"]) == ("combined", "")
    assert record["mentioned"] == [{"id": MEMBER.id, "status": "accepted"}]
    assert {"id": EVENT.id, "status": "candidate"} in record["in_prompt"]
    assert {"id": OTHER.id, "status": "accepted"} not in record["in_prompt"]
    assert record["lines"]["verified"] >= 2 and record["lines"]["clues"] == 1 and record["skipped_search"] == ["乙乙"]
    assert gc.process_line(record).startswith("知识图：仓库图加运行时候选") and "免于上网：乙乙" in gc.process_line(record)


def test_a_degraded_read_is_recorded_with_its_reason(repo, tmp_path):
    ctx = _ctx(tmp_path, runtime=None)
    found = _gather(ctx, gc.read(ctx, ["乙乙来了"]))
    assert found["graph"]["status"] == "repo_only" and "运行时候选图读不到" in found["graph"]["reason"]
    assert "（运行时候选图读不到" in gc.process_line(found["graph"])


# ----- step 6 end to end -----------------------------------------------------------------------------------------
def _answer(*bodies):
    cands = [{"title_body": body, "on_stage": ["李豆沙"], "punchline_person": "她先嘴硬",
              "swap_check": {"holds_with_other_host": False, "reason": "换主播不成立"}} for body in bodies]
    return json.dumps({"candidates": cands, "recommended": 0, "cover_lines": ["破防了", "半分钟"]}, ensure_ascii=False)


def _srt():
    rows = ["乙乙今天来聊天", "丁大会是怎么回事", "她半分钟后破防了"]
    return "\n\n".join(f"{i}\n{si.fmt_ts((i - 1) * 2.0)} --> {si.fmt_ts(i * 2.0)}\n{t}"
                       for i, t in enumerate(rows, 1)) + "\n"


def _pick(tmp_path, *, runtime=CANDIDATES):
    cpa = FakeCpa([_answer("她半分钟后真的破防了", "她先嘴硬后拆台")])
    ctx = _ctx(tmp_path, runtime=runtime, cpa=cpa)
    ctx.adapters.lookup = FakeSearch(configured=True)
    (ctx.clip_dir / "subs").mkdir(parents=True, exist_ok=True)
    (ctx.clip_dir / "subs/final.srt").write_text(_srt(), encoding="utf-8")
    return ctx, cpa, si.pick_title(ctx)


def test_the_graph_background_reaches_the_title_and_ranking_prompts_before_any_web_background(repo, tmp_path):
    ctx, cpa, picked = _pick(tmp_path)
    assert not isinstance(picked, StepOutcome), picked
    assert cpa.title_prompts
    for prompt in cpa.title_prompts:
        assert gc.VERIFIED_HEAD in prompt and gc.CLUES_HEAD in prompt and "乙某隶属于甲团" in prompt
        assert prompt.index("## 本片主线") < prompt.index(gc.VERIFIED_HEAD) < prompt.index(gc.CLUES_HEAD)
    pick = cpa.prompts[cpa.calls.index("title-pick-r0")]
    assert gc.VERIFIED_HEAD in pick and gc.CLUES_HEAD in pick
    assert any(line.startswith("知识图：仓库图加运行时候选；本片提到 ") for line in picked[2])


def test_step_6_completes_without_the_runtime_graph_and_without_any_graph(repo, tmp_path, monkeypatch):
    ctx, cpa, picked = _pick(tmp_path / "a", runtime=None)
    assert not isinstance(picked, StepOutcome), picked
    assert gc.VERIFIED_HEAD in cpa.title_prompts[0] and gc.CLUES_HEAD not in cpa.title_prompts[0]
    assert json.loads((ctx.clip_dir / "title/context.json").read_text(encoding="utf-8"))["graph"]["status"] == "repo_only"

    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setattr(gc, "load_channel_profile", lambda _root: SimpleNamespace(asset_root=empty))
    ctx, cpa, picked = _pick(tmp_path / "b")
    assert not isinstance(picked, StepOutcome), picked
    assert gc.VERIFIED_HEAD not in cpa.title_prompts[0] and gc.CLUES_HEAD not in cpa.title_prompts[0]
    record = json.loads((ctx.clip_dir / "title/context.json").read_text(encoding="utf-8"))["graph"]
    assert record["status"] == "none" and "仓库图读不到" in record["reason"]
    assert any(line.startswith("知识图：没有用图") for line in picked[2])


# ----- time as news against the clip's day (owner) -----------------------------------------------------
def test_news_is_placed_against_the_clip_day_over_is_said_over_and_far_off_is_left_out():
    import datetime as dt
    festival = node("七天乐", "event", summary="国庆活动", status="candidate", valid={"from": "2026-10-01", "until": "2026-10-14"})
    ancient = node("旧大会", "event", summary="很久以前", status="candidate", valid={"from": "2025-01-01", "until": "2025-01-10"})
    league = node("老联盟", "group", summary="长期联盟", status="candidate", valid={"from": "2024-01-01", "until": None})
    graph = Graph([MEMBER, festival, ancient, league],
                  [edge(MEMBER, "participates_in", x, status="candidate") for x in (festival, ancient, league)])
    during = gc.background(graph, [MEMBER], dt.date(2026, 10, 5)).block()
    after = gc.background(graph, [MEMBER], dt.date(2026, 11, 16)).block()
    assert "七天乐" in during and "已结束" not in during.split("七天乐")[1].split("\n")[0]
    assert "七天乐" in after and "本片录制时已结束" in after
    assert "旧大会" not in after and "老联盟" in after
    assert "旧大会" in gc.background(graph, [MEMBER]).block(), "no clip day: nothing is filtered"


# ----- circle-wide terms every channel reads (U430/U431: 嗦面) ------------------------------------------------------
def test_shared_circle_terms_join_every_channel_graph_and_never_collide():
    from src.autoslice.knowledge_graph import load_graph
    shared = load_graph(gc.SHARED_ROOT)
    assert "term:嗦面" in shared.nodes and all(n.kind == "term" for n in shared.nodes.values())
    for nodes in sorted((gc.REPO_ROOT / "assets").glob("*/knowledge/nodes.jsonl")):
        root = nodes.parent.parent
        if root == gc.SHARED_ROOT:
            continue
        combined = gc.load_layers([gc.SHARED_ROOT, root])     # raises on within-layer duplicate id or a dangling edge
        assert "term:嗦面" in combined.nodes, root
        assert not combined.shadowed, (root, combined.shadowed)


def test_chat_brings_in_circle_terms_but_not_the_people_it_names(repo, tmp_path):
    context = gc.read(_ctx(tmp_path), ["谢谢大家一直陪着我"], chat=["长嗦😭", "乙乙来了"])
    named = [m["id"] for m in context.record()["mentioned"]]
    assert "term:嗦面" in named and MEMBER.id not in named
    assert "嗦面" in context.block()
    assert not [m for m in gc.read(_ctx(tmp_path / "b"), ["谢谢大家一直陪着我"]).record()["mentioned"] if m["id"] == "term:嗦面"]


# ----- layered shadowing across authority layers -----------------------------------------------------------------
def test_load_layers_shadows_lower_layer_duplicate_node_and_edge_ids_and_resolves_edges(tmp_path):
    shared_src = (Source("owner", "u1", "2026-11-01T00:00:00Z"),)
    shared = _write(tmp_path / "shared", Graph([
        node("甲词", "term", summary="共享层释义", aliases=("甲甲",), sources=shared_src),
        node("候词", "term", summary="共享候选", status="candidate", sources=LEDGER),
    ], []))
    channel = _write(tmp_path / "channel", Graph([MEMBER, PROJECT], [edge(MEMBER, "participates_in", PROJECT)]))
    runtime = _write(tmp_path / "runtime", Graph([
        node("甲词", "term", summary="低层释义", aliases=("低层别名",), status="candidate", sources=SEARCH),
        node("候词", "term", summary="低层升级", aliases=("低层升级",), status="accepted", sources=SEARCH),
        node("丁某", "person", summary="运行时人物", status="candidate", sources=SEARCH),
    ], [
        Edge("term:甲词|related_to|person:丁某", "related_to", "term:甲词", "person:丁某", "candidate", SEARCH),
        edge(MEMBER, "participates_in", PROJECT, status="candidate"),
    ]))

    combined = load_layers([shared, channel, runtime])
    standing_term = combined.nodes["term:甲词"]
    assert standing_term.status == "accepted"
    assert standing_term.aliases == ("甲甲",)
    assert standing_term.summary == "共享层释义"
    assert standing_term.sources == shared_src

    standing_cand = combined.nodes["term:候词"]
    assert standing_cand.status == "candidate"
    assert standing_cand.aliases == ()
    assert standing_cand.summary == "共享候选"

    assert combined.nodes["person:丁某"].status == "candidate"
    assert combined.neighbors("term:甲词") == []
    assert [o.id for _, o in combined.neighbors("term:甲词", status=("accepted", "candidate"))] == ["person:丁某"]

    standing_edge = next(e for e in combined.edges if e.id == "person:乙某|participates_in|project:丙企划")
    assert standing_edge.status == "accepted" and standing_edge.sources == LEDGER

    assert combined.shadowed_nodes == ("term:候词", "term:甲词")
    assert combined.shadowed_edges == ("person:乙某|participates_in|project:丙企划",)
    assert combined.shadowed == ("term:候词", "term:甲词", "person:乙某|participates_in|project:丙企划")


def test_load_layers_rejects_within_layer_duplicates_dangling_edges_and_bad_json(tmp_path):
    high = _write(tmp_path / "high", Graph([node("甲词", "term", summary="高层")], []))

    dup_node_alone = _write(tmp_path / "dup_node_alone", Graph([], []))
    row = (high / "knowledge/nodes.jsonl").read_text(encoding="utf-8")
    (dup_node_alone / "knowledge/nodes.jsonl").write_text(row + row, encoding="utf-8")
    with pytest.raises(GraphError, match="duplicate node ids"):
        load_layers([dup_node_alone])
    with pytest.raises(GraphError, match="duplicate node ids"):
        load_layers([high, dup_node_alone])

    dup_edge_dir = _write(tmp_path / "dup_edge", Graph([MEMBER, PROJECT], [edge(MEMBER, "participates_in", PROJECT)]))
    erow = (dup_edge_dir / "knowledge/edges.jsonl").read_text(encoding="utf-8")
    (dup_edge_dir / "knowledge/edges.jsonl").write_text(erow + erow, encoding="utf-8")
    with pytest.raises(GraphError, match="duplicate edge ids"):
        load_layers([dup_edge_dir])

    dangling = _write(tmp_path / "dangling", Graph([], [edge(MEMBER, "participates_in", EVENT, status="candidate")]))
    with pytest.raises(GraphError, match="is not a node"):
        load_layers([high, dangling])

    bad_json = _write(tmp_path / "bad_json", Graph([], []))
    (bad_json / "knowledge/nodes.jsonl").write_text("{not json\n", encoding="utf-8")
    with pytest.raises(ValueError):
        load_layers([high, bad_json])

    bad_eid = _write(tmp_path / "bad_eid", Graph([MEMBER, PROJECT], []))
    (bad_eid / "knowledge/edges.jsonl").write_text(json.dumps({
        "id": "wrong-id", "type": "participates_in", "from": MEMBER.id, "to": PROJECT.id,
        "status": "candidate", "sources": [{"kind": "search", "ref": CRAWL, "at": CRAWL_AT}],
    }, ensure_ascii=False) + "\n", encoding="utf-8")
    with pytest.raises(GraphError, match="edge id must be"):
        load_layers([high, bad_eid])

    repo_clean = load_layers([gc.SHARED_ROOT, _write(tmp_path / "clean", REPO)])
    assert repo_clean.shadowed == ()

    clash = _write(tmp_path / "clash", Graph([node("重复名", "person", aliases=("嗦面",))], []))
    with pytest.raises(GraphError, match="several accepted nodes"):
        load_layers([gc.SHARED_ROOT, clash])


def test_graph_context_load_shadows_overlap_reports_detail_and_keeps_candidates_unverified(repo, tmp_path, monkeypatch):
    from src.autoslice.manual_lane import discover_aids

    runtime = Graph([
        node("嗦面", "term", summary="低层假释义", aliases=("低层假别名",), status="candidate", sources=SEARCH),
        EVENT,
    ], [
        edge(MEMBER, "participates_in", EVENT, status="candidate"),
        Edge("term:嗦面|related_to|event:丁大会", "related_to", "term:嗦面", EVENT.id, "candidate", SEARCH),
    ])
    ctx = _ctx(tmp_path, runtime=runtime)
    loaded = gc.load(ctx)
    assert loaded.status == gc.COMBINED
    assert loaded.shadowed == ("term:嗦面",)
    assert "term:嗦面" in loaded.reason and "运行时候选图损坏" not in loaded.reason
    assert loaded.graph is not None
    assert loaded.graph.nodes["term:嗦面"].status == "accepted"
    assert "低层假别名" not in loaded.graph.nodes["term:嗦面"].aliases
    assert loaded.graph.nodes[EVENT.id].status == "candidate"

    context = gc.read(ctx, ["乙乙来了"], chat=["长嗦"])
    accepted, clues = context.block().split("\n\n")
    assert "嗦面" in accepted and "低层假释义" not in accepted
    assert "丁大会" in clues and "丁大会" not in accepted
    record = context.record()
    assert record["status"] == gc.COMBINED
    assert record["shadowed"] == ["term:嗦面"]
    assert "term:嗦面" in record["reason"]
    line = gc.process_line(record)
    assert line.startswith("知识图：仓库图加运行时候选") and "term:嗦面" in line

    cand_only = _write(tmp_path / "cand_only", Graph([node("候选人", "person", status="candidate", sources=SEARCH)], []))
    monkeypatch.setattr(discover_aids, "load_channel_profile", lambda _root: SimpleNamespace(asset_root=cand_only))
    assert discover_aids.reading_aids("候选人今天来了", ["候选人"]) is None


def test_real_witness_joins_repository_layers_and_exposes_runtime_nodes():
    witness = Path(os.environ.get("KNOWLEDGE_RUNTIME_WITNESS") or "/nonexistent-knowledge-runtime-witness")
    if not (witness / "knowledge/nodes.jsonl").is_file() or not (witness / "knowledge/edges.jsonl").is_file():
        pytest.skip(f"witness files missing: {witness}")
    repo_only = load_layers([gc.SHARED_ROOT, gc.REPO_ROOT / "assets/lidousha"])
    combined = load_layers([gc.SHARED_ROOT, gc.REPO_ROOT / "assets/lidousha", witness])
    assert combined.shadowed_nodes == ("term:嗦面",)
    assert combined.shadowed_edges == ()
    added_nodes = set(combined.nodes) - set(repo_only.nodes)
    assert len(added_nodes) == 516
    assert len(combined.nodes) == len(repo_only.nodes) + 516      # the repository graph grows; the runtime layer adds 516
    assert len(combined.edges) - len(repo_only.edges) == 479
    assert combined.nodes["term:嗦面"] == repo_only.nodes["term:嗦面"]
    assert combined.nodes["term:嗦面"].status == "accepted"
    assert all(combined.nodes[nid].status == "candidate" for nid in added_nodes)
    assert combined.neighbors("term:嗦面") == []
    cand_neighbors = combined.neighbors("term:嗦面", status=("accepted", "candidate"))
    assert len(cand_neighbors) == 2
    assert all(e.status == "candidate" and o.status == "candidate" for e, o in cand_neighbors)


# ----- candidate isolation across readers (round 2) --------------------------------------------------------------
def test_candidate_alias_never_merges_identities_or_covers_and_only_yields_clue():
    nan = node("南町", summary="主播南町")
    hua = node("花礼", summary="主播花礼")
    dwarf = node("矮人联盟", "group", summary="由花礼发起的团体")
    shu = node("鼠姐", summary="花礼的昵称", status="candidate", sources=SEARCH)
    graph = Graph(
        [nan, hua, dwarf, shu],
        [
            edge(nan, "member_of", dwarf),
            edge(hua, "founded", dwarf),
            Edge(f"{shu.id}|alias_of|{nan.id}", "alias_of", shu.id, nan.id, "candidate", SEARCH),
            Edge(f"{shu.id}|alias_of|{hua.id}", "alias_of", shu.id, hua.id, "candidate", SEARCH),
        ],
    )
    groups = [set(g) for g in graph.alias_groups()]
    assert {nan.id} in groups and {hua.id} in groups and {shu.id} in groups

    bg = gc.background(graph, gc.mentions(graph, ["鼠姐"]))
    assert [line for line in bg.lines if line.verified] == []
    assert not bg.covers("鼠姐")
    assert gc.VERIFIED_HEAD not in bg.block() and gc.CLUES_HEAD in bg.block()
    assert any("鼠姐可能是南町的别名（未核实）" in line.text for line in bg.lines)
    assert any("鼠姐可能是花礼的别名（未核实）" in line.text for line in bg.lines)
    assert gc.GraphContext(gc.COMBINED, "", graph).same_initials("sj") == []

    xiao = node("小南", summary="南町别名")
    accepted_graph = Graph(
        [nan, hua, dwarf, shu, xiao],
        [
            edge(nan, "member_of", dwarf),
            edge(hua, "founded", dwarf),
            edge(xiao, "alias_of", nan),
            Edge(f"{shu.id}|alias_of|{nan.id}", "alias_of", shu.id, nan.id, "candidate", SEARCH),
        ],
    )
    acc_bg = gc.background(accepted_graph, gc.mentions(accepted_graph, ["小南"]))
    assert acc_bg.covers("小南")
    assert any(line.verified and "南町隶属于矮人联盟" in line.text for line in acc_bg.lines)


def test_streamers_and_named_not_present_ignore_candidate_persons_and_candidate_aliases(monkeypatch):
    from src.autoslice.manual_lane import collab_guests, cover_guests

    host = node("李豆沙", aliases=("豆沙",))
    sumi = node("礼墨Sumi", aliases=("礼墨",), official="礼墨Sumi")
    xiaoli = node("小礼")
    panda = node("熊猫", status="candidate", sources=SEARCH)
    shu = node("鼠姐", status="candidate", sources=SEARCH)
    stranger = node("野人主播", status="candidate", sources=SEARCH)
    graph = Graph(
        [host, sumi, xiaoli, panda, shu, stranger],
        [
            edge(xiaoli, "alias_of", sumi),
            Edge(f"{panda.id}|alias_of|{host.id}", "alias_of", panda.id, host.id, "candidate", SEARCH),
            Edge(f"{shu.id}|alias_of|{sumi.id}", "alias_of", shu.id, sumi.id, "candidate", SEARCH),
        ],
    )
    monkeypatch.setattr(gc, "load", lambda _ctx: SimpleNamespace(graph=graph))
    got = collab_guests.streamers(None)
    assert "熊猫" not in got and "鼠姐" not in got and "野人主播" not in got
    assert "礼墨Sumi" in got and "小礼" in got["礼墨Sumi"] and "鼠姐" not in got["礼墨Sumi"]
    assert cover_guests.named_not_present(None, ["因为暗影大熊猫，我是隐藏姿态"], ["李豆沙"]) == []
    assert cover_guests.named_not_present(None, ["刚才鼠姐和野人主播说话了"], ["李豆沙"]) == []
    assert cover_guests.named_not_present(None, ["刚才小礼说话了"], ["李豆沙"]) == ["礼墨Sumi"]


# ----- candidate clue caps never displace accepted background (round 3) ------------------------------------------
def test_candidates_never_displace_accepted_lines_or_change_covers_for_any_mentions():
    import datetime as dt

    fan_nodes, fan_edges, hubs = _fan(5, 5)
    shared = node("共企", "project", summary="两个实体共有")
    fan_nodes.append(shared)
    fan_edges += [edge(hubs[0], "participates_in", shared), edge(hubs[1], "participates_in", shared)]

    seki = node("星甲", summary="", aliases=("小星",))
    psp = node("星团", "group", summary="")
    repo_graph = Graph([*fan_nodes, seki, psp], [*fan_edges, edge(seki, "member_of", psp)])

    newer_cand_sources = (Source("crawler", CRAWL, "2026-11-27T00:00:00Z"),)
    cand_events = [
        Node(f"event:候活{i}", "event", {"display": f"候活{i}"}, (), f"候选活动{i}", "candidate",
             newer_cand_sources, {"from": "2026-11-20", "until": None})
        for i in range(8)
    ]
    cand_aliases = [Node(f"person:候别{i}", "person", {"display": f"候别{i}"}, (), "", "candidate", newer_cand_sources)
                    for i in range(6)]
    solo_cand = Node("term:纯候选", "term", {"display": "纯候选"}, (), "纯候选词释义", "candidate", newer_cand_sources)
    cand_edges = [
        *(Edge(f"{seki.id}|participates_in|{ev.id}", "participates_in", seki.id, ev.id, "candidate",
               newer_cand_sources, {"from": "2026-11-20", "until": None}) for ev in cand_events),
        *(Edge(f"{ca.id}|alias_of|{seki.id}", "alias_of", ca.id, seki.id, "candidate", newer_cand_sources)
          for ca in cand_aliases),
        *(Edge(f"{h.id}|participates_in|{cand_events[0].id}", "participates_in", h.id, cand_events[0].id,
               "candidate", newer_cand_sources) for h in hubs[:3]),
    ]
    combined_graph = Graph([*repo_graph.nodes.values(), *cand_events, *cand_aliases, solo_cand],
                           [*repo_graph.edges, *cand_edges])

    day = dt.date(2026, 11, 23)
    mention_sets = [
        (["小星"], ("小星", "星甲")),
        (["小星", "枢0号", "枢1号"], ("小星", "星甲", "枢0号", "枢1号")),
        (["小星", "枢0号", "枢1号", "枢2号", "枢3号", "枢4号"], ("小星", "星甲", "枢0号", "枢1号", "枢2号", "枢3号", "枢4号")),
        (["纯候选"], ("小星", "纯候选")),
    ]
    for texts, probe_terms in mention_sets:
        repo_bg = gc.background(repo_graph, gc.mentions(repo_graph, texts), day)
        comb_bg = gc.background(combined_graph, gc.mentions(combined_graph, texts), day)
        assert [line for line in comb_bg.lines if line.verified] == [line for line in repo_bg.lines if line.verified]
        for term in probe_terms:
            assert comb_bg.covers(term) == repo_bg.covers(term)
        assert sum(not line.verified for line in comb_bg.lines) <= gc.CLUES_PER_CLIP

    single_bg = gc.background(combined_graph, gc.mentions(combined_graph, ["小星"]), day)
    assert 0 < sum(not line.verified and not line.own for line in single_bg.lines) <= gc.CLUES_PER_ENTITY


def test_real_witness_seki_accepted_background_and_covers_unchanged():
    import datetime as dt

    witness = Path(os.environ.get("KNOWLEDGE_RUNTIME_WITNESS") or "/nonexistent-knowledge-runtime-witness")
    if not (witness / "knowledge/nodes.jsonl").is_file() or not (witness / "knowledge/edges.jsonl").is_file():
        pytest.skip(f"witness files missing: {witness}")
    repo = load_layers([gc.SHARED_ROOT, gc.REPO_ROOT / "assets/lidousha"])
    combined = load_layers([gc.SHARED_ROOT, gc.REPO_ROOT / "assets/lidousha", witness])
    text = (gc.REPO_ROOT / "tests/fixtures/subtitle_audio_anchor_ambiguity/01-kiss.srt").read_text(encoding="utf-8").splitlines()[6]
    day = dt.date(2026, 11, 23)

    repo_bg = gc.background(repo, gc.mentions(repo, [text]), day)
    comb_bg = gc.background(combined, gc.mentions(combined, [text]), day)
    assert [line for line in comb_bg.lines if line.verified] == [line for line in repo_bg.lines if line.verified]
    assert repo_bg.covers("星汐") is True and comb_bg.covers("星汐") is True
    assert any(line.verified and "星汐隶属于PSPLive" in line.text for line in comb_bg.lines)
