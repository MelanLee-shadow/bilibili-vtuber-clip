"""v7 producer/schema instructions for reliable-fragment occurrences."""
from copy import deepcopy

import pytest

from src.autoslice.editorial_subtitle import (
    _reliable_fragment_contract,
    validate_reliable_fragments,
)
from tests.test_targeted_relisten_public_contract import rig  # noqa: F401
from tests.test_terminal_contract_roundtrip import c_reply, produce


def test_v7_occurrence_contract_is_opt_in_and_preserves_validator_semantics():
    historical = _reliable_fragment_contract()
    historical_legacy = _reliable_fragment_contract(legacy=True)
    producer = _reliable_fragment_contract(producer=True)

    assert historical == {
        'ordered_nonoverlapping': True,
        'instructions': 'reliable_fragments列出原cue或独立音频/上下文内所有可靠片段，每项text及依据；'
                        '它们必须在输出中按序保留。'
                        '每项text必须按最终成句从左到右、互不重叠列出；不能按证据可信度排序，也不能同时列出相互包含的片段。',
    }
    assert historical_legacy == {
        'ordered_nonoverlapping': True,
        'instructions': 'reliable_fragments列出原cue或独立音频/上下文内所有可靠片段，每项text及依据；'
                        '它们必须在输出中按序保留。',
    }
    assert producer['ordered_nonoverlapping'] is True
    assert producer['occurrence_policy'] == 'MERGE_BASES_PER_OCCURRENCE_REPEAT_TRUE_OCCURRENCES'
    assert '同一出现有多路依据时合并到同一项basis中' in producer['instructions']
    assert '同一text若在最终输出中真实重复出现，则按真实出现次数分别列项' in producer['instructions']

    # The validator remains a strict occurrence consumer: the same text can
    # occur twice only when the proposed output really contains two spans.
    validate_reliable_fragments('你喜欢你喜欢', [
        {'text': '你喜欢', 'basis': '第一处的依据'},
        {'text': '你喜欢', 'basis': '第二处的依据'},
    ])
    with pytest.raises(ValueError, match='EDITORIAL_RELIABLE_FRAGMENT_LOST'):
        validate_reliable_fragments('你喜欢', [
            {'text': '你喜欢', 'basis': '同一处的第一依据'},
            {'text': '你喜欢', 'basis': '同一处的第二依据'},
        ])


def test_v8_terminal_prompt_material_and_schema_share_occurrence_contract(rig):
    from src.autoslice import semantic_terminal_adjudication as terminal
    from src.autoslice.response_schemas import terminal_schema

    _, captured = produce(rig, c_reply)
    request = captured['request']
    contract = _reliable_fragment_contract(producer=True)

    assert request['terminal_contract_version'] == 8
    assert captured['data']['reliable_fragments_contract'] == contract
    assert contract['instructions'] in captured['prompt']
    schema = terminal_schema(request)
    assert schema['properties']['reliable_fragments']['description'] == contract['instructions']

    v7_request = deepcopy(request)
    v7_request['terminal_contract_version'] = 7
    v7_materials, _ = terminal.terminal_materials(v7_request)
    v7_prompt = terminal.terminal_prompt(v7_request)
    assert v7_materials['reliable_fragments_contract'] == contract
    assert contract['instructions'] in v7_prompt
    assert '姓名槽共享合同' not in v7_prompt

    v6_request = deepcopy(request)
    v6_request['terminal_contract_version'] = 6
    v6_materials, _ = terminal.terminal_materials(v6_request)
    v6_prompt = terminal.terminal_prompt(v6_request)
    assert 'reliable_fragments_contract' not in v6_materials
    assert '同一出现有多路依据时合并到同一项basis中' not in v6_prompt
    assert terminal.terminal_contract_retry(v6_request)['kind'] == 'REASK_UNDER_CURRENT_CONTRACT'
    assert terminal.terminal_contract_retry(request)['kind'] == 'REVALIDATE_AFTER_CONSUMER_REPAIR_OR_NEW_MATERIAL'
