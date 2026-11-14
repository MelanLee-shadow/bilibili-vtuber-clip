"""The agent's `hints` tool and the self-check turn expose the pipeline's mechanical candidates as evidence only."""
import json

from agent_fakes import AgentCpa, answer_rows
from fakes import FakeRemote, make_ctx
from src.autoslice.manual_lane import playbook_step3 as pb, step3_subtitles as step


def context(tmp_path, monkeypatch, cpa, *, bcut='原句', jianying='另一句', participants=(('single speaker',),)):
    ctx = make_ctx(tmp_path, remote=FakeRemote(), cpa=cpa)
    ctx.session['participants'] = [list(group) for group in participants]
    for name, text in [('bcut', bcut), ('jianying', jianying)]:
        path = ctx.clip_dir / f'asr/{name}.srt'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f'1\n00:00:00,000 --> 00:00:02,000\n{text}\n')
    monkeypatch.setenv('MANUAL_LANE_ADJUDICATE_MODE', 'C')
    return ctx


def rows(text):
    return answer_rows([{'id': 'd001', 'state': 'ADOPT', 'final_text': text, 'confidence': '高',
                         'affects_meaning': False}])


def trace_of(ctx):
    return json.loads((ctx.clip_dir / 'subs/agent-trace.json').read_text())


def results(ctx, tool):
    return [e['result'] for e in trace_of(ctx)['events']
            if e['event'] == 'tool_result' and e['request'].get('tool') == tool]


def pair(misheard, standard):
    return {'misheard': misheard, 'standard': standard, 'evidence': [{'scope': {'kind': 'global'}}]}


def test_hints_tool_returns_the_existing_per_cue_candidates(tmp_path, monkeypatch):
    monkeypatch.setattr(pb.mishearing, 'load_pairs', lambda *_a: [pair('兴陈', '星辰')])
    cpa = AgentCpa(rows('结果'), tools=[{'tool': 'hints'}, {'tool': 'hints', 'cue_ids': [1]}])
    ctx = context(tmp_path, monkeypatch, cpa, bcut='兴陈大海', participants=[['星辰大海', '大海']])
    ctx.adapters.danmaku.xml = '<i><d p="8,1,1">兴陈大海加油</d></i>'.encode()
    assert step._step3_body(ctx).ok
    whole, one = results(ctx, 'hints')
    assert whole == one and whole['checked_cues'] == [1]
    item = whole['hints'][0]
    assert item['id'] == 1
    assert item['readout_matches'][0]['text'] == '兴陈大海加油'
    assert item['readout_matches'][0]['relation'] == 'readout_candidate'
    assert item['near_names'] == [{'name': '星辰大海', 'spellings': ['星辰大海', '大海']}]
    assert item['mishearing'] == ['- 兴陈 → 星辰']
    assert '机械候选' in whole['note']


def test_hints_with_nothing_to_report_is_empty_not_an_error(tmp_path, monkeypatch):
    monkeypatch.setattr(pb.mishearing, 'load_pairs', lambda *_a: [])
    cpa = AgentCpa(rows('结果'), tools=[{'tool': 'hints'}, {'tool': 'hints', 'cue_ids': [9]},
                                        {'tool': 'hints', 'cue_ids': []}])
    ctx = context(tmp_path, monkeypatch, cpa)
    assert step._step3_body(ctx).ok
    empty, unknown, blank = results(ctx, 'hints')
    assert empty['hints'] == [] and empty['checked_cues'] == [1] and 'error' not in empty
    assert unknown == {'error': 'cue_ids must identify input cues'} == blank


