"""The work named by the host's on-screen bar: banner reading (a fake media host), Bangumi characters (a fake HTTP
layer answering with invented Bangumi-shaped data), the name check, reuse, and the agent's use of it.  Nothing reaches the network or ssh."""
import importlib.util
import json
import re
import sys
from pathlib import Path

import pytest

from agent_fakes import AgentCpa, answer_rows
from fakes import FakeRemote, make_ctx
from src.autoslice.manual_lane import (
    name_match, playbook_step3 as pb, step3_subtitles as step, work_context as wc, work_lookup as wl)
from src.autoslice.manual_lane.adapters import RemoteResult

FIXTURES = Path(__file__).parent / "fixtures" / "work"
ROOT = Path(__file__).resolve().parents[2]
WORK = "星屑邮局的约定"
BANNER = "正在：星屑邮局的约定"
FOUND_ANSWER = json.dumps({"banner": BANNER, "work": WORK}, ensure_ascii=False)
BANNER_USAGE = {"input_tokens": 300, "output_tokens": 21, "total_tokens": 321}
SUBJECT_ID = 912002


def fixture(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class Http:
    """Bangumi, zh Wikipedia and Baike as the fixtures answer them (a fuzzy search: any keyword gets the same rows)."""

    def __init__(self, *, fail_characters=False):
        self.calls, self.fail_characters = [], fail_characters

    def install(self, web, monkeypatch):
        monkeypatch.setattr(web, "_open", self.open)
        return web

    def open(self, req):
        url = req.full_url
        self.calls.append(url)
        if url == wl.BANGUMI_SEARCH_URL:
            return {}, json.dumps(fixture("search.json"))
        if url.endswith("/characters"):
            if self.fail_characters:
                raise OSError("characters down")
            assert url == f"{wl.BANGUMI_SUBJECT_URL}/{SUBJECT_ID}/characters"
            return {}, json.dumps(fixture("subject_characters.json"))
        if url.startswith(wl.BANGUMI_CHARACTER_URL + "/"):
            return {}, json.dumps(fixture(f"character_{url.rsplit('/', 1)[1]}.json"))
        if "wikipedia" in url:
            return {}, json.dumps({"query": {"search": []}})
        return {}, ""


def banner_remote(answer=FOUND_ANSWER, *, rc=0, usage=BANNER_USAGE):
    """A media host whose read_banner.py leaves banner.json (the tool's own shape), or fails with `rc`."""
    def on_script(name, body, remote):
        if name != "work-banner":
            return RemoteResult(0, "", "")
        if rc:
            return RemoteResult(rc, "", "ffmpeg: no frame")
        _cut, out, t = re.search(r"read_banner\.py (\S+) (\S+) ([\d.]+)'", body).groups()
        doc = {"t": float(t), "answer": answer, "model": "gpt-6.1-sol", "usage": usage,
               "elapsed_s": 2.5, "rc": 0}
        remote.files[f"{out}/banner.json"] = json.dumps(doc, ensure_ascii=False).encode()
        return RemoteResult(0, "", "")
    return FakeRemote(on_script=on_script)


def banner_scripts(remote):
    return [name for name, _ in remote.scripts if name == "work-banner"]


def build_ctx(tmp_path, monkeypatch, remote, *, http=None, cpa=None):
    ctx = make_ctx(tmp_path, remote=remote, cpa=cpa or AgentCpa([]))
    if http is not False:
        http = http or Http()
        ctx.adapters.lookup = http.install(wl.WebLookup(search_mcp_url=""), monkeypatch)
    ctx.http = http
    return ctx


# ---- 1. bangumi_characters --------------------------------------------------------------------
def test_characters_keep_lead_and_supporting_only_with_their_chinese_spellings(tmp_path, monkeypatch):
    web = Http().install(wl.WebLookup(search_mcp_url=""), monkeypatch)
    rows = web.bangumi_characters(SUBJECT_ID)
    assert [(r["id"], r["relation"]) for r in rows] == [(700101, "主角"), (700102, "主角"), (700103, "主角"),
                                                         (700104, "配角")]
    by_id = {r["id"]: r for r in rows}
    assert by_id[700101]["name"] == "ミヤジマ・キラ"
    assert by_id[700101]["names_cn"] == ["宫岛·绮罗", "弥夜岛·琪萝"]        # 简体中文名, then 第二中文名
    assert by_id[700102]["names_cn"] == ["鸣濑·里央", "成濑·莉央"]
    assert by_id[700103]["names_cn"] == ["岚月", "蓝月"]                       # the 昵称 (kana) is not a Chinese spelling
    assert by_id[700104]["names_cn"] == ["维尔", "韦尔"]


def test_characters_are_limited_in_the_order_bangumi_returns_them(monkeypatch):
    related = [{"id": 1000 + i, "name": f"n{i}", "relation": "配角" if i % 2 else "主角"} for i in range(15)]
    related.insert(3, {"id": 9, "name": "extra", "relation": "闲角"})
    asked = []

    def open_(req):
        asked.append(req.full_url)
        if req.full_url.endswith("/characters"):
            return {}, json.dumps(related)
        return {}, json.dumps({"infobox": [{"key": "简体中文名", "value": "某某"}]})

    web = wl.WebLookup(search_mcp_url="")
    monkeypatch.setattr(web, "_open", open_)
    rows = web.bangumi_characters(7)
    assert [r["id"] for r in rows] == [1000 + i for i in range(12)] and len(asked) == 1 + 12
    assert [r["id"] for r in web.bangumi_characters(7, limit=2)] == [1000, 1001]


def test_a_character_without_chinese_spelling_has_an_empty_list(monkeypatch):
    web = wl.WebLookup(search_mcp_url="")
    monkeypatch.setattr(web, "_open", lambda req: ({}, json.dumps([{"id": 5, "name": "オミ", "relation": "配角"}])
                                                    if req.full_url.endswith("/characters")
                                                    else json.dumps({"infobox": [{"key": "别名", "value": [
                                                        {"k": "罗马字", "v": "Omi"}]}]})))
    assert web.bangumi_characters(1)[0]["names_cn"] == []


# ---- 2. spoken_names --------------------------------------------------------------------------
def test_spoken_names_split_dedupe_and_drop_what_is_too_short():
    rows = [{"names_cn": ["弥夜岛·琪萝", "宫岛 绮罗", "啊", "Rio・里央"]}, {"names_cn": ["琪萝", "亚•里", "A•B"]}]
    assert wl.spoken_names(rows) == ["弥夜岛·琪萝", "弥夜岛", "琪萝", "宫岛 绮罗", "宫岛", "绮罗", "Rio・里央", "里央", "亚•里"]    # 亚 and 里 alone are one character each
    assert wl.spoken_names([{"names_cn": ["琪萝"]}, {"names_cn": ["琪萝"]}, {}]) == ["琪萝"]


# ---- 4. the name check ------------------------------------------------------------------------
def row(name_cn, name="", *, type_=2, layer="bangumi", id_=1):
    return {"id": id_, "name": name, "name_cn": name_cn, "type": type_, "layer": layer}


def test_a_subject_is_accepted_only_when_its_name_is_the_work():
    assert wc.pick_subject("星屑邮局的约定", [row("星屑邮局的约定")])["id"] == 1
    assert wc.pick_subject("「星屑邮局的约定」", [row("星屑邮局的约定")]) is not None      # normalised
    assert wc.pick_subject("ほしくずポストのやくそく", [row("某某", "ほしくずポストのやくそく")]) is not None
    # one contains the other, the shorter has four characters
    assert wc.pick_subject("星屑邮局的约定第二季", [row("星屑邮局的约定")]) is not None
    assert wc.pick_subject("星屑邮局", [row("星屑邮局的约定")]) is not None
    # the shorter side has three characters: not accepted
    assert wc.pick_subject("邮局的", [row("星屑邮局的约定")]) is None
    assert wc.pick_subject("星屑邮", [row("星屑邮局的约定")]) is None
    assert wc.pick_subject("无关作品", [row("星屑邮局的约定")]) is None
    assert wc.pick_subject("", [row("")]) is None
    assert wc.pick_subject(WORK, [row(WORK, layer="zhwiki")]) is None                       # Bangumi rows only


def test_with_several_matches_an_anime_comes_first_else_the_first():
    book, anime_a, anime_b = row(WORK, type_=1, id_=1), row(WORK, type_=2, id_=2), row(WORK, type_=2, id_=3)
    assert wc.pick_subject(WORK, [book, anime_a, anime_b])["id"] == 2
    assert wc.pick_subject(WORK, [anime_b, anime_a])["id"] == 3
    other = row(WORK, type_=1, id_=4)
    assert wc.pick_subject(WORK, [book, other])["id"] == 1


# ---- 3. build, four outcomes ------------------------------------------------------------------
def test_found_gives_the_subject_the_characters_and_the_names_the_speaker_could_say(tmp_path, monkeypatch):
    remote = banner_remote()
    ctx = build_ctx(tmp_path, monkeypatch, remote)
    out = wc.build(ctx, cut_len=100.0)
    assert out["status"] == "FOUND" and out["banner"] == BANNER and out["work"] == WORK
    assert out["subject"] == {"id": SUBJECT_ID, "name": "ほしくずポストのやくそく", "name_cn": WORK, "source": "bangumi"}
    assert {"name": "ミヤジマ・キラ", "names_cn": ["宫岛·绮罗", "弥夜岛·琪萝"]} in out["characters"]
    assert "琪萝" in out["names"] and "里央" in out["names"] and "弥夜岛·琪萝" in out["names"]
    assert [c["kind"] for c in out["cpa"]] == ["vision"] and out["cpa"][0]["usage"] == BANNER_USAGE
    assert out["cpa"][0]["model"] == "gpt-6.1-sol" and out["cpa"][0]["rc"] == 0
    assert json.loads((ctx.clip_dir / "work/context.json").read_text(encoding="utf-8")) == out
    script = next(body for name, body in remote.scripts if name == "work-banner")
    assert "read_banner.py /r/clip/cut/cut.mp4 /r/clip/work/banner 50.000" in script        # half of the cut
    assert {"/r/clip/work/read_banner.py", "/r/clip/work/pick_frame.py", "/r/clip/work/screen_banner.md"} <= set(
        remote.files)


def test_banner_only_when_the_subjects_found_are_not_the_work(tmp_path, monkeypatch):
    answer = json.dumps({"banner": "正在：完全不相干的游戏", "work": "完全不相干的游戏"}, ensure_ascii=False)
    out = wc.build(build_ctx(tmp_path, monkeypatch, banner_remote(answer)), cut_len=100.0)
    assert out["status"] == "BANNER_ONLY" and out["banner"] == "正在：完全不相干的游戏"
    assert out["subject"] is None and out["characters"] == [] and out["names"] == [] and out["reason"]


def test_a_work_seen_without_the_bar_is_used_only_when_the_name_library_knows_it(tmp_path, monkeypatch):
    # read off a video title or recognised from the picture (no bar): the library match is what makes it usable
    answer = json.dumps({"banner": "", "work": "完全不相干的游戏", "from": "picture"}, ensure_ascii=False)
    out = wc.build(build_ctx(tmp_path, monkeypatch, banner_remote(answer)), cut_len=100.0)
    assert out["status"] == "NONE" and out["work"] == "" and out["names"] == [] and out["reason"]
    assert wc.agent_view(out) == {"status": "NONE"}


def test_a_work_read_off_a_title_without_the_bar_is_found_when_the_library_knows_it(tmp_path, monkeypatch):
    answer = json.dumps({"banner": "", "work": WORK, "from": "title"}, ensure_ascii=False)
    out = wc.build(build_ctx(tmp_path, monkeypatch, banner_remote(answer)), cut_len=100.0)
    assert out["status"] == "FOUND" and out["banner"] == "" and out["work"] == WORK and out["names"]


def test_banner_only_when_the_bar_names_no_work(tmp_path, monkeypatch):
    answer = json.dumps({"banner": "今日：休息", "work": ""}, ensure_ascii=False)
    ctx = build_ctx(tmp_path, monkeypatch, banner_remote(answer))
    out = wc.build(ctx, cut_len=100.0)
    assert out["status"] == "BANNER_ONLY" and out["banner"] == "今日：休息" and ctx.http.calls == []


@pytest.mark.parametrize("answer", [json.dumps({"banner": "", "work": ""}), "{}", "没有文字栏", "[]", "{\"banner\": "])
def test_none_when_the_model_reads_nothing_or_does_not_answer_in_json(tmp_path, monkeypatch, answer):
    ctx = build_ctx(tmp_path, monkeypatch, banner_remote(answer))
    out = wc.build(ctx, cut_len=100.0)
    assert out["status"] == "NONE" and out["subject"] is None and out["names"] == [] and out["reason"]
    assert ctx.http.calls == [] and len(out["cpa"]) == 1                                    # the reading was paid for


def test_a_json_answer_inside_a_code_fence_is_read(tmp_path, monkeypatch):
    out = wc.build(build_ctx(tmp_path, monkeypatch, banner_remote("```json\n" + FOUND_ANSWER + "\n```")), cut_len=100.0)
    assert out["status"] == "FOUND"


def test_unavailable_when_the_frame_cannot_be_taken_and_nothing_is_raised(tmp_path, monkeypatch):
    ctx = build_ctx(tmp_path, monkeypatch, banner_remote(rc=1))
    out = wc.build(ctx, cut_len=100.0)
    assert out["status"] == "UNAVAILABLE" and "rc=1" in out["reason"] and "no frame" in out["reason"]
    assert out["cpa"] == [] and ctx.http.calls == []


def test_unavailable_when_the_tool_leaves_no_result(tmp_path, monkeypatch):
    ctx = build_ctx(tmp_path, monkeypatch, FakeRemote())
    assert wc.build(ctx, cut_len=100.0)["status"] == "UNAVAILABLE"


def test_unavailable_when_the_lookup_is_not_configured_and_nothing_is_raised(tmp_path, monkeypatch):
    ctx = build_ctx(tmp_path, monkeypatch, banner_remote(), http=False)
    out = wc.build(ctx, cut_len=100.0)
    assert out["status"] == "UNAVAILABLE" and out["banner"] == BANNER and "Bangumi" in out["reason"]
    assert len(out["cpa"]) == 1


def test_unavailable_when_the_character_table_cannot_be_fetched(tmp_path, monkeypatch):
    ctx = build_ctx(tmp_path, monkeypatch, banner_remote(), http=Http(fail_characters=True))
    out = wc.build(ctx, cut_len=100.0)
    assert out["status"] == "UNAVAILABLE" and "characters down" in out["reason"]


def test_unavailable_when_bangumi_is_down(tmp_path, monkeypatch):
    ctx = build_ctx(tmp_path, monkeypatch, banner_remote())
    monkeypatch.setattr(ctx.adapters.lookup, "_open", lambda req: (_ for _ in ()).throw(OSError("network down")))
    out = wc.build(ctx, cut_len=100.0)
    assert out["status"] == "UNAVAILABLE" and "network down" in out["reason"]


# ---- 5. reuse ---------------------------------------------------------------------------------
@pytest.mark.parametrize("answer, status", [(FOUND_ANSWER, "FOUND"), ("{}", "NONE"),
                                            (json.dumps({"banner": "今日：休息", "work": ""}, ensure_ascii=False),
                                             "BANNER_ONLY")])
def test_a_saved_result_is_reused_without_the_picture_or_the_network(tmp_path, monkeypatch, answer, status):
    remote = banner_remote(answer)
    ctx = build_ctx(tmp_path, monkeypatch, remote)
    first = wc.build(ctx, cut_len=100.0)
    scripts, calls = len(remote.scripts), len(ctx.http.calls)
    again = wc.build(ctx, cut_len=100.0)
    assert first["status"] == again["status"] == status
    assert len(remote.scripts) == scripts and len(ctx.http.calls) == calls
    assert again == dict(first, cpa=[])                 # this call made no paid request
    assert json.loads((ctx.clip_dir / "work/context.json").read_text(encoding="utf-8"))["cpa"] == first["cpa"]


def test_an_unavailable_result_is_made_again(tmp_path, monkeypatch):
    remote = banner_remote(rc=1)
    ctx = build_ctx(tmp_path, monkeypatch, remote)
    assert wc.build(ctx, cut_len=100.0)["status"] == "UNAVAILABLE"
    assert wc.build(ctx, cut_len=100.0, retry_unavailable=False)["status"] == "UNAVAILABLE"
    assert len(banner_scripts(remote)) == 1
    ctx.adapters.remote = banner_remote()
    assert wc.build(ctx, cut_len=100.0)["status"] == "FOUND"
    assert len(banner_scripts(ctx.adapters.remote)) == 1


# ---- 6. the agent's first turn ----------------------------------------------------------------
def agent_ctx(tmp_path, monkeypatch, remote, cpa, *, http=None, bcut=("原句",)):
    ctx = build_ctx(tmp_path, monkeypatch, remote, http=http, cpa=cpa)
    ctx.session["participants"] = [["single speaker"]]
    for name in ("bcut", "jianying"):
        path = ctx.clip_dir / f"asr/{name}.srt"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(f"{i}\n00:00:{2 * i - 2:02d},000 --> 00:00:{2 * i:02d},000\n{t}\n\n"
                                for i, t in enumerate(bcut, 1)))
    monkeypatch.setenv("MANUAL_LANE_ADJUDICATE_MODE", "C")
    return ctx


