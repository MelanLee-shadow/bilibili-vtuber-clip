"""Ordinary Argon writer, persisted raw answer and lifecycle reader; no API."""
from copy import deepcopy
import json

import pytest

from tests.test_argon_consumer_integration import scene, offline  # noqa: F401
from src.autoslice import finding_lifecycle as lifecycle
from src.autoslice.review_evidence_store import persist_document, load_review


@pytest.mark.parametrize('failure', ['status_contradiction', 'span_shape', 'new_binding_failure'])
def test_rejected_argon_result_keeps_original_and_routes_to_engineering(scene, failure):
    scene.run()
    def mutate(op):
        if 'output' not in op['metadata']:
            return op
        payload = op['metadata']['output']['inlinedResponses']['inlinedResponses'][0]['response']
        part = payload['candidates'][0]['content']['parts'][0]
        heard = json.loads(part['text'])
        if failure == 'status_contradiction':
            heard.update(status='NO_SPEECH', transcript='仍有文字', reason='synthetic contradiction')
        elif failure == 'span_shape':
            heard['uncertainties'] = ['not a timed span']
        else:
            heard['binding_sha256'] = '0'*64
        part['text'] = json.dumps(heard)
        return op
    scene.provider.mutate = mutate
    output, audit = scene.run(scene.build())
    assert output == scene.srt and audit['repaired'] is False
    assert audit['argon_escalation']['status'] == 'RESPONSE_REJECTED'
    assert audit['failure_layer'] == 'CONTRACT_ERROR'
    original = deepcopy(audit['argon_escalation'])
    path = scene.source.parent/'rejected-row.json'
    persist_document(path, {**scene.finding, 'exact_release_adjudication':audit})
    row = load_review(path)
    for legacy in (False, True):
        value = deepcopy(row)
        if legacy:
            value['exact_release_adjudication'].pop('failure_layer')
        before = deepcopy(value)
        view = lifecycle.classify(value)
        assert view['state'] == lifecycle.State.CONTRACT_ERROR
        assert view['new_model_call_authorized'] is False
        assert lifecycle.release_reason_codes([value]) == [lifecycle.ENGINEERING_FAILURE]
        assert value == before
        from src.autoslice import finding_closure as c, finding_closure_shell as shell
        from src.autoslice.finding_closure_ports import RetainedObject, ValidationRequest, ArtifactSet
        owners = shell.ExistingFindingOwners(shell.OwnerInputs('a'*64, 1000,
            RetainedObject.from_value(scene.context), RetainedObject.from_value({'meaning':'recorded'}),
            RetainedObject.from_value(scene.judge.cpa_cache_identity)))
        prepared = owners.prepare_finding(scene.srt, RetainedObject.from_value(value))
        state = c.initial_state(source_sha256='a'*64, srt_text=scene.srt, scan_channels=('recorded',))
        work = c.Work('historical-decision', 'decision', c.sha(scene.srt), prepared.spec)
        artifacts = ArtifactSet(shell.finding_documents(prepared))
        old_event = c.ReviewCompleted(work.work_id, prepared.spec, c.State.AWAITING_EVIDENCE,
                                      prepared.finding.sha256)
        if legacy:
            owners.revalidate(ValidationRequest(state, work, old_event, artifacts))
        else:
            with pytest.raises(c.ClosureContractError, match='COLD_RULING_DRIFT'):
                owners.revalidate(ValidationRequest(state, work, old_event, artifacts))
        forged = c.ReviewCompleted(work.work_id, prepared.spec, c.State.KEEP, prepared.finding.sha256)
        with pytest.raises(c.ClosureContractError, match='COLD_RULING_DRIFT'):
            owners.revalidate(ValidationRequest(state, work, forged, artifacts))
    count = len(scene.provider.calls)
    scene.run(scene.build())
    assert len(scene.provider.calls) == count
    assert audit['argon_escalation'] == original
    assert len([c for c in scene.provider.calls if c['method']=='POST']) == 1


@pytest.mark.parametrize('status', ['PENDING','ADMISSION_PENDING','UNKNOWN','UNCERTAIN','NO_SPEECH'])
def test_argon_routing_does_not_promote_unknown_or_valid_hearing(status):
    row = {'exact_release_adjudication':{'status':'UNCERTAIN','repaired':False,
        'reason_code':'ARGON_ESCALATION_PENDING_OR_UNRESOLVED',
        'argon_escalation':{'status':status,'job_name':'batches/retained'}}}
    before = deepcopy(row)
    view = lifecycle.classify(row)
    assert view['state'] == lifecycle.State.AWAITING_EVIDENCE
    assert not view['new_model_call_authorized'] and row == before
