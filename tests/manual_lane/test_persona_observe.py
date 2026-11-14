"""A sealed clip is read once for what it shows of the host (persona_observe; owner,).
Fakes only: no model, no network."""

from __future__ import annotations

import json

from fakes import FakeRemote, make_ctx

from src.autoslice.manual_lane import channel_facts, persona_observe as po
from src.autoslice.manual_lane import speaker_v8 as sv

H, G = sv.HOST, sv.GUEST_LABEL


class Cpa:
    def __init__(self, answer):
        self.answer, self.prompts = answer, []

    def ask(self, *, call_id, prompt_text, model, effort, remote_dir, max_output_tokens=None):
        self.prompts.append(prompt_text)
        return {"call_id": call_id, "model": model, "effort": effort, "rc": 0 if self.answer is not None else 1, "elapsed_s": 0.1,
                "response_text": json.dumps({"traits": self.answer}, ensure_ascii=False) if self.answer is not None else "",
                "stderr_tail": ""}


def _clip(tmp_path, cid, date, answer, lines, *, reviewed=True):
    ctx = make_ctx(tmp_path, remote=FakeRemote(), cpa=Cpa(answer))
    ctx.session["recording_date"] = date
    (ctx.clip_dir / "speaker").mkdir(parents=True, exist_ok=True)
    (ctx.clip_dir / "speaker/speaker.srt").write_text("\n\n".join(
        f"{i}\n00:00:0{i},000 --> 00:00:0{i + 1},000\n[{who}] {text}" for i, (who, text) in enumerate(lines, 1)) + "\n", encoding="utf-8")
    if reviewed:
        ctx.state.record("review", ok=True, reason="", products={}, cpa=[], note="")
    return ctx


TRAIT = {"statement": "她把自己叫作小熊。", "kind": "自称", "quotes": ["可以教教小熊吗"]}
LINES = [(H, "可以教教小熊吗"), (G, "视频里的人说小熊真可爱"), (H, "今天吃了火锅")]


def test_a_trait_is_kept_only_with_her_own_words_and_only_after_the_review(tmp_path):
    not_hers = {"statement": "她觉得小熊可爱。", "kind": "立场", "quotes": ["视频里的人说小熊真可爱"]}
    invented = {"statement": "她喜欢跑步。", "kind": "喜好", "quotes": ["我每天都跑步"]}
    ctx = _clip(tmp_path, "clip", "2026-11-01", [TRAIT, not_hers, invented], LINES)
    line = po.after_run(ctx)
    assert "她把自己叫作小熊。" in line and "跑步" not in line and "觉得小熊可爱" not in line
    rows = po.load(po.task_of(ctx))
    assert [r["statement"] for r in rows] == ["她把自己叫作小熊。"]
    assert rows[0]["witnesses"] == [{"cid": "clip", "recording_date": "2026-11-01", "quotes": ["可以教教小熊吗"]}]
    assert po.after_run(ctx) == "" and len(ctx.adapters.cpa.prompts) == 1          # a clip is read once
    unreviewed = _clip(tmp_path / "u", "clip", "2026-11-01", [TRAIT], LINES, reviewed=False)
    assert po.after_run(unreviewed) == "" and unreviewed.adapters.cpa.prompts == []


def test_what_the_persona_already_says_is_not_observed_again(tmp_path, monkeypatch):
    monkeypatch.setattr(channel_facts, "spoken", lambda text=None: "- 自称：她把自己叫作小熊")
    ctx = _clip(tmp_path, "clip", "2026-11-01", [TRAIT], LINES)
    assert "没有人设里还没有的稳定特点" in po.after_run(ctx) and po.load(po.task_of(ctx)) == []


def test_a_failed_call_is_said_and_the_clip_is_read_next_time(tmp_path):
    ctx = _clip(tmp_path, "clip", "2026-11-01", None, LINES)
    assert "模型没有回答" in po.after_run(ctx) and not (ctx.clip_dir / po.MARK).exists()


def test_a_trait_two_sessions_bear_out_reaches_the_readers_of_the_channel_facts(tmp_path):
    first = _clip(tmp_path, "clip", "2026-11-01", [TRAIT], LINES)
    po.after_run(first)
    task = po.task_of(first)
    assert po.lines(task) == [] and channel_facts.observed(task) == ""                       # one session is not enough
    again = dict(TRAIT, statement="她把自己叫做小熊")                                          # the same trait in other words
    first.session["recording_date"], first.state.data["clip"]["cid"] = "2026-11-02", "clip-b"     # another clip of another session
    first.adapters.cpa.answer = [again]
    (first.clip_dir / po.MARK).unlink()
    po.after_run(first)
    rows = po.load(task)
    assert len(rows) == 1 and [w["recording_date"] for w in rows[0]["witnesses"]] == ["2026-11-01", "2026-11-02"]
    assert po.lines(task) == ["- 她把自己叫作小熊。（2 场直播的切片里出现过，尚未审定）"]
    block = channel_facts.for_subtitles(task)
    assert channel_facts.OBSERVED_HEAD in block and "她把自己叫作小熊。" in block and block.startswith(channel_facts.SUBTITLE_NOTE)
    assert channel_facts.OBSERVED_HEAD not in channel_facts.for_subtitles()
