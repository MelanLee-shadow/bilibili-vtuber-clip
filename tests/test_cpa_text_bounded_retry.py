"""Offline transport-level controls; never run a bridge or a model."""
import json
import io
import subprocess
import os
import sys
from contextlib import nullcontext
from email.message import Message
from pathlib import Path
from urllib.error import HTTPError

import pytest

from src.autoslice import infra_retry_policy as retry
from src.autoslice import llm_client as llm
from src.autoslice.cpa_runtime import bind_observation


def test_shadow_shell_imports_its_bound_repo_ahead_of_foreign_cwd(tmp_path):
    from test_subprocess_repo_bootstrap import (ROOT, shell_python_blocks,
        repo_imports, minimal_import_script)

    foreign = tmp_path / 'foreign'
    (foreign / 'src').mkdir(parents=True)
    (foreign / 'src' / '__init__.py').write_text(
        'raise RuntimeError("FOREIGN_CWD_SRC_SELECTED")\n')
    text = (ROOT / 'scripts/run_oci3_shadow_date_once.sh').read_text()
    found = []
    for number, command, code in shell_python_blocks(text):
        if 'PYTHONPATH=' not in command or not repo_imports(code)[1]:
            continue
        # Execute only the real bootstrap/import AST, never prepare, copy,
        # credentials, provider, deployment, or recorder operations.
        probe = minimal_import_script(code, repo=ROOT)
        env = {**os.environ, 'PYTHONPATH': str(ROOT), 'PYTHONDONTWRITEBYTECODE': '1'}
        completed = subprocess.run([sys.executable, '-c', probe], cwd=foreign,
            env=env, text=True, capture_output=True, timeout=60)
        assert completed.returncode == 0, f'{number}: {completed.stderr}'
        found.append(number)
    assert found, 'discover and exercise every repo-importing stdin child'


def no_response():
    return llm.LlmCallError('command failed without response', safe_reason='LLM_COMMAND_FAILED')


class FakeClock:
    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


def make_bridge(monkeypatch, *, failures=2, error=None, runtime_root=None, cpa=True,
                failure_stderr='', success_text='the successful response'):
    attempts, clock = [], FakeClock()
    sleeps = clock.sleeps
    monkeypatch.setattr(retry.time, 'sleep', clock.sleep)
    monkeypatch.setattr(retry.time, 'monotonic', clock)
    monkeypatch.setattr(retry.random, 'uniform', lambda low, high: 0.0)
    monkeypatch.setattr(llm, 'runtime_provider_slot', lambda **kwargs: nullcontext())

    def run(command, **kwargs):
        attempts.append(command)
        if len(attempts) <= failures:
            if error is not None:
                raise error
            return subprocess.CompletedProcess(command, 1, '', failure_stderr)
        completion = Path(command[3])
        completion.write_text(success_text, encoding='utf-8')
        Path(str(completion) + '.provenance.json').write_text(
            json.dumps({'answering_model': 'offline-answering-model'}))
        return subprocess.CompletedProcess(command, 0, '', '')

    monkeypatch.setattr(llm.subprocess, 'run', run)
    from conftest import _REAL_CALL_COMMAND
    # These are transport-adapter tests, not stage tests. Install the fake
    # subprocess before opening only the adapter seam guarded by conftest.
    # No command can reach a real shell/CPA channel through this fixture.
    monkeypatch.setattr(llm, '_call_command', _REAL_CALL_COMMAND)
    bridge = 'llm_via_cpa.sh' if cpa else 'custom-text-command'
    call = llm.build_llm_call(llm.LlmConfig(transport='command',
        command_template=f'bash {bridge} {{prompt_file}} {{completion_file}} offline medium 1',
        runtime_root=str(runtime_root) if runtime_root else None))
    return call, attempts, sleeps


def test_public_text_call_consumes_third_response_and_retains_two_unknowns(tmp_path, monkeypatch):
    call, attempts, sleeps = make_bridge(monkeypatch)
    trace = tmp_path / 'usage.jsonl'
    with bind_observation(trace, 'offline-attempt'):
        answer = call('the same prompt')
    rows = [json.loads(line) for line in trace.read_text().splitlines()]
    unknowns = [row for row in rows if row['event_type'] == 'text_retry_attempt']
    assert str(answer) == 'the successful response'
    assert answer.answering_model == 'offline-answering-model'
    assert len(attempts) == 3
    assert [row['status'] for row in unknowns] == ['UNKNOWN', 'UNKNOWN']
    assert [row['attempt'] for row in unknowns] == [1, 2]
    assert len({row['logical_call_id'] for row in unknowns}) == 1
    assert sleeps == [0.0, retry.CPA_TEXT_RETRY_BASE_SECONDS]


