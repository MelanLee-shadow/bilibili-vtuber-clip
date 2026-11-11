"""Consumed-ruling construction preserves history, bytes and detached public values."""
from copy import deepcopy
import gc
import hashlib
import json
from pathlib import Path
import tracemalloc

import pytest

from src.autoslice import finding_context as context
from src.autoslice import finding_closure_shell as shell
from src.autoslice.finding_closure_ports import RetainedObject
from src.autoslice.llm_client import CpaCompletion
from tests.test_wpb_finding_context import check, materials


def history(depth):
    value = {'rows': [{'text': 'original answer', 'status': 'UNCERTAIN'}]}
    for _ in range(depth):
        value = {'first': value, 'later': value}
    return value


def fixture(depth, wrapper=None):
    request = check()
    material = materials(request)
    material['contracts']['history'] = history(depth)
    material['materials_sha256'] = context.digest({k:v for k,v in material.items()
        if k not in {'physical_question_sha256', 'materials_sha256'}})
    inputs = {'original_check_request': request, 'finding_materials': material}
    if wrapper:
        inputs = {wrapper: inputs}
    invocation = shell.RulingInvocation('proposal-rebuild', 'original prompt',
        RetainedObject.from_value(inputs), Path(__file__), None, 'recorded')
    return invocation, material, RetainedObject.from_value(request)


def consume(invocation, origin):
    return shell.consumed_ruling(invocation=invocation,
        raw=CpaCompletion('original raw', 'actual-answering-model'),
        identity={'transport':'recorded','models':['nominal-model'],'effort':'high'},
        origin=origin, checkpoint=RetainedObject.from_value({}))


def measured(call):
    gc.collect()
    tracemalloc.start()
    try:
        result = call()
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    return result, peak


@pytest.mark.parametrize('wrapper', [None, 'request', 'material'])
def test_consumed_ruling_does_not_unfold_repeated_history(wrapper):
    invocation, material, origin = fixture(14, wrapper)
    result, peak = measured(lambda: consume(invocation, origin))
    assert peak < 8 * 1024**2, peak
    assert result.inputs == invocation.inputs
    assert result.check == origin
    assert result.model_identity.read_view()['models'] == ['actual-answering-model']
    view = result.materials.read_view()
    assert context.digest(view['contracts']) == context.digest(material['contracts'])
    assert view['model_identity']['models'] == ['actual-answering-model']


def test_consumed_document_writer_does_not_unfold_the_same_history_again():
    invocation, _, origin = fixture(14)
    record = consume(invocation, origin)
    document, peak = measured(record.document)
    assert peak < 8 * 1024**2, peak
    assert document.read_view()['inputs'] == invocation.inputs.read_view()
    assert document.read_view()['completion_sha256'] == hashlib.sha256(b'original raw').hexdigest()


@pytest.mark.parametrize('depth', [0, 3, 7])
def test_consumed_model_and_original_anchor_have_legacy_canonical_bytes(depth):
    invocation, material, origin = fixture(depth)
    # The origin anchor can differ from a narrower subsequent check; both
    # still refer to the same physical source and window.
    source = origin.value(); source['edit_span_binding']['codepoint_span'] = [0, 7]
    origin = RetainedObject.from_value(source)
    record = consume(invocation, origin)
    expected = deepcopy(material)
    expected['physical_question']['codepoint_span'] = [0, 7]
    expected['physical_question_sha256'] = context.digest(expected['physical_question'])
    expected['model_identity'] = {'transport':'recorded','models':['actual-answering-model'],'effort':'high'}
    expected['materials_sha256'] = context.digest({k:v for k,v in expected.items()
        if k not in {'physical_question_sha256', 'materials_sha256'}})
    legacy = json.dumps(expected, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)
    assert record.materials.json_text == legacy
    assert record.materials.sha256 == hashlib.sha256(legacy.encode()).hexdigest()
    assert record.document().value()['materials'] == expected


@pytest.mark.parametrize('operation', ['validate', 'model', 'origin'])
def test_public_material_results_keep_independent_mutable_occurrences(operation):
    _, value, origin = fixture(2)
    if operation == 'validate':
        result = context.validate_materials(value)
    elif operation == 'model':
        result = context.materials_for_model(value, {'models':['actual']})
    else:
        result = context.materials_for_origin(value, origin.read_view())
    result['contracts']['history']['first']['new'] = 1
    assert 'new' not in result['contracts']['history']['later']
    assert 'new' not in value['contracts']['history']['first']


@pytest.mark.parametrize('damage', ['material_hash', 'source', 'window'])
def test_consumed_material_rejects_original_binding_drift(damage):
    invocation, _, origin = fixture(1)
    if damage == 'material_hash':
        inputs = invocation.inputs.value()
        inputs['finding_materials']['materials_sha256'] = '0' * 64
        invocation = shell.RulingInvocation(invocation.stage, invocation.prompt,
            RetainedObject.from_value(inputs), Path(__file__), None, 'recorded')
    else:
        value = origin.value()
        if damage == 'source':
            value['clip_context_binding']['source_media_sha256s'] = ['b' * 64]
        else:
            value['matched_end_ms'] += 1
        origin = RetainedObject.from_value(value)
    with pytest.raises(ValueError):
        consume(invocation, origin)
