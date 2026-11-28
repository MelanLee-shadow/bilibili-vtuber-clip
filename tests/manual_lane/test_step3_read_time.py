"""Read-time witness: words and independently timed ASR, not letter-count padding."""
from types import SimpleNamespace

from agent_fakes import answer_rows
from src.autoslice.manual_lane import evidence_attach, playbook_step3 as pb
from src.autoslice.manual_lane.srt_text import parse_srt
from src.autoslice.subtitle_validation import validate_srt_text

BCUT = '''1
00:00:00,000 --> 00:00:01,680
春风不浪意味

2
00:00:01,720 --> 00:00:03,120
李若思维那一句

3
00:00:03,120 --> 00:00:05,080
楼下本来就知道是一句晚川话

4
00:00:05,080 --> 00:00:08,800
那到底谁到底是在什么情况下说的那句话
'''
JIANYING = '''1
00:00:00,000 --> 00:00:01,680
人们本以为

2
00:00:01,840 --> 00:00:03,160
李莫思那一句

3
00:00:03,160 --> 00:00:04,400
楼上本来就很多

4
00:00:04,400 --> 00:00:05,120
是一句玩笑话

5
00:00:05,120 --> 00:00:08,760
那到底谁那是在什么情况下说的那句话
'''
TEXT = '人们本以为，lmsm那一句李豆沙本来就是猪啊是一句玩笑话'


def witness_agent(tmp_path):
    bcut, jy = parse_srt(BCUT), parse_srt(JIANYING)
    agent = object.__new__(pb._Agent)
    agent.bcut, agent.jianying, agent.others = bcut, jy, {}
    agent.jy = evidence_attach.attach_asr(bcut, evidence_attach.segments_from_srt_cues(jy), source='jianying')
    agent.ctx, agent.clip, agent.cut_len, agent.words = SimpleNamespace(clip_dir=tmp_path), {'start_ms': 0}, 10., None
    agent.trace = {'events': [{'event': 'cpa_result', 'call_id': 'witness'}]}
    rows = answer_rows([
        {'id': 'd001', 'state': 'ADOPT', 'final_text': TEXT, 'confidence': '高', 'affects_meaning': False},
        {'id': 'd002', 'state': 'ADOPT', 'final_text': '', 'drop': True, 'confidence': '高', 'affects_meaning': False},
        {'id': 'd003', 'state': 'ADOPT', 'final_text': '', 'drop': True, 'confidence': '高', 'affects_meaning': False},
        {'id': 'd004', 'state': 'KEEP', 'final_text': '那到底谁到底是在什么情况下说的那句话', 'confidence': '高', 'affects_meaning': False},
    ])
    return agent, rows


def test_release_check_flags_the_recorded_short_witness():
    result = validate_srt_text(f'1\n00:00:00,000 --> 00:00:01,680\n{TEXT}\n', media_duration_ms=5000)
    assert any(e['code'] == 'SRT_CUE_READ_TOO_FAST' for e in result['errors'])


def test_missing_parts_uses_independent_asr_timing_without_changing_text(tmp_path):
    agent, rows = witness_agent(tmp_path)
    lines, _, _ = agent.layout(rows)
    assert (lines[0]['start_s'], lines[0]['end_s'], lines[0]['text']) == (0., 5.08, TEXT)
    assert lines[0]['end_s'] <= lines[1]['start_s']


def test_no_support_does_not_pad_and_is_returned_to_the_agent(tmp_path):
    agent, rows = witness_agent(tmp_path)
    agent.jianying = []
    agent.event = lambda kind, **data: agent.trace['events'].append({'event': kind, **data})
    before, _, _ = agent.layout(rows)
    assert round(before[0]['end_s'], 3) == 1.68
    assert not agent.final_stands(rows, None)
    assert any(event.get('read_too_fast') for event in agent.trace['events'])


