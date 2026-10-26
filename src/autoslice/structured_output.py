"""Transport-only output contracts; never part of semantic/cache identity.

Provider constraints supplement the consumer's existing validator. They grant
no evidence, edit, retry-plan, or release authority.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
import hashlib
import json

_contract = ContextVar('response_contract', default=None)


class FormatRetryExhausted(ValueError):
    """Two returned invalid answers; never rotate keys/models to repair them."""
    def __init__(self, reason_code, completion):
        self.reason_code = reason_code
        self.completion = completion
        super().__init__(reason_code)


def output_contract(response_schema=None, *, json_mode=True, name='autoslice_response'):
    return {'json_mode': bool(json_mode or response_schema is not None),
            'name': name, 'schema': deepcopy(response_schema)}


def current_contract(response_schema=None, *, json_mode=None):
    if response_schema is not None:
        return output_contract(response_schema, json_mode=json_mode)
    if json_mode is False:
        return output_contract(json_mode=False)
    return _contract.get() or output_contract(json_mode=bool(json_mode))


@contextmanager
def response_contract(response_schema=None, *, json_mode=True, name='autoslice_response', validate=None):
    value = output_contract(response_schema, json_mode=json_mode, name=name)
    value['validate'] = validate
    token = _contract.set(value)
    try:
        yield value
    finally:
        _contract.reset(token)


def contract_receipt(contract=None, *, prompt=None):
    value = contract or current_contract()
    schema = value.get('schema')
    skipped = (prompt is not None and value['json_mode'] and schema is None
               and 'json' not in prompt.lower())
    return {'json_mode': value['json_mode'] and not skipped, 'schema_name': value['name'] if schema else None,
            'schema_sha256': hashlib.sha256(json.dumps(schema, sort_keys=True,
                separators=(',', ':')).encode()).hexdigest() if schema else None,
            **({'reason_code': 'JSON_MODE_SKIPPED_NO_JSON_WORD'} if skipped else {})}


def strict_openai_schema(schema):
    """OpenAI strict objects require every property; optional ones are nullable."""
    result = deepcopy(schema)
    if result.get('type') == 'object':
        properties = result.get('properties', {})
        required = result.get('required', [])
        result['properties'] = {key: strict_openai_schema(value) if key in required else
            {'anyOf': [strict_openai_schema(value), {'type': 'null'}]}
            for key, value in properties.items()}
        result['required'] = list(properties)
        result['additionalProperties'] = False
    if isinstance(result.get('items'), dict):
        result['items'] = strict_openai_schema(result['items'])
    for key in ('anyOf', 'oneOf'):
        if key in result:
            result[key] = [strict_openai_schema(item) for item in result[key]]
    return result


def guard_json_object(response_format, prompt):
    """Do not send OpenAI JSON-object mode without the required prompt word."""
    if response_format and response_format.get('type') == 'json_object' and 'json' not in prompt.lower():
        return None
    return response_format


def openai_format(contract=None, *, prompt=None):
    value = contract or current_contract()
    if value['schema'] is not None:
        return {'type': 'json_schema', 'name': value['name'],
                'schema': strict_openai_schema(value['schema']), 'strict': True}
    result = {'type': 'json_object'} if value['json_mode'] else None
    return guard_json_object(result, prompt) if prompt is not None else result


def gemini_format(response_schema=None, *, json_mode=True):
    value = current_contract(response_schema, json_mode=json_mode)
    if not value['json_mode']:
        return {}
    fields = {'responseMimeType': 'application/json'}
    if value['schema'] is not None:
        # JSON Schema, including nullable unions; not the smaller OpenAPI Schema.
        fields['responseJsonSchema'] = deepcopy(value['schema'])
    return fields


def agy_structured_args(schema_path):
    return ['--output-format', 'stream-json', '--json-schema', str(schema_path)]


def parse_agy_stream(raw):
    """Extract exactly one successful native result, never assistant prose."""
    events = [json.loads(line) for line in raw.splitlines() if line.strip()]
    results = []
    for event in events:
        if not isinstance(event, Mapping):
            raise ValueError('AGY_STRUCTURED_EVENT_INVALID')
        if event.get('error') or event.get('is_error') or event.get('type') in {'error', 'failed'}:
            raise ValueError('AGY_STRUCTURED_ERROR')
        result = event.get('result')
        if isinstance(result, Mapping):
            if result.get('status') != 'SUCCESS' or result.get('error') or result.get('is_error'):
                raise ValueError('AGY_STRUCTURED_RESULT_FAILED')
            payload = result.get('structured_output')
            if not isinstance(payload, Mapping):
                raise ValueError('AGY_STRUCTURED_RESULT_PAYLOAD_INVALID')
            results.append(payload)
    if len(results) != 1:
        raise ValueError('AGY_STRUCTURED_RESULT_COUNT_INVALID')
    return json.dumps(dict(results[0]), ensure_ascii=False)


def format_retry_call(prompt: str, send: Callable[[str], str], *, validate=None, record=None,
                      return_invalid=False, exhausted_error=False):
    """One retry with the identical prompt; network failures never replay.

    Run inside the owning call/observation recorder so this is two physical
    attempts of one observation, not a second acoustic witness.
    """
    validator = validate or current_contract().get('validate')
    from .llm_client import LlmJsonParseError
    error = ''
    if validator is not None and _contract.get() is not None:
        _contract.get()['format_retry_handled'] = True
    for attempt in range(2):
        try:
            raw = send(prompt)
        except BaseException:
            if record:
                record({'attempt': attempt + 1, 'status': 'OUTCOME_UNKNOWN', **contract_receipt()})
            raise
        try:
            if validator is not None:
                validator(raw)
        except (ValueError, TypeError, KeyError, LlmJsonParseError) as exc:
            if current_contract().get('parse_retry_only') and not isinstance(exc, LlmJsonParseError):
                raise  # Preserve the caller's typed-parse-only retry contract.
            error = getattr(exc, 'reason_code', None) or type(exc).__name__
            if isinstance(exc, ValueError) and str(exc).replace('_', '').isalnum():
                error = str(exc)[:160]
            if record:
                record({'attempt': attempt + 1, 'status': 'FORMAT_REJECTED', 'error': error,
                        'completion_sha256': hashlib.sha256(raw.encode()).hexdigest(),
                        **contract_receipt()})
            if attempt == 1:
                if exhausted_error:
                    raise FormatRetryExhausted(error, raw) from None
                if return_invalid:
                    return raw  # Existing consumer records and rejects it under its original gate.
                raise
            continue
        if record:
            record({'attempt': attempt + 1, 'status': 'RETURNED', **contract_receipt()})
        return raw


def call_json(call, prompt, *, response_schema=None, validate=None, name='autoslice_response', reuse=True):
    # Structured judgments share the raw-answer generator by default. New
    # consumers need no cache registration. Creative generation opts out;
    # provider/schema constraints and local validators never authorize reuse.
    from .completed_review_checkpoint import ruling_response, response_in_progress, ruling_cache_allowed
    identity = getattr(call, 'cpa_cache_identity', None)
    if reuse and identity and ruling_cache_allowed() and not response_in_progress():
        from pathlib import Path
        raw, _ = ruling_response(stage='structured-ruling', prompt=prompt, llm_call=call,
            inputs={'material_binding': getattr(call, 'completed_review_binding', None)},
            parser_path=Path(__file__), response_schema=response_schema, name=name)
        if validate is not None:
            validate(raw)
        return raw
    with response_contract(response_schema, name=name, validate=validate):
        return call(prompt)
