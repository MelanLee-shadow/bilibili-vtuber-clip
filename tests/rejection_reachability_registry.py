"""Offline producer roundtrips and one-change counterexamples.

Responses are supplied at the real provider callback seam. No model/network is
used. A semantic field mutation may recompute its dependent hashes; that is one
changed producer value, not a second unrelated corruption.
"""
from copy import deepcopy
import json

from rejection_reachability_support import ReachabilityCase
from src.autoslice.entity_audio_verifier import _observe_entity_audio as REAL_OBSERVE

MODULES = (
    'argon_escalation_integration', 'entity_audio_verifier', 'context_assisted_audio',
    'acoustic_witness_adjudication', 'missing_proposal_bootstrap', 'semantic_audio_recovery',
    'exact_source_transcript_contract', 'final_review_discovery_prompt', 'final_review_schema_retry',
)
CASES = []


def register(function, code, producer, mutate, consume, accepted=lambda value: value is not None,
             description='one producer field changes'):
    CASES.append(ReachabilityCase(function, code, producer, mutate, consume, accepted, description))


def change(path, value, *, seal=None):
    def mutate(row):
        target = row
        for key in path[:-1]:
            target = target[key]
        target[path[-1]] = deepcopy(value)
        if seal:
            seal(row)
    return mutate


def rehash(row, field, digest):
    row.pop(field, None)
    row[field] = digest(row)


SRT = '1\n00:00:01,000 --> 00:00:02,000\n还没有歌杂呢\n\n2\n00:00:03,000 --> 00:00:04,000\n小李说歌债\n'
FINDING = {'cue_index': 1, 'suspect': '歌杂', 'suggestion': '歌债', 'proposed_full_cue': '还没有歌债呢',
           'repair_class': 'phonetic', 'why': '目标字音需核对'}


def check_request():
    from src.autoslice.final_review_auditor import build_context_adjudication_request
    return build_context_adjudication_request(SRT, deepcopy(FINDING), clip_context={
        'candidate_id': 'reachability-ordinary', 'source_media_sha256': 'a'*64,
        'pieces': [{'source_media_sha256':'a'*64}]})


def assisted_seed(tmp_path, monkeypatch):
    from src.autoslice import context_assisted_audio as a
    import test_targeted_relisten_public_contract as protocol
    from src.autoslice.final_review_auditor import build_context_adjudication_request
    rig=protocol.build_offline_rig(tmp_path,monkeypatch,srt=SRT,finding=deepcopy(FINDING),
                                   source_duration_ms=6000)
    witness=protocol.acquire_narrow(rig)
    # Use the ordinary request producer to carry a real saved blind receipt;
    # a made-up 64-character observation ID is not a blind prerequisite.
    check=build_context_adjudication_request(SRT,{**deepcopy(FINDING),
        '_prior_acoustic_observations':[{'request':rig.check,'verdict':witness}]},clip_context=rig.context)
    blind_ids=a.validate_prior_blind(check)
    plan = {'context_refs': ['cue:2'], 'question': '目标词尾音是什么', 'context_summary': '下句有歌债',
            'proper_names': [{'text': '小李', 'source_ref': 'cue:2'}], 'include_candidates': True}
    payload = a.build_assistance(check, plan, blind_observation_ids=blind_ids)
    raw = {'schema_version': a.assisted_response_contract().version, 'status': 'OBSERVED', 'target_audible': True,
           'heard': '还没有歌债呢', 'full_transcript': '还没有歌债呢',
           'segments': [{'segment': 'complete_block', 'start_ms': 0, 'end_ms': 1000, 'transcript': '还没有歌债呢'}],
           'inferred': [{'text': '歌债', 'source_refs': ['cue:2']}], 'uncertain': [], 'audio_observations': []}
    return {'check': check, 'plan': plan, 'ids': blind_ids, 'payload': payload, 'raw': raw}


def seal_assistance(row):
    from src.autoslice import context_assisted_audio as a
    rehash(row['payload'], 'assistance_sha256', a.digest)


def assistance_plan(row):
    from src.autoslice import context_assisted_audio as a
    return a.build_assistance(row['check'], row['plan'], blind_observation_ids=row['ids'])


def assistance_payload(row):
    from src.autoslice import context_assisted_audio as a
    return a.validate_assistance(row['payload'], row['check'])


def assistance_response(row):
    from src.autoslice import context_assisted_audio as a
    return a.validate_response(row['raw'], row['payload'])


for code, path, value in (
    ('ASSISTED_AUDIO_CONTEXT_REFS_INVALID', ['plan','context_refs'], ['cue:999']),
    ('ASSISTED_AUDIO_NAMES_INVALID', ['plan','proper_names'], None),
    ('ASSISTED_AUDIO_NAME_SOURCE_MISMATCH', ['plan','proper_names',0,'text'], '无来源的名字'),
    ('ASSISTED_AUDIO_QUESTION_REQUIRED', ['plan','question'], ''),
    ('ASSISTED_AUDIO_SUMMARY_INVALID', ['plan','context_summary'], None),
    ('ASSISTED_FIRST_OBSERVATION_MUST_BE_BLIND', ['ids'], []),
):
    register('context_assisted_audio.build_assistance', code, assisted_seed, change(path,value), assistance_plan)

for code, path, value, reseal in (
    ('ASSISTED_AUDIO_AUTHORITY_INVALID', ['payload','mutation_authorized'], True, True),
    ('ASSISTED_AUDIO_CONTEXT_HASH_DRIFT', ['payload','question'], 'changed', False),
    ('ASSISTED_AUDIO_CONTEXT_REFS_INVALID', ['payload','sources'], {}, True),
    ('ASSISTED_AUDIO_CONTEXT_SOURCE_DRIFT', ['payload','sources','cue:2','start_ms'], 3001, True),
    ('ASSISTED_AUDIO_CURRENT_CONTEXT_DRIFT', ['payload','anchor','physical_target_ms'], [1,2], True),
    ('ASSISTED_AUDIO_HYPOTHESIS_DRIFT', ['payload','hypotheses',0,'role'], 'ANSWER', True),
    ('ASSISTED_AUDIO_INPUT_INVALID', ['payload','prior_blind_observation_ids'], ['bad'], True),
    ('ASSISTED_AUDIO_NAME_SOURCE_MISMATCH', ['payload','proper_names',0,'text'], '无来源', True),
    ('ASSISTED_AUDIO_SOURCE_TEXT_DRIFT', ['payload','sources','cue:2','text'], 'different', True),
):
    register('context_assisted_audio.validate_assistance', code, assisted_seed,
             change(path,value,seal=seal_assistance if reseal else None), assistance_payload)

for code, path, value in (
    ('ASSISTED_AUDIO_INFERENCE_IN_TRANSCRIPT', ['raw','segments',0,'transcript'], '推断词'),
    ('ASSISTED_AUDIO_INFERENCE_SOURCE_INVALID', ['raw','inferred',0,'source_refs'], ['cue:999']),
    ('ASSISTED_AUDIO_RESPONSE_SCHEMA_INVALID', ['raw','schema_version'], 'other'),
    ('ASSISTED_AUDIO_UNCERTAINTY_INVALID', ['raw','uncertain'], [{'question':'?', 'audio_basis':''}]),
):
    register('context_assisted_audio.validate_response', code, assisted_seed, change(path,value), assistance_response)


def silent_seed(tmp_path, monkeypatch):
    row = assisted_seed(tmp_path, monkeypatch)
    row['raw'].update(heard='',full_transcript='',segments=[],inferred=[],uncertain=[{'question':'词界不清','audio_basis':'尾音模糊'}])
    return row


register('context_assisted_audio.validate_response','ASSISTED_AUDIO_NO_USABLE_OBSERVATION',silent_seed,
         change(['raw','uncertain'],[]), assistance_response)


def block_heard(row):
    from src.autoslice.context_assisted_audio import assisted_response_contract
    return assisted_response_contract().block_heard(row['raw'])


register('context_assisted_audio.AssistedResponseContract.block_heard','ASSISTED_AUDIO_RESPONSE_SCHEMA_INVALID',
         assisted_seed,change(['raw','heard'],'contradiction'),block_heard)


def assisted_prompt_match_seed(tmp_path, monkeypatch):
    from src.autoslice import context_assisted_audio as assisted
    row = assisted_seed(tmp_path, monkeypatch)
    request = {'schema_version': assisted.REQUEST_SCHEMA, 'target_audio_start_ms': 0,
        'target_audio_end_ms': 1000, 'timeline_binding': {}, 'assistance': row['payload']}
    return {'request': request, 'prompt': assisted.prompt(request)}


def assisted_prompt_match(row):
    from src.autoslice.context_assisted_audio import prompt_matches
    return prompt_matches(row['request'], row['prompt'])


register('context_assisted_audio.prompt_matches', '<FALSE>', assisted_prompt_match_seed,
    lambda row: row.update(prompt=row['prompt'].replace('actual audio takes precedence', 'context overrides audio')),
    assisted_prompt_match, lambda value: value is True,
    'ordinary assisted producer prompt must bind material and normative authority under version migration')


def anchor(row):
    from src.autoslice.context_assisted_audio import anchor
    return anchor(row['check'])


for code, path, value in (
    ('ASSISTED_AUDIO_CLOCK_INVALID',['check','matched_end_ms'],0),
    ('ASSISTED_AUDIO_SOURCE_BINDING_REQUIRED',['check','clip_context_binding','candidate_id'],''),
):
    register('context_assisted_audio.anchor',code,assisted_seed,change(path,value),anchor)


def cue_seed(tmp_path,monkeypatch):
    return {'row': {'cue':1}, 'count':2}


def cue_consume(row):
    from src.autoslice.final_review_schema_retry import normalize_finding_cue
    return normalize_finding_cue(row['row'],cue_count=row['count'])


for code, path, value in (
    ('ROW_NOT_OBJECT',['row'],[]),('CUE_MISSING_OR_INVALID',['row','cue'],None),('CUE_OUT_OF_RANGE',['row','cue'],3),
):
    register('final_review_schema_retry.normalize_finding_cue',code,cue_seed,change(path,value),cue_consume,
             lambda value: value == (1,None,None))


def proposal_seed(tmp_path, monkeypatch):
    monkeypatch.setenv('AUTOSLICE_BASE',str(tmp_path))
    return {'finding': deepcopy(FINDING), 'srt':SRT,
            'answer':{'status':'PROPOSED','proposed_cue':'还没有歌债呢','topic_summary':'欠歌','needs_audio':False,'reason':'保留整句只修局部'},
            'available': True}


def convergence_seed(tmp_path,monkeypatch):
    row=proposal_seed(tmp_path,monkeypatch)
    row['answer']={'decision':'REPLACE_WITH_EXACT_TEXT','replacement_text':'还没有歌债呢','needs_audio':False,'reason':'有界候选'}
    return row


def proposal_consume(row, convergence=False):
    from src.autoslice import missing_proposal_bootstrap as m
    from src.autoslice.final_review_auditor import _derive_single_span_edit
    def provider(prompt):
        if row.get('provider_error'):
            raise RuntimeError('synthetic transport failure')
        return json.dumps(row['answer'],ensure_ascii=False)
    kwargs=dict(srt_text=row['srt'],finding=row['finding'],structured_chat='',
                llm_call=provider if row['available'] else None,derive_single_span_edit=_derive_single_span_edit)
    if convergence:
        return m.converge_missing_proposal(**kwargs,proposal_audit={'status':'UNRESOLVED'})
    return m.bootstrap_missing_proposal(**kwargs)


for stage, producer, prefix, consume in (
    ('bootstrap_missing_proposal', proposal_seed, 'MISSING_PROPOSAL', proposal_consume),
    ('converge_missing_proposal', convergence_seed, 'CPA_CONVERGENCE', lambda row: proposal_consume(row,True)),
):
    common=[('CONTEXT_CUE_INDEX_INVALID',['finding','cue_index'],99),
            ('STALE_BASE',['finding','base_text_sha256'],'f'*64),
            (prefix+'_LLM_UNAVAILABLE',['available'],False),
            (prefix+'_PROVIDER_OR_JSON_UNAVAILABLE',['provider_error'],True),
            (prefix+'_TEXT_CONTRACT_INVALID',['answer','replacement_text' if stage.startswith('converge') else 'proposed_cue'],'bad\ntext')]
    for code,path,value in common:
        register('missing_proposal_bootstrap.'+stage,code,producer,change(path,value),consume,
                 lambda value: value[0] is not None and value[1]['status'] in {'PROPOSED','RESOLVED'})
for code,path,value in (
    ('SUSPECT_NOT_UNIQUE',['finding','suspect'],'missing'),
    ('SUSPECT_SPAN_INVALID',['finding','span_start_codepoint'],999),
    ('MISSING_PROPOSAL_DOES_NOT_COVER_SUSPECT',['answer','proposed_cue'],'还没有歌杂呀'),
):
    register('missing_proposal_bootstrap.bootstrap_missing_proposal',code,proposal_seed,change(path,value),proposal_consume,
             lambda value: value[0] is not None)
