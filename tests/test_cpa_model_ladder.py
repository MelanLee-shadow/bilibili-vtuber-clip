import ast
import copy
from contextlib import contextmanager
import io
import json
import pickle
from pathlib import Path
import urllib.error
import threading
from types import SimpleNamespace

import pytest

from src.autoslice import cpa_model_ladder as policy, llm_client
from src.autoslice.cpa_runtime import bind_observation
from src.autoslice import cpa_runtime
from src.autoslice.normal_text_generation_checkpoint import completed_generation
from src.autoslice.transcription_stage_cache import TranscriptionStageCache
from src.autoslice.completed_review_checkpoint import CompletedReviewCheckpoint


ROOT = Path(__file__).resolve().parents[1]


def test_only_ladder_defines_production_model_names():
    offenders = []
    for base in ('src/autoslice', 'scripts'):
        for path in (ROOT / base).rglob('*.py'):
            if path.name == 'cpa_model_ladder.py':
                continue
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                if isinstance(node, ast.Constant) and isinstance(node.value, str):
                    if any(name in node.value for name in policy.CPA_MODEL_LADDER):
                        offenders.append(f'{path.relative_to(ROOT)}:{node.lineno}')
    assert not offenders
    script = (ROOT / 'scripts/llm_via_cpa.sh').read_text()
    assert not any(name in script for name in policy.CPA_MODEL_LADDER)


def config():
    return llm_client.LlmConfig(transport='direct', model=policy.TOP_CPA_MODEL,
        api_base='https://cpa.invalid/v1', api_key_env='TEST_CPA_KEY',
        api_mode='responses', reasoning_effort='medium', timeout_seconds=policy.cpa_call_timeout_seconds())


def fake_provider(monkeypatch, outcomes):
    calls = []
    monkeypatch.setenv('TEST_CPA_KEY', 'offline-test-key')
    monkeypatch.setenv('CPA_BACKOFF_BASE_SECONDS', '0')
    monkeypatch.setenv('CPA_TRANSIENT_ATTEMPTS_PER_MODEL', '3')
    @contextmanager
    def request(req, *, timeout, model, kind):
        calls.append((json.loads(req.data), timeout))
        item = outcomes[len(calls) - 1]
        if isinstance(item, Exception):
            raise item
        yield io.BytesIO(json.dumps(item).encode())
    monkeypatch.setattr(llm_client, 'observed_cpa_request', request)
    return calls


@pytest.mark.parametrize('failure', [503, 429, 'timeout', 'empty'])
def test_direct_exhausts_transient_retries_before_fallback(monkeypatch, failure):
    item = {'status': 'completed', 'output_text': ''}
    if isinstance(failure, int):
        item = urllib.error.HTTPError('https://cpa.invalid', failure, 'offline', None, None)
    elif failure == 'timeout':
        item = TimeoutError('offline')
    calls = fake_provider(monkeypatch, [item] * 3 + [{
        'status': 'completed', 'output_text': '甲\n \t乙\n', 'model': policy.CPA_MODEL_LADDER[1]}])
    result = llm_client._call_direct('prompt', config())
    assert result == '甲\n \t乙\n'
    assert result.answering_model == policy.CPA_MODEL_LADDER[1]
    assert [c[0]['model'] for c in calls] == [policy.TOP_CPA_MODEL] * 3 + [policy.CPA_MODEL_LADDER[1]]
    assert all(c[0]['reasoning']['effort'] == 'medium' and c[1] == 150 for c in calls)


@pytest.mark.parametrize('code', [400, 401, 403, 404, 422])
def test_direct_request_error_never_falls_back(monkeypatch, code):
    calls = fake_provider(monkeypatch, [urllib.error.HTTPError('https://cpa.invalid', code, 'offline', None, None)])
    with pytest.raises(llm_client.LlmCallError):
        llm_client._call_direct('prompt', config())
    assert len(calls) == 1


def test_direct_group_routing_400_keeps_existing_transient_exception(monkeypatch):
    failures = [urllib.error.HTTPError('https://cpa.invalid', 400, 'offline', None,
        io.BytesIO(b'{"code":"group_capability_unavailable"}')) for _ in range(3)]
    calls = fake_provider(monkeypatch, [*failures, {'output_text': 'complete'}])
    answer = llm_client._call_direct('prompt', config())
    assert answer == 'complete' and answer.answering_model == policy.CPA_MODEL_LADDER[1]
    assert len(calls) == 4


