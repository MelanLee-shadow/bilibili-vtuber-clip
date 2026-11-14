"""Work and proper-name lookup (TASK-5 item 5; TASK-6 C): topic graph, Bangumi, zh Wikipedia, Baidu Baike, then the
Search Router only when configured.  Each failing layer is recorded and the next runs; all failing never blocks.  No
test reaches the network: the web side is a fake.  Synthetic names only."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

from src.autoslice.manual_lane import work_lookup as wl

GRAPH = {"entities": [{"canonical_zh": "高松灯", "entity_id": "bgm:character:127790", "kind": "character",
                       "native_names": ["高松燈"], "aliases": ["Tomorin"], "readings": ["Tomori"]}]}
ROOT = Path(__file__).resolve().parents[2]


class FakeWeb:
    def __init__(self, *, fail=(), results=None, web_configured=False):
        self.fail, self.results, self.web_configured = set(fail), results or {}, web_configured
        self.calls: list[tuple[str, str]] = []

    def _do(self, layer, query):
        self.calls.append((layer, query))
        if layer in self.fail:
            raise OSError(f"{layer} down")
        return list(self.results.get((layer, query), []))

    def bangumi(self, q):
        return self._do("bangumi", q)

    def zhwiki(self, q):
        return self._do("zhwiki", q)

    def baike(self, q):
        return self._do("baike", q)

    def web(self, q):
        return self._do("web", q)


def test_a_name_in_the_topic_graph_is_answered_there_without_the_network():
    web = FakeWeb()
    out = wl.lookup("Tomorin", GRAPH, web=web)
    assert out["source"] == "topic_entity_graph" and out["results"][0]["name"] == "高松灯" and web.calls == []


def test_each_failing_layer_is_recorded_and_the_next_layer_runs():
    web = FakeWeb(fail={"bangumi", "zhwiki"}, results={("baike", "某作品甲"): [{"title": "某作品甲", "abstract": "x"}]})
    out = wl.lookup("某作品甲", GRAPH, web=web)
    assert [c[0] for c in web.calls] == ["bangumi", "zhwiki", "baike"]
    errors = {x["layer"]: x.get("error", "") for x in out["layers"]}
    assert errors["bangumi"].startswith("OSError") and errors["zhwiki"].startswith("OSError")
    assert out["source"] == "baike"


def test_baike_is_asked_again_with_the_formal_name_found_by_an_earlier_layer():
    web = FakeWeb(results={("bangumi", "简称乙"): [{"id": 1, "name": "原名", "name_cn": "正式名乙"}],
                           ("baike", "正式名乙"): [{"title": "正式名乙", "abstract": "y"}]})
    out = wl.lookup("简称乙", GRAPH, web=web)
    assert ("baike", "简称乙") in web.calls and ("baike", "正式名乙") in web.calls
    assert any(r["layer"] == "baike" and r["title"] == "正式名乙" for r in out["results"])


def test_every_layer_failing_returns_no_result_and_never_raises():
    web = FakeWeb(fail={"bangumi", "zhwiki", "baike", "web"}, web_configured=True)
    out = wl.lookup("某作品丙", GRAPH, web=web)
    assert out["results"] == [] and out["source"] == "none"
    assert [x["layer"] for x in out["layers"]] == ["topic_entity_graph", "bangumi", "zhwiki", "baike", "search_router"]


def test_the_search_router_is_never_called_when_not_configured():
    web = FakeWeb(web_configured=False)
    out = wl.lookup("某作品丁", GRAPH, web=web)
    assert "web" not in [c[0] for c in web.calls]
    assert out["layers"][-1]["layer"] == "search_router" and "未配置" in out["layers"][-1]["skipped"]
    assert wl.WebLookup(search_mcp_url="").web_configured is False


def test_the_search_router_is_the_fallback_only_when_no_result_names_the_query():
    noise = {("bangumi", "简称戊"): [{"id": 2, "name": "x", "name_cn": "无关作品"}]}
    web = FakeWeb(results=noise | {("web", "简称戊"): [{"title": "简称戊是某作品的简称", "url": "u"}]}, web_configured=True)
    out = wl.lookup("简称戊", GRAPH, web=web)
    assert ("web", "简称戊") in web.calls and any(r["layer"] == "search_router" for r in out["results"])
    named = {("bangumi", "简称己"): [{"id": 3, "name": "x", "name_cn": "简称己全名"}]}
    web2 = FakeWeb(results=named, web_configured=True)
    wl.lookup("简称己", GRAPH, web=web2)
    assert "web" not in [c[0] for c in web2.calls]


def test_a_query_too_short_to_match_is_not_matched_to_the_graph():
    assert wl.topic_hits("灯", GRAPH) == []


def test_no_private_network_address_in_code_or_assets():
    pat = re.compile(r"(?<![\d.])(100\.(6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.\d{1,3}|10\.\d{1,3}\.\d{1,3}|192\.168\.\d{1,3}"
                     r"|172\.(1[6-9]|2\d|3[01])\.\d{1,3})\.\d{1,3}(?![\d.])")
    hits = []
    for base in ("src/autoslice/manual_lane", "assets/manual_lane", "scripts/manual_lane.py"):
        for path in [ROOT / base] if (ROOT / base).is_file() else (ROOT / base).rglob("*"):
            if path.is_file() and path.suffix in {".py", ".md", ".json", ".sh", ".txt", ".yaml"}:
                hits += [f"{path}:{m.group(0)}" for m in pat.finditer(path.read_text(encoding="utf-8", errors="ignore"))]
    assert hits == []


def test_the_mcp_result_reader_accepts_json_and_sse_bodies():
    body = {"jsonrpc": "2.0", "id": 2, "result": {"content": [{"type": "text", "text": json.dumps(
        {"results": [{"title": "t", "url": "u", "snippet": "s"}]})}]}}
    assert wl._mcp_results(json.dumps(body))[0]["url"] == "u"
    assert wl._mcp_results("event: message\ndata: " + json.dumps(body) + "\n\n")[0]["title"] == "t"


TASK_DIR = Path(os.environ.get("MANUAL_LANE_TASK_DIR", "/nonexistent"))   # the evaluation task directory, if any
TEST_SET = ("auto_193106_681_874", "auto_200110_15_229", "auto_210119_692_898")


def test_no_test_set_sentence_is_written_into_any_prompt():
    """U116/U121: the test set is for evaluation only.  No sentence of a test-set clip (BCUT or jianying text, 5+
    characters after normalisation) and no line of the d055 answer may appear in a prompt.  Skipped where the task
    directory is not present (the check reads the clips there; nothing from them is stored in the repo)."""
    import pytest

    srts = [TASK_DIR / "clips" / cid / "asr" / f"{name}.srt" for cid in TEST_SET for name in ("bcut", "jianying")]
    srts = [p for p in srts if p.exists()]
    if not srts:
        pytest.skip("task directory with the test-set clips is not present")
    def norm(t: str) -> str:
        return "".join(ch for ch in t if ch.isalnum())
    sentences = set()
    for p in srts:
        for line in p.read_text(encoding="utf-8").splitlines():
            if line.strip() and "-->" not in line and not line.strip().isdigit() and len(norm(line)) >= 5:
                sentences.add(norm(line))
    answer = TASK_DIR / "truth-page" / "test-20261101" / "doubt2" / "reviewer_answer_d055.json"
    if answer.exists():
        sentences |= {norm(str(v)) for v in json.loads(answer.read_text(encoding="utf-8")).values()
                      if isinstance(v, str) and len(norm(str(v))) >= 5}
    prompts = {p.name: norm(p.read_text(encoding="utf-8")) for p in (ROOT / "assets/manual_lane/prompts").glob("*.md")}
    hits = [(name, s) for name, text in prompts.items() for s in sentences if s in text]
    assert hits == []


def test_the_search_router_address_comes_from_the_profile_and_the_environment_overrides_it(monkeypatch):
    from src.autoslice.manual_lane import layout as lay

    monkeypatch.setenv("MANUAL_LANE_ROOM_ID", "1")
    monkeypatch.delenv(wl.SEARCH_MCP_ENV, raising=False)
    lay.layout.cache_clear()
    try:
        profile_url = json.loads((ROOT / "profiles" / "lidousha" / "manual_lane.json").read_text(encoding="utf-8"))[
            "search_mcp_url"]
        assert profile_url and wl.WebLookup().web_configured is True
        assert wl.WebLookup().search_mcp_url == profile_url
        assert wl.WebLookup(search_mcp_url="http://explicit.test/mcp").search_mcp_url == "http://explicit.test/mcp"
        monkeypatch.setenv(wl.SEARCH_MCP_ENV, "http://override.test/mcp")
        lay.layout.cache_clear()
        assert wl.WebLookup().search_mcp_url == "http://override.test/mcp"
    finally:
        lay.layout.cache_clear()
