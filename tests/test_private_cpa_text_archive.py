"""Opt-in raw text receipts do not change calls, replies, permits or credentials."""
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.autoslice import llm_client
from src.autoslice.cpa_runtime import bind_observation


@pytest.mark.parametrize('transport', ['direct', 'command'])
def test_normal_llm_dispatch_archives_actual_request_and_reply_once(tmp_path, monkeypatch, transport):
    monkeypatch.setenv('AUTOSLICE_CPA_ARCHIVE_TEXT_EXCHANGES', '1')
    prompt = 'Synthetic question; never a human answer'
    completion = '{"status":"UNRESOLVED","reason":"Synthetic observation"}'
    calls = []
    config = llm_client.LlmConfig(transport=transport, model='fixture-model',
        api_key_env='UNUSED_API_KEY', api_base='https://example.invalid',
        command_template='bridge {prompt_file} {completion_file}',
        command_child_env={'CPA_API_KEY': 'secret-not-for-archive'}, runtime_root=str(tmp_path / 'runtime'))
    if transport == 'direct':
        def direct(text, _config):
            calls.append(text)
            return completion
        monkeypatch.setattr(llm_client, '_call_direct', direct)
    else:
        def command(args, **kwargs):
            calls.append(Path(args[1]).read_text())
            Path(args[2]).write_text(completion)
            return SimpleNamespace(returncode=0, stdout='', stderr='')
        monkeypatch.setattr(llm_client.subprocess, 'run', command)
    call = llm_client.build_llm_call(config)
    with bind_observation(tmp_path / 'usage.jsonl', 'synthetic-attempt'):
        assert call(prompt) == completion
    assert calls == [prompt]
    folder, = (tmp_path / 'cpa-text-exchanges').iterdir()
    request = json.loads((folder / 'request.json').read_text())
    terminal = json.loads((folder / 'terminal.json').read_text())
    assert (folder / 'prompt.txt').read_text() == prompt
    assert (folder / 'completion.txt').read_text() == completion
    assert request['status'] == 'STARTED' and terminal['status'] == 'COMPLETED'
    assert request['prompt_sha256'] == hashlib.sha256(prompt.encode()).hexdigest()
    assert terminal['completion_sha256'] == hashlib.sha256(completion.encode()).hexdigest()
    assert request['mutation_authorized'] is False and request['cost_usd'] is None
    assert request['production_attempt_id'] == 'synthetic-attempt'
    assert 'secret-not-for-archive' not in ''.join(p.read_text() for p in folder.iterdir())
    events = [json.loads(line) for line in (tmp_path / 'usage.jsonl').read_text().splitlines()]
    assert len([e for e in events if e['event_type'] == 'model_request']) == 1


def test_opt_in_failure_retains_unknown_without_retry_or_secret_message(tmp_path, monkeypatch):
    monkeypatch.setenv('AUTOSLICE_CPA_ARCHIVE_TEXT_EXCHANGES', '1')
    failure = RuntimeError('secret-not-for-archive')
    calls = []
    def direct(prompt, config):
        calls.append(prompt)
        raise failure
    monkeypatch.setattr(llm_client, '_call_direct', direct)
    call = llm_client.build_llm_call(llm_client.LlmConfig(transport='direct', model='fixture', runtime_root=str(tmp_path / 'runtime')))
    with bind_observation(tmp_path / 'usage.jsonl', 'synthetic-attempt'), pytest.raises(RuntimeError) as exc:
        call('synthetic unresolved request')
    assert exc.value is failure and len(calls) == 1
    folder, = (tmp_path / 'cpa-text-exchanges').iterdir()
    assert not (folder / 'completion.txt').exists()
    assert json.loads((folder / 'terminal.json').read_text())['status'] == 'OUTCOME_UNKNOWN'
    assert 'secret-not-for-archive' not in ''.join(p.read_text() for p in folder.iterdir())


@pytest.mark.parametrize('enabled,trace', [(False, True), (True, False)])
def test_disabled_capture_or_missing_trace_cannot_create_fake_receipts(tmp_path, monkeypatch, enabled, trace):
    monkeypatch.setenv('AUTOSLICE_CPA_ARCHIVE_TEXT_EXCHANGES', '1' if enabled else '0')
    monkeypatch.delenv('AUTOSLICE_CPA_USAGE_FILE', raising=False)
    calls = []
    monkeypatch.setattr(llm_client, '_call_direct', lambda prompt, config: calls.append(prompt) or 'reply')
    call = llm_client.build_llm_call(llm_client.LlmConfig(transport='direct', model='fixture', runtime_root=str(tmp_path / 'runtime')))
    if trace:
        with bind_observation(tmp_path / 'usage.jsonl', 'synthetic'):
            assert call('test') == 'reply'
        assert calls == ['test']
    else:
        with pytest.raises(ValueError, match='bound usage trace'):
            call('test')
        assert calls == []
    assert not (tmp_path / 'cpa-text-exchanges').exists()
