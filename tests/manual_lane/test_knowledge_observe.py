"""A sealed clip is read once for the names it uses that the knowledge graph lacks (knowledge_observe; owner
"这种切片过程中补知识图的过程也很重要").  Fakes only: no model, no network; the graph is a small fixture."""

from __future__ import annotations

import json

from fakes import FakeRemote, make_ctx

from src.autoslice.knowledge_graph import Graph, Node, Source
from src.autoslice.manual_lane import channel_facts, knowledge_observe as ko, references
from src.autoslice.manual_lane import speaker_v8 as sv

H = sv.HOST
HOST = Node(id="person:主播甲", kind="person", names={"display": "主播甲"}, aliases=("甲甲",), summary="",
            status="accepted", sources=(Source("owner", "test", None),))


class Cpa:
    def __init__(self, answer):
        self.answer, self.prompts = answer, []

    def ask(self, *, call_id, prompt_text, model, effort, remote_dir, max_output_tokens=None):
        self.prompts.append(prompt_text)
        return {"call_id": call_id, "model": model, "effort": effort, "rc": 0 if self.answer is not None else 1,
                "elapsed_s": 0.1, "stderr_tail": "",
                "response_text": json.dumps({"items": self.answer}, ensure_ascii=False) if self.answer is not None else ""}


def _clip(tmp_path, monkeypatch, cid, date, answer, lines, *, reviewed=True):
    monkeypatch.setattr(ko, "graph", lambda: Graph([HOST], []))
    seeds = []
    monkeypatch.setattr(references, "offer_seeds", seeds.extend)
    ctx = make_ctx(tmp_path, remote=FakeRemote(), cpa=Cpa(answer))
    ctx.session["recording_date"] = date
    ctx.state.data["clip"]["cid"] = cid
    (ctx.clip_dir / "speaker").mkdir(parents=True, exist_ok=True)
    (ctx.clip_dir / "speaker/speaker.srt").write_text("\n\n".join(
        f"{i}\n00:00:0{i},000 --> 00:00:0{i + 1},000\n[{who}] {text}" for i, (who, text) in enumerate(lines, 1)) + "\n",
        encoding="utf-8")
    if reviewed:
        ctx.state.record("review", ok=True, reason="", products={}, cpa=[], note="")
    return ctx, seeds


ALIAS = {"type": "alias", "name": "李李", "of": "person:主播甲", "why": "观众对着她叫", "quotes": ["李李你好"]}
WORK = {"type": "entity", "name": "后会友期", "kind": "work", "why": "她在看的节目", "quotes": ["今天看后会友期"]}
LINES = [(H, "李李你好"), (H, "今天看后会友期"), (H, "甲甲来了"), (H, "谢谢小红的舰长")]


def test_names_are_kept_only_with_quotes_that_hold_them_and_only_after_the_review(tmp_path, monkeypatch):
    known = {"type": "alias", "name": "甲甲", "of": "person:主播甲", "quotes": ["甲甲来了"]}        # already a spelling
    no_node = {"type": "alias", "name": "小乙", "of": "person:没有这个人", "quotes": ["李李你好"]}
    invented = {"type": "entity", "name": "不存在的节目", "kind": "work", "quotes": ["今天看不存在的节目"]}
    elsewhere = {"type": "entity", "name": "火锅", "kind": "term", "quotes": ["李李你好"]}       # quote lacks the name
    viewer = {"type": "entity", "name": "小红", "kind": "person", "quotes": ["谢谢小红的舰长"]}   # a viewer thanked
    ctx, seeds = _clip(tmp_path, monkeypatch, "clip", "2026-11-01", [ALIAS, WORK, known, no_node, invented, elsewhere, viewer],
                       LINES)
    line = ko.after_run(ctx)
    assert "「李李」是「主播甲」的另一个叫法" in line and "后会友期" in line
    assert all(word not in line for word in ("甲甲」是", "小乙", "不存在的节目", "火锅", "小红"))
    rows = ko.load(ko.persona_observe.task_of(ctx))
    assert [(r["type"], r["name"]) for r in rows] == [("alias", "李李"), ("entity", "后会友期")]
    assert rows[0]["of"] == "person:主播甲" and rows[1]["kind"] == "work"
    assert rows[0]["witnesses"] == [{"cid": "clip", "recording_date": "2026-11-01", "quotes": ["李李你好"]}]
    assert [s["name"] for s in seeds] == ["李李", "后会友期"]                    # the crawler gets them as seeds
    assert "person:主播甲：主播甲、甲甲" in ctx.adapters.cpa.prompts[0]          # the model sees what the graph has
    assert ko.after_run(ctx) == "" and len(ctx.adapters.cpa.prompts) == 1      # a clip is read once
    unreviewed, _ = _clip(tmp_path / "u", monkeypatch, "clip", "2026-11-01", [ALIAS], LINES, reviewed=False)
    assert ko.after_run(unreviewed) == "" and unreviewed.adapters.cpa.prompts == []


def test_a_failed_call_is_said_and_the_clip_is_read_next_time(tmp_path, monkeypatch):
    ctx, _ = _clip(tmp_path, monkeypatch, "clip", "2026-11-01", None, LINES)
    assert "模型没有回答" in ko.after_run(ctx) and not (ctx.clip_dir / ko.MARK).exists()


def test_a_name_two_sessions_bear_out_reaches_the_subtitle_agent(tmp_path, monkeypatch):
    first, _ = _clip(tmp_path, monkeypatch, "clip", "2026-11-01", [ALIAS], LINES)
    ko.after_run(first)
    task = ko.persona_observe.task_of(first)
    assert ko.lines(task) == [] and channel_facts.observed(task) == ""
    first.session["recording_date"], first.state.data["clip"]["cid"] = "2026-11-02", "clip-b"
    (first.clip_dir / ko.MARK).unlink()
    ko.after_run(first)
    rows = ko.load(task)
    assert len(rows) == 1 and [w["recording_date"] for w in rows[0]["witnesses"]] == ["2026-11-01", "2026-11-02"]
    assert ko.lines(task) == ["- 「李李」是「主播甲」的另一个叫法（2 场直播的切片里出现过，尚未审定）"]
    assert "「李李」是「主播甲」的另一个叫法" in channel_facts.for_subtitles(task)
