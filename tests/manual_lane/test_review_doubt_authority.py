"""Synthetic ordinary-review coverage for CPA's residual doubt disposition."""
from __future__ import annotations

import json
from pathlib import Path
import shutil

import pytest
from test_review_once import _CompleteCpa, _package, _srt

from src.autoslice.manual_lane import doubt_policy, speaker_v8, subtitle_track
from src.autoslice.manual_lane import stage_package as sp
from src.autoslice.manual_lane.runner import Step, StepOutcome, run_clip


@pytest.fixture(autouse=True)
def _isolate_legacy_disposition_from_post_review_listening(monkeypatch):
    """These cases exercise legacy disposition; wide-listening integration has its own runner tests."""
    monkeypatch.setattr(sp, "_finish_or_wide", lambda ctx, record, process:
                        sp._finish(ctx, cpa=record["cpa"], process=process))


class CaptureCpa(_CompleteCpa):
    def __init__(self, responses):
        super().__init__(responses)
        self.details = []

    def ask(self, **kwargs):
        self.details.append({k: kwargs[k] for k in ('call_id', 'model', 'effort')})
        return super().ask(**kwargs)


def _answer(decisions, *, fixes=None):
    return json.dumps({'fixes': fixes or {}, 'holds': [], 'notes': [],
                       'doubt_decisions': decisions, 'review_md': '合成复审'}, ensure_ascii=False)


def _runner(tmp_path, monkeypatch, response, *, text='紫伞借给北风', doubt_id='d493'):
    final = _srt([(0, 2, text), (3, 5, '木舟等雨停')])
    speaker = _srt([(0, 2, f'[李豆沙] {text}'), (3, 5, '[李豆沙] 木舟等雨停')])
    row = {'id': doubt_id, 'cue_id': 1, 'start_s': 0.0, 'end_s': 2.0, 'state': 'ADOPT',
           'final_text': text, 'current': text, 'confidence': '低', 'affects_meaning': True,
           'basis': '先前的合成判断', 'source': 'CPA'}
    ctx, _, fmr = _package(tmp_path, monkeypatch, response, final=final, doubts=[row], speaker=speaker)
    cpa = CaptureCpa([response])
    ctx.adapters.cpa = cpa
    counts = {'burn': 0, 'tags': 0, 'package': 0}

    def stage(name):
        def run(c):
            counts[name] += 1
            if name == 'package':
                shutil.copyfile(c.clip_dir / 'subs/final.srt', c.clip_dir / 'pkg/final.srt')
                member = sp._member(c, files={}, cover_lines=[], dedup={}, cut_local=Path('/synthetic/cut.mp4'),
                                    duration_ms=5000, selection_hook='合成看点')
                (c.clip_dir / 'pkg/member.json').write_text(json.dumps(member, ensure_ascii=False), encoding='utf-8')
            product = c.clip_dir / f'{name}.synthetic-product'
            product.write_text(name, encoding='utf-8')
            return StepOutcome(True, products={name: str(product)})
        return run

    steps = [Step(5, 'burn', 'burn', stage('burn')), Step(7, 'tags', 'tags', stage('tags')),
             Step(8, 'package', 'package', stage('package')), Step(9, 'review', 'review', sp.step9_review)]
    return ctx, steps, cpa, counts, fmr


@pytest.mark.parametrize(('text', 'doubt_id', 'confidence', 'affects', 'held'), [
    ('纸燕藏在玻璃后', 'd314', '低', False, False),
    ('灯塔向晚霞眨眼', 'd827', '高', True, False),
    ('蜡笔越过白石桥', 'd562', '低', True, True),
])
def test_varied_unseen_doubts_follow_the_same_decision_path(tmp_path, monkeypatch, text, doubt_id, confidence, affects,
                                                           held):
    answer = _answer([{'id': doubt_id, 'confidence': confidence, 'affects_meaning': affects, 'reason': '合成证据'}])
    ctx, steps, cpa, counts, _ = _runner(tmp_path, monkeypatch, answer, text=text, doubt_id=doubt_id)
    before = (ctx.clip_dir / 'subs/final.srt').read_bytes()
    report = run_clip(ctx, steps)
    row = _row(ctx)
    assert report.stopped_at is None and doubt_policy.is_hold(row) is held and row['id'] == doubt_id
    assert (ctx.clip_dir / 'subs/final.srt').read_bytes() == before and cpa.calls == ['review']
    assert counts['burn'] == counts['tags'] == 1


