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

    def listen(_ctx, windows, model, _label, _on_wait=None):
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


def test_a_rate_limited_listening_window_is_waited_out_and_listened_to_again(tmp_path, monkeypatch):
    from src.autoslice.manual_lane import step3_evidence as si

    request = {'tool': 'listen', 'start': 0, 'end': 2, 'cue_ids': [1], 'reason': '核实读音', 'model': '38m'}
    answers = [{'action': 'plan', 'doubts': [], 'tools': [request]}, {'action': 'draft', 'rows': row('结果')},
               {'action': 'final', 'self_check': '核对完毕', 'rows': row('结果')}]
    cpa = AgentCpa([], answers=answers)
    ctx = context(tmp_path, monkeypatch, cpa)
    calls, slept = [], []

    def once(_ctx, label, windows, models, **_kw):
        win = windows[0]['win']
        calls.append(win)
        if len(calls) < 3:                              # refused twice, then answered
            return [{'win': win, 'status': 'QUOTA', 'retry_at': si.time.time() + 40, 'lines': [], 'error': '429 on attempt 1'}]
        suffix = hashlib.sha256(models[0]['name'].encode()).hexdigest()[:12]
        ctx.adapters.remote.files[f'{ctx.remote_dir}/listen/out/audio/{win}.{suffix}.receipt.json'] = (
            json.dumps({'usage': {'totalTokenCount': 12}}).encode())
        return [{'win': win, 'status': 'OK', 'lines': [{'start_s': 0.0, 'text': '结果'}]}]

    monkeypatch.setattr(si, '_listen_once', once)
    monkeypatch.setattr(si, '_sleep', slept.append)
    monkeypatch.setattr(pb, 'upload_listen_tools', lambda *_args: None)
    assert step._step3_body(ctx).ok and len(calls) == 3 and len(slept) == 2 and all(35 < s < 45 for s in slept)
    trace = json.loads((ctx.clip_dir / 'subs/agent-trace.json').read_text())
    assert [e['event'] for e in trace['events'] if e['event'].startswith('listen_')] == [
        'listen_dispatch', 'listen_quota_wait', 'listen_quota_wait', 'listen_result']
    assert trace['listening_calls'] == {'38m': 1, 'argon': 0} and len(trace['completed_listens']) == 1


def test_a_rate_limit_that_does_not_clear_is_handed_to_cpa_with_the_other_recognisers(tmp_path, monkeypatch):
    from src.autoslice.manual_lane import step3_evidence as si

    request = {'tool': 'listen', 'start': 0, 'end': 2, 'cue_ids': [1], 'reason': '核实读音', 'model': '38m'}
    answers = [{'action': 'plan', 'doubts': [], 'tools': [request]}, {'action': 'draft', 'rows': row('结果')},
               {'action': 'final', 'self_check': '核对完毕', 'rows': row('结果')}]
    ctx = context(tmp_path, monkeypatch, AgentCpa([], answers=answers))
    slept = []
    monkeypatch.setattr(si, '_listen_once', lambda _c, _l, windows, _m, **_k: [
        {'win': windows[0]['win'], 'status': 'QUOTA', 'retry_at': si.time.time() + 9999, 'lines': [], 'error': 'skipped: waiting'}])
    monkeypatch.setattr(si, '_sleep', slept.append)
    monkeypatch.setattr(pb, 'upload_listen_tools', lambda *_args: None)
    monkeypatch.setattr(pb, '_asr_window_pass', lambda _c, label, _w, _p, **_k: [
        {'win': label, 'provider': 'mai', 'status': 'OK', 'segments': [{'text': '结果'}]}])
    assert step._step3_body(ctx).ok and len(slept) == si.QUOTA_WAITS and max(slept) <= si.QUOTA_WAIT_MAX_S + 2
    trace = json.loads((ctx.clip_dir / 'subs/agent-trace.json').read_text())
    result = next(e['result'] for e in trace['events'] if e['event'] == 'listen_result')
    assert result['record']['status'] == 'QUOTA' and result['usage_unavailable_on_error'] is True
    assert result['other_recognisers'] == [{'recogniser': 'mai', 'status': 'OK', 'text': '结果'}]   # not left unheard