def test_public_text_call_consumes_sixth_response_with_five_unknowns(tmp_path, monkeypatch):
    call, attempts, sleeps = make_bridge(monkeypatch, failures=5)
    trace = tmp_path / 'usage.jsonl'
    with bind_observation(trace, 'sixth-response'):
        answer = call('the same prompt')
    unknowns = [json.loads(line) for line in trace.read_text().splitlines()
                if json.loads(line)['event_type'] == 'text_retry_attempt']
    assert answer == 'the successful response'
    assert answer.answering_model == 'offline-answering-model'
    assert len(attempts) == 6
    assert sleeps == [0, 2, 4, 8, 16]
    assert [row['status'] for row in unknowns] == ['UNKNOWN'] * 5
    assert [row['attempt'] for row in unknowns] == [1, 2, 3, 4, 5]
    assert [row['delay_seconds'] for row in unknowns] == sleeps
    assert [row['waited_seconds'] for row in unknowns] == [0, 0, 2, 4, 8]
    assert {row['error_category'] for row in unknowns} == {'NO_RESPONSE_COMMAND'}
    assert len({row['logical_call_id'] for row in unknowns}) == 1


@pytest.mark.parametrize('endpoint', ['low', 'high'])
def test_exponential_jitter_stays_within_twenty_percent_and_sixty_second_cap(endpoint):
    calls = []

    def jitter(low, high):
        calls.append((low, high))
        return low if endpoint == 'low' else high

    delays = [retry.cpa_text_retry_delay_seconds(attempt, no_response(), jitter=jitter)
              for attempt in range(1, 10)]
    assert delays[0] == 0
    for delay, base in zip(delays[1:], [2, 4, 8, 16, 32, 60, 60, 60]):
        expected = min(60, base * (0.8 if endpoint == 'low' else 1.2))
        assert delay == pytest.approx(expected)
    assert calls == [(-0.2, 0.2)] * 8
    assert max(delays) <= 60


@pytest.mark.parametrize('status', [429, 503])
def test_public_command_retries_explicit_transient_response_with_retry_after(tmp_path, monkeypatch, status):
    marker = llm.PROVIDER_DIAGNOSTIC_MARKER + json.dumps({
        'provider_http_status': status, 'provider_retry_after_seconds': 30})
    call, attempts, sleeps = make_bridge(monkeypatch, failures=1, failure_stderr=marker)
    trace = tmp_path / 'usage.jsonl'
    with bind_observation(trace, 'transient-http'):
        assert call('same prompt') == 'the successful response'
    rows = [json.loads(line) for line in trace.read_text().splitlines()]
    unknowns = [row for row in rows if row['event_type'] == 'text_retry_attempt']
    assert len(attempts) == 2
    assert sleeps == [30]
    assert len(unknowns) == 1
    assert unknowns[0]['error_category'] == f'HTTP_{status}'
    assert unknowns[0]['status'] == 'UNKNOWN'


