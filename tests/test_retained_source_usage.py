from copy import deepcopy
import json

import pytest

from src.autoslice.retained_source_usage import reconcile_legacy_source_usage


def _authority(*, exact=False):
    source = {"schema_version": "final-review-audit.v1", "status": "FLAGGED",
              "context_adjudication_count": 15, "findings": [{"content_risk": "meaning_reversal"}]}
    return {"retained_source_input": {"schema_version": "retained-source-input-consumption.v1",
        "inherited_cpa_adjudication_count": 18, "parent_record": {"sha256": "a"*64}, "padded_sha256": "b"*64},
        "post_transcript_entity_output_srt_sha256": "c"*64,
        "cpa_adjudication_usage": {"provider_adjudication_count": 27 if exact else 18},
        "final_review_audit": {"correction_pass": source, "status": "FLAGGED"} if exact else source}


@pytest.mark.parametrize("exact,expected", [(False, 33), (True, 42)])
def test_legacy_increment_is_recovered_once_without_changing_verdict(exact, expected):
    authority = _authority(exact=exact)
    original_review = deepcopy(authority["final_review_audit"])
    receipt = reconcile_legacy_source_usage(authority)
    assert authority["cpa_adjudication_usage"]["provider_adjudication_count"] == expected
    assert authority["final_review_audit"] == original_review
    assert receipt["release_authorized"] is False
    assert reconcile_legacy_source_usage(authority) is None
    assert authority["cpa_adjudication_usage"]["provider_adjudication_count"] == expected


def test_current_cumulative_source_pass_is_not_double_counted():
    authority = _authority()
    authority["final_review_audit"]["cpa_adjudication_usage"] = {
        "prior_provider_adjudication_count": 18, "provider_adjudication_count": 33}
    authority["cpa_adjudication_usage"]["provider_adjudication_count"] = 33
    before = deepcopy(authority)
    assert reconcile_legacy_source_usage(authority) is None
    assert authority == before


def test_legacy_local_truth_increment_does_not_hide_omitted_source_pass():
    authority = _authority(exact=True)
    authority['retained_source_input']['inherited_cpa_adjudication_count'] = 25
    authority['cpa_adjudication_usage']['provider_adjudication_count'] = 73
    authority['source_truth_satisfaction_accounted'] = ['sha256:'+'e'*64]
    source = authority['final_review_audit']['correction_pass']
    source['context_adjudication_count'] = 20
    source['cpa_adjudication_usage'] = {
        'prior_provider_adjudication_count':25, 'provider_adjudication_count':26}
    reconcile_legacy_source_usage(authority)
    assert authority['cpa_adjudication_usage']['provider_adjudication_count'] == 93
    assert reconcile_legacy_source_usage(authority) is None


def _continued_legacy_source_authority():
    authority = _authority(exact=True)
    authority['retained_source_input']['inherited_cpa_adjudication_count'] = 25
    authority['cpa_adjudication_usage']['provider_adjudication_count'] = 73
    authority['source_truth_satisfaction_accounted'] = ['sha256:' + 'e' * 64]
    source = authority['final_review_audit']['correction_pass']
    source['context_adjudication_count'] = 20
    source['cpa_adjudication_usage'] = {
        'prior_provider_adjudication_count': 25, 'provider_adjudication_count': 26}
    reconcile_legacy_source_usage(authority)
    source['boundary_semantic_review'] = {
        'status': 'BLOCK', 'reason': 'CURRENT_TEXT_REQUIRES_SOURCE_REVIEW'}
    source['findings'][0]['carryover_consumption'] = {'status': 'CONSUMED'}
    usage = {'prior_provider_adjudication_count': 93, 'provider_adjudication_count': 111}
    authority['cpa_adjudication_usage'] = deepcopy(usage)
    authority['final_review_audit']['cpa_adjudication_usage'] = usage
    return authority


@pytest.mark.parametrize('prior', [93, 100])
def test_outer_cumulative_review_preserves_reconciled_usage_on_warm_and_cold_resume(prior):
    authority = _continued_legacy_source_authority()
    authority['final_review_audit']['cpa_adjudication_usage']['prior_provider_adjudication_count'] = prior
    authority['cpa_adjudication_usage']['prior_provider_adjudication_count'] = prior
    before = deepcopy(authority)
    assert before['legacy_retained_source_usage_reconciliation']['reconciled_count'] == 93
    for cold_resume in (False, False, True):
        if cold_resume:
            authority = json.loads(json.dumps(authority))
        assert reconcile_legacy_source_usage(authority) is None
        assert authority == before
        assert authority['cpa_adjudication_usage']['provider_adjudication_count'] == 111


