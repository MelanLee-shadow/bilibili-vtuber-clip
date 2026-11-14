"""TASK-36: one name, one spelling in one clip (COACH-ADVICE e05 §5.1, §5.4, §5.6, §5.7).

The pass the hand push did last and the mechanical lane did not have: after the words are decided, the clip's spellings
of one sound are grouped by code, CPA decides once for the whole clip which of them are a name and which places are it,
and the spelling is one a written source already has.  Only the places CPA names are changed.

The first two tests run the ordinary step-3 path (variant C, the production default) and fail on the code before this
task (0a952e20a): the clip kept both spellings.  The next two are the controls — a homophone CPA does not name is left
alone, and a name written one way sends no name call at all.  The rest check the contract of the pass itself.

Synthetic texts only: no clip of any set, and none of their names.
"""

from __future__ import annotations

import importlib
import json
import re

import pytest

from fakes import FakeRemote, InFlight, make_ctx

from src.autoslice.manual_lane import stage_impl as si
from src.autoslice.manual_lane.adapters import RemoteResult

PROPER_NOUNS = si.PROPER_NOUNS
NAME_CALL = "name-consistency"


def nc():
    """The pass's module, imported late: on the code before this task it does not exist, and the two behaviour tests
    must then fail on the subtitles they read, not on an import."""
    return importlib.import_module("src.autoslice.manual_lane.name_consistency")


# ----- the table the tests run with ---------------------------------------------------------------------------------
def _clear_table_caches() -> None:
    from src.autoslice.manual_lane import name_match

    for name in ("table_rows", "sound_key"):
        clear_fn = getattr(getattr(name_match, name, None), "cache_clear", None)
        if clear_fn is not None:
            clear_fn()