def test_unchanged_text_explicit_disposition_regrades_real_member_without_burn_or_tags(tmp_path, monkeypatch):
    response = _answer([{'id': 'd493', 'confidence': '低', 'affects_meaning': False,
                         'reason': '保留的写法不改变事件指向'}])
    ctx, steps, cpa, counts, _ = _runner(tmp_path, monkeypatch, response)
    before = [(ctx.clip_dir / p).read_bytes() for p in ('subs/final.srt', 'speaker/speaker.srt')]
    report = run_clip(ctx, steps)
    row = json.loads((ctx.clip_dir / 'subs/doubts.json').read_text(encoding='utf-8'))[0]
    member = json.loads((ctx.clip_dir / 'pkg/member.json').read_text(encoding='utf-8'))
    assert report.stopped_at is None
    assert not doubt_policy.is_hold(row) and row['confidence'] == '低' and row['affects_meaning'] is False
    assert member['doubts'][0]['grade'] == 'B'
    assert report.rewinds == ['package', 'review'] and counts == {'burn': 1, 'tags': 1, 'package': 2}
    assert [(ctx.clip_dir / p).read_bytes() for p in ('subs/final.srt', 'speaker/speaker.srt')] == before
    assert cpa.calls == ['review'] and cpa.details[0]['effort'] == 'high'


def _legacy(tmp_path, monkeypatch, *, fixes=None, held=True, text='紫伞借给北风'):
    legacy = json.dumps({'fixes': fixes or {}, 'holds': [], 'notes': [{'item': '1', 'text': '旧时未判断'}],
                         'review_md': '旧记录未判遗留疑点'}, ensure_ascii=False)
    ctx, steps, cpa, counts, fmr = _runner(tmp_path, monkeypatch, legacy, text=text)
    if not held:
        path = ctx.clip_dir / 'subs/doubts.json'
        rows = json.loads(path.read_text(encoding='utf-8'))
        rows[0]['confidence'] = '高'
        path.write_text(json.dumps(rows, ensure_ascii=False), encoding='utf-8')
    with monkeypatch.context() as old:
        old.setattr(sp, 'review_prompt', lambda _ctx: '合成旧复审：原有待定项本轮不判。')
        report = run_clip(ctx, steps)
    assert report.stopped_at is None and cpa.calls == ['review']
    event = dict(ctx.state.data['review']['cpa'][0])
    files = [Path(event['cache_path']), Path(event['file'] + '.prompt.txt'), Path(event['file'] + '.completion.txt')]
    return ctx, steps, counts, fmr, event, {p: p.read_bytes() for p in files}


def _focused(affects=False, *, fixes=None):
    return _answer([{'id': 'd493', 'confidence': '低', 'affects_meaning': affects,
                     'reason': '根据合成材料判断余下歧义'}], fixes=fixes)


def _row(ctx):
    return json.loads((ctx.clip_dir / 'subs/doubts.json').read_text(encoding='utf-8'))[0]


def _remove_report(ctx):
    (ctx.clip_dir / 'pkg/REVIEW.md').unlink()


def test_legacy_only_asks_focused_once_and_reuses_both_terminals_after_material_change(tmp_path, monkeypatch):
    ctx, steps, counts, _, main, original = _legacy(tmp_path, monkeypatch)
    before = [(ctx.clip_dir / p).read_bytes() for p in ('subs/final.srt', 'speaker/speaker.srt')]
    _remove_report(ctx)
    cpa = CaptureCpa([_focused()])
    ctx.adapters.cpa = cpa
    report = run_clip(ctx, steps)
    assert report.stopped_at is None and report.rewinds == ['package', 'review']
    assert counts == {'burn': 1, 'tags': 1, 'package': 2}
    assert cpa.calls == ['review-held'] and cpa.details == [
        {'call_id': 'review-held', 'model': 'gpt-6.1-sol', 'effort': 'high'}]
    assert not doubt_policy.is_hold(_row(ctx)) and _row(ctx)['affects_meaning'] is False
    assert [(ctx.clip_dir / p).read_bytes() for p in ('subs/final.srt', 'speaker/speaker.srt')] == before
    record = ctx.state.data['review']
    assert record['held_ids'] == ['d493'] and [c['call_id'] for c in record['cpa']] == ['review', 'review-held']
    assert record['cpa'][1]['purpose'] == 'review'
    focused = dict(record['cpa'][1])
    focused_files = [Path(focused['cache_path']), Path(focused['file'] + '.prompt.txt'),
                     Path(focused['file'] + '.completion.txt')]
    preserved = {p: p.read_bytes() for p in focused_files}
    text = (ctx.clip_dir / 'pkg/REVIEW.md').read_text(encoding='utf-8')
    assert '既有复审（历史记录' in text and '遗留疑点处置（仅指定疑点）' in text
    assert '旧记录未判遗留疑点' in text
    assert '（既有复审历史）旧时未判断' in text and '最终疑点判断 d493' in text
    assert '根据合成材料判断余下歧义' in text
    _remove_report(ctx)
    (ctx.clip_dir / 'pkg/description.txt').write_text('变化的合成上下文', encoding='utf-8')
    no_more = CaptureCpa([])
    ctx.adapters.cpa = no_more
    again = run_clip(ctx, steps)
    assert again.stopped_at is None and again.rewinds == [] and no_more.calls == []
    assert [c['call_id'] for c in ctx.state.data['review']['cpa']] == ['review', 'review-held']
    assert ctx.state.data['review']['cpa'][0]['cache_path'] == main['cache_path']
    assert all(path.read_bytes() == content for path, content in {**original, **preserved}.items())
    assert counts == {'burn': 1, 'tags': 1, 'package': 2}


