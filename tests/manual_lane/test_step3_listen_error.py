"""A failed listening (provider error, no receipt) goes back to CPA as a tool result; it does not refuse the clip."""
import hashlib
import json
import threading

import pytest

from agent_fakes import AgentCpa, answer_rows
from fakes import FakeRemote, make_ctx
from src.autoslice.manual_lane import playbook_step3 as pb, step3_subtitles as step

REQUEST = {'tool': 'listen', 'start': 0, 'end': 2, 'cue_ids': [1], 'reason': '核实读音', 'model': '38m'}
ERROR = {'status': 'ERROR', 'lines': [], 'error': 'CpaGeminiError: CPA_GEMINI_EMPTY_RESPONSE', 'attempts': 3}


def row(text):
    return answer_rows([{'id': 'd001', 'state': 'ADOPT', 'final_text': text, 'confidence': '高',
                         'affects_meaning': False}])


def context(tmp_path, monkeypatch, cpa):
    ctx = make_ctx(tmp_path, remote=FakeRemote(), cpa=cpa)
    ctx.session['participants'] = [['single speaker']]
    for name, text in [('bcut', '原句'), ('jianying', '另一句')]:
        path = ctx.clip_dir / f'asr/{name}.srt'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f'1\n00:00:00,000 --> 00:00:02,000\n{text}\n')
    monkeypatch.setenv('MANUAL_LANE_ADJUDICATE_MODE', 'C')
    monkeypatch.setattr(pb, 'upload_listen_tools', lambda *_args: None)
    return ctx


def trace_of(ctx):
    return json.loads((ctx.clip_dir / 'subs/agent-trace.json').read_text())


def test_error_without_receipt_is_returned_to_cpa_and_the_clip_continues(tmp_path, monkeypatch):
    cpa = AgentCpa([], answers=[{'action': 'plan', 'doubts': [], 'tools': [REQUEST]},
                                {'action': 'tools', 'tools': []},
                                {'action': 'draft', 'rows': row('草稿')},
                                {'action': 'final', 'self_check': '听写失败，按文字证据定', 'rows': row('结果')}])
    ctx = context(tmp_path, monkeypatch, cpa)
    monkeypatch.setattr(pb, '_listen', lambda _ctx, windows, *_a: [dict(ERROR, win=windows[0]['win'])])
    assert step._step3_body(ctx).ok
    trace = trace_of(ctx)
    result = next(e['result'] for e in trace['events'] if e['event'] == 'tool_result')
    assert result['usage_unavailable_on_error'] is True and result['usage'] is None
    assert result['record']['error'] == 'CpaGeminiError: CPA_GEMINI_EMPTY_RESPONSE'
    assert result['window']['audio'] == [0.0, 2.0] and 'CPA_GEMINI_EMPTY_RESPONSE' in cpa.prompts[1]
    assert trace['status'] == 'COMPLETE' and trace['listening_calls'] == {'38m': 1, 'argon': 0}
    assert 'completed_listens' not in trace and trace['total_tokens'] == 40     # four text turns, listening 0
    assert '结果' in (ctx.clip_dir / 'subs/final.srt').read_text()


def test_failed_listening_counts_against_the_listening_cap(tmp_path, monkeypatch):
    monkeypatch.setattr(pb, 'MAX_LISTEN_CALLS', 1)
    cpa = AgentCpa([], answers=[{'action': 'plan', 'doubts': [], 'tools': [REQUEST, REQUEST]}])
    ctx = context(tmp_path, monkeypatch, cpa)
    monkeypatch.setattr(pb, '_listen', lambda _ctx, windows, *_a: [dict(ERROR, win=windows[0]['win'])])
    assert step._step3_body(ctx).reason == 'CPA_AGENT_CALL_CAP'
    assert trace_of(ctx)['listening_calls'] == {'38m': 1, 'argon': 0}