def test_word_times_take_precedence_over_the_other_transcript(tmp_path):
    agent, rows = witness_agent(tmp_path)
    agent.words = [dict(c) for c in parse_srt(JIANYING)]
    agent.words[3]['end_s'] = 5.0
    lines, _, _ = agent.layout(rows)
    assert lines[0]['end_s'] == 5.0
    assert agent.read_time_repairs[0]['source'] == 'jianying_words'


def test_evidence_that_crosses_a_neighbour_is_not_borrowed(tmp_path):
    agent, rows = witness_agent(tmp_path)
    agent.jianying[3]['end_s'] = 5.5
    lines, _, _ = agent.layout(rows)
    assert round(lines[0]['end_s'], 3) == 1.68
    assert agent.read_time_repairs == []


def test_normal_rate_is_never_extended_just_because_asr_has_words(tmp_path):
    agent, rows = witness_agent(tmp_path)
    rows[0]['final_text'] = '人们本以为'
    lines, _, _ = agent.layout(rows)
    assert round(lines[0]['end_s'], 3) == 1.68
    assert agent.read_time_repairs == []


def test_original_agent_entry_writes_the_evidence_bound_time_and_receipt(tmp_path, monkeypatch):
    import json

    from agent_fakes import AgentCpa
    from fakes import FakeRemote, make_ctx
    from src.autoslice.manual_lane import step3_subtitles as step

    _, rows = witness_agent(tmp_path)
    cpa = AgentCpa(rows)
    ctx = make_ctx(tmp_path, remote=FakeRemote(), cpa=cpa)
    ctx.session['participants'] = [['single speaker']]
    for name, text in [('bcut', BCUT), ('jianying', JIANYING)]:
        path = ctx.clip_dir / f'asr/{name}.srt'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding='utf-8')
    monkeypatch.setattr(pb, 'samples', lambda: 1)
    monkeypatch.setattr(pb, 'other_recognitions', lambda _ctx: {})
    outcome = step._step3_body(ctx)
    assert outcome.ok
    final = (ctx.clip_dir / 'subs/final.srt').read_text(encoding='utf-8')
    assert '00:00:00,000 --> 00:00:05,080' in final
    assert ''.join(TEXT.split()) in ''.join(final.split())
    receipt = json.loads((ctx.clip_dir / 'subs/cue_read_time.json').read_text(encoding='utf-8'))
    assert receipt[0]['source'] == 'jianying' and receipt[0]['new'] == [0., 5.08]


def test_character_word_times_are_usable_as_an_anchor(tmp_path):
    agent, rows = witness_agent(tmp_path)
    agent.words = [dict(c) for c in parse_srt(JIANYING)[:3]]
    for i, ch in enumerate('是一句玩笑话'):
        agent.words.append({'start_s': 4.4 + i * .1, 'end_s': 4.5 + i * .1, 'text': ch})
    lines, _, _ = agent.layout(rows)
    assert round(lines[0]['end_s'], 3) == 5.0
    assert agent.read_time_repairs[0]['source'] == 'jianying_words'


def test_evidence_can_extend_a_sentence_backwards_without_touching_the_previous_line():
    from src.autoslice.manual_lane.step3_read_time import repair

    cues = [{'id': 1, 'start_s': 0., 'end_s': .5, 'text': '上一句'},
            {'id': 2, 'start_s': 2., 'end_s': 3., 'text': '这里有一段特别长的话需要让大家读完'}]
    source = [{'start_s': .4, 'end_s': 1.8, 'text': '这里有一段'},
              {'start_s': 2., 'end_s': 3., 'text': '特别长的话需要让大家读完'}]
    result, changes = repair(cues, [('independent', source)], 4.)
    assert (result[1]['start_s'], result[1]['end_s']) == (.5, 3.)
    assert result[1]['text'] == cues[1]['text'] and result[0] == cues[0]
    assert changes[0]['source'] == 'independent'


