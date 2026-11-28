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


def test_a_title_that_opens_like_one_the_batch_already_has_is_written_again(tmp_path, monkeypatch):
    # 11-23: eight of ten clips of one round opened 「游戏里…」; the batch's titles reached the prompt for the names only.
    fake_title_skill(tmp_path, monkeypatch)
    monkeypatch.setattr(title_history, 'load_publication_registry', lambda: {'entries': []})
    first, _ = context(tmp_path, 'first', '游戏里被紫色堵门了')
    assert not isinstance(step6_title.pick_title(first), StepOutcome)
    second, cpa = context(tmp_path, 'second', '游戏里抢一个破石头')
    cpa.responses.append(answer('抢一个破石头抢崩溃了'))
    assert not isinstance(step6_title.pick_title(second), StepOutcome)
    record = json.loads((second.clip_dir / 'title/candidates.json').read_text())
    first_round = [c for c in record['candidates'] if c['round'] == 0]
    assert first_round and all('batch_opener' in c['checks']['failed'] for c in first_round)
    assert record['chosen']['title'] == '【李豆沙】抢一个破石头抢崩溃了' and record['chosen']['checks']['ok']
    assert '本片不要用和它们一样的开头' in cpa.title_prompts[0]


def test_titles_that_open_differently_or_too_short_to_tell_pass_the_opener_check():
    batch = ('【李豆沙】游戏里被紫色堵门了', '【李豆沙】好')
    assert step6_title.opener('【李豆沙】游戏里抢石头') == '游戏里'
    assert step6_title.opener('【李豆沙】好') == ''
    assert not [t for t in batch if step6_title.opener(t) and step6_title.opener(t) == step6_title.opener('【李豆沙】玩游戏抢石头')]


def test_a_title_that_writes_the_unnamed_guest_label_as_a_person_is_written_again(tmp_path, monkeypatch):
    # 11-23: step 4 heard the collab partners by voiceprint on a day whose session named none; the prompt said
    # 「[连线] 是李豆沙以外的连线方」 and the titles read 「连线提议反矮人联盟」「跟连线抓金块」.
    from src.autoslice.manual_lane import subtitle_track
    fake_title_skill(tmp_path, monkeypatch)
    monkeypatch.setattr(title_history, 'load_publication_registry', lambda: {'entries': []})
    ctx, cpa = context(tmp_path, 'collab', '连线提议炸弹取暖')
    ctx.session['participants'] = [['李豆沙', '小李']]        # the room title named nobody: host only
    cues = subtitle_track.read_track(ctx.clip_dir)
    for i, cue in enumerate(cues):
        cue['speaker'] = step6_title.GUEST_LABEL if i == 1 else step6_title.HOST
    subtitle_track.write_track(ctx.clip_dir, cues, speakers=True)
    cpa.responses.append(answer('队友提议炸弹取暖'))
    assert not isinstance(step6_title.pick_title(ctx), StepOutcome)
    record = json.loads((ctx.clip_dir / 'title/candidates.json').read_text())
    first_round = [c for c in record['candidates'] if c['round'] == 0]
    assert first_round and all('label_as_name' in c['checks']['failed'] for c in first_round)
    assert record['chosen']['title'] == '【李豆沙】队友提议炸弹取暖' and record['chosen']['checks']['ok']
    assert '[未具名嘉宾]' in cpa.title_prompts[0] and '不要把这个标签当名字写' in cpa.title_prompts[0]


def test_a_label_word_she_says_herself_may_stand_in_the_title():
    # the check is on the label, not the word: 「连线」 she says in a line may be quoted
    cand = {'title': '【李豆沙】说好连线结果掉线了', 'on_stage': ['李豆沙'], 'punchline_person': '她',
            'swap_check': {'holds_with_other_host': False}}
    said = '1\n00:00:01,000 --> 00:00:02,000\n[李豆沙] 说好连线结果掉线了\n'
    label = '1\n00:00:01,000 --> 00:00:02,000\n[连线] 掉线了\n'
    assert 'label_as_name' not in step6_title.title_checks(cand, ['李豆沙'], said)['failed']
    assert 'label_as_name' in step6_title.title_checks(cand, ['李豆沙'], label)['failed']


