import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.autoslice import cpa_runtime as cpa, llm_client
from src.autoslice.production_attempts import (
    observed_production_attempt, read_attempts, record_production_result,
)


def test_first_package_build_failure_is_observed_before_manifest_exists(tmp_path,monkeypatch):
    from src.autoslice.production_attempts import observed_package_stage
    package=tmp_path/'package';package.mkdir()
    (package/'clip.publish.json').write_text(json.dumps({'candidate_id':'fresh'}))
    (package/'clip.clip-context.json').write_text(json.dumps({'recording_date':'2026-10-12'}))
    prepared=tmp_path/'prepared.json'
    prepared.write_text(json.dumps({'lane':'talk','candidate_id':'fresh'}))
    monkeypatch.setenv('AUTOSLICE_BASE',str(tmp_path))
    with observed_package_stage(package,'package_build',prepared_delivery=prepared):
        record_production_result({'runtime_stage_result':{'status':'BLOCKED','reason_code':'DailyManifestError'}})
    row=read_attempts(tmp_path,'2026-10-12')[0]
    assert (row['candidate_id'],row['lane'],row['stage'],row['status'])==('fresh','talk','package_build','BLOCKED')
    assert not (package/'review_manifest.json').exists()


def test_verified_remote_costs_and_failures_are_reported_without_rewriting_result(tmp_path):
    import copy
    result={'status':'failed','failure_kind':'subtitle_authority',
        'model_requests_observed':3,'cpa_http_attempts_observed':4,'cache_hits_observed':2,
        'audio_model_requests_observed':0,'audio_http_attempts_observed':0,'production_elapsed_seconds':19.5}
    before=copy.deepcopy(result)
    with observed_production_attempt(runtime_root=tmp_path,date='2026-10-11',candidate_id='remote',lane='talk',
            trace=tmp_path/'usage.jsonl',execution_node='jp'):
        record_production_result(result)
    row=read_attempts(tmp_path,'2026-10-11')[0]
    assert row['status']=='BLOCKED' and row['reason_code']=='subtitle_authority'
    assert (row['model_requests_observed'],row['cpa_http_attempts_observed'],row['cache_hits_observed'])==(3,4,2)
    assert row['remote_producer_elapsed_seconds']==19.5 and result==before
    assert 'production @ jp' in (tmp_path/'reports/latest.md').read_text()
    with pytest.raises(RuntimeError):
        with observed_production_attempt(runtime_root=tmp_path,date='2026-10-11',candidate_id='unknown',lane='talk',
                trace=tmp_path/'unknown.jsonl',execution_node='sjc'):
            raise RuntimeError('OVERFLOW_DISCONNECTED')
    row=read_attempts(tmp_path,'2026-10-11')[-1]
    assert row['reason_code']=='OVERFLOW_DISCONNECTED' and row['model_requests_observed'] is None


def test_ordinary_failure_records_actual_reason_and_time_without_writing_canonical_state(tmp_path,monkeypatch):
    state=tmp_path/'state/2026-10-11.json';state.parent.mkdir()
    state.write_text('{"picks":[],"songs":[],"untouched":"canonical"}')
    before=state.read_bytes()
    monkeypatch.delenv(cpa.ATTEMPT_ENV,raising=False)
    monkeypatch.delenv(cpa.USAGE_ENV,raising=False)
    with pytest.raises(RuntimeError):
        with observed_production_attempt(runtime_root=tmp_path,date='2026-10-11',candidate_id='actual',lane='talk',trace=tmp_path/'usage.jsonl'):
            raise RuntimeError('CURRENT_USAGE_PARENT_DRIFT secret-payload-must-not-be-logged')
    row=read_attempts(tmp_path,'2026-10-11')[0]
    assert row['status']=='BLOCKED' and row['reason_code']=='CURRENT_USAGE_PARENT_DRIFT'
    assert row['production_elapsed_seconds']>=0 and row['cpa_http_attempts'] is None
    text=(tmp_path/'reports/latest.md').read_text()
    assert 'actual' in text and 'CURRENT_USAGE_PARENT_DRIFT' in text and '未知' in text
    assert b'secret-payload' not in (tmp_path/'reports/production-attempts/2026-10-11.jsonl').read_bytes()
    assert state.read_bytes()==before
    assert cpa.ATTEMPT_ENV not in os.environ and cpa.USAGE_ENV not in os.environ