@pytest.mark.parametrize('status', [429, 503])
def test_public_direct_transient_http_does_not_multiply_inner_ladder(tmp_path, monkeypatch, status):
    attempts, clock = [], FakeClock()
    monkeypatch.setenv('OFFLINE_CPA_TEST_KEY', 'offline-test-value')
    monkeypatch.setattr(retry.time, 'sleep', clock.sleep)
    monkeypatch.setattr(retry.time, 'monotonic', clock)
    monkeypatch.setattr(retry.random, 'uniform', lambda low, high: 0.0)
    monkeypatch.setattr(llm, 'runtime_provider_slot', lambda **kwargs: nullcontext())

    def transport(prompt, config):
        attempts.append(config.model)
        raise llm.LlmCallError('transient response', response_received=True,
            provider_diagnostics={'provider_http_status': status})

    monkeypatch.setattr(llm, '_call_direct_once', transport)
    call = llm.build_llm_call(llm.LlmConfig(transport='direct', model=llm.TOP_CPA_MODEL,
        api_base='https://offline.invalid/v1', api_key_env='OFFLINE_CPA_TEST_KEY'))
    trace = tmp_path / 'usage.jsonl'
    with bind_observation(trace, 'direct-http-budget'):
        with pytest.raises(llm.LlmCallError, match=retry.CPA_TEXT_RETRY_EXHAUSTED):
            call('same prompt')
    rows = [json.loads(line) for line in trace.read_text().splitlines()]
    assert len(attempts) == 10
    assert sum(row.get('status') == 'UNKNOWN' for row in rows) == 10
    assert attempts == [llm.TOP_CPA_MODEL] * 10
    assert clock.sleeps == [0, 2, 4, 8, 16, 32, 60, 60, 60]


@pytest.mark.parametrize('header,reset,expected', [('30', 75, 75), ('90', 30, 90)])
def test_native_http_header_and_cpa_reset_body_use_larger_provider_delay(monkeypatch, header, reset, expected):
    headers = Message()
    headers['Retry-After'] = header
    body = io.BytesIO(json.dumps({'error': {'metadata': {'reset_seconds': reset}}}).encode())
    cause = HTTPError('https://offline.invalid/responses', 503, 'busy', headers, body)

    def fail(*args, **kwargs):
        raise cause

    monkeypatch.setenv('OFFLINE_CPA_TEST_KEY', 'offline-test-value')
    monkeypatch.setattr(llm.urllib.request, 'urlopen', fail)
    config = llm.LlmConfig(transport='direct', model=llm.TOP_CPA_MODEL,
        api_base='https://offline.invalid/v1', api_key_env='OFFLINE_CPA_TEST_KEY')
    with pytest.raises(llm.LlmCallError) as caught:
        llm._call_direct_once('prompt', config)
    assert caught.value.response_received is True
    assert retry.cpa_text_retry_delay_seconds(2, caught.value,
        jitter=lambda low, high: low) == expected


def test_public_command_cpa_reset_message_is_honored_with_retry_after(tmp_path, monkeypatch):
    marker = llm.PROVIDER_DIAGNOSTIC_MARKER + json.dumps({
        'provider_http_status': 429, 'provider_retry_after_seconds': 30,
        'provider_error_message': '{"reset_seconds": 75}'})
    call, attempts, sleeps = make_bridge(monkeypatch, failures=1, failure_stderr=marker)
    with bind_observation(tmp_path / 'usage.jsonl', 'reset-message'):
        assert call('prompt') == 'the successful response'
    assert len(attempts) == 2
    assert sleeps == [75]


def test_http_date_retry_after_is_a_lower_bound(monkeypatch):
    headers = Message()
    headers['Retry-After'] = 'Wed, 21 Oct 2015 07:28:00 GMT'
    error = llm.LlmCallError('busy', response_received=True,
        provider_diagnostics={'provider_http_status': 503})
    error.__cause__ = HTTPError('https://offline.invalid', 503, 'busy', headers, io.BytesIO(b'{}'))
    monkeypatch.setattr(retry.time, 'time', lambda: 1445412450.0)
    assert retry.cpa_text_retry_delay_seconds(1, error) == 30


def test_invalid_delay_hints_do_not_block_authorized_retry():
    error = no_response()
    error.retry_after_seconds = float('nan')
    error.reset_seconds = -1
    error.__cause__ = HTTPError('https://offline.invalid', 503, 'busy', Message(),
        io.BytesIO(b'{"reset_seconds": "invalid"}'))
    assert retry.cpa_text_retry_delay_seconds(2, error, jitter=lambda low, high: 0.0) == 2


def test_connection_reset_uses_same_entry_and_keeps_unknown():
    clock, rows, calls = FakeClock(), [], []

    def call():
        calls.append(1)
        if len(calls) == 1:
            raise ConnectionResetError('connection reset')
        return 'reply'

    assert retry.cpa_text_retry_call(call, record=rows.append,
        clock=clock, sleeper=clock.sleep) == 'reply'
    assert len(calls) == 2
    assert clock.sleeps == [0]
    assert rows[0]['status'] == 'UNKNOWN'
    assert rows[0]['error_category'] == 'CONNECTION_RESET'


