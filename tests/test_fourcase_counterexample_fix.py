"""Source-bound real OFF counterexamples, plus ordinary public-path controls.

Set VTUBER_COUNTEREXAMPLE_REPORT to the private frozen fixture report directory.
Provider replies below are original bytes, not human truth or simulated gate PASS.
"""

from src.autoslice.discovery_channels import batch_from_rows
import copy
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys

import pytest

from src.autoslice.acoustic_witness_adjudication import build_witness_request, judge_word_choice
from src.autoslice.final_review_witness_dispatch import dispatch_context_witness
from src.autoslice.final_review_auditor import build_context_adjudication_request, persist_review_audit
from src.autoslice.acoustic_witness_availability import pending_acoustic_witness


@pytest.fixture(autouse=True)
def offline(_hermetic_autoslice_base, monkeypatch, tmp_path):
    monkeypatch.setenv('AUTOSLICE_BASE', str(tmp_path/'runtime'))
    def no(*args, **kwargs):
        raise AssertionError('No real model/network in recorded counterexample checks')
    monkeypatch.setattr(socket.socket, 'connect', no)
    monkeypatch.setattr(socket, 'create_connection', no)


@pytest.fixture
def real_five():
    root = os.environ.get('VTUBER_COUNTEREXAMPLE_REPORT')
    if root is None:
        pytest.skip('Private recorded OFF fixtures explicitly required')
    path = Path(root)/'REAL-TEXT-FIRST-FIXTURES.json'
    raw = path.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == 'b48a70f89e628f9378baac79e7a31df5c52483ce00889dc067b99ef8fe0461c2'
    return json.loads(raw)