for code,path,value in (
    ('SUSPECT_NOT_FOUND',['finding','suspect'],'missing'),
    ('CPA_CONVERGENCE_DECISION_CONTRACT_INVALID',['answer','decision'],'OTHER'),
):
    register('missing_proposal_bootstrap.converge_missing_proposal',code,convergence_seed,change(path,value),
             lambda row: proposal_consume(row,True),lambda value:value[0] is not None)


def keep_seed(tmp_path,monkeypatch):
    row=convergence_seed(tmp_path,monkeypatch)
    row['answer'].update(decision='KEEP_EXISTING',replacement_text='')
    return row


for code,path,value in (
    ('CPA_CONVERGENCE_KEEP_REQUIRES_AUDIO',['answer','needs_audio'],True),
    ('CPA_CONVERGENCE_UNRESOLVED',['answer','decision'],'UNRESOLVED'),
):
    register('missing_proposal_bootstrap.converge_missing_proposal',code,keep_seed,change(path,value),
             lambda row:proposal_consume(row,True),lambda value:value[0] is not None)


def argon_seed(tmp_path, monkeypatch, ordinary_status='UNCERTAIN'):
    import test_argon_consumer_integration as fixture
    from src.autoslice import argon_escalation_integration as a
    from src.autoslice.acoustic_witness_adjudication import build_witness_request
    scene=fixture.scene.__wrapped__(tmp_path,monkeypatch)
    scene.ordinary_status=ordinary_status
    scene.run()
    _,audit=scene.run()
    assert audit['repaired'], audit
    check=deepcopy(audit['request'])
    envelope=deepcopy(check['argon_escalation_evidence'])
    owner=scene.verifier.argon_escalation
    session=owner.session(check,scene.finding)
    wr=build_witness_request(check)
    manifest_path=scene.out/'entity_verdicts'/wr['request_sha256'][:20]/'verdict.manifest.json'
    manifest=json.loads(manifest_path.read_bytes())
    history_path=next((session.root/'ordinary-history').glob('*.json'))
    return dict(scene=scene,check=check,envelope=envelope,owner=owner,session=session,spec=session.spec,
                wr=wr,manifest_path=manifest_path,manifest=manifest,verdict=deepcopy(manifest['verdict']),
                event=json.loads(history_path.read_bytes()),history_path=history_path)


def seal_check(row):
    from src.autoslice.argon_escalation_contract import canonical,sha
    rehash(row['check'],'request_sha256',lambda value:sha(canonical(value)))


def seal_envelope(row):
    from src.autoslice.argon_escalation_contract import canonical,sha
    rehash(row['envelope'],'envelope_sha256',lambda value:sha(canonical(value)))


def consumer(row):
    from src.autoslice.argon_escalation_integration import validate_consumer_evidence
    return validate_consumer_evidence(row['check'],row['envelope'])


for code,path,value,seal in (
    ('ARGON_CONSUMER_SCHEMA_INVALID',['envelope','schema_version'],'other',seal_envelope),
    ('ARGON_CONSUMER_ENVELOPE_DRIFT',['envelope','envelope_sha256'],'0'*64,None),
    ('ARGON_CONSUMER_FINDING_DRIFT',['check','argon_finding_id'],'0'*64,seal_check),
    ('ARGON_CONSUMER_CONTEXT_DRIFT',['check','argon_context_source_ms'],[0,1],seal_check),
    ('ARGON_CONSUMER_SOURCE_DRIFT',['check','argon_source_media_sha256'],'0'*64,seal_check),
    ('ARGON_CONSUMER_TARGET_DRIFT',['envelope','source_binding','target_source_ms'],[1,2],seal_envelope),
):
    register('argon_escalation_integration.validate_consumer_evidence',code,argon_seed,change(path,value,seal=seal),consumer)


def remove_trigger(row):
    (row['session'].root/'trigger.json').unlink()


def drift_original_binding(row):
    path=row['session'].root/'source-binding.json'
    value=json.loads(path.read_bytes());value['source_path']+='-other'
    path.write_text(json.dumps(value))


register('argon_escalation_integration.validate_consumer_evidence','ARGON_TRIGGER_MISSING',argon_seed,remove_trigger,consumer)
register('argon_escalation_integration.validate_consumer_evidence','ARGON_ORIGINAL_SOURCE_BINDING_DRIFT',argon_seed,drift_original_binding,consumer)


def bare_check_seed(tmp_path,monkeypatch):
    return {'check':check_request()}


def check_consume(row):
    from src.autoslice.argon_escalation_integration import _check_request
    _check_request(row['check'])
    return True


for code,path,value,seal in (
    ('ARGON_CHECK_REQUEST_INVALID',['check'],None,None),
    ('ARGON_CHECK_REQUEST_HASH_DRIFT',['check','request_sha256'],'0'*64,None),
    ('ARGON_CURRENT_CUE_DRIFT',['check','current_cue'],'changed',seal_check),
    ('ARGON_CURRENT_CUE_INVALID',['check','cue_indexes'],[999],seal_check),
    ('ARGON_SOURCE_CLOCK_INVALID',['check','source_media_timeline_offset_ms'],-1,seal_check),
):
    register('argon_escalation_integration._check_request',code,bare_check_seed,change(path,value,seal=seal),check_consume)


def config_seed(tmp_path,monkeypatch):
    return {'config':{'enabled':True,'allow_paid':False,'request_timeout_s':30}}


def config_consume(row):
    from src.autoslice.argon_escalation_integration import argon_config_contract
    return argon_config_contract(row['config'])


for code,path,value in (
    ('ARGON_CONFIG_INVALID',['config','request_timeout_s'],-1),
    ('ARGON_OPT_IN_INVALID',['config','enabled'],'true'),
):
    register('argon_escalation_integration.argon_config_contract',code,config_seed,change(path,value),config_consume)


def persist_event(row):
    from src.autoslice.argon_escalation_contract import canonical,sha
    rehash(row['event'],'event_sha256',lambda value:sha(canonical(value)))
    row['history_path'].write_bytes(canonical(row['event']))


def event_change(path,value,*,reseal=True):
    def mutate(row):
        change(['event',*path],value)(row)
        if reseal:
            persist_event(row)
        else:
            row['history_path'].write_text(json.dumps(row['event']))
    return mutate


def history(row):
    from src.autoslice.argon_escalation_integration import read_ordinary_history
    return read_ordinary_history(row['session'].root,row['spec'])


for code,path,value,reseal in (
    ('ARGON_ORDINARY_HISTORY_DRIFT',['finding','finding_id'],'0'*64,False),
    ('ARGON_ORDINARY_OTHER_FINDING',['finding','finding_id'],'0'*64,True),
    ('ARGON_ORDINARY_HISTORY_REQUEST',['ordinary','witness_request','request_sha256'],'0'*64,True),
    ('ARGON_ORDINARY_HISTORY_STATUS',['ordinary','raw_status'],'OBSERVED',True),
    ('ARGON_ORDINARY_JUDGE_PHASE_DRIFT',['judge_after_audio'],True,True),
    ('ARGON_ORDINARY_HISTORY_CACHE_REPLAY',['ordinary','cache_replay'],{'schema_version':'other'},True),
):
    register('argon_escalation_integration.read_ordinary_history',code,argon_seed,event_change(path,value,reseal=reseal),history)


def history_artifact_change(name,path,value):
    def mutate(row):
        from src.autoslice import argon_escalation_storage as store
        from src.autoslice.argon_escalation_contract import canonical
        ref=row['event']['ordinary']['artifacts'][name]
        raw=store.get_blob(row['session'].root,ref)
        if path is None:
            changed=raw+b'drift'
        else:
            changed=json.loads(raw)
            change(path,value)(changed)
            changed=canonical(changed)
        row['event']['ordinary']['artifacts'][name]=store.blob(row['session'].root,changed)
        persist_event(row)
    return mutate


for code,name,path,value in (
    ('ARGON_ORDINARY_HISTORY_BYTES','prompt',None,None),
    ('ARGON_ORDINARY_HISTORY_MODEL','manifest',['provider'],'unknown'),
    ('ARGON_ORDINARY_HISTORY_TARGET','manifest',['timeline_binding','source_media','target_start_ms'],1),
):
    register('argon_escalation_integration.read_ordinary_history',code,argon_seed,history_artifact_change(name,path,value),history)


def judge_record_change(row):
    from src.autoslice import argon_escalation_storage as store
    from src.autoslice.argon_escalation_contract import canonical
    decision=json.loads(store.get_blob(row['session'].root,row['event']['judge_record']))
    decision['completion_sha256']='0'*64
    row['event']['judge_record']=store.blob(row['session'].root,canonical(decision))
    persist_event(row)


register('argon_escalation_integration.read_ordinary_history','ARGON_ORDINARY_HISTORY_JUDGE_INVALID',argon_seed,judge_record_change,history)


def trigger_change(field,value):
    def mutate(row):
        path=row['session'].root/'trigger.json'
        payload=json.loads(path.read_bytes());payload[field]=deepcopy(value)
        path.write_text(json.dumps(payload))
    return mutate


def trigger_consume(row):
    from src.autoslice.argon_escalation_integration import validate_trigger
    return validate_trigger(row['session'].root,row['spec'])


for code,field,value in (
    ('ARGON_TRIGGER_SCHEMA_INVALID','schema_version','other'),
    ('ARGON_TRIGGER_HISTORY_INVALID','finding',{}),
):
    register('argon_escalation_integration.validate_trigger',code,argon_seed,trigger_change(field,value),trigger_consume)


def source_drift(row):
    row['scene'].source.write_bytes(row['scene'].source.read_bytes()+b'drift')


register('argon_escalation_integration.ArgonIntegration.session','ARGON_RUNTIME_SOURCE_DRIFT',argon_seed,source_drift,
         lambda row:row['owner'].session(row['check'],row['scene'].finding))


def source_binding_change(row):
    path=row['session'].root/'source-binding.json'
    payload=json.loads(path.read_bytes());payload['context_source_ms']=[0,1]
    path.write_text(json.dumps(payload))


def session_init(row):
    from src.autoslice.argon_escalation_integration import FindingSession
    return FindingSession(row['owner'],row['session'].root,row['spec'])


register('argon_escalation_integration.FindingSession.__init__','IMMUTABLE_ARTIFACT_CONFLICT',argon_seed,source_binding_change,session_init)


def ordinary(row):
    return row['session']._ordinary(row['check'],row['verdict'])


def manifest_change(path,value):
    def mutate(row):
        change(['manifest',*path],value)(row)
        row['manifest_path'].write_text(json.dumps(row['manifest']))
    return mutate


for code,path,value in (
    ('ARGON_ORDINARY_MANIFEST_DRIFT',['source_media_sha256'],'0'*64),
    ('ARGON_ORDINARY_CLOCK_DRIFT',['timeline_binding','source_media','target_start_ms'],1),
    ('ARGON_ORDINARY_AUDIO_PATH',['audio_clip'],'/private/tmp/foreign-input.mp4'),
):
    register('argon_escalation_integration.FindingSession._ordinary',code,argon_seed,manifest_change(path,value),ordinary)
register('argon_escalation_integration.FindingSession._ordinary','ARGON_ORDINARY_VERDICT_DRIFT',argon_seed,
         change(['verdict','reason'],'changed'),ordinary)


def drift_artifact(name):
    def mutate(row):
        from src.autoslice.argon_escalation_contract import file_sha
        field='audio_clip_sha256' if name=='audio' else name+'_sha256'
        for path in row['manifest_path'].parent.iterdir():
            if path.is_file() and file_sha(path)==row['manifest'][field]:
                path.write_bytes(path.read_bytes()+b'drift')
    return mutate


for name in ('audio','prompt','response'):
    register('argon_escalation_integration.FindingSession._ordinary','ARGON_ORDINARY_'+name.upper()+'_DRIFT',
             argon_seed,drift_artifact(name),ordinary)


