import json
from pathlib import Path

import pytest

from src.autoslice.semantic_retry_progress import SemanticAttemptRecorder, RetryNeedsPlan

SRT = '1\n00:00:01,000 --> 00:00:02,000\n亚里诺科斯塔\n'


def invoke(calls, *, question, plan=None, material=None):
    def llm(prompt):
        calls.append(prompt)
        return '{"decision":"UNRESOLVED","needs_audio":true}'
    llm.cpa_cache_identity = {'model': 'test'}
    recorder = SemanticAttemptRecorder(srt_text=SRT,
        finding={'cue_index': 1, 'why': question, 'continuation_plan': plan, **(material or {})},
        clip_context={'source_media_sha256': 'a' * 64}, offset_ms=0)
    result = recorder.wrap(llm)(question)
    return result, recorder.audit()


def test_rephrasing_allowed_then_concrete_plan_not_hard_cap(tmp_path, monkeypatch):
    monkeypatch.setenv('AUTOSLICE_BASE', str(tmp_path))
    calls=[]
    for question in ['是否中文昵称', '是否日文歌词', '先定位句尾']:
        invoke(calls, question=question)
    # Identical input replays the actual raw bytes even after three failures.
    _, audit = invoke(calls, question='先定位句尾')
    assert len(calls) == 3 and audit['events'][0]['status'] == 'EXACT_RAW_REPLAY'
    with pytest.raises(RetryNeedsPlan, match='CONTINUATION_VALUE_PLAN_REQUIRED'):
        invoke(calls, question='再猜一次')
    plan = {'action': 'reframe_question', 'question': '分别对日语词句与无词哼唱定位',
            'why_previous_failed': '此前把日语词句与后续哼唱当成一个中文名字',
            'expected_gain': '保留可辨认的日语词句，单独判断无词尾部',
            'verification': '比较已有整段听写中词句结束位置与当前cue窗口'}
    _, audit = invoke(calls, question=plan['question'], plan=plan)
    assert len(calls) == 4 and audit['events'][0]['scrutiny'] == 'INCREASED'
    assert audit['events'][0]['prior_unresolved_answers'] == 3
    # A real added observation plus a concrete question permits continued work.
    invoke(calls, question='按新增整段词时间定位', plan={**plan, 'action': 'expand_context',
           'question': '按新增整段词时间定位', 'expected_gain': '用新整段听写确定词句与哼唱的边界'},
           material={'_prior_range_acoustic_observations': [{'raw_sha256': 'b' * 64}]})
    assert len(calls) == 5
    files=list((tmp_path / 'cache/semantic-cpa-attempts').rglob('*.json'))
    assert len(files) == 5
    assert all(json.loads(p.read_text())['continuation_reason']['basis'] for p in files)


def test_unrelated_srt_edit_does_not_reopen_an_already_failed_target_plan(tmp_path,monkeypatch):
    monkeypatch.setenv('AUTOSLICE_BASE',str(tmp_path))
    plan={'action':'reframe_question','question':'区分目标是词句还是无词哼唱',
        'why_previous_failed':'旧问题把两者混为一谈','expected_gain':'按目标词界区分',
        'verification':'比较已有词时间和目标窗'}
    finding={'cue_index':1,'why':'同一目标问题','continuation_plan':plan}
    calls=[]
    def call(prompt):
        calls.append(prompt)
        return '{"decision":"UNRESOLVED"}'
    call.cpa_cache_identity={'model':'test'}
    context={'source_media_sha256':'a'*64}
    for question in ('词界问法一','词界问法二','词界问法三'):
        SemanticAttemptRecorder(srt_text=SRT,finding=finding,clip_context=context,offset_ms=0).wrap(call)(question)
    changed=SRT+'\n2\n00:00:03,000 --> 00:00:04,000\n其他字幕修好了\n'
    with pytest.raises(RetryNeedsPlan,match='CONTINUATION_PLAN_ALREADY_UNRESOLVED'):
        SemanticAttemptRecorder(srt_text=changed,finding=finding,clip_context=context,offset_ms=0).wrap(call)('再问旧计划')
    assert len(calls)==3
    fresh={**finding,'continuation_plan':{**plan,'action':'correct_localization',
        'question':'按已保存目标marker排除垫片邻句','expected_gain':'纠正原先目标窗错位'}}
    SemanticAttemptRecorder(srt_text=changed,finding=fresh,clip_context=context,offset_ms=0).wrap(call)('新定位问题')
    assert len(calls)==4


def test_cold_process_replays_raw_and_preserves_attempts(tmp_path, monkeypatch):
    import os
    import subprocess
    import sys
    monkeypatch.setenv('AUTOSLICE_BASE', str(tmp_path))
    invoke([], question='是否日文歌词')
    code = '''from test_semantic_retry_progress import invoke
calls=[]
raw,audit=invoke(calls,question='是否日文歌词')
assert not calls
assert audit['events'][0]['status']=='EXACT_RAW_REPLAY'
print(raw)
'''
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).parent)+os.pathsep+str(Path(__file__).parents[1]))
    child = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True, env=env)
    assert child.returncode == 0, child.stderr
    assert json.loads(child.stdout)['decision'] == 'UNRESOLVED'


