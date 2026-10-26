"""Ordinary receipt readers reuse only unchanged, fully validated content."""
from copy import deepcopy
import hashlib
import json
import os

import pytest

from src.autoslice import argon_escalation_storage as store, review_evidence_store as evidence
from src.autoslice.validation_memo import memoized_validation


def test_reader_result_mutation_and_backdated_blob_tamper_are_not_cached(tmp_path):
    root=tmp_path/'evidence-store';raw=b'{"unknown":null,"status":"UNKNOWN"}'
    digest=store.blob(root,raw);calls=[]
    @memoized_validation(lambda root,digest:str(root)+digest)
    def validate(root,digest):
        calls.append(digest)
        return json.loads(store.get_blob(root,digest))
    result=validate(root,digest);result['status']='invented'
    assert validate(root,digest)=={'unknown':None,'status':'UNKNOWN'}
    assert len(calls)==1
    path=root/'blobs'/f'{digest}.bin';stamp=path.stat()
    changed=raw.replace(b'UNKNOWN',b'ALTERED');assert len(changed)==len(raw)
    path.write_bytes(changed);os.utime(path,ns=(stamp.st_atime_ns,stamp.st_mtime_ns))
    with pytest.raises(ValueError,match='ARTIFACT_SHA_MISMATCH'):validate(root,digest)
    assert len(calls)==2
    with pytest.raises(ValueError,match='ARTIFACT_SHA_MISMATCH'):validate(root,digest)
    assert len(calls)==3  # A failed replay never creates a successful memo.


def test_same_content_at_replaced_inode_revalidates_and_missing_source_refuses(tmp_path):
    root=tmp_path/'evidence-store';raw=b'{"raw":"original"}';digest=store.blob(root,raw);calls=[]
    @memoized_validation(lambda: digest)
    def validate():
        calls.append(1);return json.loads(store.get_blob(root,digest))
    assert validate()=={'raw':'original'}
    store.blob(root,raw)
    assert validate()=={'raw':'original'} and len(calls)==2
    (root/'blobs'/f'{digest}.bin').unlink()
    with pytest.raises(FileNotFoundError):validate()


def test_changed_current_materials_and_original_program_error_keep_identity(tmp_path):
    calls=[];error=TypeError('original bug')
    @memoized_validation(lambda current:hashlib.sha256(json.dumps(current).encode()).hexdigest())
    def validate(current):
        calls.append(deepcopy(current))
        if current['value']=='bug':raise error
        return None
    validate({'value':'first'});validate({'value':'first'})
    validate({'value':'second'})
    assert len(calls)==2
    for _ in range(2):
        with pytest.raises(TypeError) as caught:validate({'value':'bug'})
        assert caught.value is error
    assert len(calls)==4


def test_ordinary_cold_load_decodes_same_blob_once_but_checks_each_source(tmp_path,monkeypatch):
    original={'request':{'whole_clip_current_srt':'原文'*4000},'raw_completion':'原答','status':'UNKNOWN'}
    path=tmp_path/'review.json';evidence.persist_document(path,original)
    calls=[];real=store.get_blob
    def read(root,sha):calls.append((str(root),sha));return real(root,sha)
    monkeypatch.setattr(store,'get_blob',read)
    first=evidence.load_review(path);count=len(calls)
    assert first==original and count>0
    first['request']['whole_clip_current_srt']='changed in consumer'
    assert evidence.load_review(path)==original and len(calls)==count
    packed=json.loads(path.read_bytes());ref=packed['request']
    from pathlib import Path
    Path(ref['path']).write_bytes(b'{}')
    with pytest.raises(ValueError,match='ARTIFACT_SHA_MISMATCH'):evidence.load_review(path)