def test_legacy_no_hold_and_fresh_present_empty_list_never_ask_focused(tmp_path, monkeypatch):
    ctx, steps, cpa, _, _ = _runner(tmp_path / 'fresh', monkeypatch, _answer([]))
    assert run_clip(ctx, steps).stopped_at is None and cpa.calls == ['review']
    _remove_report(ctx)
    never = CaptureCpa([])
    ctx.adapters.cpa = never
    assert run_clip(ctx, steps).stopped_at is None and never.calls == []
    assert doubt_policy.is_hold(_row(ctx))

    ctx, steps, _, _, _, _ = _legacy(tmp_path / 'legacy', monkeypatch, held=False)
    _remove_report(ctx)
    never = CaptureCpa([])
    ctx.adapters.cpa = never
    assert run_clip(ctx, steps).stopped_at is None and never.calls == []


def test_explicit_still_meaningful_uncertainty_keeps_hold(tmp_path, monkeypatch):
    ctx, steps, cpa, counts, fmr = _runner(tmp_path, monkeypatch, _focused(affects=True))
    result = run_clip(ctx, steps)
    assert result.stopped_at is None and result.rewinds == []
    assert doubt_policy.is_hold(_row(ctx)) and _row(ctx)['confidence'] == '低'
    assert counts == {'burn': 1, 'tags': 1, 'package': 1} and cpa.calls == ['review']
    assert not fmr and json.loads((ctx.clip_dir / 'pkg/member.json').read_text())['doubts'][0]['grade'] == 'D'


def test_text_fix_with_explicit_residual_hold_cannot_be_promoted_implicitly(tmp_path, monkeypatch):
    replacement = '紫伞留在南坡'
    response = _focused(affects=True, fixes={'subtitles': [{'cue_id': 1, 'text': replacement}]})
    ctx, steps, cpa, counts, _ = _runner(tmp_path, monkeypatch, response)
    result = run_clip(ctx, steps)
    row = _row(ctx)
    assert result.stopped_at is None and result.rewinds == ['burn', 'tags', 'package', 'review']
    assert row['confidence'] == '低' and row['affects_meaning'] is True and doubt_policy.is_hold(row)
    assert replacement in (ctx.clip_dir / 'subs/final.srt').read_text(encoding='utf-8')
    assert replacement in (ctx.clip_dir / 'speaker/speaker.srt').read_text(encoding='utf-8')
    assert json.loads((ctx.clip_dir / 'pkg/member.json').read_text())['doubts'][0]['grade'] == 'D'
    assert counts == {'burn': 2, 'tags': 2, 'package': 2} and cpa.calls == ['review']


def test_invalid_or_missing_decisions_do_not_release_hold_even_after_a_fix(tmp_path, monkeypatch):
    cases = [None, 'not-a-list', [{'id': 'other', 'confidence': '高', 'affects_meaning': False, 'reason': '合成依据'}],
             [{'id': ['d493'], 'confidence': '高', 'affects_meaning': False, 'reason': '合成依据'}],
             [{'id': 'd493', 'confidence': '中', 'affects_meaning': False, 'reason': '合成依据'}],
             [{'id': 'd493', 'confidence': '高', 'affects_meaning': 'false', 'reason': '合成依据'}],
             [{'id': 'd493', 'confidence': '高', 'affects_meaning': False, 'reason': ''}],
             [{'id': 'd493', 'confidence': '高', 'affects_meaning': False, 'reason': '甲'},
              {'id': 'd493', 'confidence': '低', 'affects_meaning': True, 'reason': '乙'}]]
    for index, decisions in enumerate(cases):
        value = {'fixes': {'subtitles': [{'cue_id': 1, 'text': '紫伞藏在南坡'}]}, 'holds': [], 'review_md': '合成'}
        if decisions is not None:
            value['doubt_decisions'] = decisions
        ctx, steps, cpa, _, _ = _runner(tmp_path / str(index), monkeypatch, json.dumps(value, ensure_ascii=False))
        result = run_clip(ctx, steps)
        assert result.stopped_at is None and doubt_policy.is_hold(_row(ctx)) and cpa.calls == ['review']
        if decisions is not None:
            assert ctx.state.data['review']['skipped']


