"""Question-local dependencies must not require unrelated result publication."""
from copy import deepcopy
import json
import threading

import pytest

from src.autoslice import final_review_auditor as auditor
from src.autoslice.exact_review_parallel import execute_exact_review_groups
from src.autoslice.final_review_provider_budget import (
    _adjudication_input_fingerprint,
    relevant_transcript_handoffs,
)
from tests import test_exact_review_parallel_integration as integration
from tests import test_exact_review_parallel_rework2 as shaped


def test_repeated_group_does_not_wait_for_unrelated_prefix():
    successor_started = threading.Event()
    published = []

    def prepare(index):
        def run():
            if index == 0:
                assert successor_started.wait(2), 'unrelated prefix blocked group successor'
            if index == 2:
                assert 0 not in published
                successor_started.set()
            return index
        return run

    assert execute_exact_review_groups([[0], [1, 2], [3]], prepare=prepare,
        parallelism=3, collect_result=lambda index, _result: published.append(index)) == list(range(4))
    assert published == list(range(4))


def _request(srt, finding, context=integration.CONTEXT, offset=0):
    scaffold = auditor._candidate_blind_missing_proposal_scaffold(srt, finding)
    return auditor.build_context_adjudication_request(srt, scaffold or finding,
        clip_context=context, source_media_timeline_offset_ms=offset)


def test_fingerprint_scopes_accumulated_pool_and_retains_explicit_inputs():
    srt, rows = integration.fixture_agenda(2)
    own = {'input_request': _request(srt, rows[0]), 'receipt_sha256': 'own'}
    other = {'input_request': _request(srt, rows[1]), 'receipt_sha256': 'other'}
    original = deepcopy([own, other])

    def identity(row, pool):
        selected = relevant_transcript_handoffs(row, pool, request=_request(srt, row))
        return _adjudication_input_fingerprint(srt,
            {**row, '_prior_exact_source_transcript_handoffs': selected},
            clip_context=integration.CONTEXT, source_media_timeline_offset_ms=0,
            judge_llm_call=None)

    assert identity(rows[0], []) == identity(rows[0], [other])
    assert identity(rows[0], [own]) == identity(rows[0], [other, own])
    assert identity(rows[0], [own]) != identity(rows[0], [])
    changed = {**own, 'receipt_sha256': 'changed-same-question-evidence'}
    assert identity(rows[0], [own]) != identity(rows[0], [changed])
    explicit = {**rows[0], '_prior_exact_source_transcript_handoffs': [other]}
    assert identity(explicit, []) != identity(rows[0], [])
    assert relevant_transcript_handoffs(explicit, [own], request=None) == [other]
    assert [own, other] == original


@pytest.mark.parametrize('drift', ['cross_clock', 'source', 'current', 'target', 'proposal'])
def test_dependency_selection_uses_g6_without_another_question_contract(drift):
    srt, rows = integration.fixture_agenda(2)
    request = _request(srt, rows[0])
    old = deepcopy(request)
    if drift == 'cross_clock':
        old['matched_start_ms'] -= 1000
        old['matched_end_ms'] -= 1000
        old['source_media_timeline_offset_ms'] += 1000
    elif drift == 'source':
        old['clip_context_binding']['source_media_sha256s'] = ['b' * 64]
    elif drift == 'current':
        old['current_cue'] += '改'
    elif drift == 'target':
        old['matched_end_ms'] += 1
    else:
        old['proposed_cue'] += '另候选'
    handoff = {'input_request': old, 'receipt_sha256': drift}
    assert bool(relevant_transcript_handoffs(rows[0], [handoff], request=request)) == (
        drift in {'cross_clock', 'proposal'})


def test_group_handoff_available_before_unrelated_publication(tmp_path, monkeypatch):
    srt, findings = integration.fixture_agenda(2)
    findings.append({**findings[1], 'suggestion': '第二候选', 'why': 'successor'})
    later_started = threading.Event()
    seen = []
    monkeypatch.setenv('AUTOSLICE_EXACT_REVIEW_PARALLELISM', '8')
    monkeypatch.setattr(auditor, 'valid_exact_source_transcript_handoff',
                        lambda h: isinstance(h.get('input_request'), dict))
    handoff = {'receipt_sha256': 'cue-2', 'input_request': _request(srt, findings[1])}

    def adjudicate(text, finding, **kwargs):
        if finding['cue_index'] == 1:
            assert later_started.wait(2), 'unrelated publication blocked successor'
        elif finding['why'] == 'successor':
            seen.extend(finding.get('_prior_exact_source_transcript_handoffs') or [])
            later_started.set()
        return text, {'status': 'UNCERTAIN', 'repaired': False,
                      'exact_source_transcript_handoff': handoff}

    monkeypatch.setattr(auditor, 'adjudicate_context_finding', adjudicate)
    pending, closed = auditor.adjudicate_exact_release_findings(srt, findings,
        entity_verifier=None, clip_context=integration.CONTEXT)
    assert [row['cue_index'] for row in pending] == [1, 2, 2]
    assert closed == [] and seen == [handoff]