def final_rows(*texts):
    return answer_rows([{"id": f"d{i:03d}", "state": "ADOPT", "final_text": t, "confidence": "高",
                         "affects_meaning": False} for i, t in enumerate(texts, 1)])


def trace_of(ctx):
    return json.loads((ctx.clip_dir / "subs/agent-trace.json").read_text(encoding="utf-8"))


@pytest.fixture(autouse=True)
def screen_work_on(monkeypatch):
    """The lane ships with the picture reading off (lane_rules.json); these tests exercise it switched on."""
    monkeypatch.setattr(wc, "enabled", lambda: True)


def test_a_rules_file_without_the_switch_means_off(monkeypatch):
    monkeypatch.undo()

    def missing(_section):
        raise KeyError("screen_work")

    monkeypatch.setattr(wc, "rules", missing)
    assert wc.enabled() is False


def test_the_picture_reading_is_off_by_default(tmp_path, monkeypatch):
    monkeypatch.undo()
    assert wc.enabled() is False
    cpa = AgentCpa(final_rows("结果"))
    remote = banner_remote()
    out = step._step3_body(agent_ctx(tmp_path, monkeypatch, remote, cpa))
    assert out.ok and first_input(cpa)["work_context"] == {"status": "OFF"}
    assert "work-banner" not in [name for name, _ in remote.scripts]