@pytest.mark.parametrize('status', [200, 429, 503])
def test_explicit_content_refusal_is_never_retried_even_with_transient_status(status):
    error = llm.LlmCallError('refused', response_received=True,
        provider_diagnostics={'provider_http_status': status, 'provider_error_code': 'content_filter'})
    with pytest.raises(llm.LlmCallError):
        retry.cpa_text_retry_call(lambda: (_ for _ in ()).throw(error),
            record=lambda _: pytest.fail('UNKNOWN'), sleeper=lambda _: pytest.fail('sleep'))


@pytest.mark.parametrize('status', [500, 502, 504])
def test_other_received_server_statuses_do_not_expand_one_shot_retry_scope(status):
    error = llm.LlmCallError('response received', response_received=True,
        provider_diagnostics={'provider_http_status': status})
    assert retry.cpa_text_failure_category(error) is None
    with pytest.raises(llm.LlmCallError):
        retry.cpa_text_retry_call(lambda: (_ for _ in ()).throw(error),
            record=lambda _: pytest.fail('UNKNOWN'), sleeper=lambda _: pytest.fail('sleep'))


@pytest.mark.parametrize('stage', ['discovery', 'pronoun', 'correction'])
def test_ordinary_text_stages_consume_only_the_successful_reply(tmp_path, monkeypatch, stage):
    source = '1\n00:00:01,000 --> 00:00:02,000\n她来了\n'
    payloads = {
        'discovery': {'findings': []},
        'pronoun': {'decisions': [{'occurrence_id': 'cue-1-occurrence-1',
            'action': 'KEEP_CURRENT', 'current_token': '她', 'replacement_token': '她',
            'referent': 'HOST', 'reason': '当前说话人是已知女性'}]},
        'correction': {'cues': [{'n': 1, 'text': '她来了'}]},
    }
    call, attempts, _ = make_bridge(monkeypatch,
        success_text=json.dumps(payloads[stage], ensure_ascii=False))
    trace = tmp_path / 'usage.jsonl'
    with bind_observation(trace, 'stage-' + stage):
        if stage == 'discovery':
            from src.autoslice.final_review_auditor import audit_final_subtitles
            assert audit_final_subtitles(source, llm_call=call, extract_json=json.loads) == []
        elif stage == 'pronoun':
            from src.autoslice.pronoun_consistency import discover_candidate_pronoun_findings
            findings, audit = discover_candidate_pronoun_findings(source,
                policy_text='已知女性用她', candidate_context_text='说话人为女性',
                llm_call=call, extract_json=json.loads)
            assert findings == []
            assert audit['status'] == 'PASS'
        else:
            from src.autoslice.full_session_transcription import _cpa_correct_draft_cues
            corrected = _cpa_correct_draft_cues(source, danmaku_lines=[], cpa_llm_call=call)
            assert '她来了' in corrected
            assert '00:00:01,000 --> 00:00:02,000' in corrected
    assert len(attempts) == 3
    events = [json.loads(line) for line in trace.read_text().splitlines()]
    assert sum(row.get('status') == 'UNKNOWN' for row in events) == 2


def test_last_unknown_is_retained_when_ten_attempts_exhaust(tmp_path, monkeypatch):
    call, attempts, sleeps = make_bridge(monkeypatch, failures=11, runtime_root=tmp_path)
    monkeypatch.delenv('AUTOSLICE_CPA_USAGE_FILE', raising=False)
    with pytest.raises(llm.LlmCallError) as caught:
        call('one logical call')
    assert caught.value.dispatch_outcome == 'UNKNOWN'
    assert caught.value.cpa_text_retry_reason == retry.CPA_TEXT_RETRY_EXHAUSTED
    assert caught.value.cpa_text_retry['retryable'] is True
    assert retry.CPA_TEXT_RETRY_EXHAUSTED in str(caught.value)
    assert caught.value.safe_reason == 'LLM_COMMAND_FAILED'
    assert retry.CPA_TEXT_MAX_ATTEMPTS == 10
    assert len(attempts) == retry.CPA_TEXT_MAX_ATTEMPTS
    rows = [json.loads(line) for line in call.cpa_text_retry_ledger.read_text().splitlines()]
    assert [row['status'] for row in rows] == ['UNKNOWN'] * 10
    assert [row['attempt'] for row in rows] == list(range(1, 11))
    assert rows[-1]['retry_scheduled'] is False
    assert rows[-1]['delay_seconds'] is None
    assert rows[-1]['reason_code'] == retry.CPA_TEXT_RETRY_EXHAUSTED
    assert sleeps == [0, 2, 4, 8, 16, 32, 60, 60, 60]


