"""News/event crawler: seeds from existing data, CPA-judged findings as candidate graph, failures reported.

No network, no real CPA: the search client and the CPA callable are fakes (the suite blocks the real CPA channel).
"""
from __future__ import annotations

import datetime as dt
import importlib.util
import io
import json
import subprocess
import sys
import urllib.error
from pathlib import Path

import pytest

from src.autoslice import news_event_crawler as crawler
from src.autoslice import news_event_graph as store
from src.autoslice import news_event_judge as judge
from src.autoslice.knowledge_graph import (
    Edge, Graph, GraphError, Node, Source, dump_graph, load_graph)
from src.autoslice.llm_client import LlmCallError
from src.autoslice.news_event_seeds import derive_seeds, dynamics_texts

ROOT = Path(__file__).resolve().parents[1]
NOW = dt.datetime(2026, 11, 10, 21, 0, tzinfo=dt.timezone.utc)
STAMP = "2026-11-10T21:00:00Z"
OWNER = "李豆沙"
LEDGER = (Source("ledger", "test", None),)


def _node(kind, name, aliases=(), status="accepted"):
    return Node(f"{kind}:{name}", kind, {"display": name}, tuple(aliases), "", status, LEDGER)


def _edge(a, kind, b):
    return Edge(f"{a}|{kind}|{b}", kind, a, b, "accepted", LEDGER)


@pytest.fixture
def repo(tmp_path):
    """A small accepted graph: the owner (with an alias node), two people, a project and a group."""
    nodes = [_node("person", OWNER, ["小李"]), _node("person", "豆沙"), _node("person", "安晚"),
             _node("person", "礼墨"), _node("project", "女大夜谈"), _node("group", "矮人联盟"),
             _node("term", "kmx")]
    edges = [_edge("person:豆沙", "alias_of", f"person:{OWNER}"),
             _edge("person:安晚", "participates_in", "project:女大夜谈")]
    root = tmp_path / "repo"
    dump_graph(Graph(nodes, edges), root)
    return root


def _registry(*members):
    return {"members": [{"canonical": c, "official_surfaces": [c], "aliases": [], "affiliations": a}
                        for c, a in members]}


def _dynamics(*texts):
    return {"items": [{"dynamic_id": str(i), "published_at": STAMP, "text": t} for i, t in enumerate(texts)]}


# ---- seeds ---------------------------------------------------------------------------------------------------------

def test_seeds_come_from_owner_dynamics_graph_and_rotating_roster(repo):
    graph = load_graph(repo)
    registry = _registry((OWNER, ["psplive"]), ("甲", ["psplive"]), ("乙", ["psplive"]), ("丙", ["psplive"]),
                         ("丁", ["virtuareal"]))
    dynamics = _dynamics("今晚和安晚[安晚_爱心]一起，小李也来，礼墨[礼墨_x]不在", "豆沙豆沙")
    seeds = derive_seeds(owner=OWNER, graph=graph, registry=registry, dynamics=dynamics,
                         extracted=["塔露拉航天"], day=0, limit=8)
    by_name = {s.name: s.origin for s in seeds}
    assert seeds[0].name == OWNER and seeds[0].origin == "owner"
    assert by_name["安晚"] == "dynamics" and by_name["塔露拉航天"] == "dynamics"
    assert by_name["女大夜谈"] == "graph" and by_name["矮人联盟"] == "graph"
    assert "豆沙" not in by_name and "小李" not in by_name, "the owner's own names are not seeds"
    assert "kmx" not in by_name, "a term node is not an event source"
    assert "丁" not in by_name, "a roster member of another affiliation is not related to the owner"
    assert len({s.name for s in seeds}) == len(seeds)
    # the emote tag [礼墨_x] is not text: 礼墨 is mentioned only in the prose, which does name her
    assert by_name["礼墨"] == "dynamics"
    assert "礼墨" not in dynamics_texts(_dynamics("好[礼墨_x]"))[0]


def test_roster_window_rotates_with_the_day_and_limit_is_hard(repo):
    graph = load_graph(repo)
    members = [(OWNER, ["psplive"])] + [(f"成员{i}", ["psplive"]) for i in range(6)]
    registry = _registry(*members)
    picked = []
    for day in range(3):
        seeds = derive_seeds(owner=OWNER, graph=graph, registry=registry, dynamics=None, day=day, limit=6)
        assert len(seeds) == 6
        picked.append({s.name for s in seeds if s.origin == "roster"})
    assert len(picked[0] | picked[1] | picked[2]) > len(picked[0])
    assert all(len(p) == 3 for p in picked)  # owner + 2 graph seeds leave 3 of the 6 slots to the roster