def test_captured_command_environment_gets_current_observation_and_counts_retries_separately(tmp_path,monkeypatch):
    monkeypatch.delenv(cpa.ATTEMPT_ENV,raising=False)
    monkeypatch.delenv(cpa.USAGE_ENV,raising=False)
    config=llm_client.LlmConfig(transport='command',command_template='bridge {prompt_file} {completion_file}',
        command_child_env={cpa.USAGE_ENV:'old-usage',cpa.ATTEMPT_ENV:'old-id','CPA_API_KEY':'offline-fixture'})
    observed=[]
    def fake_run(argv,**kwargs):
        env=kwargs['env'];observed.append(env)
        assert env[cpa.USAGE_ENV]==str(tmp_path/'usage.jsonl')
        assert env[cpa.ATTEMPT_ENV]==cpa.observation_environment()[cpa.ATTEMPT_ENV]
        assert env['CPA_API_KEY']=='offline-fixture'
        cpa.record_cpa_attempt(model='fixture',kind='text',status=503,elapsed_seconds=1)
        cpa.record_cpa_attempt(model='fixture',kind='text',status=200,elapsed_seconds=1)
        Path(argv[2]).write_text('complete original response')
        return SimpleNamespace(returncode=0,stdout='',stderr='')
    monkeypatch.setattr(llm_client.subprocess,'run',fake_run)
    monkeypatch.setattr(llm_client,'_provider_dispatch',lambda call,_root=None,**kwargs:call())
    call=llm_client.build_llm_call(config)
    with observed_production_attempt(runtime_root=tmp_path,date='2026-10-11',candidate_id='actual',lane='talk',trace=tmp_path/'usage.jsonl'):
        assert call('offline prompt')=='complete original response'
        cpa.record_cpa_event('cache_hit',{'kind':'completed_review'})
    row=read_attempts(tmp_path,'2026-10-11')[0]
    assert row['model_requests_observed']==1 and row['cpa_http_attempts_observed']==2 and row['cache_hits_observed']==1
    assert row['cpa_http_attempts'] is None
    assert cpa.cpa_attempt_count(tmp_path/'usage.jsonl')==2
    assert config.command_child_env[cpa.USAGE_ENV]=='old-usage' and len(observed)==1


