"""Synthetic, unseen inputs: evidence recovery and ordinary-entry behaviour, no reviewed subtitles."""
import json

import pytest

from fakes import FakeRemote, IdCpa, make_ctx
from src.autoslice.manual_lane import evidence_attach
from src.autoslice.manual_lane.step3_evidence import _danmaku_lines, _evidence_for_cue


def evidence(text, jy_text, rows=(), words=()):
    cue = {'id': 1, 'start_s': 10., 'end_s': 14., 'text': text}
    jy = evidence_attach.attach_asr([cue], [{'start_s': 10., 'end_s': 14., 'text': jy_text}], source='jianying')
    lines = {'jianying': [{'abs_start_s': 10., 'text': jy_text, 'words': list(words)}]} if words else {}
    return _evidence_for_cue(cue, 0, [cue], jy, {}, lines, list(rows))


def test_readout_danmaku_and_sc_mechanical_evidence():
    rows = [{'t_s': -20., 'text': '今天有没有那个计划'},
            {'t_s': -80., 'text': '今天有没有那个计划', 'sender': '远山来客', 'type': 'sc'}]
    hits = evidence('今天有没有那个计划', '今天有没有那个计划', rows)['playbook']['readout_matches']
    assert {h['type'] for h in hits} == {'danmaku', 'sc'}
    assert all(h['sent_s'] < 0 for h in hits)


def test_recording_events_replace_subset_and_reach_back_to_the_broadcast_start(tmp_path):
    xml = b'<i><sc ts="750" user="sender">message</sc><gift ts="730" user="g" giftname="gift" giftcount="2"/><guard ts="710" user="h" level="3" count="1"/><sc ts="699" user="old">x</sc></i>'
    ctx = make_ctx(tmp_path, remote=FakeRemote(), cpa=IdCpa(), danmaku_xml=xml)
    ctx.state.data['clip']['start_ms'] = 1000000
    subset = ctx.clip_dir / 'danmaku/source.xml'
    subset.parent.mkdir(parents=True, exist_ok=True)
    subset.write_text('<i/>')
    rows = _danmaku_lines(ctx, 30)
    assert [(r['type'], r['t_s']) for r in rows] == [('sc', -250.), ('gift', -270.), ('guard', -290.), ('sc', -301.)]
    assert subset.read_text() == '<i/>'


@pytest.mark.parametrize('written,heard', [('山林', '山陵'), ('边关', '边光')])
def test_toneless_spam_matches_asr_near_homophone(written, heard):
    rows = [{'t_s': t, 'text': written * 2} for t in (8., 11., 13.)]
    hits = evidence('我说的是' + heard, '我说的是' + heard, rows)['playbook']['repeated_danmaku']
    assert any(h['text'] == written and h['asr_span'] == heard and h['count'] == 3 for h in hits)
    assert not evidence('我说的是' + heard, '我说的是' + heard, rows[:2])['playbook']['repeated_danmaku']


def test_word_time_repeat_evidence_is_not_interpolated():
    words = [{'abs_start_s': 10.1, 'abs_end_s': 10.3, 'text': '再'},
             {'abs_start_s': 10.5, 'abs_end_s': 10.7, 'text': '再'},
             {'abs_start_s': 15., 'abs_end_s': 16., 'text': '再'}]
    timing = evidence('再', '再再', words=words)['playbook']['word_time']
    assert timing['repeats'] == [{'text': '再', 'count': 2, 'times': [[10.1, 10.3], [10.5, 10.7]]}]
    assert evidence('再', '再再')['playbook']['word_time']['available'] is False


def test_audio_classification_and_grouping():
    from src.autoslice.manual_lane import playbook_evidence as pe
    assert pe.classify('素材', '素菜', [])['kind'] == 'rule_only'
    assert pe.classify('哈哈', '', [])['kind'] == 'rule_only'
    assert pe.classify('', '', [])['kind'] == 'rule_only'
    assert pe.classify('再', '再再', [])['kind'] == 'needs_audio'
    cues = [{'id': i, 'start_s': float(i), 'end_s': float(i + 1), 'text': '文字'} for i in range(1, 5)]
    doubts = [{'id': f'd{i}', 'cue_id': i, 'audio_class': pe.classify('天空', '湖水', [])} for i in range(1, 5)]
    windows = pe.grouped_windows(cues, doubts, 10)
    assert len(windows) == 1 and len(windows[0]['triggers']) == 4
    assert not pe.left_open({'status': 'OK', 'lines': [{'text': '晴天'}]})
    assert pe.left_open({'status': 'OK', 'lines': [{'text': '晴（?）天'}]})


def test_free_asr_real_word_schema():
    from src.autoslice.manual_lane import playbook_evidence as pe
    assert pe.word_times({'utterances': [{'words': [{'label': '再', 'start_time': 100, 'end_time': 300}]}]}) == [
        {'text': '再', 'start_s': .1, 'end_s': .3}]


