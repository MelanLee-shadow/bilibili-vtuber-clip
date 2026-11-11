import json
from types import SimpleNamespace

import pytest

from src.autoslice.restatement_recall import discover_restatement_findings
from src.autoslice.discovery_channels import batch_from_rows,_prepared_scan_findings
from src.autoslice.final_review_auditor import audit_final_subtitles
from src.autoslice.finding_closure_shell import ExistingFindingOwners,OwnerInputs
from src.autoslice.finding_closure_ports import RetainedObject
from src.autoslice.review_evidence_store import persist_review,load_review


@pytest.mark.parametrize('early,late,kind',[
    ('你能想象','你能想象这个','spoken_unit'),
    ('你能想象这个场景','你能想象场景','spoken_unit'),
    ('你能想象这是啥','你能想象这是吗','phonetic'),
])
def test_restatement_generator_to_persisted_discovery_keeps_its_pending_task(tmp_path,monkeypatch,early,late,kind):
    monkeypatch.setenv('AUTOSLICE_FINAL_REVIEW_MODE','per_finding')
    text=f'1\n00:00:00,000 --> 00:00:02,000\n{early}\n\n2\n00:00:04,000 --> 00:00:05,000\n无关填充\n\n3\n00:00:10,000 --> 00:00:13,000\n{late}\n'
    rows,receipt=discover_restatement_findings(text)
    assert receipt['status']=='PASS' and len(rows)==1
    assert rows[0]['repair_class']==kind
    assert rows[0]['candidate_provenance']['mutation_authorized'] is False
    context={'candidate_id':'restatement-member','pieces':[{'source_media_sha256':'a'*64}]}
    batch=batch_from_rows(rows,context=context)
    def answer(prompt):
        if '本次不授权改字、删字或听音调度' in prompt:
            return json.dumps({'findings':[{'cue':1,'content_risk':'meaning_reversal','risk_basis':'核对原声是否只是开头片段'}]},ensure_ascii=False)
        return '{"findings":[]}'
    captured=[]
    findings=audit_final_subtitles(text,llm_call=answer,extract_json=json.loads,
        candidate_context=context,discovery_batch=batch,discovery_result_sink=captured.append)
    path=tmp_path/'discovery.json';persist_review(path,{'findings':findings})
    restored=load_review(path)['findings']
    owners=ExistingFindingOwners(OwnerInputs('a'*64,0,RetainedObject.from_value(context),
        RetainedObject.from_value({'role':'ordinary-no-mutation'}),
        RetainedObject.from_value({'transport':'recorded','models':['recorded-model'],'effort':'medium'})))
    prepared=_prepared_scan_findings(SimpleNamespace(prepare_finding=owners.prepare_finding),text,captured[0],restored,frozenset())
    assert len(prepared)==1 and len(captured[0].tasks)==1
    assert late in prepared[0].materials.value()['candidates']
    assert not any(row.get('mutation_authority') for row in restored)
