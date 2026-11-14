"""A future text review receives existing cover evidence without pretending to view the image."""

from __future__ import annotations

import json

import pytest
from test_review_once import _package, _review, _srt

from src.autoslice.manual_lane import stage_package as sp


def _ctx(tmp_path, monkeypatch):
    return _package(tmp_path, monkeypatch, _review(verdict="PASS"), final=_srt([(0, 2, "新合成台词")]), doubts=[])


def test_anonymous_speaker_remains_a_role_in_main_and_focused_review(tmp_path, monkeypatch):
    ctx, _, _ = _ctx(tmp_path, monkeypatch)
    ctx.session['participants'] = [['李豆沙'], ['连线']]
    for prompt in (sp.review_prompt(ctx), sp.review_held_prompt(ctx, ['d001'])):
        assert '参与者：李豆沙。连线方身份未知，标签不是名字。' in prompt
        assert '参与者：李豆沙、连线' not in prompt
    skipped = []
    assert not sp._title_fix(ctx, '【李豆沙×连线】她听完后愣住了', skipped, [])
    assert skipped and 'talk_partner_unresolved' in skipped[0]
    skipped.clear()
    assert not sp._title_fix(ctx, '【李豆沙】她先让连线放心，后来却突然愣住了', skipped, [])
    assert skipped and 'placeholder_identity' in skipped[0]


def test_review_receives_only_selected_existing_image_verdict_and_exact_cover_lines(tmp_path, monkeypatch):
    ctx, cpa, _ = _ctx(tmp_path, monkeypatch)
    lines = ["纸鸢{title_rules}", "云边"]
    selected = {"hook_fit": {"score": 0, "reason": "遮住了线索 {pronoun_rule}"},
                "people": {"pass": True, "reason": "只看到一位"}, "no_fabrication": {"pass": True}}
    candidates = ctx.clip_dir / "title/candidates.json"
    candidates.write_text(json.dumps({"cover_lines": lines}, ensure_ascii=False), encoding="utf-8")
    choose = ctx.clip_dir / "cover/cand/choose.json"
    choose.parent.mkdir(parents=True)
    choose.write_text(json.dumps({"chosen": "kite", "rows": [
        {"id": "other", "verdict": {"hook_fit": {"reason": "UNSELECTED_PRIVATE_REASON"}}},
        {"id": "kite", "verdict": selected},
    ]}, ensure_ascii=False), encoding="utf-8")
    originals = {p: p.read_bytes() for p in (candidates, choose)}

    assert sp.step9_review(ctx).ok
    prompt = cpa.prompts[0]
    assert "## 封面字" in prompt and all(line in prompt for line in lines)
    assert "既有 CPA 图像判读" in prompt and "本次文字复审未独立看图" in prompt
    assert all(value in prompt for value in ("kite", "遮住了线索 {pronoun_rule}", "只看到一位", "no_fabrication"))
    image_section = prompt.split("## 选中封面", 1)[1].split("\n", 1)[1].split("\n\n##", 1)[0]
    assert json.loads(image_section) == {"chosen": "kite", "verdict": selected}
    assert "UNSELECTED_PRIVATE_REASON" not in prompt
    assert all(p.read_bytes() == data for p, data in originals.items())


@pytest.mark.parametrize("choice", [None, "{broken", {"chosen": "absent", "rows": [{"id": "other", "verdict": {"ok": True}}]},
                                    {"chosen": "same", "rows": [{"id": "same", "verdict": {"ok": True}},
                                                                {"id": "same", "verdict": {"ok": False}}]}])
def test_old_or_unusable_cover_evidence_is_reported_missing_without_blocking_review(tmp_path, monkeypatch, choice):
    ctx, cpa, _ = _ctx(tmp_path, monkeypatch)
    if choice is not None:
        choose = ctx.clip_dir / "cover/cand/choose.json"
        choose.parent.mkdir(parents=True)
        choose.write_text(choice if isinstance(choice, str) else json.dumps(choice), encoding="utf-8")
    assert sp.step9_review(ctx).ok
    prompt = cpa.prompts[0]
    assert "## 封面字" in prompt and "## 选中封面" in prompt
    assert prompt.count("未提供") >= 2
    assert "本次文字复审未独立看图" in prompt


def test_focused_and_main_review_share_facts_and_insert_literal_evidence_last(tmp_path, monkeypatch):
    row = {'id': 'd731', 'start_s': 0.0, 'end_s': 2.0, 'state': 'FIT_CONTEXT', 'final_text': '星灯{session}',
           'confidence': '低', 'affects_meaning': True, 'source': 'CPA', 'basis': '先前依据{hold_rule}',
           'readings': {'source_a': '旧读法{materials}', 'source_b': '另一读法'},
           'candidates': [{'text': '旧读法{materials}', 'source': 'source_a', 'sources': ['source_a']}]}
    final = _srt([(0, 2, '星灯{session}')])
    ctx, _, _ = _package(tmp_path, monkeypatch, _review(verdict='PASS'), final=final, doubts=[row])
    ctx.session['participants'] = [['李豆沙{hold_rule}']]
    candidates = ctx.clip_dir / 'title/candidates.json'
    candidates.write_text(json.dumps({'cover_lines': ['风帆{title_rules}']}, ensure_ascii=False), encoding='utf-8')
    choose = ctx.clip_dir / 'cover/cand/choose.json'
    choose.parent.mkdir(parents=True)
    choose.write_text(json.dumps({'chosen': 'sail', 'rows': [
        {'id': 'other', 'verdict': {'hook_fit': {'reason': 'UNSELECTED'}}},
        {'id': 'sail', 'verdict': {'hook_fit': {'score': 0, 'reason': '偏离{pronoun_rule}'}}},
    ]}, ensure_ascii=False), encoding='utf-8')
    main = sp.review_prompt(ctx)
    focused = sp.review_held_prompt(ctx, ['d731'])
    for prompt in (main, focused):
        for text in ('李豆沙{hold_rule}', '星灯{session}', '先前依据{hold_rule}', '旧读法{materials}', 'source_a',
                     '风帆{title_rules}', '偏离{pronoun_rule}', '本次文字复审未独立看图'):
            assert text in prompt
        assert 'UNSELECTED' not in prompt
    assert '"d731"' in focused and '只处置' in focused and '不重新全片找错' in focused