def test_cpa_final_text_and_bcut_grid_preserved(tmp_path):
    from src.autoslice.manual_lane.step3_doubts import write_subtitles
    ctx = make_ctx(tmp_path, remote=FakeRemote(), cpa=IdCpa())
    cue = {'id': 1, 'start_s': 0., 'end_s': 2., 'text': '他'}
    d = {'id': 'd001', 'cue_id': 1, 'state': 'ADOPT', 'current': '他', 'final_text': '他，呃',
         'confidence': '高', 'affects_meaning': False}
    _, final, _ = write_subtitles(ctx, [cue], [d], preserve_decisions=True)
    assert '他，呃' in final.read_text() and '00:00:00,000 --> 00:00:02,000' in final.read_text()


def test_cpa_decision_is_word_safely_shaped_without_changing_spoken_text(tmp_path):
    from src.autoslice.manual_lane import subtitle_track
    from src.autoslice.manual_lane.step3_doubts import write_subtitles

    ctx = make_ctx(tmp_path, remote=FakeRemote(), cpa=IdCpa())
    text = '他们打算在10月21号一起看《晚风中的花园》，然后回大学校园找Mujica的朋友'
    cue = {'id': 1, 'start_s': 0., 'end_s': 6., 'text': '原文'}
    decision = {'id': 'd001', 'cue_id': 1, 'state': 'ADOPT', 'current': '原文', 'final_text': text,
                'confidence': '高', 'affects_meaning': False}
    _, final, _ = write_subtitles(ctx, [cue], [decision], preserve_decisions=True)
    shaped = subtitle_track.parse_blocks(final.read_text(encoding='utf-8'))
    assert ''.join(''.join(c['text'].split()) for c in shaped) == ''.join(text.split())
    assert shaped[0]['start_s'] == 0. and shaped[-1]['end_s'] == 6.
    assert all(len(line) <= 18 for cue in shaped for line in cue['text'].splitlines())
    assert any('《晚风中的花园》' in line for cue in shaped for line in cue['text'].splitlines())
    assert any('10月21号' in line for cue in shaped for line in cue['text'].splitlines())


def test_ordinary_single_entry_one_text_call_escalates_only_open_window(tmp_path, monkeypatch):
    import hashlib
    from agent_fakes import AgentCpa, answer_rows
    from src.autoslice.manual_lane import playbook_step3 as pb, step3_subtitles as step
    rows = answer_rows([{'id': 'd001', 'state': 'ADOPT', 'final_text': '湖水',
                         'confidence': '高', 'affects_meaning': False}])
    request = {'tool': 'listen', 'start': 0, 'end': 2, 'cue_ids': [1], 'reason': '核实读音'}
    cpa = AgentCpa([], answers=[
        {'action': 'plan', 'doubts': [{'cue_ids': [1], 'question': '天空还是湖水'}],
         'tools': [dict(request, model='38m')]},
        {'action': 'tools', 'tools': [dict(request, model='argon')]},
        {'action': 'draft', 'rows': rows}, {'action': 'final', 'self_check': '核对两次听音', 'rows': rows}])
    ctx = make_ctx(tmp_path, remote=FakeRemote(), cpa=cpa, danmaku_xml=b'<i/>')
    ctx.session['participants'] = [['single test speaker']]
    for name, text in [('bcut', '天空'), ('jianying', '湖水')]:
        p = ctx.clip_dir / f'asr/{name}.srt'
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f'1\n00:00:00,000 --> 00:00:02,000\n{text}\n')
    monkeypatch.setenv('MANUAL_LANE_ADJUDICATE_MODE', 'C')
    calls = []
    def listen(_ctx, windows, model, label):
        calls.append(model['slug'])
        suffix = hashlib.sha256(model['name'].encode()).hexdigest()[:12]
        ctx.adapters.remote.files[f"{ctx.remote_dir}/listen/out/audio/{windows[0]['win']}.{suffix}.receipt.json"] = (
            json.dumps({'usage': {'totalTokenCount': 12}}).encode())
        return [{'win': w['win'], 'slug': model['slug'], 'status': 'OK',
                 'lines': [{'start_s': 0., 'text': '湖水（?）' if model['slug'] == '38m' else '湖水'}]}
                for w in windows]
    monkeypatch.setattr(pb, '_listen', listen)
    monkeypatch.setattr(pb, 'upload_listen_tools', lambda *_args: None)
    outcome = step._step3_body(ctx)
    assert outcome.ok and calls == ['38m', 'argon'] and len(outcome.cpa) == 4
    from src.autoslice.manual_lane.step_io import PRONOUN_RULE
    assert PRONOUN_RULE in cpa.prompts[0]
    assert '湖水' in (ctx.clip_dir / 'subs/final.srt').read_text()
    trace = json.loads((ctx.clip_dir / 'subs/agent-trace.json').read_text())
    assert [e['request']['model'] for e in trace['events']
            if e['event'] == 'tool_request'] == ['38m', 'argon']
    assert trace['total_tokens'] == 64