def test_a_second_hearing_nobody_can_give_is_handed_over_once_and_not_asked_again(tmp_path, monkeypatch):
    from src.autoslice.manual_lane import step3_evidence as si

    def ask(model):
        return {'tool': 'listen', 'start': 0, 'end': 2, 'cue_ids': [1], 'reason': '核实读音', 'model': model}
    answers = [{'action': 'plan', 'doubts': [], 'tools': [ask('38m')]}, {'action': 'tools', 'tools': [ask('argon')]},
               {'action': 'tools', 'tools': [ask('argon')]}, {'action': 'draft', 'rows': row('结果')},
               {'action': 'final', 'self_check': '核对完毕', 'rows': row('结果')}]
    ctx = context(tmp_path, monkeypatch, AgentCpa([], answers=answers))
    asked = []

    def once(_ctx, label, windows, models, **_kw):
        win = windows[0]['win']
        asked.append(models[0]['slug'])
        if models[0]['slug'] == 'argon':
            return [{'win': win, 'model': models[0]['then'][0], 'status': 'UNAVAILABLE', 'lines': [], 'tried': []}]
        suffix = hashlib.sha256(models[0]['name'].encode()).hexdigest()[:12]
        ctx.adapters.remote.files[f'{ctx.remote_dir}/listen/out/audio/{win}.{suffix}.receipt.json'] = (
            json.dumps({'usage': {'totalTokenCount': 12}}).encode())
        return [{'win': win, 'model': models[0]['name'], 'status': 'OK', 'lines': [{'start_s': 0.0, 'text': '结果'}]}]

    monkeypatch.setattr(si, '_listen_once', once)
    monkeypatch.setattr(si, '_sleep', lambda _s: pytest.fail('an unavailable level is not waited for'))
    monkeypatch.setattr(pb, 'upload_listen_tools', lambda *_args: None)
    monkeypatch.setattr(pb, '_asr_window_pass', lambda _c, label, _w, _p, **_k: [
        {'win': label, 'provider': 'mai', 'status': 'OK', 'segments': [{'text': '结果'}]}])
    assert step._step3_body(ctx).ok and asked == ['38m', 'argon']
    trace = json.loads((ctx.clip_dir / 'subs/agent-trace.json').read_text())
    results = [e['result'] for e in trace['events'] if e['event'] in ('listen_result', 'tool_result')]
    handed = next(r for r in results if r.get('record', {}).get('status') == 'UNAVAILABLE')
    assert handed['other_recognisers'] == [{'recogniser': 'mai', 'status': 'OK', 'text': '结果'}]
    assert any('cannot be heard now' in str(r.get('error', '')) for r in results)
    assert len(si.ARGON['then']) >= 1 and si.M38['slug'] == '38m'


def test_every_listening_path_waits_out_a_rate_limit_and_reuse_only_never_waits(tmp_path, monkeypatch):
    from src.autoslice.manual_lane import step3_evidence as si

    ctx = context(tmp_path, monkeypatch, AgentCpa([]))
    calls, slept, told = [], [], []

    def once(_ctx, label, windows, models, **kw):
        calls.append((label, kw.get('prepared')))
        status = 'QUOTA' if len(calls) == 1 else 'OK'
        return [{'win': w['win'], 'model': models[0]['name'], 'status': status, 'retry_at': si.time.time() + 30} for w in windows]

    monkeypatch.setattr(si, '_listen_once', once)
    monkeypatch.setattr(si, '_sleep', slept.append)
    got = si._listen_pass(ctx, 'L2', [{'win': 'C000'}, {'win': 'C001'}], [si.M38], on_quota_wait=lambda s, n: told.append(n))
    assert [r['status'] for r in got] == ['OK', 'OK'] and calls == [('L2', False), ('L2', True)]
    assert len(slept) == 1 and told == [2]
    calls.clear(); slept.clear()
    monkeypatch.setenv('MANUAL_LANE_REUSE_ONLY', '1')
    monkeypatch.setattr(si, '_listen_once', lambda *_a, **_k: [{'win': 'C000', 'status': 'QUOTA', 'retry_at': 0}])
    assert si._listen_pass(ctx, 'L2', [{'win': 'C000'}], [si.M38])[0]['status'] == 'QUOTA' and slept == []


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


SUNG = ('1\n00:00:00,000 --> 00:00:02,000\n下一首歌是什么\n\n2\n00:00:02,400 --> 00:00:04,000\n马忒打忒哇卡忒哟\n\n'
        '3\n00:00:04,100 --> 00:00:07,600\n南达莫奈卡拉波库诺乌塔哦\n\n4\n00:00:07,700 --> 00:00:09,000\n后面忘了\n')