def test_self_check_turn_carries_the_hints_and_cpa_text_is_not_rewritten(tmp_path, monkeypatch):
    monkeypatch.setattr(pb.mishearing, 'load_pairs', lambda *_a: [])
    answers = [{'action': 'plan', 'doubts': [], 'tools': []},
               {'action': 'draft', 'rows': rows('草稿')},
               {'action': 'draft', 'rows': rows('再草稿')},
               {'action': 'final', 'self_check': 'd001 与原记录不同：她实际念的是另一写法', 'rows': rows('代理自定文字')}]
    cpa = AgentCpa([], answers=answers)
    ctx = context(tmp_path, monkeypatch, cpa, bcut='兴陈大海')
    ctx.adapters.danmaku.xml = '<i><d p="8,1,1">兴陈大海加油</d></i>'.encode()
    assert step._step3_body(ctx).ok
    first, second = [e for e in trace_of(ctx)['events'] if e['event'] == 'draft_recorded']
    assert first['hints']['hints'][0]['readout_matches'][0]['text'] == '兴陈大海加油'
    assert 'readout_candidate' in first['instruction'] and 'hints' not in second
    assert '兴陈大海加油' in cpa.prompts[2] and '可能有误' in cpa.prompts[2]
    final = (ctx.clip_dir / 'subs/final.srt').read_text()
    assert '代理自定文字' in final and '兴陈大海加油' not in final


def test_self_check_with_no_hints_or_unavailable_danmaku_is_recorded_and_the_clip_continues(tmp_path, monkeypatch):
    monkeypatch.setattr(pb.mishearing, 'load_pairs', lambda *_a: [])
    ctx = context(tmp_path, monkeypatch, AgentCpa(rows('结果')))
    assert step._step3_body(ctx).ok
    draft = next(e for e in trace_of(ctx)['events'] if e['event'] == 'draft_recorded')
    assert draft['hints']['hints'] == [] and 'error' not in draft['hints']

    def refuse(_path):
        raise RuntimeError('danmaku source unreachable')

    other = context(tmp_path / 'b', monkeypatch, AgentCpa(rows('结果')))
    other.adapters.danmaku.fetch = refuse
    assert step._step3_body(other).ok
    draft = next(e for e in trace_of(other)['events'] if e['event'] == 'draft_recorded')
    assert draft['hints']['error'] == 'RuntimeError: danmaku source unreachable'


def test_sender_name_with_latin_suffix_matches_a_near_homophone_spoken_form():
    from src.autoslice.manual_lane import playbook_evidence as pe
    cue = {'id': 1, 'start_s': 10., 'end_s': 14., 'text': ''}
    rows = [{'t_s': -20., 'type': 'sc', 'sender': '远山来客_Channel', 'text': '', 'gift_name': None}]
    hit = pe.readout_matches(cue, ['谢谢源三来课'], rows)
    assert [h['text'] for h in hit] == ['远山来客_Channel'] and hit[0]['pinyin_match']
    assert pe.readout_matches(cue, ['今天吃什么好呢'], rows) == []


def test_cue_before_a_read_out_paid_message_gets_its_sender_as_a_name_candidate():
    from src.autoslice.manual_lane import playbook_evidence as pe
    cues = [{'id': 1, 'start_s': 0., 'end_s': 2., 'text': '谢谢一段听不清的称呼'},
            {'id': 2, 'start_s': 2.5, 'end_s': 6., 'text': '今天下班特别早所以来看直播'},
            {'id': 3, 'start_s': 30., 'end_s': 33., 'text': '后面说的是别的事情了'}]
    rows = [{'t_s': -600., 'type': 'sc', 'sender': '甲乙丙丁_Channel', 'text': '今天下班特别早所以来看直播', 'gift_name': None},
            {'t_s': 1., 'text': '后面说的是别的事情了'}]
    hits = {c['id']: pe.readout_matches(c, [c['text']], rows) for c in cues}
    assert not any(h['text'] == '甲乙丙丁_Channel' for h in hits[1])        # the name itself was not matched by sound
    heard = {c['id']: [c['text']] for c in cues}
    got = pe.senders_read_next(cues, hits, heard)
    assert set(got) == {1, 2} and got[1] == got[2] == [
        {'sender': '甲乙丙丁_Channel', 'type': 'sc', 'sent_s': -600., 'body_read_from_cue': 2}]
    # a paid message that only shares scattered characters with a sentence is not being read: no name candidate
    loose = [{'t_s': -900., 'type': 'sc', 'sender': '戊己', 'text': '事情说了后面再看别的吧', 'gift_name': None}]
    hits = {c['id']: pe.readout_matches(c, [c['text']], loose) for c in cues}
    assert hits == {1: [], 2: [], 3: []}        # sent long ago and only scattered characters in common: not a read-out
    assert pe.read_run('啦啦啦啦啦可以吗', '你是啦啦啦啦啦') == 0 and pe.read_run('今天下班特别早', '今天下班特别早所以') == 7
    recent = [dict(loose[0], t_s=25.)]           # the same message sent seconds before the sentence stays a candidate,
    hits = {c['id']: pe.readout_matches(c, [c['text']], recent) for c in cues}     # but names nobody to thank
    assert hits[3] and pe.senders_read_next(cues, hits, heard) == {}