def first_input(cpa):
    prompt = cpa.prompts[0]
    return json.loads(prompt.split("初始转写与片长：")[1].split("\n此前代理和工具轨迹")[0])


def test_the_first_turn_carries_the_work_context_and_the_selection_reason(tmp_path, monkeypatch):
    cpa = AgentCpa(final_rows("结果"))
    remote = banner_remote()
    ctx = agent_ctx(tmp_path, monkeypatch, remote, cpa)
    out = step._step3_body(ctx)
    assert out.ok
    given = first_input(cpa)
    view = given["work_context"]
    assert set(view) == {"status", "banner", "work", "subject", "names"} and view["status"] == "FOUND"
    assert view["banner"] == BANNER and view["work"] == WORK and view["subject"] == WORK
    assert "琪萝" in view["names"] and "里央" in view["names"]
    assert given["hook"] == "测试钩子" and given["cut_duration_s"] and given["bcut"]
    assert "work_context 是画面文字栏写明" in cpa.prompts[0]
    assert "50.000" not in json.dumps(given) and len(banner_scripts(remote)) == 1


def test_the_picture_reading_is_counted_as_one_of_the_clips_calls_and_its_tokens(tmp_path, monkeypatch):
    cpa = AgentCpa(final_rows("结果"))
    ctx = agent_ctx(tmp_path, monkeypatch, banner_remote(), cpa)
    out = step._step3_body(ctx)
    assert [c["call_id"] for c in out.cpa].count("work-banner-1") == 1 and len(out.cpa) == 4     # 3 turns + 1 reading
    trace = trace_of(ctx)
    assert trace["total_tokens"] == 3 * 10 + BANNER_USAGE["total_tokens"]
    event = next(e for e in trace["events"] if e["event"] == "work_context")
    assert event["status"] == "FOUND" and event["cpa"][0]["usage"] == BANNER_USAGE