def test_recorded_focused_missing_or_bad_cache_stops_without_new_request_or_deletion(tmp_path, monkeypatch):
    for case in ('missing', 'invalid', 'incomplete', 'unparseable'):
        ctx, steps, _, _, _, main_original = _legacy(tmp_path / case, monkeypatch)
        _remove_report(ctx)
        cpa = CaptureCpa([_focused()])
        ctx.adapters.cpa = cpa
        assert run_clip(ctx, steps).stopped_at is None and cpa.calls == ['review-held']
        event = next(c for c in ctx.state.data['review']['cpa'] if c['call_id'] == 'review-held')
        cache = Path(event['cache_path'])
        if case == 'missing':
            cache.unlink()
            expected = None
        elif case == 'invalid':
            cache.write_text('{broken', encoding='utf-8')
            expected = cache.read_bytes()
        else:
            raw = json.loads(cache.read_text(encoding='utf-8'))
            if case == 'incomplete':
                raw['complete'] = False
            else:
                raw['response_text'] = '{broken'
            cache.write_text(json.dumps(raw, ensure_ascii=False), encoding='utf-8')
            expected = cache.read_bytes()
        _remove_report(ctx)
        never = CaptureCpa([])
        ctx.adapters.cpa = never
        result = run_clip(ctx, steps)
        assert result.stopped_at == 9 and result.reason in {'CPA_CALL_FAILED', 'CPA_RESPONSE_UNPARSEABLE'}
        assert never.calls == [] and (cache.read_bytes() if cache.exists() else None) == expected
        assert all(p.read_bytes() == data for p, data in main_original.items())


def test_focused_scope_rejects_unrelated_fix_and_decision(tmp_path, monkeypatch):
    ctx, steps, _, _, _, _ = _legacy(tmp_path, monkeypatch)
    _remove_report(ctx)
    answer = json.loads(_focused())
    answer['fixes'] = {'subtitles': [{'cue_id': 2, 'text': '无关句被改写'}], 'title': '无关标题'}
    answer['doubt_decisions'].append({'id': 'unrelated', 'confidence': '高', 'affects_meaning': False, 'reason': '越界'})
    cpa = CaptureCpa([json.dumps(answer, ensure_ascii=False)])
    ctx.adapters.cpa = cpa
    before = (ctx.clip_dir / 'subs/final.srt').read_bytes()
    result = run_clip(ctx, steps)
    assert result.stopped_at is None and result.rewinds == ['package', 'review']
    assert (ctx.clip_dir / 'subs/final.srt').read_bytes() == before
    assert not doubt_policy.is_hold(_row(ctx)) and cpa.calls == ['review-held']
    assert any('outside' in item for item in ctx.state.data['review']['skipped'])


def test_fresh_answer_missing_field_does_not_trigger_focus_during_its_own_rewind(tmp_path, monkeypatch):
    value = {'fixes': {'subtitles': [{'cue_id': 1, 'text': '紫伞交给南风'}]}, 'holds': [], 'review_md': '合成'}
    ctx, steps, cpa, _, _ = _runner(tmp_path, monkeypatch, json.dumps(value, ensure_ascii=False))
    result = run_clip(ctx, steps)
    assert result.stopped_at is None and result.rewinds == ['burn', 'tags', 'package', 'review']
    assert cpa.calls == ['review'] and doubt_policy.is_hold(_row(ctx))
    assert 'fresh_rebuild' not in ctx.state.data['review']


def test_fresh_missing_field_becomes_a_missing_disposition_on_later_recovery(tmp_path, monkeypatch):
    answer = json.dumps({'fixes': {}, 'holds': [], 'review_md': '未列出最终疑点决定'}, ensure_ascii=False)
    ctx, steps, cpa, _, _ = _runner(tmp_path, monkeypatch, answer)
    assert run_clip(ctx, steps).stopped_at is None and cpa.calls == ['review']
    _remove_report(ctx)
    focused = CaptureCpa([_focused()])
    ctx.adapters.cpa = focused
    result = run_clip(ctx, steps)
    assert result.stopped_at is None and focused.calls == ['review-held'] and not doubt_policy.is_hold(_row(ctx))


def test_legacy_interrupted_rewind_resumes_missing_disposition_once(tmp_path, monkeypatch):
    value = {'fixes': {'subtitles': [{'cue_id': 1, 'text': '紫伞交给西风'}]}, 'holds': [], 'review_md': '旧答复'}
    ctx, steps, cpa, counts, _ = _runner(tmp_path, monkeypatch, json.dumps(value, ensure_ascii=False))
    cpa.responses.append(_focused())
    burn = steps[0]

    def interrupt_second_burn(c):
        if counts['burn']:
            raise RuntimeError('模拟中断烧录')
        return burn.run(c)

    original_save = ctx.state.save

    def save_as_legacy():
        # The old writer had no in-flight fresh marker; DONE and rewind are still produced by the real runner.
        record = ctx.state.data.get('review')
        if record:
            record.pop('fresh_rebuild', None)
        original_save()

    with monkeypatch.context() as old:
        old.setattr(ctx.state, 'save', save_as_legacy)
        first = run_clip(ctx, [Step(5, 'burn', 'burn', interrupt_second_burn), *steps[1:]])
    assert first.stopped_at == 5 and ctx.state.data['rewind']['by'] == 'review'
    assert ctx.state.data['review']['rebuild'] is True and cpa.calls == ['review']
    resumed = run_clip(ctx, steps)
    assert resumed.stopped_at is None and resumed.rewinds == ['package', 'review']
    assert cpa.calls == ['review', 'review-held'] and not doubt_policy.is_hold(_row(ctx))
    assert [c['call_id'] for c in ctx.state.data['review']['cpa']] == ['review', 'review-held']


