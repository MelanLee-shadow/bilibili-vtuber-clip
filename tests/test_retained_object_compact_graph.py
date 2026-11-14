"""Public-contract tests for compact immutable retained JSON.

These tests do not grant domain/release authority. The large actual journal
still requires a complete guarded native replay.
"""
from copy import copy, deepcopy
from dataclasses import FrozenInstanceError, asdict, fields, is_dataclass, replace
import hashlib
import json
import pickle
import sys

import pytest

from src.autoslice import finding_closure_ports as ports
from src.autoslice import canonical_json_hash as codec

RetainedObject = ports.RetainedObject


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), allow_nan=False)


def container_ids(value):
    """Count occurrences, rejecting any mutable alias in a detached value."""
    found = set()
    pending = [value]
    while pending:
        node = pending.pop()
        if isinstance(node, (dict, list)):
            assert id(node) not in found, 'detached JSON contains a mutable alias'
            found.add(id(node))
            pending.extend(node.values() if isinstance(node, dict) else node)
    return found


def test_detached_values_separate_every_mutable_occurrence_and_every_read():
    shared = {'z': [{'text': 'original', 'numbers': [1, 2]}], 'a': []}
    source = {'right': shared, 'left': shared, 'many': (shared, shared)}
    document = RetainedObject.from_value(source)
    expected = json.loads(canonical(source))
    source['right']['z'][0]['text'] = 'caller mutation'
    source['left']['a'].append('caller mutation')
    first, second = document.value(), document.value()
    assert first == second == expected
    first_ids, second_ids = container_ids(first), container_ids(second)
    assert first_ids.isdisjoint(second_ids)
    first['left']['z'][0]['numbers'].append(9)
    first['left']['z'][0]['text'] = 'consumer mutation'
    first['many'][0]['a'].append('another mutation')
    assert first['right'] == expected['right']
    assert first['many'][1] == expected['many'][1]
    assert second == document.value() == expected
    assert isinstance(first['many'], list)


def test_value_preserves_canonical_key_iteration_order_at_every_depth():
    document = RetainedObject.from_value({'z': {'中': 1, 'a': 2, 'Z': 3},
                                           'a': [{'b': 1, 'a': 2}]})
    value = document.value()
    assert list(value) == ['a', 'z']
    assert list(value['z']) == ['Z', 'a', '中']
    assert list(value['a'][0]) == ['a', 'b']


def test_large_immutable_leaf_can_be_reused_while_containers_stay_independent():
    text = 'historical answer 汉字🙂\\\n' * 8192
    branch = {'answer': text, 'parts': [text]}
    document = RetainedObject.from_value({'a': branch, 'b': branch})
    first, second = document.value(), document.value()
    container_ids(first)
    container_ids(second)
    assert first['a'] is not first['b']
    assert first['a']['parts'] is not first['b']['parts']
    # Immutable string identity is not observable as a mutation channel. Its
    # reuse is the point of removing multi-GiB copies of repeated raw answers.
    assert first['a']['answer'] is first['b']['answer']
    assert first['a']['answer'] is second['a']['answer']
    assert first['a']['answer'] is first['a']['parts'][0]


@pytest.mark.parametrize('value', [
    {'z': [None, True, False, 0, 1, -1, 1.0, -0.0, 1.25, 1e100]},
    {'unicode': '汉字🙂é\u2028\u2029', 'escape': '\\"\n\t\r\x00'},
    {'a': [{'z': 2, 'a': 1}], 'b': (1, 2)},
    {'text': '汉字🙂' * 24000},
])
def test_exact_canonical_text_digest_and_value(value):
    text = canonical(value)
    document = RetainedObject.from_value(value)
    assert document.sha256 == hashlib.sha256(text.encode('utf-8')).hexdigest()
    assert document.json_text == text
    assert document.value() == json.loads(text)
    assert document == RetainedObject(text)
    assert RetainedObject(text) == document


