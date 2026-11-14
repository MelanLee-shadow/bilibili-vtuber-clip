"""Ordinary-entry request regressions using synthetic profile and audio evidence."""
import json

import pytest

from fakes import FakeRemote, make_ctx
from agent_fakes import AgentCpa
from src.autoslice.manual_lane import step3_subtitles as step


def single_ctx(tmp_path, monkeypatch, text='今天喝茶'):
    cpa = AgentCpa([{'id': 'd001', 'state': 'KEEP', 'final_text': text,
                     'confidence': '高', 'affects_meaning': False}], tools=[{'tool': 'spelling'}])
    ctx = make_ctx(tmp_path, remote=FakeRemote(), cpa=cpa, danmaku_xml=b'<i/>')
    ctx.session['participants'] = [['single synthetic speaker']]
    for name in ('bcut', 'jianying'):
        path = ctx.clip_dir / f'asr/{name}.srt'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f'1\n00:00:00,000 --> 00:00:02,000\n{text}\n')
    monkeypatch.setenv('MANUAL_LANE_ADJUDICATE_MODE', 'C')
    return ctx, cpa


@pytest.mark.parametrize('canonical,variant', [('青岚', '青蓝'), ('朝露', '潮露')])
def test_spelling_table_reaches_the_one_cpa_request(tmp_path, monkeypatch, canonical, variant):
    from src.autoslice.manual_lane import name_match
    table = tmp_path / 'spelling.json'
    entry = {'name': canonical, 'aliases': [], 'role': '测试', 'canonical': canonical,
             'variants': [variant], 'condition': '确指这个名字', 'source': 'synthetic'}
    table.write_text(json.dumps([entry], ensure_ascii=False))
    monkeypatch.setattr(name_match, 'PROPER_NOUNS', table)
    name_match.table_rows.cache_clear()
    ctx, cpa = single_ctx(tmp_path, monkeypatch)
    assert step._step3_body(ctx).ok
    assert len(cpa.prompts) == 3
    trace = json.loads((ctx.clip_dir / 'subs/agent-trace.json').read_text())
    result = next(e['result'] for e in trace['events'] if e['event'] == 'tool_result')
    assert result == [{k: entry[k] for k in ('canonical', 'variants', 'condition', 'source')}]
    assert canonical in cpa.prompts[1] and variant in cpa.prompts[1]
    assert '指称不确定时不套表' in cpa.prompts[0]
    assert 'profile_name_table' not in result[0]
    assert 'mishearing' not in result[0]
    name_match.table_rows.cache_clear()


def test_one_tagged_listening_version_singing_rule_reaches_request(tmp_path, monkeypatch):
    ctx, cpa = single_ctx(tmp_path, monkeypatch)
    assert step._step3_body(ctx).ok
    assert len(cpa.prompts) == 3
    prompt = cpa.prompts[0]
    assert '一个听写版本标【唱】或【哼唱】就够' in prompt
    assert 'ASR 不是连贯说话' in prompt and '语境指向既有歌曲' in prompt
    assert '两 ASR 都是完整说话时保留' in prompt
    assert '即兴自编词即使有旋律仍保留' in prompt
    assert '无词哼唱一律不出字幕' in prompt
    assert '两版同标【唱】' not in prompt