@pytest.mark.parametrize('after_terminal', [False, True])
@pytest.mark.parametrize('affects_meaning', [False, True])
def test_legacy_rebuild_interrupted_again_before_focused_adoption(tmp_path, monkeypatch, after_terminal, affects_meaning):
    old = {'fixes': {'subtitles': [{'cue_id': 1, 'text': '紫伞交给西风'}]}, 'holds': [], 'review_md': '旧答复'}
    ctx, steps, cpa, counts, _ = _runner(tmp_path, monkeypatch, json.dumps(old, ensure_ascii=False))
    cpa.responses.append(_focused(affects=affects_meaning))
    burn = steps[0]

    def interrupt_burn(c):
        if counts['burn']:
            raise RuntimeError('合成旧包重建中断')
        return burn.run(c)

    save = ctx.state.save

    def legacy_save():
        record = ctx.state.data.get('review')
        if record:
            record.pop('fresh_rebuild', None)
        save()

    with monkeypatch.context() as legacy:
        legacy.setattr(ctx.state, 'save', legacy_save)
        first = run_clip(ctx, [Step(5, 'burn', 'burn', interrupt_burn), *steps[1:]])
    assert first.stopped_at == 5 and ctx.state.data['review']['rebuild'] is True
    ask = sp._ask_cpa

    def interrupt_focus(*args, **kwargs):
        if after_terminal:
            ask(*args, **kwargs)
        raise RuntimeError('合成遗留处置采用前再次中断')

    with monkeypatch.context() as interrupted:
        interrupted.setattr(sp, '_ask_cpa', interrupt_focus)
        second = run_clip(ctx, steps)
    assert second.stopped_at == 9
    persisted = json.loads(ctx.state.path.read_text(encoding='utf-8'))['review']
    assert persisted['rebuild'] is False
    event = next(c for c in persisted['cpa'] if c['call_id'] == 'review-held')
    assert Path(event['cache_path']).exists() is after_terminal
    assert cpa.calls == (['review', 'review-held'] if after_terminal else ['review'])
    final = run_clip(ctx, steps)
    assert final.stopped_at is None and cpa.calls == ['review', 'review-held']
    assert doubt_policy.is_hold(_row(ctx)) is affects_meaning
    assert _row(ctx)['affects_meaning'] is affects_meaning
    assert ctx.state.data['review']['applied']['decisions'] == 1


def test_selected_track_text_can_be_explicitly_settled_from_unresolved_state(tmp_path, monkeypatch):
    response = _answer([{'id': 'd493', 'confidence': '高', 'affects_meaning': True, 'reason': '现稿有充分依据'}])
    ctx, steps, cpa, _, _ = _runner(tmp_path, monkeypatch, response)
    path = ctx.clip_dir / 'subs/doubts.json'
    rows = json.loads(path.read_text(encoding='utf-8'))
    rows[0]['state'], rows[0]['final_text'] = 'UNRESOLVED', ''
    path.write_text(json.dumps(rows, ensure_ascii=False), encoding='utf-8')
    before = (ctx.clip_dir / 'subs/final.srt').read_bytes()
    result = run_clip(ctx, steps)
    row = _row(ctx)
    assert result.stopped_at is None and result.rewinds == ['package', 'review']
    assert row['state'] == 'FIT_CONTEXT' and row['final_text'] == '紫伞借给北风'
    assert not doubt_policy.is_hold(row) and (ctx.clip_dir / 'subs/final.srt').read_bytes() == before
    assert cpa.calls == ['review']


def test_deciding_one_of_two_doubts_does_not_release_the_other(tmp_path, monkeypatch):
    response = _focused()
    ctx, steps, cpa, _, _ = _runner(tmp_path, monkeypatch, response)
    path = ctx.clip_dir / 'subs/doubts.json'
    rows = json.loads(path.read_text(encoding='utf-8'))
    rows.append({'id': 'd982', 'start_s': 3.0, 'end_s': 5.0, 'state': 'ADOPT', 'final_text': '木舟等雨停',
                 'confidence': '低', 'affects_meaning': True, 'source': 'CPA', 'basis': '另一处合成疑点'})
    path.write_text(json.dumps(rows, ensure_ascii=False), encoding='utf-8')
    result = run_clip(ctx, steps)
    after = {r['id']: r for r in json.loads(path.read_text(encoding='utf-8'))}
    assert result.stopped_at is None and not doubt_policy.is_hold(after['d493']) and doubt_policy.is_hold(after['d982'])
    assert cpa.calls == ['review'] and '未封存' in ctx.state.step('review')['note']