@pytest.mark.parametrize('code', ['CPA_WRAPPER_FAILED', 'SSH_RUNTIME_RESPONSE_INVALID',
                                  'LLM_COMMAND_FAILED', 'CURL_EXIT_28'])
def test_existing_response_less_bridge_shapes_use_the_same_entry(tmp_path, monkeypatch, code):
    marker = llm.PROVIDER_DIAGNOSTIC_MARKER + json.dumps({'provider_error_code': code})
    call, attempts, _ = make_bridge(monkeypatch, failure_stderr=marker)
    with bind_observation(tmp_path / 'usage.jsonl', 'bridge-shape'):
        assert call('same prompt') == 'the successful response'
    assert len(attempts) == 3


@pytest.mark.parametrize('status', [400, 401, 403, 408, 422])
def test_received_4xx_other_than_429_is_not_retried_by_one_shot_policy(status):
    attempts, rows = [], []
    error = llm.LlmCallError('response received', safe_reason='LLM_COMMAND_FAILED',
        provider_diagnostics={'provider_http_status': status})

    def call():
        attempts.append(1)
        raise error

    with pytest.raises(llm.LlmCallError):
        retry.cpa_text_retry_call(call, record=rows.append, sleeper=lambda _: pytest.fail('sleep'))
    assert attempts == [1]
    assert rows == []


@pytest.mark.parametrize('case', ['response', 'refusal_code', 'json', 'code'])
def test_refusals_and_contract_or_code_errors_never_retry(case):
    if case == 'response':
        error = llm.LlmCallError('refused', safe_reason='LLM_COMMAND_FAILED')
        error.response_received = True
    elif case == 'refusal_code':
        error = llm.LlmCallError('refused', safe_reason='LLM_COMMAND_FAILED',
            provider_diagnostics={'provider_error_code': 'content_filter'})
    elif case == 'json':
        error = llm.LlmJsonParseError('LLM_JSON_UNPARSEABLE_OBJECT')
    else:
        error = ValueError('saved material contains timeout UNKNOWN')
    attempts = []

    def call():
        attempts.append(1)
        raise error

    with pytest.raises(type(error)):
        retry.cpa_text_retry_call(call, record=lambda _: pytest.fail('UNKNOWN'),
            sleeper=lambda _: pytest.fail('sleep'))
    assert len(attempts) == 1


def test_public_timeout_without_response_retries(tmp_path, monkeypatch):
    error = subprocess.TimeoutExpired(['offline'], 1)
    call, attempts, _ = make_bridge(monkeypatch, failures=1, error=error)
    with bind_observation(tmp_path / 'usage.jsonl', 'timeout-control'):
        assert call('prompt') == 'the successful response'
    assert len(attempts) == 2


@pytest.mark.parametrize('model', [llm.TOP_CPA_MODEL, 'offline-other-cpa-model'])
def test_public_direct_cpa_uses_the_same_bounded_entry(tmp_path, monkeypatch, model):
    attempts = []
    monkeypatch.setattr(retry.time, 'sleep', lambda _: None)
    monkeypatch.setattr(llm, 'runtime_provider_slot', lambda **kwargs: nullcontext())

    def transport(prompt, config):
        attempts.append(prompt)
        if len(attempts) < 3:
            raise llm.LlmCallError('offline connection loss', response_received=False)
        return llm.CpaCompletion('third direct response', 'offline-model')

    monkeypatch.setattr(llm, '_call_direct', transport)
    call = llm.build_llm_call(llm.LlmConfig(transport='direct', model=model, api_key_env='CPA_API_KEY'))
    trace = tmp_path / 'usage.jsonl'
    with bind_observation(trace, 'direct'):
        answer = call('same direct prompt')
    assert answer == 'third direct response'
    assert answer.answering_model == 'offline-model'
    assert attempts == ['same direct prompt'] * 3
    rows = [json.loads(line) for line in trace.read_text().splitlines()]
    assert sum(row.get('status') == 'UNKNOWN' for row in rows) == 2


