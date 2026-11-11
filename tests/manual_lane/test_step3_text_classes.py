"""Ordinary-entry synthetic checks for table projection, source chat and sentence timing."""
import json
from dataclasses import replace

from fakes import FakeRemote, make_ctx
from agent_fakes import AgentCpa
from src.autoslice.manual_lane import name_match
from src.autoslice.manual_lane import step3_subtitles as step
from src.autoslice.manual_lane.srt_text import parse_srt
from src.autoslice.manual_lane import normalize
from src.autoslice.manual_lane import step3_adjudicate
from src.autoslice import term_authority
from src.autoslice.subtitle_validation import validate_srt_file


def make_single(tmp_path, monkeypatch, srt, words, rows, xml=b'<i/>'):
    cpa = AgentCpa(rows, tools=[{'tool': 'words', 'start': 0, 'end': 70},
                                {'tool': 'danmaku', 'start': -10, 'end': 70}, {'tool': 'spelling'}])
    ctx = make_ctx(tmp_path, remote=FakeRemote(), cpa=cpa, danmaku_xml=xml)
    ctx.session['participants'] = [['单人']]
    for name in ('bcut', 'jianying'):
        path = ctx.clip_dir / f'asr/{name}.srt'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(srt)
    (ctx.clip_dir / 'asr/jianying.json').write_text(json.dumps({'utterances': [{'words': words}]}, ensure_ascii=False))
    monkeypatch.setenv('MANUAL_LANE_ADJUDICATE_MODE', 'C')
    return ctx, cpa


def word(label, start, end):
    return {'label': label, 'start_time': start, 'end_time': end}


def decision(id, text, parts):
    return {'id': id, 'state': 'ADOPT', 'final_text': text, 'confidence': '高',
            'affects_meaning': False, 'parts': parts}


def test_readout_and_reaction_use_actual_word_spans(tmp_path, monkeypatch):
    srt = '1\n00:00:00,000 --> 00:00:04,000\n今晚要喝奶茶我也想喝\n'
    words = [word('今晚', 0, 400), word('要喝', 450, 900), word('奶茶', 950, 1500),
             word('我也', 1700, 2200), word('想喝', 2250, 2800)]
    parts = [{'text': '今晚要喝奶茶', 'first_word': 0, 'last_word': 2},
             {'text': '我也想喝', 'first_word': 3, 'last_word': 4}]
    xml = '<i><d p="9,1,1">今晚要喝奶茶</d><d p="9.5,1,1">今晚要喝咖啡</d></i>'.encode()
    ctx, cpa = make_single(tmp_path, monkeypatch, srt, words,
                           [decision('d001', '今晚要喝奶茶我也想喝', parts)], xml)
    assert step._step3_body(ctx).ok
    cues = parse_srt((ctx.clip_dir / 'subs/final.srt').read_text())
    assert [(c['start_s'], c['end_s'], c['text']) for c in cues] == [
        (0., 1.5, '今晚要喝奶茶'), (1.7, 2.8, '我也想喝')]
    assert '今晚要喝咖啡' in cpa.prompts[1] and all('咖啡' not in c['text'] for c in cues)
    assert len(cpa.calls) == 3


def test_words_move_between_neighbouring_cues_with_observed_timing(tmp_path, monkeypatch):
    srt = ('1\n00:00:00,000 --> 00:00:02,000\n今天很好但是\n\n'
           '2\n00:00:02,000 --> 00:00:04,000\n你觉得呢\n')
    words = [word('今天', 0, 400), word('很好', 500, 1000), word('但是', 1500, 1900),
             word('你觉得', 2100, 2600), word('呢', 2700, 2900)]
    rows = [decision('d001', '今天很好', [{'text': '今天很好', 'first_word': 0, 'last_word': 1}]),
            decision('d002', '但是你觉得呢', [{'text': '但是你觉得呢', 'first_word': 2, 'last_word': 4}])]
    ctx, _cpa = make_single(tmp_path, monkeypatch, srt, words, rows)
    assert step._step3_body(ctx).ok
    cues = parse_srt((ctx.clip_dir / 'subs/final.srt').read_text())
    assert [(c['start_s'], c['end_s'], c['text']) for c in cues] == [
        (0., 1., '今天很好'), (1.5, 2.9, '但是你觉得呢')]


