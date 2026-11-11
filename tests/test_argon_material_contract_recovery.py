"""A retained batch refusal must survive the ordinary terminal/cold consumer."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path

import pytest

from src.autoslice import finding_lifecycle
from src.autoslice.exact_final_terminal_funnel import terminalize_review, review_pass_receipt
from src.autoslice.review_evidence_store import persist_document, load_review
from tests.test_argon_targeted_relisten import attach_argon, run_public, public_judge
from tests.test_argon_consumer_integration import offline  # noqa: F401
from tests.test_targeted_relisten_public_contract import build_offline_rig, three_observations


@pytest.mark.parametrize('damage', ['integer_span', 'string_span', 'binding'])
def test_saved_batch_rejection_reaches_terminal_recovery_without_redispatch(tmp_path, monkeypatch, damage):
    rig = build_offline_rig(tmp_path, monkeypatch)
    _, check, finding, _ = three_observations(rig)
    provider, integration = attach_argon(rig, monkeypatch)
    _, pending = run_public(rig, finding, public_judge(rig))
    assert pending['argon_escalation']['status'] == 'PENDING'

    def malformed(operation):
        if 'output' not in operation['metadata']:
            return operation
        payload = operation['metadata']['output']['inlinedResponses']['inlinedResponses'][0]['response']
        part = payload['candidates'][0]['content']['parts'][0]
        heard = json.loads(part['text'])
        if damage == 'binding':
            heard['binding_sha256'] = '0' * 64
        else:
            heard['uncertainties'] = [-1] if damage == 'integer_span' else ['not a timed span']
        part['text'] = json.dumps(heard)
        return operation

    provider.mutate = malformed
    _, refused = run_public(rig, finding, public_judge(rig))
    assert refused['argon_escalation']['status'] == 'RESPONSE_REJECTED'
    finding = {**finding, 'exact_release_adjudication': refused}
    original = deepcopy(refused['argon_escalation'])
    owner = integration.session(check, finding)
    assert owner.resume() and owner.envelope is None
    snapshots = {path: hashlib.sha256(path.read_bytes()).hexdigest()
                 for path in (owner.root / 'batch').rglob('*') if path.is_file()}
    calls = len(provider.calls)

    receipt = review_pass_receipt(pass_index=1, srt_text=rig.srt,
        audit={'findings': [finding]}, repaired_text=rig.srt, repairs=[])
    result = terminalize_review(rig.srt,
        {'findings': [finding], 'unresolved_findings_disclosed': [], 'reason_codes': ['PENDING']},
        pass_receipts=[receipt], adapters={'clip_context': rig.context,
            'entity_verifier': rig.verifier, 'judge_llm_call': public_judge(rig)})
    path = tmp_path / 'terminal.json'
    persist_document(path, result)
    row = load_review(path)['findings'][0]
    error = row['terminal_funnel_error']
    assert error['status'] == 'CONTRACT_ERROR', error
    assert error['reason_code'] == original['reason']
    recovery = error['material_recovery']
    assert recovery['kind'] == 'REVALIDATE_ORIGINAL_ANSWER_AFTER_CONSUMER_REPAIR'
    assert recovery['new_model_call_authorized'] is False
    assert recovery['owner_audit']['response_sha256'] == original['response_sha256']
    assert recovery['owner_audit']['result_sha256'] == original['result_sha256']
    assert recovery['owner_audit']['request_sha256'] == original['request_sha256']
    assert finding_lifecycle.classify(row)['state'] == 'CONTRACT_ERROR'
    assert result['release_gate'] == 'BLOCK'
    assert len(provider.calls) == calls
    assert snapshots == {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in snapshots}
    assert len([c for c in provider.calls if c['method'] == 'POST']) == 1
