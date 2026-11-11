"""A returned model redirect keeps its exact request owner across cold replay."""

import hashlib
import json
from pathlib import Path

import pytest

from src.autoslice import completed_review_checkpoint as saved
from src.autoslice import cpa_request_lifecycle as life
from src.autoslice.llm_client import CpaCompletion, completion_identity


PROMPT = 'Review the original evidence. Return JSON.'
OLDER = '{"findings":[{"cue":1}]}'
CURRENT = '{"findings":[{"cue":2}]}'
REQUESTED = {'transport': 'recorded', 'models': ['requested'], 'effort': 'medium'}
ANSWERED = {**REQUESTED, 'models': ['answering']}


def provider(root, identity, answer, calls):
    def send(prompt):
        calls.append(prompt)
        return answer
    send.cpa_cache_identity = identity
    send.completed_review_root = root
    return send


def invoke(call):
    return saved.ruling_response(stage='final-review-findings', prompt=PROMPT,
        llm_call=call, inputs={}, parser_path=Path(__file__))


def durable(root, identity, answer, calls):
    def transport(prompt):
        return life.physical_text_call(provider(root, identity, answer, calls),
                                       prompt, model=identity, root=root)
    transport.cpa_cache_identity = identity
    transport.completed_review_root = root
    return transport


def tree(root):
    return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob('*') if p.is_file()}


def test_live_redirect_and_cold_replay_keep_request_owned_answer(tmp_path, monkeypatch):
    monkeypatch.setenv('AUTOSLICE_BASE', str(tmp_path))
    root = tmp_path / 'cache/completed-reviews'
    calls = []
    invoke(provider(root, ANSWERED, CpaCompletion(OLDER, 'answering'), calls))
    old = tree(root)
    call = durable(root, REQUESTED, CpaCompletion(CURRENT, 'answering'), calls)
    raw, hit = invoke(call)
    assert str(raw) == CURRENT and raw.answering_model == 'answering' and not hit
    call = durable(root, REQUESTED, 'must not dispatch', calls)
    raw, hit = invoke(call)
    assert str(raw) == CURRENT and raw.answering_model == 'answering'
    assert calls == [PROMPT, PROMPT]
    assert all((root / p).read_bytes() == b for p, b in old.items())
    assert invoke(provider(root, ANSWERED, 'must not dispatch', calls))[0] == OLDER
    top = saved.CompletedReviewCheckpoint.for_ruling(stage='final-review-findings',
        prompt=PROMPT, llm_call=call, inputs={}, parser_path=Path(__file__))
    assert top.load() is None  # The physical return did not become a top-model observation.


def legacy_save(method):
    """Restore the responsible old writer solely to produce retained evidence."""
    def old(self):
        routing = self.binding['provider']['routing']
        answered = completion_identity(routing, self.raw)
        if answered != routing:
            binding = {**self.binding, 'provider': {**self.binding['provider'], 'routing': answered}}
            actual = type(self)(self.root, binding, self.prompt)
            actual.raw = self.raw
            actual.save()
            return
        method(self)
    return old


def test_original_dispatch_completion_recovers_old_redirect_without_resending(tmp_path, monkeypatch):
    monkeypatch.setenv('AUTOSLICE_BASE', str(tmp_path))
    cache = tmp_path / 'cache/completed-reviews'
    calls = []
    invoke(provider(cache, ANSWERED, CpaCompletion(OLDER, 'answering'), calls))

    def transport(prompt):
        return life.physical_text_call(
            provider(cache, REQUESTED, CpaCompletion(CURRENT, 'answering'), calls),
            prompt, model=REQUESTED, root=cache)
    transport.cpa_cache_identity = REQUESTED
    transport.completed_review_root = cache
    with monkeypatch.context() as old:
        old.setattr(saved.CompletedReviewCheckpoint, 'save', legacy_save(saved.CompletedReviewCheckpoint.save))
        with pytest.raises(saved.CompletedReviewCheckpointError, match='AMBIGUOUS'):
            invoke(transport)
    original = tree(cache)
    state_file = next((cache / '.dispatch-claims').glob('*/state'))
    state = state_file.read_bytes()
    assert json.loads(state)['events'][-1]['status'] == 'COMPLETED'
    raw, _ = invoke(transport)
    assert str(raw) == CURRENT and raw.answering_model == 'answering'
    assert invoke(transport)[0] == CURRENT and calls == [PROMPT, PROMPT]
    assert state_file.read_bytes() == state
    assert all((cache / p).read_bytes() == b for p, b in original.items())
    with pytest.raises(saved.CompletedReviewCheckpointError, match='AMBIGUOUS'):
        invoke(provider(cache, ANSWERED, 'must not dispatch', calls))