def entity_seed(tmp_path,monkeypatch):
    from src.autoslice import entity_audio_verifier as e
    source=tmp_path/'source.mp4';source.write_bytes(b'offline media transport fixture')
    output=tmp_path/'out';output.mkdir()
    row={'request':check_request(),'check':check_request(),'provider':'agy','model':'gemini-3.8-flash',
         'observed':{'schema_version':'entity-audio-observation.v1','status':'OBSERVED','target_audible':True,
                     'heard_syllables':'hai mei you ge zhai ne','current_fit':'INCOMPATIBLE','proposed_fit':'SUPPORTED',
                     'confidence_current':0.2,'confidence_proposed':0.9,'reason':'synthetic acoustic observation'},
         'crop_ok':True}
    def crop(**kwargs):
        kwargs['audio_path'].parent.mkdir(parents=True,exist_ok=True)
        kwargs['audio_path'].write_bytes(b'offline cropped transport fixture')
        return row['crop_ok'], 'synthetic crop failure' if not row['crop_ok'] else ''
    def observe(**kwargs):
        prompt,_,_=e._entity_observation_prompt(request=kwargs['request'],candidates=kwargs['candidates'],
            recording_date=kwargs['recording_date'],timely_context=kwargs['timely_context'])
        pp=kwargs['job_dir']/'prompt.md';pp.write_text(prompt)
        rp=kwargs['job_dir']/'verdict.raw.json';rp.write_text(json.dumps(row['observed'],ensure_ascii=False))
        result=e._EntityProviderOutcome(row['observed'],row['provider'],row['model'],pp,rp,None,None,[])
        row['outcome']=result;row['audio_path']=kwargs['audio_path']
        return result
    monkeypatch.setattr(e,'_crop_black_frame_audio',crop)
    monkeypatch.setattr(e,'_observe_entity_audio',observe)
    row['verifier']=e.build_local_audio_entity_verifier(source_media=source,output_dir=output,
        recording_date='2026-10-22',source_duration_ms=6000,agy_bin='/usr/bin/true',model=row['model'])
    from src.autoslice.clip_context import build_clip_context
    from src.autoslice.speech_memory_ledger import SCHEMA_VERSION
    from src.autoslice.final_review_auditor import build_context_adjudication_request
    memory=tmp_path/'memory.json';memory.write_text(json.dumps({'schema_version':SCHEMA_VERSION,'entries':[]}))
    row['context']=build_clip_context(candidate_id='reachability-entity',
        spec={'date':'2026-10-22','pieces':[{'start_ms':0,'end_ms':6000,'source_media_sha256':row['verifier'].source_sha256}]},
        draft_srt=SRT,authoritative_chat=(),topic_resolution={},session_topic_authorities=(),
        speech_memory_ledger_path=memory,source_truth_ledger_path=None)
    row['check']=build_context_adjudication_request(SRT,deepcopy(FINDING),clip_context=row['context'])
    row['request']=deepcopy(row['check'])
    return row


def entity_consume(row):
    return row['verifier'](row['request'])


def seal_entity_request(row):
    from src.autoslice.entity_audio_verifier import _json_sha256
    rehash(row['request'],'request_sha256',_json_sha256)


for code,path,value,seal in (
    ('ENTITY_AUDIO_REQUEST_INVALID',['request','request_sha256'],'bad',None),
    ('ENTITY_AUDIO_REQUEST_HASH_MISMATCH',['request','request_sha256'],'0'*64,None),
    ('ENTITY_AUDIO_CANDIDATES_INVALID',['request','candidate_entities'],[],seal_entity_request),
    ('ENTITY_AUDIO_TIMELINE_OFFSET_INVALID',['request','source_media_timeline_offset_ms'],-1,seal_entity_request),
    ('ENTITY_AUDIO_SPAN_INVALID',['request','matched_start_ms'],-10,seal_entity_request),
    ('ENTITY_AUDIO_CROP_FAILED',['crop_ok'],False,None),
    ('ENTITY_AUDIO_PROVIDER_FAILED',['observed'],None,None),
    ('ENTITY_AUDIO_UNCERTAIN',['observed','current_fit'],'WRONG',None),
):
    register('entity_audio_verifier._verify_local_audio_request',code,entity_seed,change(path,value,seal=seal),
             entity_consume,lambda value:value.get('status')=='OBSERVED')


def witness_seed(tmp_path,monkeypatch):
    from src.autoslice.acoustic_witness_adjudication import build_witness_request
    row=entity_seed(tmp_path,monkeypatch)
    row['request']=build_witness_request(row['check'])
    row['observed']={'schema_version':'subtitle-span-acoustic-witness.v1','status':'OBSERVED','target_audible':True,
                     'heard_pinyin':'hai mei you ge zhai ne','uncertain_positions':[],'syllable_count':6,
                     'confidence':0.9,'reason':'synthetic target syllables'}
    return row


for code,path,value in (
    ('WITNESS_REQUEST_PROTOCOL_INVALID',['request','witness_protocol'],'wrong'),
    ('WITNESS_REQUEST_CARRIES_CANDIDATES',['request','proposed_cue'],'priming'),
):
    register('entity_audio_verifier._verify_local_audio_request',code,witness_seed,change(path,value,seal=seal_entity_request),
             entity_consume,lambda value:value.get('status')=='OBSERVED')


def exact_seed(tmp_path,monkeypatch):
    from src.autoslice.exact_source_transcript_contract import build_exact_source_transcript_request,PROVIDER_REPORT_SCHEMA
    row=witness_seed(tmp_path,monkeypatch)
    row['request']=build_exact_source_transcript_request(row['request'])
    row['observed']={'schema_version':PROVIDER_REPORT_SCHEMA,'status':'OBSERVED','target_audible':True,
                     'audible_language':'zh','exact_transcript':'还没有歌债呢','reason':'synthetic target transcript'}
    return row


register('entity_audio_verifier._verify_local_audio_request','EXACT_SOURCE_TRANSCRIPT_REQUEST_INVALID',exact_seed,
         change(['request','cue_indexes'],[0],seal=seal_entity_request),entity_consume,lambda value:value.get('status')=='OBSERVED')
for code,path,value in (
    ('EXACT_SOURCE_TRANSCRIPT_PROVIDER_ROUTE_INVALID',['provider'],'other'),
    ('EXACT_SOURCE_TRANSCRIPT_REPORT_INVALID',['observed','audible_language'],'other'),
):
    register('entity_audio_verifier._persist_exact_source_transcript',code,exact_seed,change(path,value),entity_consume,
             lambda value:value.get('status')=='OBSERVED')
register('entity_audio_verifier._subtitle_acoustic_verdict','ENTITY_AUDIO_UNCERTAIN',entity_seed,
         change(['observed','current_fit'],'WRONG'),entity_consume,lambda value:value.get('status')=='OBSERVED')


def word_seed(tmp_path,monkeypatch):
    row=witness_seed(tmp_path,monkeypatch)
    row['witness']=entity_consume(row)
    row['answer']={'choice':'PROPOSED','needs_audio':False,'reason':'target and context support the proposal'}
    row['llm_available']=True
    return row


def word_provider(row):
    def call(prompt):
        row['prompt']=prompt
        if row.get('provider_error'):
            raise RuntimeError('synthetic transport failure')
        return json.dumps(row['answer'],ensure_ascii=False)
    if row.get('identity'):
        call.cpa_cache_identity=row['identity']
    return call


def word_consume(row):
    from src.autoslice.acoustic_witness_adjudication import _judge_word_choice_body
    return _judge_word_choice_body(llm_call=word_provider(row),check_request=row['check'],witness=row['witness'])


def current_word_seed(tmp_path,monkeypatch):
    row=word_seed(tmp_path,monkeypatch)
    row['check']['require_complete_utterance_support']=True;seal_check(row)
    row['answer'].update(choice='CURRENT',current_utterance_supported=True,current_utterance_support_reason='current full utterance supported')
    return row


def transcript_word_seed(tmp_path,monkeypatch):
    row=word_seed(tmp_path,monkeypatch)
    from src.autoslice.native_context_witness import _native_transcript_witness
    # The exact transcript is sealed by the real provider path, then supplied
    # as its candidate-blind witness representation (not invented pinyin).
    row['witness']={**row['witness'],'witness_protocol':'candidate_blind_transcript','candidate_exposure':'none',
                    'exact_transcript':'还没有歌债呢'}
    for key in ('heard_pinyin','confidence','syllable_count','uncertain_positions'):
        row['witness'].pop(key,None)
    row['answer']['candidate_id']='PROPOSAL'
    return row


for code,producer,path,value in (
    ('JUDGE_CALL_FAILED',word_seed,['provider_error'],True),
    ('JUDGE_CHOICE_OUT_OF_SET',word_seed,['answer','choice'],'OTHER'),
    ('CURRENT_UTTERANCE_SUPPORT_REQUIRED',current_word_seed,['answer','current_utterance_support_reason'],''),
    ('JUDGE_CANDIDATE_ID_OUT_OF_SET',transcript_word_seed,['answer','candidate_id'],'SOURCE_999'),
):
    register('acoustic_witness_adjudication._judge_word_choice_body',code,producer,change(path,value),word_consume,
             lambda value:value.get('status')=='JUDGED')


def adjudicate_consume(row):
    from src.autoslice.acoustic_witness_adjudication import _adjudicate_with_witness_body
    return _adjudicate_with_witness_body(check_request=row['check'],witness=row['witness'],
        llm_call=word_provider(row) if row['llm_available'] else None)


for code,path,value in (
    ('JUDGE_UNAVAILABLE_KEEP_CURRENT',['llm_available'],False),
    ('JUDGE_REQUESTS_MORE_AUDIO',['answer','needs_audio'],True),
    ('JUDGE_REJECTS_CLOSED_SET',['answer','choice'],'NEITHER'),
    ('JUDGE_UNCERTAIN_KEEP_CURRENT',['answer','choice'],'OTHER'),
    ('JUDGE_KEEPS_CURRENT',['answer','choice'],'CURRENT'),
):
    register('acoustic_witness_adjudication._adjudicate_with_witness_body',code,word_seed,change(path,value),
             adjudicate_consume,lambda value:value[0] is True)


def witness_admission(row):
    from src.autoslice.acoustic_witness_adjudication import witness_judgment_contract
    error=witness_judgment_contract(row['witness']).admission_error()
    return {'reason_code':error} if error else True


for code,producer,path,value in (
    ('WITNESS_UNAVAILABLE_KEEP_CURRENT',word_seed,['witness','schema_version'],'other'),
    ('TRANSCRIPT_OBSERVATION_INVALID_KEEP_CURRENT',transcript_word_seed,['witness','candidate_exposure'],'sighted'),
    ('LEGACY_SIGHTED_WITNESS_NOT_REUSABLE',word_seed,['witness','witness_protocol'],'legacy_sighted'),
):
    register('acoustic_witness_adjudication.WitnessJudgmentContract.admission_error',code,producer,change(path,value),witness_admission,
             lambda value:value is True)


def semantic_seed(tmp_path,monkeypatch):
    import test_targeted_relisten_public_contract as protocol
    import test_semantic_audio_recovery as fixture
    from types import SimpleNamespace
    rig=protocol.build_offline_rig(tmp_path,monkeypatch,srt=fixture.SOURCE,finding=fixture.FINDING,
                                   offset_ms=30000,source_duration_ms=40000)
    rig.transport.pinyin='le'
    finding,check,witness=fixture.full_case(rig)
    row={'rig':rig,'finding':finding,'check':deepcopy(check),'witness':witness,'srt':fixture.SOURCE,
         'plan':fixture.range_plan(),'available':True,'budget_mode':'normal','budget_reads':0}
    def observe(**kwargs):
        if row.get('adapter_error'):
            raise RuntimeError('synthetic adapter failure')
        if 'adapter_result' in row:
            return row['adapter_result']
        return rig.verifier.observe_range_context(**kwargs)
    real=rig.verifier.observe_range_context
    observe.available_providers=getattr(real,'available_providers',('gemini',))
    observe.context_assisted_providers=getattr(real,'context_assisted_providers',('gemini',))
    def budget():
        row['budget_reads']+=1
        if row['budget_mode']=='pre_error' or row['budget_mode']=='post_error' and row['budget_reads']>1:
            raise OSError('synthetic ledger read failure')
        if row['budget_mode']=='none':
            return None
        if row['budget_mode']=='post_missing' and row['budget_reads']>1:
            return {'receipt':None}
        return rig.verifier.relisten_budget_state()
    row['verifier']=SimpleNamespace(observe_range_context=observe,relisten_budget_state=budget,
        probe_witness_cache=rig.verifier.probe_witness_cache)
    return row


def semantic_consume(row):
    from src.autoslice.semantic_audio_recovery import recover_semantic_audio_range
    def provider(prompt):
        if row.get('provider_error'):
            raise RuntimeError('synthetic planner failure')
        from test_targeted_relisten_public_contract import planner_data
        plan=row['plan'](planner_data(prompt)) if callable(row['plan']) else row['plan']
        return json.dumps(plan,ensure_ascii=False)
    return recover_semantic_audio_range(srt_text=row['srt'],finding=row['finding'],check_request=row['check'],
        entity_verifier=row['verifier'] if row['available'] else None,llm_call=provider,
        argon_session=row.get('argon_session'))


for code,path,value,seal in (
    ('SEMANTIC_RANGE_CURRENT_GATE_DRIFT',['check','request_sha256'],'0'*64,None),
    ('SEMANTIC_RANGE_OBSERVER_UNAVAILABLE',['available'],False,None),
    ('SEMANTIC_RANGE_TARGET_INVALID',['check','cue_indexes'],[],seal_check),
    ('SEMANTIC_RANGE_PLAN_INVALID',['plan','first_cue'],999,None),
    ('SEMANTIC_RANGE_PLAN_FAILED',['provider_error'],True,None),
    ('SEMANTIC_RANGE_TIMELINE_INVALID',['plan','before_ms'],35000,None),
    ('RELISTEN_OBSERVATION_CLASS_INVALID',['plan','observation_class'],'other',None),
    ('RELISTEN_PRE_DISPATCH_LEDGER_UNAVAILABLE',['budget_mode'],'pre_error',None),
    ('RELISTEN_POST_DISPATCH_LEDGER_UNAVAILABLE',['budget_mode'],'post_error',None),
    ('SEMANTIC_RANGE_OBSERVATION_FAILED',['adapter_error'],True,None),
    ('SEMANTIC_RANGE_OBSERVATION_INVALID',['adapter_result'],{},None),
    ('SEMANTIC_RANGE_NO_VALID_CONTEXT_EVIDENCE',['adapter_result'],[],None),
    ('RELISTEN_DISPATCH_PROOF_REQUIRED',['budget_mode'],'none',None),
):
    register('semantic_audio_recovery.recover_semantic_audio_range',code,semantic_seed,change(path,value,seal=seal),
             semantic_consume,lambda value:value[1] and value[1]['status']=='OBSERVED')