def test_public_command_with_received_4xx_never_retries(tmp_path, monkeypatch):
    call, attempts, sleeps = make_bridge(monkeypatch, failures=10)
    marker = llm.PROVIDER_DIAGNOSTIC_MARKER + json.dumps({'provider_http_status': 400})

    def run(command, **kwargs):
        attempts.append(command)
        return subprocess.CompletedProcess(command, 1, '', marker)

    monkeypatch.setattr(llm.subprocess, 'run', run)
    with bind_observation(tmp_path / 'usage.jsonl', 'public-4xx'):
        with pytest.raises(llm.LlmCallError):
            call('prompt')
    assert len(attempts) == 1
    assert sleeps == []


def test_timeout_after_received_http_reply_never_retries(tmp_path, monkeypatch):
    marker = llm.PROVIDER_DIAGNOSTIC_MARKER + json.dumps({'provider_http_status': 400})
    error = subprocess.TimeoutExpired(['offline'], 1, stderr=marker.encode())
    call, attempts, _ = make_bridge(monkeypatch, failures=10, error=error)
    with bind_observation(tmp_path / 'usage.jsonl', 'received-timeout'):
        with pytest.raises(llm.LlmCallError):
            call('prompt')
    assert len(attempts) == 1


def test_bridge_cannot_erase_earlier_response_with_last_http_zero(tmp_path):
    stderr = ('[cpa] model=offline attempt=1 http=400 curl_exit=22 body_bytes=10 result=failed\n'
              '[cpa] model=offline attempt=2 http=000 curl_exit=28 body_bytes=0 result=failed')
    diagnostics = llm._command_provider_diagnostics(stderr, None)
    assert 'provider_http_status' not in diagnostics
    assert llm._command_received_response(tmp_path / 'missing', stderr, diagnostics)


def test_existing_terminal_scope_does_not_multiply_the_new_attempt_budget():
    attempts, rows = [], []

    def call():
        attempts.append(1)
        raise no_response()

    with retry.terminal_retry_scope():
        with pytest.raises(llm.LlmCallError):
            retry.cpa_text_retry_call(call, record=rows.append)
    assert len(attempts) == 1
    assert rows == []


def test_unrelated_command_retains_its_existing_one_shot_contract(tmp_path, monkeypatch):
    call, attempts, _ = make_bridge(monkeypatch, failures=10, cpa=False)
    with bind_observation(tmp_path / 'usage.jsonl', 'non-cpa'):
        with pytest.raises(llm.LlmCallError):
            call('prompt')
    assert len(attempts) == 1


def test_ledger_failure_prevents_another_dispatch():
    attempts = []

    def call():
        attempts.append(1)
        raise no_response()

    def broken_recorder(row):
        raise OSError('ledger unavailable')

    with pytest.raises(OSError, match='ledger unavailable'):
        retry.cpa_text_retry_call(call, record=broken_recorder)
    assert len(attempts) == 1