def sung_rows(*, parts=None, reason='歌词已并入上一行'):
    line = {'id': 'd002', 'state': 'ADOPT', 'final_text': '待って、分かってよ。何でもないから。', 'confidence': '低',
            'affects_meaning': False, **({'parts': parts} if parts else {})}
    return answer_rows([{'id': 'd001', 'state': 'KEEP', 'final_text': '下一首歌是什么', 'confidence': '高', 'affects_meaning': False},
                        line,
                        {'id': 'd003', 'state': 'ADOPT', 'final_text': '', 'drop': True, 'confidence': '低',
                         'affects_meaning': False, 'reason': reason, **({'parts': []} if parts else {})},
                        {'id': 'd004', 'state': 'KEEP', 'final_text': '后面忘了', 'confidence': '高', 'affects_meaning': False}])


def sung_context(tmp_path, monkeypatch, answers):
    cpa = AgentCpa([], answers=answers)
    ctx = context(tmp_path, monkeypatch, cpa)
    (ctx.clip_dir / 'asr/bcut.srt').write_text(SUNG)
    return ctx, cpa


def test_a_final_that_leaves_speech_without_a_subtitle_is_sent_back_and_the_sentence_gets_its_time(tmp_path, monkeypatch):
    #, the first clip the unattended lane packaged: a sung line was moved into the row before it without its
    # time (a foreign line has no word times), and the package step stopped at SPEECH_WITHOUT_SUBTITLES
    moved, timed = sung_rows(), sung_rows(parts=[{'text': '待って、分かってよ。何でもないから。', 'first_cue': 2, 'last_cue': 3}])
    ctx, cpa = sung_context(tmp_path, monkeypatch, [
        {'action': 'plan', 'doubts': [], 'tools': [{'tool': 'words', 'start': 0, 'end': 9}]},
        {'action': 'draft', 'rows': moved}, {'action': 'final', 'self_check': 'checked', 'rows': moved},
        {'action': 'final', 'self_check': 'the sung line keeps its time', 'rows': timed}])
    assert step._step3_body(ctx).ok and len(cpa.calls) == 4
    events = json.loads((ctx.clip_dir / 'subs/agent-trace.json').read_text())['events']
    told = [e for e in events if e.get('unsubtitled')]
    assert len(told) == 1 and told[0]['unsubtitled'][0]['text'] == '南达莫奈卡拉波库诺乌塔哦' and 'first_cue' in told[0]['error']
    final = (ctx.clip_dir / 'subs/final.srt').read_text()
    assert '00:00:02,400 --> 00:00:07,600' in final and '待って' in final
    assert not (ctx.clip_dir / 'subs/unsubtitled.json').exists()      # nothing is left without a line: nothing to decide


def test_the_agent_may_keep_its_rows_when_it_names_the_stretch_and_its_sound(tmp_path, monkeypatch):
    rows = sung_rows(reason='这一段是背景音乐里的人声，不是口播')
    named = [{'start_s': 4.0, 'end_s': 7.7, 'sound': '背景音乐里的人声'}]
    ctx, cpa = sung_context(tmp_path, monkeypatch, [
        {'action': 'plan', 'doubts': [], 'tools': []}, {'action': 'draft', 'rows': rows},
        {'action': 'final', 'self_check': 'checked', 'rows': rows},
        {'action': 'final', 'self_check': 'not speech', 'rows': rows, 'not_subtitled': [{'start_s': 4.0, 'end_s': 7.7}]},
        {'action': 'final', 'self_check': 'not speech', 'rows': rows, 'not_subtitled': named}])
    assert step._step3_body(ctx).ok and len(cpa.calls) == 5          # told once; a name without the sound is no name
    assert 'not_subtitled must be' in cpa.prompts[4] and trace_of(ctx)['not_subtitled'] == named
    from src.autoslice.manual_lane import package_guards as pg
    decided = json.loads((ctx.clip_dir / pg.DECIDED_FILE).read_text())
    assert [(g['start_s'], g['end_s'], g['text']) for g in decided] == [(4.0, 7.7, '南达莫奈卡拉波库诺乌塔哦')]
    gap = {'start_s': 4.0, 'end_s': 7.7, 'text': '南达莫奈卡拉波库诺乌塔哦'}
    other = {'start_s': 20.0, 'end_s': 26.0, 'text': '别处一段没字幕的话'}
    assert pg.undecided(ctx.clip_dir, [gap, other]) == [other]      # the package step stops only for what nobody decided