def blind_seed(tmp_path,monkeypatch):
    row=semantic_seed(tmp_path,monkeypatch)
    _,audit=semantic_consume(row)
    assert audit['status']=='OBSERVED',audit
    row['check']=audit['consumer_check_request']
    return row


def blind_consume(row):
    from src.autoslice.context_assisted_audio import validate_prior_blind
    return validate_prior_blind(row['check'])


register('context_assisted_audio.validate_prior_blind','ASSISTED_FIRST_OBSERVATION_MUST_BE_BLIND',blind_seed,
         change(['check','prior_acoustic_observations'],[]),blind_consume)


def assisted_request_seed(tmp_path,monkeypatch):
    row=assisted_seed(tmp_path,monkeypatch)
    from src.autoslice import context_assisted_audio as a
    check=row['check']
    row['request']={'schema_version':a.REQUEST_SCHEMA,'target_audio_start_ms':0,'target_audio_end_ms':1000,
                    'timeline_binding':{
                        'schema_version':'subtitle-audio-timeline-binding.v1','source_media_timeline_offset_ms':0,
                        'delivery_local':{'target_start_ms':1000,'target_end_ms':2000},
                        'source_media':{'target_start_ms':1000,'target_end_ms':2000,'crop_start_ms':1000,'crop_end_ms':2000}},
                    'assistance':row['payload']}
    return row


def assisted_prompt_consume(row):
    from src.autoslice.context_assisted_audio import prompt
    return prompt(row['request'])


for code,path,value in (
    ('ASSISTED_AUDIO_CLOCK_INVALID',['request','target_audio_end_ms'],0),
    ('ASSISTED_AUDIO_REQUEST_SCHEMA_INVALID',['request','schema_version'],'other'),
):
    register('context_assisted_audio.prompt',code,assisted_request_seed,change(path,value),assisted_prompt_consume)


def repair_cue_consume(row):
    from src.autoslice.final_review_schema_retry import schema_repair_new_finding_diagnostic
    return schema_repair_new_finding_diagnostic({1,2},row['row']['cue'])


register('final_review_schema_retry.schema_repair_new_finding_diagnostic','SCHEMA_REPAIR_NEW_FINDING_FORBIDDEN',
         cue_seed,change(['row','cue'],3),repair_cue_consume,lambda value:value is None)


def ordinary_response_change(field,value):
    def mutate(row):
        from src.autoslice.argon_escalation_contract import file_sha
        manifest=row['manifest']
        paths=[p for p in row['manifest_path'].parent.glob('verdict.raw*')
               if p.is_file() and file_sha(p)==manifest['response_sha256']]
        assert paths
        raw=json.loads(paths[0].read_bytes());raw[field]=deepcopy(value)
        for path in paths:
            path.write_text(json.dumps(raw))
        manifest['response_sha256']=file_sha(paths[0])
        row['manifest_path'].write_text(json.dumps(manifest))
    return mutate


for code,field,value in (
    ('ARGON_ORDINARY_NOT_BLIND','current_cue','primed'),
    ('ARGON_ORDINARY_UNCERTAIN_INVALID','reason',''),
):
    register('argon_escalation_integration.FindingSession._ordinary',code,argon_seed,
             ordinary_response_change(field,value),ordinary)


def history_judge_change(field,value):
    def mutate(row):
        from src.autoslice import argon_escalation_storage as store
        from src.autoslice.argon_escalation_contract import canonical,sha
        record=json.loads(store.get_blob(row['session'].root,row['event']['judge_record']))
        payload=json.loads(record['completion']);payload[field]=deepcopy(value)
        record['completion']=json.dumps(payload,ensure_ascii=False)
        record['completion_sha256']=sha(record['completion'].encode())
        row['event']['judge_record']=store.blob(row['session'].root,canonical(record))
        persist_event(row)
    return mutate


register('argon_escalation_integration.validate_trigger','ARGON_DIAGNOSED_TRIGGER_REQUIRED',argon_seed,
         history_judge_change('argon_escalation',{'reading_chat':False,'lyrics_reference_conflict':False,'reason':'neither diagnosis'}),trigger_consume)


def config_runtime_seed(tmp_path,monkeypatch):
    from src.autoslice import argon_escalation_runtime as runtime
    monkeypatch.setattr(runtime,'load_credentials',lambda _: {})
    monkeypatch.delenv('AUTOSLICE_BASE',raising=False)
    source=tmp_path/'source';source.write_bytes(b'synthetic media')
    return {'source':source,'root':tmp_path,'runtime_root':tmp_path/'runtime'}


def config_runtime_consume(row):
    from src.autoslice.argon_escalation_integration import build_argon_integration
    return build_argon_integration(config={'enabled':True},source_media=row['source'],source_duration_ms=6000,
                                  ordinary_output_dir=row['root']/'out',runtime_root=row['runtime_root'])


register('argon_escalation_integration.build_argon_integration','ARGON_RUNTIME_REQUIRED',config_runtime_seed,
         change(['runtime_root'],None),config_runtime_consume)


def valid_argon_seed(tmp_path,monkeypatch):
    from src.autoslice.argon_escalation_integration import transcript_witness
    row=argon_seed(tmp_path,monkeypatch)
    row['witness']=transcript_witness(row['check'],row['envelope'])
    return row


def valid_argon_consume(row):
    from src.autoslice.argon_escalation_integration import valid_argon_witness
    return valid_argon_witness(row['check'],row['witness'])


for code,path,value in (
    ('ARGON_WITNESS_ENVELOPE_DRIFT',['witness','argon_escalation_evidence'],{}),
    ('ARGON_WITNESS_PROJECTION_DRIFT',['witness','exact_transcript'],'changed'),
):
    register('argon_escalation_integration.valid_argon_witness',code,valid_argon_seed,change(path,value),valid_argon_consume,
             lambda value:value is True)


def missing_blind_source(row):
    row['check']['clip_context_binding']['source_media_sha256s']=['0'*64]


register('context_assisted_audio.validate_prior_blind','ASSISTED_FIRST_OBSERVATION_MUST_BE_BLIND',blind_seed,
         missing_blind_source,blind_consume)


def plan_name_consume(row):
    from src.autoslice.context_assisted_audio import assistance_plan_contract
    return assistance_plan_contract(row['check']).valid_name(row['plan']['proper_names'][0],row['payload']['sources'],closed=True,strip=True)


register('context_assisted_audio.AssistancePlanContract.valid_name','<FALSE>',assisted_seed,
         change(['plan','proper_names',0,'text'],'missing'),plan_name_consume,lambda value:value is True)


def inference_consume(row):
    from src.autoslice.context_assisted_audio import assisted_response_contract
    return assisted_response_contract(row['payload']).valid_inference(row['raw']['inferred'][0])


register('context_assisted_audio.AssistedResponseContract.valid_inference','<FALSE>',assisted_seed,
         change(['raw','inferred',0,'source_refs'],['cue:999']),inference_consume,lambda value:value is True)


def proposal_text_consume(row):
    from src.autoslice.missing_proposal_bootstrap import missing_proposal_answer_contract
    return missing_proposal_answer_contract('还没有歌杂呢').valid_text(row['answer']['proposed_cue'])


register('missing_proposal_bootstrap.MissingProposalAnswerContract.valid_text','<FALSE>',proposal_seed,
         change(['answer','proposed_cue'],'bad\ntext'),proposal_text_consume,lambda value:value is True)


def exact_request_consume(row):
    from src.autoslice.exact_source_transcript_contract import build_exact_source_transcript_request
    return build_exact_source_transcript_request(row['request'])


register('exact_source_transcript_contract.build_exact_source_transcript_request','EXACT_SOURCE_TRANSCRIPT_GEOMETRY_INVALID',
         witness_seed,change(['request','cue_indexes'],[0]),exact_request_consume)


def valid_exact_request_consume(row):
    from src.autoslice.exact_source_transcript_contract import valid_exact_source_transcript_request
    return valid_exact_source_transcript_request(row['request'])


register('exact_source_transcript_contract.valid_exact_source_transcript_request','<FALSE>',exact_seed,
         change(['request','cue_indexes'],[0]),valid_exact_request_consume,lambda value:value is True)


def exact_prompt_seed(tmp_path,monkeypatch):
    from src.autoslice.entity_audio_verifier import _prepare_audio_span
    row=exact_seed(tmp_path,monkeypatch)
    row['span']=_prepare_audio_span(request=row['request'],context_mode=True,
        source_media_timeline_offset_ms=0,source_duration_ms=6000,witness_mode=True)
    row['timeline']=deepcopy(row['span'].timeline_binding)
    return row


def exact_prompt_consume(row):
    from src.autoslice.exact_source_transcript_contract import exact_source_transcript_prompt
    return exact_source_transcript_prompt(request=row['request'],timeline_binding=row['timeline'])


register('exact_source_transcript_contract.exact_source_transcript_prompt','EXACT_SOURCE_TRANSCRIPT_TIMELINE_INVALID',
         exact_prompt_seed,change(['timeline','source_media','crop_end_ms'],0),exact_prompt_consume)


def sealed_exact_seed(tmp_path,monkeypatch):
    row=exact_seed(tmp_path,monkeypatch)
    row['observation']=entity_consume(row)
    assert row['observation']['status']=='OBSERVED',row['observation']
    return row


def seal_exact_consume(row):
    from src.autoslice.exact_source_transcript_contract import seal_exact_source_transcript_observation
    obs=row['observation']
    return seal_exact_source_transcript_observation(request=row['request'],exact_transcript=obs['exact_transcript'],
        audible_language=obs['audible_language'],source_media_sha256=obs['source_media_sha256'],
        audio_clip_sha256=obs['audio_clip_sha256'],provider=obs['provider'],model=obs['model'],
        response_sha256=obs['response_sha256'],timeline_binding=obs['timeline_binding'],provider_route=obs['provider_route'])


for code,path,value in (
    ('EXACT_SOURCE_TRANSCRIPT_REQUEST_INVALID',['request','cue_indexes'],[0]),
    ('EXACT_SOURCE_TRANSCRIPT_OBSERVATION_INVALID',['observation','audible_language'],'other'),
):
    register('exact_source_transcript_contract.seal_exact_source_transcript_observation',code,sealed_exact_seed,change(path,value),seal_exact_consume)


def valid_exact_observation(row):
    from src.autoslice.exact_source_transcript_contract import valid_exact_source_transcript_observation
    return valid_exact_source_transcript_observation(row['observation'],request=row['request'])


register('exact_source_transcript_contract.valid_exact_source_transcript_observation','<FALSE>',sealed_exact_seed,
         change(['observation','exact_transcript'],'changed'),valid_exact_observation,lambda value:value is True)


def digest_seed(tmp_path,monkeypatch):
    return {'digest':check_request()['request_sha256']}


def digest_consume(row,module):
    from importlib import import_module
    return getattr(import_module('src.autoslice.'+module),'_valid_digest' if module.startswith('exact') else '_valid_sha256')(row['digest'])


for module in ('acoustic_witness_adjudication','exact_source_transcript_contract'):
    register(module+'.'+('_valid_digest' if module.startswith('exact') else '_valid_sha256'),'<FALSE>',digest_seed,
             change(['digest'],'bad'),lambda row,m=module:digest_consume(row,m),lambda value:value is True)


def valid_witness_consume(row):
    from src.autoslice.acoustic_witness_adjudication import valid_witness_evidence
    return valid_witness_evidence(row['witness'],request_sha256=row['witness']['request_sha256'])


register('acoustic_witness_adjudication.valid_witness_evidence','<FALSE>',word_seed,
         change(['witness','schema_version'],'other'),valid_witness_consume,lambda value:value is True)


def read_aloud_seed(tmp_path,monkeypatch):
    from src.autoslice.read_aloud_llm_verifier import _closed_choice_with_witness
    row=entity_seed(tmp_path,monkeypatch)
    row['observed']={'schema_version':'subtitle-span-acoustic-witness.v1','status':'OBSERVED','target_audible':True,
                     'heard_pinyin':'hai mei you ge zhai ne','uncertain_positions':[],'syllable_count':6,
                     'confidence':0.9,'reason':'synthetic target syllables'}
    def judge(prompt):
        return json.dumps({'choice':'还没有歌债呢','needs_audio':'字幕文字闭集裁决' in prompt,'reason':'synthetic lexical choice',
            'ranking':[{'canonical':'还没有歌债呢','p':0.9},{'canonical':'还没有歌杂呢','p':0.1}]},ensure_ascii=False)
    request=deepcopy(row['check']);request['exact_text']='还没有歌债呢'
    row['entity_verdict']=_closed_choice_with_witness(request=request,llm_call=judge,next_verifier=row['verifier'])
    assert row['entity_verdict'] is not None
    row['entity_row']={'verdict':row['entity_verdict'],'request_sha256':row['entity_verdict']['request_sha256']}
    return row