def test_later_focused_fix_wins_without_replaying_superseded_text_on_recovery(tmp_path, monkeypatch):
    ctx, steps, counts, _, _, _ = _legacy(
        tmp_path, monkeypatch, fixes={'subtitles': [{'cue_id': 1, 'text': '紫伞交给北风'}]})
    _remove_report(ctx)
    later = '紫伞交给山岚'
    cpa = CaptureCpa([_focused(fixes={'subtitles': [{'cue_id': 1, 'text': later}]})])
    ctx.adapters.cpa = cpa
    first = run_clip(ctx, steps)
    assert first.stopped_at is None and cpa.calls == ['review-held']
    assert later in (ctx.clip_dir / 'subs/final.srt').read_text(encoding='utf-8')
    counts_before = dict(counts)
    _remove_report(ctx)
    never = CaptureCpa([])
    ctx.adapters.cpa = never
    again = run_clip(ctx, steps)
    assert again.stopped_at is None and again.rewinds == [] and never.calls == []
    assert later in (ctx.clip_dir / 'subs/final.srt').read_text(encoding='utf-8')
    assert counts == counts_before


def test_terminal_survives_interruption_before_focused_event_update(tmp_path, monkeypatch):
    ctx, steps, _, _, _, main_original = _legacy(tmp_path, monkeypatch)
    _remove_report(ctx)
    cpa = CaptureCpa([_focused()])
    ctx.adapters.cpa = cpa
    ask = sp._ask_cpa

    def interrupt_after_terminal(*args, **kwargs):
        result = ask(*args, **kwargs)
        if args[1] == 'review-held':
            raise RuntimeError('模拟终态已落盘但记录尚未更新')
        return result

    with monkeypatch.context() as broken:
        broken.setattr(sp, '_ask_cpa', interrupt_after_terminal)
        first = run_clip(ctx, steps)
    assert first.stopped_at == 9 and cpa.calls == ['review-held']
    event = next(c for c in ctx.state.data['review']['cpa'] if c['call_id'] == 'review-held')
    cache = Path(event['cache_path'])
    saved = cache.read_bytes()
    (ctx.clip_dir / 'pkg/description.txt').write_text('中断后材料变化', encoding='utf-8')
    never = CaptureCpa([])
    ctx.adapters.cpa = never
    resumed = run_clip(ctx, steps)
    assert resumed.stopped_at is None and never.calls == [] and not doubt_policy.is_hold(_row(ctx))
    assert cache.read_bytes() == saved and all(p.read_bytes() == v for p, v in main_original.items())
    assert ctx.state.data['review']['cpa'][1]['cached'] is True


def test_pre_dispatch_interruption_resumes_same_question_once(tmp_path, monkeypatch):
    ctx, steps, _, _, _, main_original = _legacy(tmp_path, monkeypatch)
    _remove_report(ctx)
    cpa = CaptureCpa([_focused()])
    ctx.adapters.cpa = cpa
    prompts = []

    def interrupt_before_adapter(_ctx, call_id, prompt_text, **_kwargs):
        assert call_id == 'review-held'
        prompts.append(prompt_text)
        raise RuntimeError('模拟准备完成、provider 零调用前中断')

    with monkeypatch.context() as broken:
        broken.setattr(sp, '_ask_cpa', interrupt_before_adapter)
        first = run_clip(ctx, steps)
    assert first.stopped_at == 9 and cpa.calls == []
    event = next(c for c in ctx.state.data['review']['cpa'] if c['call_id'] == 'review-held')
    assert not Path(event['cache_path']).exists()
    prompt_path = Path(event['file'] + '.prompt.txt')
    saved_prompt = prompt_path.read_text(encoding='utf-8') if prompt_path.exists() else None
    (ctx.clip_dir / 'pkg/description.txt').write_text('后续材料已变化', encoding='utf-8')
    resumed = run_clip(ctx, steps)
    assert resumed.stopped_at is None and cpa.calls == ['review-held'] and not doubt_policy.is_hold(_row(ctx))
    assert saved_prompt == prompts[0] == cpa.prompts[0]
    assert cpa.details == [{'call_id': 'review-held', 'model': 'gpt-6.1-sol', 'effort': 'high'}]
    assert ctx.state.data['review']['held_ids'] == ['d493']
    assert all(p.read_bytes() == v for p, v in main_original.items())


