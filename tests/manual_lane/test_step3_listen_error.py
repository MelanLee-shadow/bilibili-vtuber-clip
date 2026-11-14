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


def test_a_model_that_gives_no_answer_for_minutes_is_not_asked_again_and_the_next_model_is(tmp_path, monkeypatch):
    """the first level's model returned empty replies after six minutes, three tries a window."""
    from assets.manual_lane.remote import listen_windows

    window = {'win': 'AG0001', 'audio': [0., 2.], 'core': [0., 2.], 'tempo': 1., 'mp3': 'unused'}
    state = {'exhausted': {}, 'lock': threading.Lock()}
    now, asked = [1000.0], []
    monkeypatch.setattr(listen_windows.time, 'sleep', lambda _s: None)

    def generate(_prompt, name, _mp3):
        asked.append(name)
        if name == 'slow':
            now[0] += 370                     # the attempt hangs, then comes back empty
            raise RuntimeError('CPA_GEMINI_EMPTY_RESPONSE')
        return '{"lines": []}'

    level = {'name': 'slow', 'slug': '38m', 'then': ['other']}
    assert listen_windows.one((window, level), outdir=tmp_path, prompt_tpl='test', state=state, generate=generate,
                              max_attempts=3, clock=lambda: now[0])[1] == 'OK'
    assert asked == ['slow', 'other']         # one try of the silent model, not three; then the level's next model
    second = dict(window, win='AG0002')
    assert listen_windows.one((second, level), outdir=tmp_path, prompt_tpl='test', state=state, generate=generate,
                              max_attempts=3, clock=lambda: now[0])[1] == 'OK'
    assert asked == ['slow', 'other', 'other']                     # the next window does not wait for it again
    # a level with no other model waits for it like a quota, instead of spending three silent tries on every window
    alone, state2 = {'name': 'slow', 'slug': 'argon'}, {'exhausted': {}, 'lock': threading.Lock()}
    assert listen_windows.one((window, alone), outdir=tmp_path, prompt_tpl='test', state=state2, generate=generate,
                              max_attempts=3, clock=lambda: now[0])[1] == 'QUOTA'
    assert asked.count('slow') == 2 and state2['exhausted']['slow'] == now[0] + listen_windows.STALLED_S
    # a quick failure is still a transient error: retried as before
    quick = [RuntimeError('CPA_GEMINI_EMPTY_RESPONSE'), '{"lines": []}']
    def flaky(*_a):
        answer = quick.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer
    third = dict(window, win='AG0003')
    assert listen_windows.one((third, {'name': 'm', 'slug': '38m'}), outdir=tmp_path, prompt_tpl='test',
                              state={'exhausted': {}, 'lock': threading.Lock()}, generate=flaky, max_attempts=3,
                              clock=lambda: now[0])[1] == 'OK'


class _Down(RuntimeError):
    def __init__(self, said):
        super().__init__('CPA_GEMINI_HTTP_ERROR')
        self.code, self.said = 503, said


def test_first_failure_asks_the_model_and_the_cheapest_model_a_one_line_question(tmp_path, monkeypatch):
    """维护者: a relay says why a model is down; then it is not asked again and again."""
    from assets.manual_lane.remote import listen_windows

    monkeypatch.setattr(listen_windows.time, 'sleep', lambda _s: None)
    window = {'win': 'AG0001', 'audio': [0., 2.], 'core': [0., 2.], 'tempo': 1., 'mp3': 'unused'}
    said = 'model_unavailable 当前分组「Gemini argon」下模型 暂无可用服务'
    now, asked, probed = [1000.0], [], []

    def generate(_prompt, name, _mp3):
        asked.append(name)
        raise _Down(said)

    def probe_ask(name):
        probed.append(name)
        raise _Down(said)

    state = {'exhausted': {}, 'lock': threading.Lock()}
    alone = {'name': 'm', 'slug': 'argon'}
    how = dict(outdir=tmp_path, prompt_tpl='test', generate=generate, max_attempts=3, clock=lambda: now[0],
               probe_ask=probe_ask, cheapest='lite')
    assert listen_windows.one((window, alone), state=state, **how)[1] == 'QUOTA'
    assert asked == ['m'] and probed == ['m', 'lite']          # one try, not three; both one-line questions failed
    record = json.loads((tmp_path / 'AG0001.argon.json').read_text())
    assert said in record['error'] and record['retry_at'] == now[0] + listen_windows.WAIT_DOWN_S
    assert json.loads((tmp_path / '.probe.json').read_text())[0]['gateway_down'] is True
    # the whole gateway is down: another model of another level is not asked either, and nothing is probed again
    other = dict(window, win='AG0002')
    assert listen_windows.one((other, {'name': 'n', 'slug': '38m'}), state=state, **how)[1] == 'quota_wait'
    assert asked == ['m'] and probed == ['m', 'lite']


def test_a_model_that_answers_a_one_line_question_is_retried_as_before(tmp_path, monkeypatch):
    from assets.manual_lane.remote import listen_windows

    waits = []
    monkeypatch.setattr(listen_windows.time, 'sleep', waits.append)
    window = {'win': 'AG0001', 'audio': [0., 2.], 'core': [0., 2.], 'tempo': 1., 'mp3': 'unused'}
    answers, probed = [RuntimeError('CPA_GEMINI_EMPTY_RESPONSE'), '{"lines": []}'], []

    def generate(*_a):
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    state = {'exhausted': {}, 'lock': threading.Lock()}
    assert listen_windows.one((window, {'name': 'm', 'slug': '38m'}), outdir=tmp_path, prompt_tpl='test', state=state,
                              generate=generate, max_attempts=3, probe_ask=probed.append, cheapest='lite')[1] == 'OK'
    assert probed == ['m'] and waits == [45]       # the model answered the question: not the cheapest, retried once


def test_a_one_line_question_that_only_times_out_does_not_stop_the_model(tmp_path, monkeypatch):
    """16:41Z one word took 72-333 s and 1-28 s thirteen minutes later: slow is not down."""
    from assets.manual_lane.remote import listen_windows

    monkeypatch.setattr(listen_windows.time, 'sleep', lambda _s: None)
    window = {'win': 'AG0001', 'audio': [0., 2.], 'core': [0., 2.], 'tempo': 1., 'mp3': 'unused'}
    answers = [RuntimeError('CPA_GEMINI_EMPTY_RESPONSE'), '{"lines": []}']

    def generate(*_a):
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    def probe_ask(_name):
        raise TimeoutError('timed out')

    state = {'exhausted': {}, 'lock': threading.Lock()}
    assert listen_windows.one((window, {'name': 'm', 'slug': '38m'}), outdir=tmp_path, prompt_tpl='test', state=state,
                              generate=generate, max_attempts=3, probe_ask=probe_ask, cheapest='lite')[1] == 'OK'
    assert state['exhausted'] == {}