def test_wrong_completed_digest_cannot_select_another_response_or_resend(tmp_path, monkeypatch):
    monkeypatch.setenv('AUTOSLICE_BASE', str(tmp_path))
    cache = tmp_path / 'cache/completed-reviews'
    calls = []
    call = provider(cache, REQUESTED, CpaCompletion(CURRENT, 'answering'), calls)
    with monkeypatch.context() as old:
        old.setattr(saved.CompletedReviewCheckpoint, 'save', legacy_save(saved.CompletedReviewCheckpoint.save))
        assert life.physical_text_call(call, PROMPT, model=REQUESTED, root=cache) == CURRENT
    state_file = next((cache / '.dispatch-claims').glob('*/state'))
    state = json.loads(state_file.read_text())
    state['events'][-1]['completion_sha256'] = hashlib.sha256(OLDER.encode()).hexdigest()
    state_file.write_text(json.dumps(state))
    from src.autoslice.semantic_retry_progress import RetryNeedsPlan
    with pytest.raises(RetryNeedsPlan, match='NOT_REUSABLE'):
        life.physical_text_call(call, PROMPT, model=REQUESTED, root=cache)
    assert calls == [PROMPT]


def test_redirect_receipt_drift_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv('AUTOSLICE_BASE', str(tmp_path))
    root = tmp_path / 'cache/completed-reviews'
    calls = []
    call = durable(root, REQUESTED, CpaCompletion(CURRENT, 'answering'), calls)
    invoke(call)
    receipt = next((root / 'requested-model-responses').glob('*/response.json'))
    value = json.loads(receipt.read_text())
    value['answering_identity']['effort'] = 'different'
    receipt.write_text(json.dumps(value))
    with pytest.raises(saved.CompletedReviewCheckpointError, match='REQUEST_RESPONSE_DRIFT'):
        invoke(call)
    assert calls == [PROMPT]


def test_redirect_retention_rejection_stays_volatile(tmp_path, monkeypatch):
    monkeypatch.setenv('AUTOSLICE_BASE', str(tmp_path))
    root = tmp_path / 'cache/completed-reviews'
    calls = []
    with saved.ruling_material_scope({}, retain=lambda raw: False):
        call = durable(root, REQUESTED, CpaCompletion(CURRENT, 'answering'), calls)
        assert invoke(call)[0] == CURRENT
        assert invoke(call)[0] == CURRENT
    assert not list(tmp_path.rglob('*.json')) and calls == [PROMPT]