def cpa_witness_consume(row):
    from src.autoslice.acoustic_witness_adjudication import valid_cpa_witness_adjudication
    return valid_cpa_witness_adjudication(row['entity_verdict'])


def cpa_entity_consume(row):
    from src.autoslice.acoustic_witness_adjudication import valid_cpa_entity_repair
    return valid_cpa_entity_repair(row['entity_row'],expected_entity='还没有歌债呢')


register('acoustic_witness_adjudication.valid_cpa_witness_adjudication','<FALSE>',read_aloud_seed,
         change(['entity_verdict','judge_completion_sha256'],'bad'),cpa_witness_consume,lambda value:value is True)
register('acoustic_witness_adjudication.valid_cpa_entity_repair','<FALSE>',read_aloud_seed,
         change(['entity_row','verdict','canonical_entity'],'other'),cpa_entity_consume,lambda value:value is True)


def authority_seed(tmp_path,monkeypatch,mode='drop'):
    from src.autoslice.final_review_auditor import adjudicate_context_finding
    row=witness_seed(tmp_path,monkeypatch)
    row['observed'].update(target_audible=False,heard_pinyin='',syllable_count=0)
    finding=deepcopy(FINDING)
    if mode=='drop':
        finding.update(suspect='还没有歌杂呢',suggestion='',proposed_full_cue='',repair_class='acoustic_drop_cue',
                       span_start_codepoint=0,span_end_codepoint=len('还没有歌杂呢'))
    answer={'choice':'DROP' if mode=='drop' else 'CURRENT' if mode=='current' else 'PROPOSED',
            'needs_audio':False,'reason':'explicit synthetic judgment',
            'current_utterance_supported':True,'current_utterance_support_reason':'complete current utterance supported'}
    if mode=='current':
        row['observed'].update(target_audible=True,heard_pinyin='hai mei you ge za ne',syllable_count=6)
    def judge(prompt):
        from test_argon_consumer_integration import _synthetic_target_scope
        if prompt.startswith('# 字幕文字闭集裁决'):
            return json.dumps({**answer,'choice':'CURRENT','needs_audio':True,**_synthetic_target_scope(prompt,True)})
        return json.dumps({**answer,**_synthetic_target_scope(prompt,False)})
    row['finding']=finding
    row['final_srt'],row['audit']=adjudicate_context_finding(SRT,finding,clip_context=row['context'],
        entity_verifier=row['verifier'],judge_llm_call=judge,require_complete_utterance_support=True)
    assert row['audit'].get('witness_judge'),row['audit']
    return row


def drop_seed(tmp_path,monkeypatch):
    return authority_seed(tmp_path,monkeypatch,'drop')


def override_seed(tmp_path,monkeypatch):
    return authority_seed(tmp_path,monkeypatch,'override')


def current_authority_seed(tmp_path,monkeypatch):
    return authority_seed(tmp_path,monkeypatch,'current')


def authority_consume(row,mode):
    from src.autoslice import acoustic_witness_adjudication as a
    return (a.valid_inaudible_drop_authority(row['audit']) if mode=='drop' else
            a.valid_inaudible_witness_override(check_request=row['audit']['request'],witness=row['audit']['verdict'],
                witness_judge=row['audit']['witness_judge']))


for mode,producer,function in (
    ('drop',drop_seed,'valid_inaudible_drop_authority'),
    ('override',override_seed,'valid_inaudible_witness_override'),
):
    mutate=(change(['audit','repaired'],False) if mode=='drop' else
            change(['audit','witness_judge','inaudible_witness_override','status'],'BLOCK'))
    register('acoustic_witness_adjudication.'+function,'<FALSE>',producer,mutate,
             lambda row,m=mode:authority_consume(row,m),lambda value:value is True)


def compact_seed(tmp_path,monkeypatch,mode):
    from src.autoslice.producer_package_finalization import _apply_exact_final_cpa_repairs
    row=authority_seed(tmp_path,monkeypatch,mode)
    _,repairs=_apply_exact_final_cpa_repairs(SRT,{'findings':[{**row['finding'],
        'base_text_sha256':row['audit']['request']['base_text_sha256'],'context_audio_adjudication':row['audit']}]},
        clip_context=row['context'],adjudication_stage='context_audio_adjudication')
    assert repairs,row['audit']
    row['repair']=repairs[0]
    return row


for mode,function in (('drop','valid_inaudible_drop_repair'),('override','valid_inaudible_override_repair')):
    def compact_consume(row,fn=function):
        from src.autoslice import acoustic_witness_adjudication as a
        return getattr(a,fn)(row['repair'])
    register('acoustic_witness_adjudication.'+function,'<FALSE>',lambda p,m,mode=mode:compact_seed(p,m,mode),
             change(['repair','timing_immutable'],False),compact_consume,lambda value:value is True)


def current_support_consume(row):
    from src.autoslice.acoustic_witness_adjudication import valid_current_utterance_support
    audit=row['audit']
    return valid_current_utterance_support(check_request=audit['request'],judge=audit['witness_judge']['judge'],
                                          witness_judge=audit['witness_judge'])


register('acoustic_witness_adjudication.valid_current_utterance_support','<FALSE>',current_authority_seed,
         change(['audit','witness_judge','judge','current_utterance_support_reason'],''),current_support_consume,lambda value:value is True)


def valid_argon_audit(row):
    from src.autoslice.argon_escalation_integration import valid_argon_adjudication
    return valid_argon_adjudication(row['audit'])


def argon_audit_seed(tmp_path,monkeypatch):
    row=argon_seed(tmp_path,monkeypatch)
    _,row['audit']=row['scene'].run()
    return row


register('argon_escalation_integration.valid_argon_adjudication','<FALSE>',argon_audit_seed,
         change(['audit','request','request_sha256'],'0'*64),valid_argon_audit,lambda value:value is True)


def handoff_seed(tmp_path,monkeypatch):
    from src.autoslice.exact_source_transcript_contract import build_exact_source_transcript_request,PROVIDER_REPORT_SCHEMA,build_exact_source_transcript_handoff
    row=witness_seed(tmp_path,monkeypatch)
    row['witness']=entity_consume(row)
    row['witness_request']=deepcopy(row['request'])
    row['request']=build_exact_source_transcript_request(row['witness_request'])
    row['observed']={'schema_version':PROVIDER_REPORT_SCHEMA,'status':'OBSERVED','target_audible':True,
                     'audible_language':'zh','exact_transcript':'还没有歌债呀','reason':'synthetic third wording'}
    row['observation']=entity_consume(row)
    row['handoff']=build_exact_source_transcript_handoff(observation=row['observation'],physical_request=row['request'],
        initial_check_request=row['check'],witness_request=row['witness_request'],witness=row['witness'],srt_text=SRT,clip_context=row['context'])
    return row


def build_handoff(row):
    from src.autoslice.exact_source_transcript_contract import build_exact_source_transcript_handoff
    return build_exact_source_transcript_handoff(observation=row['observation'],physical_request=row['request'],
        initial_check_request=row['check'],witness_request=row['witness_request'],witness=row['witness'],srt_text=SRT,clip_context=row['context'])


register('exact_source_transcript_contract.build_exact_source_transcript_handoff','EXACT_SOURCE_TRANSCRIPT_HANDOFF_BINDING_INVALID',
         handoff_seed,change(['context','candidate_id'],''),build_handoff)


def valid_handoff(row):
    from src.autoslice.exact_source_transcript_contract import valid_exact_source_transcript_handoff
    # The handoff validator consumes a request whose candidate is now the actual
    # third transcript. Use its ordinary context-candidate constructor.
    return valid_exact_source_transcript_handoff(row['handoff'])


register('exact_source_transcript_contract.valid_exact_source_transcript_handoff','<FALSE>',handoff_seed,
         change(['handoff','mutation_authorized'],True),valid_handoff,lambda value:value is True)


def bound_audio_seed(tmp_path,monkeypatch):
    from src.autoslice.semantic_retry_progress import SemanticAttemptRecorder
    from src.autoslice.missing_proposal_bootstrap import _proposal_audio_recorded_call,proposal_audio_prompt_payload
    from src.autoslice.closed_set_proposal_rebuild import proposal_phonemes
    row=sealed_exact_seed(tmp_path,monkeypatch)
    monkeypatch.setenv('AUTOSLICE_BASE',str(tmp_path/'runtime'))
    audio={'mutation_authorized':False,'authority':'EVIDENCE_ONLY','original_language_observation':row['observation'],
           'phonetic_evidence':proposal_phonemes(row['check']['current_cue'],None)}
    recorder=SemanticAttemptRecorder(srt_text=SRT,finding=deepcopy(FINDING),clip_context=row['context'],offset_ms=0)
    def provider(prompt):
        return '{"status":"UNRESOLVED","reason":"offline test"}'
    provider.cpa_cache_identity={'transport':'offline','models':['fixture'],'effort':'none'}
    callback=recorder.wrap(provider)
    row['bound']=_proposal_audio_recorded_call(callback,row['check'],audio)
    row['prompt']=proposal_audio_prompt_payload(audio).prompt_fragment
    return row


register('missing_proposal_bootstrap._proposal_audio_recorded_call.<locals>.bound_call','PROPOSAL_AUDIO_PROMPT_BINDING_MISSING',
         bound_audio_seed,change(['prompt'],'{}'),lambda row:row['bound'](row['prompt']),lambda value:isinstance(value,str))


def exact_failure_seed(tmp_path,monkeypatch):
    row=exact_seed(tmp_path,monkeypatch);row['failures']=[]
    return row


def exact_failure_consume(row):
    from src.autoslice.entity_audio_verifier import _exact_provider_failure_receipt
    return _exact_provider_failure_receipt(row['request'],row['failures'])


register('entity_audio_verifier._exact_provider_failure_receipt','EXACT_SOURCE_TRANSCRIPT_PROVIDER_RESULT_INVALID',
         exact_failure_seed,change(['failures'],[{'provider':'agy','category':'AGY_STALE_OUTPUT_CLEANUP_FAILED'}]),
         exact_failure_consume,lambda value:value.get('reason_code')=='EXACT_SOURCE_TRANSCRIPT_CAPABILITY_UNAVAILABLE')


def transport_seed(tmp_path,monkeypatch,mode='assisted'):
    from types import SimpleNamespace
    import subprocess
    from src.autoslice import entity_audio_verifier as e
    if mode=='exact':
        row=exact_prompt_seed(tmp_path,monkeypatch)
        row['request']=row['span'].observed_request
        row['raw']=row['observed']
    elif mode=='witness':
        row=witness_seed(tmp_path,monkeypatch)
        from src.autoslice.entity_audio_verifier import _prepare_audio_span
        span=_prepare_audio_span(request=row['request'],context_mode=True,source_media_timeline_offset_ms=0,
                                 source_duration_ms=6000,witness_mode=True)
        row['request']=span.observed_request;row['raw']=row['observed']
    else:
        row=assisted_request_seed(tmp_path,monkeypatch)
    row.update(job=tmp_path/'transport',extract_ok=True,api=False,disabled=False,quota=False,raw_text=None,patch=monkeypatch)
    row['job'].mkdir();row['audio_path']=row['job']/'input.mp4';row['audio_path'].write_bytes(b'offline audio transport bytes')
    row['circuit']=e._AgyQuotaCircuitBreaker()
    def runner(argv,*,cwd,env,timeout,response_validator=None):
        raw=row['raw_text'] if row['raw_text'] is not None else json.dumps(row['raw'],ensure_ascii=False)
        (row['job']/'verdict.json').write_text(raw)
        done=subprocess.CompletedProcess(argv,0,raw,'')
        if response_validator:
            response_validator(done)
        return SimpleNamespace(completed=done,failure_category=None,launch_error_type=None)
    def extract(argv,**kwargs):
        if row['extract_ok']:
            from pathlib import Path
            Path(argv[-1]).write_bytes(b'offline encoded API attachment')
        return subprocess.CompletedProcess(argv,0 if row['extract_ok'] else 1,'','')
    def api(**kwargs):
        raw=json.dumps(row['raw'],ensure_ascii=False)
        if kwargs.get('response_validator'):
            kwargs['response_validator'](raw)
        kwargs['response_path'].write_text(raw)
        return SimpleNamespace(observed=row['raw'],accepted_key_tier='free',paid_policy_stamp=None,
            accepted_key_ordinal=1,configured_key_count=1,provider_failures=kwargs['provider_failures'],paid_gate_reason=None)
    monkeypatch.setattr(e.agy_gemini_client,'run_local_agy',runner)
    monkeypatch.setattr(e.agy_gemini_client,'local_agy_available',lambda _:True)
    monkeypatch.setattr(e,'agy_subprocess_env',lambda:{})
    monkeypatch.setattr(e,'_run_gemini_web_if_enabled',lambda *a,**k:None)
    monkeypatch.setattr(e,'_run_gemini_api_fallback',api)
    monkeypatch.setattr(e.subprocess,'run',extract)
    monkeypatch.setenv('AUTOSLICE_BASE',str(tmp_path/'runtime'))
    monkeypatch.delenv(e.ENTITY_AUDIO_DISABLE_AGY_ENV,raising=False)
    return row


