#!/usr/bin/env python3
"""Build the channel knowledge graph from the existing reviewed files, and project the proper-noun
table back out of it.

  seed     one-time: proper-noun table + roster + registry + tag policy + profile + persona facts
           -> assets/<profile>/knowledge/{nodes,edges}.jsonl (refuses to overwrite without --force)
  project  graph -> assets/manual_lane/proper_nouns.json; --check only compares (exit 1 on drift)

Table rows live in node.subtitle: node.subtitle.order is the row position; node.subtitle.table_aliases
is the row's own alias list when it differs from node.aliases (the graph aliases are the full set).
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import tempfile
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from src.autoslice.knowledge_graph import (
    Edge, Graph, GraphError, JsonValue, Node, Source, dump_graph, load_graph)

TABLE = ROOT / "assets/manual_lane/proper_nouns.json"
# Role keywords -> node kind, first match wins; anything else is a plain term.
ROLE_KINDS = (("观众称呼", "community"), ("企划", "project"), ("作品", "work"), ("角色", "character"),
              ("游戏", "game"), ("团体", "group"), ("乐队", "group"), ("主播", "person"), ("嘉宾", "person"))
BEFORE_CANONICAL = ("rejected_variants", "legacy_equivalences", "variants")
FILES = {"roster": "assets/lidousha/psplive_roster.v1.md",
         "overrides": "assets/lidousha/psplive_roster_sources.v1.json",
         "registry": "assets/lidousha/streamer_registry_sources.v1.json",
         "tags": "assets/lidousha/upload_tag_policy.json", "profile": "profiles/lidousha/profile.json",
         "facts": "assets/lidousha/persona_facts.v1.json"}


def _key(text: str) -> str:
    return text.casefold()


def _src(kind: str, ref: str, at: str | None = None) -> Source:
    return Source(kind, ref, at)


def _read(rel: str) -> object:
    return json.loads((ROOT / rel).read_text("utf-8"))


class Draft:
    def __init__(self, kind: str, display: str, subtitle_name: str | None = None) -> None:
        self.kind, self.display, self.subtitle_name = kind, display, subtitle_name
        self.official: str | None = None
        self.tag: str | None = None
        self.aliases: list[str] = []
        self.alias_refs: dict[str, Source] = {}
        self.summary = ""
        self.sources: list[Source] = []
        self.subtitle: dict[str, JsonValue] | None = None

    @property
    def id(self) -> str:
        return f"{self.kind}:{self.display}"

    def add_alias(self, alias: str, source: Source) -> None:
        known = {_key(self.display), *(_key(a) for a in self.aliases)}
        if _key(alias) not in known:
            self.aliases.append(alias)
            self.alias_refs[_key(alias)] = source

    def add_source(self, source: Source) -> None:
        if source not in self.sources:
            self.sources.append(source)


class Seeder:
    def __init__(self) -> None:
        self.table = json.loads(TABLE.read_text("utf-8"))
        self.drafts: dict[str, Draft] = {}  # keyed by display
        self.by_key: dict[str, Draft] = {}
        self.persons: list[Draft] = []
        self.edges: dict[tuple[str, str, str], tuple[list[Source], str | None]] = {}
        self.skipped_tags: list[str] = []

    # ---- helpers
    def entity(self, display: str, kind: str, source: Source) -> Draft:
        draft = self.by_key.get(_key(display))
        if draft is None:
            draft = Draft(kind, display)
            self.drafts[display] = self.by_key[_key(display)] = draft
        elif draft.kind != kind:
            draft.kind = kind  # table rows are typed provisionally; sources of truth override
        draft.add_source(source)
        return draft

    def person(self, display: str, source: Source) -> Draft:
        draft = self.entity(display, "person", source)
        if draft not in self.persons:
            self.persons.append(draft)
        return draft

    def edge(self, type_: str, src: Draft, dst: Draft, source: Source, note: str | None = None) -> None:
        self.edges.setdefault((type_, src.display, dst.display), ([source], note))

    # ---- stages
    def table_rows(self) -> None:
        for index, row in enumerate(self.table):
            kind = next((k for word, k in ROLE_KINDS if word in row["role"]), "term")
            owner = "维护者" in row["source"].lower()
            draft = Draft(kind, row["name"], row["canonical"])
            draft.aliases = list(row["aliases"])
            draft.add_source(_src("owner" if owner else "ledger", row["source"]))
            rest = {k: v for k, v in row.items() if k not in ("name", "aliases", "canonical")}
            draft.subtitle = {"order": index, **rest}
            self.drafts[draft.display] = self.by_key[_key(draft.display)] = draft
            for alias in row["aliases"]:
                draft.alias_refs[_key(alias)] = draft.sources[0]

    def match(self, *names: str) -> Draft | None:
        for person in self.persons:
            surfaces = {_key(s) for s in (person.display, person.official or "", *person.aliases)}
            if any(_key(n) in surfaces for n in names):
                return person
        return None

    def roster(self) -> None:
        overrides = _read(FILES["overrides"])["member_overrides"]
        ref = FILES["overrides"]
        for official, row in overrides.items():
            base = self.by_key.get(_key(row["canonical"]))
            person = self.person(base.display if base else row["canonical"], _src("ledger", f"{ref}#{official}"))
            person.official = official
            for alias in (row["canonical"], *row["aliases"]):
                person.add_alias(alias, _src("ledger", f"{ref}#{official}"))
        md_ref = FILES["roster"]
        group = self.entity("PSPLive", "group", _src("ledger", md_ref))
        text = (ROOT / md_ref).read_text("utf-8")
        for line in text.splitlines():
            if line.startswith("- 组织："):
                head, *rest = [s.strip() for s in line[len("- 组织："):].split("；")]
                for name in re.split(r"\s*/\s*", head):
                    clean = re.sub(r"[（(].*", "", name).strip("“”\" ")
                    group.add_alias(clean, _src("ledger", md_ref))
                for seg in rest:
                    found = re.match(r"创始人\s*([^（(]+)(?:[（(]([^）)]+)[）)])?", seg)
                    if found:
                        founder = self.person(self.by_key[_key(found[1].strip())].display, _src("ledger", md_ref))
                        if found[2]:
                            founder.add_alias(found[2], _src("ledger", md_ref))
                        self.edge("founded", founder, group, _src("ledger", md_ref))
        retired = False
        for line in text.splitlines():
            if line.startswith("## "):
                retired = "休止" in line or "退出" in line
            cells = [c.strip() for c in line.strip().strip("|").split("|")] if line.startswith("|") else []
            if not cells or cells[0] in ("艺名", "") or set(cells[0]) <= {"-", " "}:
                continue
            found = re.match(r"([^（(]+)[（(]([^）)]+)[）)]", cells[0])
            name, extra = (found[1], [found[2]]) if found else (cells[0], [])
            if not retired and len(cells) > 1:
                for token in re.split(r"[；;]", cells[1]):
                    token = re.sub(r"^(粉丝名|梗)[：:]", "", token.replace("**", "").strip())
                    extra += [t.strip() for t in re.split(r"[（(]", token)[0].split("/") if t.strip() not in ("", "—")]
            base = self.by_key.get(_key(name))
            person = self.match(name) or self.person(base.display if base else name, _src("ledger", md_ref))
            person.add_source(_src("ledger", md_ref))
            for alias in (name, *extra):
                person.add_alias(alias, _src("ledger", md_ref))
            self.edge("member_of", person, group, _src("ledger", md_ref),
                      note=f"名册备注：{cells[-1]}" if retired and len(cells) > 1 else None)

    def registry(self) -> None:
        data = _read(FILES["registry"])
        owners = {s["organization_id"]: s["owner_name"] for s in data["sources"] if "owner_name" in s}
        for account in data["reviewed_accounts"]:
            ref = _src("ledger", f"{FILES['registry']}#{account['account_name']}", account["verified_at"])
            person = self.match(account["canonical"], account["account_name"]) or self.person(account["canonical"], ref)
            person.add_source(ref)
            person.official = person.official or account["account_name"]
            person.add_alias(account["canonical"], ref)
            if _key(account["account_name"]) != _key(person.official):
                person.add_alias(account["account_name"], ref)
            org = account["affiliation"]
            name = self.by_key[_key(org)].display if _key(org) in self.by_key else owners.get(org, org)
            self.edge("member_of", person, self.entity(name, "group", ref), ref)

    def identity(self) -> None:
        profile = _read(FILES["profile"])["identity"]
        ref = _src("ledger", f"{FILES['profile']}#identity")
        host = self.match(profile["display_name"])
        assert host is not None, "host must be in the roster"
        for alias in (*profile["self_reference_aliases"], *profile["speaker_identity_aliases"]):
            host.add_alias(alias, ref)
        for text in re.findall(r"粉丝\s*([A-Za-z0-9_]+)\s*[(（]", json.dumps(profile, ensure_ascii=False)):
            fans = self.entity(self.by_key[_key(text)].display, "community", ref)
            self.edge("fan_name_of", fans, host, ref)

    def facts(self) -> None:
        revisions = _read(FILES["facts"])["revisions"]
        rows = [r for r in revisions if r["claim"]["kind"] == "IDENTITY_BACKGROUND"][-4:]
        groups: dict[str, Draft] = {}
        quoted = [(re.search(r"“([^”]+)”", r["statement_zh"])[1], r["statement_zh"]) for r in rows]
        # An entity the facts call a 企划 is a project (members participate); otherwise a group.
        projects = {name for name, statement in quoted if "企划" in statement}
        for row in rows:
            text = row["statement_zh"]
            ref = _src("ledger", f"{FILES['facts']}#{row['fact_id']}", row["recorded_at"])
            name = re.search(r"“([^”]+)”", text)[1]
            kind, link = ("project", "participates_in") if name in projects else ("group", "member_of")
            group = groups.get(name) or groups.setdefault(name, self.entity(name, kind, ref))
            group.add_source(ref)
            members = re.search(r"最初成员是(.+?)。|三人(.+?)的", text)
            founder = re.search(r"由[^的]*?的\s*([^\s发]+?)\s*发起", text)
            if members:
                for member in re.split(r"、|和", members[1] or members[2]):
                    member = re.sub(r"[（(].*", "", member).strip()
                    node = self.by_key.get(_key(member))
                    self.edge(link, self.person(node.display if node else member, ref), group, ref)
            if founder:
                node = next((d for d in self.persons if founder[1].startswith(d.display)), None)
                person = self.person(node.display if node else founder[1], ref)
                person.add_alias(founder[1], ref)
                self.edge("founded", person, group, ref)
            summary = re.sub(r"^圈内背景（?[^：）]*）?：?", "", text).strip()
            if row["claim"]["object_text"] == text and len(summary) > len(group.summary):
                group.summary = summary

    def alias_edges(self) -> None:
        for owner in self.drafts.values():
            for surface in (*owner.aliases, owner.official, owner.subtitle_name):
                other = self.by_key.get(_key(surface or ""))
                if other is not None and other is not owner:
                    ref = owner.alias_refs.get(_key(surface or ""), owner.sources[0])
                    self.edges.setdefault(("alias_of", other.display, owner.display), ([ref], None))

    def tags(self) -> None:
        parent = {src: dst for type_, src, dst in self.edges if type_ == "alias_of"}

        def root(display: str) -> str:
            while display in parent:
                display = parent[display]
            return display

        found: dict[str, set[str]] = {}
        for rule in _read(FILES["tags"])["term_rules"]:
            if len(rule["tags"]) != 1:
                continue
            hits = {root(self.by_key[_key(p)].display) for p in rule["patterns"]
                    if not re.search(r"[\\()\[\]?*+|^${}]", p) and _key(p) in self.by_key}
            if len(hits) == 1:
                found.setdefault(hits.pop(), set()).add(rule["tags"][0])
        for display, tags in found.items():
            if len(tags) == 1:
                self.drafts[display].tag = next(iter(tags))
                self.drafts[display].add_source(_src("ledger", FILES["tags"]))
            else:
                self.skipped_tags.append(f"{display}: {sorted(tags)}")

    def graph(self) -> Graph:
        self.table_rows()
        self.roster()
        self.registry()
        self.identity()
        self.facts()
        person_keys = {_key(s) for p in self.persons for s in (p.display, p.official or "", *p.aliases)}
        for draft in self.drafts.values():
            if draft.subtitle is not None and _key(draft.display) in person_keys:
                draft.kind = "person" if draft.kind != "group" else "group"
        self.alias_edges()
        self.tags()
        nodes = []
        for draft in self.drafts.values():
            uses = {"subtitle": draft.subtitle_name, "official": draft.official, "tag": draft.tag}
            names = {"display": draft.display, **{use: value for use, value in uses.items() if value}}
            subtitle = draft.subtitle
            if subtitle is not None and subtitle["order"] is not None:
                row_aliases = self.table[subtitle["order"]]["aliases"]
                if row_aliases != draft.aliases:
                    subtitle = {"order": subtitle["order"], "table_aliases": row_aliases,
                                **{k: v for k, v in subtitle.items() if k != "order"}}
            nodes.append(Node(draft.id, draft.kind, names, tuple(draft.aliases), draft.summary, "accepted",
                              tuple(draft.sources), None, subtitle))
        ids = {d.display: d.id for d in self.drafts.values()}
        edges = [Edge(f"{ids[a]}|{t}|{ids[b]}", t, ids[a], ids[b], "accepted", tuple(src), note)
                 for (t, a, b), (src, note) in self.edges.items()]
        return Graph(nodes, edges)


def project_text(graph: Graph) -> str:
    rows = sorted((n for n in graph.nodes.values() if n.subtitle and "order" in n.subtitle),
                  key=lambda n: n.subtitle["order"])
    entries = []
    for node in rows:
        sub = dict(node.subtitle or {})
        sub.pop("order")
        aliases = sub.pop("table_aliases", list(node.aliases))
        entry: dict[str, JsonValue] = {"name": node.names["display"], "aliases": aliases}
        for key, value in sub.items():
            if key in BEFORE_CANONICAL and "canonical" not in entry:
                entry["canonical"] = node.names["subtitle"]
            entry[key] = value
        entries.append(entry)
    return json.dumps(entries, ensure_ascii=False, indent=1) + "\n"


def _report(graph: Graph, seeder: Seeder) -> None:
    print("nodes:", dict(Counter(n.kind for n in graph.nodes.values())), len(graph.nodes))
    print("edges:", dict(Counter(e.type for e in graph.edges)), len(graph.edges))
    terms = [n.names["display"] for n in graph.nodes.values() if n.kind == "term"]
    print("term count:", len(terms), "first30:", terms[:30])
    print("alias_of:", [(e.from_id, e.to_id) for e in graph.edges if e.type == "alias_of"])
    print("tag conflicts skipped:", seeder.skipped_tags)


def seed(profile: str, force: bool) -> int:
    root = ROOT / "assets" / profile
    if (root / "knowledge/nodes.jsonl").exists() and not force:
        print("graph exists; pass --force to overwrite", file=sys.stderr)
        return 2
    seeder = Seeder()
    graph = seeder.graph()
    with tempfile.TemporaryDirectory() as tmp:
        dump_graph(graph, Path(tmp))
        try:
            load_graph(Path(tmp))
        except GraphError as error:
            print(f"seed produced an invalid graph: {error}", file=sys.stderr)
            return 1
    dump_graph(graph, root)
    _report(graph, seeder)
    return 0


def project(profile: str, check: bool) -> int:
    text = project_text(load_graph(ROOT / "assets" / profile))
    current = TABLE.read_text("utf-8")
    if check:
        if text == current:
            return 0
        old, new = json.loads(current), json.loads(text)
        names = [a.get("name") for a, b in zip(old, new, strict=False) if a != b]
        print(f"proper_nouns.json differs from the graph: len {len(old)} vs {len(new)}, entries {names}")
        return 1
    TABLE.write_text(text, encoding="utf-8")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", default="lidousha")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("seed").add_argument("--force", action="store_true")
    sub.add_parser("project").add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    return seed(args.profile, args.force) if args.command == "seed" else project(args.profile, args.check)


if __name__ == "__main__":
    raise SystemExit(main())