def test_two_interleaved_runner_threads_keep_independent_ids_traces_and_report_rows(tmp_path,monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    monkeypatch.delenv(cpa.ATTEMPT_ENV,raising=False)
    monkeypatch.delenv(cpa.USAGE_ENV,raising=False)
    a_entered,b_entered,a_recorded,b_finished=Event(),Event(),Event(),Event()
    def run_a():
        with observed_production_attempt(runtime_root=tmp_path,date='2026-10-11',candidate_id='A',lane='talk',trace=tmp_path/'A.jsonl'):
            a_entered.set();assert b_entered.wait(10)
            cpa.record_cpa_event('model_request',{'kind':'text','candidate_marker':'A'})
            a_recorded.set();assert b_finished.wait(10)
    def run_b():
        assert a_entered.wait(10)
        with observed_production_attempt(runtime_root=tmp_path,date='2026-10-11',candidate_id='B',lane='talk',trace=tmp_path/'B.jsonl'):
            b_entered.set();assert a_recorded.wait(10)
            cpa.record_cpa_event('model_request',{'kind':'text','candidate_marker':'B'})
        b_finished.set()
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures=[pool.submit(run_a),pool.submit(run_b)]
        for future in futures:future.result(timeout=20)
    rows=read_attempts(tmp_path,'2026-10-11')
    assert len(rows)==2 and len({r['attempt_id'] for r in rows})==2
    assert {r['candidate_id']:r['model_requests_observed'] for r in rows}=={'A':1,'B':1}
    for cid in ('A','B'):
        events=[json.loads(line) for line in (tmp_path/f'{cid}.jsonl').read_text().splitlines()]
        assert [event['candidate_marker'] for event in events]==[cid]
    assert all(cid in (tmp_path/'reports/latest.md').read_text() for cid in ('A','B'))
    assert cpa.ATTEMPT_ENV not in os.environ and cpa.USAGE_ENV not in os.environ


def test_explicit_child_process_keeps_parent_attempt_and_cache_is_not_a_new_model_call(tmp_path,monkeypatch):
    import subprocess,sys
    monkeypatch.delenv(cpa.ATTEMPT_ENV,raising=False)
    monkeypatch.delenv(cpa.USAGE_ENV,raising=False)
    code='''from pathlib import Path
import sys
from src.autoslice.production_attempts import observed_production_attempt
from src.autoslice.cpa_runtime import record_cpa_event
with observed_production_attempt(runtime_root=Path(sys.argv[1]),date='2026-10-11',candidate_id='parent-child',lane='talk',trace=Path(sys.argv[2]),inherit_process_attempt=True):
    record_cpa_event('model_request',{'kind':'text'})
'''
    trace=tmp_path/'usage.jsonl'
    with observed_production_attempt(runtime_root=tmp_path,date='2026-10-11',candidate_id='parent-child',lane='talk',trace=trace):
        env={**os.environ,**cpa.observation_environment()}
        subprocess.run([sys.executable,'-c',code,str(tmp_path),str(trace)],env=env,check=True)
        cpa.record_cpa_event('cache_hit',{'kind':'completed_review'})
    rows=read_attempts(tmp_path,'2026-10-11')
    assert len(rows)==1 and rows[0]['model_requests_observed']==1 and rows[0]['cache_hits_observed']==1
    events=[json.loads(line) for line in trace.read_text().splitlines()]
    assert len({row['production_attempt_id'] for row in events})==1
    assert rows[0]['cpa_http_attempts'] is None


@pytest.mark.parametrize('complete',[False,True])
def test_zero_exit_prepared_result_cannot_become_complete_package_acceptance(tmp_path,monkeypatch,complete):
    monkeypatch.delenv(cpa.ATTEMPT_ENV,raising=False)
    monkeypatch.delenv(cpa.USAGE_ENV,raising=False)
    roles={'video','subtitle','record','publish','chat_authority','clip_context'}
    if complete:roles|={'cover','cover_request','cover_response'}
    result={'candidate_id':'candidate','prepared_delivery':{'manifest_path':'actual-native-handle'},
        'prepared_artifacts':{key:{} for key in roles},'cover_status':'AI_COVER_READY' if complete else 'BLOCKED_AI_COVER_REQUIRED',
        'reason_codes':[] if complete else ['COVER_REVIEW_CURRENT_INPUT_DRIFT']}
    with observed_production_attempt(runtime_root=tmp_path,date='2026-10-11',candidate_id='candidate',lane='talk',trace=tmp_path/'usage.jsonl'):
        record_production_result(result)
    row=read_attempts(tmp_path,'2026-10-11')[0]
    assert row['status']==('PREPARED_COMPLETE' if complete else 'PARTIAL_PREPARED')
    assert row['public_package_accepted'] is False
    text=(tmp_path/'reports/latest.md').read_text()
    assert '登记产出 未记录' in text and '未知' in text
    if not complete:assert 'COVER_REVIEW_CURRENT_INPUT_DRIFT' in text


def test_package_stages_require_both_current_audit_and_qc_and_do_not_hide_a_later_block(tmp_path):
    from src.autoslice.production_attempts import package_stage_binding
    date='2026-10-12';root=tmp_path/'package';root.mkdir();manifest=root/'review_manifest.json'
    manifest.write_text(json.dumps({'date':date,'candidate_id':'actual','items':[{'classification':'talk'}]}))
    def stage(name,status,reason=None):
        with observed_production_attempt(runtime_root=tmp_path,date=date,candidate_id='actual',lane='talk',
                stage=name,trace=tmp_path/(name+'.jsonl')):
            record_production_result({'runtime_stage_result':{'status':status,'reason_code':reason,**package_stage_binding(root)}})
    stage('package_build','PACKAGE_BUILT')
    stage('package_audit','BLOCKED','BOUNDARY_SOURCE_SEPARATION_WITNESS_INVALID')
    stage('joint_qc','JOINT_QC_PASS')
    text=(tmp_path/'reports/latest.md').read_text()
    assert '已验完整包 0' in text and '阻断 1' in text
    stage('package_audit','PACKAGE_AUDIT_PASS')
    assert '已验完整包 1' in (tmp_path/'reports/latest.md').read_text()
    manifest.write_text(manifest.read_text()+'\n')
    stage('package_build','PACKAGE_BUILT');stage('package_audit','PACKAGE_AUDIT_PASS')
    assert '已验完整包 0' in (tmp_path/'reports/latest.md').read_text()
    stage('joint_qc','JOINT_QC_PASS')
    assert '已验完整包 1' in (tmp_path/'reports/latest.md').read_text()
    assert all(not r['public_package_accepted'] for r in read_attempts(tmp_path,date))
    assert not (tmp_path/'state').exists()


def test_uncovered_command_http_is_unknown_and_audio_http_is_independently_counted(tmp_path):
    import urllib.error
    from src.autoslice import agy_gemini_client as audio
    from contextlib import nullcontext
    from unittest.mock import patch
    with observed_production_attempt(runtime_root=tmp_path,date='2026-10-12',candidate_id='audio',
            lane='song',trace=tmp_path/'usage.jsonl'):
        cpa.record_cpa_event('model_request',{'transport':'uncovered_command'})
        with patch.object(audio,'runtime_provider_slot',lambda **_:nullcontext()), patch.object(audio.urllib.request,'urlopen',
                side_effect=urllib.error.HTTPError('https://example.invalid',503,'unavailable',{},None)):
            with pytest.raises(urllib.error.HTTPError):
                audio.generate_content(prompt='audio',key='secret-never-recorded',model='gemini-3-flash-preview',
                    inline_data=b'complete audio',mime_type='audio/mpeg')
    row=read_attempts(tmp_path,'2026-10-12')[0]
    assert row['cpa_http_attempts_observed'] is None
    assert row['audio_model_requests_observed']==row['audio_http_attempts_observed']==1
    assert 'secret-never-recorded' not in (tmp_path/'usage.jsonl').read_text()


def test_normal_package_cli_records_its_actual_gate_result_and_current_binding(tmp_path, monkeypatch, capsys):
    import sys
    from scripts import audit_review_package as audit
    from scripts import run_title_cover_joint_qc as qc
    root=tmp_path/'package';root.mkdir();manifest=root/'review_manifest.json'
    manifest.write_text(json.dumps({'date':'2026-10-12','candidate_id':'ordinary','items':[{'classification':'talk'}]}))
    receipt=root/'title-cover-joint-qc.json';receipt.write_text('{"status":"PASS"}')
    monkeypatch.setenv('AUTOSLICE_BASE',str(tmp_path))
    monkeypatch.setattr(audit,'audit_package',lambda _: {'passed':False,'blocking_issue_count':1,'issue_count':1,
        'issues':[{'code':'BOUNDARY_SOURCE_SEPARATION_WITNESS_INVALID','severity':'BLOCK'}]})
    monkeypatch.setattr(sys,'argv',['audit',str(root),'--json'])
    assert audit.main()==1
    monkeypatch.setattr(qc,'run_qc',lambda *a,**kw: {'pass':True,'status':'PASS','verdict':{}})
    monkeypatch.setattr(sys,'argv',['qc',str(root),'same title',str(receipt),'--reuse-valid'])
    assert qc.main()==0
    assert '已验完整包 0' in (tmp_path/'reports/latest.md').read_text()
    assert '阻断 1' in (tmp_path/'reports/latest.md').read_text()
    rows=read_attempts(tmp_path,'2026-10-12')
    assert {r['stage'] for r in rows}=={'package_audit','joint_qc'}
    assert rows[-1]['model_requests_observed']==0 and rows[-1]['cache_hits_observed']==1
    monkeypatch.setattr(audit,'audit_package',lambda _: {'passed':True,'blocking_issue_count':0,'issue_count':0,'issues':[]})
    monkeypatch.setattr(sys,'argv',['audit',str(root),'--json'])
    assert audit.main()==0 and '已验完整包 1' in (tmp_path/'reports/latest.md').read_text()
    def changing(_):
        manifest.write_text(manifest.read_text()+'\n')
        return {'passed':True,'blocking_issue_count':0,'issue_count':0,'issues':[]}
    monkeypatch.setattr(audit,'audit_package',changing)
    assert audit.main()==0
    latest=read_attempts(tmp_path,'2026-10-12')[-1]
    assert latest['status']=='BLOCKED' and latest['reason_code']=='PACKAGE_INPUT_BINDING_DRIFT'
    assert not (tmp_path/'state').exists()