def test_a_finished_run_whose_rows_leave_unsubtitled_speech_is_taken_up_again(tmp_path, monkeypatch):
    # one of several runs finished in an earlier attempt of the step (before this check, or by an older deployment)
    moved, timed = sung_rows(), sung_rows(parts=[{'text': '待って、分かってよ。何でもないから。', 'first_cue': 2, 'last_cue': 3}])
    ctx, cpa = sung_context(tmp_path, monkeypatch, [{'action': 'tools', 'tools': [{'tool': 'words', 'start': 0, 'end': 9}]},
                                                    {'action': 'final', 'self_check': 'the sung line keeps its time', 'rows': timed}])
    bcut = [{'id': n, 'start_s': a, 'end_s': b, 'text': t} for n, (a, b, t) in enumerate(
        [(0., 2., '下一首歌是什么'), (2.4, 4., '马忒打忒哇卡忒哟'), (4.1, 7.6, '南达莫奈卡拉波库诺乌塔哦'), (7.7, 9., '后面忘了')], 1)]
    agent = pb._Agent(ctx, bcut, [], ctx.state.data['clip'], 9.0, tag='b')
    agent.trace.update(status='COMPLETE', final_rows=moved, events=[
        {'event': 'cpa_result', 'call_id': 'agentb-000', 'response': json.dumps({'action': 'plan', 'doubts': [], 'tools': []})},
        {'event': 'draft_recorded', 'instruction': 'x'}])
    agent.save()
    again = pb._Agent(ctx, bcut, [], ctx.state.data['clip'], 9.0, tag='b')
    rows = again.execute(finish=False)
    assert rows == timed and len(cpa.calls) == 2 and any(e.get('unsubtitled') for e in again.trace['events'])


def test_a_finished_run_that_named_its_stretch_stands_when_taken_up_again(tmp_path, monkeypatch):
    from src.autoslice.manual_lane.srt_text import parse_srt

    rows, named = sung_rows(reason='背景音乐里的人声'), [{'start_s': 4.0, 'end_s': 7.7, 'sound': '背景音乐里的人声'}]
    ctx, cpa = sung_context(tmp_path, monkeypatch, [])
    agent = pb._Agent(ctx, parse_srt(SUNG), [], ctx.state.data['clip'], 9.0, tag='b')
    agent.trace.update(status='COMPLETE', final_rows=rows, not_subtitled=named, events=[
        {'event': 'cpa_result', 'call_id': 'agentb-000', 'response': json.dumps({'action': 'final', 'rows': rows})}])
    agent.save()
    again = pb._Agent(ctx, parse_srt(SUNG), [], ctx.state.data['clip'], 9.0, tag='b')
    assert again.execute(finish=False) == rows and not cpa.calls


# semantictalk_1621740_1804080_11242159, cut down: she imitates answering a professor in Japanese; the
# recogniser wrote the Japanese of cues 5-6 as Chinese-sounding nonsense, and far from it stands a 「对」 by itself
ANSWERED = ('1\n00:00:00,000 --> 00:00:01,500\n这是真情实感\n\n2\n00:00:02,000 --> 00:00:02,680\n对\n\n'
            '3\n00:00:03,020 --> 00:00:05,800\n一边用来应付教授\n\n4\n00:00:05,800 --> 00:00:09,190\n他在那呱呱呱呱叫我说嗨嗨\n\n'
            '5\n00:00:09,190 --> 00:00:11,490\n社死你涩死了，涩涩死你\n\n6\n00:00:11,490 --> 00:00:12,229\n他是搞你\n\n'
            '7\n00:00:12,229 --> 00:00:15,740\n然后其实脑子早就已经飞到直播间\n')
JAPANESE = '他在那呱呱呱呱讲，我说はいはい、そうっすね、そう、それはそう、そうですね、確かに。'


def answered_rows(*, lone_merged):
    keep = {'state': 'KEEP', 'confidence': '高', 'affects_meaning': False}
    moved = {'state': 'ADOPT', 'final_text': '', 'drop': True, 'confidence': '高', 'affects_meaning': False, 'parts': []}
    lone = ([{'id': 'd002', 'reason': '并入d003', **moved},
             {'id': 'd003', 'state': 'ADOPT', 'final_text': '对，一边用来应付教授', 'confidence': '高', 'affects_meaning': False}]
            if lone_merged else [{'id': 'd002', 'final_text': '对', **keep}, {'id': 'd003', 'final_text': '一边用来应付教授', **keep}])
    return answer_rows([
        {'id': 'd001', 'final_text': '这是真情实感', **keep}, *lone,
        {'id': 'd004', 'state': 'ADOPT', 'final_text': JAPANESE, 'confidence': '高', 'affects_meaning': False,
         'parts': [{'text': JAPANESE, 'first_cue': 4, 'last_cue': 6}]},
        {'id': 'd005', 'reason': '是d004所引日语的连续部分，已全部移入d004，未删口播。', **moved},
        {'id': 'd006', 'reason': '是日语確かに的近音误识别，并入d004。', **moved},
        {'id': 'd007', 'final_text': '然后其实脑子早就已经飞到直播间', **keep}])


