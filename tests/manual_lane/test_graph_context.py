"""Step 6 reads the knowledge graph before it goes to the web (graph_context, circle_context.gather).  The graphs here are
small made-up ones written to a temporary directory (invented names, no real clip's subtitles); no model, no network."""

from __future__ import annotations

import json
import random
from types import SimpleNamespace

import pytest
from fakes import MERGE, FakeCpa, FakeRemote, FakeSearch, make_ctx

from src.autoslice.knowledge_graph import Edge, Graph, Node, Source, dump_graph
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
    assert [t["term"] for t in found["terms"]] == ["没人知道的词"]
    assert search.queries == ["没人知道的词 李豆沙", "没人知道的词 虚拟主播"]
    assert found["graph"]["skipped_search"] == ["乙乙", "丁大会"]       # an accepted node and a candidate one


def test_a_reference_in_the_graph_but_without_background_in_the_prompt_is_still_searched(repo, tmp_path):
    ctx = _ctx(tmp_path)
    context = gc.read(ctx, ["今天没有提到任何实体"])
    search = FakeSearch(configured=True)
    found = _gather(ctx, context, "乙乙", search=search)
    assert [t["term"] for t in found["terms"]] == ["乙乙"] and len(search.calls) == 2
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
