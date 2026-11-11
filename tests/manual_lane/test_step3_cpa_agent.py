"""Agent authority, self-check and runaway controls through the ordinary single-speaker entry."""
import hashlib
import json

import pytest

from agent_fakes import AgentCpa, FaultyCpa, answer_rows
from fakes import FakeRemote, make_ctx
from src.autoslice.manual_lane import playbook_step3 as pb, step3_subtitles as step
from src.autoslice.manual_lane.adapters import CPA_TEXT_TRIES


def context(tmp_path, monkeypatch, cpa):
    ctx = make_ctx(tmp_path, remote=FakeRemote(), cpa=cpa)
    ctx.session['participants'] = [['single speaker']]
    for name, text in [('bcut', '原句'), ('jianying', '另一句')]:
        path = ctx.clip_dir / f'asr/{name}.srt'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f'1\n00:00:00,000 --> 00:00:02,000\n{text}\n')
    monkeypatch.setenv('MANUAL_LANE_ADJUDICATE_MODE', 'C')
    return ctx


def row(text):
    return answer_rows([{'id': 'd001', 'state': 'ADOPT', 'final_text': text,
                         'confidence': '高', 'affects_meaning': False}])


@pytest.mark.parametrize('mode', ['C', 'A', 'B', 'full'])
def test_every_unanchored_single_speaker_mode_routes_to_agent(tmp_path, monkeypatch, mode):
    cpa = AgentCpa(row('结果'))
    ctx = context(tmp_path, monkeypatch, cpa)
    monkeypatch.setenv('MANUAL_LANE_ADJUDICATE_MODE', mode)
    monkeypatch.setattr(step, '_full_listening', lambda *_a, **_k: pytest.fail('old listening path chosen'))
    assert step._step3_body(ctx).ok and len(cpa.calls) == 3


@pytest.mark.parametrize('mode', ['C', 'A', 'B', 'full'])
def test_linked_sessions_keep_their_existing_mode_path(tmp_path, monkeypatch, mode):
    from src.autoslice.manual_lane.runner import StepOutcome

    ctx = context(tmp_path, monkeypatch, AgentCpa([]))
    ctx.session['participants'] = [['speaker one'], ['speaker two']]
    monkeypatch.setenv('MANUAL_LANE_ADJUDICATE_MODE', mode)
    monkeypatch.setattr(pb, 'run', lambda *_a, **_k: pytest.fail('single-speaker agent chosen'))
    chosen = []
    monkeypatch.setattr(step.variant_c, 'run_step3', lambda *_a, **_k: chosen.append('C') or StepOutcome(True))
    monkeypatch.setattr(step, '_full_listening', lambda *_a, **_k: chosen.append('full') or StepOutcome(True))
    assert step._step3_body(ctx).ok and chosen == (['C'] if mode == 'C' else ['full'])


def test_cpa_selects_no_listening_and_revises_its_draft(tmp_path, monkeypatch):
    answers = [{'action': 'plan', 'doubts': [{'cue_ids': [1], 'question': '两种读法'}], 'tools': []},
               {'action': 'draft', 'rows': row('草稿')},
               {'action': 'final', 'self_check': '交叉检查后改字', 'rows': row('修订')}]
    cpa = AgentCpa([], answers=answers)
    ctx = context(tmp_path, monkeypatch, cpa)
    monkeypatch.setattr(pb, '_listen', lambda *_args: pytest.fail('code chose a listening window'))
    assert step._step3_body(ctx).ok
    assert '修订' in (ctx.clip_dir / 'subs/final.srt').read_text()
    trace = json.loads((ctx.clip_dir / 'subs/agent-trace.json').read_text())
    assert trace['status'] == 'COMPLETE' and trace['listening_calls'] == {'38m': 0, 'argon': 0}
    assert any(e['event'] == 'draft_recorded' for e in trace['events'])
    before = (ctx.clip_dir / 'subs/agent-trace.json').read_bytes()
    second = step._step3_body(ctx)
    assert second.reason == 'CPA_AGENT_EXISTING_TRACE'
    assert (ctx.clip_dir / 'subs/agent-trace.json').read_bytes() == before


def test_each_agent_turn_records_its_registered_purpose_model_and_tier(tmp_path, monkeypatch):
    from src.autoslice.manual_lane.lane_rules import rules

    ctx = context(tmp_path, monkeypatch, AgentCpa(row('结果')))
    assert step._step3_body(ctx).ok
    trace = json.loads((ctx.clip_dir / 'subs/agent-trace.json').read_text())
    calls = [event for event in trace['events'] if event['event'] == 'cpa_result']
    settings = rules('effort')['purposes']['subtitle-agent']
    assert len(calls) == 3
    assert all((event['purpose'], event['model'], event['effort'], event['max_output_tokens']) ==
               ('subtitle-agent', settings['model'], settings['effort'], settings['max_output_tokens'])
               for event in calls)


