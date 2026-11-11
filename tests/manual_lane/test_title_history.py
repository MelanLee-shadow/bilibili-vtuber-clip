"""Ordinary title requests preserve authorship and consume previous batch choices."""
import json

import pytest
from fakes import FakeCpa, FakeRemote, fake_title_skill, make_ctx, srt
from src.autoslice.manual_lane import step6_title, title_authority, title_history
from src.autoslice.manual_lane.runner import StepOutcome
from src.autoslice.manual_lane.adapters import RemoteResult


def answer(body):
    return json.dumps({'candidates':[{'title_body':body,'on_stage':['李豆沙'],
        'punchline_person':'她自己说的反应','swap_check':{'holds_with_other_host':False}}],
        'recommended':0,'cover_lines':['她嘴硬了']},ensure_ascii=False)


def context(tmp_path, cid, body):
    cpa=FakeCpa([answer(body)])
    ctx=make_ctx(tmp_path/cid, remote=FakeRemote(), cpa=cpa)
    ctx.state.data['clip']['cid']=cid
    ctx.state.path=tmp_path/'state'/f'{cid}.json'
    ctx.state.save()
    ctx.clip_dir=tmp_path/'clips'/cid
    (ctx.clip_dir/'subs').mkdir(parents=True)
    (ctx.clip_dir/'subs/final.srt').write_text(srt(3))
    return ctx,cpa


def test_ordinary_requests_label_corpus_and_observe_prior_choices(tmp_path,monkeypatch):
    fake_title_skill(tmp_path,monkeypatch)
    rows=[{'candidate_id':'own','title':'证据齐全的亲写示例',
           'style_authorship':{'kind':'reviewer_written','evidence':'verbatim author evidence'}},
          {'candidate_id':'accepted','title':'批准采用但并非亲写示例'},
          {'candidate_id':'fresh','title':'未来新增但没有作者证据'}]
    corpus=tmp_path/'corpus.json'
    corpus.write_text(json.dumps({'overrides':rows}))
    monkeypatch.setattr(title_authority,'TITLE_CORPUS',corpus)
    monkeypatch.setattr(title_history,'load_publication_registry',lambda:{'entries':[
        {'candidate_id':'p1','status':'published','bvid':'BV1','recording_date':'2026-11-01',
         'title':'【李豆沙】熊猫头又把话说反了'},
        {'candidate_id':'p2','status':'hold_pending_review','title':'禁止把搁置件当已发'},
        {'candidate_id':'p3','status':'published','bvid':'BV3'}]})
    first,cpa=context(tmp_path,'first','小李半分钟后真的嘴硬了')
    assert not isinstance(step6_title.pick_title(first),StepOutcome)
    prompt=cpa.title_prompts[0]
    own=prompt.split('### 维护者 亲写标题',1)[1].split('### 维护者 认可的参考标题',1)[0]
    assert '证据齐全的亲写示例' in own
    assert '批准采用但并非亲写示例' not in own and '未来新增但没有作者证据' not in own
    assert '批准采用但并非亲写示例' in prompt and '未来新增但没有作者证据' in prompt
    history=prompt.split('## 最近标题的称呼',1)[1].split('## 本流水线的输入',1)[0]
    assert '- 熊猫头' in history and '又把话说反了' not in history and '禁止把搁置件当已发' not in history
    second,cpa=context(tmp_path,'unseen','熊猫头半分钟后真的嘴硬了')
    assert not isinstance(step6_title.pick_title(second),StepOutcome)
    assert '本批已起：\n- 【李豆沙】小李半分钟后真的嘴硬了' in cpa.title_prompts[0]
    record=json.loads((second.clip_dir/'title/candidates.json').read_text())
    assert record['title_history']['batch']==['【李豆沙】小李半分钟后真的嘴硬了']


def test_unreadable_registry_is_recorded_without_blocking_title(tmp_path,monkeypatch):
    fake_title_skill(tmp_path,monkeypatch)
    def missing():raise PermissionError('fixture registry unreadable')
    monkeypatch.setattr(title_history,'load_publication_registry',missing)
    ctx,cpa=context(tmp_path,'fresh','小李半分钟后真的嘴硬了')
    result=step6_title.pick_title(ctx)
    assert not isinstance(result,StepOutcome)
    assert any('PermissionError' in s for s in result[2])
    assert '无可读标题' in cpa.title_prompts[0]