@pytest.mark.parametrize('index', range(5))
def test_real_text_first_dispatch_persistence_and_cold_consumer(real_five, index, tmp_path):
    row = real_five[index]
    request = copy.deepcopy(row['original_check_request'])
    original = row['original_judge']
    completion = original['span_review_raw_completion']
    assert hashlib.sha256(completion.encode()).hexdigest() == original['completion_sha256']
    calls = []
    def llm(prompt):
        calls.append(prompt)
        return completion
    def fetch(req):
        witness_request = build_witness_request(req)
        return pending_acoustic_witness(witness_request), witness_request
    response = dispatch_context_witness(
        request=request, convergence=None, entity_verifier=None,
        judge_llm_call=llm, structured_chat_context='', fetch_witness=fetch,
        judge_word_choice=judge_word_choice,
    )
    fresh = response[3]
    assert len(calls) == 1 and fresh['span_review_raw_completion'] == completion
    assert fresh['check_request_sha256'] == original['check_request_sha256']
    assert fresh['needs_audio'] == original['needs_audio']
    outcome = fresh.get('span_compilation')
    assert outcome is not None, 'retained text_first_judge missed the common compilation boundary'
    assert outcome['blocked_reason'] and outcome['resolution'] is None  # No acoustic evidence here.
    assert outcome['request']['request_sha256'] == original['check_request_sha256']
    saved = tmp_path/'dispatch.json'
    persist_review_audit(saved, {'text_first_judge': fresh})
    script = '''import json,sys,socket
from pathlib import Path
from src.autoslice.span_phoneme_resolution import validate_persisted_judge_scopes
socket.socket.connect=lambda *a,**k:(_ for _ in ()).throw(AssertionError('network'))
p=Path(sys.argv[1]);before=p.read_bytes();value=json.loads(before)
assert validate_persisted_judge_scopes(value)>=1
removed=json.loads(before);removed['text_first_judge'].pop('span_compilation')
try: validate_persisted_judge_scopes(removed)
except ValueError: pass
else: raise AssertionError('required scope result removed')
value['text_first_judge']['span_compilation']['request']['matched_start_ms']+=1
try: validate_persisted_judge_scopes(value)
except ValueError: pass
else: raise AssertionError('tampered source clock accepted')
assert p.read_bytes()==before
print('COLD_SCOPE_VALIDATED_AND_DRIFT_REJECTED')
'''
    proc = subprocess.run([sys.executable, '-B', '-c', script, str(saved)], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, (proc.stdout, proc.stderr)


@pytest.mark.parametrize('current,proposed,span', [
    ('当然要就是喜欢这种年、年上凌云', '当然要就是喜欢这种年、年上零', [13, 15]),
    ('他说，他说门都得给你起飞的时候', '他说，他说メンタル的にきついですか？', [5, 15]),
])
def test_coarse_proposal_enters_public_request_as_minimal_change(current, proposed, span):
    text = f'1\n00:00:01,000 --> 00:00:04,000\n{current}\n'
    finding = {'cue_index':1,'kind':'context','suspect':current,'suggestion':proposed,
               'proposed_full_cue':proposed,'span_start_codepoint':0,'span_end_codepoint':len(current),
               'repair_class':'phonetic','why':'recorded failure shape'}
    q = build_context_adjudication_request(text, finding)
    assert q['edit_span_binding']['codepoint_span'] == span
    assert q['current_cue'] == current and q['proposed_cue'] == proposed
    assert q['span_localization_contract'] == 'located-or-explicitly-unlocatable.v1'


def test_formal_terms_use_existing_display_owner_without_global_zero_replacement():
    from src.autoslice.surface_canon import normalize_caption_display_surfaces
    text = '1\n00:00:01,000 --> 00:00:04,000\n姐0和年上0，数字0、10、凌云、姐01、年上0A都不改\n'
    output, audit = normalize_caption_display_surfaces(text)
    assert output == text.replace('姐0和年上0', '姐零和年上零')
    assert audit['decision_authority'] == 'CAPTION_DISPLAY_CANON'


def boundary_fixture(tmp_path, name):
    """Use the three real interval/provenance shapes with synthetic local bytes.
    No assertion below labels the synthetic media as the real recording.
    """
    report = os.environ.get('VTUBER_COUNTEREXAMPLE_REPORT')
    if report is None:
        pytest.skip('Private real boundary fixtures explicitly required')
    saved = json.loads((Path(report)/'BOUNDARY-INPUTS.json').read_bytes())['packages'][name]
    proof = copy.deepcopy(saved['recut_provenance']['content'])
    source = tmp_path/'recording.flv';source.write_bytes(b'synthetic original recording')
    piece = tmp_path/'piece.mp4';piece.write_bytes(b'synthetic padded media')
    padded = tmp_path/'padded.mp4';padded.write_bytes(piece.read_bytes())
    recut = tmp_path/'recut.mp4';recut.write_bytes(b'synthetic final physical crop')
    sha = lambda p: hashlib.sha256(p.read_bytes()).hexdigest()
    p = proof['source_piece']
    p.update(source_path=str(source), source_sha256=sha(source), source_media_binding='sha256:'+sha(source),
             output_path=str(piece), output_sha256=sha(piece))
    piece.with_suffix('.provenance.json').write_text(json.dumps(p))
    proof['padded'].update(output_path=str(padded), output_sha256=sha(padded),
                           inputs=[{'path':str(piece),'sha256':sha(piece)}])
    padded.with_suffix('.provenance.json').write_text(json.dumps(proof['padded']))
    proof['final_recut'].update(source_path=str(padded), source_sha256=sha(padded),
                                output_path=str(recut), output_sha256=sha(recut))
    recut.with_suffix('.provenance.json').write_text(json.dumps(proof))
    context = {'candidate_id':saved['record']['content']['candidate_id'],
               'pieces':[{'source_media_sha256':sha(source)}]}
    return saved, proof, source, recut, context


@pytest.mark.parametrize('name', ['sister','guimi','stomach'])
@pytest.mark.parametrize('damage', [None,'missing_provenance','source_sha','wrong_origin'])
def test_real_intervals_through_prepared_constructor_without_second_audio_offset(
    tmp_path, monkeypatch, name, damage,
):
    from types import SimpleNamespace
    from src.autoslice import producer_text_pipeline as pipeline, producer_media
    from src.autoslice.boundary_semantic_review import _source_separation_witness
    saved, proof, source, recut, context = boundary_fixture(tmp_path, name)
    record = saved['record']['content']
    record['subtitle_path'] = str(tmp_path/'original.srt')
    Path(record['subtitle_path']).write_text('1\n00:00:00,000 --> 00:00:01,000\nfixture\n')
    interval = [proof['final_recut']['start_ms'],proof['final_recut']['end_ms']]
    duration = interval[1]-interval[0]
    if damage=='missing_provenance':recut.with_suffix('.provenance.json').unlink()
    elif damage=='source_sha':source.write_bytes(b'changed source of the same synthetic name')
    elif damage=='wrong_origin':
        # Equal duration alone is insufficient; original source endpoints bind.
        proof['final_recut']['start_ms']+=1;proof['final_recut']['end_ms']+=1
        proof['final_recut']['absolute_source_start_ms']+=1;proof['final_recut']['absolute_source_end_ms']+=1
        recut.with_suffix('.provenance.json').write_text(json.dumps(proof))
    class ConstructorCaptured(Exception):
        def __init__(self, values):self.values=values
    def capture(**kwargs):raise ConstructorCaptured(kwargs)
    monkeypatch.setattr(pipeline, '_build_exact_final_review_callback', capture)
    monkeypatch.setattr(pipeline, '_build_entity_verification_context',
                        lambda **kw: SimpleNamespace(verify_confusable_entity=lambda req:{}))
    monkeypatch.setattr(producer_media, 'ffprobe_duration_ms', lambda _:duration)
    kwargs=dict(spec={'clip_context':context},out_root=tmp_path,cid=context['candidate_id'],padded=recut,
                host='localhost',authority={'final_review_audit':{}},completed_record=record,
                adapters=None,native_audio_state=None)
    if damage:
        with pytest.raises(ValueError,match='PREPARED_BOUNDARY'):
            reviewer=pipeline.build_prepared_exact_final_reviewer(**kwargs)
            reviewer(Path(record['subtitle_path']).read_text(),{},0,duration)
        return
    reviewer=pipeline.build_prepared_exact_final_reviewer(**kwargs)
    with pytest.raises(ConstructorCaptured) as raised:
        reviewer(Path(record['subtitle_path']).read_text(),{},0,duration)
    # Capture only construction args: no final gate is mocked to return PASS.
    values=raised.value.values
    assert values.get('source_boundary_timeline_offset_ms')==interval[0]
    original=record['boundary_audit']['boundary_semantic_review']
    assert _source_separation_witness(original,source_final_start_ms=interval[0],
                                      source_final_end_ms=duration+interval[0])['status']=='PASS'
    for bad in [0,interval[0]+1]:
        assert _source_separation_witness(original,source_final_start_ms=bad,
                                          source_final_end_ms=duration+bad)['status']=='BLOCK'


@pytest.mark.parametrize('name,cue', [('sister',24),('sister',11),('stomach',15)])
def test_original_retry_history_new_question_without_reset(tmp_path, monkeypatch, name, cue):
    """Copy every real issue record unchanged; only the new transport is faked."""
    import shutil
    from src.autoslice.acoustic_witness_adjudication import adjudicate_with_witness
    from src.autoslice.independent_context_adjudication import bind_request
    from src.autoslice.semantic_retry_progress import SemanticAttemptRecorder
    report = os.environ.get('VTUBER_COUNTEREXAMPLE_REPORT')
    if report is None:pytest.skip('Private original issue histories required')
    report=Path(report);data=report.parent/'fourcase-consumer/readback-off-848fbd3/files/fourcase-off'
    context=json.loads((report/'SOURCE-CONTEXTS.json').read_bytes())[name]['content']
    run=json.loads((data/name/'run.json').read_bytes())
    finding=next(f for f in run['reviews'][-1]['findings']
                 if f['cue_index']==cue and f.get('proposed_full_cue'))
    original=finding['exact_release_adjudication']
    assert (original['witness_judge']['judge'].get('failure_layer')=='LOCAL_RETRY_ADMISSION')
    text=(data/name/'final.srt').read_text()
    recorder=SemanticAttemptRecorder(srt_text=text,finding=finding,clip_context=context,offset_ms=0)
    original_dir=data/'runtime-state/cache/semantic-cpa-attempts'/recorder.directory.name
    originals={p.name:p.read_bytes() for p in original_dir.glob('*.json')}
    assert len(originals)>=4
    recorder.directory.parent.mkdir(parents=True,exist_ok=True)
    shutil.copytree(original_dir,recorder.directory)
    old_judges=data/'runtime-state/cache/judge-verdicts'
    copied_judges=recorder.directory.parent.parent/'judge-verdicts'
    shutil.copytree(old_judges,copied_judges)
    judge_originals={str(p.relative_to(old_judges)):p.read_bytes() for p in old_judges.rglob('*.json')}
    request=build_context_adjudication_request(text,finding,clip_context=context,
                                               require_complete_utterance_support=True)
    # A new ordinary request retains original hearing/runtime enrichment; it
    # never mutates or re-seals the old stored request's already bound span.
    extra={k:copy.deepcopy(v) for k,v in original['request'].items() if k not in request}
    request=bind_request(request,**extra)
    witness=copy.deepcopy(original['verdict'])
    calls=[]
    def transport(prompt):
        calls.append(prompt)
        return '{"choice":"NEITHER","needs_audio":true,"reason":"Explicit fixed provider fixture; still unlocalized."}'
    # Preserve the real model/cache identity; only its transport function is
    # replaced. Otherwise the fixture would evade the very caches under test.
    transport.cpa_cache_identity=json.loads(next(iter(originals.values())))['request']['model']
    repaired,branch,audit=adjudicate_with_witness(check_request=request,witness=witness,
                                               llm_call=recorder.wrap(transport),clip_context=context)
    assert len(calls)==1,(recorder.audit(),audit)
    assert not repaired
    assert audit['judge']['span_compilation']['blocked_reason']=='SPAN_LOCALIZATION_RESULT_REQUIRED'
    assert recorder.plan is not None and recorder.material['scoped_continuation_basis']
    for file,raw in originals.items():assert (recorder.directory/file).read_bytes()==raw
    for file,raw in judge_originals.items():assert (copied_judges/file).read_bytes()==raw
    assert len(list(recorder.directory.glob('*.json')))==len(originals)+1
    cold=SemanticAttemptRecorder(srt_text=text,finding={k:v for k,v in finding.items() if k!='continuation_plan'},
                                clip_context=context,offset_ms=0)
    reworded=bind_request(request,reason='Only changing controller wording is not evidence')
    repaired,branch,last=adjudicate_with_witness(check_request=reworded,witness=witness,
                                               llm_call=cold.wrap(transport),clip_context=context)
    assert len(calls)==1 and not repaired
    assert last['judge']['status'] in {'JUDGE_NO_NEW_EVIDENCE','JUDGE_UNAVAILABLE'}
    assert len(list(recorder.directory.glob('*.json')))==len(originals)+1


@pytest.mark.parametrize('name,cue', [('sister',24),('stomach',34)])
def test_real_adverse_edits_remain_in_current_public_review_after_empty_discovery(tmp_path,name,cue):
    from src.autoslice.producer_text_pipeline import _pending_current_exact_findings
    from src.autoslice.final_review_auditor import audit_final_subtitles
    from src.autoslice.producer_package_finalization import _postburn_current_risk_partition
    report=os.environ.get('VTUBER_COUNTEREXAMPLE_REPORT')
    if report is None:pytest.skip('Private adverse edit original records required')
    data=Path(report).parent/'fourcase-consumer/readback-off-848fbd3/files/fourcase-off'
    authority=json.loads((data/name/'authority.json').read_bytes())
    text=(data/name/'final.srt').read_text()
    candidates=_pending_current_exact_findings(text,authority)
    assert cue in {r['cue'] for r in candidates}
    # New discovery says no new issue; original typed history/critical carryover
    # must still receive ordinary current risk review, not a fabricated gate.
    calls=[]
    def model(prompt):
        calls.append(prompt)
        if len(calls)==1:return '{"findings":[]}'
        agenda=json.loads(prompt.rsplit('\n',1)[-1])
        return json.dumps({'findings':[{'cue':r['cue'],'content_risk':'meaning_reversal',
            'risk_basis':'Recorded conflict between actual before/after text and retained hearing still unresolved.'}
            for r in agenda]})
    findings=audit_final_subtitles(text,llm_call=model,extract_json=json.loads,
                                  discovery_batch=batch_from_rows(candidates))
    assert cue in {r['cue_index'] for r in _postburn_current_risk_partition(findings)[0]}
    path=tmp_path/'current-review.json';persist_review_audit(path,{'findings':findings})
    reread=json.loads(path.read_bytes())
    assert cue in {r['cue_index'] for r in reread['findings']}


def test_real_sister_display_enters_public_gate_with_original_typed_history(tmp_path):
    from src.autoslice.producer_package_finalization import CurrentTextReviewArtifacts, _run_exact_final_review_gate
    from src.autoslice.final_review_contract import _validate_exact_final_cpa_self_heal
    report=os.environ.get('VTUBER_COUNTEREXAMPLE_REPORT')
    if report is None:pytest.skip('Private real typed history required')
    data=Path(report).parent/'fourcase-consumer/readback-off-848fbd3/files/fourcase-off/sister'
    before=(data/'final.srt').read_text();authority=json.loads((data/'authority.json').read_bytes())
    original=copy.deepcopy(authority['exact_final_cpa_self_heal']['passes'])
    srt=tmp_path/'final.srt';srt.write_text(before)
    path=tmp_path/'authority.json';path.write_text(json.dumps(authority))
    class ActualReviewBoundaryReached(Exception):pass
    def reviewer(text,audit,start,end):
        assert '你想姐零啊' in text and '年上凌云' in text
        assert start==0 and end==64680
        assert audit['exact_final_cpa_self_heal']['passes']==original
        raise ActualReviewBoundaryReached()
    with pytest.raises(ActualReviewBoundaryReached):
        _run_exact_final_review_gate(cid='auto_200311_218_282',out_root=tmp_path,
            final_start=0,final_end=64680,recut=CurrentTextReviewArtifacts(srt,None,None),
            chat_authority_audit=authority,chat_authority_path=path,run_exact_final_review=reviewer)
    actual=json.loads(path.read_bytes())
    assert actual['exact_final_cpa_self_heal']['status']=='REVIEW_PENDING'
    assert len(actual['exact_final_cpa_self_heal']['caption_display_successors'])==1
    _validate_exact_final_cpa_self_heal(actual,
        expected_srt_sha256='sha256:'+hashlib.sha256(srt.read_bytes()).hexdigest(),allow_pending=True)
    assert srt.read_text()==before.replace('你想姐0啊','你想姐零啊')
    assert actual['final_review_audit']['release_gate']=='BLOCK'


@pytest.mark.parametrize('name,cue,index', [('sister',11,16),('stomach',15,11)])
@pytest.mark.parametrize('fixture_needs_audio', [True,False])
def test_real_missing_scope_reply_cannot_become_invented_localized_authority(
    tmp_path,name,cue,index,fixture_needs_audio,
):
    from src.autoslice.acoustic_witness_adjudication import adjudicate_with_witness
    from src.autoslice.independent_context_adjudication import bind_request
    report=os.environ.get('VTUBER_COUNTEREXAMPLE_REPORT')
    if report is None:pytest.skip('Private original target replies required')
    report=Path(report);data=report.parent/'fourcase-consumer/readback-off-848fbd3/files/fourcase-off'
    texts=json.loads((report.parent/'fourcase-consumer/terminal-analysis-20261017/TARGET-RAW-CPA.json').read_bytes())
    recorded=next(row for row in texts if row['case']==f'{name}-cue{cue}')
    raw=recorded['completion']
    assert hashlib.sha256(raw.encode()).hexdigest()==recorded['completion_sha256']
    assert 'span_review' not in json.loads(raw)
    first=json.loads((data/name/'review-01.json').read_bytes())
    finding=first['findings'][index];original=finding['exact_release_adjudication']
    text=(data/name/'review-input-01.srt').read_text()
    context=json.loads((report/'SOURCE-CONTEXTS.json').read_bytes())[name]['content']
    q=build_context_adjudication_request(text,finding,clip_context=context,
                                         require_complete_utterance_support=True)
    q=bind_request(q,**{k:copy.deepcopy(v) for k,v in original['request'].items() if k not in q})
    if fixture_needs_audio is False:
        # Additional low-level synthetic adversarial reply, NOT the original:
        # a false flag without located evidence cannot authorize a repair.
        payload=json.loads(raw);payload['needs_audio']=False;raw=json.dumps(payload,ensure_ascii=False)
    calls=[]
    def response(prompt):calls.append(prompt);return raw
    repaired,branch,audit=adjudicate_with_witness(check_request=q,witness=original['verdict'],
                                               llm_call=response,clip_context=context)
    assert len(calls)==1 and not repaired
    judge=audit['judge']
    assert judge['needs_audio'] is fixture_needs_audio and 'span_review' not in judge
    assert judge['span_review_raw_completion']==raw
    assert judge['span_compilation']['blocked_reason']=='SPAN_LOCALIZATION_RESULT_REQUIRED'
    assert judge['span_compilation']['resolution'] is None