def real_offline_bridge(tmp_path, monkeypatch, sequence, *, broken_observer=False, broken_extractor=False):
    """Exercise the public caller through the actual shell with fake HTTP only."""
    import os
    import shlex
    import sys
    from test_llm_via_cpa import _write_fake_tools, SCRIPT, SENTINEL_SECRET

    tools, _ = _write_fake_tools(tmp_path)
    if broken_observer or broken_extractor:
        wrapper = tools / 'python3'
        wrapper.write_text(f'''#!{sys.executable}
import os, sys
args = sys.argv[4:] if len(sys.argv) > 4 and sys.argv[1] == '-c' else sys.argv[1:]
if len(args) > 1 and args[0] == '-' and args[1].startswith('offline-'):
    if {broken_observer!r}: raise SystemExit(1)
if len(args) > 1 and args[0] == '-' and args[1].startswith({str(tmp_path / 'temporary-files')!r}):
    if {broken_extractor!r}: raise SystemExit(1)
os.execv({sys.executable!r}, [{sys.executable!r}, *sys.argv[1:]])
''')
        wrapper.chmod(0o755)
    child = {**os.environ, 'PATH': str(tools) + os.pathsep + os.environ['PATH'],
        'CPA_BASE_URL': 'https://offline.invalid/v1', 'CPA_API_KEY': SENTINEL_SECRET,
        'CURL_CAPTURE': str(tmp_path / 'curl.json'), 'FAKE_TMP_ROOT': str(tmp_path / 'temporary-files'),
        'SLEEP_CAPTURE': str(tmp_path / 'shell-sleeps.txt'),
        'FAKE_CURL_STATUS_SEQUENCE': ','.join(sequence), 'CPA_TRANSIENT_ATTEMPTS_PER_MODEL': '7',
        'CPA_BACKOFF_BASE_SECONDS': '0'}
    child.pop('PYTHONPATH', None)
    from conftest import _REAL_CALL_COMMAND
    # The suite's blanket guard predates offline shell integration tests.
    # Restore only this seam after installing fake curl and an invalid URL.
    assert (tools / 'curl').is_file() and child['CPA_BASE_URL'] == 'https://offline.invalid/v1'
    monkeypatch.setattr(llm, '_call_command', _REAL_CALL_COMMAND)
    monkeypatch.chdir(tmp_path)
    clock = FakeClock()
    from types import SimpleNamespace
    # Keep subprocess.wait's real polling clock/sleep intact.
    monkeypatch.setattr(retry, 'time', SimpleNamespace(
        sleep=clock.sleep, monotonic=clock, time=__import__('time').time))
    monkeypatch.setattr(retry.random, 'uniform', lambda *_: 0)
    monkeypatch.setattr(llm, 'runtime_provider_slot', lambda **kwargs: nullcontext())
    diagnostics = []
    call = llm.build_llm_call(llm.LlmConfig(transport='command',
        command_template=f'bash {shlex.quote(str(SCRIPT))} {{prompt_file}} {{completion_file}} "offline-a offline-b" medium 7',
        command_child_env=child, command_diagnostic_sink=diagnostics.append))
    return call, clock, diagnostics


@pytest.mark.parametrize('sequence,count,delays,final_status', [
    (('429', '200'), 2, [0], 200),
    (('timeout',), 10, [0, 2, 4, 8, 16, 32, 60, 60, 60], None),
    (('503',), 10, [0, 2, 4, 8, 16, 32, 60, 60, 60], 503),
    (('403',), 1, [], 403),
])
def test_real_shell_and_public_owner_share_ten_http_attempts(tmp_path, monkeypatch, sequence, count, delays, final_status):
    call, clock, diagnostics = real_offline_bridge(tmp_path, monkeypatch, sequence)
    usage = tmp_path / 'usage.jsonl'
    with bind_observation(usage, 'real-offline-bridge'):
        if final_status == 200:
            assert call('new ordinary prompt') == 'safe completion'
        else:
            with pytest.raises(llm.LlmCallError) as caught:
                call('new ordinary prompt')
            assert caught.value.provider_diagnostics.get('provider_http_status') == final_status
            if sequence == ('timeout',):
                assert caught.value.response_received is False
    capture = json.loads((tmp_path / 'curl.json').read_text())
    assert capture['invocation_count'] == count
    assert clock.sleeps == delays
    assert not (tmp_path / 'shell-sleeps.txt').exists()
    rows = [json.loads(line) for line in usage.read_text().splitlines()]
    assert sum(row['event_type'] == 'http_attempt' for row in rows) == count
    assert diagnostics[-1].get('provider_http_status') == final_status
    if count == 10:
        assert [body['model'] for body in capture['bodies']] == ['offline-a'] * 7 + ['offline-b'] * 3


