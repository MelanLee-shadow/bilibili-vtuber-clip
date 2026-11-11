"""Equal ordinary mutable histories have one private immutable representation."""
import copy
import hashlib
import json

from src.autoslice.finding_closure_ports import RetainedObject


def containers(value):
    pending, seen = [value], set()
    while pending:
        item = pending.pop()
        if isinstance(item, (dict, list)) and id(item) not in seen:
            seen.add(id(item))
            pending.extend(item.values() if isinstance(item, dict) else item)
    return len(seen)


def test_independent_consumer_copies_share_only_inside_the_retained_owner():
    history = {'request': {'observations': [{'index': n, 'text': 'retained original'}
                                          for n in range(40)]}}
    value = {'histories': [copy.deepcopy(history) for _ in range(64)]}
    before = copy.deepcopy(value)
    expected = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    retained = RetainedObject.from_value(value)
    assert containers(retained._json_graph) <= containers(history) + 2
    assert retained.json_text == expected
    assert retained.sha256 == hashlib.sha256(expected.encode()).hexdigest()
    assert value == before
    value['histories'][0]['request']['observations'][0]['text'] = 'caller mutation'
    first, second = retained.value(), retained.value()
    assert first == second == before
    first['histories'][0]['request']['observations'][0]['text'] = 'consumer mutation'
    assert first['histories'][1]['request']['observations'][0]['text'] == 'retained original'
    assert second == retained.value() == before


def test_unordered_equal_objects_share_but_canonical_number_types_stay_distinct():
    value = {'rows': [{'a': [1, True], 'b': 'x'}, {'b': 'x', 'a': [1, True]}],
             'numbers': [[True], [1], [1.0], [0.0], [-0.0]]}
    retained = RetainedObject.from_value(value)
    graph = retained._json_graph
    assert graph['rows'][0] is graph['rows'][1]
    assert len({id(item) for item in graph['numbers']}) == 5
    assert retained.json_text == json.dumps(value, sort_keys=True, separators=(',', ':'))


def test_public_expansion_can_be_retained_again_without_retaining_all_copies():
    leaf = {'original': [{'text': str(i), 'values': [i, i + 1]} for i in range(20)]}
    shared = leaf
    for _ in range(8):
        shared = {'left': shared, 'right': shared}
    original = RetainedObject.from_value(shared)
    expanded = original.value()
    restored = RetainedObject.from_value(expanded)
    assert containers(expanded) > 10000
    assert containers(restored._json_graph) == containers(original._json_graph)
    assert restored == original and restored.sha256 == original.sha256