def test_invalid_or_overlapping_word_spans_are_not_invented(tmp_path, monkeypatch):
    srt = '1\n00:00:00,000 --> 00:00:02,000\n今天很好\n'
    rows = [decision('d001', '今天很好', [{'text': '今天很好', 'first_word': 0, 'last_word': 4}])]
    ctx, cpa = make_single(tmp_path, monkeypatch, srt, [word('今天', 0, 400)], rows)
    corrected = {'id': 'd001', 'state': 'ADOPT', 'final_text': '今天很好', 'drop': False,
                 'confidence': '高', 'affects_meaning': False, 'reason': '保留原句时间'}
    cpa.responses.append(json.dumps({'action': 'final', 'self_check': '修正无效时间', 'rows': [corrected]},
                                    ensure_ascii=False))
    assert step._step3_body(ctx).ok
    assert 'selected word span was not supplied' in cpa.prompts[-1]
    assert parse_srt((ctx.clip_dir / 'subs/final.srt').read_text())[0]['text'] == '今天很好'


def test_source_word_boundary_trims_adjacent_original_cue_padding(tmp_path, monkeypatch):
    srt = ('1\n00:00:00,000 --> 00:00:02,060\n前一句\n\n'
           '2\n00:00:02,060 --> 00:00:04,000\n下一句\n')
    words = [word('前一句', 0, 2000), word('下一句', 2000, 3000)]
    rows = [{'id': 'd001', 'state': 'KEEP', 'final_text': '前一句', 'confidence': '高', 'affects_meaning': False},
            decision('d002', '下一句', [{'text': '下一句', 'first_word': 1, 'last_word': 1}])]
    ctx, _cpa = make_single(tmp_path, monkeypatch, srt, words, rows)
    assert step._step3_body(ctx).ok
    cues = parse_srt((ctx.clip_dir / 'subs/final.srt').read_text())
    assert [(c['start_s'], c['end_s']) for c in cues] == [(0., 2.), (2., 3.)]


def test_missing_word_times_can_merge_complete_original_cues(tmp_path, monkeypatch):
    srt = ('1\n00:00:00,000 --> 00:00:01,000\n今晚要\n\n'
           '2\n00:00:01,000 --> 00:00:02,000\n喝奶茶\n\n'
           '3\n00:00:02,000 --> 00:00:03,000\n我也想喝\n')
    rows = [decision('d001', '今晚要喝奶茶', [{'text': '今晚要喝奶茶', 'first_cue': 1, 'last_cue': 2}]),
            {'id': 'd002', 'state': 'ADOPT', 'final_text': '喝奶茶', 'drop': True,
             'confidence': '高', 'affects_meaning': False, 'parts': []},
            {'id': 'd003', 'state': 'KEEP', 'final_text': '我也想喝', 'confidence': '高', 'affects_meaning': False}]
    ctx, cpa = make_single(tmp_path, monkeypatch, srt, [], rows)
    assert step._step3_body(ctx).ok
    cues = parse_srt((ctx.clip_dir / 'subs/final.srt').read_text())
    assert [(c['start_s'], c['end_s'], c['text']) for c in cues] == [
        (0., 2., '今晚要喝奶茶'), (2., 3., '我也想喝')]
    assert '没有逐词时间' in cpa.prompts[1]


def test_unresolved_transferred_cue_remains_a_hold_without_duplicate_output(tmp_path, monkeypatch):
    srt = ('1\n00:00:00,000 --> 00:00:01,000\n今晚要\n\n'
           '2\n00:00:01,000 --> 00:00:02,000\n喝奶茶\n')
    rows = [decision('d001', '今晚要喝奶茶', [{'text': '今晚要喝奶茶', 'first_cue': 1, 'last_cue': 2}]),
            {'id': 'd002', 'state': 'UNRESOLVED', 'final_text': '', 'drop': True,
             'confidence': '低', 'affects_meaning': True, 'parts': []}]
    ctx, _cpa = make_single(tmp_path, monkeypatch, srt, [], rows)
    assert step._step3_body(ctx).ok
    cues = parse_srt((ctx.clip_dir / 'subs/final.srt').read_text())
    assert [cue['text'] for cue in cues] == ['今晚要喝奶茶']
    accounting = json.loads((ctx.clip_dir / 'subs/playbook-accounting.json').read_text())
    assert [hold['id'] for hold in accounting['holds']] == ['d002']


def test_partitioned_reading_does_not_add_a_comma_to_cpa_text(tmp_path, monkeypatch):
    srt = '1\n00:00:00,000 --> 00:00:03,500\n茶是热的吗谁知道呢\n'
    words = [word('茶是', 0, 500), word('热的吗', 550, 1600),
             word('谁', 1800, 2100), word('知道呢', 2200, 3000)]
    parts = [{'text': '茶是热的吗', 'first_word': 0, 'last_word': 1},
             {'text': '谁', 'first_word': 2, 'last_word': 2},
             {'text': '知道呢', 'first_word': 3, 'last_word': 3}]
    ctx, _ = make_single(tmp_path, monkeypatch, srt, words,
                         [decision('d001', '茶是热的吗谁知道呢', parts)])
    assert step._step3_body(ctx).ok
    path = ctx.clip_dir / 'subs/final.srt'
    cues = parse_srt(path.read_text())
    assert [c['text'] for c in cues] == ['茶是热的吗谁知道呢']
    assert validate_srt_file(path, media_duration_ms=3500)['status'] == 'PASS'