def test_unprivileged_lane_uses_titles_only_projection_and_rejects_duplicate(tmp_path, monkeypatch):
    fake_title_skill(tmp_path, monkeypatch)
    monkeypatch.setattr(title_history, 'load_publication_registry',
                        lambda: (_ for _ in ()).throw(PermissionError('root-only registry')))
    ctx, _ = context(tmp_path, 'fresh', 'unused')
    previous = '【李豆沙】熊猫头半分钟后真的嘴硬了'
    ctx.adapters.cpa = FakeCpa([answer('熊猫头半分钟后真的嘴硬了'), answer('小李又改口说要再买一本')])
    ctx.adapters.remote.on_script = lambda name, body, remote: RemoteResult(
        0, json.dumps({'titles': [previous], 'note': '已发标题：可读 1 条，登记缺标题 0 条'}, ensure_ascii=False), '')
    result = step6_title.pick_title(ctx)
    assert not isinstance(result, StepOutcome)
    record = json.loads((ctx.clip_dir / 'title/candidates.json').read_text())
    assert record['title_history']['published'] == [previous]
    assert record['title_history']['published_names'] == ['熊猫头']
    assert record['candidates'][0]['checks']['failed'] == ['published_duplicate']
    assert record['chosen']['title'] != previous
    assert '熊猫头半分钟后真的嘴硬了' not in ctx.adapters.cpa.title_prompts[0]
    assert any(name == 's6-published-titles' and 'sudo -n' in body and 'cpa.env' not in body
               for name, body in ctx.adapters.remote.scripts)


def test_projection_omits_held_and_current_candidate_and_checks_all_published(monkeypatch):
    rows = [{'status': 'published', 'candidate_id': 'own', 'title': '自己的标题'},
            {'status': 'hold_pending_review', 'candidate_id': 'held', 'title': '不得泄露'},
            *({'status': 'published', 'candidate_id': str(i), 'recording_date': f'{i:02}',
               'title': f'【李豆沙】已发标题{i}'} for i in range(15))]
    monkeypatch.setattr(title_history, 'load_publication_registry', lambda: {'entries': rows})
    titles, note = title_history.published_titles('own', limit=12)
    assert len(titles) == 15 and '不得泄露' not in titles and '自己的标题' not in titles
    assert '可读 15 条' in note
    assert title_history.duplicate_title('【李豆沙】已发标题0！', tuple(titles))
    assert not title_history.duplicate_title('【李豆沙】另一个标题', tuple(titles))


def test_claiming_authorship_without_evidence_stops_before_provider(tmp_path,monkeypatch):
    fake_title_skill(tmp_path,monkeypatch)
    corpus=tmp_path/'corpus.json'
    corpus.write_text(json.dumps({'overrides':[{'title':'虚假署名','style_authorship':{'kind':'reviewer_written'}}]}))
    monkeypatch.setattr(title_authority,'TITLE_CORPUS',corpus)
    ctx,cpa=context(tmp_path,'control','小李半分钟后真的嘴硬了')
    result=step6_title.pick_title(ctx)
    assert isinstance(result,StepOutcome) and result.reason=='TITLE_AUTHORITY_MISSING'
    assert not cpa.prompts


@pytest.mark.parametrize('title,names',[
    ('【李豆沙】不用写名也可以',()),
    ('【李豆沙】礼豆沙这次的CP名怎么这么长',()),
    ('【李豆沙×雾深Girimi】李豆沙和雾深玩游戏',('李豆沙',)),
    ('【李豆沙】小李半分钟后嘴硬了',('小李',)),
    ('【李豆沙】熊猫头、李姐都在说自己',('熊猫头','李姐'))])
def test_names_ignore_archive_prefix_and_overlapping_forms(title,names):
    assert title_history.title_names(title)==names


def test_a_clip_published_once_does_not_meet_its_own_title_as_a_duplicate(monkeypatch):
    """486140 was published, then made again for a same-BV replacement: the registry row carries the publication id
    fm-<date>-<slug>, the lane asks with the clip id."""
    monkeypatch.setattr(title_history, 'load_publication_registry', lambda: {'entries': [
        {'status': 'published', 'candidate_id': 'fm-20261106-semantictalk-486140-633940', 'title': '【李豆沙】本片自己的标题'},
        {'status': 'published', 'candidate_id': 'fm-20261104-semantictalk-632500-775610', 'title': '【李豆沙】别的片的标题'},
        {'status': 'published', 'candidate_id': 'fm-20261106-xsemantictalk-486140-633940', 'title': '【李豆沙】名字只是相像'}]})
    titles, _ = title_history.published_titles('semantictalk_486140_633940')
    assert titles == ['【李豆沙】别的片的标题', '【李豆沙】名字只是相像']