def test_a_matching_prefix_does_not_extend_over_an_unrelated_asr_tail(tmp_path):
    agent, rows = witness_agent(tmp_path)
    agent.jianying[3]['text'] += '谢谢大家另外有一件事'
    lines, _, _ = agent.layout(rows)
    assert round(lines[0]['end_s'], 3) == 1.68
    assert agent.read_time_repairs == []


def test_agent_is_asked_for_evidence_before_an_unsupported_line_is_accepted(tmp_path, monkeypatch):
    import copy
    import json

    from agent_fakes import AgentCpa
    from fakes import FakeRemote, make_ctx
    from src.autoslice.manual_lane import step3_subtitles as step

    _, initial = witness_agent(tmp_path)
    corrected = copy.deepcopy(initial)
    corrected[0]['parts'] = [{'text': TEXT, 'first_cue': 1, 'last_cue': 3}]
    answers = [{'action': 'plan', 'doubts': [], 'tools': [{'tool': 'words', 'start': 0, 'end': 10}]},
               {'action': 'draft', 'rows': initial},
               {'action': 'final', 'self_check': '初稿', 'rows': initial},
               {'action': 'final', 'self_check': '识别句 1 至 3 的时间', 'rows': corrected}]
    cpa = AgentCpa([], answers=answers)
    ctx = make_ctx(tmp_path, remote=FakeRemote(), cpa=cpa)
    ctx.session['participants'] = [['single speaker']]
    for name, text in [('bcut', BCUT), ('jianying', BCUT.replace('春风不浪意味', '完全不同'))]:
        path = ctx.clip_dir / f'asr/{name}.srt'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding='utf-8')
    monkeypatch.setattr(pb, 'samples', lambda: 1)
    monkeypatch.setattr(pb, 'other_recognitions', lambda _ctx: {})
    assert step._step3_body(ctx).ok
    events = json.loads((ctx.clip_dir / 'subs/agent-trace.json').read_text())['events']
    assert any(event.get('read_too_fast') for event in events)
    assert 'read_too_fast' in cpa.prompts[-1]
    assert '00:00:00,000 --> 00:00:05,080' in (ctx.clip_dir / 'subs/final.srt').read_text()


def test_an_unrelated_existing_short_line_uses_its_character_word_times():
    from src.autoslice.manual_lane.step3_read_time import repair

    cues = [{'id': 21, 'start_s': 48.44, 'end_s': 49.22,
             'text': '禁止。好了，大家，今天直播就到这里结束了。'},
            {'id': 22, 'start_s': 51.72, 'end_s': 55.7, 'text': '后面全都是这个。'}]
    words = [{'start_s': a, 'end_s': b, 'text': t} for a, b, t in [
        (48.44, 48.68, '禁止'), (48.68, 48.8, '好'), (48.8, 48.92, '了'), (48.92, 49.24, '大家'),
        (49.28, 49.52, '今天'), (49.6, 49.88, '直播'), (49.88, 50.04, '就'), (50.04, 50.16, '到'),
        (50.16, 50.4, '这里'), (50.4, 50.88, '结束'), (50.88, 51.64, '哈哈哈')]]
    result, changes = repair(cues, [('jianying_words', words)], 60.)
    assert (result[0]['start_s'], result[0]['end_s']) == (48.44, 50.88)
    assert result[0]['text'] == cues[0]['text'] and result[1] == cues[1]
    assert changes[0]['anchors'][0]['text'] == '今天直播就到这里结束'


def test_packed_fast_line_is_not_returned_to_agent(tmp_path):
    agent, _ = witness_agent(tmp_path)
    agent.layout = lambda _answers: ([
        {'id': 'd001', 'start_s': 0., 'end_s': 1., 'text': '前一句话'},
        {'id': 'd002', 'start_s': 1., 'end_s': 1.405, 'text': '然后自己在那'},
        {'id': 'd003', 'start_s': 1.405, 'end_s': 2.5, 'text': '后一句话'},
    ], [], False)
    assert agent.unreadable([]) == []