def test_partitioned_reading_uses_cpa_comma_when_joining_lone_character(tmp_path, monkeypatch):
    srt = '1\n00:00:00,000 --> 00:00:03,500\n茶是热的吗谁知道呢\n'
    words = [word('茶是', 0, 500), word('热的吗', 550, 1600),
             word('谁', 1800, 2100), word('知道呢', 2200, 3000)]
    parts = [{'text': '茶是热的吗', 'first_word': 0, 'last_word': 1},
             {'text': '谁，', 'first_word': 2, 'last_word': 2},
             {'text': '知道呢', 'first_word': 3, 'last_word': 3}]
    ctx, _ = make_single(tmp_path, monkeypatch, srt, words,
                         [decision('d001', '茶是热的吗谁，知道呢', parts)])
    assert step._step3_body(ctx).ok
    path = ctx.clip_dir / 'subs/final.srt'
    assert [c['text'] for c in parse_srt(path.read_text())] == ['茶是热的吗', '谁，知道呢']
    assert validate_srt_file(path, media_duration_ms=3500)['status'] == 'PASS'


def test_ordinary_partitioned_path_shapes_long_decision_and_uses_context_pronouns(tmp_path, monkeypatch):
    from src.autoslice.manual_lane import subtitle_track

    text = '他们打算在10月21号一起看《晚风中的花园》，然后回大学校园找朋友'
    srt = f'1\n00:00:00,000 --> 00:00:06,000\n{text}\n'
    parts = [{'text': text, 'first_word': 0, 'last_word': 0}]
    ctx, cpa = make_single(tmp_path, monkeypatch, srt, [word(text, 0, 6000)],
                           [decision('d001', text, parts)])
    assert step._step3_body(ctx).ok
    cues = subtitle_track.parse_blocks((ctx.clip_dir / 'subs/final.srt').read_text())
    assert ''.join(''.join(c['text'].split()) for c in cues) == text
    assert all(len(line) <= 18 for cue in cues for line in cue['text'].splitlines())
    assert any('《晚风中的花园》' in line for cue in cues for line in cue['text'].splitlines())
    from src.autoslice.manual_lane.step_io import PRONOUN_RULE
    assert PRONOUN_RULE in cpa.prompts[0]
    assert '指人第三人称写她' not in cpa.prompts[0]


def test_unjoinable_single_character_keeps_the_unsplit_spoken_text(tmp_path, monkeypatch):
    srt = '1\n00:00:00,000 --> 00:00:03,000\n谁会来呢\n'
    words = [word('谁', 0, 300), word('会来呢', 1800, 2600)]
    parts = [{'text': '谁', 'first_word': 0, 'last_word': 0},
             {'text': '会来呢', 'first_word': 1, 'last_word': 1}]
    ctx, _ = make_single(tmp_path, monkeypatch, srt, words,
                         [decision('d001', '谁会来呢', parts)])
    assert step._step3_body(ctx).ok
    path = ctx.clip_dir / 'subs/final.srt'
    assert [c['text'] for c in parse_srt(path.read_text())] == ['谁会来呢']
    assert validate_srt_file(path, media_duration_ms=3000)['status'] == 'PASS'


def test_partition_shaping_cannot_rewrite_cpa_words(tmp_path, monkeypatch):
    text = '第二十句'
    srt = f'1\n00:00:00,000 --> 00:00:02,000\n{text}\n'
    parts = [{'text': text, 'first_word': 0, 'last_word': 0}]
    ctx, _ = make_single(tmp_path, monkeypatch, srt, [word(text, 0, 2000)],
                         [decision('d001', text, parts)])
    monkeypatch.setattr(normalize, 'release_shape', lambda *_a, **_k: (
        [{'id': 1, 'start_s': 0., 'end_s': 2., 'text': '第20句'}], []))
    assert step._step3_body(ctx).ok
    assert parse_srt((ctx.clip_dir / 'subs/final.srt').read_text())[0]['text'] == text


def test_parts_that_omit_cpa_text_return_to_cpa_for_revision(tmp_path, monkeypatch):
    text = '今天很好'
    srt = f'1\n00:00:00,000 --> 00:00:02,000\n{text}\n'
    rows = [decision('d001', text, [{'text': '今天', 'first_word': 0, 'last_word': 0}])]
    ctx, cpa = make_single(tmp_path, monkeypatch, srt, [word(text, 0, 2000)], rows)
    corrected = {'id': 'd001', 'state': 'KEEP', 'final_text': text, 'drop': False,
                 'confidence': '高', 'affects_meaning': False, 'reason': '保留全部口播'}
    cpa.responses.append(json.dumps({'action': 'final', 'self_check': '修正遗漏', 'rows': [corrected]},
                                    ensure_ascii=False))
    assert step._step3_body(ctx).ok
    assert 'selected parts do not preserve CPA final text' in cpa.prompts[-1]
    assert parse_srt((ctx.clip_dir / 'subs/final.srt').read_text())[0]['text'] == text