def test_corrupt_focused_target_binding_stops_without_resending(tmp_path, monkeypatch):
    ctx, steps, _, _, _, _ = _legacy(tmp_path, monkeypatch)
    _remove_report(ctx)
    ctx.adapters.cpa = CaptureCpa([_focused()])
    assert run_clip(ctx, steps).stopped_at is None
    event = next(c for c in ctx.state.data['review']['cpa'] if c['call_id'] == 'review-held')
    cache = Path(event['cache_path'])
    original = cache.read_bytes()
    ctx.state.data['review']['held_ids'] = ['another-id']
    ctx.state.save()
    _remove_report(ctx)
    never = CaptureCpa([])
    ctx.adapters.cpa = never
    result = run_clip(ctx, steps)
    assert result.stopped_at == 9 and result.reason == 'CPA_CALL_FAILED'
    assert cache.read_bytes() == original and never.calls == []


def _synthetic_track(ctx, *, text=None, speaker=None):
    track = subtitle_track.read_track(ctx.clip_dir)
    if text is not None:
        track[0]['text'] = text
    if speaker is not None:
        track[0]['speaker'] = speaker
    subtitle_track.write_track(ctx.clip_dir, track, speakers=True)


def _count_shared_writer(monkeypatch):
    writes = []
    writer = sp._write_edits

    def counted(*args, **kwargs):
        writes.append(1)
        return writer(*args, **kwargs)

    monkeypatch.setattr(sp, '_write_edits', counted)
    return writes


def test_invalid_later_subtitle_keeps_earlier_valid_fix_and_replays_without_call(tmp_path, monkeypatch):
    current, earlier = '我在门口点灯', '我在窗边点灯'
    ctx, steps, _, _, _, _ = _legacy(tmp_path, monkeypatch, text=current,
                                     fixes={'subtitles': [{'cue_id': 1, 'text': earlier}]})
    _synthetic_track(ctx, text=current)
    _remove_report(ctx)
    cpa = CaptureCpa([_focused(fixes={'subtitles': [{'cue_id': 1, 'text': ''}]})])
    ctx.adapters.cpa = cpa
    writes = _count_shared_writer(monkeypatch)
    first = run_clip(ctx, steps)
    assert first.stopped_at is None and cpa.calls == ['review-held']
    assert subtitle_track.read_track(ctx.clip_dir)[0]['text'] == earlier and writes == [1]
    assert any('subtitle fix not applied' in x for x in ctx.state.data['review']['skipped'])
    _remove_report(ctx)
    never = CaptureCpa([])
    ctx.adapters.cpa = never
    again = run_clip(ctx, steps)
    assert again.stopped_at is None and again.rewinds == [] and never.calls == [] and writes == [1]
    assert subtitle_track.read_track(ctx.clip_dir)[0]['text'] == earlier


def test_valid_later_subtitle_choosing_current_cancels_earlier_without_burn(tmp_path, monkeypatch):
    current, earlier = '我在桥边看月亮', '我在桥边看星星'
    ctx, steps, counts, _, _, _ = _legacy(tmp_path, monkeypatch, text=current,
                                          fixes={'subtitles': [{'cue_id': 1, 'text': earlier}]})
    _synthetic_track(ctx, text=current)
    _remove_report(ctx)
    cpa = CaptureCpa([_focused(fixes={'subtitles': [{'cue_id': 1, 'text': current}]})])
    ctx.adapters.cpa = cpa
    writes = _count_shared_writer(monkeypatch)
    before = dict(counts)
    report = run_clip(ctx, steps)
    assert report.stopped_at is None and report.rewinds == ['package', 'review']
    assert subtitle_track.read_track(ctx.clip_dir)[0]['text'] == current and writes == []
    assert counts['burn'] == before['burn'] and counts['tags'] == before['tags']
    assert cpa.calls == ['review-held']


@pytest.mark.parametrize('bad_fix', [
    {'cue_id': 1, 'speaker': '未知身份'},
    {'cue_id': 1, 'speaker': speaker_v8.GUEST_LABEL, 'evidence': '猜测', 'quote': '我是木偶'},
])
def test_invalid_later_speaker_keeps_earlier_valid_fix_and_replays(tmp_path, monkeypatch, bad_fix):
    text = '我是木偶修理师'
    main_fix = {'cue_id': 1, 'speaker': speaker_v8.GUEST_LABEL, 'evidence': '自称', 'quote': '我是木偶'}
    ctx, steps, _, _, _, _ = _legacy(tmp_path, monkeypatch, text=text, fixes={'speakers': [main_fix]})
    _synthetic_track(ctx, speaker=speaker_v8.HOST)
    _remove_report(ctx)
    cpa = CaptureCpa([_focused(fixes={'speakers': [bad_fix]})])
    ctx.adapters.cpa = cpa
    writes = _count_shared_writer(monkeypatch)
    first = run_clip(ctx, steps)
    assert first.stopped_at is None and cpa.calls == ['review-held']
    assert subtitle_track.read_track(ctx.clip_dir)[0]['speaker'] == speaker_v8.GUEST_LABEL and writes == [1]
    assert any('speaker fix not applied' in x for x in ctx.state.data['review']['skipped'])
    _remove_report(ctx)
    never = CaptureCpa([])
    ctx.adapters.cpa = never
    again = run_clip(ctx, steps)
    assert again.stopped_at is None and again.rewinds == [] and never.calls == [] and writes == [1]
    assert subtitle_track.read_track(ctx.clip_dir)[0]['speaker'] == speaker_v8.GUEST_LABEL