@pytest.mark.parametrize("answer, remote_rc", [("{}", 0), (FOUND_ANSWER, 1)])
def test_with_nothing_found_the_first_turn_carries_only_the_status(tmp_path, monkeypatch, answer, remote_rc):
    cpa = AgentCpa(final_rows("结果"))
    ctx = agent_ctx(tmp_path, monkeypatch, banner_remote(answer, rc=remote_rc), cpa)
    assert step._step3_body(ctx).ok                    # a failing reading never fails the subtitle step
    assert first_input(cpa)["work_context"] == {"status": "NONE" if remote_rc == 0 else "UNAVAILABLE"}
    assert first_input(cpa)["hook"] == "测试钩子"


def test_a_resumed_run_sees_the_same_input_and_does_not_read_the_picture_again(tmp_path, monkeypatch):
    original = pb._Agent.finish
    cpa = AgentCpa(final_rows("结果"))
    remote = banner_remote(rc=1)                       # UNAVAILABLE: a rerun would normally try again
    ctx = agent_ctx(tmp_path, monkeypatch, remote, cpa)
    monkeypatch.setattr(pb._Agent, "finish", lambda *_args: (_ for _ in ()).throw(RuntimeError("interrupted")))
    with pytest.raises(RuntimeError):
        step._step3_body(ctx)
    monkeypatch.setattr(pb._Agent, "finish", original)
    ctx.adapters.remote = banner_remote()              # the host is back, but the started run keeps its input
    assert step._step3_body(ctx).ok and len(cpa.calls) == 3
    assert len(banner_scripts(remote)) == 1 and banner_scripts(ctx.adapters.remote) == []


