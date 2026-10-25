"""Ordinary routed consumption, shared provider admission and raw singleflight."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import hashlib
from pathlib import Path
from threading import Barrier, Event, Lock
import time

import pytest

from src.autoslice import correction_review_parallel as parallel
from src.autoslice.deferred_same_cue_resolution import adjudicate_routed_findings
from src.autoslice import provider_admission as shared

SRT='1\n00:00:01,000 --> 00:00:02,000\n甲呀\n\n2\n00:00:03,000 --> 00:00:04,000\n乙呀\n\n3\n00:00:05,000 --> 00:00:06,000\n丙呀\n'


def controlled(original=None):
    from tests.test_f1_ruling_reuse import provider
    if original is None:original=provider()
    if not hasattr(original,'cpa_cache_identity'):
        original.cpa_cache_identity={'transport':'recorded','models':['top'],'effort':'high'}
    return shared.FinalReviewCall(original, provider='canonical-runtime-cpa:local',
        controller=shared.PROVIDER_CONTROLLER)


def rows():
    return [{'cue_index':i,'suspect':text,'suggestion':text+'好',
        'base_text_sha256':hashlib.sha256((text+'呀').encode()).hexdigest(),
        'routed':'disclosure'} for i,text in enumerate('甲乙丙',1)]


@pytest.fixture(autouse=True)
def offline_controller(monkeypatch):
    import socket
    def forbidden(*args,**kwargs):raise AssertionError('OFFLINE_NO_NETWORK')
    monkeypatch.setattr(socket.socket,'connect',forbidden)
    monkeypatch.setattr(shared,'PROVIDER_CONTROLLER',shared.ProviderController())


def test_ordinary_router_consumes_concurrent_unchanged_decisions_with_shared_controller():
    barrier=Barrier(2);active=peak=0;lock=Lock()
    def model(prompt):
        nonlocal active,peak
        with lock:active+=1;peak=max(peak,active)
        try:
            if prompt in ('1','2'):barrier.wait(timeout=5)
            return 'original raw'
        finally:
            with lock:active-=1
    call=controlled(model)
    def adjudicate(text,row):
        call(str(row['cue_index']))
        return text,{'repaired':False,'status':'UNCERTAIN'}
    findings=rows()
    with parallel.prefetch_correction_reviews(SRT,findings,adjudicate=adjudicate,provider_call=call) as prepared:
        result=adjudicate_routed_findings(SRT,findings,max_adjudications=None,adjudicate=prepared)
    assert result[0]==SRT and result[1]==3 and result[3]==0
    assert prepared.receipt['consumed']==3 and peak>=2
    assert shared.PROVIDER_CONTROLLER.windows[('canonical-runtime-cpa:local','text')].inflight==0


def test_changed_live_input_reenters_original_consumer_instead_of_applying_stale_result():
    calls=[];lock=Lock()
    def adjudicate(text,row):
        with lock:calls.append((row['cue_index'],text))
        if row['cue_index']==1:
            return text.replace('甲呀','甲好呀'),{'repaired':True,'request':{
                'matched_start_ms':1000,'matched_end_ms':2000,'current_cue':'甲呀','proposed_cue':'甲好呀'}}
        return text,{'repaired':False}
    findings=rows()
    with parallel.prefetch_correction_reviews(SRT,findings,adjudicate=adjudicate,provider_call=controlled()) as prepared:
        result=adjudicate_routed_findings(SRT,findings,max_adjudications=None,adjudicate=prepared)
    assert '甲好呀' in result[0] and result[3]==1
    assert prepared.receipt['input_changed']==2
    assert [(index,text) for index,text in calls if text!=SRT]==[(2,result[0]),(3,result[0])]


def test_same_cue_and_missing_controller_keep_serial_semantics(monkeypatch):
    findings=rows();findings.append(deepcopy(findings[0]));calls=[]
    baseline_calls=[]
    def ordinary(text,row):baseline_calls.append(row['cue_index']);return text,{'repaired':False}
    baseline=adjudicate_routed_findings(SRT,deepcopy(findings),max_adjudications=None,adjudicate=ordinary)
    monkeypatch.setattr(parallel,'_controller_module',lambda:None)
    def adjudicate(text,row):calls.append(row['cue_index']);return text,{'repaired':False}
    with parallel.prefetch_correction_reviews(SRT,findings,adjudicate=adjudicate,provider_call=controlled()) as prepared:
        result=adjudicate_routed_findings(SRT,findings,max_adjudications=None,adjudicate=prepared)
    assert calls==baseline_calls and result[:4]==baseline[:4]
    assert prepared.receipt['reason_code']=='SHARED_PROVIDER_CONTROLLER_NOT_INSTALLED'


def test_same_window_is_never_speculatively_readjudicated():
    findings=[rows()[0],deepcopy(rows()[0])];calls=[]
    baseline_calls=[]
    def ordinary(text,row):baseline_calls.append(row['cue_index']);return text,{'repaired':False}
    baseline=adjudicate_routed_findings(SRT,deepcopy(findings),max_adjudications=None,adjudicate=ordinary)
    def adjudicate(text,row):calls.append(row['cue_index']);return text,{'repaired':False}
    with parallel.prefetch_correction_reviews(SRT,findings,adjudicate=adjudicate,provider_call=controlled()) as prepared:
        assert not calls
        result=adjudicate_routed_findings(SRT,findings,max_adjudications=None,adjudicate=prepared)
    assert prepared.receipt['prefetched']==0 and calls==baseline_calls and result[:4]==baseline[:4]


def test_failure_joins_all_started_work_before_return():
    finished=Event()
    def adjudicate(text,row):
        if row['cue_index']==1:raise RuntimeError('original failure')
        time.sleep(.03);finished.set();return text,{'repaired':False}
    findings=rows()[:2]
    with pytest.raises(RuntimeError,match='original failure'):
        with parallel.prefetch_correction_reviews(SRT,findings,adjudicate=adjudicate,provider_call=controlled()) as prepared:
            prepared(SRT,findings[0])
    assert finished.is_set() and prepared.pool is None


def test_parallel_identical_material_uses_existing_canonical_singleflight(tmp_path,monkeypatch):
    from src.autoslice.completed_review_checkpoint import ruling_response
    from tests.test_f1_ruling_reuse import provider
    original=provider();original.completed_review_root=tmp_path/'raw'
    call=controlled(original)
    gate=Barrier(4)
    def invoke(_):
        gate.wait(timeout=5)
        return ruling_response(stage='same-material',prompt='same original question',llm_call=call,
            inputs={'material':{'audio':'a'*64}},parser_path=Path(__file__))
    with ThreadPoolExecutor(max_workers=4) as pool:results=list(pool.map(invoke,range(4)))
    assert len(original.calls)==1
    assert all(raw=='{"findings":[]}' for raw,_ in results)
    assert sum(hit for _,hit in results)==3



def test_unidentified_legacy_transport_keeps_serial_execution():
    calls=[]
    def adjudicate(text,row):calls.append(row['cue_index']);return text,{'repaired':False}
    findings=rows()
    with parallel.prefetch_correction_reviews(SRT,findings,adjudicate=adjudicate,provider_call=lambda x:x) as prepared:
        assert calls==[]
        adjudicate_routed_findings(SRT,findings,max_adjudications=None,adjudicate=prepared)
    assert calls==[1,2,3] and prepared.receipt['reason_code']=='CANONICAL_PROVIDER_CALL_REQUIRED'


def test_native_hard_admission_uses_the_existing_ordered_owner_context():
    from src.autoslice.exact_review_parallel import wait_for_native_turn,finish_native_admission
    later_started=Event();order=[]
    def adjudicate(text,row):
        index=row['cue_index']
        if index==1:assert later_started.wait(5)
        else:later_started.set()
        wait_for_native_turn(require_open=True)
        order.append(index)
        finish_native_admission()
        return text,{'repaired':False}
    findings=rows()[:2]
    with parallel.prefetch_correction_reviews(SRT,findings,adjudicate=adjudicate,provider_call=controlled()) as prepared:
        adjudicate_routed_findings(SRT,findings,max_adjudications=None,adjudicate=prepared)
    assert order==[1,2]



def test_ordinary_correction_entry_counts_prefetch_and_current_input_revalidation(monkeypatch):
    from src.autoslice import producer_text_pipeline as pipeline
    from tests.test_producer_text_pipeline_final_review import _adapters
    findings=rows()
    for row,text in zip(findings,'甲乙丙'):
        row.update(proposed_full_cue=text+'好呀',repair_class='phonetic')
    monkeypatch.setattr(pipeline,'_build_final_review_llm_call',lambda:controlled())
    monkeypatch.setattr(pipeline,'audit_final_subtitles',lambda *a,**k:deepcopy(findings))
    monkeypatch.setattr(pipeline,'route_findings',lambda text,rows,**k:(text,{'findings':rows,'applied_count':0}))
    def adjudicate(text,finding,**kwargs):
        row=finding
        if row['cue_index']==1:
            return text.replace('甲呀','甲好呀'),{'repaired':True,'request':{
                'matched_start_ms':1000,'matched_end_ms':2000,'current_cue':'甲呀','proposed_cue':'甲好呀'}}
        return text,{'repaired':False}
    monkeypatch.setattr(pipeline,'adjudicate_context_finding',adjudicate)
    text,audit=pipeline._run_final_review(srt_text=SRT,chat_authority_audit={'applied':[]},
        handled_entity_cues=set(),verify_confusable_entity=lambda request:None,
        adapters=_adapters(),initial_provider_adjudication_count=0)
    assert '甲好呀' in text,audit
    assert audit['context_adjudication_target_count']==3
    assert audit['context_adjudication_count']==5
    assert audit['cpa_adjudication_usage']['provider_adjudication_count']==5
    assert audit['correction_review_execution']['input_changed']==2


@pytest.mark.parametrize('repair', [False, True])
def test_entire_routed_output_matches_serial_byte_for_byte(repair):
    import json
    def adjudicate(text, row):
        if repair and row['cue_index'] == 1:
            return text.replace('甲呀', '甲好呀'), {'repaired': True, 'request': {
                'matched_start_ms': 1000, 'matched_end_ms': 2000,
                'current_cue': '甲呀', 'proposed_cue': '甲好呀'}}
        return text, {'repaired': False, 'status': 'UNCERTAIN', 'raw': '原始答复\n'}
    serial_rows, concurrent_rows = rows(), rows()
    serial = adjudicate_routed_findings(SRT, serial_rows, max_adjudications=None, adjudicate=adjudicate)
    with parallel.prefetch_correction_reviews(SRT, concurrent_rows, adjudicate=adjudicate,
            provider_call=controlled()) as prepared:
        concurrent = adjudicate_routed_findings(SRT, concurrent_rows,
            max_adjudications=None, adjudicate=prepared)
    encode = lambda result, findings: json.dumps([result, findings], ensure_ascii=False, sort_keys=True).encode()
    assert encode(concurrent, concurrent_rows) == encode(serial, serial_rows)
