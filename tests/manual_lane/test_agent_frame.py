"""The subtitle agent's `frame` tool (agent_frame.py): a fake media host answers the remote reader (look_at_frames.py)
in its own output shape.  Nothing reaches the network or ssh."""
import importlib.util
import json
import re
import sys

import pytest

from agent_fakes import AgentCpa
from fakes import FakeRemote, make_ctx
from src.autoslice.manual_lane import agent_frame, playbook_step3 as pb, step3_subtitles as step
from src.autoslice.manual_lane.adapters import RemoteResult
from src.autoslice.manual_lane.step_io import vision_models

LOOK_USAGE = {"input_tokens": 900, "output_tokens": 30, "total_tokens": 930}
ASK = {"tool": "frame", "t": 1.0, "question": "她头上戴着什么"}


def look_remote(answer="头上戴着一副很大的墨镜", *, seen=True, ok=True):
    def on_script(name, body, remote):
        if not name.startswith("s4-screen-look"):
            return RemoteResult(0, "", "")
        frames_json, out = re.search(r"look_at_frames\.py \S+ \S+ (\S+) (\S+)'", body).groups()
        request = json.loads(remote.files[frames_json])
        rows = [dict(f, ok=ok, **({"seen": seen, "answer": answer} if ok else {"error": "CalledProcessError"}))
                for f in request["frames"]]
        model, _, _ = vision_models()
        doc = {"question": request["question"], "model": model, "frames": rows,
               "calls": [{"kind": "look", "t": f["t"], "model": model, "effort": "low", "rc": 0,
                          "usage": LOOK_USAGE} for f in request["frames"]]}
        remote.files[out] = json.dumps(doc, ensure_ascii=False).encode()
        return RemoteResult(0, "", "")
    return FakeRemote(on_script=on_script)


def looks(remote):
    return [name for name, _ in remote.scripts if name.startswith("s4-screen-look")]


def agent_ctx(tmp_path, remote, cpa, monkeypatch):
    ctx = make_ctx(tmp_path, remote=remote, cpa=cpa)
    ctx.session["participants"] = [["single speaker"]]
    for name in ("bcut", "jianying"):
        path = ctx.clip_dir / f"asr/{name}.srt"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("1\n00:00:00,000 --> 00:00:02,000\n是我那么惊呆了\n\n")
    monkeypatch.setenv("MANUAL_LANE_ADJUDICATE_MODE", "C")
    return ctx


def rows():
    return [{"id": "d001", "state": "ADOPT", "final_text": "是我大墨镜大爷", "confidence": "高", "affects_meaning": False}]


def trace_of(ctx):
    return json.loads((ctx.clip_dir / "subs/agent-trace.json").read_text(encoding="utf-8"))


def frame_result(ctx):
    return next(e["result"] for e in trace_of(ctx)["events"]
                if e["event"] == "tool_result" and e["request"].get("tool") == "frame")


def test_the_agent_sees_what_the_picture_shows_and_the_reading_is_paid_like_its_own_calls(tmp_path, monkeypatch):
    remote = look_remote()
    ctx = agent_ctx(tmp_path, remote, AgentCpa(rows(), tools=[ASK]), monkeypatch)
    out = step._step3_body(ctx)
    assert out.ok
    result = frame_result(ctx)
    assert result["frames"] == [{"t": 1.0, "seen": True, "answer": "头上戴着一副很大的墨镜"}]
    assert result["question"] == "她头上戴着什么" and "也会看错" in result["note"]
    assert trace_of(ctx)["total_tokens"] == 3 * 10 + LOOK_USAGE["total_tokens"]
    assert [c["purpose"] for c in out.cpa].count(agent_frame.PURPOSE) == 1
    sent = next(v for k, v in remote.files.items() if k.endswith("-frames.json") and "look" in k)
    assert json.loads(sent) == {"frames": [{"id": "f0", "t": 1.0}], "question": "她头上戴着什么"}


def test_a_resumed_conversation_counts_the_reading_again_and_does_not_look_twice(tmp_path, monkeypatch):
    original = pb._Agent.finish
    remote = look_remote()
    cpa = AgentCpa(rows(), tools=[ASK])
    ctx = agent_ctx(tmp_path, remote, cpa, monkeypatch)
    monkeypatch.setattr(pb._Agent, "finish", lambda *_args: (_ for _ in ()).throw(RuntimeError("interrupted")))
    with pytest.raises(RuntimeError):
        step._step3_body(ctx)
    monkeypatch.setattr(pb._Agent, "finish", original)
    out = step._step3_body(ctx)
    assert out.ok and len(cpa.calls) == 3 and len(looks(remote)) == 1
    assert trace_of(ctx)["total_tokens"] == 3 * 10 + LOOK_USAGE["total_tokens"]
    assert [c["purpose"] for c in out.cpa].count(agent_frame.PURPOSE) == 1


@pytest.mark.parametrize("ask, error", [({"tool": "frame", "t": 1.0}, "question"),
                                        ({"tool": "frame", "times": [0.5, 1, 1.5, 1.9], "question": "q"}, "1–3"),
                                        ({"tool": "frame", "t": 99.0, "question": "q"}, "in the cut"),
                                        ({"tool": "frame", "t": "1", "question": "q"}, "in the cut")])