@pytest.fixture
def table(tmp_path, monkeypatch):
    """A proper-noun table of the test's own (the shipped one is 维护者's private channel table, and its names must not
    decide what these tests carry): one name with a note, one with aliases."""
    path = tmp_path / "proper_nouns.test.json"
    path.write_text(json.dumps([
        {"name": "沙豆李", "aliases": [], "role": "测试用倒读称呼",
         "note": "中文语境里念时写「沙豆李」，不写成近音词"},
        {"name": "李豆沙", "aliases": ["小李", "豆沙"], "role": "测试用主播"},
    ], ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr("src.autoslice.manual_lane.name_match.PROPER_NOUNS", path)
    _clear_table_caches()
    yield path
    _clear_table_caches()


# ----- one clip through the ordinary step 3 -------------------------------------------------------------------------
def _srt(rows) -> str:
    return "\n\n".join(f"{i}\n{si.fmt_ts((i - 1) * 2.0)} --> {si.fmt_ts(i * 2.0)}\n{text}"
                       for i, text in enumerate(rows, 1)) + "\n"


class StepCpa:
    """The whole-clip review, the adjudication batches and the name pass, answered by script: the adjudication keeps
    every sentence as it is (KEEP) unless `change` says otherwise, so what the clip decides is what the test wrote."""

    def __init__(self, *, names, review=(), change=None, name_fails=False):
        self.names = names                    # the name pass's answer (a list of group rows)
        self.review = list(review)            # cue ids the whole-clip review marks
        self.change = dict(change or {})      # doubt id -> row the adjudication answers with
        self.name_fails = name_fails
        self.calls: list[str] = []
        self.prompts: dict[str, str] = {}
        self.meter = InFlight()

    def ask(self, *, call_id, prompt_text, model, effort, remote_dir):
        with self.meter:
            self.calls.append(call_id)
            self.prompts[call_id] = prompt_text
        if call_id == NAME_CALL:
            text = json.dumps(self.names, ensure_ascii=False)
            return {"call_id": call_id, "model": model, "effort": effort, "rc": 0 if not self.name_fails else 1,
                    "elapsed_s": 0.1, "response_text": "" if self.name_fails else text,
                    "stderr_tail": "CPA_HTTP_ERROR (fake)" if self.name_fails else ""}
        if call_id == "review-c":
            text = json.dumps({"suspicious": [{"cue_id": c, "reason": "读不通"} for c in self.review]},
                              ensure_ascii=False)
        else:
            ids = list(dict.fromkeys(re.findall(r'"id": "(d\d+)"', prompt_text)))
            text = json.dumps([self.change.get(i, {"id": i, "state": "KEEP", "final_text": "", "confidence": "高",
                                                   "affects_meaning": False}) for i in ids], ensure_ascii=False)
        return {"call_id": call_id, "model": model, "effort": effort, "rc": 0, "elapsed_s": 0.1,
                "response_text": text, "stderr_tail": ""}


def run_clip(tmp_path, rows, cpa: StepCpa, *, hook="", danmaku=()):
    """The clip through step 3 (variant C) with only the network faked: returns (outcome, ctx, cpa)."""
    text = _srt(rows).encode()

    def script(name, body, remote):
        if name == "s3-asr":
            return RemoteResult(0, "ASR_OK\n", "")
        if name.startswith("s3-listen-"):
            model = {m["slug"]: m["name"] for m in si.LISTEN_MODELS}["38m"]
            remote.files["/r/clip/listen/out/all.json"] = json.dumps(
                [{"win": "C000", "model": model, "slug": "38m", "status": "OK",
                  "lines": [{"start_s": 0.0, "text": rows[0]}]}], ensure_ascii=False).encode()
            return RemoteResult(0, 'LISTEN_DONE 1 records 1 {"OK": 1}\n', "")
        if name == "s3-usage":
            return RemoteResult(0, '{"calls": 1, "responded": 1, "not_responded": 0, "input_tokens": 1, '
                                   '"output_tokens": 1, "thinking_tokens": 0, "output_plus_thinking": 1}\n', "")
        return RemoteResult(0, "", "")

    remote = FakeRemote(on_script=script)
    remote.files["/r/clip/asr/bcut.srt"] = text
    remote.files["/r/clip/asr/jianying.srt"] = text
    ctx = make_ctx(tmp_path, remote=remote, cpa=cpa, bcut_text=_srt(rows))
    ctx.state.data["clip"]["hook"] = hook
    if danmaku:
        (ctx.clip_dir / "danmaku").mkdir(parents=True, exist_ok=True)
        (ctx.clip_dir / "danmaku" / "lines.json").write_text(
            json.dumps([{"t_s": 1.0, "text": t} for t in danmaku], ensure_ascii=False), encoding="utf-8")
    return si.step3_subtitles(ctx), ctx, cpa


def final_text(ctx) -> str:
    return (ctx.clip_dir / "subs" / "final.srt").read_text(encoding="utf-8")


# ----- 1. two spellings of one name, CPA chooses one: the whole clip keeps one -------------------------------
def test_a_name_written_two_ways_is_written_one_way_in_the_whole_clip(tmp_path, monkeypatch, table):
    monkeypatch.setenv("MANUAL_LANE_ADJUDICATE_MODE", "C")
    rows = ["我和奈奈莉娅一起走", "奈奈莉亚说等一下", "我们还是一起走吧"]
    cpa = StepCpa(names=[{"group": "g01", "is_name": True, "spelling": "奈奈莉亚",
                          "places": ["p001", "p002"], "reason": "同一个玩家，两处都念 nài nài lì yà"}])
    out, ctx, cpa = run_clip(tmp_path, rows, cpa)
    assert out.ok, (out.reason, out.note)
    text = final_text(ctx)
    assert "奈奈莉娅" not in text and text.count("奈奈莉亚") == 2          # one spelling, both sentences
    assert "我们还是一起走吧" in text                                       # the third sentence is untouched
    assert any(line.startswith("同名统一写法") and "改名 1 处" in line for line in out.process)
    assert any("奈奈莉娅" in line and "奈奈莉亚" in line for line in out.process)
    record = json.loads((ctx.clip_dir / "subs" / "name_consistency.json").read_text(encoding="utf-8"))
    assert [c["from"] for c in record["changes"]] == ["奈奈莉娅"]
    assert record["changes"][0]["to"] == "奈奈莉亚" and record["changes"][0]["cue_id"] == 1
    assert record["changes"][0]["reason"]


# ----- 2. a name the table has a note for: the near-homophone is written as the table writes it ------------
def test_a_name_with_a_table_note_is_written_the_tables_way(tmp_path, monkeypatch, table):
    monkeypatch.setenv("MANUAL_LANE_ADJUDICATE_MODE", "C")
    rows = ["沙斗里今天怎么没来", "你们等沙斗里啊"]
    cpa = StepCpa(names=[{"group": "g01", "is_name": True, "spelling": "沙斗里",   # CPA picks the clip's own spelling
                          "places": ["p001", "p002"], "reason": "说的是主播名字的倒读"}])
    out, ctx, _cpa = run_clip(tmp_path, rows, cpa)
    assert out.ok, (out.reason, out.note)
    text = final_text(ctx)
    assert "沙豆李今天怎么没来" in text and "你们等沙豆李啊" in text and "沙斗里" not in text
    record = json.loads((ctx.clip_dir / "subs" / "name_consistency.json").read_text(encoding="utf-8"))
    assert {c["spelling_source"] for c in record["changes"]} == {"table"}
    assert "沙豆李" in record["changes"][0]["reason"] and "note" in record["changes"][0]["reason"]


# ----- 3. control: a homophone CPA did not name is not touched ---------------------------------------------
def test_a_homophone_cpa_did_not_name_is_not_changed(tmp_path, monkeypatch, table):
    monkeypatch.setenv("MANUAL_LANE_ADJUDICATE_MODE", "C")
    rows = ["奈奈莉娅在旁边", "奈奈莉亚先说", "这是事实", "这是是事吧"]
    cpa = StepCpa(names=[{"group": "g01", "is_name": True, "spelling": "奈奈莉娅",
                          "places": ["p001", "p002"], "reason": "同一个玩家的名字"},
                         {"group": "g02", "is_name": False, "spelling": "", "places": [],
                          "reason": "「事实」「是事」都是普通词"}])
    out, ctx, _cpa = run_clip(tmp_path, rows, cpa)
    assert out.ok, (out.reason, out.note)
    text = final_text(ctx)
    assert "这是事实" in text and "这是是事吧" in text       # the ordinary words, exactly as they were
    record = json.loads((ctx.clip_dir / "subs" / "name_consistency.json").read_text(encoding="utf-8"))
    assert [c["group"] for c in record["changes"]] == ["g01"]
    assert {g["decision"] for g in record["groups"]} == {"cpa", "ordinary"}


# ----- 4. control: a name written one way sends no name call ----------------------------------------------
def test_a_name_with_one_spelling_needs_no_name_call(tmp_path, monkeypatch, table):
    monkeypatch.setenv("MANUAL_LANE_ADJUDICATE_MODE", "C")
    rows = ["沙豆李今天来了", "沙豆李说等一下"]
    out, ctx, cpa = run_clip(tmp_path, rows, StepCpa(names=[]))
    assert out.ok, (out.reason, out.note)
    assert NAME_CALL not in cpa.calls
    assert final_text(ctx).count("沙豆李") == 2
    assert not (ctx.clip_dir / "subs" / "name_consistency.json").exists()
    assert any("没有同一读音的多写法" in line for line in out.process)


# ----- the pass's own contract (unit) ----------------------------------------------------------------------
def _doubts(*texts):
    return [{"id": f"k{i:03d}", "cue_id": i, "state": "KEEP", "current": text, "final_text": text}
            for i, text in enumerate(texts, 1)]


class ScriptedCpa:
    def __init__(self, answer, *, rc=0):
        self.answer, self.rc = answer, rc
        self.calls: list[str] = []
        self.prompts: list[str] = []
        self.meter = InFlight()

    def ask(self, *, call_id, prompt_text, model, effort, remote_dir):
        with self.meter:
            self.calls.append(call_id)
            self.prompts.append(prompt_text)
        return {"call_id": call_id, "model": model, "effort": effort, "rc": self.rc, "elapsed_s": 0.1,
                "response_text": json.dumps(self.answer, ensure_ascii=False) if self.rc == 0 else "",
                "stderr_tail": "" if self.rc == 0 else "CPA_HTTP_ERROR (fake)"}


def _pass(tmp_path, texts, cpa, *, danmaku=()):
    ctx = make_ctx(tmp_path, remote=FakeRemote(), cpa=cpa, bcut_text=_srt(texts))
    if danmaku:
        (ctx.clip_dir / "danmaku").mkdir(parents=True, exist_ok=True)
        (ctx.clip_dir / "danmaku" / "lines.json").write_text(
            json.dumps([{"t_s": 1.0, "text": t} for t in danmaku], ensure_ascii=False), encoding="utf-8")
    doubts = _doubts(*texts)
    adapter = ctx.adapters.cpa

    def ask(_ctx, call_id, prompt):
        return adapter.ask(call_id=call_id, prompt_text=prompt, model="fake", effort="fake", remote_dir="/r/clip")

    return nc().resolve(ctx, doubts, [d["cue_id"] for d in doubts], ask=ask), doubt_rows(doubts), ctx, cpa


def doubt_rows(doubts):
    return [d["final_text"] for d in doubts]


def test_the_question_carries_every_group_at_once_with_its_readings_and_sources(tmp_path, table):
    texts = ["奈奈莉娅先说", "奈奈莉亚后说", "这是事实", "这是是事吧"]
    cpa = ScriptedCpa([])
    ctx = make_ctx(tmp_path, remote=FakeRemote(), cpa=cpa, bcut_text=_srt(texts))
    doubts = _doubts(*texts)
    for doubt in doubts:
        doubt["readings"] = {"bcut": doubt["current"], "jianying": doubt["current"], "audio:38m@P0": "另一种读法"}
        doubt["danmaku"] = ["弹幕里写的名字（句前 3 秒发送）"]
    adapter = ctx.adapters.cpa
    nc().resolve(ctx, doubts, [d["cue_id"] for d in doubts],
                 ask=lambda c, i, p: adapter.ask(call_id=i, prompt_text=p, model="m", effort="e", remote_dir="/r/clip"))
    assert cpa.calls == [NAME_CALL]                                   # one call for the whole clip
    prompt = cpa.prompts[0]
    groups = _groups_of(prompt)
    assert {g["group"] for g in groups} == {"g01", "g02"}             # both groups in the one question
    first = next(g for g in groups if "奈奈莉亚" in g["same_sound"])
    assert set(first["may_write"]) == {"奈奈莉娅", "奈奈莉亚"}
    place = first["places"][0]
    assert set(place) >= {"id", "cue_id", "sentence", "written", "previous", "next", "readings", "danmaku"}
    assert place["readings"]["audio:38m@P0"] == "另一种读法" and place["danmaku"]
    assert place["previous"] or place["next"]                          # the sentence's neighbours
    assert "沙豆李" in prompt and "note" in prompt                      # the table, with its notes
    prompt_file = (PROPER_NOUNS.parent / "prompts" / "name_consistency.md").read_text(encoding="utf-8")
    assert "同一片里同一个名字，全片只用一种写法" in prompt_file and "不许自己拼写法" in prompt_file
    assert "奈奈莉亚" not in prompt_file                                 # a prompt, never one clip's name


def _groups_of(prompt: str) -> list[dict]:
    marker = "输入里的 groups："
    body = prompt[prompt.index(marker) + len(marker):]
    return json.loads(body[body.index("["):])


def test_cpa_may_not_invent_a_spelling_and_such_a_group_is_left_as_it_is(tmp_path, table):
    invented = "nainailiya"                                            # in no table, danmaku or the clip
    cpa = ScriptedCpa([{"group": "g01", "is_name": True, "spelling": invented, "places": ["p001", "p002"],
                        "reason": "按读音拼的"}])
    record, texts, ctx, _cpa = _pass(tmp_path, ["奈奈莉娅先说", "奈奈莉亚后说"], cpa)
    assert texts == ["奈奈莉娅先说", "奈奈莉亚后说"]                      # nothing changed
    assert record["changes"] == [] and record["undecided"] == ["g01"]
    rows = nc().undecided_rows(record)
    assert any(r["spelling"] == "奈奈莉娅" and r["count"] == 1 for r in rows)
    assert all("§5.4" in r["note"] or "自拼" in r["note"] for r in rows)
    doc = json.loads((ctx.clip_dir / "subs" / "name_consistency.json").read_text(encoding="utf-8"))
    assert doc["undecided"] == ["g01"] and doc["changes"] == []


def test_a_spelling_the_danmaku_carries_may_be_chosen(tmp_path, table):
    cpa = ScriptedCpa([{"group": "g01", "is_name": True, "spelling": "奈奈莉雅", "places": ["p001", "p002"],
                        "reason": "弹幕里就是这么写的"}])
    record, texts, _ctx, _cpa = _pass(tmp_path, ["奈奈莉娅先说", "奈奈莉雅后说"], cpa,
                                      danmaku=["奈奈莉雅今天好可爱"])
    assert texts == ["奈奈莉雅先说", "奈奈莉雅后说"]
    assert {c["spelling_source"] for c in record["changes"]} == {"cpa"}


def test_a_latin_spelling_groups_with_the_chinese_one_by_its_initials(tmp_path, table):
    cpa = ScriptedCpa([{"group": "g01", "is_name": True, "spelling": "ntnt", "places": ["p001", "p002", "p003"],
                        "reason": "弹幕只写 ntnt，四个音节 n t n t"}])
    record, texts, _ctx, _cpa = _pass(tmp_path, ["南亭南亭你在吗", "ntnt 快来", "南庭南庭人呢"], cpa,
                                      danmaku=["ntnt 今天好可爱"])
    assert len(record["groups"]) == 1
    group = record["groups"][0]
    assert group.key == ("lan", "tin", "lan", "tin") and group.syllables == ("nan", "ting", "nan", "ting")
    assert "ntnt" in group.spellings
    assert texts == ["ntnt你在吗", "ntnt 快来", "ntnt人呢"]


def test_a_table_alias_in_another_script_is_written_the_tables_chinese_way(tmp_path, table):
    """e05 §5.1: an alias in another script, spoken in a Chinese sentence, is written the table's Chinese way.  The two
    spellings never sound alike, so the grouping joins them through the table's own row, not through pinyin."""
    table.write_text(json.dumps([
        {"name": "李豆沙", "aliases": ["小李", "lds", "Shadow Lee"], "role": "测试用主播"},
        {"name": "雾深Girimi", "aliases": ["雾深", "Girimi"], "role": "测试用嘉宾"},
    ], ensure_ascii=False), encoding="utf-8")
    _clear_table_caches()
    record, texts, _ctx, _cpa = _pass(tmp_path, ["Shadow Lee，你还有何话说", "李豆沙在藏尸体"],
                                      ScriptedCpa([{"group": "g01", "is_name": True, "spelling": "Shadow Lee",
                                                    "places": ["p001", "p002"], "reason": "说的是主播"}]))
    assert texts == ["李豆沙，你还有何话说", "李豆沙在藏尸体"]
    group = next(g for g in record["groups"] if g.fixed == "李豆沙")
    assert set(group.spellings) == {"Shadow Lee", "李豆沙"}
    assert {c["spelling_source"] for c in record["changes"]} == {"table"}
    # a name that already mixes scripts keeps what the speaker said (§5.1: Girimi stays Girimi)
    record2, texts2, _ctx2, _cpa2 = _pass(tmp_path / "b", ["Girimi 快来", "雾深Girimi 你好"], ScriptedCpa([]))
    assert texts2 == ["Girimi 快来", "雾深Girimi 你好"]
    assert not [g for g in record2["groups"] if g.fixed == "雾深Girimi" and "Girimi" in g.spellings]


def test_one_name_seen_at_two_lengths_is_one_group(tmp_path, table):
    """莉亚 and 奈奈莉亚: the short sound sits inside the long one and all its places inside the long one's places, so the
    clip asks about the full name once instead of asking about 莉亚 separately."""
    cpa = ScriptedCpa([])
    record, _texts, _ctx, _cpa = _pass(tmp_path, ["奈奈莉娅先说", "奈奈莉亚后说"], cpa)
    assert [g.key for g in record["groups"]] == [("lai", "lai", "li", "ya")]


def test_a_change_on_a_kept_sentence_is_written_by_the_writer(tmp_path, table):
    """The writer applies `final_text` only for ADOPT / FIT_CONTEXT (step3_adjudicate._final_cues), so a kept sentence
    whose name changes becomes ADOPT with the rewritten words: the words are CPA's decision, the name its one spelling."""
    cpa = ScriptedCpa([{"group": "g01", "is_name": True, "spelling": "奈奈莉亚", "places": ["p001", "p002"],
                        "reason": "同一人"}])
    ctx = make_ctx(tmp_path, remote=FakeRemote(), cpa=cpa, bcut_text=_srt(["奈奈莉娅先说", "奈奈莉亚后说"]))
    doubts = _doubts("奈奈莉娅先说", "奈奈莉亚后说")
    adapter = ctx.adapters.cpa
    nc().resolve(ctx, doubts, [1, 2],
                 ask=lambda c, i, p: adapter.ask(call_id=i, prompt_text=p, model="m", effort="e", remote_dir="/r/clip"))
    assert doubts[0]["state"] == "ADOPT" and doubts[0]["name_fix"][0]["to"] == "奈奈莉亚"
    cues = [{"id": d["cue_id"], "start_s": float(i), "end_s": float(i + 1), "text": d["current"]}
            for i, d in enumerate(doubts, 1)]
    out, _log = si._final_cues(cues, doubts)
    assert [c["text"] for c in out] == ["奈奈莉亚先说", "奈奈莉亚后说"]


def test_a_failed_call_leaves_every_sentence_and_records_the_groups(tmp_path, table):
    cpa = ScriptedCpa([], rc=1)
    record, texts, ctx, _cpa = _pass(tmp_path, ["奈奈莉娅先说", "奈奈莉亚后说"], cpa)
    assert texts == ["奈奈莉娅先说", "奈奈莉亚后说"]                       # the pass never stops the clip
    assert record["cpa_error"] == "CPA_CALL_FAILED"
    assert {g.decision for g in record["groups"]} == {"cpa_failed"}
    assert record["undecided"] == ["g01"]
    doc = json.loads((ctx.clip_dir / "subs" / "name_consistency.json").read_text(encoding="utf-8"))
    assert doc["cpa_error"] == "CPA_CALL_FAILED" and len(doc["groups"]) == 1


def test_the_unresolved_groups_join_the_undecided_name_list(tmp_path, table):
    """`name_match.unknown_line` (the 未定专名 line of PROCESS) shows the groups this pass could not settle too, so a
    reader finds every unresolved name in one place (e05 §5.7)."""
    from src.autoslice.manual_lane import name_match

    line = name_match.unknown_line([], [], [], extra=[{"spelling": "奈奈莉娅", "count": 1, "note": "定不下"}])
    assert "未定专名" in line and "奈奈莉娅×1" in line
    assert name_match.unknown_line([], [], []) == "未定专名（字母写法在名字表、弹幕和选题钩子里都没有，e05 §5.4；只记录）：无"