def test_core_operations_do_not_materialize_or_decode_the_full_json_tree(monkeypatch):
    marker = 'compact-retained-public-contract-marker'
    branch = {'text': 'long unchanged historical answer 汉' * 4096}
    value = {'marker': marker, 'copies': [branch] * 24}
    expected = canonical(value)
    expected_digest = hashlib.sha256(expected.encode()).hexdigest()
    original_dumps, original_loads = json.dumps, json.loads

    def reject_full_dump(obj, *args, **kwargs):
        if isinstance(obj, dict) and obj.get('marker') == marker:
            raise AssertionError('eager complete retained JSON serialization')
        return original_dumps(obj, *args, **kwargs)

    def reject_full_loads(obj, *args, **kwargs):
        if isinstance(obj, str) and marker in obj:
            raise AssertionError('complete retained JSON decode')
        return original_loads(obj, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(json, 'dumps', reject_full_dump)
        patch.setattr(json, 'loads', reject_full_loads)
        left = RetainedObject.from_value(value)
        right = RetainedObject.from_value(value)
        assert left == right
        assert left.sha256 == expected_digest
        detached = left.value()
        assert detached['marker'] == marker
        container_ids(detached)
    assert left.json_text == expected


@pytest.mark.parametrize(('text', 'reason'), [
    ('{"x":NaN,', 'FINDING_CLOSURE_JSON_INVALID'),
    ('{"x":NaN,"x":0}', 'FINDING_CLOSURE_JSON_NOT_CANONICAL'),
    ('{"x":0,"x":NaN}', 'FINDING_CLOSURE_JSON_NONFINITE'),
    ('{"b":1,"a":2}', 'FINDING_CLOSURE_JSON_NOT_CANONICAL'),
    ('{"a":1,"a":1}', 'FINDING_CLOSURE_JSON_NOT_CANONICAL'),
    (' [] ', 'FINDING_CLOSURE_JSON_OBJECT_REQUIRED'),
    ('not json', 'FINDING_CLOSURE_JSON_INVALID'),
])
def test_public_string_constructor_preserves_error_precedence(text, reason):
    with pytest.raises(ports.ClosureContractError, match=reason):
        RetainedObject(text)


def test_nonfinite_cycle_and_non_object_from_value_are_still_rejected():
    with pytest.raises(ports.ClosureContractError, match='FINDING_CLOSURE_JSON_NONFINITE'):
        RetainedObject.from_value({'x': float('nan')})
    cycle = {}
    cycle['self'] = cycle
    try:
        with pytest.raises(ports.ClosureContractError, match='FINDING_CLOSURE_JSON_VALUE_INVALID'):
            RetainedObject.from_value(cycle)
    finally:
        cycle.clear()
    with pytest.raises(ports.ClosureContractError, match='FINDING_CLOSURE_JSON_OBJECT_REQUIRED'):
        RetainedObject.from_value([])


def test_from_value_keeps_integer_encoding_failure_at_construction():
    limit = getattr(sys, 'get_int_max_str_digits', lambda: 0)()
    if not limit:
        pytest.skip('interpreter has no integer-to-string limit')
    value = {'large': 10 ** (limit + 20)}
    with pytest.raises(ValueError):
        canonical(value)
    with pytest.raises(ValueError):
        RetainedObject.from_value(value)


def test_surrogate_text_is_retained_and_encoding_fails_only_when_hashing():
    value = {'text': '\ud800'}
    text = canonical(value)
    document = RetainedObject.from_value(value)
    assert document.json_text == text
    assert document.value() == value
    assert RetainedObject(text) == document
    with pytest.raises(UnicodeEncodeError):
        _ = document.sha256


def test_scalar_subclasses_are_normalized_like_the_standard_json_roundtrip():
    class StrangeText(str):
        def __str__(self):
            return 'wrong overridden str'

    class StrangeInt(int):
        def __int__(self):
            return -999

        def __repr__(self):
            return 'wrong overridden repr'

    class StrangeFloat(float):
        def __float__(self):
            return -999.0

        def __repr__(self):
            return 'wrong overridden repr'

    source = {'s': StrangeText('original'), 'n': StrangeInt(7), 'f': StrangeFloat(1.25)}
    text = canonical(source)
    document = RetainedObject.from_value(source)
    result = document.value()
    assert document.json_text == text
    assert result == json.loads(text)
    assert type(result['s']) is str
    assert type(result['n']) is int
    assert type(result['f']) is float


def test_key_subclass_returning_itself_is_plain_after_value_roundtrip():
    class Key(str):
        def __str__(self):
            return self

    source = {Key('key'): {'nested': 1}}
    document = RetainedObject.from_value(source)
    result = document.value()
    assert document.json_text == canonical(source)
    assert result == json.loads(canonical(source))
    assert type(next(iter(result))) is str


def test_reduce_payload_does_not_expose_the_mutable_private_graph():
    source = {'marker': 'reduce-payload-detached', 'values': [1, {'text': 'original'}]}
    document = RetainedObject.from_value(source)
    digest = document.sha256
    pending = [document.__reduce__()]
    seen = set()
    while pending:
        value = pending.pop()
        if not isinstance(value, (dict, list, tuple)) or id(value) in seen:
            continue
        seen.add(id(value))
        if isinstance(value, dict):
            if value.get('marker') == 'reduce-payload-detached':
                value['values'][1]['text'] = 'protocol-state mutation'
            pending.extend(value.values())
        else:
            pending.extend(value)
    assert document.value() == source
    assert document.sha256 == digest


def test_dataclass_public_field_replace_repr_copy_pickle_and_match_remain_compatible():
    document = RetainedObject.from_value({'z': [1], 'a': '汉'})
    text = canonical({'z': [1], 'a': '汉'})
    assert is_dataclass(document)
    assert [field.name for field in fields(document)] == ['json_text']
    assert asdict(document) == {'json_text': text}
    assert RetainedObject.__match_args__ == ('json_text',)
    assert repr(document) == 'RetainedObject(json_text=' + repr(text) + ')'
    assert hash(document) == hash((text,))
    assert replace(document) == document
    other = replace(document, json_text='{"changed":true}')
    assert other.value() == {'changed': True}
    for restored in (copy(document), deepcopy(document), pickle.loads(pickle.dumps(document))):
        assert restored == document
        assert restored.json_text == text
        assert restored.sha256 == document.sha256
        restored.value()['z'].append(2)
        assert document.value()['z'] == [1]
    with pytest.raises(FrozenInstanceError):
        document.json_text = '{}'


def test_subclass_factory_keeps_its_public_string_constructor_hook_and_type_equality():
    calls = []

    class Child(RetainedObject):
        def __post_init__(self):
            calls.append(self.json_text)
            super().__post_init__()

    text = canonical({'b': 2, 'a': 1})
    child = Child.from_value({'b': 2, 'a': 1})
    assert type(child) is Child
    assert calls == [text]
    assert child.json_text == text
    assert child.value() == {'a': 1, 'b': 2}
    assert child == Child(text)
    assert child != RetainedObject(text)
    assert RetainedObject(text) != child


def test_equality_remains_strict_when_the_digest_function_collides(monkeypatch):
    # Patch both the original function and a possible module-local import.
    # Equality must remain a canonical comparison under a digest collision.
    collision = lambda *_args, **_kwargs: '0' * 64
    monkeypatch.setattr(codec, 'canonical_sha256', collision)
    monkeypatch.setattr(ports, 'canonical_sha256', collision, raising=False)
    cases = [({'a': 1}, {'a': True}), ({'a': 1}, {'a': 1.0}),
             ({'a': 0.0}, {'a': -0.0}), ({'a': 'x'}, {'a': 'y'}),
             ({'a': [1, 2]}, {'a': [2, 1]}), ({'a': 1}, {'b': 1})]
    for left_value, right_value in cases:
        left, right = RetainedObject.from_value(left_value), RetainedObject.from_value(right_value)
        assert left.sha256 == right.sha256 == '0' * 64
        assert left != right
        assert right != left
    same_a = RetainedObject.from_value({'z': [1], 'a': {'b': 2}})
    same_b = RetainedObject.from_value({'a': {'b': 2}, 'z': [1]})
    assert same_a == same_b