def test_a_request_the_tool_cannot_answer_is_a_tool_error_and_nothing_is_looked_at(tmp_path, monkeypatch, ask, error):
    remote = look_remote()
    ctx = agent_ctx(tmp_path, remote, AgentCpa(rows(), tools=[ask]), monkeypatch)
    assert step._step3_body(ctx).ok
    assert error in frame_result(ctx)["error"] and looks(remote) == []


def test_a_picture_that_cannot_be_read_is_unknown_not_absent_and_its_cost_is_kept(tmp_path, monkeypatch):
    remote = look_remote(ok=False)
    ctx = agent_ctx(tmp_path, remote, AgentCpa(rows(), tools=[ASK]), monkeypatch)
    assert step._step3_body(ctx).ok
    result = frame_result(ctx)
    assert "结果未知" in result["error"] and result["cpa"][0]["usage"] == LOOK_USAGE
    assert trace_of(ctx)["total_tokens"] == 3 * 10 + LOOK_USAGE["total_tokens"]


def test_the_same_question_at_the_same_moment_is_read_once(tmp_path, monkeypatch):
    remote = look_remote()
    ctx = agent_ctx(tmp_path, remote, AgentCpa(rows()), monkeypatch)
    paid = []
    first = agent_frame.look(ctx, ASK, 2.0, "", paid.append)
    again = agent_frame.look(ctx, dict(ASK, question=" 她头上戴着什么 "), 2.0, "", paid.append)
    assert len(looks(remote)) == 1 and len(paid) == 1
    assert again["frames"] == first["frames"] and "cpa" not in again


@pytest.mark.parametrize("mixed", [False, True])
def test_a_fallback_reading_is_not_reused_as_a_primary_reading(tmp_path, monkeypatch, mixed):
    remote = look_remote()
    ctx = agent_ctx(tmp_path, remote, AgentCpa(rows()), monkeypatch)
    paid = []
    agent_frame.look(ctx, ASK, 2.0, "", paid.append)
    primary, fallback, _ = vision_models()
    cache, = (ctx.clip_dir / "speaker").glob("look-*-readings.json")
    cached = json.loads(cache.read_text(encoding="utf-8"))
    cached["model"] = primary if mixed else fallback
    cached["calls"][0]["model"] = fallback
    if mixed:
        cached["calls"][0]["fallback_from"] = primary
    cache.write_text(json.dumps(cached), encoding="utf-8")

    recovered = agent_frame.look(ctx, ASK, 2.0, "", paid.append)
    again = agent_frame.look(ctx, ASK, 2.0, "", paid.append)
    assert len(looks(remote)) == 2 and len(paid) == 2
    assert recovered["cpa"][0]["model"] == primary and "cpa" not in again


def test_the_remote_reader_keeps_only_an_answer_that_says_whether_it_was_seen():
    spec = importlib.util.spec_from_file_location("look_at_frames", agent_frame.SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(agent_frame.SCRIPT.parent))
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path.remove(str(agent_frame.SCRIPT.parent))
    assert module.read_answer('```json\n{"seen": true, "answer": " 大墨镜 "}\n```') == {"seen": True, "answer": "大墨镜"}
    assert module.read_answer('{"seen": "yes", "answer": "x"}') is None and module.read_answer("看不出") is None
    assert "{question}" not in module.WRAP.format(question="q") and "问题：q" in module.WRAP.format(question="q")


def test_visual_reference_hint_leads_to_frame_reading_with_sticker_captions_and_character(tmp_path, monkeypatch):
    sticker_desc = "画面左上角贴着戴带护目镜军帽的黑发双马尾Q版角色表情包，竖排配字「没有和你们混熟的打算」「我要一个人去…」"
    remote = look_remote(sticker_desc)
    tools = [
        {"tool": "hints"},
        {"tool": "frame", "t": 1.0, "question": "画面上展示的表情包角色和上面的文字是什么"},
    ]
    cpa = AgentCpa(
        [{"id": "d001", "state": "ADOPT", "final_text": "我发个这个", "confidence": "高", "affects_meaning": False}],
        tools=tools,
    )
    ctx = make_ctx(tmp_path, remote=remote, cpa=cpa)
    ctx.session["participants"] = [["single speaker"]]
    for name in ("bcut", "jianying"):
        path = ctx.clip_dir / f"asr/{name}.srt"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("1\n00:00:00,000 --> 00:00:02,000\n我发个这个\n\n", encoding="utf-8")
    monkeypatch.setenv("MANUAL_LANE_ADJUDICATE_MODE", "C")
    assert step._step3_body(ctx).ok
    assert "各来源一致时也看该刻画面" in cpa.prompts[0]
    hint = next(
        e["result"]["hints"][0]
        for e in trace_of(ctx)["events"]
        if e["event"] == "tool_result" and e["request"].get("tool") == "hints"
    )
    assert hint["visual_reference"]["t"] == 1.0
    result = frame_result(ctx)
    assert result["frames"] == [{"t": 1.0, "seen": True, "answer": sticker_desc}]