def test_missing_inputs_still_give_the_owner_and_graph_seeds(repo):
    seeds = derive_seeds(owner=OWNER, graph=load_graph(repo), registry=None, dynamics=None, day=1, limit=10)
    assert [s.origin for s in seeds] == ["owner", "graph", "graph"]


# ---- judge contract -------------------------------------------------------------------------------------------------

ROWS = [("安晚", [{"title": "t1", "url": "https://x.test/1", "snippet": "s"},
                  {"title": "t2", "url": "https://x.test/2", "snippet": "s"}])]


def _answer(*items):
    return json.dumps({"items": list(items)}, ensure_ascii=False)


def _item(url="https://x.test/1", **fields):
    base = {"url": url, "kind": "event", "name": "新企划", "participants": [
        {"name": "东爱璃", "kind": "person", "relation": "participates_in"}]}
    return base | fields


def test_judgement_keeps_contract_items_and_counts_the_rest():
    raw = _answer(_item(), _item(url="https://made.up/"), _item(kind="rumour"),
                  _item(participants=[{"name": "x", "kind": "person", "relation": "loves"}]),
                  _item(url="https://x.test/2", valid_from="2026-11-01"), _item(url="https://x.test/2",
                                                                                  valid_from="soon"))
    found, rejected = judge.parse_judgement(raw, ROWS)
    assert [f.url for f in found] == ["https://x.test/1", "https://x.test/2"]
    assert found[1].valid_from == "2026-11-01" and found[0].seed == "安晚"
    assert sum(rejected.values()) == 4
    assert "url is not one of the search results" in rejected


@pytest.mark.parametrize("raw", ["not json at all", '{"items": "nope"}', "[]"])
def test_a_malformed_envelope_raises(raw):
    with pytest.raises((judge.JudgeError, LlmCallError)):
        judge.parse_judgement(raw, ROWS)


def test_mentions_must_occur_in_the_text():
    texts = ["今晚和安晚玩塔露拉航天"]
    got = judge.parse_mentions(json.dumps({"mentions": ["安晚", "塔露拉航天", "编造的人", "安晚"]},
                                          ensure_ascii=False), texts)
    assert got == ["安晚", "塔露拉航天"]


# ---- candidate graph ------------------------------------------------------------------------------------------------

def _cands(findings, graph, *, now):
    return store.candidates_from(findings, graph, {e.id for e in graph.edges}, now=now)


def _finding(**fields):
    found, _ = judge.parse_judgement(_answer(_item(**fields)), ROWS)
    return found[0]


def test_known_nodes_are_referenced_and_new_ones_become_candidates(repo):
    graph = load_graph(repo)
    finding = _finding(kind="project", id="project:女大夜谈", name="女大夜话", participants=[
        {"name": "安晚", "kind": "person", "relation": "participates_in"},   # edge already accepted
        {"name": "礼墨", "kind": "person", "id": "person:礼墨", "relation": "participates_in"},
        {"name": "东爱璃", "kind": "person", "relation": "participates_in"}])
    (nodes, edges), left_out = _cands([finding], graph, now=STAMP)
    assert left_out == 0
    assert set(nodes) == {"person:东爱璃"}, "the project and the people the graph knows are not copied"
    assert set(edges) == {"person:礼墨|participates_in|project:女大夜谈", "person:东爱璃|participates_in|project:女大夜谈"}
    for item in (*nodes.values(), *edges.values()):
        assert item.status == "candidate"
        assert item.sources == (Source("search", "https://x.test/1", STAMP),)


def test_new_event_with_a_known_and_a_new_participant(repo):
    graph = load_graph(repo)
    finding = _finding(name="女高夜谈", aliases=["女高夜话"], summary="三人新企划", valid_from="2026-11-01",
                       participants=[{"name": "安晚", "kind": "person", "relation": "founded"},
                                     {"name": "新人甲", "kind": "person", "relation": "participates_in"}])
    (nodes, edges), _ = _cands([finding], graph, now=STAMP)
    assert set(nodes) == {"event:女高夜谈", "person:新人甲"}
    assert nodes["event:女高夜谈"].aliases == ("女高夜话",)
    assert nodes["event:女高夜谈"].valid == {"from": "2026-11-01", "until": None}
    assert set(edges) == {"person:安晚|founded|event:女高夜谈", "person:新人甲|participates_in|event:女高夜谈"}