def test_a_resumed_run_keeps_the_picture_reading_in_its_calls_and_tokens(tmp_path, monkeypatch):
    original = pb._Agent.finish
    cpa = AgentCpa(final_rows("结果"))
    ctx = agent_ctx(tmp_path, monkeypatch, banner_remote(), cpa)
    monkeypatch.setattr(pb._Agent, "finish", lambda *_args: (_ for _ in ()).throw(RuntimeError("interrupted")))
    with pytest.raises(RuntimeError):
        step._step3_body(ctx)
    monkeypatch.setattr(pb._Agent, "finish", original)
    out = step._step3_body(ctx)
    assert out.ok and len(cpa.calls) == 3
    assert [c["call_id"] for c in out.cpa].count("work-banner-1") == 1
    assert trace_of(ctx)["total_tokens"] == 3 * 10 + BANNER_USAGE["total_tokens"]


# ---- 7. hints ---------------------------------------------------------------------------------
def saved_context(ctx, names):
    path = ctx.clip_dir / "work/context.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"status": "FOUND", "banner": BANNER, "work": WORK, "reason": "", "cpa": [],
                                "subject": {"id": SUBJECT_ID, "name": "x", "name_cn": WORK, "source": "bangumi"},
                                "characters": [], "names": names}, ensure_ascii=False), encoding="utf-8")