def test_real_observer_failure_consumes_saved_answer_without_rebuy(tmp_path, monkeypatch):
    call, clock, diagnostics = real_offline_bridge(tmp_path, monkeypatch, ('200',), broken_observer=True)
    monkeypatch.setenv('AUTOSLICE_CPA_ARCHIVE_TEXT_EXCHANGES', '1')
    with bind_observation(tmp_path / 'usage.jsonl', 'observer-recovery'):
        answer = call('new prompt whose response has already arrived')
    assert answer == 'safe completion'
    assert answer.answering_model == 'offline-a'
    assert answer.local_failure['provider_error_code'] == 'CPA_RESPONSE_LOCAL_FAILURE'
    assert diagnostics[-1]['provider_http_status'] == 200
    assert diagnostics[-1]['provider_error_code'] == 'CPA_RESPONSE_LOCAL_FAILURE'
    assert json.loads((tmp_path / 'curl.json').read_text())['invocation_count'] == 1
    assert clock.sleeps == []
    terminal, = (tmp_path / 'cpa-text-exchanges').glob('*/terminal.json')
    receipt = json.loads(terminal.read_text())
    assert receipt['status'] == 'COMPLETED'
    assert receipt['local_failure']['provider_error_code'] == 'CPA_RESPONSE_LOCAL_FAILURE'


def test_real_terminal_scope_and_shell_share_the_same_physical_cap(tmp_path, monkeypatch):
    call, clock, _ = real_offline_bridge(tmp_path, monkeypatch, ('503',))
    with bind_observation(tmp_path / 'usage.jsonl', 'terminal-physical-cap'):
        with retry.terminal_retry_scope(wallclock_seconds=300, clock=clock, sleeper=clock.sleep):
            with pytest.raises(llm.LlmCallError, match=retry.CPA_TEXT_RETRY_EXHAUSTED):
                call('a terminal prompt')
    assert json.loads((tmp_path / 'curl.json').read_text())['invocation_count'] == 10
    assert not (tmp_path / 'shell-sleeps.txt').exists()


def test_no_response_plus_observer_failure_remains_local_and_does_not_invent_a_response(tmp_path, monkeypatch):
    call, clock, _ = real_offline_bridge(tmp_path, monkeypatch, ('timeout',), broken_observer=True)
    with bind_observation(tmp_path / 'usage.jsonl', 'no-response-local-failure'):
        with pytest.raises(llm.LlmCallError) as caught:
            call('a request with no HTTP response')
    assert caught.value.safe_reason == 'LLM_RESPONSE_LOCAL_FAILURE'
    assert caught.value.response_received is False
    assert 'provider_http_status' not in caught.value.provider_diagnostics
    assert caught.value.command_response_body == b''
    assert json.loads((tmp_path / 'curl.json').read_text())['invocation_count'] == 1
    assert clock.sleeps == []


def test_local_extraction_failure_retains_raw_response_for_engineering_recovery(tmp_path, monkeypatch):
    call, clock, _ = real_offline_bridge(tmp_path, monkeypatch, ('200',), broken_extractor=True)
    monkeypatch.setenv('AUTOSLICE_CPA_ARCHIVE_TEXT_EXCHANGES', '1')
    with bind_observation(tmp_path / 'usage.jsonl', 'extract-failure'):
        with pytest.raises(llm.LlmCallError) as caught:
            call('new prompt')
    assert caught.value.safe_reason == 'LLM_RESPONSE_LOCAL_FAILURE'
    assert caught.value.response_received is True
    assert caught.value.provider_diagnostics['provider_http_status'] == 200
    assert json.loads(caught.value.command_response_body)['output_text'] == 'safe completion'
    assert json.loads((tmp_path / 'curl.json').read_text())['invocation_count'] == 1
    assert clock.sleeps == []
    raw, = (tmp_path / 'cpa-text-exchanges').glob('*/response.json')
    assert json.loads(raw.read_bytes())['output_text'] == 'safe completion'
    terminal = json.loads((raw.parent / 'terminal.json').read_text())
    assert terminal['status'] == 'RESPONSE_LOCAL_FAILURE'


@pytest.mark.parametrize('status', [200, 429, 503])
def test_received_local_failure_never_reenters_either_retry_owner(status):
    error = llm.LlmCallError('response received; local bookkeeping failed',
        safe_reason='LLM_RESPONSE_LOCAL_FAILURE', response_received=True,
        provider_diagnostics={'provider_http_status': status, 'provider_error_code': 'CPA_RESPONSE_LOCAL_FAILURE'})
    assert retry.cpa_text_failure_category(error) is None
    assert retry.terminal_failure_is_transient(error) is False
    with pytest.raises(llm.LlmCallError):
        retry.cpa_text_retry_call(lambda: (_ for _ in ()).throw(error),
            record=lambda _: pytest.fail('retry'), sleeper=lambda _: pytest.fail('sleep'))