def test_output_validates_with_the_repository_graph_but_not_alone(tmp_path, repo):
    graph = load_graph(repo)
    finding = _finding(kind="project", id="project:女大夜谈", name="女大夜谈",
                       participants=[{"name": "东爱璃", "kind": "person", "relation": "participates_in"},
                                     {"name": "礼墨", "kind": "person", "relation": "founded"}])
    candidates, _ = _cands([finding], graph, now=STAMP)
    runtime = tmp_path / "runtime"
    combined = store.write_runtime_graph(repo, runtime, candidates, {"ok": True})
    assert "person:礼墨|founded|project:女大夜谈" in {e.id for e in combined.edges}
    assert store.load_combined(repo, runtime).nodes["person:东爱璃"].status == "candidate"
    assert json.loads((runtime / store.REPORT_FILE).read_text()) == {"ok": True}
    with pytest.raises(GraphError):  # an edge points at an accepted node that lives in the repository graph
        load_graph(runtime)


def test_a_failed_validation_leaves_the_previous_output_untouched(tmp_path, repo, monkeypatch):
    graph = load_graph(repo)
    first, _ = _cands([_finding(name="旧企划")], graph, now=STAMP)
    runtime = tmp_path / "runtime"
    store.write_runtime_graph(repo, runtime, first, {"run": 1})
    before = (runtime / "knowledge/nodes.jsonl").read_bytes()

    def broken(*_args, **_kwargs):
        raise GraphError("bad")

    monkeypatch.setattr(store, "load_combined", broken)
    second, _ = _cands([_finding(name="新企划")], graph, now=STAMP)
    with pytest.raises(GraphError):
        store.write_runtime_graph(repo, runtime, second, {"run": 2})
    assert (runtime / "knowledge/nodes.jsonl").read_bytes() == before
    assert sorted(p.name for p in tmp_path.iterdir()) == ["repo", "runtime"], "no staging directory is left behind"


def test_accumulation_merges_sources_and_drops_stale_items(repo):
    graph = load_graph(repo)
    old_stamp = "2026-05-01T00:00:00Z"
    stale, _ = _cands([_finding(name="过期企划")], graph, now=old_stamp)
    kept, _ = _cands([_finding(name="常青企划")], graph, now=old_stamp)
    again, _ = _cands([_finding(name="常青企划", url="https://x.test/2")], graph, now=STAMP)
    merged = store.accumulate((stale[0] | kept[0], stale[1] | kept[1]), again, repo=graph, now=STAMP)
    assert "event:过期企划" not in merged[0] and "event:常青企划" in merged[0]
    assert {s.ref for s in merged[0]["event:常青企划"].sources} == {"https://x.test/1", "https://x.test/2"}
    assert all("过期企划" not in key for key in merged[1]), "edges of a dropped node go with it"
    assert any("常青企划" in key for key in merged[1])


# ---- the run --------------------------------------------------------------------------------------------------------

class FakeLookup:
    def __init__(self, rows=None, fail=()):
        self.queries, self.rows, self.fail = [], rows or {}, set(fail)

    def web(self, query, limit=5):
        self.queries.append(query)
        if any(word in query for word in self.fail):
            raise RuntimeError("search router down")
        for word, rows in self.rows.items():
            if word in query:
                return rows
        return []


def _search(lookup, **kw):
    return crawler.RouterSearch(lookup, deadline=10**9, sleep=lambda s: None, **kw)


def _cpa(answer=None, mentions=(), error=None):
    calls = []

    def call(prompt):
        calls.append(prompt)
        if error:
            raise error
        if "只输出 JSON：{\"mentions\"" in prompt:
            return json.dumps({"mentions": list(mentions)}, ensure_ascii=False)
        return answer(prompt) if callable(answer) else answer or _answer()

    call.calls = calls
    return call


def _run(tmp_path, repo, lookup, llm, **kw):
    kwargs = dict(owner=OWNER, repo_root=repo, runtime_root=tmp_path / "runtime",
                  registry=kw.pop("registry", _registry((OWNER, ["psplive"]), ("甲", ["psplive"]))),
                  dynamics=kw.pop("dynamics", _dynamics("和安晚一起玩塔露拉航天")), search=_search(lookup),
                  llm_call=llm, now=NOW, max_seeds=10)
    return crawler.crawl(**(kwargs | kw))


RESULT = {"title": "女高夜谈开播", "url": "https://x.test/1", "snippet": "李豆沙、安晚、东爱璃的新夜谈企划"}