def test_strict_json_still_rejects_duplicates_at_every_depth_and_nonfinite():
    from src.autoslice.argon_escalation_contract import strict_json
    assert strict_json('{"a":{"x":1},"b":[false,null,"原字节"]}')=={'a':{'x':1},'b':[False,None,'原字节']}
    for raw in ['{"a":1,"a":1}','{"a":{"x":1,"x":2}}','{"a":[{"x":1,"x":2}]}','{"a":NaN}']:
        with pytest.raises(ValueError):strict_json(raw)


def test_compactor_uses_one_size_pass_and_retains_original_boundary(tmp_path,monkeypatch):
    original={'findings':[{'cue_index':1,'raw_completion':'原答'}], 'history':{'raw':'x'*3990,'other':'y'*3990}}
    original['history']={'nested':original['history'],'integer':3,'unknown':None}
    calls=[];real=evidence.encoded
    def encode(value):calls.append(value);return real(value)
    monkeypatch.setattr(evidence,'encoded',encode)
    packed=evidence.compact_review(original,tmp_path)
    # Size calculation no longer recursively serializes each inline parent.
    assert not any(value is original['findings'][0] for value in calls)
    assert evidence.expand(packed)==original
    assert original['history']['nested']['raw']=='x'*3990
    assert evidence.is_reference(packed['history']['nested'])

from tests.test_argon_consumer_integration import scene, offline  # noqa: F401
from tests.test_targeted_relisten_public_contract import build_offline_rig,three_observations,public_judge
from tests.test_argon_targeted_relisten import attach_argon,run_public


def test_real_ordinary_consumer_reuses_validation_and_reopens_tampered_native_raw(scene,monkeypatch):
    from pathlib import Path
    from src.autoslice.argon_escalation_integration import validate_consumer_evidence
    from src.autoslice.argon_escalation_witness import ArgonEscalationWitness
    scene.run();_,audit=scene.run(scene.build())
    check=audit['request'];envelope=check['argon_escalation_evidence']
    reads=[];real=ArgonEscalationWitness.validate_evidence
    def validate(self,*args,**kwargs):reads.append(1);return real(self,*args,**kwargs)
    monkeypatch.setattr(ArgonEscalationWitness,'validate_evidence',validate)
    original=validate_consumer_evidence(check,envelope);count=len(reads)
    methods=len(scene.provider.calls)
    assert validate_consumer_evidence(check,envelope)==original and len(reads)==count
    assert len(scene.provider.calls)==methods
    assert validate_consumer_evidence.__wrapped__(check,envelope)==original
    assert len(reads)==count+1  # The old responsible validator actually replays.
    task=Path(envelope['engine_root'])/envelope['task_id']
    result=json.loads((task/'blobs'/(envelope['result_sha256']+'.bin')).read_bytes())
    path=task/'blobs'/(result['response_sha256']+'.bin')
    path.write_bytes(path.read_bytes()+b'changed')
    with pytest.raises(ValueError):validate_consumer_evidence(check,envelope)
    assert len(scene.provider.calls)==methods


@pytest.mark.parametrize('warm', [False, True])
def test_argon_memo_resolves_missing_source_and_keys_physical_location(scene,monkeypatch,warm):
    from pathlib import Path
    from src.autoslice import bound_media_recovery as recovery
    from src.autoslice.argon_escalation_integration import validate_consumer_evidence
    from src.autoslice.argon_escalation_witness import ArgonEscalationWitness
    scene.run();_,audit=scene.run(scene.build())
    check=audit['request'];envelope=check['argon_escalation_evidence']
    saved=deepcopy(envelope)
    source=Path(envelope['source_binding']['source_path'])
    copy=source.parent/'copy'/source.name;copy.parent.mkdir();copy.write_bytes(source.read_bytes())
    reads=[];resolutions=[];real=ArgonEscalationWitness.validate_evidence
    resolve=recovery.resolve_bound_media
    def validate(self,*args,**kwargs):
        reads.append(kwargs['expected'].source_path)
        return real(self,*args,**kwargs)
    def locate(*args,**kwargs):
        resolved=resolve(*args,**kwargs);resolutions.append(resolved);return resolved
    monkeypatch.setattr(ArgonEscalationWitness,'validate_evidence',validate)
    monkeypatch.setattr(recovery,'resolve_bound_media',locate)
    if warm:validate_consumer_evidence(check,envelope)
    source.unlink()
    expected=validate_consumer_evidence(check,envelope)
    assert reads[-1]==copy and not source.exists() and envelope==saved
    count=len(reads);lookups=len(resolutions);calls=len(scene.provider.calls)
    assert validate_consumer_evidence(check,envelope)==expected
    assert len(reads)==count and len(resolutions)>lookups
    # A new same-SHA physical copy must not inherit the previous copy's memo.
    later=source.parent/'later'/source.name;later.parent.mkdir();copy.rename(later)
    assert validate_consumer_evidence(check,envelope)==expected
    assert reads[-1]==later and len(reads)==count+1
    assert len(scene.provider.calls)==calls and envelope==saved