@pytest.mark.parametrize('legacy', [False, True])
def test_typed_consumer_cold_revalidates_request_owned_observation(tmp_path, monkeypatch, legacy):
    from src.autoslice import finding_closure_shell as shell
    from tests.test_finding_closure_wiring import check, prepared, proposal_inputs

    monkeypatch.setenv('AUTOSLICE_BASE', str(tmp_path))
    root = tmp_path / 'cache/completed-reviews'
    calls, records = [], []
    inputs = proposal_inputs(prepared(check()))

    def ask(call, sink=None):
        return saved.ruling_response(stage='proposal-rebuild', prompt=PROMPT,
            llm_call=call, inputs=inputs, parser_path=Path(__file__),
            consumed_ruling_sink=sink)

    ask(provider(root, ANSWERED, CpaCompletion(OLDER, 'answering'), calls))
    transport = durable(root, REQUESTED, CpaCompletion(CURRENT, 'answering'), calls)
    if legacy:
        with monkeypatch.context() as old:
            old.setattr(saved.CompletedReviewCheckpoint, 'save', legacy_save(saved.CompletedReviewCheckpoint.save))
            with pytest.raises(saved.CompletedReviewCheckpointError, match='AMBIGUOUS'):
                ask(transport)
    original = tree(root)
    assert ask(transport, records.append)[0] == CURRENT
    record, = records
    assert record.raw_completion == CURRENT
    assert record.model_identity.value()['models'] == ['answering']
    assert '/dispatch-observations/' in record.checkpoint.value()['root']
    assert shell.consumed_ruling_from_document(record.document()) == record

    records.clear()
    assert ask(durable(root, REQUESTED, 'must not dispatch', calls), records.append)[0] == CURRENT
    assert records == [record] and calls == [PROMPT, PROMPT]
    assert all((root / p).read_bytes() == b for p, b in original.items())
    stored = next(Path(record.checkpoint.value()['root']).glob('*/*.json'))
    value = json.loads(stored.read_text())
    value['response'] = OLDER
    stored.write_text(json.dumps(value))
    with pytest.raises(ValueError):
        shell.consumed_ruling_from_document(record.document())


@pytest.mark.parametrize('drift', ['response', 'prompt', 'physical'])
def test_completion_owner_proof_cannot_cross_request_or_answer(tmp_path, monkeypatch, drift):
    from src.autoslice.semantic_retry_progress import RetryNeedsPlan

    monkeypatch.setenv('AUTOSLICE_BASE', str(tmp_path))
    root = tmp_path / 'cache/completed-reviews'
    calls = []
    raw, _ = invoke(durable(root, REQUESTED, CpaCompletion(CURRENT, 'answering'), calls))
    assert life.completed_response_scope(raw, PROMPT)
    prompt = PROMPT
    if drift == 'response':
        other = CpaCompletion(OLDER, 'answering')
        other.completed_text_request = raw.completed_text_request
        raw = other
    elif drift == 'prompt':
        prompt += ' changed'
    else:
        raw.completed_text_request['physical']['model'] = ANSWERED
    with pytest.raises(RetryNeedsPlan, match='CPA_TEXT_COMPLETION_'):
        life.completed_response_scope(raw, prompt)
    assert calls == [PROMPT]


@pytest.mark.parametrize('missing', ['state', 'response'])
def test_semantic_request_replay_cannot_open_attempt_without_original(tmp_path, monkeypatch, missing):
    import shutil
    from src.autoslice.semantic_retry_progress import SemanticAttemptRecorder, RetryNeedsPlan

    monkeypatch.setenv('AUTOSLICE_BASE', str(tmp_path))
    calls = []
    call = provider(tmp_path / 'cache/completed-reviews', REQUESTED,
                    CpaCompletion(CURRENT, 'answering'), calls)
    def recorder():
        return SemanticAttemptRecorder(srt_text='1\n00:00:00,000 --> 00:00:01,000\n原文\n',
            finding={'cue_index': 1}, clip_context={'source_media_sha256': 'a' * 64}, offset_ms=0)
    assert recorder().wrap(call)(PROMPT) == CURRENT
    if missing == 'state':
        next((tmp_path / 'cache/semantic-cpa-attempts').glob('*/.dispatch-claims/text-*/state')).unlink()
    else:
        shutil.rmtree(tmp_path / 'cache/completed-reviews/requested-model-responses')
    original = tree(tmp_path)
    with pytest.raises(RetryNeedsPlan, match='CPA_TEXT_COMPLETED_RESPONSE_REQUIRED'):
        recorder().wrap(call)(PROMPT)
    assert tree(tmp_path) == original and calls == [PROMPT]
