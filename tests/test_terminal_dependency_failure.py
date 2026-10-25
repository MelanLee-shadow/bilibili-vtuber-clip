"""Reviewer failure must not become a new content-binding contract error.

The pulled report identifies the real post-exit candidate. Its complete chat
was not among the pulled small files. This fixture therefore explicitly
reconstructs the dependency outage using the existing captured sister SRT and
its two original cue-13 findings; it is not a claim to replay the full snapshot.
"""
import gzip
import hashlib
import json
from copy import deepcopy
from pathlib import Path

import pytest

from src.autoslice import exact_final_terminal_funnel as funnel

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / 'tests/fixtures/exact_final_funnel/sister-dependency-outage.json'


def recorded_case():
    from tests.continuation_support import srt
    from tests.test_producer_text_pipeline_final_review import _correction_pass
    text = srt()
    rows = [{'cue_index': 13, 'kind': 'context', 'repair_class': 'phonetic',
             'suspect': '合成上下文第13句', 'proposed_full_cue': None,
             'why': reason} for reason in ('synthetic subject ambiguity', 'synthetic word ambiguity')]
    return {'srt': text, 'guard_review': {'correction_pass': _correction_pass(), 'findings': rows},
            'requests': {'synthetic': {'clip_context_binding': {
                'candidate_id': 'synthetic-outage', 'source_media_sha256s': ['sha256:' + 'a' * 64]}}}}, rows


def outage_audit(run, rows, *, discovery_status='AUDITOR_UNAVAILABLE'):
    return {**deepcopy(run['guard_review']), 'findings': deepcopy(rows),
        'unresolved_findings_disclosed': [],
        'status': 'FLAGGED', 'validated_finding_count': len(rows), 'release_gate': 'BLOCK',
        'reason_codes': ['CANDIDATE_PRONOUN_PROVIDER_OR_JSON_UNAVAILABLE'],
        'discovery': {'status': discovery_status, 'detail': 'LLM_COMMAND_FAILED'}}


def test_recorded_cue13_outage_is_call_failure_without_constructing_content_checks(monkeypatch):
    from src.autoslice import final_review_auditor as auditor
    run, rows = recorded_case()
    audit = outage_audit(run, rows)
    before = deepcopy(audit)
    builds = []
    original = auditor.build_context_adjudication_request

    def track(*args, **kwargs):
        builds.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(auditor, 'build_context_adjudication_request', track)
    result = funnel.terminalize_review(run['srt'], audit, pass_receipts=[], adapters={})
    # The same assertion fails on 9e84b672c: rows there are CONTRACT_ERROR with
    # ASSISTED_AUDIO_SOURCE_BINDING_REQUIRED, after constructing missing inputs.
    assert {r['terminal_funnel_error']['status'] for r in result['findings']} == {'CALL_FAILURE'}, result
    assert {r['terminal_funnel_error']['reason_code'] for r in result['findings']} == {
        'TERMINAL_REVIEW_AUDITOR_UNAVAILABLE'}
    assert builds == []
    assert result['terminal_funnel']['attempts'] == []
    assert result['release_gate'] == 'BLOCK'
    assert 'FINAL_REVIEW_ENGINEERING_FAILURE' in result['reason_codes']
    assert 'FINAL_REVIEW_TRUE_CONTENT_BLOCK' not in result['reason_codes']
    assert result['discovery'] == before['discovery']
    for key in ('exact_release_adjudication', 'context_audio_adjudication'):
        assert [r.get(key) for r in result['findings']] == [r.get(key) for r in before['findings']]
    assert audit == before


@pytest.mark.parametrize('failure', ['status', 'discovery', 'missing_adapters'])
def test_other_upstream_dependency_outcomes_use_the_same_entry(failure):
    run, rows = recorded_case()
    audit = outage_audit(run, rows, discovery_status='COMPLETED')
    adapters = {'clip_context': {'candidate_id': 'new-candidate'}}
    if failure == 'status':
        audit['status'] = 'AUDITOR_UNAVAILABLE'
    elif failure == 'discovery':
        audit['discovery']['status'] = 'AUDITOR_UNAVAILABLE'
    else:
        adapters = {}
    result = funnel.terminalize_review(run['srt'], audit, pass_receipts=[], adapters=adapters)
    assert result['terminal_funnel']['content_checks_constructed'] is False
    assert all(row['lifecycle']['state'] == 'CALL_FAILURE' for row in result['findings'])