def test_direct_incomplete_body_is_never_success(monkeypatch):
    calls = fake_provider(monkeypatch, [{'status': 'incomplete', 'output_text': 'half'}] * 6)
    with pytest.raises(llm_client.LlmCallError):
        llm_client._call_direct('prompt', config())
    assert len(calls) == 6


def test_answering_model_is_in_usage_and_exchange(monkeypatch, tmp_path):
    fallback = policy.CPA_MODEL_LADDER[1]
    fake_provider(monkeypatch, [{'output_text': '', 'status': 'completed'}] * 3 + [
        {'output_text': 'answer', 'status': 'completed', 'model': fallback}])
    monkeypatch.setenv('AUTOSLICE_CPA_ARCHIVE_TEXT_EXCHANGES', '1')
    usage = tmp_path / 'cpa-usage.jsonl'
    with bind_observation(usage, 'offline'):
        result = llm_client.build_llm_call(config())('prompt')
    assert result.answering_model == fallback
    assert any(row.get('answering_model') == fallback for row in map(json.loads, usage.read_text().splitlines()))
    terminal = json.loads(next(tmp_path.glob('cpa-text-exchanges/*/terminal.json')).read_text())
    assert terminal['answering_model'] == fallback


def test_ladder_changes_do_not_change_top_cache_identity(monkeypatch):
    cfg = llm_client.LlmConfig(transport='command',
        command_template='bash scripts/llm_via_cpa.sh {prompt_file} {completion_file}', command_child_env={})
    before = llm_client._cpa_cache_identity(cfg)
    monkeypatch.setattr(policy, 'CPA_MODEL_LADDER', (*policy.CPA_MODEL_LADDER, 'future-offline-model'))
    assert llm_client._cpa_cache_identity(cfg) == before == {
        'transport': 'cpa_command', 'models': [policy.TOP_CPA_MODEL], 'effort': 'medium'}


@pytest.mark.parametrize('clone', [copy.copy, copy.deepcopy, lambda value: pickle.loads(pickle.dumps(value))])
def test_answer_text_and_model_survive_evidence_copies(clone):
    original = llm_client.CpaCompletion('甲\r\n \t乙\n', policy.CPA_MODEL_LADDER[1])
    restored = clone(original)
    assert restored == original
    assert restored.answering_model == original.answering_model


def test_cache_identity_normalization_preserves_explicit_route_format_guard(monkeypatch):
    from src.autoslice.structured_output import response_contract
    calls = []
    monkeypatch.setattr(llm_client, '_call_command', lambda prompt, config:
        calls.append(prompt) or 'complete but invalid JSON')
    cfg = llm_client.LlmConfig(transport='command', command_child_env={},
        command_template="bash scripts/llm_via_cpa.sh {prompt_file} {completion_file} 'offline-a offline-b' medium")
    call = llm_client.build_llm_call(cfg)
    assert call.cpa_cache_identity['models'] == ['offline-a']
    with response_contract(validate=llm_client.extract_json_object):
        assert call('same prompt JSON') == 'complete but invalid JSON'
    assert calls == ['same prompt JSON']


def test_fallback_checkpoint_and_transcription_cache_use_answering_model(tmp_path):
    top, fallback = policy.CPA_MODEL_LADDER
    def answer(_):
        return llm_client.CpaCompletion('unchanged bytes\n', fallback)
    answer.cpa_cache_identity = {'transport': 'offline', 'models': [top], 'effort': 'medium'}
    kwargs = dict(artifact_root=tmp_path, candidate_id='offline', lane='title', attempt=1,
        prompt='prompt', call=answer, dependencies=lambda: {})
    assert completed_generation(**kwargs) == 'unchanged bytes\n'
    saved = [json.loads(p.read_text()) for p in tmp_path.glob('.normal-text-generations/*/*/*/result.json')]
    reusable = [s for s in saved if s['status'] == 'COMPLETED']
    assert len(reusable) == 1
    assert reusable[0]['identity']['provider']['models'] == [fallback]
    answer.cpa_cache_identity['models'] = [fallback]
    assert completed_generation(**{**kwargs, 'allow_dispatch': False}) == 'unchanged bytes\n'
    cache = TranscriptionStageCache(tmp_path / 'media.mp4', b'audio')
    identity = {'model_identity': {'models': [top], 'effort': 'medium'}}
    assert cache._run('cpa', identity, lambda: answer('prompt'), lambda x: x) == 'unchanged bytes\n'
    entry = json.loads(next(cache.root.glob('cpa/*.json')).read_text())
    assert entry['identity']['model_identity']['models'] == [fallback]