def test_split_readout_evidence_reaches_one_cpa_and_selected_text_reaches_srt(tmp_path, monkeypatch):
    from agent_fakes import AgentCpa
    from src.autoslice.manual_lane import step3_subtitles as step

    chosen = '请给小猫系红绳'
    cpa = AgentCpa([{'id': 'd001', 'state': 'KEEP', 'final_text': chosen,
                     'confidence': '高', 'affects_meaning': False,
                     'reason': '分歧槽红/蓝，念读弹幕对应红'}],
                   tools=[{'tool': 'danmaku', 'start': -10, 'end': 10},
                          {'tool': 'compare', 'cue_ids': [1]}])
    ctx = make_ctx(tmp_path, remote=FakeRemote(), cpa=cpa,
                   danmaku_xml='<i><d p="0,1,1">请给小猫系红绳</d></i>'.encode())
    ctx.session['participants'] = [['single test speaker']]
    for name, text in [('bcut', chosen), ('jianying', '请给小猫系蓝绳')]:
        path = ctx.clip_dir / f'asr/{name}.srt'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f'1\n00:00:01,000 --> 00:00:03,000\n{text}\n')
    monkeypatch.setenv('MANUAL_LANE_ADJUDICATE_MODE', 'C')
    assert step._step3_body(ctx).ok
    trace = json.loads((ctx.clip_dir / 'subs/agent-trace.json').read_text())
    evidence = [e for e in trace['events'] if e['event'] == 'tool_result']
    assert any(row['text'] == chosen for row in evidence[0]['result'])
    assert evidence[1]['result'][0]['jianying'] == '请给小猫系蓝绳'
    assert len(cpa.calls) == 3
    assert chosen in (ctx.clip_dir / 'subs/final.srt').read_text()


def test_same_transcribed_name_sound_can_use_table_spelling_without_replacing_another_sound(tmp_path, monkeypatch):
    from agent_fakes import AgentCpa
    from src.autoslice.manual_lane import step3_subtitles as step

    source = ('1\n00:00:00,000 --> 00:00:02,000\n我看油菜尤娜的直播\n\n'
              '2\n00:00:03,000 --> 00:00:05,000\n我喝的是红茶\n')
    responses = [
        {'id': 'd001', 'state': 'ADOPT', 'final_text': '我看由菜Yuna的直播',
         'confidence': '高', 'affects_meaning': False, 'reason': '同音专名按表写法'},
        {'id': 'd002', 'state': 'KEEP', 'final_text': '我喝的是红茶',
         'confidence': '高', 'affects_meaning': False, 'reason': '两来源红茶音一致'},
    ]
    cpa = AgentCpa(responses, tools=[{'tool': 'spelling'}, {'tool': 'danmaku', 'start': -10, 'end': 10}])
    xml = '<i><d p="0,1,1">由菜Yuna直播</d><d p="2.9,1,1">我喝的是绿茶</d></i>'.encode()
    ctx = make_ctx(tmp_path, remote=FakeRemote(), cpa=cpa, danmaku_xml=xml)
    ctx.session['participants'] = [['single test speaker']]
    for name in ('bcut', 'jianying'):
        path = ctx.clip_dir / f'asr/{name}.srt'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source)
    monkeypatch.setenv('MANUAL_LANE_ADJUDICATE_MODE', 'C')

    assert step._step3_body(ctx).ok
    trace = json.loads((ctx.clip_dir / 'subs/agent-trace.json').read_text())
    table = next(e['result'] for e in trace['events'] if e['event'] == 'tool_result'
                 and e['request']['tool'] == 'spelling')
    assert any(row['canonical'] == '由菜Yuna' for row in table)
    assert len(cpa.calls) == 3
    result = (ctx.clip_dir / 'subs/final.srt').read_text()
    assert '由菜Yuna' in result and '红茶' in result and '绿茶' not in result


def test_existing_human_anchors_keep_their_native_owner(tmp_path, monkeypatch):
    from src.autoslice.manual_lane import step3_subtitles as step
    from src.autoslice.manual_lane.runner import StepOutcome
    ctx = make_ctx(tmp_path, remote=FakeRemote(), cpa=IdCpa())
    ctx.session['participants'] = [['single test speaker']]
    for name in ('bcut', 'jianying'):
        p = ctx.clip_dir / f'asr/{name}.srt'
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text('1\n00:00:00,000 --> 00:00:02,000\n原句\n')
    ctx.state.data['reviewer_labels'] = {'labels': [{'current': '原句', 'text': '人工冻结',
                                                'abs_start_ms': 10000, 'abs_end_ms': 12000}]}
    monkeypatch.setenv('MANUAL_LANE_ADJUDICATE_MODE', 'C')
    seen = []
    def native(_ctx, **kwargs):
        seen.extend(kwargs['labels'].values())
        return StepOutcome(True)
    monkeypatch.setattr(step.variant_c, 'run_step3', native)
    assert step._step3_body(ctx).ok
    assert seen[0]['text'] == '人工冻结'