def test_release_shaping_cannot_change_cpa_punctuation(tmp_path, monkeypatch):
    cpa = AgentCpa(row('你好？'))
    ctx = context(tmp_path, monkeypatch, cpa)
    original = {'id': 1, 'start_s': 0., 'end_s': 2., 'text': '你好？'}
    changed = dict(original, text='你好。')
    monkeypatch.setattr(pb.step3_text_classes, 'partitioned_cues', lambda *_args: [original])
    monkeypatch.setattr(pb.normalize, 'release_shape', lambda *_args, **_kwargs: ([changed], []))
    assert step._step3_body(ctx).ok
    result = (ctx.clip_dir / 'subs/final.srt').read_text()
    assert '你好？' in result and '你好。' not in result


def test_final_before_self_check_is_returned_to_cpa_as_protocol_error(tmp_path, monkeypatch):
    answers = [{'action': 'plan', 'doubts': [], 'tools': []},
               {'action': 'final', 'self_check': 'too early', 'rows': row('错误草稿')},
               {'action': 'draft', 'rows': row('草稿')},
               {'action': 'final', 'self_check': '复查', 'rows': row('结果')}]
    cpa = AgentCpa([], answers=answers)
    ctx = context(tmp_path, monkeypatch, cpa)
    assert step._step3_body(ctx).ok
    assert 'final requires an earlier draft' in cpa.prompts[2]
    assert '结果' in (ctx.clip_dir / 'subs/final.srt').read_text()


def test_cpa_can_request_viewer_echo_after_the_clip(tmp_path, monkeypatch):
    cpa = AgentCpa(row('结果'), tools=[{'tool': 'danmaku', 'start': 70, 'end': 80}])
    ctx = context(tmp_path, monkeypatch, cpa)
    ctx.adapters.danmaku.xml = '<i><d p="85,1,1">观众回声</d></i>'.encode()
    assert step._step3_body(ctx).ok
    trace = json.loads((ctx.clip_dir / 'subs/agent-trace.json').read_text())
    result = next(e['result'] for e in trace['events'] if e['event'] == 'tool_result')
    assert any(item['text'] == '观众回声' and item['t_s'] == 75 for item in result)


def test_call_cap_stops_without_final_and_retains_trace(tmp_path, monkeypatch):
    monkeypatch.setattr(pb, 'MAX_TEXT_CALLS', 2)
    cpa = AgentCpa([], answers=[{'action': 'plan', 'doubts': [], 'tools': []},
                                {'action': 'tools', 'tools': []}])
    ctx = context(tmp_path, monkeypatch, cpa)
    outcome = step._step3_body(ctx)
    assert outcome.reason == 'CPA_AGENT_CALL_CAP' and len(cpa.calls) == 2
    trace = json.loads((ctx.clip_dir / 'subs/agent-trace.json').read_text())
    assert trace['status'] == 'REFUSED' and trace['text_calls'] == 2
    assert not (ctx.clip_dir / 'subs/final.srt').exists()


def test_token_cap_stops_with_the_measured_receipts(tmp_path, monkeypatch):
    monkeypatch.setattr(pb, 'MAX_TOTAL_TOKENS', 20)
    cpa = AgentCpa([], answers=[{'action': 'plan', 'doubts': [], 'tools': []},
                                {'action': 'draft', 'rows': row('草稿')}])
    ctx = context(tmp_path, monkeypatch, cpa)
    outcome = step._step3_body(ctx)
    trace = json.loads((ctx.clip_dir / 'subs/agent-trace.json').read_text())
    assert outcome.reason == 'CPA_AGENT_TOKEN_CAP' and trace['total_tokens'] == 20
    assert trace['refusal'] == 'CPA_AGENT_TOKEN_CAP' and len(cpa.calls) == 2


def test_missing_usage_refuses_instead_of_silently_resetting_budget(tmp_path, monkeypatch):
    class MissingUsage(AgentCpa):
        def ask(self, **kwargs):
            result = super().ask(**kwargs)
            result['usage'] = None
            return result

    cpa = MissingUsage([], answers=[{'action': 'plan', 'doubts': [], 'tools': []}])
    ctx = context(tmp_path, monkeypatch, cpa)
    outcome = step._step3_body(ctx)
    trace = json.loads((ctx.clip_dir / 'subs/agent-trace.json').read_text())
    assert outcome.reason == 'CPA_AGENT_USAGE_UNKNOWN' and trace['refusal'] == outcome.reason
    assert trace['events'][-1]['event'] == 'cpa_result'


