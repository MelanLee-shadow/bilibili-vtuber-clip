"""Step 6 per the lidousha-title-cover skill: the title names the participants on stage, the punchline is Lidousha's
own reaction, every candidate passes the swap test (recorded in candidates.json), the cover frame is chosen away from
unrelated pages, and the danmaku strip is blurred."""

from __future__ import annotations

import json

from fakes import FakeCpa, FakeRemote, fake_cover, make_ctx, srt

from src.autoslice.manual_lane import stage_impl as si
from src.autoslice.manual_lane.adapters import RemoteResult

RD = "/r/clip"
NAMES = ["李豆沙", "雾深Girimi"]


def _cand(title, on_stage, *, person="她越长大越讨厌看那种有规矩的", holds=False):
    return {"title": title, "on_stage": on_stage, "punchline_person": person,
            "swap_check": {"holds_with_other_host": holds, "reason": "换人就不成立" if not holds else "换人也成立"}}


def test_a_title_that_leaves_out_a_participant_on_stage_is_rejected():
    chk = si.title_checks(_cand("【李豆沙】她和弹幕吵起来了", ["李豆沙", "雾深Girimi"]), NAMES, srt(3))
    assert not chk["ok"] and any("未点名出场者：雾深Girimi" in r for r in chk["reasons"])
    assert any("前缀未写出场者" in r for r in chk["reasons"])


def test_a_named_guest_must_also_be_in_the_prefix():
    ok = si.title_checks(_cand("【李豆沙×雾深Girimi】她和雾深Girimi抢着看切片", ["李豆沙", "雾深Girimi"]), NAMES, srt(3))
    assert ok["ok"], ok["reasons"]
    no_prefix = si.title_checks(_cand("【李豆沙】她和雾深Girimi抢着看切片", ["李豆沙", "雾深Girimi"]), NAMES, srt(3))
    assert any("前缀未写出场者" in r for r in no_prefix["reasons"])


def test_a_title_that_still_holds_with_another_host_is_rejected():
    chk = si.title_checks(_cand("【李豆沙】她半分钟后真的破防了", ["李豆沙"], holds=True), NAMES, srt(3))
    assert not chk["ok"] and any("换主播自检未通过" in r for r in chk["reasons"])


def test_a_punchline_not_on_lidousha_herself_is_rejected():
    chk = si.title_checks(_cand("【李豆沙】漫画里的人狼杀真的把人灭口了", ["李豆沙"], person=""), NAMES, srt(3))
    assert not chk["ok"] and any("笑点未落在李豆沙本人身上" in r for r in chk["reasons"])


def test_on_stage_names_must_come_from_the_participant_list():
    chk = si.title_checks(_cand("【李豆沙×路人甲】她和路人甲聊天", ["李豆沙", "路人甲"]), NAMES, srt(3))
    assert not chk["ok"] and any("不在参与者名单" in r for r in chk["reasons"])


def test_a_banned_word_still_fails_the_candidate():
    chk = si.title_checks(_cand("【李豆沙】这段直接把人笑哭", ["李豆沙"]), NAMES, srt(3))
    assert not chk["ok"] and any("禁词" in r for r in chk["reasons"])


def _title_ctx(tmp_path, candidates, *, frames=None, render_script=None):
    def script(name, body, remote):
        if name == "s6-frame":
            if frames is not None:
                remote.files[f"{RD}/cover/frames.json"] = json.dumps(frames).encode()
            return RemoteResult(0, "", "")
        if name.startswith("s6-cover-job"):
            fake_cover(remote, "/r/clip")
            return RemoteResult(0, "", "")
        return RemoteResult(0, "", "")
    remote = FakeRemote(on_script=script)
    cpa = FakeCpa([json.dumps({"candidates": candidates, "recommended": 0, "cover_lines": ["甲", "乙"]},
                              ensure_ascii=False)])
    ctx = make_ctx(tmp_path, remote=remote, cpa=cpa, session={
        "recording_date": "2026-10-24", "speaker_mode": "uniform_host", "enablement_evidence": None,
        "participants": [["李豆沙", "小李"], ["雾深Girimi", "雾深"]], "own_uploader_mids": [1]})
    (ctx.clip_dir / "subs").mkdir(parents=True, exist_ok=True)
    (ctx.clip_dir / "subs" / "final.srt").write_text(srt(3), encoding="utf-8")
    return ctx, cpa, remote


def test_the_title_prompt_carries_the_participant_list(tmp_path):
    ctx, cpa, _ = _title_ctx(tmp_path, [_cand("【李豆沙】她半分钟后真的破防了", ["李豆沙"])])
    si.step6_title_cover(ctx)
    assert "本场参与者名单（正式名）：李豆沙、雾深Girimi" in cpa.title_prompts[0]


def test_the_first_candidate_that_fails_a_check_is_skipped_and_the_reasons_are_recorded(tmp_path):
    ctx, _, _ = _title_ctx(tmp_path, [
        _cand("【李豆沙】她和弹幕吵起来了", ["李豆沙", "雾深Girimi"]),
        _cand("【李豆沙×雾深Girimi】越长大越讨厌看有规矩的，她说雾深Girimi也一样", ["李豆沙", "雾深Girimi"]),
    ])
    out = si.step6_title_cover(ctx)
    assert out.ok, out.note
    cands = json.loads((ctx.clip_dir / "title" / "candidates.json").read_text(encoding="utf-8"))
    assert cands["chosen"]["title"].startswith("【李豆沙×雾深Girimi】")
    assert cands["candidates"][0]["checks"]["ok"] is False and cands["candidates"][1]["checks"]["ok"] is True


def test_cover_spec_blurs_the_danmaku_strip(tmp_path):
    ctx, _, remote = _title_ctx(tmp_path, [_cand("【李豆沙×雾深Girimi】她和雾深Girimi抢着看切片",
                                                    ["李豆沙", "雾深Girimi"])], frames={"chosen": 12.0, "checked": []})
    assert si.step6_title_cover(ctx).ok
    spec = json.loads(remote.files[f"{RD}/cover/spec.json"].decode("utf-8"))
    assert spec["blur"] == [[1700, 700, 1830, 1060]]      # the fixed strip, minus the host body (not the old wide box)


def test_the_cover_frame_chosen_by_cpa_vision_is_the_one_rendered(tmp_path):
    ctx, _, remote = _title_ctx(tmp_path, [_cand("【李豆沙×雾深Girimi】她和雾深Girimi抢着看切片",
                                                    ["李豆沙", "雾深Girimi"])], frames={"chosen": 12.0, "checked": []})
    out = si.step6_title_cover(ctx)
    assert out.ok and any("CPA 看图选帧 12.00s" in line for line in out.process)
    cover_script = next(body for name, body in remote.scripts if name == "s6-cover-frame")
    assert "-ss 12.000" in cover_script


def test_without_a_clean_frame_the_default_moment_is_kept_and_recorded(tmp_path):
    ctx, _, remote = _title_ctx(tmp_path, [_cand("【李豆沙×雾深Girimi】她和雾深Girimi抢着看切片",
                                                    ["李豆沙", "雾深Girimi"])],
                                frames={"chosen": None, "checked": [{"t": 1.0, "verdict": "NOT_CLEAN"}]})
    out = si.step6_title_cover(ctx)
    assert out.ok and any("保持" in line and "记录" in line for line in out.process)