def hint_of(ctx, cue_id):
    found = next(e["result"] for e in trace_of(ctx)["events"]
                 if e["event"] == "tool_result" and e["request"].get("tool") == "hints")
    return next((h for h in found["hints"] if h["id"] == cue_id), {})


def test_hints_compare_the_sentence_with_the_work_names_and_mark_their_source(tmp_path, monkeypatch):
    """RECORDS THE CURRENT RULE (name_match.near_names, not changed here): 起乐 (qi-le) is not a near sound of 绮罗
    (qi-luo) because the second finals differ, so the first sentence is not flagged; 奇罗 (qi-luo) is.  里央 is written
    in the first sentence as it stands, so it is not a differently written name."""
    monkeypatch.setattr(pb.mishearing, "load_pairs", lambda *_a: [])
    texts = ("其实起乐，比里央小很多很多", "而且奇罗一直没回消息")
    cpa = AgentCpa(final_rows(*texts), tools=[{"tool": "hints"}])
    ctx = agent_ctx(tmp_path, monkeypatch, banner_remote(), cpa, bcut=texts)
    saved_context(ctx, ["绮罗", "里央"])
    assert step._step3_body(ctx).ok
    assert hint_of(ctx, 1) == {}                                          # 起乐: no match under the present rule
    assert hint_of(ctx, 2)["near_names"] == [{"name": "绮罗", "spellings": ["绮罗"], "source": "work"}]
    assert name_match.near_names("其实起乐，比里央小很多很多", [("绮罗", ()), ("里央", ())]) == []
    assert name_match.near_names("而且奇罗一直没回消息", [("绮罗", ()), ("里央", ())]) == ["绮罗"]