def test_the_beginning_of_a_known_mishearing_where_the_recognitions_differ_is_a_name_candidate():
    from src.autoslice.manual_lane import playbook_evidence as pe
    heads = pe.misheard_heads([{'misheard': '问路熊', 'standard': '甲乙'}, {'misheard': '短', 'standard': '丙'},
                               {'misheard': '谢谢甲', 'standard': '丁'}])
    assert set(heads) == {'问路', '谢谢'}                       # a mishearing under three characters has no beginning
    readings = {1: ['问路什么是小孩', '你们为什么是小孩'],          # the recognitions scatter after the same two syllables
                2: ['谢谢大家的礼物', '谢谢大家的礼物'],            # an ordinary word both of them hear
                3: ['谢谢你呀', '些些你呀'],                      # ... so its other, garbled occurrence is no candidate
                4: ['那边都是小孩', '屏幕上都是小孩', '问路什么是小孩']}   # only a third recogniser writes the beginning
    got = pe.begun_mishearings(readings, heads)
    assert got == {1: [{'standard': '甲乙', 'known_mishearing': '问路熊', 'heard': '问路'}],
                   4: [{'standard': '甲乙', 'known_mishearing': '问路熊', 'heard': '问路'}]}
    assert pe.begun_mishearings({1: ['问路去哪里', '问路去哪里']}, heads) == {}       # agreed: she really said it


def test_a_known_mishearing_candidate_says_when_viewers_just_wrote_the_name():
    from src.autoslice.manual_lane import playbook_evidence as pe
    cue = {'id': 6, 'start_s': 36.9, 'end_s': 38.5, 'text': '问路什么是小孩'}
    chat = [{'t_s': -200., 'text': '甲乙来了'}, {'t_s': 25.7, 'text': '不让甲乙看'}, {'t_s': 30., 'text': '别的话'},
            {'t_s': 60., 'text': '甲乙在后面'}]
    assert pe.said_in_chat_before('甲乙', cue, chat) == {'chat_just_before': [{'sent_s': 25.7, 'text': '不让甲乙看'}]}
    assert pe.said_in_chat_before('KmX', cue, [{'t_s': 20., 'text': '不让kmx看'}])['chat_just_before'][0]['sent_s'] == 20.
    assert pe.said_in_chat_before('丙丁', cue, chat) == {}


def test_characters_no_other_recogniser_has_a_counterpart_for_are_listed():
    from src.autoslice.manual_lane import playbook_evidence as pe
    others = ['我也没看呀', '我也没看呀我也', '我也没看呀。']
    assert pe.written_by_one('嗯一大哥，那我也没看呀', others) == ['嗯一大哥那']
    assert pe.written_by_one('我也没看呀', others) == []
    assert pe.written_by_one('我野没看呀', others) == []                  # another wording is a counterpart, not silence
    assert pe.written_by_one('嗯，我也没看呀', ['嗯我也没看呀', '我也没看呀', '我也没看呀']) == []   # one other heard it
    assert pe.written_by_one('嗯一大哥，那我也没看呀', others[:1]) == []    # a single other recogniser proves nothing
    assert pe.written_by_one('咳咳', ['', '', '']) == [] and pe.written_by_one('咪咪', ['', '咪咪', '']) == []   # a whole cue is not a stretch inside one
    assert pe.written_by_one('嗯一大哥，那我也没看呀', ['我也没看呀', '', '我也没看呀']) == ['嗯一大哥那']   # two wrote the sentence
    assert pe.written_by_one('苏五', ['别的话', '另一句']) == []            # nothing of the cue is in the others: a whole cue again


