"""Executed word-judge coverage inside the ordinary persisted finding audit.

This is not a second runtime ledger: the existing review/authority writer owns
these receipts and its cold validator compares original executions to outcomes.
"""

from .review_errors import ContractRejection
from contextvars import ContextVar
from copy import deepcopy
from functools import wraps
from typing import Mapping

from src.autoslice.semantic_retry_progress import digest
from .review_evidence_store import cold_input

_active = ContextVar('ordinary_span_judgments', default=None)
SCHEMA = 'executed-span-judgment-history.v1'


def expected_execution(request, witness, raw, prompt_sha256):
    """Capture an actual transport/cache reply before parsing/branch selection."""
    from src.autoslice.span_phoneme_resolution import text_sha
    if not isinstance(raw, str):
        return None
    # A real insertion/whole-utterance reply is still an executed judgment.
    # Its scope outcome is NOT_APPLICABLE, never a fabricated replacement span.
    row = {'request_sha256': request['request_sha256'],
           'witness_sha256': digest(witness), 'prompt_sha256': prompt_sha256,
           'completion_sha256': text_sha(raw), 'raw_completion': raw}
    row['execution_id'] = digest(row)
    current = _active.get()
    if current is not None:
        current['expected'][row['execution_id']] = deepcopy(row)
    return row


def expected_legacy_cache_execution(request, witness, parsed):
    """Account for a cache return whose original wire text was never saved.

    It is explicitly not a raw model observation. Other legacy authority keeps
    its own contract; this outcome can never authorize the span repair route.
    """
    row = {'request_sha256': request['request_sha256'],
           'witness_sha256': digest(witness), 'prompt_sha256': parsed.get('prompt_sha256'),
           'completion_sha256': parsed.get('completion_sha256'), 'raw_completion': None,
           'raw_status': 'LEGACY_RAW_UNAVAILABLE', 'parsed_reply': deepcopy(dict(parsed)),
           'parsed_reply_sha256': digest(parsed)}
    row['execution_id'] = digest(row)
    current = _active.get()
    if current is not None:
        current['expected'][row['execution_id']] = deepcopy(row)
    return row


def complete_execution(expected, judge):
    if expected is not None and _active.get() is not None:
        _active.get()['outcomes'][expected['execution_id']] = deepcopy(judge)


def snapshot_context_judgments(audit):
    """Expose actual completed returns to recovery before the outer return.

    Copies prevent the active collector from becoming its own nested child.
    This is the same review/typed history, not a separate ledger.
    """
    state = _active.get()
    if not state or not state['expected']:
        return dict(audit)
    history = {'schema_version': SCHEMA,
               'expected_executions': deepcopy(list(state['expected'].values())),
               'outcomes': deepcopy(state['outcomes']), 'mutation_authorized': False}
    history['history_sha256'] = digest(history)
    return {**audit, 'executed_span_judgments_required': True,
            'executed_span_judgments': history}


def capture_context_judgments(call):
    """Cover all ordinary branches, including nested/rebound early returns."""
    @wraps(call)
    def wrapped(*args, **kwargs):
        parent = _active.get()
        state = {'expected': {}, 'outcomes': {}}
        token = _active.set(state)
        try:
            output, audit = call(*args, **kwargs)
            return output, snapshot_context_judgments(audit)
        finally:
            _active.reset(token)
            if parent is not None:
                parent['expected'].update(state['expected'])
                parent['outcomes'].update(state['outcomes'])
    return wrapped


@cold_input
def validate_history(node):
    """Require coverage of expected original replies, not only surviving judges."""
    from src.autoslice.span_phoneme_resolution import text_sha
    history = node.get('executed_span_judgments')
    if node.get('executed_span_judgments_required') is True and history is None:
        raise ContractRejection('SPAN_EXECUTED_HISTORY_REQUIRED')
    if history is None:
        return
    if (not isinstance(history, Mapping) or history.get('schema_version') != SCHEMA
            or history.get('mutation_authorized') is not False
            or history.get('history_sha256') != digest({k: v for k, v in history.items() if k != 'history_sha256'})):
        raise ContractRejection('SPAN_EXECUTED_HISTORY_DRIFT')
    expected, outcomes = history.get('expected_executions'), history.get('outcomes')
    if not isinstance(expected, list) or not expected or not isinstance(outcomes, Mapping):
        raise ContractRejection('SPAN_EXECUTED_HISTORY_INVALID')
    ids = [r.get('execution_id') for r in expected if isinstance(r, Mapping)]
    if len(ids) != len(expected) or len(set(ids)) != len(ids) or set(ids) != set(outcomes):
        raise ContractRejection('SPAN_EXECUTED_OUTCOME_COVERAGE_MISMATCH')
    for row in expected:
        if row['execution_id'] != digest({k: v for k, v in row.items() if k != 'execution_id'}):
            raise ContractRejection('SPAN_EXECUTED_INPUT_DRIFT')
        judge = outcomes[row['execution_id']]
        co = judge.get('span_compilation') or {}
        missing_raw = row.get('raw_status') == 'LEGACY_RAW_UNAVAILABLE'
        if missing_raw:
            raw_valid = (row['raw_completion'] is None
                and digest(row.get('parsed_reply')) == row.get('parsed_reply_sha256')
                and judge.get('legacy_cached_reply') == row['parsed_reply']
                and judge.get('raw_reply_status') == 'LEGACY_RAW_UNAVAILABLE'
                and co.get('blocked_reason') == 'SPAN_LEGACY_RAW_UNAVAILABLE'
                and co.get('resolution') is None)
        else:
            raw_valid = (isinstance(row['raw_completion'], str)
                and text_sha(row['raw_completion']) == row['completion_sha256']
                and judge.get('span_review_raw_completion') == row['raw_completion'])
        if (not raw_valid
                or judge.get('completion_sha256') != row['completion_sha256']
                or judge.get('prompt_sha256') != row['prompt_sha256']
                or co.get('request_sha256') != row['request_sha256']
                or co.get('completion_sha256') != row['completion_sha256']
                or digest(co.get('witness')) != row['witness_sha256']):
            raise ContractRejection('SPAN_EXECUTED_OUTCOME_IDENTITY_DRIFT')
