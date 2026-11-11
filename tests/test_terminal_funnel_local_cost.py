"""Historical evidence is a shared graph, with ordered occurrence semantics."""
from copy import deepcopy
import json

import pytest

from src.autoslice import exact_final_terminal_funnel as funnel
from src.autoslice.final_review_auditor import build_context_adjudication_request


SRT = '1\n00:00:01,000 --> 00:00:02,000\n我喜欢她啊\n'
CONTEXT = {'candidate_id': 'history-graph', 'pieces': [
    {'start_ms': 0, 'end_ms': 3000, 'source_media_sha256': 'a' * 64}]}
FINDING = {'cue_index': 1, 'suspect': '啊', 'suggestion': '呀',
           'proposed_full_cue': '我喜欢她呀', 'repair_class': 'phonetic', 'why': '尾音'}


def request():
    value = build_context_adjudication_request(SRT, FINDING, clip_context=CONTEXT,
        source_media_timeline_offset_ms=0, require_complete_utterance_support=True)
    value['offline_history_marker'] = 'retained-original'
    value.pop('request_sha256', None)
    value['request_sha256'] = funnel.digest(value)
    return value


def check(tree):
    return funnel.terminal_check(SRT, deepcopy(FINDING), clip_context=CONTEXT,
        offset_ms=0, historical_findings=[{'exact_release_adjudication': tree}])


def test_shared_historical_request_is_hashed_once_per_terminal_check(monkeypatch):
    original = request()
    unsigned_hashes = []
    real_digest = funnel.digest

    def counted(value):
        if (isinstance(value, dict) and value.get('offline_history_marker')
                and 'request_sha256' not in value):
            unsigned_hashes.append(value)
        return real_digest(value)

    monkeypatch.setattr(funnel, 'digest', counted)
    result = check([{'request': original}] * 32)
    assert result['current_cue'] == '我喜欢她啊'
    assert result['prior_acoustic_observations'] == []
    assert len(unsigned_hashes) == 1


def test_shared_irrelevant_subtrees_are_scanned_once_without_losing_later_owner():
    class Counted(dict):
        scans = 0

        def values(self):
            self.scans += 1
            return super().values()

    irrelevant = Counted({'tokens': [{'text': 'historical context'}] * 20})
    owner = {'consumer_check_request': request(),
             'argon_escalation': {'status': 'UNCERTAIN', 'reason': 'last owner'}}
    result = check({'prefix': [irrelevant] * 64, 'later': owner})
    assert result['target_argon_state']['owner_audit']['reason'] == 'last owner'
    # One public cold-input scan, then one owner-index scan; neither expands
    # the shared subtree once per occurrence.
    assert irrelevant.scans <= 2


@pytest.mark.parametrize('field', ['request', 'consumer_check_request'])
def test_shared_history_retains_first_owner_and_duplicate_occurrence_order(field):
    original = request()
    first = {field: original, 'argon_escalation': {'status': 'UNCERTAIN', 'reason': 'first'}}
    second = {field: original, 'argon_escalation': {'status': 'NO_SPEECH', 'reason': 'second'}}
    graph = [first, {'nested': (second, first)}, second]
    shared = check(graph)
    inline = check(json.loads(json.dumps(graph, ensure_ascii=False)))
    assert json.dumps(shared, ensure_ascii=False) == json.dumps(inline, ensure_ascii=False)
    assert shared['target_argon_state']['owner_audit']['reason'] == 'first'
    shared['target_argon_state']['owner_audit']['reason'] = 'caller edit'
    assert first['argon_escalation']['reason'] == 'first'


@pytest.mark.parametrize('drift', ['checksum', 'source', 'unknown_owner'])
def test_repeated_terminal_checks_revalidate_mutated_history_and_keep_guards(drift):
    original = request()
    owner = {'request': original, 'argon_escalation': {'status': 'UNCERTAIN'}}
    assert check([owner, owner])['target_argon_state']['state'] == 'TERMINAL'
    if drift == 'checksum':
        original['request_sha256'] = 'f' * 64
    elif drift == 'source':
        original['clip_context_binding']['source_media_sha256s'] = ['f' * 64]
        original.pop('request_sha256')
        original['request_sha256'] = funnel.digest(original)
    else:
        owner['argon_escalation']['status'] = 'UNKNOWN'
    result = check([owner, owner])
    assert 'target_argon_state' not in result
    assert result['prior_acoustic_observations'] == []