@pytest.mark.parametrize('raw', ['{"lines": [invalid}', ''])
def test_cpa_receives_malformed_listening_then_selects_retry(tmp_path, monkeypatch, raw):
    request = {'tool': 'listen', 'start': 0, 'end': 2, 'cue_ids': [1],
               'reason': '核实读音', 'model': '38m'}
    cpa = AgentCpa([], answers=[{'action': 'plan', 'doubts': [], 'tools': [request]},
                                {'action': 'tools', 'tools': [dict(request, reason='重听')]},
                                {'action': 'draft', 'rows': row('草稿')},
                                {'action': 'final', 'self_check': '复查', 'rows': row('结果')}])
    ctx = context(tmp_path, monkeypatch, cpa)
    calls = []

    def listen(_ctx, windows, model, _label):
        win = windows[0]['win']
        calls.append(win)
        suffix = hashlib.sha256(model['name'].encode()).hexdigest()[:12]
        receipt = {'usage': {'totalTokenCount': 12}, 'text_sha256': hashlib.sha256(raw.encode()).hexdigest()}
        ctx.adapters.remote.files[f'{ctx.remote_dir}/listen/out/audio/{win}.{suffix}.receipt.json'] = (
            json.dumps(receipt).encode())
        if len(calls) == 1:
            ctx.adapters.remote.files[f'{ctx.remote_dir}/listen/out/{win}.38m.raw.txt'] = raw.encode()
            return [{'win': win, 'status': 'ERROR', 'error': 'JSONDecodeError'}]
        return [{'win': win, 'status': 'OK', 'lines': []}]

    monkeypatch.setattr(pb, '_listen', listen)
    monkeypatch.setattr(pb, 'upload_listen_tools', lambda *_args: None)
    assert step._step3_body(ctx).ok and len(calls) == 2
    trace = json.loads((ctx.clip_dir / 'subs/agent-trace.json').read_text())
    failed = next(e['result'] for e in trace['events'] if e['event'] == 'listen_result')
    assert failed['record']['status'] == 'ERROR' and failed['raw_response'] == raw
    assert 'JSONDecodeError' in cpa.prompts[1] and trace['total_tokens'] == 64
    assert len(trace['completed_listens']) == 1


def test_unknown_listening_usage_refuses_without_asking_cpa_again(tmp_path, monkeypatch):
    request = {'tool': 'listen', 'start': 0, 'end': 2, 'cue_ids': [1],
               'reason': '核实读音', 'model': '38m'}
    cpa = AgentCpa([], answers=[{'action': 'plan', 'doubts': [], 'tools': [request]}])
    ctx = context(tmp_path, monkeypatch, cpa)
    monkeypatch.setattr(pb, '_listen', lambda *_args: [{'status': 'OK', 'lines': []}])
    monkeypatch.setattr(pb, 'upload_listen_tools', lambda *_args: None)
    outcome = step._step3_body(ctx)
    assert outcome.reason == 'CPA_AGENT_USAGE_UNKNOWN' and len(cpa.calls) == 1


def test_malformed_listening_response_is_preserved_without_redispatch(tmp_path):
    import threading
    from assets.manual_lane.remote.listen_windows import one

    window = {'win': 'AG0001', 'audio': [0., 2.], 'core': [0., 2.], 'tempo': 1., 'mp3': 'unused'}
    model = {'name': 'model', 'slug': '38m'}
    state = {'exhausted': {}, 'lock': threading.Lock()}
    calls = []

    def malformed(*args):
        calls.append(args)
        return '{malformed'

    assert one((window, model), outdir=tmp_path, prompt_tpl='test', state=state,
               generate=malformed, max_attempts=1)[1] == 'ERROR'
    assert (tmp_path / 'AG0001.38m.raw.txt').read_text() == '{malformed'
    assert one((window, model), outdir=tmp_path, prompt_tpl='test', state=state,
               generate=malformed, max_attempts=1)[1] == 'dispatch_unknown'
    assert len(calls) == 1