def test_a_lone_character_with_no_touching_sentence_is_not_subtitled_and_the_line_that_took_in_its_neighbours_keeps_their_time(
        tmp_path, monkeypatch):
    # The agent moved cues 5-6 into cue 4 and said so with the cue span.  The lone 「对」 (2.00-2.68) has no touching
    # neighbour (the next sentence starts at 3.02, 0.34 s later): the release shape left it out, so that shape was
    # refused and the 「对」 stood alone in final.srt - the release check refused the file (SRT_SINGLE_CJK_CHARACTER,
    # finished-media review A2 FAIL).  A sentence that is one lone character takes U138 as in every writer: not
    # subtitled, recorded in subs/lone_dropped.json; never joined across the silence, and the agent is not asked.
    from src.autoslice.manual_lane import package_guards as pg, subtitle_track
    from src.autoslice.manual_lane.srt_text import parse_srt
    from src.autoslice.subtitle_validation import validate_srt_file

    rows = answered_rows(lone_merged=False)
    cpa = AgentCpa([], answers=[{'action': 'plan', 'doubts': [], 'tools': [{'tool': 'words', 'start': 0, 'end': 16}]},
                                {'action': 'draft', 'rows': rows}, {'action': 'final', 'self_check': 'checked', 'rows': rows}])
    ctx = context(tmp_path, monkeypatch, cpa)
    (ctx.clip_dir / 'asr/bcut.srt').write_text(ANSWERED)
    outcome = step._step3_body(ctx)
    assert outcome.ok and len(cpa.calls) == 3
    assert not [e for e in trace_of(ctx)['events'] if e.get('event') == 'protocol_error']
    path = ctx.clip_dir / 'subs/final.srt'
    assert validate_srt_file(path)['status'] == 'PASS'                # what the release check and A2 run
    final = subtitle_track.parse_blocks(path.read_text())
    assert [(c['start_s'], c['text']) for c in final[:2]] == [(0.0, '这是真情实感'), (3.02, '一边用来应付教授')]
    left_out = json.loads((ctx.clip_dir / 'subs/lone_dropped.json').read_text(encoding='utf-8'))
    assert [(r['start_s'], r['end_s'], r['text']) for r in left_out] == [(2.0, 2.68, '对')]
    japanese = [c for c in final if 'はいはい' in c['text'] or '確かに' in c['text']]
    assert (japanese[0]['start_s'], japanese[-1]['end_s']) == (5.8, 12.229)     # cues 4-6, whole
    assert pg.gaps_with_speech(final, parse_srt(ANSWERED)) == []      # what step 8 looks for
    assert not any('句内不拆' in line for line in outcome.process)       # the release shape stands: U138 lost no word


def test_a_lone_character_only_a_cut_leaves_alone_stays_in_its_sentence(tmp_path, monkeypatch):
    # The agent wrote 「对，一边用来应付教授」 as one sentence and gave it two parts; the first, 「对」, has no touching
    # neighbour.  Leaving it out would lose a word of a longer sentence: the release shape is refused, the sentence
    # stays one line over its parts' time (the agent's own words, as before).
    from src.autoslice.manual_lane import subtitle_track
    from src.autoslice.subtitle_validation import validate_srt_file

    keep = {'confidence': '高', 'affects_meaning': False}
    rows = answer_rows([
        {'id': 'd001', 'state': 'KEEP', 'final_text': '这是真情实感', **keep},
        {'id': 'd002', 'state': 'ADOPT', 'final_text': '', 'drop': True, 'reason': '并入d003', 'parts': [], **keep},
        {'id': 'd003', 'state': 'ADOPT', 'final_text': '对，一边用来应付教授', **keep,
         'parts': [{'text': '对，', 'first_cue': 2, 'last_cue': 2}, {'text': '一边用来应付教授', 'first_cue': 3, 'last_cue': 3}]}])
    cpa = AgentCpa([], answers=[{'action': 'plan', 'doubts': [], 'tools': [{'tool': 'words', 'start': 0, 'end': 6}]}, {'action': 'draft', 'rows': rows},
                                {'action': 'final', 'self_check': 'checked', 'rows': rows}])
    ctx = context(tmp_path, monkeypatch, cpa)
    (ctx.clip_dir / 'asr/bcut.srt').write_text(ANSWERED.split('\n\n4\n')[0] + '\n')
    outcome = step._step3_body(ctx)
    assert outcome.ok and len(cpa.calls) == 3 and any('句内不拆' in line for line in outcome.process)
    path = ctx.clip_dir / 'subs/final.srt'
    assert validate_srt_file(path)['status'] == 'PASS'
    final = subtitle_track.parse_blocks(path.read_text())
    assert [(c['start_s'], c['end_s'], c['text']) for c in final] == [(0.0, 1.5, '这是真情实感'), (2.0, 5.8, '对，一边用来应付教授')]