def test_a_run_turns_judged_results_into_a_validated_candidate_graph(tmp_path, repo):
    lookup = FakeLookup({"塔露拉航天": [RESULT]})
    llm = _cpa(_answer(_item(url=RESULT["url"], kind="project", name="女高夜谈", participants=[
        {"name": "安晚", "kind": "person", "relation": "founded"},
        {"name": "东爱璃", "kind": "person", "relation": "participates_in"}])), mentions=["塔露拉航天", "编造"])
    report = _run(tmp_path, repo, lookup, llm)
    assert any("塔露拉航天" in q for q in lookup.queries) and not any("编造" in q for q in lookup.queries)
    assert report.search["ok"] == report.search["attempted"] and report.cpa["judge_ok"] == 1
    assert report.new_nodes == ["person:东爱璃", "project:女高夜谈"]
    combined = store.load_combined(repo, tmp_path / "runtime")
    assert {n.id for n in combined.nodes.values() if n.status == "candidate"} == {"person:东爱璃", "project:女高夜谈"}
    assert all(s.kind == "search" and s.at == STAMP for n in combined.nodes.values() if n.status == "candidate"
               for s in n.sources)
    assert "安晚" in llm.calls[-1] and "person:安晚" in llm.calls[-1], "CPA is told the known nodes by id"
    # a second day with no new hit keeps what was found
    again = _run(tmp_path, repo, FakeLookup(), _cpa(), now=NOW + dt.timedelta(days=1))
    assert again.candidates == report.candidates


def test_partial_failure_is_counted_and_the_run_still_delivers(tmp_path, repo):
    lookup = FakeLookup({"塔露拉航天": [RESULT]}, fail=("甲",))
    llm = _cpa(_answer(_item(url=RESULT["url"], name="女高夜谈")), mentions=["塔露拉航天"])
    report = _run(tmp_path, repo, lookup, llm)
    assert report.search["failed"] == 1 and report.search["ok"] >= 1
    assert any("search '甲'" in e for e in report.errors)
    assert (tmp_path / "runtime/knowledge/nodes.jsonl").is_file()


def test_no_search_succeeding_is_a_failure_and_writes_nothing(tmp_path, repo):
    with pytest.raises(crawler.NewsEventError) as caught:
        _run(tmp_path, repo, FakeLookup(fail=(OWNER, "甲", "安晚", "塔露拉航天", "女大夜谈", "矮人联盟")), _cpa())
    assert caught.value.code == "sources_unavailable"
    assert not (tmp_path / "runtime").exists()


def test_cpa_answering_nothing_is_a_failure(tmp_path, repo):
    lookup = FakeLookup({OWNER: [RESULT]})
    with pytest.raises(crawler.NewsEventError) as caught:
        _run(tmp_path, repo, lookup, _cpa(error=LlmCallError("budget exhausted")))
    assert caught.value.code == "cpa_unavailable"
    assert caught.value.report.cpa["judge_failed"] >= 1 and not (tmp_path / "runtime").exists()


def test_unreadable_previous_graph_is_discarded_loudly(tmp_path, repo):
    broken = tmp_path / "runtime/knowledge"
    broken.mkdir(parents=True)
    (broken / "nodes.jsonl").write_text("{not json\n")
    (broken / "edges.jsonl").write_text("")
    report = _run(tmp_path, repo, FakeLookup({OWNER: [RESULT]}), _cpa())
    assert report.previous_discarded
    assert load_graph(repo) and store.load_combined(repo, tmp_path / "runtime")


# ---- rate limits ----------------------------------------------------------------------------------------------------

class Clock:
    def __init__(self):
        self.now, self.slept = 0.0, []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds


def _http_429(retry_after=None):
    headers = {"Retry-After": retry_after} if retry_after else {}
    return urllib.error.HTTPError("http://router", 429, "Too Many Requests", headers, io.BytesIO(b""))


class Flaky:
    def __init__(self, errors):
        self.errors, self.calls = list(errors), 0

    def web(self, query, limit=5):
        self.calls += 1
        if self.errors:
            raise self.errors.pop(0)
        return [{"url": "https://x.test/1", "title": "t", "snippet": "s"}]


def test_a_429_is_waited_out_with_retry_after_and_the_gap_adapts():
    clock = Clock()
    flaky = Flaky([_http_429("30"), _http_429()])
    search = crawler.RouterSearch(flaky, deadline=1000, clock=clock, sleep=clock.sleep, jitter=lambda: 0.0)
    assert search.search("q") and flaky.calls == 3 and search.waits == 2
    assert clock.slept[0] == 30.0 and clock.slept[1] == 4.0  # Retry-After first, then 2**2 backoff
    assert search.interval == pytest.approx(3.2)  # 1 -> 2 -> 4 after two 429s, then x0.8 on success
    search.search("q2")  # the next search keeps the pace gap
    assert clock.slept[-1] > 0


