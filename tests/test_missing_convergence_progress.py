import json
import pytest

from src.autoslice.final_review_auditor import _derive_single_span_edit
from src.autoslice.missing_proposal_bootstrap import converge_missing_proposal


@pytest.mark.parametrize('stage',['bootstrap','converge'])
@pytest.mark.parametrize('with_plan',[False,True])
def test_local_retry_gate_is_not_reported_as_a_provider_or_json_failure(tmp_path,monkeypatch,stage,with_plan):
    from src.autoslice.semantic_retry_progress import SemanticAttemptRecorder
    from src.autoslice.missing_proposal_bootstrap import bootstrap_missing_proposal
    monkeypatch.setenv('AUTOSLICE_BASE',str(tmp_path))
    finding={'cue_index':1,'suspect':'来吧来人放那','why':'旧问题未定位词界'}
    context={'source_media_sha256':'a'*64}
    calls=[]
    call=call_factory(calls)
    recorder=SemanticAttemptRecorder(srt_text=SRT,finding=finding,clip_context=context,offset_ms=0)
    wrapped=recorder.wrap(call)
    for question in ('按中文昵称判断','按日文歌词判断','按句尾判断'):
        wrapped(question)
    assert len(calls)==3
    if with_plan:
        finding['continuation_plan']={'action':'correct_localization',
            'question':'区分垫片邻句和目标句的词界','why_previous_failed':'把邻句词并入了目标',
            'expected_gain':'只保留目标句对应的提案','verification':'核对已存目标markers与词时间'}
        wrapped=SemanticAttemptRecorder(srt_text=SRT,finding=finding,clip_context=context,offset_ms=0).wrap(call)
    kwargs=dict(srt_text=SRT,finding=finding,structured_chat='',llm_call=wrapped,
        derive_single_span_edit=_derive_single_span_edit)
    if stage=='converge':
        _,audit=converge_missing_proposal(**kwargs,clip_context=context,proposal_audit={'status':'UNRESOLVED'})
    else:
        _,audit=bootstrap_missing_proposal(**kwargs)
    if with_plan:
        assert len(calls)==4 and audit['status']=='UNRESOLVED'
    else:
        assert len(calls)==3 and audit['status']=='RETRY_BLOCKED'
        assert audit['reason_code']=='CONTINUATION_VALUE_PLAN_REQUIRED'
        assert audit['provider_calls_this_attempt']==0 and audit['dispatch_outcome']=='NOT_DISPATCHED'
        assert 'completion_sha256' not in audit

SRT = '1\n00:00:00,000 --> 00:00:01,000\n来吧来人放那\n'


def call_factory(calls, *, model='test-model'):
    def call(prompt):
        calls.append(prompt)
        return json.dumps({'decision': 'UNRESOLVED', 'needs_audio': True,
                           'replacement_text': '', 'reason': '原音仍有词界分歧'})
    call.cpa_cache_identity = {'transport': 'test', 'models': [model], 'effort': 'high'}
    return call


def run(call, *, why='先前发现', **kwargs):
    args = dict(srt_text=SRT, finding={'cue_index': 1, 'suspect': '来吧来人放那', 'why': why},
                structured_chat='', clip_context={'source_sha256': 'a' * 64},
                proposal_audit={'status': 'UNRESOLVED', 'reason_code': 'NO_CANDIDATE'},
                derive_single_span_edit=_derive_single_span_edit, llm_call=call)
    args.update(kwargs)
    return converge_missing_proposal(**args)


def test_exact_question_restart_and_controller_rephrasing_reuse_raw(tmp_path, monkeypatch):
    monkeypatch.setenv('AUTOSLICE_BASE', str(tmp_path))
    calls = []
    _, first = run(call_factory(calls))
    paths = list((tmp_path / 'cache/missing-convergence-no-progress').glob('*.json'))
    assert len(paths) == 1
    original = paths[0].read_bytes()
    _, again = run(call_factory(calls))
    assert len(calls) == 1
    assert again['status'] == 'UNRESOLVED'
    assert again['needs_audio'] is True and again['mutation_authorized'] is False
    assert again['prompt_sha256'] == first['prompt_sha256']
    assert again['completion_sha256'] == first['completion_sha256']
    assert again['no_new_evidence']['status'] == 'SAME_QUESTION_REPLAY'
    assert paths[0].read_bytes() == original
    run(call_factory(calls), why='重新按日语歌词定位，不再按中文昵称提问')
    assert len(calls) == 1


def test_new_source_clock_model_or_observation_makes_new_decision(tmp_path, monkeypatch):
    monkeypatch.setenv('AUTOSLICE_BASE', str(tmp_path))
    calls = []
    call = call_factory(calls)
    run(call)
    run(call, source_media_timeline_offset_ms=100)
    run(call, clip_context={'source_sha256': 'b' * 64})
    run(call, exact_acoustic_observations=({'observation_sha256': 'c' * 64, 'transcript': '新听写'},))
    run(call_factory(calls, model='different-model'))
    assert len(calls) == 5


def test_corruption_does_not_retry_or_promote(tmp_path, monkeypatch):
    monkeypatch.setenv('AUTOSLICE_BASE', str(tmp_path))
    calls = []
    call = call_factory(calls)
    run(call)
    p = next((tmp_path / 'cache/missing-convergence-no-progress').glob('*.json'))
    data = json.loads(p.read_text());data['completion'] = '{}';p.write_text(json.dumps(data))
    rebuilt, audit = run(call)
    assert rebuilt is None and audit['status'] == 'UNAVAILABLE'
    assert audit['reason_code'] == 'MISSING_CONVERGENCE_PROGRESS_CACHE_INVALID'
    assert len(calls) == 1


def test_resolved_result_is_not_semantically_rebound(tmp_path, monkeypatch):
    monkeypatch.setenv('AUTOSLICE_BASE', str(tmp_path))
    calls=[]
    def call(prompt):
        calls.append(prompt)
        return json.dumps({'decision': 'KEEP_EXISTING', 'needs_audio': False, 'replacement_text': '', 'reason': '完整当前句支持'})
    call.cpa_cache_identity = {'transport': 'test', 'models': ['test-model']}
    run(call)
    run(call, why='一个新发现')
    assert len(calls) == 2
    assert not list((tmp_path / 'cache/missing-convergence-no-progress').glob('*.json'))
