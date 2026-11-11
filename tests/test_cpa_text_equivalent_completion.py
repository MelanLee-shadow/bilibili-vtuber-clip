import hashlib
import json
import shutil

import pytest

from src.autoslice.cpa_request_lifecycle import (
    TextRequest, binding_for, bind_request_meaning, completed_response_scope, digest, persistent_text_call,
)
from src.autoslice.completed_review_checkpoint import CompletedReviewCheckpoint, SCHEMA, ruling_material_scope, ruling_response
from src.autoslice.llm_client import CpaCompletion
from src.autoslice.semantic_retry_progress import RetryNeedsPlan


MODEL = {'transport': 'cpa_command', 'models': ['gpt-6.1-sol'], 'effort': 'medium'}
OLD = 'Review JSON:\n{"evidence": {"a": 1, "b": 2}}'
NEW = 'Review JSON:\n{"evidence":{"b":2,"a":1}}'
ANSWER = '{"findings":[]}'


@pytest.mark.parametrize('answer_model', ['gpt-6.1-sol', 'gpt-6-sol'])
def test_equivalent_completion_keeps_original_physical_owner(tmp_path, monkeypatch, answer_model):
    monkeypatch.setenv('AUTOSLICE_BASE', str(tmp_path))
    binding = binding_for(OLD, MODEL)
    assert binding == binding_for(NEW, MODEL)
    root = tmp_path / 'cache/completed-reviews'
    owner = TextRequest(root, binding)
    with owner.claim():
        first = owner.physical(OLD, MODEL, lambda _: CpaCompletion(ANSWER, answer_model))
    before = owner.path.read_bytes()
    resumed = TextRequest(root, binding)
    with resumed.claim():
        result = resumed.physical(NEW, MODEL, lambda _: pytest.fail('must not dispatch'))
    assert result == first == ANSWER
    assert result.answering_model == answer_model
    assert owner.path.read_bytes() == before
    proof = result.completed_text_request
    assert proof['original_prompt'] == OLD
    assert proof['physical']['input_sha256'] != hashlib.sha256(NEW.encode()).hexdigest()
    assert completed_response_scope(result, NEW) == digest(proof['physical'])
    with pytest.raises(RetryNeedsPlan, match='CPA_TEXT_COMPLETION_OWNER_INVALID'):
        completed_response_scope(result, 'different content')
    with ruling_material_scope({'source': 'different'}), pytest.raises(RetryNeedsPlan, match='CPA_TEXT_COMPLETION_OWNER_INVALID'):
        completed_response_scope(result, NEW)


@pytest.mark.parametrize('answer_model', ['gpt-6.1-sol', 'gpt-6-sol'])
def test_legacy_completion_without_grant_uses_bound_observation(tmp_path, monkeypatch, answer_model):
    monkeypatch.setenv('AUTOSLICE_BASE', str(tmp_path))
    owner = TextRequest(tmp_path / 'cache/completed-reviews', binding_for(OLD, MODEL))
    with owner.claim():
        owner.physical(OLD, MODEL, lambda _: CpaCompletion(ANSWER, answer_model))
    state = json.loads(owner.path.read_text())
    del state['events'][0]['physical_binding']
    owner.path.write_text(json.dumps(state))
    before = owner.path.read_bytes()
    with owner.claim():
        result = owner.physical(NEW, MODEL, lambda _: pytest.fail('must not dispatch'))
    assert result == ANSWER and completed_response_scope(result, NEW)
    assert result.answering_model == answer_model
    assert owner.path.read_bytes() == before


def test_missing_legacy_observation_does_not_authorize_resend(tmp_path, monkeypatch):
    monkeypatch.setenv('AUTOSLICE_BASE', str(tmp_path))
    root = tmp_path / 'cache/completed-reviews'
    owner = TextRequest(root, binding_for(OLD, MODEL))
    with owner.claim():
        owner.physical(OLD, MODEL, lambda _: CpaCompletion(ANSWER, 'gpt-6.1-sol'))
    state = json.loads(owner.path.read_text())
    del state['events'][0]['physical_binding']
    owner.path.write_text(json.dumps(state))
    for child in root.iterdir():
        if len(child.name) == 64 and child.is_dir():
            shutil.rmtree(child)
    with owner.claim(), pytest.raises(RetryNeedsPlan, match='CPA_TEXT_COMPLETED_OR_REJECTED_RESPONSE_NOT_REUSABLE'):
        owner.physical(NEW, MODEL, lambda _: pytest.fail('must not dispatch'))


def test_physical_index_tampering_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setenv('AUTOSLICE_BASE', str(tmp_path))
    owner = TextRequest(tmp_path / 'cache/completed-reviews', binding_for(OLD, MODEL))
    with owner.claim():
        owner.physical(OLD, MODEL, lambda _: CpaCompletion(ANSWER, 'gpt-6.1-sol'))
    state = json.loads(owner.path.read_text())
    state['events'][0]['physical_binding']['input_sha256'] = 'a' * 64
    owner.path.write_text(json.dumps(state))
    with pytest.raises(RetryNeedsPlan, match='CPA_TEXT_REQUEST_STATE_INVALID'):
        with owner.claim():
            pass


