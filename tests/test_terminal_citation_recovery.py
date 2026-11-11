"""Ordinary terminal requests expose exactly the witness groups they require."""
from copy import deepcopy
import hashlib
import json
import subprocess
import sys

import pytest

from tests.test_targeted_relisten_public_contract import rig  # noqa: F401
from tests.test_terminal_contract_roundtrip import c_reply, produce


def _a_answer(data, refs):
    groups = data['grade_a_citation_contract']['eligible_witness_groups']
    count = data['grade_a_citation_contract']['minimum_distinct_witnesses']
    assert len(groups) >= count
    selected = [row['observation_refs'][0] for row in groups[:count]]
    return {**c_reply(data, refs), 'choice': 'CURRENT', 'grade': 'A',
        'keep_route': 'B_EVIDENCE_SUPPORTS_CURRENT', 'evidence_decisive': True,
        'comparison_text': data['current'], 'evidence_refs': selected,
        'meaning_equivalence': None}


def test_current_producer_prompt_schema_and_validator_share_exact_witness_groups(rig):
    from src.autoslice import semantic_terminal_adjudication as terminal
    from src.autoslice.final_review_contract import is_release_resolved_keep
    audit, captured = produce(rig, _a_answer, native=True,
        wide=('我听到了，呜呜', '我听到了，呜呜'))
    contract = captured['data']['grade_a_citation_contract']
    request = captured['request']
    assert contract == terminal.grade_a_citation_contract(request)
    assert json.dumps(contract, sort_keys=True) in captured['response_schema']['properties']['grade']['description']
    assert 'reason 中提到证人也不替代 evidence_refs' in captured['prompt']
    for group in contract['eligible_witness_groups']:
        assert terminal.grade_a_witnesses(request, group['observation_refs']) == {
            tuple(group['identity'][key] for key in contract['independence_fields'])}
    assert terminal.validate_terminal_answer(request, captured['answer']) == 'CURRENT'
    assert is_release_resolved_keep({**audit['rebuilt_finding'], 'exact_release_adjudication': audit})


def test_v5_missing_citation_reasks_once_without_rewriting_original_answer(rig, tmp_path, monkeypatch):  # noqa: F811
    from src.autoslice import semantic_terminal_adjudication as terminal
    from src.autoslice import targeted_relisten as relisten
    from src.autoslice.editorial_subtitle import replay_terminal_adjudication
    from src.autoslice.final_review_contract import is_release_resolved_keep
    from src.autoslice.review_evidence_store import persist_review

    def missing_citation(data, refs):
        wide = [row for row in data['observations'] if row['receipt_kind'] == 'blind_range'
                and row['observation_id'] in data['current_support_observation_refs']]
        # The real failure: the answer names all support but cites one group.
        return {**c_reply(data, refs), 'grade': 'A', 'keep_route': 'B_EVIDENCE_SUPPORTS_CURRENT',
            'evidence_decisive': True, 'comparison_text': data['current'],
            'current_support_observation_refs': data['current_support_observation_refs'],
            'evidence_refs': [wide[0]['observation_id']], 'meaning_equivalence': None}

    with monkeypatch.context() as old:
        old.setattr(terminal, 'TERMINAL_CONTRACT_VERSION', 5)
        rejected, _ = produce(rig, missing_citation, native=True,
            wide=('我听到了，呜呜', '我听到了，呜呜'))
    assert rejected['reason_code'] == 'TERMINAL_A_INDEPENDENT_WIDE_EVIDENCE_REQUIRED'
    original = deepcopy(rejected['terminal_review'])
    row = {**rig.finding, 'exact_release_adjudication': rejected}
    replay = replay_terminal_adjudication(rig.srt, row,
        check_request=original['request']['check_request'], clip_context=rig.context)
    assert replay['reason_code'] == rejected['reason_code']
    assert replay['contract_retry']['kind'] == 'REASK_UNDER_CURRENT_CONTRACT'
    calls = []
    def judge(prompt):
        calls.append(prompt)
        data = json.loads(prompt.rsplit('\n', 1)[1])
        return json.dumps(_a_answer(data, {}), ensure_ascii=False)
    result = relisten.close_with_existing_editorial(srt_text=rig.srt, finding=row,
        recovery_audit={'terminal_check_request': original['request']['check_request'],
                        'no_gain_proof': original['request']['audio_gain_termination']},
        prior=rejected, clip_context=rig.context, entity_verifier=rig.verifier, judge_llm_call=judge)
    assert len(calls) == 1
    assert rejected['terminal_review'] == original
    assert result['superseded_contract_answer']['original_raw_sha256'] == hashlib.sha256(
        original['raw_completion'].encode()).hexdigest()
    assert terminal.terminal_contract_retry(result['terminal_review']['request'])['reask_under_current_contract'] is False
    released = {**result['rebuilt_finding'], 'exact_release_adjudication': result}
    assert is_release_resolved_keep(released)
    path = tmp_path / 'review.json'
    persist_review(path, {'findings': [], 'unresolved_findings_disclosed': [released]})
    code = ('import sys; from src.autoslice.review_evidence_store import load_review; '
            'from src.autoslice.final_review_contract import is_release_resolved_keep; '
            'assert is_release_resolved_keep(load_review(sys.argv[1])["unresolved_findings_disclosed"][0])')
    subprocess.run([sys.executable, '-B', '-c', code, str(path)], check=True)