def test_valid_later_speaker_choosing_current_cancels_earlier_without_burn(tmp_path, monkeypatch):
    text = '我是竹林的画师'
    main_fix = {'cue_id': 1, 'speaker': speaker_v8.GUEST_LABEL, 'evidence': '自称', 'quote': '我是竹林'}
    ctx, steps, counts, _, _, _ = _legacy(tmp_path, monkeypatch, text=text, fixes={'speakers': [main_fix]})
    _synthetic_track(ctx, speaker=speaker_v8.HOST)
    _remove_report(ctx)
    cpa = CaptureCpa([_focused(fixes={'speakers': [{'cue_id': 1, 'speaker': speaker_v8.HOST}]})])
    ctx.adapters.cpa = cpa
    writes = _count_shared_writer(monkeypatch)
    before = dict(counts)
    report = run_clip(ctx, steps)
    assert report.stopped_at is None and report.rewinds == ['package', 'review']
    assert subtitle_track.read_track(ctx.clip_dir)[0]['speaker'] == speaker_v8.HOST and writes == []
    assert counts['burn'] == before['burn'] and counts['tags'] == before['tags']
    assert cpa.calls == ['review-held']


def test_interrupted_unknown_attempt_keeps_failure_evidence_when_resuming_same_prompt(tmp_path, monkeypatch):
    ctx, steps, _, _, _, main_original = _legacy(tmp_path, monkeypatch)
    _remove_report(ctx)

    class UnknownAttempt:
        def __init__(self):
            self.prompts = []

        def ask(self, **kwargs):
            self.prompts.append(kwargs['prompt_text'])
            raise RuntimeError('模拟 provider 已进入但结果未知')

    first_adapter = UnknownAttempt()
    ctx.adapters.cpa = first_adapter
    first = run_clip(ctx, steps)
    assert first.stopped_at == 9 and len(first_adapter.prompts) == 1
    original_process = (ctx.clip_dir / 'PROCESS.md').read_text(encoding='utf-8')
    assert '模拟 provider 已进入但结果未知' in original_process
    (ctx.clip_dir / 'pkg/description.txt').write_text('中断后另有材料', encoding='utf-8')
    resumed_adapter = CaptureCpa([_focused()])
    ctx.adapters.cpa = resumed_adapter
    resumed = run_clip(ctx, steps)
    assert resumed.stopped_at is None and resumed_adapter.calls == ['review-held']
    assert resumed_adapter.prompts == first_adapter.prompts and not doubt_policy.is_hold(_row(ctx))
    assert original_process in (ctx.clip_dir / 'PROCESS.md').read_text(encoding='utf-8')
    assert all(p.read_bytes() == data for p, data in main_original.items())


def test_returned_exhausted_failure_is_preserved_and_not_restarted(tmp_path, monkeypatch):
    ctx, steps, _, _, _, main_original = _legacy(tmp_path, monkeypatch)
    _remove_report(ctx)

    class ExhaustedCpa:
        def __init__(self):
            self.calls = []

        def ask(self, **kwargs):
            self.calls.append(kwargs['call_id'])
            return {'call_id': kwargs['call_id'], 'model': kwargs['model'], 'effort': kwargs['effort'],
                    'rc': 1, 'complete': False, 'attempts': 4, 'reason': 'CPA_ANSWER_INCOMPLETE',
                    'response_text': '', 'stderr_tail': '模拟既有 provider 重试耗尽'}

    failed_adapter = ExhaustedCpa()
    ctx.adapters.cpa = failed_adapter
    first = run_clip(ctx, steps)
    assert first.stopped_at == 9 and first.reason == 'CPA_CALL_FAILED' and failed_adapter.calls == ['review-held']
    event = next(c for c in ctx.state.data['review']['cpa'] if c['call_id'] == 'review-held')
    assert event['rc'] == 1 and event['attempts'] == 4 and event['reason'] == 'CPA_ANSWER_INCOMPLETE'
    never = CaptureCpa([])
    ctx.adapters.cpa = never
    resumed = run_clip(ctx, steps)
    assert resumed.stopped_at == 9 and resumed.reason == 'CPA_CALL_FAILED' and never.calls == []
    event_after = next(c for c in ctx.state.data['review']['cpa'] if c['call_id'] == 'review-held')
    assert event_after == event and '模拟既有 provider 重试耗尽' in (ctx.clip_dir / 'PROCESS.md').read_text()
    assert all(p.read_bytes() == data for p, data in main_original.items())