def transport_consume(row):
    from src.autoslice import entity_audio_verifier as e
    if row['disabled'] or row['api']:
        row['patch'].setenv(e.ENTITY_AUDIO_DISABLE_AGY_ENV,'1')
    if row['quota']:
        row['circuit'].observe_failure('AGY_QUOTA_EXHAUSTED')
    return REAL_OBSERVE(request=row['request'],candidates=[],audio_path=row['audio_path'],job_dir=row['job'],
        recording_date='2026-10-22',timely_context='',binary='/offline/agy',model='gemini-3.8-flash',timeout='10m',
        agy_quota_circuit=row['circuit'])


def raw_transport_seed(tmp_path,monkeypatch):
    row=transport_seed(tmp_path,monkeypatch);row['api']=True
    return row


for producer,function in (
    (transport_seed,'validate_assisted_process'),(raw_transport_seed,'validate_assisted_raw'),
):
    register('entity_audio_verifier._observe_entity_audio.<locals>.'+function,'ASSISTED_AUDIO_RESPONSE_SCHEMA_INVALID',
             producer,change(['raw','schema_version'],'other'),transport_consume,lambda value:value.observed is not None)


def category_consume(row,code):
    outcome=transport_consume(row)
    if outcome.observed is not None and not row.get('expect_category'):
        return {'status':'OBSERVED'}
    categories=[x.get('category') for x in outcome.provider_failures]
    return {'reason_code':code if code in categories else 'MISSING_CATEGORY','categories':categories}


def extraction_seed(tmp_path,monkeypatch):
    row=transport_seed(tmp_path,monkeypatch,'witness');row['api']=True
    return row


def stale_seed(tmp_path,monkeypatch):
    return transport_seed(tmp_path,monkeypatch,'exact')


def stale_directory(row):
    (row['job']/'verdict.json').mkdir();row['expect_category']=True


for code,producer,mutate in (
    ('AGY_DISABLED_BY_ENV',transport_seed,change(['disabled'],True)),
    ('AGY_QUOTA_EXHAUSTED',transport_seed,change(['quota'],True)),
    ('AGY_INVALID_OUTPUT',lambda p,m:transport_seed(p,m,'witness'),change(['raw_text'],'not JSON')),
    ('GEMINI_API_AUDIO_EXTRACTION_FAILED',extraction_seed,change(['extract_ok'],False)),
    ('AGY_STALE_OUTPUT_CLEANUP_FAILED',stale_seed,stale_directory),
):
    def mutate_with_marker(row,mutate=mutate):
        mutate(row);row['expect_category']=True
    register('entity_audio_verifier._observe_entity_audio',code,producer,mutate_with_marker,
             lambda row,code=code:category_consume(row,code),lambda value:value.get('status')=='OBSERVED')


def public_word_consume(row):
    from src.autoslice.acoustic_witness_adjudication import judge_word_choice
    return judge_word_choice(llm_call=word_provider(row),check_request=row['check'],witness=row['witness'])


def missing_check_hash(row):
    row['check'].pop('request_sha256')


register('acoustic_witness_adjudication._local_after_return_failure','LOCAL_CONTRACT_ERROR_AFTER_PROVIDER_RETURN',
         word_seed,missing_check_hash,public_word_consume,lambda value:value.get('status')=='JUDGED')


def legacy_cache_seed(tmp_path,monkeypatch):
    row=word_seed(tmp_path,monkeypatch)
    monkeypatch.setenv('AUTOSLICE_BASE',str(tmp_path/'runtime'))
    row['identity']={'models':['offline-cache-fixture'],'effort':'none'}
    row['answer'].pop('needs_audio')
    verdict=word_consume(row)
    assert verdict['status']=='JUDGED'
    row['cache_path']=next((tmp_path/'runtime/cache/judge-verdicts').glob('*/*.json'))
    # This case exercises the explicit old parsed-only lane. New receipts
    # retain raw bytes and correctly reconstruct the flag with today's parser.
    payload=json.loads(row['cache_path'].read_bytes())
    payload['verdict'].pop('span_review_raw_completion')
    row['cache_path'].write_text(json.dumps(payload))
    return row


def drop_legacy_flag(row):
    payload=json.loads(row['cache_path'].read_bytes());payload['verdict'].pop('needs_audio_requested')
    row['cache_path'].write_text(json.dumps(payload))


register('acoustic_witness_adjudication._adjudicate_with_witness_body','JUDGE_AUDIO_REQUEST_UNRESOLVED',
         legacy_cache_seed,drop_legacy_flag,adjudicate_consume,lambda value:value[0] is True)


def raw_judge_cache_seed(tmp_path, monkeypatch, *, semantic=False):
    row=word_seed(tmp_path,monkeypatch)
    monkeypatch.setenv('AUTOSLICE_BASE',str(tmp_path/'runtime'))
    row['identity']={'models':['offline-raw-cache-fixture'],'effort':'none'}
    assert word_consume(row)['status']=='JUDGED'
    root=tmp_path/'runtime/cache/judge-verdicts'
    row['cache_path']=next((root/'semantic-no-new').glob('*/*.json')) if semantic else next(root.glob('*/*.json'))
    row['cache_entry']=json.loads(row['cache_path'].read_bytes())
    return row


def semantic_judge_cache_consume(row):
    from src.autoslice.acoustic_witness_adjudication import _read_semantic_judge_cache
    return _read_semantic_judge_cache(fingerprint=row['cache_entry']['semantic_evidence_fingerprint'],
        model_identity=row['identity'])


def corrupt_judge_cache(row, damage):
    payload=json.loads(row['cache_path'].read_bytes())
    if damage=='invalid_json':
        row['cache_path'].write_text('invalid JSON')
        return
    if damage=='missing_model':
        payload['verdict'].pop('model_identity')
    else:
        payload['verdict']['span_review_raw_completion']+=' drift'
    row['cache_path'].write_text(json.dumps(payload))


for semantic in (False,True):
    for code,damage in (
        ('JUDGE_ANSWER_MODEL_UNKNOWN','missing_model'),
        ('JUDGE_CACHE_ENTRY_INVALID','invalid_json'),
        ('JUDGE_CACHE_RAW_RESPONSE_DRIFT','raw_drift'),
    ):
        register('acoustic_witness_adjudication.'+('_read_semantic_judge_cache' if semantic else '_judge_word_choice_body'),
            code,lambda p,m,semantic=semantic:raw_judge_cache_seed(p,m,semantic=semantic),
            lambda row,damage=damage:corrupt_judge_cache(row,damage),
            semantic_judge_cache_consume if semantic else word_consume,
            lambda value:value is not None,'real raw cache roundtrip; one provenance/integrity field changes')


def no_progress_seed(tmp_path,monkeypatch):
    row=word_seed(tmp_path,monkeypatch)
    monkeypatch.setenv('AUTOSLICE_BASE',str(tmp_path/'runtime'))
    row['identity']={'models':['offline-no-progress-fixture'],'effort':'none'}
    row['answer'].update(choice='NEITHER',needs_audio=True)
    first=word_consume(row)
    assert first['status']=='JUDGED'
    return row


def change_retry_reason(row):
    row['check']['reason']='different controller retry wording'
    seal_check(row)


register('acoustic_witness_adjudication._semantic_no_new_evidence_verdict','JUDGE_NO_NEW_EVIDENCE',no_progress_seed,
         change_retry_reason,word_consume,lambda value:value.get('status')=='JUDGED')
register('acoustic_witness_adjudication._adjudicate_with_witness_body','JUDGE_NO_NEW_EVIDENCE',no_progress_seed,
         change_retry_reason,adjudicate_consume,lambda value:value[1]=='JUDGE_REQUESTS_MORE_AUDIO')


def silent_word_seed(tmp_path,monkeypatch):
    row=witness_seed(tmp_path,monkeypatch)
    row['observed'].update(target_audible=False,heard_pinyin='',syllable_count=0)
    row['witness']=entity_consume(row)
    row['answer']={'choice':'PROPOSED','needs_audio':False,'reason':'explicit nonempty proposal override'}
    row['llm_available']=True
    return row


register('acoustic_witness_adjudication._adjudicate_with_witness_body','INAUDIBLE_EMPTY_PROPOSED_REQUIRES_EXPLICIT_DROP',
         silent_word_seed,change(['check','proposed_cue'],'',seal=seal_check),adjudicate_consume,lambda value:value[0] is True)
register('acoustic_witness_adjudication._adjudicate_with_witness_body','WITNESS_CONFLICT_UNSUPPORTED_PROPOSED_KEPT_CURRENT',
         word_seed,change(['check','proposed_cue'],'',seal=seal_check),adjudicate_consume,lambda value:value[0] is True)


def attempted_failure_seed(tmp_path,monkeypatch):
    row=witness_seed(tmp_path,monkeypatch);row['observed']=None
    row['witness']=entity_consume(row)
    row['answer']={'choice':'PROPOSED','needs_audio':False,'reason':'contextual proposal after actual provider failure'}
    row['llm_available']=True
    return row


register('acoustic_witness_adjudication._adjudicate_with_witness_body','WITNESS_NEVER_ATTEMPTED_KEEP_CURRENT_DISCLOSED',
         attempted_failure_seed,change(['witness','reason_code'],'AUDIO_VERIFIER_UNAVAILABLE'),adjudicate_consume,lambda value:value[0] is True)


def registered_name_seed(tmp_path,monkeypatch):
    from src.autoslice.final_review_auditor import build_context_adjudication_request
    row=word_seed(tmp_path,monkeypatch)
    srt=SRT.replace('还没有歌杂呢','李豆沙说歌杂')
    finding={**FINDING,'proposed_full_cue':'李豆沙说歌债'}
    row['check']=build_context_adjudication_request(srt,finding,clip_context=row['context'])
    return row


register('acoustic_witness_adjudication._adjudicate_with_witness_body','REGISTERED_NAME_REFERENT_DECISION_REQUIRED',
         registered_name_seed,change(['check','proposed_cue'],'她说歌债',seal=seal_check),adjudicate_consume,lambda value:value[0] is True)


def cache_replay_seed(tmp_path,monkeypatch):
    row=argon_seed(tmp_path,monkeypatch,'OBSERVED')
    row['verdict']=row['owner'].ordinary_verifier.probe_witness_cache(row['wr'])
    assert row['verdict'] and row['verdict'].get('witness_cache_replay'),row['verdict']
    return row


def lose_probe(row):
    from types import SimpleNamespace
    row['owner'].ordinary_verifier=SimpleNamespace(probe_witness_cache=None)


def empty_replay(row):
    from types import SimpleNamespace
    row['owner'].ordinary_verifier=SimpleNamespace(probe_witness_cache=lambda _:None)


for code,mutate in (
    ('ARGON_ORDINARY_CACHE_PROBE_MISSING',lose_probe),('ARGON_ORDINARY_CACHE_REPLAY_DRIFT',empty_replay),
):
    register('argon_escalation_integration.FindingSession._ordinary',code,cache_replay_seed,mutate,ordinary)


def raw_judge_seed(tmp_path,monkeypatch,observed=False):
    from types import SimpleNamespace
    from src.autoslice import argon_escalation_storage as store
    from src.autoslice.argon_escalation_contract import sha
    row=argon_seed(tmp_path,monkeypatch,'OBSERVED' if observed else 'UNCERTAIN')
    record=json.loads(store.get_blob(row['session'].root,row['event']['judge_record']))
    row['raw_record']=record
    row['judge']={'status':'JUDGED','check_request_sha256':row['check']['request_sha256'],
                  'completion_sha256':record['completion_sha256'],'prompt_sha256':sha(record['request']['prompt'].encode()),
                  'choice':json.loads(record['completion'])['choice']}
    row['recorder']=SimpleNamespace(_entries=lambda:[row['raw_record']])
    return row


def raw_judge_consume(row):
    return row['session']._judge_raw(row['check'],row['judge'],row['recorder'])


def judge_source_drift(row):
    row['check']['whole_clip_current_srt']+='\n'
    seal_check(row)
    row['judge']['check_request_sha256']=row['check']['request_sha256']


register('argon_escalation_integration.FindingSession._judge_raw','ARGON_ORDINARY_JUDGE_DRIFT',raw_judge_seed,
         change(['judge','choice'],'OTHER'),raw_judge_consume)
register('argon_escalation_integration.FindingSession._judge_raw','ARGON_ORDINARY_JUDGE_SOURCE_DRIFT',raw_judge_seed,
         judge_source_drift,raw_judge_consume)