def test_participant_hints_keep_their_format_and_win_over_a_work_name_with_the_same_spelling(tmp_path, monkeypatch):
    monkeypatch.setattr(pb.mishearing, "load_pairs", lambda *_a: [])
    texts = ("而且奇罗一直没回消息",)
    cpa = AgentCpa(final_rows(*texts), tools=[{"tool": "hints"}])
    ctx = agent_ctx(tmp_path, monkeypatch, banner_remote(), cpa, bcut=texts)
    ctx.session["participants"] = [["绮罗", "琪萝"]]
    saved_context(ctx, ["绮罗", "里央"])
    assert step._step3_body(ctx).ok
    assert hint_of(ctx, 1)["near_names"] == [{"name": "绮罗", "spellings": ["绮罗", "琪萝"]}]


# ---- 8. the work tool -------------------------------------------------------------------------
def tool_result(ctx, tool):
    return next(e["result"] for e in trace_of(ctx)["events"]
                if e["event"] == "tool_result" and e["request"].get("tool") == tool)


def test_the_work_tool_adds_the_character_table_of_the_subject_it_found(tmp_path, monkeypatch):
    cpa = AgentCpa(final_rows("结果"), tools=[{"tool": "work", "query": WORK}])
    ctx = agent_ctx(tmp_path, monkeypatch, banner_remote("{}"), cpa)
    assert step._step3_body(ctx).ok
    result = tool_result(ctx, "work")
    assert result["query"] == WORK and any(r["layer"] == "bangumi" and r["id"] == SUBJECT_ID for r in result["results"])
    assert {"name": "ナルセ・リオ", "names_cn": ["鸣濑·里央", "成濑·莉央"]} in result["characters"]
    assert "characters_error" not in result


