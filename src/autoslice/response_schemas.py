"""Structural projections of existing consumer validators (not decision rules)."""
from copy import deepcopy


def obj(properties, required=None, *, closed=False):
    value = {'type': 'object', 'properties': properties,
             'required': list(properties) if required is None else required}
    if closed:
        value['additionalProperties'] = False
    return value


S = {'type': 'string'}
B = {'type': 'boolean'}
INTEGER = {'type': 'integer'}
NS = {'type': ['string', 'null']}


def array(items):
    return {'type': 'array', 'items': deepcopy(items)}


def enum(*values):
    return {'type': 'string', 'enum': list(values)}


def hearing_schema():
    from .argon_escalation_contract import HEARING_SCHEMA
    return deepcopy(obj({'schema_version': enum(HEARING_SCHEMA), 'binding_sha256': S,
        'target_window_ms': array(INTEGER), 'context_window_ms': array(INTEGER),
        'status': enum('OBSERVED', 'UNCERTAIN', 'NO_SPEECH', 'ERROR'),
        'transcript': NS, 'uncertainties': array(obj({'start_ms': INTEGER, 'end_ms': INTEGER, 'reason': S}, closed=True)),
        'speech_role': enum('spontaneous', 'reading_chat', 'singing', 'unknown'), 'reason': NS}, closed=True))


def hearing_prompt_format():
    """Render the hearing shape; preserve frozen semantic request bytes.

    The prose below explains evidence semantics. Field names, nested object
    keys and allowed role values come only from hearing_schema, just as they
    do for the provider and the local structural checks.
    """
    schema = hearing_schema()
    fields = schema['properties']
    roles = fields['speech_role']['enum']
    status_meanings = {
        'OBSERVED': 'for a nonempty target transcript with no unresolved spans',
        'UNCERTAIN': 'for unclear target words (keep partial transcript or null)',
        'NO_SPEECH': 'if no target speech (transcript null)',
        'ERROR': 'for unavailable audio (transcript null)',
    }
    statuses = '; '.join(value + ' ' + status_meanings[value] for value in fields['status']['enum'])
    return (
        'Return one JSON object, no markdown. Exact keys: '
        + ', '.join(schema['required']) + '. '
        + f"schema_version must be {fields['schema_version']['enum'][0]}. "
        'Copy the three binding markers below exactly. '
        + 'status: ' + statuses + '. uncertainties is '
        + _hearing_prompt_type(fields['uncertainties'])
        + ' within the target; nonempty for UNCERTAIN, otherwise empty. '
        + 'speech_role: ' + ', '.join(roles[:-1]) + ', or ' + roles[-1]
        + "; do not infer a person's identity. "
        'reason is null or a nonempty string; required for UNCERTAIN, NO_SPEECH and ERROR. '
    )


def _hearing_prompt_type(schema):
    kind = schema['type']
    if kind == 'array':
        return 'a list of ' + _hearing_prompt_type(schema['items'])
    if kind == 'object':
        return '{' + ','.join(schema['properties']) + '}'
    if isinstance(kind, list):
        return ' or '.join(_hearing_prompt_type({**schema, 'type': item}) for item in kind)
    return 'an integer' if kind == 'integer' else 'a ' + kind


def schema_value_matches(value, schema):
    """Strict structural check for the JSON Schema vocabulary used by hearing.

    Cross-field evidence/clock rules stay with the consumer. In particular,
    JSON booleans and floating-point values never count as integer clocks.
    No coercion or malformed-response rescue takes place here.
    """
    if set(schema) - {'type', 'enum', 'properties', 'required', 'additionalProperties', 'items'}:
        raise ValueError('UNSUPPORTED_HEARING_SCHEMA_KEYWORD')
    kind = schema['type']
    if isinstance(kind, list):
        return any(schema_value_matches(value, {**schema, 'type': item}) for item in kind)
    if 'enum' in schema and value not in schema['enum']:
        return False
    if kind == 'object':
        fields = schema['properties']
        return (isinstance(value, dict) and set(schema['required']) <= set(value)
                and (schema.get('additionalProperties', True) or set(value) <= set(fields))
                and all(schema_value_matches(value[key], field)
                        for key, field in fields.items() if key in value))
    if kind == 'array':
        return isinstance(value, list) and all(schema_value_matches(item, schema['items'])
                                             for item in value)
    if kind == 'string':
        return isinstance(value, str)
    if kind == 'integer':
        return type(value) is int
    if kind == 'null':
        return value is None
    raise ValueError('UNSUPPORTED_HEARING_SCHEMA_TYPE')


def assisted_audio_schema():
    event = obj({'kind': enum('environment_sound', 'nonsemantic_vocalization'),
        'status': enum('OBSERVED', 'UNCERTAIN'), 'start_ms': INTEGER, 'end_ms': INTEGER, 'description': S})
    return obj({'schema_version': enum('context-assisted-audio-response.v1'),
        'status': enum('OBSERVED', 'UNCERTAIN'), 'target_audible': B,
        'heard': S, 'full_transcript': S,
        'inferred': array(obj({'text': S, 'source_refs': array(S)})),
        'uncertain': array(obj({'question': S, 'audio_basis': S})),
        'segments': array(obj({'segment': enum('complete_block'), 'start_ms': INTEGER, 'end_ms': INTEGER, 'transcript': S})),
        'audio_observations': array({'anyOf': [S, event]})})


