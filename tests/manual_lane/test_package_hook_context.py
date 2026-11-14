"""The package and reviewer use the same existing, provenance-labelled hook without changing its inputs."""

from __future__ import annotations

import json
import subprocess

import pytest
from test_fmr_calls import _prepared

from src.autoslice.manual_lane import stage_package as sp


def _case(tmp_path, monkeypatch, hook, candidates):
    ctx, cpa = _prepared(tmp_path, cpa_review={"fixes": {}, "holds": [], "review_md": "review"})
    ctx.state.data["clip"]["hook"] = hook
    ctx.state.data["clip"]["selection_file"] = ""
    ctx.state.save()
    candidate_path = ctx.clip_dir / "title/candidates.json"
    candidate_path.write_text(json.dumps(candidates, ensure_ascii=False, indent=2), encoding="utf-8")
    response_path = ctx.clip_dir / "title/raw-response.json"
    response_path.write_text('{"synthetic": "original CPA response bytes"}\n', encoding="utf-8")
    (ctx.clip_dir / "cover/cover.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    originals = {p: p.read_bytes() for p in (ctx.state.path, candidate_path, response_path)}
    actual_fmr = sp._fmr
    calls = []

    def fmr(*args):
        calls.append(args[0])
        if args[0] == "init":
            return actual_fmr(*args)
        # Init is real; the synthetic media are not used for a media check or sealing.
        return subprocess.CompletedProcess(args, 1 if args[0] == "attest" else 0, "", "")

    monkeypatch.setattr(sp, "_fmr", fmr)
    monkeypatch.setattr(sp, "description_text", lambda title: "简介")
    return ctx, cpa, calls, originals


@pytest.mark.parametrize(
    ("hook", "punchline", "expected", "origin"),
    [
        (" \t", "  小熊把雨伞借给月亮  ", "  小熊把雨伞借给月亮  ", "CPA 已选候选 chosen.punchline"),
        ("", "  纸鹤写下 {title_rules} 和 {pronoun_rule}  ",
         "  纸鹤写下 {title_rules} 和 {pronoun_rule}  ", "CPA 已选候选 chosen.punchline"),
        ("  原始看点：纸船停在楼梯  ", "另一条备选", "  原始看点：纸船停在楼梯  ", "原始选片 clip.hook"),
    ],
)
def test_real_fmr_init_and_review_share_existing_context(tmp_path, monkeypatch, hook, punchline, expected, origin):
    candidates = {"cover_lines": ["甲", "乙"], "chosen": {"punchline": punchline},
                  "candidates": [{"punchline": "未选中的另一句"}]}
    ctx, cpa, calls, originals = _case(tmp_path, monkeypatch, hook, candidates)
    before_clip = dict(ctx.state.data["clip"])
    out = sp.step8_package(ctx)
    assert out.ok, (out.reason, out.note)
    assert calls == ["init", "check"]
    assert cpa.calls == []
    member = json.loads((ctx.clip_dir / "pkg/clip.member.json").read_text(encoding="utf-8"))
    assert member["selection_hook"] == expected
    assert origin in "\n".join(out.process)
    assert all(p.read_bytes() == b for p, b in originals.items())

    result = sp.step9_review(ctx)
    assert result.ok
    assert cpa.calls == ["review"]  # FakeCpa only; no model or audio is invoked.
    assert expected in cpa.prompts[0]
    assert origin in cpa.prompts[0]
    assert ctx.state.data["clip"] == before_clip
    assert all(p.read_bytes() == b for p, b in originals.items() if p != ctx.state.path)


@pytest.mark.parametrize(
    "chosen",
    [None, {}, {"punchline": ""}, {"punchline": " \n"}, {"punchline": 42},
     {"punchline": ["not a string"]}, "not an object"],
)
def test_missing_or_invalid_chosen_punchline_keeps_real_fmr_refusal(tmp_path, monkeypatch, chosen):
    candidates = {"cover_lines": ["甲", "乙"], "candidates": [{"punchline": "未选中的话不能使用"}]}
    if chosen is not None:
        candidates["chosen"] = chosen
    ctx, cpa, calls, originals = _case(tmp_path, monkeypatch, "", candidates)
    out = sp.step8_package(ctx)
    assert not out.ok and out.reason == "FMR_INIT_REFUSED"
    assert "FMR_MEMBER_SELECTION_HOOK_MISSING" in out.note
    assert calls == ["init"]
    assert cpa.calls == []
    assert all(p.read_bytes() == b for p, b in originals.items())