def test_a_failing_character_request_leaves_the_work_tool_answering_with_the_subject(tmp_path, monkeypatch):
    cpa = AgentCpa(final_rows("结果"), tools=[{"tool": "work", "query": WORK}])
    ctx = agent_ctx(tmp_path, monkeypatch, banner_remote("{}"), cpa, http=Http(fail_characters=True))
    assert step._step3_body(ctx).ok
    result = tool_result(ctx, "work")
    assert any(r["id"] == SUBJECT_ID for r in result["results"])
    assert "characters" not in result and "characters down" in result["characters_error"]


def test_the_work_tool_adds_nothing_when_no_subject_is_the_query(tmp_path, monkeypatch):
    cpa = AgentCpa(final_rows("结果"), tools=[{"tool": "work", "query": "完全不相干"}])
    ctx = agent_ctx(tmp_path, monkeypatch, banner_remote("{}"), cpa)
    assert step._step3_body(ctx).ok
    assert "characters" not in tool_result(ctx, "work")


# ---- the media-host tool ----------------------------------------------------------------------
def load_tool(monkeypatch):
    remote_dir = str(ROOT / "assets/manual_lane/remote")
    monkeypatch.syspath_prepend(remote_dir)
    sys.modules.pop("pick_frame", None)
    spec = importlib.util.spec_from_file_location("read_banner_under_test", Path(remote_dir) / "read_banner.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_read_banner_writes_banner_json_with_the_raw_answer(tmp_path, monkeypatch):
    tool = load_tool(monkeypatch)
    monkeypatch.setattr(tool, "QUESTION_FILE", str(ROOT / "assets/manual_lane/prompts/screen_banner.md"))   # beside the tool once sent
    monkeypatch.setenv("CPA_BASE_URL", "http://cpa.invalid")
    monkeypatch.setenv("CPA_API_KEY", "k")
    monkeypatch.setattr(tool, "frame", lambda cut, t, path: "aW1n")
    asked = {}

    def ask(base, key, image, question, max_tokens=64, kind="clean", t=None):
        asked.update(base=base, image=image, question=question, max_tokens=max_tokens)
        tool.pick_frame.CALLS.append({"kind": kind, "t": t, "model": tool.pick_frame.MODEL, "rc": 0, "elapsed_s": 1.5,
                                      "usage": {"total_tokens": 9}})
        return FOUND_ANSWER

    monkeypatch.setattr(tool.pick_frame, "ask", ask)
    assert tool.main(["read_banner.py", "cut.mp4", str(tmp_path / "out"), "12.5"]) == 0
    doc = json.loads((tmp_path / "out/banner.json").read_text(encoding="utf-8"))
    assert doc == {"t": 12.5, "answer": FOUND_ANSWER, "model": tool.pick_frame.MODEL, "usage": {"total_tokens": 9},
                   "elapsed_s": 1.5, "rc": 0}
    assert asked["max_tokens"] == 200 and asked["image"] == "aW1n"
    assert asked["question"] == (ROOT / "assets/manual_lane/prompts/screen_banner.md").read_text(encoding="utf-8").strip()


def test_read_banner_fails_non_zero_with_the_reason_and_leaves_no_result(tmp_path, monkeypatch, capsys):
    tool = load_tool(monkeypatch)
    monkeypatch.setattr(tool, "QUESTION_FILE", str(ROOT / "assets/manual_lane/prompts/screen_banner.md"))   # beside the tool once sent
    monkeypatch.setenv("CPA_BASE_URL", "http://cpa.invalid")
    monkeypatch.setenv("CPA_API_KEY", "k")
    monkeypatch.setattr(tool, "frame", lambda cut, t, path: "aW1n")
    monkeypatch.setattr(tool.pick_frame, "ask", lambda *a, **k: (_ for _ in ()).throw(ValueError("answer not complete")))
    with pytest.raises(SystemExit) as stopped:
        tool.main(["read_banner.py", "cut.mp4", str(tmp_path / "out"), "1"])
    assert "answer not complete" in str(stopped.value) and stopped.value.code != 0
    assert not (tmp_path / "out/banner.json").exists()