@pytest.mark.parametrize('damage', ['restored_original', 'recovered_bytes', 'native_response'])
def test_argon_recovery_memo_keeps_original_byte_and_receipt_guards(scene,damage):
    from pathlib import Path
    from src.autoslice.argon_escalation_integration import validate_consumer_evidence
    scene.run();_,audit=scene.run(scene.build())
    check=audit['request'];envelope=check['argon_escalation_evidence']
    source=Path(envelope['source_binding']['source_path'])
    copy=source.parent/'copy'/source.name;copy.parent.mkdir();copy.write_bytes(source.read_bytes())
    validate_consumer_evidence(check,envelope)
    source.unlink();validate_consumer_evidence(check,envelope)
    calls=len(scene.provider.calls)
    if damage=='restored_original':
        source.write_bytes(b'wrong restored bytes')
        reason='SOURCE_SHA_MISMATCH'
    elif damage=='recovered_bytes':
        copy.write_bytes(b'wrong recovered bytes')
        reason='BOUND_MEDIA_REACQUISITION_REQUIRED'
    else:
        task=Path(envelope['engine_root'])/envelope['task_id']
        result=json.loads((task/'blobs'/(envelope['result_sha256']+'.bin')).read_bytes())
        path=task/'blobs'/(result['response_sha256']+'.bin')
        path.write_bytes(path.read_bytes()+b'changed')
        reason='ARTIFACT_SHA_MISMATCH'
    with pytest.raises(ValueError,match=reason):validate_consumer_evidence(check,envelope)
    assert len(scene.provider.calls)==calls


def test_real_targeted_trigger_reuses_same_receipt_then_reopens_original(tmp_path,monkeypatch):
    from src.autoslice import argon_targeted_relisten as target
    rig=build_offline_rig(tmp_path,monkeypatch)
    _,check,finding,_=three_observations(rig)
    provider,integration=attach_argon(rig,monkeypatch)
    run_public(rig,finding,public_judge(rig))
    session=integration.session(check,finding)
    trigger=store.get_json(session.root/'trigger.json')
    calls=[];real=target._recommendation
    def recommend(*args,**kwargs):calls.append(1);return real(*args,**kwargs)
    monkeypatch.setattr(target,'_recommendation',recommend)
    expected=target.validate_trigger(session.root,session.spec,trigger,current_check=check)
    count=len(calls)
    assert target.validate_trigger(session.root,session.spec,trigger,current_check=check)==expected
    assert len(calls)==count
    assert target._validate_trigger_bytes.__wrapped__(session.root,session.spec,trigger,
        store.get_blob(session.root,trigger['evidence_sha256']),current_check=check) is None
    assert len(calls)==count+1
    path=session.root/'blobs'/(trigger['evidence_sha256']+'.bin')
    path.write_bytes(b'{}')
    with pytest.raises(ValueError,match='ARTIFACT_SHA_MISMATCH'):
        target.validate_trigger(session.root,session.spec,trigger,current_check=check)