def test_the_agent_hints_divide_the_other_recognisers_lines_among_the_cues(tmp_path, monkeypatch):
    monkeypatch.setattr(pb.mishearing, 'load_pairs', lambda *_a: [])
    answer = answer_rows([{'id': 'd001', 'state': 'ADOPT', 'final_text': '', 'drop': True, 'confidence': '高',
                           'affects_meaning': False},
                          {'id': 'd002', 'state': 'ADOPT', 'final_text': '我也没看呀', 'confidence': '高',
                           'affects_meaning': False}])
    cpa = AgentCpa(answer, tools=[{'tool': 'hints'}])
    ctx = context(tmp_path, monkeypatch, cpa, bcut='咳咳', jianying='')
    (ctx.clip_dir / 'asr/bcut.srt').write_text('1\n00:00:00,000 --> 00:00:02,000\n咳咳\n\n'
                                               '2\n00:00:02,000 --> 00:00:04,000\n嗯一大哥，那我也没看呀\n')
    (ctx.clip_dir / 'asr/jianying.srt').write_text('1\n00:00:03,000 --> 00:00:04,000\n我也没看呀\n')
    lines = {'mai': [{'start_s': 2.9, 'end_s': 6., 'text': '我也没看呀，后面的话。'}],
             'moss': [{'start_s': 3., 'end_s': 4., 'text': '我也没看呀'}]}
    monkeypatch.setattr(pb, 'other_recognitions', lambda _ctx: lines)
    assert step._step3_body(ctx).ok
    hints = {h['id']: h for h in results(ctx, 'hints')[0]['hints']}
    assert 1 not in hints and hints[2]['only_one_recogniser_wrote'] == ['嗯一大哥那']


def test_agent_prompt_carries_the_profile_principle_sections_not_a_copy(tmp_path, monkeypatch):
    from src.autoslice.manual_lane import step_io
    cpa = AgentCpa(rows('结果'))
    ctx = context(tmp_path, monkeypatch, cpa)
    assert step._step3_body(ctx).ok
    assert step_io.AGENT_PRINCIPLES and cpa.prompts[0].count(step_io.AGENT_PRINCIPLES) == 1
    assert '## 七、superchat / SC 语境' in step_io.AGENT_PRINCIPLES

    path = tmp_path / 'custom.md'
    path.write_text('## 五、外来词保留原文\n\n- 甲\n\n## 七、superchat / SC 语境\n\n- 乙\n\n## 八、其他\n', encoding='utf-8')

    class Profile:
        def asset_file(self, key):
            assert key == 'subtitle_correction_principles'
            return path
    monkeypatch.setattr(step_io, 'load_channel_profile', lambda _root: Profile())
    assert step_io._agent_principles() == ('## 五、外来词保留原文\n\n- 甲\n\n## 七、superchat / SC 语境\n\n- 乙')


def segment_xml(start, body):
    info = f'<BililiveRecorderRecordInfo roomid="1" start_time="{start}"/>'
    return f'<i>{info}{body}</i>'.encode()


NOW = segment_xml('2026-11-04T21:00:00.5+08:00', '<d p="125,1,1">普通弹幕</d>')
EARLIER = segment_xml('2026-11-04T20:30:00.5+08:00',
                      '<sc ts="964.8" user="漏掉的人">谢谢你的陪伴</sc><d p="965,1,1">旧的普通弹幕</d>')


def paid_context(tmp_path, monkeypatch, cpa, segments):
    ctx = context(tmp_path, monkeypatch, cpa)
    ctx.adapters.danmaku.xml = NOW
    ctx.adapters.danmaku.segments = dict(segments, **{'src.xml': NOW}) if segments else {}
    ctx.state.data['clip'].update(start_ms=120000, end_ms=150000)
    return ctx


def test_sc_sent_in_an_earlier_segment_is_found_on_the_cut_clock(tmp_path, monkeypatch):
    cpa = AgentCpa(rows('结果'), tools=[{'tool': 'danmaku', 'start': -3000, 'end': 10}])
    ctx = paid_context(tmp_path, monkeypatch, cpa, {'earlier.xml': EARLIER})
    assert step._step3_body(ctx).ok
    [result] = results(ctx, 'danmaku')
    assert isinstance(result, list), result
    sc = [r for r in result if r.get('type') == 'sc']
    assert [(r['sender'], r['text'], r['t_s']) for r in sc] == [('漏掉的人', '谢谢你的陪伴', -955.2)]
    assert all(r['text'] != '旧的普通弹幕' for r in result)   # ordinary comments keep their window