@pytest.mark.parametrize('drift', [
    'parent', 'padded', 'srt', 'source_count', 'source_count_reduced', 'rollback',
    'outer_missing', 'prior_missing', 'prior_under', 'prior_after_current',
    'current_missing', 'current_below_reported', 'current_above_reported',
    'bool_prior', 'bool_current', 'receipt_math', 'receipt_schema',
    'receipt_binding_missing', 'receipt_digest', 'receipt_scope', 'receipt_release',
])
def test_outer_cumulative_review_requires_original_binding_and_complete_count_proof(drift):
    authority = _continued_legacy_source_authority()
    outer = authority['final_review_audit']['cpa_adjudication_usage']
    receipt = authority['legacy_retained_source_usage_reconciliation']
    if drift == 'parent':
        authority['retained_source_input']['parent_record']['sha256'] = 'd' * 64
    elif drift == 'padded':
        authority['retained_source_input']['padded_sha256'] = 'd' * 64
    elif drift == 'srt':
        authority['post_transcript_entity_output_srt_sha256'] = 'd' * 64
    elif drift == 'source_count':
        authority['final_review_audit']['correction_pass']['context_adjudication_count'] += 1
    elif drift == 'source_count_reduced':
        authority['final_review_audit']['correction_pass']['context_adjudication_count'] = 1
    elif drift == 'rollback':
        authority['cpa_adjudication_usage']['provider_adjudication_count'] = 92
    elif drift == 'outer_missing':
        del authority['final_review_audit']['cpa_adjudication_usage']
    elif drift == 'prior_missing':
        del outer['prior_provider_adjudication_count']
    elif drift == 'prior_under':
        outer['prior_provider_adjudication_count'] = 92
    elif drift == 'prior_after_current':
        outer['prior_provider_adjudication_count'] = 112
    elif drift == 'current_missing':
        del outer['provider_adjudication_count']
    elif drift == 'current_below_reported':
        outer['provider_adjudication_count'] = 110
    elif drift == 'current_above_reported':
        outer['provider_adjudication_count'] = 112
    elif drift == 'bool_prior':
        outer['prior_provider_adjudication_count'] = True
    elif drift == 'bool_current':
        outer['provider_adjudication_count'] = True
    elif drift == 'receipt_math':
        receipt['reconciled_count'] = 92
    elif drift == 'receipt_schema':
        receipt['schema_version'] = 'unknown'
    elif drift == 'receipt_binding_missing':
        del receipt['binding']
    elif drift == 'receipt_digest':
        receipt['binding']['source_review_sha256'] = None
    elif drift == 'receipt_scope':
        receipt['scope'] = 'HTTP'
    elif drift == 'receipt_release':
        receipt['release_authorized'] = True
    before = deepcopy(authority)
    with pytest.raises(ValueError, match='RECONCILIATION_DRIFT'):
        reconcile_legacy_source_usage(authority)
    assert authority == before


def test_partly_understood_source_counter_is_not_guessed():
    authority = _authority()
    authority['cpa_adjudication_usage']['provider_adjudication_count'] = 20
    authority['final_review_audit']['cpa_adjudication_usage'] = {
        'prior_provider_adjudication_count':18,'provider_adjudication_count':20}
    with pytest.raises(ValueError,match='AMBIGUOUS'):
        reconcile_legacy_source_usage(authority)


def test_new_completed_cumulative_review_can_advance_the_retained_input():
    authority=_authority()
    reconcile_legacy_source_usage(authority)
    assert authority['cpa_adjudication_usage']['provider_adjudication_count']==33
    authority['final_review_history']=[deepcopy(authority['final_review_audit'])]
    authority['cpa_adjudication_usage']['provider_adjudication_count']=35
    authority['final_review_audit']={'context_adjudication_count':2,
        'cpa_adjudication_usage':{'prior_provider_adjudication_count':33,'provider_adjudication_count':35}}
    authority['post_transcript_entity_output_srt_sha256']='d'*64
    assert reconcile_legacy_source_usage(authority) is None
    assert authority['cpa_adjudication_usage']['provider_adjudication_count']==35


@pytest.mark.parametrize('drift', ['parent', 'padded'])
def test_new_cumulative_source_pass_cannot_rebind_the_retained_media(drift):
    authority = _authority()
    reconcile_legacy_source_usage(authority)
    authority['cpa_adjudication_usage']['provider_adjudication_count'] = 35
    authority['final_review_audit'] = {'context_adjudication_count': 2,
        'cpa_adjudication_usage': {'prior_provider_adjudication_count': 33,
                                 'provider_adjudication_count': 35}}
    if drift == 'parent':
        authority['retained_source_input']['parent_record']['sha256'] = 'd' * 64
    else:
        authority['retained_source_input']['padded_sha256'] = 'd' * 64
    with pytest.raises(ValueError, match='RECONCILIATION_DRIFT'):
        reconcile_legacy_source_usage(authority)


@pytest.mark.parametrize('prior', [None, 32, 36, True])
def test_new_cumulative_source_pass_must_include_the_prior_reconciliation(prior):
    authority = _authority()
    reconcile_legacy_source_usage(authority)
    authority['cpa_adjudication_usage']['provider_adjudication_count'] = 35
    authority['final_review_audit'] = {'context_adjudication_count': 2,
        'cpa_adjudication_usage': {'prior_provider_adjudication_count': prior,
                                 'provider_adjudication_count': 35}}
    with pytest.raises(ValueError, match='RECONCILIATION_DRIFT'):
        reconcile_legacy_source_usage(authority)


@pytest.mark.parametrize("drift", ["counter", "review", "parent"])
def test_reconciliation_rejects_rollback_or_changed_observation(drift):
    authority = _authority()
    reconcile_legacy_source_usage(authority)
    if drift == "counter":
        authority["cpa_adjudication_usage"]["provider_adjudication_count"] = 18
    elif drift == "review":
        authority["final_review_audit"]["context_adjudication_count"] += 1
    else:
        authority["retained_source_input"]["parent_record"]["sha256"] = "d"*64
    with pytest.raises(ValueError, match="RECONCILIATION_DRIFT"):
        reconcile_legacy_source_usage(authority)
