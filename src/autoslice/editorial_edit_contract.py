"""One code-point edit contract for the prompt, schema and typed consumer."""
from __future__ import annotations

from .review_errors import ContractRejection


def edit_contract(current: str) -> dict:
    return {'base_field': 'current_cue', 'base_text': current,
        'unit': 'Unicode code point (Python str index)', 'interval': '[edit_start, edit_end)',
        'length': len(current), 'minimum_start': 0, 'maximum_end': len(current),
        'formula': 'current_cue[:edit_start] + replacement + current_cue[edit_end:]',
        'instructions': ('编辑以 current_cue 的原始字符串为基准，不预先去标点、空白或归一化；'
            '按 Unicode 码点（Python str 下标，非字节）计数，半开区间 [edit_start, edit_end)。'
            f'本句长度为 {len(current)}，须 0 <= edit_start < edit_end <= {len(current)}。'
            'replacement 仅是被选区间的替换文字，区间外自动原样保留；'
            f'整句替换必须用 edit_start=0、edit_end={len(current)}。'
            '结果=current_cue[:edit_start]+replacement+current_cue[edit_end:]。')}


def edit_schema(current: str | None = None) -> dict:
    spec = edit_contract(current if current is not None else '')
    bound = {'maximum': spec['maximum_end']} if current is not None else {}
    return {'edit_start': {'type': 'integer', 'minimum': spec['minimum_start'], **bound},
        'edit_end': {'type': 'integer', 'minimum': spec['minimum_start'] + 1, **bound},
        'replacement': {'type': 'string'}}


def apply_edit(current: str, answer) -> str:
    spec = edit_contract(current)
    start, end, replacement = (answer.get(k) for k in ('edit_start', 'edit_end', 'replacement'))
    if not (type(start) is int and type(end) is int
            and spec['minimum_start'] <= start < end <= spec['maximum_end']
            and isinstance(replacement, str) and '\n' not in replacement):
        raise ContractRejection('EDITORIAL_EDIT_INVALID')
    return current[:start] + replacement + current[end:]