def test_uncaptured_started_call_does_not_blindly_resend(tmp_path, monkeypatch):
    monkeypatch.setenv('AUTOSLICE_BASE', str(tmp_path))
    calls=[]
    invoke(calls, question='问题')
    p=next((tmp_path / 'cache/semantic-cpa-attempts').rglob('*.json'))
    data=json.loads(p.read_text())
    data['status']='STARTED'
    data.pop('completion')
    p.write_text(json.dumps(data))
    with pytest.raises(RetryNeedsPlan, match='PRIOR_CALL_OUTCOME_UNKNOWN'):
        invoke(calls, question='问题')
    assert len(calls)==1


def test_corrupt_raw_record_blocks_without_new_call(tmp_path, monkeypatch):
    monkeypatch.setenv('AUTOSLICE_BASE', str(tmp_path))
    calls=[]
    invoke(calls, question='问题')
    p=next((tmp_path / 'cache/semantic-cpa-attempts').rglob('*.json'))
    data=json.loads(p.read_text())
    data['completion']='{"decision":"KEEP_EXISTING"}'
    p.write_text(json.dumps(data))
    with pytest.raises(RetryNeedsPlan, match='SEMANTIC_ATTEMPT_RECORD_INVALID'):
        invoke(calls, question='问题')
    assert len(calls)==1


@pytest.mark.parametrize('mode', [None, 'per_finding'])
def test_discovery_receives_actual_failed_questions_before_proposing_fourth_check(tmp_path, monkeypatch, mode):
    from src.autoslice.semantic_retry_progress import retry_context
    monkeypatch.setenv('AUTOSLICE_BASE', str(tmp_path))
    if mode is None:
        monkeypatch.delenv('AUTOSLICE_FINAL_REVIEW_MODE', raising=False)
    else:
        monkeypatch.setenv('AUTOSLICE_FINAL_REVIEW_MODE', mode)
    calls=[]
    for question in ['是否中文名字', '是否日语词句', '定位哼唱起点']:
        invoke(calls, question=question)
    context = retry_context(SRT, {'source_media_sha256': 'a' * 64}, 0)
    if mode is None:
        assert '"unresolved_answers": 3' in context and 'continuation_plan' in context
        assert all(question in context for question in calls)
    else:
        assert context == ''
        assert all(question not in context for question in calls)
    assert retry_context(SRT, {'source_media_sha256': 'b' * 64}, 0) == ''
    assert len(calls) == 3


def error_recorder(call, *, plan=None):
    call.cpa_cache_identity = {'model': 'test'}
    return SemanticAttemptRecorder(srt_text=SRT,
        finding={'cue_index': 1, 'why': '问题', 'continuation_plan': plan},
        clip_context={'source_media_sha256': 'a' * 64}, offset_ms=0)


@pytest.mark.parametrize('failure_kind', ['timeout', 'command_timeout', 'transport'])
def test_transport_exception_preserves_unknown_and_never_blindly_redispatches(tmp_path, monkeypatch, failure_kind):
    from src.autoslice.llm_client import LlmCallError
    monkeypatch.setenv('AUTOSLICE_BASE', str(tmp_path))
    calls=[]
    def fail(prompt):
        calls.append(prompt)
        if failure_kind == 'timeout':
            raise TimeoutError('response lost')
        if failure_kind == 'command_timeout':
            raise LlmCallError('timeout', safe_reason='LLM_COMMAND_TIMEOUT')
        raise ConnectionError('connection lost after possible send')
    recorder=error_recorder(fail)
    with pytest.raises((TimeoutError, LlmCallError, ConnectionError)):
        recorder.wrap(fail)('问题')
    assert recorder.audit()['status'] == 'CALL_OUTCOME_UNKNOWN'
    assert recorder.audit()['events'][-1]['provider_calls'] is None
    p=next((tmp_path / 'cache/semantic-cpa-attempts').rglob('*.json'))
    original=p.read_bytes()
    assert json.loads(original)['status']=='OUTCOME_UNKNOWN'
    with pytest.raises(RetryNeedsPlan, match='PRIOR_CALL_OUTCOME_UNKNOWN'):
        error_recorder(fail).wrap(fail)('问题')
    # A new plan alone is not evidence that the identical transport input was
    # never sent. Material/question and physical target remain unchanged.
    plan={'action':'reframe_question','question':'same transport question',
          'why_previous_failed':'timeout','expected_gain':'retry','verification':'read reply'}
    with pytest.raises(RetryNeedsPlan, match='PRIOR_CALL_OUTCOME_UNKNOWN'):
        error_recorder(fail, plan=plan).wrap(fail)('问题')
    assert calls==['问题'] and p.read_bytes()==original