def test_cold_inline_authority_skips_identity_walk_and_new_reference_is_not_blessed(tmp_path,monkeypatch):
    original={'history':[{'text':'current','unknown':None} for _ in range(1000)]}
    packed=evidence.persist_document(tmp_path/'source.json',{'request':{'raw':'real evidence'}})
    walk=evidence.expand;calls=[]
    def expand(value,**kwargs):calls.append(1);return walk(value,**kwargs)
    monkeypatch.setattr(evidence,'expand',expand)
    @evidence.cold_input
    def consume(value):return value
    assert consume(original) is original and not calls
    original['new']=packed['request']
    result=consume(original)
    assert result['new']=={'raw':'real evidence'} and calls
    from pathlib import Path
    Path(packed['request']['path']).write_bytes(b'{}')
    with pytest.raises(ValueError,match='ARTIFACT_SHA_MISMATCH'):consume(original)


def test_nested_absolute_store_and_explicit_colocation_keep_distinct_authority(tmp_path):
    from pathlib import Path
    external=evidence.persist_document(tmp_path/'external'/'doc.json',{'request':{'raw':'original retained answer'}})['request']
    local=tmp_path/'local'/'evidence-store'
    sha=store.blob(local,evidence.encoded({'nested':external}))
    ref={'schema_version':evidence.REF_SCHEMA,'sha256':sha,'path':str(local/'blobs'/(sha+'.bin'))}
    assert evidence.expand(ref)=={'nested':{'raw':'original retained answer'}}
    # An explicitly colocated review must never silently read an external store.
    with pytest.raises(ValueError,match='REVIEW_EVIDENCE_BLOB_MISSING'):
        evidence.review_from_bytes(evidence.encoded(ref),directory=local.parent)


def test_identical_validations_singleflight_without_serializing_other_keys():
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event,Lock
    both=Event();lock=Lock();entered=[]
    @memoized_validation(lambda key:key)
    def validate(key):
        with lock:
            entered.append(key)
            if set(entered)=={'first','second'}:both.set()
        assert both.wait(5),'independent validation keys were serialized'
        return {'key':key,'unknown':None}
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures=[pool.submit(validate,key) for key in ['first','second']*4]
        results=[future.result(timeout=10) for future in futures]
    assert sorted(entered)==['first','second']
    assert [r['key'] for r in results]==['first','second']*4
    assert len({id(r) for r in results})==8


def test_successful_verdict_keys_survive_more_than_32_distinct_receipts():
    calls=[]
    @memoized_validation(lambda key:key)
    def validate(key):calls.append(key)
    for _ in range(2):
        for key in map(str,range(100)):validate(key)
    assert len(calls)==100  # Verdict keys retain no raw/decoded result bodies.


def test_provenance_sidecar_and_permitted_alias_remain_live_dependencies(tmp_path):
    from src.autoslice.independent_context_adjudication import _read_json
    from src.autoslice.evidence_identity import file_sha256
    sidecar=tmp_path/'context.provenance.json';sidecar.write_text('{"source":"original"}')
    source=tmp_path/'source';source.write_bytes(b'original')
    other=tmp_path/'other';other.write_bytes(b'different')
    alias=tmp_path/'alias';alias.symlink_to(source)
    calls=[]
    @memoized_validation(lambda: 'sidecar-and-alias')
    def validate():
        calls.append(1)
        assert _read_json(str(sidecar))=={'source':'original'}
        return file_sha256(alias)
    assert validate()==file_sha256(source)
    assert validate()==file_sha256(source) and len(calls)==1
    sidecar.write_text('{"source":"changed"}')
    with pytest.raises(AssertionError):validate()
    sidecar.write_text('{"source":"original"}')
    alias.unlink();alias.symlink_to(other)
    assert validate()==file_sha256(other)