def proposal_schema(*, convergence=False):
    fields = {'needs_audio': B, 'reason': S}
    if convergence:
        fields.update(decision=enum('KEEP_EXISTING', 'REPLACE_WITH_EXACT_TEXT', 'UNRESOLVED'), replacement_text=NS)
    else:
        fields.update(status=enum('PROPOSED', 'CURRENT_SUPPORTED', 'UNRESOLVED'), proposed_cue=NS, topic_summary=S)
    required = list(fields)
    # TOPIC_PHONEME_POLICY is appended by both ordinary proposal builders.
    # These are conditional evidence fields, not a new decision or authority.
    fields.update(proposal_basis=enum('topic_homophone', 'audio_transcript'),
        topic_support=array(S))
    return obj(fields, required=required)


def closed_set_schema(choices):
    choice = enum(*sorted(choices))
    alignment = obj({'observation_id': S, 'span': array(INTEGER), 'quote': S})
    question = obj({'codepoint_span': array(INTEGER), 'reason': S, 'observation_ids': array(S),
        'observation_alignments': array(alignment)}, required=['codepoint_span', 'reason', 'observation_ids'])
    span = obj({'binding_sha256': S, 'request_sha256': S, 'context_basis': S,
        'all_audio_questions_located': B, 'target_needs_audio': B, 'alignments': array(alignment),
        'audio_questions': array(question), 'status': S, 'unlocatable_reason': S},
        required=['binding_sha256', 'request_sha256', 'all_audio_questions_located'])
    return obj({'choice': choice, 'ranking': array(obj({'choice': choice, 'p': {'type': 'number'}})),
        'needs_audio': B, 'reason': S, 'candidate_id': NS,
        'current_utterance_supported': B, 'current_utterance_support_reason': S,
        'span_review': span,
        'registered_name_decisions': array(obj({'name': S,
            'usage': enum('LITERAL_NONENTITY', 'DIFFERENT_REFERENT'), 'replacement': S,
            'context_quote': S, 'reason': S})),
        'argon_escalation': obj({'reading_chat': B, 'lyrics_reference_conflict': B, 'reason': S})},
        required=['choice', 'needs_audio', 'reason'])


def terminal_schema(policy=None, *, request=None):
    """Derive citations from this request; retain policy-only legacy callers."""
    from .semantic_terminal_adjudication import KEEP_ROUTES, offered_keep_contract, terminal_materials
    if request is None and policy is not None and 'terminal_policy' in policy:
        request = policy
    evidence_ref = S
    keep_routes = KEEP_ROUTES
    if request is not None:
        policy = request['terminal_policy']
        materials, refs = terminal_materials(request)
        evidence_ref = enum(*list(refs))
        # The schema offers exactly the routes rendered in the same materials.
        keep_routes = offered_keep_contract(materials)[0]
    fields = {'choice': enum(*policy['terminal_choices']), 'grade': enum(*policy['grades']),
        'reason': S, 'evidence_refs': array(evidence_ref), 'evidence_decisive': B, 'context_can_decide': B,
        'keep_route': {'type': ['string', 'null'], 'enum': [*keep_routes, None]},
        'current_support_observation_refs': array(S),
        'comparison_text': S, 'name_only': B, 'written_meaning_error': S, 'best_guess': S,
        'name_span': array(INTEGER), 'name_source_ref': S, 'name_source_text': S, 'sourced_name': S,
        'meaning_equivalence': obj({'status': enum('EQUIVALENT'), 'current_meaning': S,
            'alternative_meaning': S, 'shared_meaning': S, 'uncertain_detail': S,
            'context_basis': S, 'evidence_cue_ids': array(INTEGER)}),
        'status': S, 'mode': S,
        'reliable_fragments': array(obj({'text': S, 'basis': S})), 'meaning_basis': S,
        'literal_uncertainty': S, 'no_new_facts': B}
    from .answer_contract import TERMINAL
    fields['contract_version'] = enum(TERMINAL)
    if request is not None:
        from .semantic_terminal_adjudication import grade_a_witness_contract
        import json
        fields['grade']['description'] = (
            'A requires evidence_refs to cite the minimum number of distinct witnesses defined by '
            + json.dumps(grade_a_witness_contract(request), sort_keys=True)
            + '. independence_fields are read from raw_observation. Repeated observations from '
            'the same provider/model count once; Argon is not a blind_range. '
            'If the selected citations do not meet this threshold, use another eligible grade.')
    from src.autoslice.editorial_edit_contract import edit_schema
    fields.update(edit_schema(request.get('current_cue') if request is not None else None))
    fields.update(gift_thanks_confirmed=B, name_sources_exhausted=B, screen_index=INTEGER,
        event_index=INTEGER, event_field=enum('text', 'sender'), referent_basis=S, alternatives_considered=S,
        nonsemantic_vocalization_confirmed=B, spoken_or_lyric_content_absent=B, nonsemantic_audio_basis=S)
    return obj(fields, required=['choice', 'grade', 'reason', 'evidence_refs'])