def test_one_added_table_row_reaches_both_step3_spelling_paths(tmp_path, monkeypatch):
    import test_task40_other_speaker_dup as multi_case

    table_path = tmp_path / 'proper_nouns.json'
    table = json.loads(name_match.PROPER_NOUNS.read_text())
    table_path.write_text(json.dumps(table, ensure_ascii=False))
    monkeypatch.setattr(name_match, 'PROPER_NOUNS', table_path)
    monkeypatch.setattr(step3_adjudicate, 'PROPER_NOUNS', table_path)
    monkeypatch.setattr(term_authority, 'CHANNEL_PROFILE',
                        replace(term_authority.CHANNEL_PROFILE, spelling_table_path=table_path))

    class TableCpa(AgentCpa):
        def __init__(self):
            super().__init__([], answers=[])

        def ask(self, *, call_id, prompt_text, model, effort, remote_dir, tries=1, max_output_tokens=None):
            if not self.calls:
                answer = {'action': 'plan', 'doubts': [], 'tools': [{'tool': 'spelling'}]}
            else:
                events = json.loads(prompt_text.split('此前代理和工具轨迹：\n', 1)[1])
                table = next(e['result'] for e in events if e['event'] == 'tool_result')
                result = next((row['canonical'] for row in table if '彩羽团' in row['variants']), '彩羽团')
                row = {'id': 'd001', 'state': 'ADOPT', 'final_text': result, 'drop': False,
                       'confidence': '高', 'affects_meaning': False, 'reason': 'test table'}
                answer = {'action': 'draft', 'rows': [row]} if len(self.calls) == 1 else {
                    'action': 'final', 'self_check': 'checked', 'rows': [row]}
            self.responses = [json.dumps(answer, ensure_ascii=False)]
            return super().ask(call_id=call_id, prompt_text=prompt_text, model=model,
                               effort=effort, remote_dir=remote_dir, tries=tries,
                               max_output_tokens=max_output_tokens)

    def single(folder):
        srt = '1\n00:00:00,000 --> 00:00:02,000\n彩羽团\n'
        ctx, _ = make_single(folder, monkeypatch, srt, [], [])
        cpa = TableCpa()
        ctx.adapters.cpa = cpa
        assert step._step3_body(ctx).ok
        assert len(cpa.calls) == 3
        return parse_srt((ctx.clip_dir / 'subs/final.srt').read_text())[0]['text']

    def multi(folder):
        monkeypatch.setattr(multi_case, 'CUES', [(0., 2., '彩羽团'), (2., 4., '今天工作'),
                                                (4., 6., '明天出门')])
        _, ctx = multi_case._run(folder, monkeypatch, [], 'C')
        text = parse_srt((ctx.clip_dir / 'subs/final.srt').read_text())[0]['text']
        return text, '\n'.join(ctx.adapters.cpa.prompts)

    assert single(tmp_path / 'before') == '彩羽团'
    assert normalize.canon_text('彩羽团')[0] == '彩羽团'
    assert multi(tmp_path / 'multi-before')[0] == '彩羽团'
    table.append({'name': 'cyt', 'aliases': [], 'role': '测试词形', 'detection_name': False,
                  'canonical': 'cyt', 'variants': ['彩羽团', 'cyt'],
                  'legacy_equivalences': ['彩羽团'], 'condition': '确指该词', 'source': 'test'})
    table_path.write_text(json.dumps(table, ensure_ascii=False))
    assert single(tmp_path / 'after') == 'cyt'
    assert normalize.canon_text('彩羽团')[0] == 'cyt'
    multi_text, multi_prompt = multi(tmp_path / 'multi-after')
    assert multi_text == 'cyt'
    assert '规范词形：cyt' in multi_prompt


def test_multi_speaker_prompt_uses_the_tables_rejected_spellings(tmp_path, monkeypatch):
    table = tmp_path / 'proper_nouns.json'
    table.write_text(json.dumps([{'canonical': '新称呼', 'variants': ['新称呼'],
                                  'condition': '指该称呼', 'rejected_variants': ['错误词形']}],
                                ensure_ascii=False))
    monkeypatch.setattr(step3_adjudicate, 'PROPER_NOUNS', table)
    assert '规范词形：新称呼' in step3_adjudicate.render_proper_nouns()
    assert '不要写成：错误词形' in step3_adjudicate.render_proper_nouns()