def test_unrelated_handoff_does_not_defeat_actual_budget_replay(monkeypatch):
    srt, rows = integration.fixture_agenda(2)
    calls = []
    # The original input is repeated after another group produced evidence.
    monkeypatch.setattr(auditor, 'coalesce_exact_review_agenda', lambda _srt, rows: list(rows))
    monkeypatch.setattr(auditor, 'valid_exact_source_transcript_handoff', lambda h: True)

    def adjudicate(text, finding, **kwargs):
        calls.append(finding['cue_index'])
        result = {'status': 'UNCERTAIN', 'repaired': False}
        if finding['cue_index'] == 2:
            result['exact_source_transcript_handoff'] = {
                'receipt_sha256': 'unrelated', 'input_request': _request(srt, rows[1])}
        return text, result

    monkeypatch.setattr(auditor, 'adjudicate_context_finding', adjudicate)
    for workers in (1, 8):
        calls.clear()
        monkeypatch.setenv('AUTOSLICE_EXACT_REVIEW_PARALLELISM', str(workers))
        pending, _ = auditor.adjudicate_exact_release_findings(srt,
            [rows[0], rows[1], dict(rows[0])], entity_verifier=None,
            clip_context=integration.CONTEXT)
        assert sorted(calls) == [1, 2]
        assert pending[2]['exact_release_adjudication']['same_input_replay']['provider_call_count'] == 0


@pytest.mark.parametrize('workers', [1, 8])
def test_scoped_empty_pool_reaches_actual_adjudicator(monkeypatch, workers):
    srt, rows = integration.fixture_agenda(2)
    handoff = {'receipt_sha256': 'cue-1', 'input_request': _request(srt, rows[0])}
    seen = []
    monkeypatch.setenv('AUTOSLICE_EXACT_REVIEW_PARALLELISM', str(workers))
    monkeypatch.setattr(auditor, 'valid_exact_source_transcript_handoff', lambda h: True)

    def adjudicate(text, finding, **kwargs):
        seen.append(finding.get('_prior_exact_source_transcript_handoffs') or [])
        return text, {'status': 'UNCERTAIN', 'repaired': False,
                      'exact_source_transcript_handoff': handoff}

    monkeypatch.setattr(auditor, 'adjudicate_context_finding', adjudicate)
    auditor.adjudicate_exact_release_findings(srt, rows,
        entity_verifier=None, clip_context=integration.CONTEXT)
    assert seen == [[], []]


@pytest.mark.parametrize('seed', [20261022, 13, 91])
def test_paired_production_shape_benchmark_and_decision_equivalence(tmp_path, monkeypatch, seed):
    source = tmp_path / 'source.mp4'
    source.write_bytes(b'fixed offline media')
    serial_oracle = integration.serial_oracle.__wrapped__()
    serial = shaped.shaped_run(monkeypatch, serial_oracle, tmp_path/'serial', source,
        workers=1, seed=seed, paired=True)
    parallel = shaped.shaped_run(monkeypatch, auditor, tmp_path/'parallel', source,
        workers=8, seed=seed, paired=True)
    assert shaped.comparable(serial) == shaped.comparable(parallel)
    assert len(serial['calls']) == 88  # 20 planning + 38 audio + 30 decision CPA
    speedup = serial['wall_s']/parallel['wall_s']
    assert parallel['peak'] >= 6
    assert speedup >= 3
    print('PAIRED_PREFIX_BENCHMARK=' + json.dumps({
        'items': 20, 'groups': 10, 'members_per_group': 2,
        'ordering': 'first members then second members', 'seed': seed,
        'delay_range_seconds': [5, 60], 'scale': .001,
        'serial_s': serial['wall_s'], 'parallel_8_s': parallel['wall_s'],
        'speedup': speedup, 'peak_provider_calls': parallel['peak'],
        'peak_audio_calls': parallel['peak_audio'],
        'decision_release_budget_equal': True,
        'provider_call_count_each': len(serial['calls'])}))
