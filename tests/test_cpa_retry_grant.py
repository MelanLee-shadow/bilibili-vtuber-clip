"""Extra attempts remain scoped to one exhausted durable request."""
import hashlib
import json
import os
import subprocess
import sys

import pytest

from src.autoslice import cpa_request_lifecycle as life
from src.autoslice.cpa_retry_grant import GRANT_FILE
from src.autoslice.infra_retry_policy import CPA_TEXT_MAX_ATTEMPTS
from src.autoslice.semantic_retry_progress import RetryNeedsPlan

PROMPT = 'JSON exact prompt'
MODEL = {'transport': 'recorded', 'models': ['fixture-model'], 'effort': 'medium'}
TEXT = 'I authorize two additional attempts for this request.'
AUTHORITY = {'source': 'human', 'thread_id': 'fixture', 'message_id': 'fixture',
             'recorded_at': 'fixture', 'message_text': TEXT,
             'message_sha256': hashlib.sha256(TEXT.encode()).hexdigest()}


def owner(root, prompt=PROMPT, model=MODEL):
    return life.TextRequest(root, life.binding_for(prompt, model, None, {}, None))


def exhaust(root):
    request = owner(root)
    def unknown(_):
        raise SystemExit('unknown')
    for _ in range(CPA_TEXT_MAX_ATTEMPTS):
        with request.claim(), pytest.raises(SystemExit):
            request.physical(PROMPT, MODEL, unknown)
    return request


@pytest.fixture(autouse=True)
def fast(monkeypatch, tmp_path):
    monkeypatch.setenv('AUTOSLICE_BASE', str(tmp_path))
    monkeypatch.setattr(life, 'cpa_text_retry_delay_seconds', lambda *args: 0)


def authorize(request):
    with request.claim():
        request.authorize_additional_attempts(PROMPT, MODEL, additional_attempts=2, authority=AUTHORITY)


def test_cold_restart_exhaustion_and_original_history(tmp_path):
    request = exhaust(tmp_path)
    before = json.loads(request.path.read_text())
    authorize(request)
    anchored = json.loads(request.path.read_text())
    assert anchored['events'] == before['events']
    assert anchored['legacy_unknown'] == before['legacy_unknown']
    code = '''
from pathlib import Path
import sys
from src.autoslice import cpa_request_lifecycle as life
from tests.test_cpa_retry_grant import owner, PROMPT, MODEL
life.cpa_text_retry_delay_seconds = lambda *args: 0
r = owner(Path(sys.argv[1]))
def fail(_): raise SystemExit('unknown')
for _ in range(2):
    try:
        with r.claim(): r.physical(PROMPT, MODEL, fail)
    except SystemExit: pass
try:
    with r.claim(): r.physical(PROMPT, MODEL, fail)
except Exception as e:
    assert 'CPA_TEXT_RETRY_EXHAUSTED' in str(e)
else: raise AssertionError('budget was reset')
'''
    run = subprocess.run([sys.executable, '-c', code, str(tmp_path)], env=os.environ.copy(),
                         capture_output=True, text=True, timeout=30)
    assert run.returncode == 0, run.stderr
    final = json.loads(request.path.read_text())
    assert final['reserved_attempts'] == CPA_TEXT_MAX_ATTEMPTS + 2
    assert final['events'][:len(before['events'])] == before['events']
    assert final['max_attempts'] == CPA_TEXT_MAX_ATTEMPTS
    assert all(e['possible_duplicate_billing'] for e in final['events'] if e['status'] == 'STARTED' and e['attempt'] > 1)


def test_mismatch_concurrency_and_other_request(tmp_path):
    request = exhaust(tmp_path)
    authorize(request)
    def forbidden(_):
        raise AssertionError('unexpected dispatch')
    with request.claim():
        with pytest.raises(RetryNeedsPlan, match='INPUT_MISMATCH'):
            request.physical(PROMPT + ' changed', MODEL, forbidden)
        with pytest.raises(RetryNeedsPlan, match='INPUT_MISMATCH'):
            request.physical(PROMPT, {**MODEL, 'effort': 'high'}, forbidden)
        with pytest.raises(RetryNeedsPlan, match='INPUT_MISMATCH'):
            request.physical(PROMPT, MODEL, forbidden, contract={'changed': True})
        with pytest.raises(RetryNeedsPlan, match='IN_FLIGHT'):
            with owner(tmp_path).claim(): pass
    other = owner(tmp_path, 'JSON unrelated')
    with other.claim():
        assert other.limit == CPA_TEXT_MAX_ATTEMPTS and other.grant is None
    assert other.folder != request.folder


def test_completed_response_is_not_sent_again(tmp_path):
    request = exhaust(tmp_path)
    authorize(request)
    calls = []
    with request.claim():
        assert request.physical(PROMPT, MODEL, lambda p: calls.append(p) or '{"ok":true}') == '{"ok":true}'
    second = owner(tmp_path)
    with second.claim():
        assert second.physical(PROMPT, MODEL, lambda p: pytest.fail('replayed')) == '{"ok":true}'
    assert calls == [PROMPT]


@pytest.mark.parametrize('tamper', ['grant', 'prefix', 'missing', 'duplicate'])
def test_tamper_and_second_grant_refused(tmp_path, tamper):
    request = exhaust(tmp_path)
    authorize(request)
    grant = request.folder / GRANT_FILE
    if tamper == 'grant':
        data = json.loads(grant.read_text()); data['additional_attempts'] = 3
        grant.write_text(json.dumps(data))
    elif tamper == 'prefix':
        data = json.loads(request.path.read_text()); data['events'][0]['pid'] += 1
        request.path.write_text(json.dumps(data))
    elif tamper == 'missing':
        grant.unlink()
    else:
        with request.claim(), pytest.raises(RetryNeedsPlan, match='GRANT_INVALID'):
            request.authorize_additional_attempts(PROMPT, MODEL, additional_attempts=2, authority=AUTHORITY)
        return
    with pytest.raises(RetryNeedsPlan, match='GRANT_INVALID'):
        with owner(tmp_path).claim(): pass


def test_grant_requires_claim_and_matching_original_request(tmp_path):
    request = exhaust(tmp_path)
    with pytest.raises(RetryNeedsPlan, match='REQUIRES_CLAIM'):
        request.authorize_additional_attempts(PROMPT, MODEL, additional_attempts=2, authority=AUTHORITY)
    with request.claim(), pytest.raises(RetryNeedsPlan, match='GRANT_INVALID'):
        request.authorize_additional_attempts(PROMPT + ' changed', MODEL, additional_attempts=2, authority=AUTHORITY)
    assert not (request.folder / GRANT_FILE).exists()


def test_grant_refused_before_exhaustion_or_after_permanent_failure(tmp_path):
    request = owner(tmp_path)
    with request.claim(), pytest.raises(RetryNeedsPlan, match='GRANT_INVALID'):
        request.authorize_additional_attempts(PROMPT, MODEL, additional_attempts=2, authority=AUTHORITY)
    request = exhaust(tmp_path)
    state = json.loads(request.path.read_text())
    state['events'][-1]['status'] = 'PERMANENT_FAILURE'
    request.path.write_text(json.dumps(state))
    with request.claim(), pytest.raises(RetryNeedsPlan, match='GRANT_INVALID'):
        request.authorize_additional_attempts(PROMPT, MODEL, additional_attempts=2, authority=AUTHORITY)
