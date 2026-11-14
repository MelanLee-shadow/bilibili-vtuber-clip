"""Knowledge graph: format, validation, queries, and the proper-noun table projection guard."""
import dataclasses
import json
import re
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

import scripts.build_knowledge_graph as builder
from src.autoslice.knowledge_graph import GraphError, dump_graph, load_graph

ROOT = Path(__file__).resolve().parents[1]
PROFILE_ROOT = ROOT / "assets/lidousha"
TABLE = ROOT / "assets/manual_lane/proper_nouns.json"
SRC = [{"kind": "ledger", "ref": "fake.json", "at": None}]


def _node(kind, display, aliases=(), status="accepted", **names):
    return {"id": f"{kind}:{display}", "kind": kind, "names": {"display": display, **names},
            "aliases": list(aliases), "summary": "", "status": status, "sources": SRC}


def _edge(type_, src, dst, status="accepted"):
    return {"id": f"{src}|{type_}|{dst}", "type": type_, "from": src, "to": dst,
            "status": status, "sources": SRC}


def _write(root, nodes, edges):
    (root / "knowledge").mkdir(parents=True, exist_ok=True)
    for name, rows in (("nodes", nodes), ("edges", edges)):
        text = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in sorted(rows, key=lambda r: r["id"]))
        (root / f"knowledge/{name}.jsonl").write_text(text, encoding="utf-8")
    return root


def _small(tmp_path):
    nodes = [_node("person", "甲", ["Alpha Nick"], subtitle="甲字幕", official="甲Official"),
             _node("group", "丙团"), _node("person", "乙")]
    edges = [_edge("member_of", "person:甲", "group:丙团"), _edge("founded", "person:乙", "group:丙团")]
    return _write(tmp_path / "small", nodes, edges)


def _real():
    return load_graph(PROFILE_ROOT)


def _same_entity(graph, nodes):
    groups = {i for i, g in enumerate(graph.alias_groups()) for n in nodes if n.id in g}
    return len(groups) == 1


# ---- format and validation
def test_round_trip_is_byte_identical(tmp_path):
    for root in (_small(tmp_path), PROFILE_ROOT):
        out = tmp_path / "out"
        dump_graph(load_graph(root), out)
        for name in ("nodes", "edges"):
            assert (out / f"knowledge/{name}.jsonl").read_bytes() == (root / f"knowledge/{name}.jsonl").read_bytes()


def _mutate(tmp_path, change):
    rows = SimpleNamespace(nodes=[_node("person", "甲"), _node("person", "乙")],
                           edges=[_edge("related_to", "person:甲", "person:乙")])
    change(rows)
    return _write(tmp_path / "bad", rows.nodes, rows.edges)


def _drop_status(rows):
    del rows.nodes[0]["status"]


def _bad_kind(rows):
    rows.nodes[0]["kind"] = "robot"


def _bad_edge_type(rows):
    rows.edges[0]["type"] = "likes"


def _bad_source_kind(rows):
    rows.nodes[0]["sources"] = [{"kind": "rumor", "ref": "x", "at": None}]


def _dup_node(rows):
    rows.nodes.append(dict(rows.nodes[0]))


def _dup_edge(rows):
    rows.edges.append(dict(rows.edges[0]))


def _dangling(rows):
    rows.edges[0] = _edge("related_to", "person:甲", "person:丁")


def _same_spelling(rows):
    rows.nodes[1]["aliases"] = ["甲"]


@pytest.mark.parametrize("change,fragment", [
    (_drop_status, "missing keys"), (_bad_kind, "not in"), (_bad_edge_type, "not in"),
    (_bad_source_kind, "not in"), (_dup_node, "duplicate node ids"), (_dup_edge, "duplicate edge ids"),
    (_dangling, "is not a node"), (_same_spelling, "several accepted nodes")])
def test_validation_rejects(tmp_path, change, fragment):
    with pytest.raises(GraphError, match=fragment):
        load_graph(_mutate(tmp_path, change))


def test_shared_spelling_is_allowed_with_alias_of_or_without_accepted_status(tmp_path):
    def linked(rows):
        _same_spelling(rows)
        rows.edges.append(_edge("alias_of", "person:乙", "person:甲"))

    def candidate(rows):
        _same_spelling(rows)
        rows.nodes[1]["status"] = "candidate"

    for change in (linked, candidate):
        assert load_graph(_mutate(tmp_path / change.__name__, change))


# ---- queries
def test_find_neighbors_name_on_synthetic_graph(tmp_path):
    graph = load_graph(_small(tmp_path))
    assert [n.id for n in graph.find("甲")] == ["person:甲"]
    assert [n.id for n in graph.find("甲Official")] == ["person:甲"]
    assert [n.id for n in graph.find("alpha nick")] == ["person:甲"]  # pure ASCII ignores case
    assert graph.find("甲official") == []  # non-ASCII spelling is exact
    assert {(e.type, n.id) for e, n in graph.neighbors("group:丙团")} == {
        ("member_of", "person:甲"), ("founded", "person:乙")}
    assert [n.id for _, n in graph.neighbors("group:丙团", types=["founded"])] == ["person:乙"]
    assert graph.name("person:甲", "subtitle") == "甲字幕"
    assert graph.name("person:甲", "tag") == "甲"  # no tag: falls back to display
    assert graph.name("person:乙", "official") == "乙"
    with pytest.raises(ValueError):
        graph.name("person:甲", "nickname")