def test_ok_record_without_receipt_still_refuses_by_name(tmp_path, monkeypatch):
    cpa = AgentCpa([], answers=[{'action': 'plan', 'doubts': [], 'tools': [REQUEST]}])
    ctx = context(tmp_path, monkeypatch, cpa)
    monkeypatch.setattr(pb, '_listen', lambda *_args: [{'status': 'OK', 'lines': []}])
    outcome = step._step3_body(ctx)
    assert outcome.reason == 'CPA_AGENT_USAGE_UNKNOWN' and trace_of(ctx)['refusal'] == outcome.reason
    assert len(cpa.calls) == 1


def test_interrupted_run_resumes_over_a_failed_listening_without_usage(tmp_path, monkeypatch):
    cpa = AgentCpa([], answers=[{'action': 'plan', 'doubts': [], 'tools': [REQUEST]},
                                {'action': 'draft', 'rows': row('草稿')},
                                {'action': 'final', 'self_check': '复查', 'rows': row('结果')}])
    ctx = context(tmp_path, monkeypatch, cpa)
    monkeypatch.setattr(pb, '_listen', lambda _ctx, windows, *_a: [dict(ERROR, win=windows[0]['win'])])
    original = pb._Agent.finish
    monkeypatch.setattr(pb._Agent, 'finish', lambda *_args: (_ for _ in ()).throw(RuntimeError('interrupted')))
    with pytest.raises(RuntimeError, match='interrupted'):
        step._step3_body(ctx)
    monkeypatch.setattr(pb._Agent, 'finish', original)
    monkeypatch.setattr(pb, '_listen', lambda *_args: pytest.fail('listened again on resume'))
    assert step._step3_body(ctx).ok and len(cpa.calls) == 3


def test_agent_listening_uses_the_pipeline_retry_and_reports_earlier_failed_tries(tmp_path, monkeypatch):
    cpa = AgentCpa([], answers=[{'action': 'plan', 'doubts': [], 'tools': [REQUEST]},
                                {'action': 'draft', 'rows': row('草稿')},
                                {'action': 'final', 'self_check': '复查', 'rows': row('结果')}])
    ctx = context(tmp_path, monkeypatch, cpa)
    seen = {}

    def listen_pass(_ctx, label, windows, models, **kwargs):
        seen.update(kwargs)
        suffix = hashlib.sha256(models[0]['name'].encode()).hexdigest()[:12]
        ctx.adapters.remote.files[f'{ctx.remote_dir}/listen/out/audio/{label}.{suffix}.receipt.json'] = (
            json.dumps({'usage': {'totalTokenCount': 12}}).encode())
        return [{'win': label, 'status': 'OK', 'lines': [], 'attempts': 2}]

    monkeypatch.setattr(pb, '_listen_pass', listen_pass)
    assert step._step3_body(ctx).ok
    assert not seen.get('one_attempt')
    trace = trace_of(ctx)
    result = next(e['result'] for e in trace['events'] if e['event'] == 'tool_result')
    assert result['earlier_failed_attempts_without_receipt'] == 1 and trace['total_tokens'] == 42


def test_listening_tool_retries_an_empty_answer_then_succeeds_and_keeps_the_raw_answer(tmp_path, monkeypatch):
    from assets.manual_lane.remote import listen_windows

    window = {'win': 'AG0001', 'audio': [0., 2.], 'core': [0., 2.], 'tempo': 1., 'mp3': 'unused'}
    model = {'name': 'model', 'slug': '38m'}
    state = {'exhausted': {}, 'lock': threading.Lock()}
    waits, answers = [], [RuntimeError('CPA_GEMINI_EMPTY_RESPONSE'), '{"lines": []}']
    monkeypatch.setattr(listen_windows.time, 'sleep', waits.append)

    def generate(*_args):
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    assert listen_windows.one((window, model), outdir=tmp_path, prompt_tpl='test', state=state,
                              generate=generate, max_attempts=3)[1] == 'OK'
    assert waits == [45] and json.loads((tmp_path / 'AG0001.38m.json').read_text())['attempts'] == 2
    assert (tmp_path / 'AG0001.38m.raw.txt').read_text() == '{"lines": []}'
