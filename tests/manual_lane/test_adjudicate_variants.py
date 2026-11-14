"""The two adjudication variants compared in round 2 (TASK-5 item 2): A keeps only the witness-disagreeing cues and
runs one pass; B adjudicates every cue but keeps a witness-agreed reading unless CPA gives a reason."""

from __future__ import annotations

import json

import pytest
from test_steps import _final_text, _listen_records, _subs_ctx

from src.autoslice.manual_lane import normalize
from src.autoslice.manual_lane import stage_impl as si


def test_mode_must_be_one_of_the_known_variants(monkeypatch):
    monkeypatch.setenv("MANUAL_LANE_ADJUDICATE_MODE", "Z")
    with pytest.raises(ValueError):
        si.adjudicate_mode()


def test_variant_a_sends_only_the_disagreeing_cues_and_runs_one_pass(tmp_path, monkeypatch):
    monkeypatch.setenv("MANUAL_LANE_ADJUDICATE_MODE", "A")
    records = _listen_records(("C000", "38m", "句子甲"))          # cue 1 disagrees; cues 2 and 3 agree with the text
    ctx, cpa = _subs_ctx(tmp_path, records, [{"id": "d001", "state": "ADOPT", "final_text": "句子甲",
                                              "affects_meaning": True, "reason_key": "同音", "confidence": "高"}])
    out = si.step3_subtitles(ctx)
    assert out.ok, out.note
    assert sorted(cpa.calls) == ["adjudicate-p1-1"]                 # one pass, one batch
    assert not any('"d002"' in p or '"d003"' in p for p in cpa.prompts)
    assert _final_text(ctx) == "句子甲"
    assert _final_text(ctx, 2) == "句子2"   # an undisputed cue keeps its text


def test_variant_b_has_no_code_guard_cpa_choice_stands_and_the_rules_live_in_the_prompt():
    # the constraints are written in the prompt only: the code does not overturn CPA's answer (TASK-5, coordinator)
    assert not hasattr(si, "_guard_agreed")
    assert "证人一致的读法默认保留" in si.STRICT_RULES and "逐字保留" in si.STRICT_RULES


def test_the_strict_rules_are_only_in_variant_b_prompts(tmp_path, monkeypatch):
    monkeypatch.setenv("MANUAL_LANE_ADJUDICATE_MODE", "full")
    assert "严格规则" not in si._render("subtitle_adjudicate.md", doubts="[]", context="", participants="李豆沙",
                                        confusions="")
    assert "严格规则" in si._render("subtitle_adjudicate.md", doubts="[]", context="", participants="李豆沙",
                                    confusions="", strict_rules=si.STRICT_RULES)


def test_laughter_written_as_a_sound_is_removed_and_a_pure_laugh_leaves_nothing():
    assert normalize.strip_laughter("哈哈哈哈哈哈哈。乱讲") == "乱讲"
    assert normalize.strip_laughter("哈哈哈") == ""
    assert normalize.strip_laughter("真的吗？ 啊。") == "真的吗？ 啊。"   # 啊 is kept: it is speech


def test_the_production_default_is_variant_c(monkeypatch):
    # U119: production listens only the doubts (C); A and B listen every cue and are reference modes only.  The suite's
    # conftest pins "full" for the older tests; without the variable the production default applies.
    monkeypatch.delenv("MANUAL_LANE_ADJUDICATE_MODE", raising=False)
    assert si.DEFAULT_ADJUDICATE_MODE == "C" and si.adjudicate_mode() == "C"


def test_low_tier_bare_string_lines_become_the_window_reading(tmp_path):
    # Regression: the Low tier returns lines as bare strings (no start_s).  They must not crash the step and must
    # become the window's reading for its cue.
    from test_steps import _subs_ctx
    records = [{"win": "C000", "model": si.M38["name"], "slug": "38m", "status": "OK", "lines": ["句子甲"]}]
    ctx, _ = _subs_ctx(tmp_path, records, [{"id": "d001", "state": "KEEP", "final_text": "", "confidence": "高",
                                            "affects_meaning": False}])
    out = si.step3_subtitles(ctx)
    assert out.ok, out.note
    doubt = json.loads((ctx.clip_dir / "subs/doubts.json").read_text(encoding="utf-8"))[0]
    assert doubt["readings"].get("audio:38m@P0") == "句子甲"