def test_a_router_error_message_with_429_also_counts_as_rate_limited():
    clock = Clock()
    flaky = Flaky([RuntimeError("MCP error: upstream 429 too many requests")])
    search = crawler.RouterSearch(flaky, deadline=1000, clock=clock, sleep=clock.sleep, jitter=lambda: 0.0)
    assert search.search("q") and search.waits == 1


def test_rate_limit_gives_up_after_the_attempt_cap_and_other_errors_are_not_retried():
    clock = Clock()
    flaky = Flaky([_http_429() for _ in range(crawler.RATE_LIMIT_ATTEMPTS)])
    search = crawler.RouterSearch(flaky, deadline=10**6, clock=clock, sleep=clock.sleep, jitter=lambda: 0.0)
    with pytest.raises(urllib.error.HTTPError):
        search.search("q")
    assert flaky.calls == crawler.RATE_LIMIT_ATTEMPTS
    other = Flaky([RuntimeError("boom")])
    with pytest.raises(RuntimeError):
        crawler.RouterSearch(other, deadline=1000, clock=clock, sleep=clock.sleep).search("q")
    assert other.calls == 1


def test_the_wait_never_overruns_the_runs_deadline():
    clock = Clock()
    flaky = Flaky([_http_429("500")])
    search = crawler.RouterSearch(flaky, deadline=60, clock=clock, sleep=clock.sleep, jitter=lambda: 0.0)
    with pytest.raises(crawler.NewsEventError) as caught:
        search.search("q")
    assert caught.value.code == "runtime_budget_exhausted" and clock.slept == []


# ---- entry point ----------------------------------------------------------------------------------------------------