def test_neighbors_hide_non_accepted_by_default(tmp_path):
    root = _write(tmp_path / "g", [_node("person", "甲"), _node("person", "乙", status="candidate")],
                  [_edge("related_to", "person:甲", "person:乙")])
    graph = load_graph(root)
    assert graph.neighbors("person:甲") == []
    assert len(graph.neighbors("person:甲", status=("accepted", "candidate"))) == 1


# ---- real data
def test_find_on_repo_graph_reaches_one_entity_for_every_use():
    graph = _real()
    overrides = json.loads((ROOT / "assets/lidousha/psplive_roster_sources.v1.json").read_text("utf-8"))
    official, row = next((k, v) for k, v in overrides["member_overrides"].items()
                         if any(a.isascii() and a.isalpha() for a in v["aliases"]))
    ascii_alias = next(a for a in row["aliases"] if a.isascii() and a.isalpha())
    found = graph.find(row["canonical"])
    assert found
    for surface in (official, ascii_alias, ascii_alias.swapcase()):
        assert graph.find(surface), surface
        assert _same_entity(graph, graph.find(surface) + found), surface


def test_circle_groups_have_members_and_founder_from_persona_facts():
    graph = _real()
    facts = json.loads((ROOT / "assets/lidousha/persona_facts.v1.json").read_text("utf-8"))["revisions"]
    # the circle-background facts (other background facts, such as how she names herself, name no group)
    background = [f["statement_zh"] for f in facts
                  if f["claim"]["kind"] == "IDENTITY_BACKGROUND" and f["statement_zh"].startswith("圈内背景")][-4:]
    names = {re.search(r"“([^”]+)”", text)[1] for text in background}
    assert len(names) == 2
    for name in names:
        # facts that call the entity a 企划 make it a project whose members participate
        planned = any("企划" in text and f"“{name}”" in text for text in background)
        kind, link = ("project", "participates_in") if planned else ("group", "member_of")
        (node,) = graph.find(name)
        assert node.kind == kind
        members = graph.neighbors(node.id, types=[link])
        assert len(members) >= 2 and all(n.kind == "person" for _, n in members)
        assert not graph.neighbors(node.id, types=["member_of" if planned else "participates_in"])
    for text in (t for t in background if "发起" in t):
        node = graph.find(re.search(r"“([^”]+)”", text)[1])[0]
        ((_, founder),) = graph.neighbors(node.id, types=["founded"])
        assert founder.names["display"] in text


def test_every_roster_and_registry_name_resolves_to_one_entity():
    graph = _real()
    overrides = json.loads((ROOT / "assets/lidousha/psplive_roster_sources.v1.json").read_text("utf-8"))
    registry = json.loads((ROOT / "assets/lidousha/streamer_registry_sources.v1.json").read_text("utf-8"))
    names = set()
    for official, row in overrides["member_overrides"].items():
        names |= {official, row["canonical"]}
    for account in registry["reviewed_accounts"]:
        names |= {account["canonical"], account["account_name"]}
    for line in (ROOT / "assets/lidousha/psplive_roster.v1.md").read_text("utf-8").splitlines():
        cell = line.strip("| ").split("|")[0].strip() if line.startswith("|") else ""
        if cell and cell != "艺名" and not set(cell) <= {"-", " "}:
            names.add(re.split(r"[（(]", cell)[0].strip())
    assert len(names) > 20
    for name in sorted(names):
        found = graph.find(name)
        assert found, name
        assert _same_entity(graph, found), name


def test_only_accepted_nodes_and_edges_are_seeded():
    graph = _real()
    assert {n.status for n in graph.nodes.values()} == {"accepted"}
    assert {e.status for e in graph.edges} == {"accepted"}


# ---- proper-noun table projection
def test_repo_table_equals_graph_projection():
    assert builder.project_text(_real()) == TABLE.read_text("utf-8")
    assert builder.main(["project", "--check"]) == 0


def test_check_fails_when_graph_changes(tmp_path, monkeypatch, capsys):
    shutil.copytree(PROFILE_ROOT / "knowledge", tmp_path / "assets/lidousha/knowledge")
    (tmp_path / "assets/manual_lane").mkdir(parents=True)
    shutil.copy(TABLE, tmp_path / "assets/manual_lane/proper_nouns.json")
    monkeypatch.setattr(builder, "ROOT", tmp_path)
    monkeypatch.setattr(builder, "TABLE", tmp_path / "assets/manual_lane/proper_nouns.json")
    assert builder.main(["project", "--check"]) == 0
    graph = load_graph(tmp_path / "assets/lidousha")
    victim = next(n for n in graph.nodes.values() if n.subtitle and "subtitle" in n.names)
    changed = dataclasses.replace(victim, names={**victim.names, "subtitle": victim.names["subtitle"] + "改"})
    graph.nodes[victim.id] = changed
    dump_graph(graph, tmp_path / "assets/lidousha")
    assert builder.main(["project", "--check"]) == 1
    assert victim.names["display"] in capsys.readouterr().out


def test_seed_refuses_to_overwrite_existing_graph(capsys):
    assert builder.main(["seed"]) == 2
    assert "--force" in capsys.readouterr().err