@pytest.mark.parametrize(('attempts', 'waits'), [(1, []), (3, [45, 90, 135])])
def test_listening_failure_waits_keep_legacy_default(tmp_path, monkeypatch, attempts, waits):
    import threading
    from assets.manual_lane.remote import listen_windows

    window = {'win': 'AG0001', 'audio': [0., 2.], 'core': [0., 2.], 'tempo': 1., 'mp3': 'unused'}
    model = {'name': 'model', 'slug': '38m'}
    state = {'exhausted': {}, 'lock': threading.Lock()}
    seen = []
    monkeypatch.setattr(listen_windows.time, 'sleep', seen.append)

    def fail(*_args):
        raise OSError('synthetic failure')

    assert listen_windows.one((window, model), outdir=tmp_path, prompt_tpl='test', state=state,
                              generate=fail, max_attempts=attempts)[1] == 'ERROR'
    assert seen == waits


def test_interrupted_final_is_recovered_without_reasking_cpa(tmp_path, monkeypatch):
    cpa = AgentCpa(row('复核后的结果'))
    ctx = context(tmp_path, monkeypatch, cpa)
    original = pb._Agent.finish

    def fail_once(*_args, **_kwargs):
        raise RuntimeError('test interrupted before writing')

    monkeypatch.setattr(pb._Agent, 'finish', fail_once)
    with pytest.raises(RuntimeError, match='interrupted'):
        step._step3_body(ctx)
    assert len(cpa.calls) == 3
    monkeypatch.setattr(pb._Agent, 'finish', original)
    assert step._step3_body(ctx).ok and len(cpa.calls) == 3
    trace = json.loads((ctx.clip_dir / 'subs/agent-trace.json').read_text())
    assert trace['status'] == 'COMPLETE' and any(e['event'] == 'resume_final' for e in trace['events'])
    assert '复核后的结果' in (ctx.clip_dir / 'subs/final.srt').read_text()


def test_resume_rejects_changed_raw_completion_without_reasking(tmp_path, monkeypatch):
    cpa = AgentCpa(row('结果'))
    ctx = context(tmp_path, monkeypatch, cpa)
    monkeypatch.setattr(pb._Agent, 'finish', lambda *_args: (_ for _ in ()).throw(RuntimeError('interrupted')))
    with pytest.raises(RuntimeError):
        step._step3_body(ctx)
    completion = ctx.clip_dir / 'cpa/agent-003.completion.txt'
    completion.write_text('tampered', encoding='utf-8')
    before = (ctx.clip_dir / 'subs/agent-trace.json').read_bytes()
    assert step._step3_body(ctx).reason == 'CPA_AGENT_EXISTING_TRACE'
    assert len(cpa.calls) == 3 and (ctx.clip_dir / 'subs/agent-trace.json').read_bytes() == before


@pytest.mark.parametrize('slug', ['38m', 'argon'])
def test_single_attempt_never_redispatches_an_unknown_listening_call(tmp_path, slug):
    import threading
    from assets.manual_lane.remote.listen_windows import one

    window = {'win': 'AG0001', 'audio': [0., 2.], 'core': [0., 2.], 'tempo': 1., 'mp3': 'unused'}
    model = {'name': slug, 'slug': slug}
    calls = []

    def failed(*args):
        calls.append(args)
        raise OSError('response unknown')

    state = {'exhausted': {}, 'lock': threading.Lock()}
    first = one((window, model), outdir=tmp_path, prompt_tpl='test', state=state,
                generate=failed, max_attempts=1)
    second = one((window, model), outdir=tmp_path, prompt_tpl='test', state=state,
                 generate=failed, max_attempts=1)
    assert first[1] == 'ERROR' and second[1] == 'dispatch_unknown' and len(calls) == 1


def trace_of(ctx):
    return json.loads((ctx.clip_dir / 'subs/agent-trace.json').read_text())


def test_one_overloaded_request_is_retried_by_the_pipeline_tries_and_the_clip_continues(tmp_path, monkeypatch):
    cpa = FaultyCpa(row('结果'), faults=['overloaded'])
    ctx = context(tmp_path, monkeypatch, cpa)
    assert step._step3_body(ctx).ok
    trace = trace_of(ctx)
    assert cpa.tries_seen == [CPA_TEXT_TRIES] * 3 and cpa.requests == 4
    assert [e['requests'] for e in trace['events'] if e['event'] == 'cpa_result'] == [2, 1, 1]
    assert trace['status'] == 'COMPLETE' and trace['text_calls'] == 4 and trace['total_tokens'] == 30


