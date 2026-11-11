"""A second ordinary question keeps a real non-name comparison visible."""
import json
from copy import deepcopy

import pytest

from tests.test_terminal_contract_roundtrip import c_reply, produce


def test_mixed_name_and_other_difference_enters_v5_recovery(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from scripts import profile_glossary_terms
    from tests import test_targeted_relisten_public_contract as protocol
    from src.autoslice import semantic_terminal_adjudication as terminal
    from src.autoslice import targeted_relisten as relisten
    from src.autoslice.final_review_contract import is_release_resolved_keep
    current, alternate = '谢谢阿珍的花', '谢阿强的花'
    monkeypatch.setattr(profile_glossary_terms, 'load_glossary_terms',
                        lambda _: SimpleNamespace(canon=['阿珍']))
    srt = protocol.SRT.replace('我听到了，呜呜', current)
    monkeypatch.setattr(protocol, 'SRT', srt)
    finding = {**protocol.FINDING, 'suspect': current, 'suggestion': alternate,
               'proposed_full_cue': alternate}
    rig = protocol.build_offline_rig(tmp_path, monkeypatch, srt=srt, finding=finding)
    def old_reply(data, refs):
        return {**c_reply(data, refs), 'comparison_text': alternate,
                'name_only': True, 'name_span': [2, 4]}
    with monkeypatch.context() as old:
        old.setattr(terminal, 'TERMINAL_CONTRACT_VERSION', 4)
        rejected, _ = produce(rig, old_reply)
    assert rejected['reason_code'] == 'TERMINAL_NAME_SLOT_BINDING_REQUIRED'
    assert rejected['terminal_review']['request']['terminal_glossary'] == ['阿珍']
    review = deepcopy(rejected['terminal_review'])
    calls = []
    def judge(prompt):
        calls.append(prompt)
        assert '若比较句在槽外也有差异，name_only 必须为 false' in prompt
        data = json.loads(prompt.rsplit('\n', 1)[1])
        refs = json.loads(next(line.split('：', 1)[1] for line in prompt.splitlines()
                               if line.startswith('可引用编号')))
        return {**c_reply(data, refs), 'comparison_text': alternate, 'name_only': False,
                'reason': '两句均表达感谢礼物，姓名和重复谢语均不确定。',
                'meaning_equivalence': {'status': 'EQUIVALENT',
                    'current_meaning': '感谢阿珍送花', 'alternative_meaning': '感谢阿强送花',
                    'shared_meaning': '感谢被念到的用户送花', 'uncertain_detail': '姓名与谢语重复',
                    'context_basis': '上下文为感谢礼物', 'evidence_cue_ids': [2, 3]}}
    def call(prompt):
        return json.dumps(judge(prompt), ensure_ascii=False)
    row = {**rig.finding, 'exact_release_adjudication': rejected}
    result = relisten.close_with_existing_editorial(srt_text=rig.srt, finding=row,
        recovery_audit={'terminal_check_request': review['request']['check_request'],
                        'no_gain_proof': review['request']['audio_gain_termination']},
        prior=rejected, clip_context=rig.context, entity_verifier=rig.verifier, judge_llm_call=call)
    assert len(calls) == 1
    assert result.get('policy_branch') == 'SUB18_CONTEXT_MEANING_EQUIVALENT_KEEP', result.get('reason_code')
    assert result['terminal_review']['request']['terminal_contract_version'] == terminal.TERMINAL_CONTRACT_VERSION
    assert json.loads(result['terminal_review']['raw_completion'])['comparison_text'] == alternate
    assert is_release_resolved_keep({**result['rebuilt_finding'], 'exact_release_adjudication': result})
    assert rejected['terminal_review'] == review
    answer = json.loads(result['terminal_review']['raw_completion'])
    with pytest.raises(ValueError, match='TERMINAL_UNSOURCED_NAME_RETAIN_CURRENT'):
        terminal.validate_terminal_answer(result['terminal_review']['request'],
            {**answer, 'choice': 'PROPOSED', 'keep_route': None})


def test_mixed_glossary_comparison_cannot_override_qualified_clear_audio(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from scripts import profile_glossary_terms
    from tests import test_targeted_relisten_public_contract as protocol
    from src.autoslice import semantic_terminal_adjudication as terminal
    current, alternate = '谢谢阿珍的花', '谢阿强的花'
    srt = protocol.SRT.replace('我听到了，呜呜', current)
    monkeypatch.setattr(protocol, 'SRT', srt)
    monkeypatch.setattr(profile_glossary_terms, 'load_glossary_terms',
                        lambda _: SimpleNamespace(canon=['阿珍']))
    finding = {**protocol.FINDING, 'suspect': current, 'suggestion': alternate,
               'proposed_full_cue': alternate}
    rig = protocol.build_offline_rig(tmp_path, monkeypatch, srt=srt, finding=finding)
    def mixed(data, refs):
        return {**c_reply(data, refs), 'comparison_text': alternate, 'name_only': False}
    audit, captured = produce(rig, mixed, wide=('谢谢阿珍没送花', '谢谢阿珍没送花'))
    request = captured['request']
    assert captured['data']['clear_audio_constraints']
    assert audit.get('reason_code') in {
        'TERMINAL_KEEP_MEANING_CONTRACT_REQUIRED', 'EDITORIAL_CLEAR_AUDIO_CONFLICT'}
    with pytest.raises(ValueError, match='EDITORIAL_CLEAR_AUDIO_CONFLICT'):
        terminal._protect_terminal_current(request, captured['answer'], equivalent_current=True)


def test_v8_shared_name_slot_contract_repairs_sourced_generator_and_controls(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from tests import test_targeted_relisten_public_contract as protocol
    from src.autoslice import semantic_terminal_adjudication as terminal
    from src.autoslice.clip_context import _canonical_json_sha256
    from src.autoslice.final_review_contract import is_release_resolved_keep
    from scripts import profile_glossary_terms

    current, alternate = '谢谢阿珍的花', '谢谢阿强的花'
    srt = protocol.SRT.replace('我听到了，呜呜', current)
    monkeypatch.setattr(protocol, 'SRT', srt)
    monkeypatch.setattr(profile_glossary_terms, 'load_glossary_terms',
                        lambda _: SimpleNamespace(canon=['阿珍']))
    finding = {**protocol.FINDING, 'suspect': current, 'suggestion': alternate,
               'proposed_full_cue': alternate}
    rig = protocol.build_offline_rig(tmp_path, monkeypatch, srt=srt, finding=finding)
    rig.context['source_fact'] = {'surfaces': ['阿珍'], 'display_name': '阿珍'}
    rig.context['context_sha256'] = _canonical_json_sha256(
        {key: value for key, value in rig.context.items() if key != 'context_sha256'})
    reference = 'whole_session_and_session_context.source_fact'

    def sourced_reply(data, refs, *, span):
        source = data['whole_session_and_session_context']['source_fact']
        assert refs[reference] == ['whole_session_and_session_context', 'source_fact']
        answer = {**c_reply(data, refs), 'comparison_text': alternate, 'name_only': False,
                  'name_source_ref': reference,
                  'name_source_text': json.dumps(source, ensure_ascii=False, sort_keys=True,
                                                 separators=(',', ':')),
                  'sourced_name': source['display_name']}
        if span is not None:
            answer['name_span'] = span
        return answer

    # The old producer had source fields but no shared name_span instruction;
    # the unchanged consumer correctly rejects that generated answer.
    with monkeypatch.context() as old:
        old.setattr(terminal, 'TERMINAL_CONTRACT_VERSION', 7)
        rejected, old_captured = produce(rig, lambda data, refs: sourced_reply(data, refs, span=None))
    assert rejected['reason_code'] == 'TERMINAL_NAME_SLOT_BINDING_REQUIRED'
    assert '姓名槽共享合同' not in old_captured['prompt']
    assert old_captured['response_schema']['properties']['name_span'] == {
        'type': 'array', 'items': {'type': 'integer'}}

    # v8 tells the generator that a source citation does not replace the
    # current-text slot, and the exact span is accepted by the same validator.
    with monkeypatch.context() as current_contract:
        current_contract.setattr(terminal, 'TERMINAL_CONTRACT_VERSION', 8)
        accepted, captured = produce(rig, lambda data, refs: sourced_reply(data, refs, span=[2, 4]))
    assert captured['request']['terminal_contract_version'] == 8
    assert '姓名槽共享合同' in captured['prompt']
    assert captured['data']['name_slot_contract'] == terminal.terminal_name_slot_contract()
    contract = captured['data']['name_slot_contract']
    assert captured['response_schema']['description'] == contract['schema_description']
    for name in ('name_span', 'name_source_ref', 'name_source_text', 'sourced_name'):
        assert captured['response_schema']['properties'][name]['description'] == contract[name + '_description']
    from src.autoslice.response_schemas import terminal_schema
    for version in range(1, 8):
        legacy = terminal_schema({**captured['request'], 'terminal_contract_version': version})
        assert 'description' not in legacy
        for name in ('name_only', 'name_span', 'name_source_ref', 'name_source_text', 'sourced_name'):
            assert 'description' not in legacy['properties'][name]
    assert terminal.validate_terminal_answer(captured['request'], captured['answer']) == 'CURRENT'
    assert is_release_resolved_keep({**accepted['rebuilt_finding'], 'exact_release_adjudication': accepted})

    # Controls: unsourced name-only still needs the span, while a correctly
    # bound span is sufficient; a non-name comparison does not need one.
    unsourced = {key: value for key, value in captured['answer'].items()
                 if key not in {'name_source_ref', 'name_source_text', 'sourced_name', 'name_span'}}
    unsourced['name_only'] = True
    with pytest.raises(ValueError, match='TERMINAL_NAME_SLOT_BINDING_REQUIRED'):
        terminal.validate_terminal_answer(captured['request'], unsourced)
    assert terminal.validate_terminal_answer(captured['request'],
        {**unsourced, 'name_span': [2, 4]}) == 'CURRENT'

    non_name = {key: value for key, value in unsourced.items()
                if key not in {'name_only', 'name_span'}}
    non_name.update(comparison_text='谢谢阿珍的叶', name_only=False)
    assert terminal.validate_terminal_answer(captured['request'], non_name) == 'CURRENT'