def test_unreadable_segment_is_unknown_to_the_agent_and_the_clip_continues(tmp_path, monkeypatch):
    cpa = AgentCpa(rows('结果'), tools=[{'tool': 'danmaku', 'start': -3000, 'end': 10}, {'tool': 'hints'}])
    ctx = paid_context(tmp_path, monkeypatch, cpa,
                       {'earlier.xml': EARLIER, 'gone.xml': OSError('read failed'), 'bare.xml': b'<i></i>'})
    assert step._step3_body(ctx).ok
    [result] = results(ctx, 'danmaku')
    assert {u['segment'] for u in result['unknown_segments']} == {'gone', 'bare'}
    assert [r['text'] for r in result['rows'] if r.get('type') == 'sc'] == ['谢谢你的陪伴']
    assert '未知' in result['note']
    [hints] = results(ctx, 'hints')
    assert len(hints['unknown_segments']) == 2


def test_paid_readout_window_reaches_back_but_ordinary_comments_keep_90_seconds():
    from src.autoslice.manual_lane import playbook_evidence as pe
    cue = {'id': 1, 'start_s': 10., 'end_s': 14., 'text': ''}
    old = {'t_s': -955., 'text': '谢谢你的陪伴'}
    assert [h['relation'] for h in pe.readout_matches(cue, ['谢谢你的陪伴'], [dict(old, type='sc', sender='甲')])] \
        == ['readout_candidate']
    assert pe.readout_matches(cue, ['谢谢你的陪伴'], [old]) == []


def test_ordinary_comment_of_the_previous_segment_is_found_when_the_cut_starts_near_a_segment_head(tmp_path, monkeypatch):
    # the previous segment started 1800 s earlier: its comment sent at 1790 s is 10 s before this segment's head
    earlier = segment_xml('2026-11-04T20:30:00.5+08:00', '<d p="1790,1,25,1">上一段尾巴的弹幕</d><d p="1000,1,25,1">太早</d>')
    cpa = AgentCpa(rows('结果'), tools=[{'tool': 'danmaku', 'start': -90, 'end': 10}])
    ctx = paid_context(tmp_path, monkeypatch, cpa, {'earlier.xml': earlier})
    ctx.state.data['clip'].update(start_ms=30000, end_ms=60000)   # the cut begins 30 s into this segment
    assert step._step3_body(ctx).ok
    [result] = results(ctx, 'danmaku')
    assert [(r['text'], r['t_s']) for r in result if 'type' not in r and '弹幕' in r['text']
            and r['text'] != '普通弹幕'] == [('上一段尾巴的弹幕', -40.0)]


def test_a_single_segment_session_reads_like_before(tmp_path, monkeypatch):
    cpa = AgentCpa(rows('结果'), tools=[{'tool': 'danmaku', 'start': -90, 'end': 10}])
    ctx = paid_context(tmp_path, monkeypatch, cpa, {})
    assert step._step3_body(ctx).ok
    [result] = results(ctx, 'danmaku')
    assert result == [{'t_s': 5.0, 'text': '普通弹幕'}]


def test_compilation_takes_what_precedes_the_first_piece_from_the_whole_session():
    from src.autoslice.manual_lane import compilation, session_events
    manifest = {'pieces': [{'recording': 'r.flv', 'source_start_ms': 30000, 'source_end_ms': 90000,
                            'compiled_start_ms': 0}]}
    own = segment_xml('2026-11-04T21:00:00+08:00', '<d p="10,1,25,1">本段早先弹幕</d>')
    earlier = segment_xml('2026-11-04T20:30:00+08:00',
                          '<sc ts="964.8" user="漏掉的人">谢谢你的陪伴</sc><d p="1790,1,25,1">上一段尾巴</d>')

    class Source:
        def names(self, _):
            return ['earlier.xml', 'r.xml']

        def fetch(self, path):
            return earlier

    rows_, unknown = session_events.load(Source(), 'd', 'r', own)
    assert unknown == []
    xml = compilation.remap_danmaku(manifest, {'r.flv': own.decode()}, rows_)
    assert '谢谢你的陪伴' in xml and '上一段尾巴' in xml and '本段早先弹幕' in xml