def test_rows_without_parts_take_u138_for_a_lone_character(tmp_path, monkeypatch):
    # No row gives parts: the rows' own lines (decided_cues).  The 「对」 touching the next sentence joins it; the 「行」
    # after a silence is not subtitled.  Both used to stand alone in final.srt, refused on release.
    from src.autoslice.manual_lane import subtitle_track
    from src.autoslice.subtitle_validation import validate_srt_file

    keep = {'state': 'KEEP', 'confidence': '高', 'affects_meaning': False}
    rows = answer_rows([{'id': 'd001', 'final_text': '这是真情实感', **keep}, {'id': 'd002', 'final_text': '对', **keep},
                        {'id': 'd003', 'final_text': '一边用来应付教授', **keep}, {'id': 'd004', 'final_text': '行', **keep},
                        {'id': 'd005', 'final_text': '我说好的好的', **keep}])
    cpa = AgentCpa([], answers=[{'action': 'plan', 'doubts': [], 'tools': []}, {'action': 'draft', 'rows': rows},
                                {'action': 'final', 'self_check': 'checked', 'rows': rows}])
    ctx = context(tmp_path, monkeypatch, cpa)
    (ctx.clip_dir / 'asr/bcut.srt').write_text(
        '1\n00:00:00,000 --> 00:00:01,500\n这是真情实感\n\n2\n00:00:02,000 --> 00:00:02,600\n对\n\n'
        '3\n00:00:02,600 --> 00:00:05,000\n一边用来应付教授\n\n4\n00:00:06,000 --> 00:00:06,500\n行\n\n'
        '5\n00:00:07,500 --> 00:00:09,000\n我说好的好的\n')
    outcome = step._step3_body(ctx)
    assert outcome.ok and len(cpa.calls) == 3
    path = ctx.clip_dir / 'subs/final.srt'
    assert validate_srt_file(path)['status'] == 'PASS'
    final = subtitle_track.parse_blocks(path.read_text())
    assert [(c['start_s'], c['end_s'], c['text']) for c in final] == [
        (0.0, 1.5, '这是真情实感'), (2.0, 5.0, '对，一边用来应付教授'), (7.5, 9.0, '我说好的好的')]
    left_out = json.loads((ctx.clip_dir / 'subs/lone_dropped.json').read_text(encoding='utf-8'))
    assert [(r['start_s'], r['text']) for r in left_out] == [(6.0, '行')]


def test_a_long_sentence_the_shape_cuts_never_leaves_a_lone_character_piece(tmp_path, monkeypatch):
    # A 31 s sentence is cut into pieces by its duration.  The cut used to end on 「对。」 alone (punctuation was
    # counted as a character): the release check refused it, and the hand-in check, looking for that piece's row,
    # threw TypeError (a cut piece has no id) and sent the same rows back as "invalid selected parts" until the cap.
    from src.autoslice.subtitle_validation import validate_srt_file

    text = '我觉得这个东西其实还挺好玩的你们觉得呢。对。'
    rows = answer_rows([{'id': 'd001', 'state': 'ADOPT', 'final_text': text, 'confidence': '高', 'affects_meaning': False,
                         'parts': [{'text': text, 'first_cue': 1, 'last_cue': 1}]}])
    cpa = AgentCpa([], answers=[{'action': 'plan', 'doubts': [], 'tools': [{'tool': 'words', 'start': 0, 'end': 31}]}, {'action': 'draft', 'rows': rows},
                                {'action': 'final', 'self_check': 'checked', 'rows': rows}])
    ctx = context(tmp_path, monkeypatch, cpa)
    (ctx.clip_dir / 'asr/bcut.srt').write_text('1\n00:00:00,000 --> 00:00:31,000\n我觉得这个东西其实还挺好玩的你们觉得呢对\n')
    outcome = step._step3_body(ctx)
    assert outcome.ok and len(cpa.calls) == 3
    assert not [e for e in trace_of(ctx)['events'] if e.get('event') == 'protocol_error']
    assert validate_srt_file(ctx.clip_dir / 'subs/final.srt')['status'] == 'PASS'


