"""Local material readers retain shared history without changing public values."""
from collections import UserDict
import gc
import tracemalloc

import pytest

from src.autoslice import finding_context as context
from src.autoslice import finding_closure_shell as shell
from src.autoslice.finding_closure_ports import ConsumedRuling, RetainedObject
from tests.test_wpb_finding_context import check, materials
from tests.test_retained_read_view import owners
from tests.test_terminal_funnel_local_cost import SRT, FINDING


def repeated_history(depth=17):
    value = {'preserved': ['first', 'second'], 'raw_completion': 'original answer'}
    for _ in range(depth):
        value = {'first': value, 'later': value}
    return value


def measured(call):
    gc.collect()
    tracemalloc.start()
    try:
        result = call()
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    return result, peak


@pytest.mark.parametrize('location', ['request', 'finding', 'material'])
def test_context_consumers_do_not_expand_irrelevant_shared_history(location):
    request = check()
    expected = materials(request) if location == 'material' else context.context_for_check(request)
    history = repeated_history()
    finding = {'history': history} if location == 'finding' else None
    if location != 'finding':
        request['history'] = history
    call = (lambda: materials(request)) if location == 'material' else (
        lambda: context.context_for_check(request, finding=finding))
    result, peak = measured(call)
    assert result == expected
    assert peak < 8 * 1024**2, peak
    assert history['first'] is history['later']


@pytest.mark.parametrize('bad', [{1: 'non-string key'}, {'unused': object()}])
def test_readonly_context_still_validates_unconsumed_fields(bad):
    for field in ('request', 'finding'):
        request = check()
        finding = {'unused': bad} if field == 'finding' else None
        if field == 'request':
            request['unused'] = bad
        with pytest.raises(ValueError, match='FINDING_JSON_INVALID'):
            context.context_for_check(request, finding=finding)


def test_public_json_values_preserve_independent_mutable_occurrences_and_mapping_support():
    original = UserDict({'nested': ('text', {'value': [1]})})
    result = context.json_value([original, original])
    assert result == [{'nested': ['text', {'value': [1]}]}] * 2
    result[0]['nested'][1]['value'].append(2)
    assert result[1]['nested'][1]['value'] == [1]
    assert original['nested'][1]['value'] == [1]
    request = check()
    assert context.context_for_check(UserDict(request)) == context.context_for_check(request)


def test_consumed_ruling_attachment_keeps_history_and_ordinary_preparation_bounded():
    owner = owners()
    source = RetainedObject.from_value({**FINDING, 'history': repeated_history(16)})
    prepared = owner.prepare_finding(SRT, RetainedObject.from_value(FINDING))
    empty = RetainedObject.from_value({})
    record = ConsumedRuling('terminal', prepared.check, prepared.materials, empty,
        empty, 'original prompt', 'original answer', prepared.check, empty)
    with shell.ruling_capture(None, prepared) as capture:
        capture.records.append(record)
        actual, peak = measured(lambda: shell.attach_consumed_rulings(source, owner, SRT))
    assert peak < 20 * 1024**2, peak
    next_prepared = owner.prepare_finding(SRT, source, origin_check=prepared.check)
    expected = source.read_view()
    expected.update(next_finding_materials={'check': next_prepared.check.read_view(),
        'materials': next_prepared.materials.read_view()}, consumed_rulings=[record.document().read_view()],
        consumed_ruling=record.document().read_view())
    assert actual == RetainedObject.from_value(expected)
    assert actual.sha256 == RetainedObject.from_value(expected).sha256
    assert 'consumed_rulings' not in source.read_view()
    public = actual.value()
    public['history']['first']['added'] = True
    assert 'added' not in public['history']['later']
    assert 'added' not in source.read_view()['history']['first']