def test_citation_groups_do_not_make_nonwide_or_duplicate_identity_count(rig):
    from src.autoslice import semantic_terminal_adjudication as terminal
    _, captured = produce(rig, _a_answer, native=True,
        wide=('我听到了，呜呜', '我听到了，呜呜'))
    request, answer = captured['request'], captured['answer']
    contract = terminal.grade_a_citation_contract(request)
    first = contract['eligible_witness_groups'][0]['observation_refs']
    nonwide = [r['observation_id'] for r in request['audio_gain_termination']['projection']['observations']
               if r.get('receipt_kind') != 'blind_range']
    with pytest.raises(ValueError, match='TERMINAL_A_INDEPENDENT_WIDE_EVIDENCE_REQUIRED'):
        terminal.validate_terminal_answer(request, {**answer,
            'current_support_observation_refs': captured['data']['current_support_observation_refs'],
            'evidence_refs': [*first, *first, *nonwide]})
    with pytest.raises(ValueError, match='TERMINAL_EVIDENCE_REFS_INVALID'):
        terminal.validate_terminal_answer(request, {**answer, 'evidence_refs': [*answer['evidence_refs'], 'invented']})
    old_request = {**request, 'terminal_contract_version': 5}
    assert 'grade_a_citation_contract' not in terminal.terminal_materials(old_request)[0]
    assert 'reason 中提到证人也不替代 evidence_refs' not in terminal.terminal_prompt(old_request)


def test_new_ordinary_question_generates_its_own_citation_groups(tmp_path, monkeypatch):
    from tests import test_targeted_relisten_public_contract as protocol
    from src.autoslice.final_review_contract import is_release_resolved_keep
    current = '请帮我关门'
    srt = protocol.SRT.replace('我听到了，呜呜', current)
    monkeypatch.setattr(protocol, 'SRT', srt)
    finding = {**protocol.FINDING, 'suspect': current, 'suggestion': '请帮我开门',
               'proposed_full_cue': '请帮我开门'}
    sibling = protocol.build_offline_rig(tmp_path, monkeypatch, srt=srt, finding=finding)
    audit, captured = produce(sibling, _a_answer, native=True, wide=(current, current))
    assert captured['data']['current'] == current
    assert len(captured['answer']['evidence_refs']) >= 2
    assert is_release_resolved_keep({**audit['rebuilt_finding'], 'exact_release_adjudication': audit})
