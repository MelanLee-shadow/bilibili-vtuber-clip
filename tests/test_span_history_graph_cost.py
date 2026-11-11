"""Ordinary generated judgments retain their checks after shared cold storage."""
from copy import deepcopy
import json

import pytest

from src.autoslice import review_evidence_store as store
from src.autoslice import span_judgment_history as history
from src.autoslice.review_errors import ContractRejection
from src.autoslice.span_phoneme_resolution import validate_persisted_judge_scopes
from tests.test_c_subtitle_route_contracts import hearing


def published(tmp_path, choice, layout):
    (_, _, audit), _ = hearing(choice, False)
    node = audit
    for _ in range(6):
        node = {'left': node, 'right': node} if layout == 'mapping' else [node, node]
    path = tmp_path / 'history.json'
    store.persist_document(path, {'terminal_review': node})
    return path, audit


@pytest.mark.parametrize('choice,layout', [('CURRENT', 'mapping'), ('PROPOSED', 'list')])
def test_ordinary_writer_cold_reader_checks_each_shared_container_once(tmp_path, monkeypatch, choice, layout):
    path, audit = published(tmp_path, choice, layout)
    cold = store.load_review(path)
    calls = []
    original = history.validate_history

    def counted(node):
        calls.append(id(node))
        return original(node)

    monkeypatch.setattr(history, 'validate_history', counted)
    assert validate_persisted_judge_scopes(cold) == validate_persisted_judge_scopes(audit) == 1
    # A second public invocation must validate again; the optimization is local
    # to one traversal, never a persistent cache of authority or mutable input.
    calls.clear()
    assert validate_persisted_judge_scopes(cold) == 1
    assert len(calls) == len(set(calls))
    first = list(calls)
    calls.clear()
    assert validate_persisted_judge_scopes(cold) == 1
    assert calls == first


@pytest.mark.parametrize('damage', ['missing', 'identity', 'separate_equal_node'])
def test_shared_history_cannot_hide_changed_or_unseen_scope(tmp_path, damage):
    path, audit = published(tmp_path, 'CURRENT', 'mapping')
    cold = store.load_review(path)
    assert validate_persisted_judge_scopes(cold) == 1
    broken = deepcopy(audit)
    if damage == 'missing':
        broken['judge'].pop('span_compilation')
        reason = 'SPAN_COMPILATION_REQUIRED'
    else:
        broken['judge']['span_compilation']['outcome_sha256'] = '0' * 64
        reason = 'SPAN_COMPILATION_IDENTITY_DRIFT'
    # The valid shared graph comes first; a distinct later node still reaches
    # the original guard. Republish and reload so no hand conversion is needed.
    value = {'terminal_review': cold, 'new_member': broken}
    if damage == 'separate_equal_node':
        value['before_new_member'] = deepcopy(audit)
    rejected = tmp_path / 'rejected.json'
    store.persist_document(rejected, value)
    with pytest.raises(ContractRejection, match=reason):
        validate_persisted_judge_scopes(store.load_review(rejected))


def test_inline_equal_copies_and_nonjudgment_input_keep_original_results(tmp_path):
    _, audit = published(tmp_path, 'CURRENT', 'mapping')
    assert validate_persisted_judge_scopes(json.loads(json.dumps([audit, audit]))) == 1
    assert validate_persisted_judge_scopes({'ordinary': [{'text': 'unchanged'}, 1, None]}) == 0


def test_cycle_still_fails_instead_of_skipping_an_active_container():
    cycle = []
    cycle.append(cycle)
    with pytest.raises(RecursionError):
        validate_persisted_judge_scopes(cycle)