def test_attempt_timeout_and_outer_budget_follow_environment(monkeypatch):
    assert policy.attempt_timeout_seconds({}) == 150
    assert policy.chain_deadline_seconds({}) == 930
    assert policy.cpa_call_timeout_seconds({}) == 1140
    assert policy.attempt_timeout_seconds({'CPA_ATTEMPT_TIMEOUT_SECONDS': '175'}) == 175
    assert policy.cpa_call_timeout_seconds({'CPA_ATTEMPT_TIMEOUT_SECONDS': '175'}) == 1315
    with pytest.raises(ValueError):
        policy.attempt_timeout_seconds({'CPA_ATTEMPT_TIMEOUT_SECONDS': '0'})
    monkeypatch.setenv('CPA_ATTEMPT_TIMEOUT_SECONDS', '17')
    calls = fake_provider(monkeypatch, [{'output_text': 'ok', 'status': 'completed'}])
    llm_client._call_direct('prompt', config())
    assert calls[0][1] == 17


def test_command_outer_budget_uses_captured_environment_and_attempts(monkeypatch):
    monkeypatch.setenv('CPA_ATTEMPT_TIMEOUT_SECONDS', '999')
    env = {'CPA_ATTEMPT_TIMEOUT_SECONDS': '175', 'CPA_TRANSIENT_ATTEMPTS_PER_MODEL': '4'}
    cfg = llm_client.LlmConfig(transport='command', command_child_env=env,
        command_template=f"bash scripts/llm_via_cpa.sh {{prompt_file}} {{completion_file}} '{policy.TOP_CPA_MODEL}' high 5")
    call = llm_client.build_llm_call(cfg)
    assert call.timeout_seconds == policy.cpa_call_timeout_seconds(env, models=2, attempts=5)
    assert call.cpa_cache_identity['effort'] == 'high'
    direct = llm_client.build_llm_call(llm_client.replace(config(), timeout_seconds=90,
        command_child_env=env))
    assert direct.timeout_seconds == policy.cpa_call_timeout_seconds()


def test_direct_attempt_deadline_interrupts_a_trickling_body(monkeypatch):
    interrupted = threading.Event()
    class Response:
        status = 200
        fp = SimpleNamespace(raw=SimpleNamespace(_sock=SimpleNamespace(
            shutdown=lambda _: interrupted.set())))
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return False
        def read(self):
            assert interrupted.wait(5), 'body was not interrupted at the total attempt deadline'
            return b'{}'
    monkeypatch.setattr(cpa_runtime.urllib.request, 'urlopen', lambda *args, **kwargs: Response())
    with pytest.raises(TimeoutError, match='attempt deadline exceeded'):
        with cpa_runtime.observed_cpa_request(object(), timeout=0.01,
                model=policy.TOP_CPA_MODEL, kind='text') as response:
            response.read()
    assert interrupted.is_set()


def test_completed_review_cache_isolated_by_answering_model(tmp_path):
    top, fallback = policy.CPA_MODEL_LADDER
    def checkpoint(model):
        return CompletedReviewCheckpoint(tmp_path, {'provider': {'routing': {
            'transport': 'offline', 'models': [model], 'effort': 'medium'},
            'runtime': {'provider_model': [model], 'provider_effort': 'medium'}}}, 'same prompt')
    original = checkpoint(top)
    original.response(lambda _: llm_client.CpaCompletion('answer bytes\n', fallback))
    original.save()
    assert checkpoint(top).load() is None
    cached = checkpoint(fallback).load()
    assert cached == 'answer bytes\n' and cached.answering_model == fallback


def test_existing_top_reply_survives_ladder_change_without_dispatch(tmp_path, monkeypatch):
    calls = []
    def answer(prompt):
        calls.append(prompt)
        return llm_client.CpaCompletion('original reply\n', policy.TOP_CPA_MODEL)
    answer.cpa_cache_identity = {'transport': 'offline', 'models': [policy.TOP_CPA_MODEL], 'effort': 'medium'}
    kwargs = dict(artifact_root=tmp_path, candidate_id='old-top', lane='title', attempt=1,
        prompt='same prompt', call=answer, dependencies=lambda: {})
    assert completed_generation(**kwargs) == 'original reply\n'
    monkeypatch.setattr(policy, 'CPA_MODEL_LADDER', (*policy.CPA_MODEL_LADDER, 'future-offline-model'))
    cached = completed_generation(**kwargs, allow_dispatch=False)
    assert cached == 'original reply\n' and cached.answering_model == policy.TOP_CPA_MODEL
    assert len(calls) == 1