def test_cover_text_that_writes_the_unnamed_guest_label_as_a_person_is_rewritten_and_the_title_kept(tmp_path, monkeypatch):
    from src.autoslice.manual_lane import subtitle_track
    fake_title_skill(tmp_path, monkeypatch)
    monkeypatch.setattr(title_history, 'load_publication_registry', lambda: {'entries': []})
    ctx, cpa = context(tmp_path, 'cover', '队友提议炸弹取暖')
    ctx.session['participants'] = [['李豆沙', '小李']]
    cues = subtitle_track.read_track(ctx.clip_dir)
    cues[1]['speaker'] = step6_title.GUEST_LABEL
    subtitle_track.write_track(ctx.clip_dir, cues, speakers=True)
    leaky = json.loads(cpa.responses[0])
    leaky['cover_lines'] = ['连线说', '炸弹取暖']
    cpa.responses[0] = json.dumps(leaky, ensure_ascii=False)
    cpa.responses.append(json.dumps({'cover_lines': ['队友说', '炸弹取暖']}, ensure_ascii=False))     # the cover text fix
    picked = step6_title.pick_title(ctx)
    assert not isinstance(picked, StepOutcome), picked
    record = json.loads((ctx.clip_dir / 'title/candidates.json').read_text())
    # the cover text's own fault no longer throws out the round's titles (11-24 178410: two rounds of eight went that way)
    assert all(c['checks']['ok'] for c in record['candidates']) and record['rounds'] == ['title']
    assert 'cover-text-fix-1' in cpa.calls and '「连线」是说话人标签' in cpa.prompts[cpa.calls.index('cover-text-fix-1')]
    assert record['cover_lines'] == ['队友说', '炸弹取暖'] and picked[1] == ['队友说', '炸弹取暖']
    assert any(line.startswith('封面文字重写（第 1 次，标题不变）：队友说 / 炸弹取暖') for line in picked[2])


def test_clips_of_one_batch_pick_their_titles_one_after_another(tmp_path, monkeypatch):
    # 11-23: ten clips reached step 6 together; each saw only the batch's old titles and ten opened 「游戏里」
    import threading
    import time
    inside, most = [0], [0]

    def slow(ctx):
        inside[0] += 1
        most[0] = max(most[0], inside[0])
        time.sleep(0.1)
        inside[0] -= 1
        return StepOutcome(ok=False, reason='X', note='')
    monkeypatch.setattr(step6_title, '_pick_title', slow)
    ctxs = [context(tmp_path, f'c{i}', '她嘴硬了')[0] for i in range(3)]
    threads = [threading.Thread(target=step6_title.pick_title, args=(c,)) for c in ctxs]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert most[0] == 1


def test_a_pause_mark_in_the_cover_text_is_not_rendered(tmp_path, monkeypatch):
    # 11-23 625460: the punchline 「勾到了。我要笑」 went onto the cover with its full stop
    fake_title_skill(tmp_path, monkeypatch)
    monkeypatch.setattr(title_history, 'load_publication_registry', lambda: {'entries': []})
    ctx, cpa = context(tmp_path, 'pause', '小李半分钟后真的嘴硬了')
    said = json.loads(cpa.responses[0])
    said['cover_lines'] = ['队友求助勾石头，', '勾到了。我要笑']
    cpa.responses[0] = json.dumps(said, ensure_ascii=False)
    assert not isinstance(step6_title.pick_title(ctx), StepOutcome)
    record = json.loads((ctx.clip_dir / 'title/candidates.json').read_text())
    assert record['cover_lines'] == ['队友求助勾石头', '勾到了 我要笑']


def test_the_name_and_words_form_keeps_its_colon_on_the_cover():
    # 11-23 443680: 「安晚：暖一暖」 (step 70 allows 「名字：原话」) came out as 「安晚 暖一暖」
    from src.autoslice.manual_lane.step6_cover import cover_line
    assert cover_line('安晚：暖一暖') == '安晚：暖一暖'
    assert cover_line('勾到了。我要笑') == '勾到了 我要笑' and cover_line('快帮我！') == '快帮我！'
    assert cover_line('她说：') == '她说'