def test_one_cut_answer_is_asked_again_and_its_spent_tokens_are_charged(tmp_path, monkeypatch):
    cpa = FaultyCpa(row('结果'), faults=['cut'])
    ctx = context(tmp_path, monkeypatch, cpa)
    assert step._step3_body(ctx).ok
    trace = trace_of(ctx)
    first = next(e for e in trace['events'] if e['event'] == 'cpa_result')
    assert first['requests'] == 2 and first['rejected_answers'][0]['reason'].startswith('CPA_ANSWER_INCOMPLETE')
    assert first['rejected_answers'][0]['usage'] == {'total_tokens': 7}
    assert (ctx.clip_dir / 'cpa/agent-001.rejected-1.completion.txt').is_file()
    assert trace['status'] == 'COMPLETE' and trace['text_calls'] == 4 and trace['total_tokens'] == 37


def test_overloaded_through_every_try_still_refuses_by_name_and_keeps_the_evidence(tmp_path, monkeypatch):
    cpa = FaultyCpa(row('结果'), faults=['overloaded'] * 30)
    ctx = context(tmp_path, monkeypatch, cpa)
    outcome = step._step3_body(ctx)
    trace = trace_of(ctx)
    assert outcome.reason == 'CPA_CALL_FAILED' and trace['refusal'] == 'CPA_CALL_FAILED'
    assert cpa.tries_seen == [CPA_TEXT_TRIES] and cpa.requests == CPA_TEXT_TRIES
    assert trace['text_calls'] == CPA_TEXT_TRIES and trace['events'][-1]['rc'] == 1
    assert not (ctx.clip_dir / 'subs/final.srt').exists()


def test_cut_through_every_ask_still_refuses_by_name_and_keeps_every_original(tmp_path, monkeypatch):
    cpa = FaultyCpa(row('结果'), faults=['cut'] * 30)
    ctx = context(tmp_path, monkeypatch, cpa)
    outcome = step._step3_body(ctx)
    trace = trace_of(ctx)
    last = trace['events'][-1]
    assert outcome.reason == 'CPA_CALL_FAILED' and trace['refusal'] == 'CPA_CALL_FAILED'
    assert last['rc'] == 1 and len(last['rejected_answers']) == CPA_TEXT_TRIES == last['requests']
    assert trace['text_calls'] == CPA_TEXT_TRIES and trace['total_tokens'] == 7 * CPA_TEXT_TRIES
    for attempt in range(1, CPA_TEXT_TRIES + 1):
        assert (ctx.clip_dir / f'cpa/agent-001.rejected-{attempt}.completion.txt').is_file()


def test_call_cap_counts_the_retried_requests_not_only_the_turns(tmp_path, monkeypatch):
    monkeypatch.setattr(pb, 'MAX_TEXT_CALLS', 3)
    cpa = FaultyCpa([], answers=[{'action': 'plan', 'doubts': [], 'tools': []},
                                 {'action': 'tools', 'tools': []}, {'action': 'tools', 'tools': []}],
                    faults=['overloaded'])
    ctx = context(tmp_path, monkeypatch, cpa)
    outcome = step._step3_body(ctx)
    assert outcome.reason == 'CPA_AGENT_CALL_CAP' and len(cpa.calls) == 2 and cpa.requests == 3
    assert trace_of(ctx)['text_calls'] == 3


def test_token_cap_counts_a_rejected_answers_tokens(tmp_path, monkeypatch):
    monkeypatch.setattr(pb, 'MAX_TOTAL_TOKENS', 17)
    cpa = FaultyCpa([], answers=[{'action': 'plan', 'doubts': [], 'tools': []}], faults=['cut'])
    ctx = context(tmp_path, monkeypatch, cpa)
    outcome = step._step3_body(ctx)
    assert outcome.reason == 'CPA_AGENT_TOKEN_CAP' and trace_of(ctx)['total_tokens'] == 17


def test_interrupted_run_resumes_over_a_retried_call_without_asking_again(tmp_path, monkeypatch):
    cpa = FaultyCpa(row('复核后的结果'), faults=['overloaded', 'cut'])
    ctx = context(tmp_path, monkeypatch, cpa)
    original = pb._Agent.finish
    monkeypatch.setattr(pb._Agent, 'finish', lambda *_args: (_ for _ in ()).throw(RuntimeError('interrupted')))
    with pytest.raises(RuntimeError, match='interrupted'):
        step._step3_body(ctx)
    asked = cpa.requests
    monkeypatch.setattr(pb._Agent, 'finish', original)
    assert step._step3_body(ctx).ok and cpa.requests == asked
    trace = trace_of(ctx)
    assert trace['status'] == 'COMPLETE' and trace['text_calls'] == 5 and trace['total_tokens'] == 37