def test_unavailable_review_with_no_findings_still_blocks_as_engineering():
    audit = {'status': 'AUDITOR_UNAVAILABLE', 'reason_codes': [], 'findings': []}
    result = funnel.terminalize_review('', audit, pass_receipts=[], adapters={})
    assert result['release_gate'] == 'BLOCK'
    assert result['reason_codes'] == ['FINAL_REVIEW_ENGINEERING_FAILURE']
    assert result['terminal_funnel']['status'] == 'CALL_FAILURE'


def test_dependency_recovery_reenters_ordinary_check_and_keeps_binding_guard(monkeypatch):
    run, rows = recorded_case()
    failed = outage_audit(run, rows)
    result = funnel.terminalize_review(run['srt'], failed, pass_receipts=[], adapters={})
    assert result['terminal_funnel']['content_checks_constructed'] is False
    # A new reviewer outcome is authoritative for this invocation. The old
    # execution receipt cannot keep the recovered dependency permanently off.
    healthy = outage_audit(run, rows, discovery_status='COMPLETED')
    healthy['reason_codes'] = []
    recovered = funnel.terminalize_review(run['srt'], healthy, pass_receipts=[],
        adapters={'clip_context': {}})
    assert all(row['terminal_funnel_error']['status'] == 'CONTRACT_ERROR'
               for row in recovered['findings'])
    assert all(row['terminal_funnel_error']['reason_code'] == 'ASSISTED_AUDIO_SOURCE_BINDING_REQUIRED'
               for row in recovered['findings'])


def test_real_reviewer_provider_failure_is_not_sent_to_the_content_constructor(tmp_path, monkeypatch):
    from src.autoslice import producer_text_pipeline as pipeline
    from src.autoslice.llm_client import LlmCallError
    from src.autoslice.clip_context import build_clip_context
    from src.autoslice.speech_memory_ledger import SCHEMA_VERSION
    from tests.test_producer_text_pipeline_final_review import _adapters

    run, rows = recorded_case()
    request = next(iter(run['requests'].values()))
    binding = request['clip_context_binding']
    # Enter through the ordinary context producer. A hand-built partial
    # mapping fails CLIP_CONTEXT_SCHEMA_INVALID before the provider is reached.
    memory = tmp_path / 'speech-memory.json'
    memory.write_text(json.dumps({'schema_version': SCHEMA_VERSION, 'entries': []}))
    context = build_clip_context(candidate_id=binding['candidate_id'],
        spec={'date': '2026-10-24', 'pieces': [
            {'start_ms': 0, 'end_ms': 64000,
             'source_media_sha256': binding['source_media_sha256s'][0]}]},
        draft_srt=run['srt'], authoritative_chat=[], topic_resolution={},
        session_topic_authorities=[], speech_memory_ledger_path=memory)
    monkeypatch.delenv('AUTOSLICE_DISABLE_FINAL_REVIEW', raising=False)

    def unavailable(prompt):
        raise LlmCallError('offline transport outage', safe_reason='LLM_COMMAND_FAILED')

    with funnel.capture_terminal_adapters() as adapters:
        review = pipeline._run_exact_final_release_review(srt_text=run['srt'],
            correction_audit=run['guard_review']['correction_pass'], adapters=_adapters(),
            authoritative_chat=[], selection_hook='', clip_context=context,
            pronoun_audit_llm=unavailable, final_review_llm=unavailable)
    assert review['status'] == 'AUDITOR_UNAVAILABLE', review
    assert review['reason_codes'] == ['FINAL_REVIEW_PROVIDER_OR_JSON_UNAVAILABLE'], review
    assert adapters == {}
    # Existing obligations remain outstanding after the failed reviewer. The
    # recorded rows are the actual sister questions, not provider observations
    # invented by this offline test.
    review['findings'] = rows
    result = funnel.terminalize_review(run['srt'], review, pass_receipts=[], adapters=adapters)
    assert all(row['lifecycle']['state'] == 'CALL_FAILURE' for row in result['findings'])
    assert result['terminal_funnel']['content_checks_constructed'] is False
