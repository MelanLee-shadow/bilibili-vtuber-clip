"""Cover text too long for the renderer: CPA shortens it and the renderer is called again; the final lines are kept."""

from __future__ import annotations

import json

from fakes import UNDERSTANDING_CALLS, FakeCpa, FakeRemote, cover_verdict, fake_cover, make_ctx, srt

from src.autoslice.manual_lane import stage_impl as si
from src.autoslice.manual_lane.adapters import RemoteResult

RD = "/r/clip"
TITLE = json.dumps({"candidates": [{"title": "【李豆沙】半分钟后她破防了", "on_stage": ["李豆沙"], "punchline_person": "她破防了", "swap_check": {"holds_with_other_host": False, "reason": "测试"}}], "recommended": 0,
                    "cover_lines": ["关进QQ空间会饿死", "真的很长"]}, ensure_ascii=False)


def _render_script(renders):
    def script(name, body, remote):
        if name == "s6-cover":
            rc, note = renders.pop(0)
            if rc == 0:
                fake_cover(remote, "/r/clip")
            return RemoteResult(rc, note, "")
        return RemoteResult(0, "", "")
    return script


def _ctx(tmp_path, renders, answers):
    remote = FakeRemote(on_script=_render_script(renders))
    cpa = FakeCpa([TITLE] + answers)
    ctx = make_ctx(tmp_path, remote=remote, cpa=cpa)
    (ctx.clip_dir / "subs").mkdir(parents=True, exist_ok=True)
    (ctx.clip_dir / "subs" / "final.srt").write_text(srt(3), encoding="utf-8")
    return ctx, cpa


def test_too_long_cover_text_is_shortened_by_cpa_and_rendered_again(tmp_path):
    ctx, cpa = _ctx(tmp_path,
                    renders=[(23, "最多只能放到 114px（下限 120px）：缩短文字"), (0, ""), (0, "")],
                    answers=[json.dumps({"cover_lines": ["不会饿死"]}, ensure_ascii=False)])
    out = si.step6_title_cover(ctx)
    assert out.ok, out.note
    assert cpa.calls == [*UNDERSTANDING_CALLS, "title", "title-judge-r0-1", "title-judge-r0-2", "cover-brief", "title-cover-retry-1"]
    cands = json.loads((ctx.clip_dir / "title" / "candidates.json").read_text(encoding="utf-8"))
    assert cands["cover_lines"] == ["不会饿死"]
    assert any("封面文字缩短（第 1 次）" in line for line in out.process)


def test_retries_are_bounded_and_the_last_failure_stops_the_step(tmp_path):
    ctx, cpa = _ctx(tmp_path,
                    renders=[(23, "放不下"), (23, "放不下"), (23, "还是放不下")],
                    answers=[json.dumps({"cover_lines": ["甲"]}), json.dumps({"cover_lines": ["乙"]})])
    out = si.step6_title_cover(ctx)
    assert not out.ok and out.reason == "COVER_RENDER_FAILED_RC23"
    assert cpa.calls == [*UNDERSTANDING_CALLS, "title", "title-judge-r0-1", "title-judge-r0-2", "cover-brief", "title-cover-retry-1", "title-cover-retry-2"]


def test_shortening_cannot_reintroduce_an_anonymous_label_as_a_name(tmp_path):
    ctx, cpa = _ctx(tmp_path, renders=[(23, "放不下"), (0, "")],
                    answers=[json.dumps({"cover_lines": ["让连线放心"]}, ensure_ascii=False)])
    ctx.session["participants"] = [["李豆沙"], ["连线"]]
    out = si.step6_title_cover(ctx)
    assert not out.ok and out.reason == "COVER_RENDER_FAILED_RC23"
    assert "title-cover-retry-1" in cpa.calls
    assert not any("封面文字缩短" in line for line in out.process)


def test_shortening_preserves_paid_calls_from_the_failed_render(tmp_path):
    renders = 0

    def script(name, _body, remote):
        nonlocal renders
        if name != "s6-cover":
            return RemoteResult(0, "", "")
        renders += 1
        routes = json.loads(remote.files[f"{RD}/cover/job.json"])["routes"]
        verdict = cover_verdict(face=(1380, 470, 1600, 650)) if renders <= 3 else cover_verdict()
        calls = ([{"kind": "image_edit", "candidate": route, "rc": 0,
                   "selected_model": "gpt-image-2.5", "reported_model": "UNKNOWN"}
                  for route in routes if route in ("cutout", "ai_sticker", "ai_scene")]
                 + [{"kind": "vision", "candidate": route, "model": "gpt-6.1-sol", "rc": 0}
                    for route in routes])
        fake_cover(remote, RD, rows=[(route, verdict) for route in routes], calls=calls)
        return RemoteResult(23, "文字放不下", "") if renders == 3 else RemoteResult(0, "", "")

    remote = FakeRemote(on_script=script)
    ctx = make_ctx(tmp_path, remote=remote,
                   cpa=FakeCpa([TITLE, json.dumps({"cover_lines": ["不会饿死"]}, ensure_ascii=False)]))
    (ctx.clip_dir / "subs").mkdir(parents=True, exist_ok=True)
    (ctx.clip_dir / "subs/final.srt").write_text(srt(3), encoding="utf-8")
    out = si.step6_title_cover(ctx)
    assert out.ok, (out.reason, out.note)
    ids = [str(call.get("call_id")) for call in out.cpa]
    assert sum(call.startswith("cover-image-") for call in ids) == 2
    assert sum(call.startswith("cover-judge-") for call in ids) >= 4
    assert sum(line.startswith("封面第 ") and "付费调用" in line for line in out.process) == 4
    assert next(i for i, line in enumerate(out.process) if "封面第 3 轮" in line) < next(
        i for i, line in enumerate(out.process) if "封面文字缩短" in line)