def test_changed_material_is_not_served_from_original_owner(tmp_path, monkeypatch):
    monkeypatch.setenv('AUTOSLICE_BASE', str(tmp_path))
    owner = TextRequest(tmp_path / 'cache/completed-reviews', binding_for(OLD, MODEL))
    with owner.claim():
        owner.physical(OLD, MODEL, lambda _: CpaCompletion(ANSWER, 'gpt-6.1-sol'))
    before = owner.path.read_bytes()
    changed = 'Review JSON:\n{"evidence":{"b":3,"a":1}}'
    with owner.claim(), pytest.raises(RetryNeedsPlan, match='CPA_TEXT_COMPLETED_OR_REJECTED_RESPONSE_NOT_REUSABLE'):
        owner.physical(changed, MODEL, lambda _: pytest.fail('must not dispatch'))
    assert owner.path.read_bytes() == before


def test_legacy_discovery_limit_never_authorizes_dispatch(tmp_path, monkeypatch):
    monkeypatch.setenv('AUTOSLICE_BASE', str(tmp_path))
    root = tmp_path / 'cache/completed-reviews'
    owner = TextRequest(root, binding_for(OLD, MODEL))
    with owner.claim():
        owner.physical(OLD, MODEL, lambda _: CpaCompletion(ANSWER, 'gpt-6.1-sol'))
    state = json.loads(owner.path.read_text())
    del state['events'][0]['physical_binding']
    owner.path.write_text(json.dumps(state))
    oversized = root / ('f' * 64) / 'unrelated.json'
    oversized.parent.mkdir()
    with oversized.open('wb') as stream:
        stream.truncate(8 * 1024 * 1024 + 1)
    before = owner.path.read_bytes()
    with owner.claim(), pytest.raises(RetryNeedsPlan, match='CPA_TEXT_LEGACY_DISCOVERY_LIMIT'):
        owner.physical(NEW, MODEL, lambda _: pytest.fail('must not dispatch'))
    assert owner.path.read_bytes() == before


def test_multiple_completed_equivalent_observations_are_ambiguous(tmp_path, monkeypatch):
    monkeypatch.setenv('AUTOSLICE_BASE', str(tmp_path))
    root = tmp_path / 'cache/completed-reviews'
    owner = TextRequest(root, binding_for(OLD, MODEL))
    with owner.claim(), ruling_material_scope(None, cache_allowed=False):
        for prompt, answer in ((OLD, '{"findings":[]}'), (NEW, '{"findings":[1]}')):
            owner.physical(prompt, MODEL, lambda _, answer=answer: CpaCompletion(answer, 'gpt-6.1-sol'))
    starts = [event for event in owner.state['events'] if event['status'] == 'STARTED']
    for event, prompt, answer in zip(starts, (OLD, NEW), ('{"findings":[]}', '{"findings":[1]}')):
        physical = event['physical_binding']
        checkpoint = CompletedReviewCheckpoint(root, {'schema_version': SCHEMA,
            'stage': 'cpa-text-physical', 'semantic_policy': 'h104-cpa-text-v1',
            'inputs': physical, 'provider': {'routing': MODEL}}, prompt)
        checkpoint.raw = CpaCompletion(answer, 'gpt-6.1-sol')
        checkpoint.save()
    third = 'Review JSON:\n{ "evidence": { "a": 1, "b": 2 } }'
    before = owner.path.read_bytes()
    with owner.claim(), pytest.raises(RetryNeedsPlan, match='CPA_TEXT_COMPLETION_AMBIGUOUS'):
        owner.physical(third, MODEL, lambda _: pytest.fail('must not dispatch'))
    assert owner.path.read_bytes() == before


@pytest.mark.parametrize('answer_model', ['gpt-6.1-sol', 'gpt-6-sol'])
def test_ordinary_ruling_persists_owned_semantic_reuse(tmp_path, monkeypatch, answer_model):
    monkeypatch.setenv('AUTOSLICE_BASE', str(tmp_path))
    root = tmp_path / 'cache/completed-reviews'
    provider = {'provider_transport': 'runtime_cpa', 'provider_endpoint_host': 'example.test',
                'provider_endpoint_path': '/v1'}
    contract = {'json_mode': True, 'schema_name': None, 'schema_sha256': None}
    def request(prompt):
        return persistent_text_call(lambda _: pytest.fail('must not dispatch'), prompt,
                                    model=MODEL, root=root, provider=provider, contract=contract)
    request.cpa_cache_identity = MODEL
    request.provider_runtime_binding = {**provider, 'provider_credential_source': '/fixture'}
    request.completed_review_root = root
    @bind_request_meaning
    def original(**kwargs):
        return persistent_text_call(lambda _: CpaCompletion(ANSWER, answer_model), kwargs['prompt'],
                                    model=MODEL, root=root, provider=provider, contract=contract)
    params = dict(stage='final-review-findings', llm_call=request, inputs={}, parser_path=__file__)
    assert original(prompt=OLD, **params) == ANSWER
    state = next((root / '.dispatch-claims').glob('text-*/state'))
    before = state.read_bytes()
    for _ in range(2):
        result, _ = ruling_response(prompt=NEW, **params)
        assert result == ANSWER and result.answering_model == answer_model
        assert completed_response_scope(result, NEW)
    assert state.read_bytes() == before
