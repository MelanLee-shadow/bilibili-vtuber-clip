"""What a clip refers to outside itself is found once and read by every step (references.py; owner a
capability is made once and called wherever it is needed).  Fakes only: no model, no network."""

from __future__ import annotations

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