def test_words_the_parts_moved_to_a_neighbour_are_not_lost_when_the_release_shape_is_not_applied(tmp_path, monkeypatch):
    # 「今晚要」 stays a kept row whose own parts are empty: its words stand in the next row's part.  Where the release
    # shape is refused (here: it cannot lay out a line it joined), the lines written instead must still hold every
    # word the parts hold.
    from src.autoslice.manual_lane import subtitle_track

    def unshapable(*_args, **_kwargs):
        raise ValueError('cannot split subtitle within visual limits without starting a line with punctuation')

    rows = answer_rows([
        {'id': 'd001', 'state': 'ADOPT', 'final_text': '今晚要', 'confidence': '高', 'affects_meaning': False, 'parts': []},
        {'id': 'd002', 'state': 'ADOPT', 'final_text': '喝奶茶', 'confidence': '高', 'affects_meaning': False,
         'parts': [{'text': '今晚要喝奶茶', 'first_cue': 1, 'last_cue': 2}]}])
    cpa = AgentCpa([], answers=[{'action': 'plan', 'doubts': [], 'tools': [{'tool': 'words', 'start': 0, 'end': 4}]},
                                {'action': 'draft', 'rows': rows}, {'action': 'final', 'self_check': 'checked', 'rows': rows}])
    ctx = context(tmp_path, monkeypatch, cpa)
    (ctx.clip_dir / 'asr/bcut.srt').write_text('1\n00:00:02,000 --> 00:00:03,000\n今晚要\n\n'
                                               '2\n00:00:03,000 --> 00:00:04,000\n喝奶茶\n')
    monkeypatch.setattr(pb.normalize, 'release_shape', unshapable)
    outcome = step._step3_body(ctx)
    assert outcome.ok and len(cpa.calls) == 3 and any('句内不拆' in line for line in outcome.process)
    final = subtitle_track.parse_blocks((ctx.clip_dir / 'subs/final.srt').read_text())
    assert [(c['start_s'], c['end_s'], c['text']) for c in final] == [(2.0, 4.0, '今晚要喝奶茶')]


# semantictalk_298620_487180_11242159, cut down: the lead run wrote a title in 《》 of 21 characters; a
# subtitle line holds 18 and a bracketed title is never cut, so writing final.srt threw ValueError and step 3 stopped
TITLED = '1\n00:00:00,000 --> 00:00:02,000\n你们知道吗\n\n2\n00:00:02,000 --> 00:00:04,000\nLoveLive Sunshine吗\n'


def titled_rows(text):
    return answer_rows([{'id': 'd001', 'state': 'KEEP', 'final_text': '你们知道吗', 'confidence': '高', 'affects_meaning': False},
                        {'id': 'd002', 'state': 'ADOPT', 'final_text': text, 'confidence': '高', 'affects_meaning': False}])


def test_a_title_longer_than_a_line_is_sent_back_with_its_sentence_and_why(tmp_path, monkeypatch):
    titled, plain = titled_rows('《LoveLive! Sunshine!!》吗？'), titled_rows('LoveLive! Sunshine!!吗？')
    cpa = AgentCpa([], answers=[{'action': 'plan', 'doubts': [], 'tools': []}, {'action': 'draft', 'rows': titled},
                                {'action': 'final', 'self_check': 'checked', 'rows': titled},
                                {'action': 'final', 'self_check': 'the title without its brackets', 'rows': plain}])
    ctx = context(tmp_path, monkeypatch, cpa)
    (ctx.clip_dir / 'asr/bcut.srt').write_text(TITLED)
    outcome = step._step3_body(ctx)
    assert outcome.ok and len(cpa.calls) == 4
    told = [e['unfit'] for e in trace_of(ctx)['events'] if e.get('unfit')]
    assert [[(t['id'], t['text']) for t in unfit] for unfit in told] == [[('d002', '《LoveLive! Sunshine!!》吗？')]]
    assert '《LoveLive! Sunshine!!》' in told[0][0]['why'] and pb.UNFIT_NOTE in cpa.prompts[3]
    assert 'LoveLive!\nSunshine!!吗？' in (ctx.clip_dir / 'subs/final.srt').read_text()