def pending_argon_seed(tmp_path,monkeypatch):
    import test_argon_consumer_integration as fixture
    from src.autoslice.argon_escalation_integration import argon_consumer_contract
    scene=fixture.scene.__wrapped__(tmp_path,monkeypatch)
    _,first=scene.run()
    owner=scene.verifier.argon_escalation
    check=first['request']
    session=owner.session(check,scene.finding)
    return {'scene':scene,'owner':owner,'session':session,'spec':session.spec,'check':check,
            'native_status':'OBSERVED','ledger_failed':False}


def finish_argon_consume(row):
    from src.autoslice.argon_escalation_integration import argon_consumer_contract
    from src.autoslice import argon_escalation_storage as store
    from src.autoslice.argon_escalation_contract import sha,canonical
    hearing=row['scene'].provider.hearing
    if row['native_status']=='UNCERTAIN':
        hearing.update(status='UNCERTAIN',reason='synthetic uncertain final word',uncertainties=[{
            'start_ms':hearing['target_window_ms'][0],'end_ms':hearing['target_window_ms'][1],
            'reason':'synthetic ambiguous word'}])
    outcome=row['owner'].runtime.advance(root=row['session'].root/'batch',spec=row['spec'])
    row['envelope']=argon_consumer_contract(row['spec']).envelope(row['session'].root,outcome)
    if row['ledger_failed']:
        for item in outcome['result']['accounting']['histories']:
            if item['mode']==outcome['result']['mode']:
                from pathlib import Path
                path=Path(item['ledger_path']);payload=json.loads(path.read_bytes())
                payload['events'][-1]['state']='FAILED';payload['state']='FAILED'
                rehash(payload,'record_sha256',lambda value:sha(canonical(value)))
                path.write_bytes(canonical(payload))
    return consumer(row)


for code,path,value in (
    ('ARGON_CONSUMER_NOT_OBSERVED',['native_status'],'UNCERTAIN'),
    ('ARGON_COMPLETED_SUBMISSION_REQUIRED',['ledger_failed'],True),
):
    register('argon_escalation_integration.validate_consumer_evidence',code,pending_argon_seed,change(path,value),finish_argon_consume)


def targeted_argon_seed(tmp_path,monkeypatch,needs_audio=False):
    import test_argon_targeted_relisten as fixture
    import test_targeted_relisten_public_contract as protocol
    from src.autoslice import argon_escalation_integration as a
    rig=protocol.build_offline_rig(tmp_path,monkeypatch)
    witness,check,finding,_=protocol.three_observations(rig)
    provider,owner=fixture.attach_argon(rig,monkeypatch)
    fixture.run_public(rig,finding,fixture.argon_judge(rig,needs_audio=needs_audio))
    _,audit=fixture.run_public(rig,finding,fixture.argon_judge(rig,needs_audio=needs_audio))
    if needs_audio and not audit.get('request',{}).get('argon_escalation_evidence'):
        audit=audit['prior_audio_adjudication']
    assert audit.get('request',{}).get('argon_escalation_evidence'),audit.get('reason_code')
    check=audit['request'];envelope=check['argon_escalation_evidence']
    session=owner.session(check,finding)
    return dict(rig=rig,scene=rig,provider=provider,owner=owner,session=session,spec=session.spec,check=check,
                envelope=envelope,finding=finding,audit=audit)


def targeted_question_drift(row):
    from src.autoslice.argon_escalation_contract import sha
    check=row['check']
    old=check['current_cue'];new='另一个目标文本'
    check['current_cue']=new
    check['whole_clip_current_srt']=check['whole_clip_current_srt'].replace(old,new)
    check['base_text_sha256']=sha(new.encode())
    seal_check(row)


register('argon_escalation_integration.validate_consumer_evidence','ARGON_TARGETED_CURRENT_CONTEXT_DRIFT',
         targeted_argon_seed,targeted_question_drift,consumer)


def media_seed(tmp_path,monkeypatch):
    row=semantic_seed(tmp_path,monkeypatch)
    row['media']=row['witness']['relisten_artifacts']['audio_path']
    row['duration']=row['witness']['audio_end_ms']-row['witness']['audio_start_ms']
    return row


def media_consume(row):
    from pathlib import Path
    from src.autoslice.entity_audio_verifier import _validate_cropped_audio_media
    return _validate_cropped_audio_media(Path(row['media']),expected_duration_ms=row['duration'])[0]


register('entity_audio_verifier._validate_cropped_audio_media','<FALSE>',media_seed,change(['duration'],100000),
         media_consume,lambda value:value is True)


def assisted_semantic_seed(tmp_path,monkeypatch):
    row=semantic_seed(tmp_path,monkeypatch)
    row['plan'].update(observation_class='CONTEXT_ASSISTED_AUDIO',purpose='SOURCED_CONTEXT',context_refs=['cue:1'],
                        question='前句是否解释目标字音',context_summary='前句提供词界语境',proper_names=[])
    return row


def lose_assisted_provider(row):
    row['verifier'].observe_range_context.context_assisted_providers=()


register('semantic_audio_recovery.recover_semantic_audio_range','CONTEXT_ASSISTED_PROVIDER_UNSUPPORTED',
         assisted_semantic_seed,lose_assisted_provider,semantic_consume,lambda value:value[1]['status']=='OBSERVED')
register('semantic_audio_recovery.recover_semantic_audio_range.<locals>.record_dispatches','RELISTEN_DISPATCH_LEDGER_DISAPPEARED',
         lambda p,m:fourth_seed(p,m),change(['budget_mode'],'post_missing'),semantic_consume,lambda value:value[1]['status']=='OBSERVED')


def no_gain_seed(tmp_path,monkeypatch):
    from types import SimpleNamespace
    import test_targeted_relisten_public_contract as protocol
    rig=protocol.build_offline_rig(tmp_path,monkeypatch)
    witness,check,finding,audits=protocol.three_observations(rig)
    verifier=SimpleNamespace(observe_range_context=rig.verifier.observe_range_context,
        relisten_budget_state=rig.verifier.relisten_budget_state,probe_witness_cache=rig.verifier.probe_witness_cache)
    return {'rig':rig,'srt':protocol.SRT,'finding':finding,'check':check,'verifier':verifier,'audits':audits,'witness':witness,
            'available':True,'plan':protocol.no_gain_plan}


def lose_terminal_ledger(row):
    row['verifier'].relisten_budget_state=None


register('semantic_audio_recovery.recover_semantic_audio_range','RELISTEN_TERMINAL_LEDGER_REQUIRED',no_gain_seed,
         lose_terminal_ledger,semantic_consume,lambda value:value[1]['status']=='CONTEXT_REVIEW')


def argon_trigger_semantic_seed(tmp_path,monkeypatch):
    import test_targeted_relisten_public_contract as protocol
    import test_argon_targeted_relisten as argon
    row=no_gain_seed(tmp_path,monkeypatch)
    provider,integration=argon.attach_argon(row['rig'],monkeypatch)
    session=integration.session(row['check'],row['finding'])
    row['check']=session.mark_request(row['check'])
    row['argon_session']=session;row['provider']=provider
    return row


register('semantic_audio_recovery.recover_semantic_audio_range','ARGON_INFRASTRUCTURE_BLOCKED',argon_trigger_semantic_seed,
         lambda row:setattr(row['provider'],'post_status',401),semantic_consume,
         lambda value:value[1]['status']=='ARGON_PENDING')


def infrastructure_consume(row):
    # Run the real public consumer; it selects the session's infrastructure
    # receipt only after the actual native runtime classifies a refused call.
    import test_argon_targeted_relisten as fixture
    fixture.run_public(row['rig'],row['finding'],fixture.argon_judge(row['rig']))
    return fixture.run_public(row['rig'],row['finding'],fixture.argon_judge(row['rig']))[1]


register('argon_escalation_integration.FindingSession.infrastructure_blocked_audit','ARGON_INFRASTRUCTURE_BLOCKED',
         argon_trigger_semantic_seed,lambda row:setattr(row['provider'],'post_status',401),infrastructure_consume,
         lambda value:value.get('status')=='OBSERVED' and value.get('repaired') is True)


def consumed_argon_seed(tmp_path,monkeypatch):
    import test_targeted_relisten_public_contract as protocol
    row=targeted_argon_seed(tmp_path,monkeypatch,True)
    row['session'].resume()
    row.update(srt=protocol.SRT,available=True,verifier=row['rig'].verifier,
               argon_session=row['session'],plan=protocol.no_gain_plan,
               finding={**row['finding'],'exact_release_adjudication':row['audit']})
    return row


register('semantic_audio_recovery.recover_semantic_audio_range','ARGON_READY_CURRENT_CPA_CONSUMPTION_REQUIRED',
         consumed_argon_seed,change(['finding','exact_release_adjudication','verdict','argon_escalation_evidence'],{}),
         semantic_consume,lambda value:value[1] and value[1]['status']=='CONTEXT_REVIEW')


def fourth_seed(tmp_path,monkeypatch):
    import test_targeted_relisten_public_contract as protocol
    row=no_gain_seed(tmp_path,monkeypatch)
    def plan(data):
        projection={'physical_target':data['physical_target'],'observations':data['already_heard_ranges'],
                    'effective_distinct_observations':data['effective_distinct_observations']}
        return protocol.strong_fourth(row['check'],projection)
    row['plan']=plan;row['budget_mode']='normal';row['budget_reads']=0
    real_budget=row['verifier'].relisten_budget_state
    def budget():
        row['budget_reads']+=1
        current=real_budget()
        if row['budget_mode']=='post_missing' and row['budget_reads']>1:
            return {**current,'receipt':None}
        return current
    row['verifier'].relisten_budget_state=budget
    return row


def repeated_geometry_seed(tmp_path,monkeypatch):
    import test_targeted_relisten_public_contract as protocol
    from src.autoslice import entity_audio_verifier as e
    from src.autoslice.argon_escalation_contract import canonical,sha
    row=fourth_seed(tmp_path,monkeypatch)
    request=deepcopy(row['audits'][0]['range_witness_request'])
    request['evidence_id']='e'*64
    rehash(request,'request_sha256',lambda value:sha(canonical(value)))
    secondary_root=tmp_path/'second-producer';secondary_root.mkdir()
    with monkeypatch.context() as secondary_patch:
        secondary=protocol.build_offline_rig(secondary_root,secondary_patch)
        original=e.agy_gemini_client.run_local_agy
        def fresh_response(argv,**kwargs):
            result=original(argv,**kwargs)
            from pathlib import Path
            path=Path(kwargs['cwd'])/'verdict.json'
            raw=json.loads(path.read_bytes());raw['reason']='a distinct synthetic reply to the earlier window'
            path.write_text(json.dumps(raw,ensure_ascii=False))
            return result
        secondary_patch.setattr(e.agy_gemini_client,'run_local_agy',fresh_response)
        row['wrong_window_observations']=secondary.verifier.observe_range_context(check_request=row['check'],
            range_witness_request=request,provider='gemini')
    assert row['wrong_window_observations']
    return row


def return_previous_window(row):
    original=row['verifier'].observe_range_context
    def wrong(**kwargs):
        return row['wrong_window_observations']
    wrong.available_providers=original.available_providers
    wrong.context_assisted_providers=original.context_assisted_providers
    row['verifier'].observe_range_context=wrong


register('semantic_audio_recovery.recover_semantic_audio_range','RELISTEN_REPEATED_INPUT_NO_INFORMATION_GAIN',
         repeated_geometry_seed,return_previous_window,semantic_consume,lambda value:value[1]['status']=='OBSERVED')


def source_match_seed(tmp_path,monkeypatch):
    from src.autoslice.argon_escalation_integration import argon_source_binding_contract
    row=argon_seed(tmp_path,monkeypatch)
    row['binding_contract']=argon_source_binding_contract(row['spec'])
    row['binding']=row['binding_contract'].payload
    return row


register('argon_escalation_integration.ArgonSourceBindingContract.matches','<FALSE>',source_match_seed,
         change(['binding','context_source_ms'],[0,1]),lambda row:row['binding_contract'].matches(row['binding']),lambda value:value is True)


def model_seed(tmp_path,monkeypatch):
    row=entity_seed(tmp_path,monkeypatch)
    return row


def model_consume(row):
    from src.autoslice.argon_escalation_integration import ordinary_38_model
    return ordinary_38_model(row['provider'],row['model'])


register('argon_escalation_integration.ordinary_38_model','<FALSE>',model_seed,change(['model'],'other-model'),model_consume,lambda value:value is True)


def marked_consume(row):
    from src.autoslice.argon_escalation_integration import argon_adjudication_marked
    return not argon_adjudication_marked(row['audit'])


register('argon_escalation_integration.argon_adjudication_marked','<FALSE>',current_authority_seed,
         change(['audit','argon_escalation'],{}),marked_consume,lambda value:value is True)


def consider_consume(row,completed=False):
    session=row['session']
    if completed:
        return session.consider_completed_chain(row['check'],row['judge'],row['recorder'])
    return session.consider(row['check'],row['verdict'],row['judge'],row['recorder'])


for function,producer,consume in (
    ('consider',raw_judge_seed,consider_consume),
    ('consider_completed_chain',lambda p,m:raw_judge_seed(p,m,True),lambda row:consider_consume(row,True)),
):
    register('argon_escalation_integration.FindingSession.'+function,'<FALSE>',producer,
             lambda row:setattr(row['session'],'advanced',True),consume,lambda value:value is True)


