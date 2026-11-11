"""Ordinary ruling identities retain history without copying every occurrence."""
from copy import deepcopy
import gc
import hashlib
import json
import tracemalloc

import pytest

from src.autoslice import acoustic_witness_adjudication as acoustic
from src.autoslice import argon_escalation_integration as argon
from src.autoslice import finding_closure_shell as shell
from src.autoslice import finding_context as context
from src.autoslice.finding_closure_ports import ConsumedRuling, RetainedObject
from tests.test_consumed_ruling_memory import fixture, history
from tests.test_retained_read_view import owners
from tests.test_terminal_funnel_local_cost import SRT, FINDING
from tests.test_wpb_finding_context import check, materials


def measured(call):
    gc.collect()
    tracemalloc.start()
    try:
        result = call()
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    return result, peak


def word_choice(depth):
    request = check()
    request['finding_context'] = context.context_for_check(request)
    request['retained_history'] = history(depth)
    return dict(check_request=request,
        witness={'witness_protocol':'candidate_blind_transcript', 'history':history(depth)},
        context_before='', context_after='', structured_chat_context='',
        model_identity={'models':['actual']}, decision_contract='CURRENT_OR_PROPOSED',
        allowed_choices={'CURRENT','PROPOSED'}, candidates={
            'CURRENT':{'text':request['current_cue']}, 'PROPOSED':{'text':request['proposed_cue']}},
        require_complete_support=True,
        dependencies=acoustic.WordChoiceDependencies('term', (), '', '{}', ''))


def test_ordinary_word_choice_fingerprint_keeps_shared_materials_bounded():
    args = word_choice(13)
    (digest, payload), peak = measured(lambda: acoustic._semantic_fingerprint(**args))
    assert peak < 12 * 1024**2, peak
    assert digest == context.digest(payload)
    assert payload['original_check_request']['current_cue'] == args['check_request']['current_cue']
    bound = payload['finding_materials']
    assert context.validate_materials(bound, _read_only=True) == bound


def test_retained_input_binding_and_effect_identity_do_not_unfold_history():
    invocation, material, origin = fixture(14)
    prepared = owners().prepare_finding(SRT, RetainedObject.from_value(FINDING))
    # Use the same physical question at the origin of this ordinary record.
    from dataclasses import replace
    prepared = replace(prepared, origin_check=origin,
        spec=replace(prepared.spec, origin_check_ref=origin.sha256))
    bound, peak = measured(lambda: shell.bind_ruling_inputs(invocation.inputs, prepared))
    assert peak < 8 * 1024**2, peak
    assert bound.read_view()['finding_materials'] == material
    key, peak = measured(lambda: context.effect_key(material, channel='terminal',
        provider='CPA', model=material['model_identity']))
    assert peak < 8 * 1024**2, peak
    assert key == context.digest({'physical_question_sha256':material['physical_question_sha256'],
        'materials_sha256':material['materials_sha256'], 'channel':'terminal',
        'provider':'CPA', 'model':material['model_identity']})


def argon_request(depth):
    request = check()
    request.update(context_start_ms=12000, context_end_ms=19000,
        base_text_sha256=hashlib.sha256(request['current_cue'].encode()).hexdigest(),
        history=history(depth))
    request['request_sha256'] = context.digest(request)
    return request


def test_argon_request_identity_streams_the_unchanged_signed_history():
    request = argon_request(15)
    _, peak = measured(lambda: argon._check_request(request))
    assert peak < 3 * 1024**2, peak
    broken = {**request, 'request_sha256':'0'*64}
    with pytest.raises(ValueError, match='ARGON_CHECK_REQUEST_HASH_DRIFT'):
        argon._check_request(broken)


@pytest.mark.parametrize('depth', [0, 3, 6])
def test_readonly_and_public_materials_keep_the_same_canonical_content(depth):
    request = check()
    data = {'first':history(depth), 'later':history(depth)}
    kwargs = dict(data=data, contracts={'policy':'unchanged'}, model_identity={'models':['actual']})
    public = context.decision_materials(request, **kwargs)
    internal = context.decision_materials(request, **kwargs, _read_only=True)
    assert internal == public
    canonical = json.dumps(public, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)
    assert RetainedObject.from_value(internal).json_text == canonical
    assert RetainedObject.from_value(internal).sha256 == hashlib.sha256(canonical.encode()).hexdigest()
    assert acoustic._semantic_fingerprint_payload(**word_choice(depth)) == acoustic._semantic_fingerprint(**word_choice(depth))[1]


def test_projection_respects_path_roles_clocks_and_public_mutation_isolation():
    shared = {'clock':'DELIVERY', 'target_start_ms':3, 'target_end_ms':7,
        'rows':[{'offset_ms':5,'text':'original'}], 'request_sha256':'audit address'}
    span = {'schema_version':'span.v1', 'current_span':'x', 'replacement':'y',
        'physical_cue_window_ms':[3,7], 'extra':'kept outside span'}
    data = {'structured_chat_sender_context':shared, 'other':shared,
        'edit_span_binding':span, 'ordinary':span}
    actual = context.model_data(data, source_offset_ms=100)
    assert actual['structured_chat_sender_context'] == {'clock':'SOURCE',
        'target_start_ms':103,'target_end_ms':107,'rows':[{'offset_ms':105,'text':'original'}]}
    assert actual['other'] == {'clock':'DELIVERY','target_start_ms':3,'target_end_ms':7,
        'rows':[{'offset_ms':5,'text':'original'}]}
    assert 'extra' not in actual['edit_span_binding'] and actual['ordinary']['extra']=='kept outside span'
    assert actual == context.model_data(data, source_offset_ms=100, _read_only=True)
    public = context.model_data({'first':shared,'later':shared})
    public['first']['rows'][0]['text'] = 'changed'
    assert public['later']['rows'][0]['text'] == shared['rows'][0]['text'] == 'original'


@pytest.mark.parametrize('field', ['request_sha256', 'included'])
def test_projection_keeps_invalid_original_fields_rejected(field):
    with pytest.raises(ValueError, match='FINDING_JSON_INVALID'):
        context.model_data({field:{'bad':object()}}, _read_only=True)


def test_f2_attachment_preserves_raw_provenance_and_public_copy_contract():
    invocation, _, origin = fixture(13)
    from tests.test_consumed_ruling_memory import consume
    record = consume(invocation, origin)
    value = {'raw_completion':record.raw_completion, 'history':history(13)}
    with shell.ruling_capture(None, None) as capture:
        capture.records.append(record)
        attached, peak = measured(lambda: shell.attach_f2_consumption(value, _read_only=True))
    assert peak < 10 * 1024**2, peak
    assert attached['consumed_ruling']['raw_completion'] == record.raw_completion
    assert RetainedObject.from_value(attached['next_finding']) == RetainedObject.from_value(value)
    assert 'consumed_ruling' not in value
    short = {'raw_completion':record.raw_completion, 'history':history(2)}
    with shell.ruling_capture(None, None):
        public = shell.attach_f2_consumption(short)
    public['history']['first']['changed']=True
    assert 'changed' not in public['history']['later']
    assert 'changed' not in short['history']['first']