def test_rows_release_keeps_refusing_stop_at_the_call_cap_by_name(tmp_path, monkeypatch):
    monkeypatch.setattr(pb, 'MAX_TEXT_CALLS', 4)
    titled = titled_rows('《LoveLive! Sunshine!!》吗？')
    cpa = AgentCpa([], answers=[{'action': 'plan', 'doubts': [], 'tools': []}, {'action': 'draft', 'rows': titled},
                                {'action': 'final', 'self_check': 'checked', 'rows': titled},
                                {'action': 'final', 'self_check': 'checked again', 'rows': titled}])
    ctx = context(tmp_path, monkeypatch, cpa)
    (ctx.clip_dir / 'asr/bcut.srt').write_text(TITLED)
    outcome = step._step3_body(ctx)
    assert outcome.reason == 'CPA_AGENT_CALL_CAP' and len(cpa.calls) == 4
    assert trace_of(ctx)['status'] == 'REFUSED' and not (ctx.clip_dir / 'subs/final.srt').exists()


MOVED = ('1\n00:00:00,000 --> 00:00:01,500\n这是真情实感\n\n2\n00:00:02,000 --> 00:00:03,000\n我跟你说嗨嗨\n\n'
         '3\n00:00:03,000 --> 00:00:09,000\n社死你涩死了涩涩死你\n\n4\n00:00:09,000 --> 00:00:10,500\n然后脑子早就飞到直播间\n')


def moved_rows(parts=None):
    keep = {'state': 'KEEP', 'confidence': '高', 'affects_meaning': False}
    return answer_rows([
        {'id': 'd001', 'final_text': '这是真情实感', **keep},
        {'id': 'd002', 'state': 'ADOPT', 'final_text': '我跟你说はいはい、そうですね', 'confidence': '高',
         'affects_meaning': False, **({'parts': parts} if parts else {})},
        {'id': 'd003', 'state': 'ADOPT', 'final_text': '', 'drop': True, 'confidence': '高', 'affects_meaning': False,
         'parts': [], 'reason': '并入d002'},
        {'id': 'd004', 'final_text': '然后脑子早就飞到直播间', **keep}])


def test_a_stretch_given_neither_a_line_nor_a_name_is_sent_back_again(tmp_path, monkeypatch):
    # The line took in the next sentence (Japanese: no character of it matches what the recogniser wrote there), the
    # agent gave it no time and, asked, handed in the same rows.  Handing in the same rows decides nothing: taken as
    # "kept without a line", 6 s of her speech went out with no subtitle and the package step no longer stopped for it.
    from src.autoslice.manual_lane import package_guards as pg, subtitle_track
    from src.autoslice.manual_lane.srt_text import parse_srt

    moved = moved_rows()
    timed = moved_rows([{'text': '我跟你说はいはい、そうですね', 'first_cue': 2, 'last_cue': 3}])
    cpa = AgentCpa([], answers=[{'action': 'plan', 'doubts': [], 'tools': [{'tool': 'words', 'start': 0, 'end': 11}]},
                                {'action': 'draft', 'rows': moved}, {'action': 'final', 'self_check': 'checked', 'rows': moved},
                                {'action': 'final', 'self_check': 'checked again', 'rows': moved},
                                {'action': 'final', 'self_check': 'the line keeps the time', 'rows': timed}])
    ctx = context(tmp_path, monkeypatch, cpa)
    (ctx.clip_dir / 'asr/bcut.srt').write_text(MOVED)
    assert step._step3_body(ctx).ok and len(cpa.calls) == 5
    told = [e['unsubtitled'] for e in trace_of(ctx)['events'] if e.get('unsubtitled')]
    assert [[(g['start_s'], g['end_s']) for g in gaps] for gaps in told] == [[(3.0, 9.0)], [(3.0, 9.0)]]
    final = subtitle_track.parse_blocks((ctx.clip_dir / 'subs/final.srt').read_text())
    assert pg.gaps_with_speech(final, parse_srt(MOVED)) == [] and not (ctx.clip_dir / pg.DECIDED_FILE).exists()