register('argon_escalation_integration.FindingSession.consider_targeted_relisten','<FALSE>',argon_trigger_semantic_seed,
         lambda row:setattr(row['argon_session'],'advanced',True),
         lambda row:semantic_consume(row)[1]['status']=='ARGON_PENDING',lambda value:value is True)
register('argon_escalation_integration.FindingSession.resume','<FALSE>',argon_seed,remove_trigger,
         lambda row:row['session'].resume(),lambda value:value is True)


def entity_accepts(row):
    from src.autoslice.entity_audio_verifier import entity_verdict_contract
    return entity_verdict_contract(row['request']).accepts(row['observed'])


register('entity_audio_verifier.EntityVerdictContract.accepts','<FALSE>',entity_seed,
         change(['observed','current_fit'],'other'),entity_accepts,lambda value:value is True)


def circuit_seed(tmp_path,monkeypatch):
    from src.autoslice.entity_audio_verifier import _AgyQuotaCircuitBreaker
    return {'circuit':_AgyQuotaCircuitBreaker(),'category':'AGY_QUOTA_EXHAUSTED'}


register('entity_audio_verifier._AgyQuotaCircuitBreaker.observe_failure','<FALSE>',circuit_seed,
         change(['category'],'AGY_INVALID_OUTPUT'),lambda row:row['circuit'].observe_failure(row['category']),lambda value:value is True)
register('entity_audio_verifier._AgyQuotaCircuitBreaker.is_open','<FALSE>',circuit_seed,
         lambda row:row['circuit'].observe_failure(row['category']),lambda row:not row['circuit'].is_open(),lambda value:value is True)


def provider_predicate_seed(tmp_path,monkeypatch):
    from src.autoslice.context_assisted_audio import assistance_plan_contract
    row=assisted_seed(tmp_path,monkeypatch)
    row['provider']='gemini'
    row['plan_contract']=assistance_plan_contract(row['check'],providers=('gemini','native'),assistance_providers=('gemini',))
    return row


register('context_assisted_audio.AssistancePlanContract.provider_available','<FALSE>',provider_predicate_seed,
         change(['provider'],'native'),lambda row:row['plan_contract'].provider_available(row['provider']),lambda value:value is True)


def transient_seed(tmp_path,monkeypatch):
    return {'error':str(RuntimeError('HTTPERROR 503 synthetic provider response'))}


def transient_consume(row):
    from src.autoslice.acoustic_witness_adjudication import _judge_call_provider_transient
    return _judge_call_provider_transient(row['error'])


register('acoustic_witness_adjudication._judge_call_provider_transient','<FALSE>',transient_seed,
         change(['error'],'synthetic semantic response invalid'),transient_consume,lambda value:value is True)


def same_hearing_consume(row):
    from src.autoslice.acoustic_witness_adjudication import same_hearing,build_witness_request
    return same_hearing(row['witness'],build_witness_request(row['check']))


register('acoustic_witness_adjudication.same_hearing','<FALSE>',word_seed,
         change(['witness','request_sha256'],'0'*64),same_hearing_consume,lambda value:value is True)


def unsupported_current_seed(tmp_path,monkeypatch):
    row=word_seed(tmp_path,monkeypatch)
    row['answer'].update(choice='NEITHER',needs_audio=False)
    row['verdict']=word_consume(row)
    return row


def unresolved_predicate(row):
    from src.autoslice.acoustic_witness_adjudication import _semantic_unresolved_verdict
    return _semantic_unresolved_verdict(row['verdict'])


register('acoustic_witness_adjudication._semantic_unresolved_verdict','<FALSE>',unsupported_current_seed,
         change(['verdict','choice'],'PROPOSED'),unresolved_predicate,lambda value:value is True)


def witness_shape(row):
    from src.autoslice.acoustic_witness_adjudication import witness_judgment_contract
    return witness_judgment_contract(row['witness']).shape_valid()


register('acoustic_witness_adjudication.WitnessJudgmentContract.shape_valid','<FALSE>',word_seed,
         change(['witness','schema_version'],'other'),witness_shape,lambda value:value is True)


def support_predicate(row,fields_only=False):
    from src.autoslice.acoustic_witness_adjudication import word_choice_contract
    contract=word_choice_contract(llm_call=word_provider(row),check_request=row['check'],witness=row['witness'])
    return (contract.current_support_fields_valid(row['answer']) if fields_only else
            contract.current_support_valid(row['answer'],row['answer']['choice']))


for function,fields in (('current_support_valid',False),('current_support_fields_valid',True)):
    register('acoustic_witness_adjudication.WordChoiceContract.'+function,'<FALSE>',current_word_seed,
             change(['answer','current_utterance_support_reason'],''),lambda row,fields=fields:support_predicate(row,fields),lambda value:value is True)


def physical_pair_seed(tmp_path,monkeypatch):
    check=check_request()
    return {'saved':check,'current':deepcopy(check)}


def same_physical(row):
    from src.autoslice.acoustic_witness_adjudication import same_physical_target
    return same_physical_target(row['saved'],row['current'])


register('acoustic_witness_adjudication.same_physical_target','<FALSE>',physical_pair_seed,
         change(['current','source_media_timeline_offset_ms'],1),same_physical,lambda value:value is True)


def span_seed(tmp_path,monkeypatch):
    row=proposal_seed(tmp_path,monkeypatch)
    rebuilt,audit=proposal_consume(row)
    row['span']=[rebuilt['span_start_codepoint'],rebuilt['span_end_codepoint']]
    row['original_span']=list(row['span'])
    return row


def span_consume(row):
    from src.autoslice.missing_proposal_bootstrap import missing_proposal_answer_contract
    return missing_proposal_answer_contract('还没有歌杂呢').covers_suspect(*row['span'],row['original_span'],None)


register('missing_proposal_bootstrap.MissingProposalAnswerContract.covers_suspect','<FALSE>',span_seed,
         change(['span',1],0),span_consume,lambda value:value is True)


def keep_predicate(row):
    from src.autoslice.missing_proposal_bootstrap import proposal_convergence_contract
    return not proposal_convergence_contract('还没有歌杂呢').keep_requires_audio(row['answer'])


register('missing_proposal_bootstrap.MissingProposalAnswerContract.keep_requires_audio','<FALSE>',keep_seed,
         change(['answer','needs_audio'],True),keep_predicate,lambda value:value is True)


def same_pending(row):
    from src.autoslice.semantic_audio_recovery import _same_pending_audio_target
    return _same_pending_audio_target(row['saved'],row['current'])


register('semantic_audio_recovery._same_pending_audio_target','<FALSE>',physical_pair_seed,
         change(['current','current_cue'],'other'),same_pending,lambda value:value is True)


def sealed_predicate(row):
    from src.autoslice.exact_source_transcript_contract import _sealed
    return _sealed(row['observation'],'observation_sha256')


register('exact_source_transcript_contract._sealed','<FALSE>',sealed_exact_seed,
         change(['observation','exact_transcript'],'other'),sealed_predicate,lambda value:value is True)


def report_predicate(row):
    from src.autoslice.exact_source_transcript_contract import exact_transcript_response_contract
    return exact_transcript_response_contract().accepts_report(row['observed'])


register('exact_source_transcript_contract.ExactTranscriptResponseContract.accepts_report','<FALSE>',exact_seed,
         change(['observed','schema_version'],'other'),report_predicate,lambda value:value is True)


def timeline_predicate(row):
    from src.autoslice.exact_source_transcript_contract import _timeline_matches_request
    return _timeline_matches_request(row['request'],row['timeline'])


register('exact_source_transcript_contract._timeline_matches_request','<FALSE>',exact_prompt_seed,
         change(['timeline','source_media','target_start_ms'],0),timeline_predicate,lambda value:value is True)


def same_timeline_seed(tmp_path,monkeypatch):
    row=exact_prompt_seed(tmp_path,monkeypatch);row['other_timeline']=deepcopy(row['timeline'])
    return row


def same_timeline_predicate(row):
    from src.autoslice.exact_source_transcript_contract import _same_timeline
    return _same_timeline(row['timeline'],row['other_timeline'])


register('exact_source_transcript_contract._same_timeline','<FALSE>',same_timeline_seed,
         change(['other_timeline','source_media','target_start_ms'],0),same_timeline_predicate,lambda value:value is True)


def same_source_seed(tmp_path,monkeypatch):
    row=exact_seed(tmp_path,monkeypatch);row['saved_request']=deepcopy(row['request'])
    return row


def same_source_predicate(row):
    from src.autoslice.exact_source_transcript_contract import _same_physical_source_request
    return _same_physical_source_request(row['saved_request'],row['request'])


register('exact_source_transcript_contract._same_physical_source_request','<FALSE>',same_source_seed,
         change(['request','source_media_timeline_offset_ms'],1),same_source_predicate,lambda value:value is True)


def cue_available(row):
    from src.autoslice.final_review_schema_retry import finding_cue_contract
    return finding_cue_contract(row['count']).available(row['row']['cue'])


register('final_review_schema_retry.FindingCueContract.available','<FALSE>',cue_seed,change(['row','cue'],3),
         cue_available,lambda value:value is True)


def schema_repair_seed(tmp_path,monkeypatch):
    from src.autoslice.final_review_schema_retry import schema_repair_prompt
    row=proposal_seed(tmp_path,monkeypatch)
    row['original']='ordinary review prompt'
    row['repaired']=schema_repair_prompt(row['original'],'{"invalid_rows":[{"cue":1}]}')
    return row


def repair_prompt_predicate(row):
    from src.autoslice.final_review_schema_retry import is_schema_repair_prompt_for
    return is_schema_repair_prompt_for(row['repaired'],row['original'])


register('final_review_schema_retry.is_schema_repair_prompt_for','<FALSE>',schema_repair_seed,
         change(['original'],'unrelated prompt'),repair_prompt_predicate,lambda value:value is True)


def text_revision_seed(tmp_path,monkeypatch):
    from src.autoslice.context_assisted_audio import anchor
    row=assisted_seed(tmp_path,monkeypatch)
    row['recorded_anchor']=anchor(row['check'])
    return row


def text_revision_consume(row):
    from src.autoslice.context_assisted_audio import anchor,_text_only_revision
    return _text_only_revision(row['recorded_anchor'],anchor(row['check']),
                               row['check']['whole_clip_current_srt'],row['check'])


register('context_assisted_audio._text_only_revision','<FALSE>',text_revision_seed,
         change(['recorded_anchor','candidate_id'],'another-candidate'),text_revision_consume,
         lambda value:value is True)


def complete_proposal_materials(row):
    from src.autoslice.missing_proposal_bootstrap import proposal_audio_prompt_payload
    return not proposal_audio_prompt_payload(row['proposal_materials']).missing_from(row['prompt'])


def proposal_materials_seed(tmp_path,monkeypatch):
    row=bound_audio_seed(tmp_path,monkeypatch)
    row['proposal_materials']=json.loads(row['prompt'])
    return row


register('missing_proposal_bootstrap.ProposalAudioPromptPayload.missing_from','<FALSE>',proposal_materials_seed,
         change(['prompt'],'{}'),complete_proposal_materials,lambda value:value is True,
         'one material is omitted from the ordinary producer prompt')


def consumer_evidence_spec(row):
    from src.autoslice.argon_escalation_integration import _consumer_evidence_spec
    return _consumer_evidence_spec(row['check'], row['envelope'])


for code, field, value in (
    ('ARGON_CONSUMER_SCHEMA_INVALID', 'schema_version', 'other'),
    ('ARGON_CONSUMER_ENVELOPE_DRIFT', 'envelope_sha256', '0' * 64),
):
    register('argon_escalation_integration._consumer_evidence_spec', code,
             argon_seed, change(['envelope', field], value), consumer_evidence_spec)


def typed_word_materials_seed(tmp_path, monkeypatch):
    from tests.test_wpb_live_wiring import bound
    from tests.lidousha.test_acoustic_witness_adjudication import _semantic_cache_witness
    from src.autoslice.acoustic_witness_adjudication import word_choice_contract
    check, witness = bound(), _semantic_cache_witness()
    contract = word_choice_contract(llm_call=lambda _: '{}', check_request=check, witness=witness)
    return dict(check_request=check, witness=witness, context_before='', context_after='',
        structured_chat_context='', model_identity={'transport': 'fixture', 'models': ['bounded'], 'effort': 'high'},
        decision_contract=contract.decision_contract, allowed_choices=contract.choices,
        candidates=contract.candidates, require_complete_support=contract.require_complete_support,
        dependencies=contract.dependencies)


def typed_word_materials_consume(row):
    from src.autoslice.acoustic_witness_adjudication import _semantic_fingerprint_payload
    return _semantic_fingerprint_payload(**row)


register('acoustic_witness_adjudication._semantic_fingerprint_payload',
         'WORD_CHOICE_MATERIAL_DEPENDENCIES_REQUIRED', typed_word_materials_seed,
         change(['dependencies'], None), typed_word_materials_consume,
         description='remove the authority views consumed by the ordinary typed word prompt')