def _script():
    spec = importlib.util.spec_from_file_location("crawl_news_events", ROOT / "scripts/crawl_news_events.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_entry_point_help_parses():
    done = subprocess.run([sys.executable, str(ROOT / "scripts/crawl_news_events.py"), "--help"], cwd=ROOT,
                          capture_output=True, text=True, check=False)
    assert done.returncode == 0, done.stderr


def _snapshots(tmp_path):
    registry, dynamics = tmp_path / "registry.json", tmp_path / "dynamics.json"
    members = [{"entity_id": f"psplive:{i}", "canonical": name, "official_surfaces": [name], "aliases": [],
                "official_mid": None, "affiliations": ["psplive"],
                "official_sources": [{"kind": "k", "url": "https://x.test/"}]}
               for i, name in enumerate([OWNER] + [f"成员{n}" for n in range(25)])]
    registry.write_text(json.dumps({
        "schema_version": "vtuber-slice.streamer-registry.v1", "status": "fresh", "members": members,
        "generated_at": STAMP, "expires_at": "2027-01-01T00:00:00+00:00", "sources": [],
        "occurrence_policy": "ENTITY_EXISTENCE_ONLY_NOT_CUE_OCCURRENCE_EVIDENCE", "authority": "x"}))
    dynamics.write_text("{}")  # invalid on purpose: one bad input is a note, not the end of the run
    return registry, dynamics


def test_entry_point_exit_codes(tmp_path, monkeypatch, capsys):
    script = _script()
    registry, dynamics = _snapshots(tmp_path)
    runtime = tmp_path / "graph"
    lookup = FakeLookup({OWNER: [RESULT]})
    llm = _cpa(_answer(_item(url=RESULT["url"], kind="group", name="新联盟")))
    monkeypatch.setattr(script, "WebLookup", lambda: lookup)
    monkeypatch.setattr(script, "build_llm_call", lambda config: llm)
    argv = ["--registry", str(registry), "--dynamics", str(dynamics), "--write-root", str(runtime),
            "--max-seeds", "6", "--now", STAMP]
    assert script.main(argv) == 0
    summary = capsys.readouterr().err
    assert "dynamics unavailable" in json.loads((runtime / store.REPORT_FILE).read_text())["errors"][0]
    assert '"new_nodes": 1' in summary
    candidates = store.load_combined(script.PROFILE.asset_root, runtime)  # the real repository graph + the output
    assert candidates.nodes["group:新联盟"].status == "candidate"

    monkeypatch.setattr(script, "WebLookup", lambda: FakeLookup(fail=("",)))
    assert script.main(argv) == 2
    assert "sources_unavailable" in capsys.readouterr().err
    monkeypatch.setattr(script, "WebLookup", lambda: lookup)
    monkeypatch.setattr(script, "build_llm_call", lambda config: _cpa(error=LlmCallError("CPA quota exhausted")))
    assert script.main(argv) == 2
    assert "cpa_unavailable" in capsys.readouterr().err


# ---- one thing, one node ---------------------------------------------------------------------------------------------

def test_a_later_spelling_of_a_found_candidate_joins_it_instead_of_adding_a_twin(tmp_path, repo):
    graph = load_graph(repo)
    first, _ = _cands([_finding(name="女高夜谈", summary="旧")], graph, now="2026-11-01T00:00:00Z")
    known = store.with_candidates(graph, first)
    variants = [_finding(name="女高夜话", id="event:女高夜谈", url="https://x.test/2", summary="新"),
                _finding(name="女高夜谈", url="https://x.test/2")]  # same spelling, no id: found by name
    (nodes, _), left_out = _cands(variants, known, now=STAMP)
    assert left_out == 0 and set(nodes) == {"event:女高夜谈", "person:东爱璃"}  # the participant is re-confirmed too
    merged = store.accumulate(first, (nodes, {}), repo=graph, now=STAMP)
    node = merged[0]["event:女高夜谈"]
    assert "女高夜话" in node.aliases and node.summary == "新" and node.names == {"display": "女高夜谈"}
    assert {s.ref for s in node.sources} == {"https://x.test/1", "https://x.test/2"}


def test_a_spelling_shared_by_an_accepted_node_and_a_candidate_resolves_to_the_accepted_one(repo):
    graph = load_graph(repo)
    twin = Node("event:女大夜谈", "event", {"display": "女大夜谈"}, (), "", "candidate", (Source("search", "u", STAMP),))
    known = store.with_candidates(graph, ({twin.id: twin}, {}))
    (nodes, edges), _ = _cands([_finding(kind="project", name="女大夜谈", participants=[
        {"name": "东爱璃", "kind": "person", "relation": "participates_in"}])], known, now=STAMP)
    assert set(nodes) == {"person:东爱璃"} and list(edges) == ["person:东爱璃|participates_in|project:女大夜谈"]


def test_later_batches_and_later_days_are_shown_what_was_already_found(tmp_path, repo, monkeypatch):
    monkeypatch.setattr(crawler, "SEEDS_PER_JUDGE_CALL", 1)
    lookup = FakeLookup({"": [RESULT]})
    prompts = []

    def answer(prompt):
        prompts.append(prompt)
        return _answer(_item(url=RESULT["url"], name="新企划")) if len(prompts) == 1 else _answer()

    _run(tmp_path, repo, lookup, _cpa(answer), dynamics=None)
    assert len(prompts) >= 2
    assert "event:新企划" not in prompts[0] and "event:新企划" in prompts[1]
    later = []
    _run(tmp_path, repo, lookup, _cpa(lambda p: later.append(p) or _answer()), dynamics=None,
         now=NOW + dt.timedelta(days=1))
    assert later and all("event:新企划" in p for p in later)


def test_a_seed_is_searched_with_the_owner_and_nothing_else():
    assert crawler._query("安晚", OWNER) == "安晚 李豆沙"
    assert OWNER in crawler._query(OWNER, OWNER) and "安晚" not in crawler._query(OWNER, OWNER)


def test_a_seed_no_result_mentions_is_searched_once_more():
    seen = []

    class Lookup:
        def web(self, query, limit=5):
            seen.append(query)
            hit = "联动 企划" in query
            return [{"url": "https://x.test/2" if hit else "https://x.test/1", "title": "安晚的夜谈" if hit else "无关",
                     "snippet": ""}]

    report = crawler.Report(STAMP, OWNER, {})
    rows = crawler._search_seed(_search(Lookup()), report, crawler.Seed("安晚", "graph"), OWNER)
    assert seen == ["安晚 李豆沙", "安晚 联动 企划"] and report.search["asked_again"] == 1
    assert [r["url"] for r in rows] == ["https://x.test/1", "https://x.test/2"]
    seen.clear()
    done = {"": [{"url": "u", "title": "安晚来了", "snippet": ""}]}
    assert crawler._search_seed(_search(FakeLookup(done)), report, crawler.Seed("安晚", "graph"), OWNER)
    assert report.search["asked_again"] == 1
