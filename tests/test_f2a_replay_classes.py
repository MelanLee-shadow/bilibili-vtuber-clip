"""F2a ordinary-path relations and authorization controls; no business models."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path

import pytest

from tests.test_targeted_relisten_public_contract import build_offline_rig
from tests.test_terminal_contract_roundtrip import produce, c_reply


@pytest.fixture
def rig(tmp_path, monkeypatch):
    return build_offline_rig(tmp_path, monkeypatch)


def test_ordinary_terminal_material_bag_cold_read_and_controls(rig):
    from src.autoslice.semantic_terminal_adjudication import terminal_prompt_matches
    from src.autoslice.prompt_materials import same_prompt_materials
    audit, capture = produce(rig, c_reply)
    assert audit.get('keep_route') == 'C_MEANING_EQUIVALENT', audit
    prompt, request = capture['prompt'], capture['request']
    prefix, raw = prompt.rsplit('\n', 1)
    data = json.loads(raw)
    # The real ordinary producer supplies the bag; no manually registered ID.
    bag = data['stopped_finding_evidence']['recorded_evidence']
    assert len(bag) > 1
    bag.reverse()
    permuted = prefix + '\n' + json.dumps(data, ensure_ascii=False, sort_keys=True)
    assert terminal_prompt_matches(request, permuted)
    assert prompt != permuted  # old exact-byte generator fails this check
    bag.append(deepcopy(bag[0]))
    assert not same_prompt_materials(prompt, prefix + '\n' + json.dumps(data, ensure_ascii=False))
    data = json.loads(raw)
    data['current'] += '伪造事实'
    assert not terminal_prompt_matches(request, prefix + '\n' + json.dumps(data, ensure_ascii=False))
    assert not same_prompt_materials(prompt, '新增授权\n' + prompt)


def test_nonterminal_ordinary_raw_checkpoint_uses_bag_not_prompt_bytes(tmp_path):
    from src.autoslice.completed_review_checkpoint import ruling_response
    from tests.test_f1_ruling_reuse import provider
    call = provider('{"findings":[]}')
    call.completed_review_root = tmp_path / 'raw'
    def send(rows):
        return ruling_response(stage='non-terminal-evidence',
            prompt='Only discover, no edit authority. JSON\n' + json.dumps({
                'source': 'a' * 64,
                'stopped_finding_evidence': {'recorded_evidence': rows}}),
            llm_call=call, inputs={'source': 'a' * 64}, parser_path=Path(__file__))
    rows = [{'source': 'a' * 64, 'text': '甲'}, {'source': 'a' * 64, 'text': '乙'}]
    assert send(rows)[1] is False
    assert send(rows[::-1])[1] is True
    assert len(call.calls) == 1
    assert send(rows + [rows[0]])[1] is False
    assert send([{**rows[0], 'source': 'b' * 64}, rows[1]])[1] is False
    assert len(call.calls) == 3





def test_unrelated_partial_cache_cannot_block_a_new_material_question(tmp_path):
    from src.autoslice.completed_review_checkpoint import CompletedReviewCheckpoint
    root = tmp_path / 'raw'
    unrelated = root / ('a' * 64)
    unrelated.mkdir(parents=True)
    (unrelated / '.partial.tmp').write_text('{')
    prompt = 'JSON\n' + json.dumps({'stopped_finding_evidence': {'recorded_evidence': ['甲', '乙']}})
    current = CompletedReviewCheckpoint(root, {'stage': 'different', 'current': 'bound'}, prompt)
    assert current.load() is None


@pytest.mark.parametrize('damage', ['none', 'matching_partial', 'different_answer', 'material_change'])
def test_empty_destination_recovers_original_without_redispatch_and_bound_errors_block(tmp_path, damage):
    from src.autoslice.completed_review_checkpoint import CompletedReviewCheckpoint, CompletedReviewCheckpointError
    root = tmp_path / 'raw'
    def prompt(rows):
        return 'JSON\n' + json.dumps({'stopped_finding_evidence': {'recorded_evidence': rows}})
    binding = {'stage': 'same', 'source': 'a' * 64}
    first = CompletedReviewCheckpoint(root, binding, prompt(['甲', '乙']))
    first.raw = 'original negative answer'
    first.save()
    first.directory.rename(first.legacy_directory)
    original = {p: p.read_bytes() for p in first.legacy_directory.glob('*.json')}
    new_prompt = prompt(['甲', '丙'] if damage == 'material_change' else ['乙', '甲'])
    after = CompletedReviewCheckpoint(root, binding, new_prompt)
    after.directory.mkdir(parents=True)
    calls = []
    def never(prompt):
        calls.append(prompt)
        return 'new response only for actually new material'
    if damage == 'matching_partial':
        (first.legacy_directory / '.partial.tmp').write_text('{')
        with pytest.raises(CompletedReviewCheckpointError, match='BYTES_DRIFT'):
            after.response(never)
    elif damage == 'different_answer':
        original_payload = json.loads(next(iter(original.values())))
        original_payload['response'] = 'different original answer'
        original_payload['response_sha256'] = hashlib.sha256(original_payload['response'].encode()).hexdigest()
        payload = json.dumps(original_payload, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()
        (first.legacy_directory / (hashlib.sha256(payload).hexdigest() + '.json')).write_bytes(payload)
        with pytest.raises(CompletedReviewCheckpointError, match='AMBIGUOUS'):
            after.response(never)
    elif damage == 'material_change':
        assert after.load() is None
        assert after.response(never) == 'new response only for actually new material'
        assert len(calls) == 1
    else:
        assert after.response(never) == 'original negative answer'
    if damage != 'material_change':
        assert calls == []
    assert all(p.read_bytes() == raw for p, raw in original.items())





@pytest.mark.parametrize('reason', ['不能据此认定人名', '不能认定“没有依据提出不同于现稿”就能保留现稿',
    '缺音频，不足以提出不同于现稿的候选，但现稿同样未获支持'])
def test_free_text_never_selects_a_route(reason, monkeypatch):
    from src.autoslice.semantic_answer import proposal_status
    from src.autoslice.missing_proposal_bootstrap import bootstrap_missing_proposal
    monkeypatch.delenv('AUTOSLICE_BASE', raising=False)
    value = {'status': 'UNRESOLVED', 'proposed_cue': '', 'needs_audio': True, 'reason': reason}
    assert proposal_status(value) == 'UNRESOLVED'
    finding, audit = bootstrap_missing_proposal(srt_text='1\n00:00:00,000 --> 00:00:01,000\n未知短句\n',
        finding={'cue_index': 1, 'suspect': '未知短句'}, structured_chat='',
        llm_call=lambda _: json.dumps(value), derive_single_span_edit=lambda *a: None)
    assert finding is None and audit['status'] == 'UNRESOLVED'
    assert proposal_status({**value, 'status': 'CURRENT_SUPPORTED'}) == 'CURRENT_SUPPORTED'





def test_version_decoder_does_not_grant_new_authority(rig):
    from src.autoslice.answer_contract import decode_answer, TERMINAL
    from src.autoslice.semantic_terminal_adjudication import validate_terminal_answer
    _, capture = produce(rig, c_reply)
    request = capture['request']
    assert request['answer_contract_version'] == TERMINAL
    missing_route = {k:v for k,v in capture['answer'].items() if k != 'keep_route'}
    with pytest.raises(ValueError, match='KEEP_ROUTE_REQUIRED'):
        validate_terminal_answer(request, missing_route)
    with pytest.raises(ValueError, match='VERSION_MISMATCH'):
        decode_answer(request, {**capture['answer'], 'contract_version': 'subtitle-terminal-answer.v1'})
    with pytest.raises(ValueError, match='UNSUPPORTED'):
        decode_answer({**request, 'answer_contract_version': 'future'}, capture['answer'])
    old_request = {k:v for k,v in request.items() if k not in {'answer_contract_version', 'terminal_contract_version'}}
    # Missing version and missing original prompt cannot silently mean latest or oldest.
    assert 'keep_route' not in decode_answer(old_request, missing_route)
    assert 'no_new_facts' not in decode_answer(old_request, missing_route)


def test_old_editorial_request_is_decoded_without_fabricated_fields():
    from src.autoslice.answer_contract import decode_answer, answer_version
    old = {'schema_version': 'editorial-subtitle-request.v1', 'current_cue': '原句'}
    raw = {'status': 'UNRESOLVED', 'reason': '没有编辑授权'}
    assert answer_version(old, family='editorial') == 'subtitle-editorial-answer.v1'
    assert decode_answer(old, raw, family='editorial') == raw
    from src.autoslice.editorial_subtitle import _decision
    with pytest.raises(ValueError, match='EDITORIAL_UNRESOLVED'):
        _decision(old, raw)


def test_legacy_stop_diagnostic_change_never_admits_new_material(rig, monkeypatch):
    from src.autoslice import targeted_relisten as relisten
    _, captured = produce(rig, c_reply)
    request = captured['request']
    proof = deepcopy(request['audio_gain_termination'])
    check = deepcopy(request['check_request'])
    # Ordinary new contract remains strict; representation migration below
    # applies only to original legacy requests, never creates target exhaustion.
    current = deepcopy(proof['projection'])
    current['rejected_observations'] = [{'observation_id': 'excluded', 'reason_code': 'NEW_DIAGNOSTIC'}]
    from src.autoslice.prompt_materials import same_legacy_observation_materials
    assert same_legacy_observation_materials(proof['projection'], current)
    extra = deepcopy(current)
    extra['observations'].append(deepcopy(extra['observations'][0]))
    assert not same_legacy_observation_materials(proof['projection'], extra)
    extra = deepcopy(current)
    extra['effective_distinct_observations'] += 1
    assert not same_legacy_observation_materials(proof['projection'], extra)
    monkeypatch.setattr(relisten, 'evidence_projection', lambda *a, **k: current)
    with pytest.raises(ValueError, match='FINAL_STOP_OBSERVATION_DRIFT'):
        relisten.compile_loop_stop_no_gain(check=check, finding=proof['stopped_finding'],
            pass_receipts=proof['pass_receipts'], projection=proof['projection'])


def test_language_route_uses_typed_provenance_not_negative_explanation(rig):
    from tests.test_targeted_relisten_public_contract import acquire_narrow
    from src.autoslice.missing_proposal_bootstrap import collect_proposal_audio
    witness = acquire_narrow(rig)
    calls = []
    def exact(request):
        calls.append(request)
        return {'reason_code': 'OFFLINE_NO_NEW_EVIDENCE'}
    base = dict(request=rig.check, entity_verifier=lambda _: witness, transcript_provider=exact)
    collect_proposal_audio(**base, finding={**rig.finding, 'why': '不是日语或外语，不应该走foreign路线'})
    assert not calls
    collect_proposal_audio(**base, finding={**rig.finding,
        'candidate_provenance': {'kind': 'audio_language_discovery', 'mutation_authorized': False}})
    assert len(calls) == 1