@pytest.mark.parametrize('stored_status', ['ERROR', 'OUTCOME_UNKNOWN'])
def test_ambiguous_error_stays_unknown_across_cold_process(tmp_path, monkeypatch, stored_status):
    import os
    import subprocess
    import sys
    monkeypatch.setenv('AUTOSLICE_BASE', str(tmp_path))
    calls=[]
    invoke(calls, question='问题')
    p=next((tmp_path / 'cache/semantic-cpa-attempts').rglob('*.json'))
    data=json.loads(p.read_text())
    data.update(status=stored_status, error_type='TimeoutError')
    data.pop('completion')
    data.pop('completion_sha256')
    p.write_text(json.dumps(data))
    original=p.read_bytes()
    code='''from test_semantic_retry_progress import invoke
from src.autoslice.semantic_retry_progress import RetryNeedsPlan
calls=[]
try:
    invoke(calls,question='问题')
except RetryNeedsPlan as exc:
    assert str(exc)=='PRIOR_CALL_OUTCOME_UNKNOWN'
else:
    raise AssertionError('legacy ambiguous call was resent')
assert not calls
'''
    env=dict(os.environ, PYTHONPATH=str(Path(__file__).parent)+os.pathsep+str(Path(__file__).parents[1]))
    child=subprocess.run([sys.executable, '-c', code], capture_output=True, text=True, env=env)
    assert child.returncode==0, child.stderr
    assert p.read_bytes()==original


def test_only_explicit_pre_dispatch_failure_allows_same_request_retry(tmp_path, monkeypatch):
    from src.autoslice.semantic_retry_progress import CallNotDispatched
    monkeypatch.setenv('AUTOSLICE_BASE', str(tmp_path))
    dispatches=[]
    def call(prompt):
        raise CallNotDispatched('LOCAL_PRE_DISPATCH_REJECTED')
    recorder=error_recorder(call)
    with pytest.raises(CallNotDispatched):
        recorder.wrap(call)('问题')
    p=next((tmp_path / 'cache/semantic-cpa-attempts').rglob('*.json'))
    assert json.loads(p.read_text())['status']=='NOT_DISPATCHED'
    assert recorder.audit()['events'][-1]['provider_calls']==0
    def repaired(prompt):
        dispatches.append(prompt)
        return '{"decision":"UNRESOLVED","needs_audio":true}'
    raw=error_recorder(repaired).wrap(repaired)('问题')
    assert dispatches==['问题']
    saved=json.loads(p.read_text())
    assert saved['completion']==raw
    assert saved['prior_not_dispatched_failures'][0]['reason_code']=='LOCAL_PRE_DISPATCH_REJECTED'
    assert error_recorder(repaired).wrap(repaired)('问题')==raw
    assert dispatches==['问题']


def test_generic_exception_cannot_assert_non_dispatch_with_attributes(tmp_path, monkeypatch):
    monkeypatch.setenv('AUTOSLICE_BASE', str(tmp_path))
    calls=[]
    def fail(prompt):
        calls.append(prompt)
        exc=RuntimeError('not sent')
        exc.dispatched=False
        exc.dispatch_outcome='NOT_DISPATCHED'
        raise exc
    with pytest.raises(RuntimeError, match='not sent'):
        error_recorder(fail).wrap(fail)('问题')
    with pytest.raises(RetryNeedsPlan, match='PRIOR_CALL_OUTCOME_UNKNOWN'):
        error_recorder(fail).wrap(fail)('问题')
    assert calls==['问题']


@pytest.mark.parametrize('stored_status', ['STARTED', 'ERROR', 'OUTCOME_UNKNOWN'])
def test_new_material_field_cannot_launder_unknown_transport(tmp_path, monkeypatch, stored_status):
    monkeypatch.setenv('AUTOSLICE_BASE', str(tmp_path))
    calls = []
    def call(prompt):
        calls.append(prompt)
        return '{"decision":"UNRESOLVED"}'
    old = error_recorder(call)
    old.wrap(call)('same actual transport input')
    path, = (tmp_path / 'cache/semantic-cpa-attempts').rglob('*.json')
    row = json.loads(path.read_text())
    row.update(status=stored_status)
    row.pop('completion')
    row.pop('completion_sha256')
    path.write_text(json.dumps(row))
    original = path.read_bytes()
    new = error_recorder(call)
    new.material = {**new.material, 'bound_judge_evidence': [],
                    'future_diagnostic_metadata': 'not a new transport input'}
    with pytest.raises(RetryNeedsPlan, match='PRIOR_CALL_OUTCOME_UNKNOWN'):
        new.wrap(call)('same actual transport input')
    assert calls == ['same actual transport input']
    assert path.read_bytes() == original


def test_same_completed_transport_replays_across_material_format(tmp_path, monkeypatch):
    monkeypatch.setenv('AUTOSLICE_BASE', str(tmp_path))
    calls = []
    def call(prompt):
        calls.append(prompt)
        return '{"decision":"UNRESOLVED"}'
    original = error_recorder(call).wrap(call)('same completed input')
    files = {p: p.read_bytes() for p in (tmp_path / 'cache/semantic-cpa-attempts').rglob('*.json')}
    upgraded = error_recorder(call)
    upgraded.material = {**upgraded.material, 'bound_judge_evidence': []}
    assert upgraded.wrap(call)('same completed input') == original
    assert calls == ['same completed input']
    assert {p: p.read_bytes() for p in files} == files
    assert upgraded.audit()['events'][-1]['status'] == 'EXACT_RAW_REPLAY'
